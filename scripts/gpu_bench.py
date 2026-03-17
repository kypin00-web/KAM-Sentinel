#!/usr/bin/env python3
"""
KAM GPU Bench  v1.0
Standalone headless GPU stress test (OpenGL 3.3 via ModernGL).

Phases:
  1. Fill Rate      — fullscreen quad, heavy multi-blend fragment shader
  2. Shader         — 32-iteration trig/sqrt chain + texture + lighting
  3. VRAM Bandwidth — 2K ping-pong FBO blit (internal VRAM stress)

Writes progress to %TEMP%/kam_bench_progress.json every 2 s.
Writes final results to %TEMP%/kam_bench_results.json on completion.
Exits with code 2 on thermal abort, code 1 on error.

Usage:
  KAM_GPU_Bench.exe [--tier quick|standard|extended] [--run-id ID]
"""

import argparse, json, os, struct, subprocess, sys, tempfile, time

# ── Progress file paths ────────────────────────────────────────────────────────
_TMP          = tempfile.gettempdir()
PROGRESS_FILE = os.path.join(_TMP, 'kam_bench_progress.json')
RESULTS_FILE  = os.path.join(_TMP, 'kam_bench_results.json')

# ── Safety ────────────────────────────────────────────────────────────────────
TEMP_LIMIT_C = 90.0

# ── Tier durations (seconds per phase) ────────────────────────────────────────
TIER_DURATIONS = {
    'quick':    {'fill': 20,  'shader': 30,  'bandwidth': 20},
    'standard': {'fill': 60,  'shader': 90,  'bandwidth': 60},
    'extended': {'fill': 120, 'shader': 180, 'bandwidth': 120},
}

# ── Render resolution ─────────────────────────────────────────────────────────
RENDER_W, RENDER_H = 1920, 1080
BW_SIZE            = 2048  # ping-pong FBO side length

# ── Scoring calibration ────────────────────────────────────────────────────────
# Reference values calibrated so RTX 3080 ≈ 83,000.
# Caps ensure RTX 4090-class GPUs approach 100,000.
# NOTE: these are design targets; fine-tune after hardware testing.
FILL_REF_FPS   = 8_000      # RTX 3080 fill phase FPS @ 1080p
SHADER_REF_FPS = 1_500      # RTX 3080 shader phase FPS @ 1080p
BW_REF_GBPS    = 448.0      # RTX 3080 effective ping-pong BW (≈60 % of 760 GB/s)

FILL_MAX_PTS   = 40_000     # 40 % weight
SHADER_MAX_PTS = 35_000     # 35 % weight
BW_MAX_PTS     = 25_000     # 25 % weight

# RTX 3080 contributions (83,000 × weight)
FILL_REF_PTS   = 33_200
SHADER_REF_PTS = 29_050
BW_REF_PTS     = 20_750


# ── GLSL sources ──────────────────────────────────────────────────────────────
_VERT = """
#version 330
in  vec2 in_vert;
out vec2 v_uv;
void main() {
    v_uv = in_vert * 0.5 + 0.5;
    gl_Position = vec4(in_vert, 0.0, 1.0);
}
"""

# Phase 1 — 8-layer blend cascade inside the shader (heavy ALU + ROP)
_FRAG_FILL = """
#version 330
in  vec2  v_uv;
out vec4  fragColor;
uniform float u_time;
void main() {
    vec4 c = vec4(0.4, 0.4, 0.4, 1.0);
    for (int i = 0; i < 8; i++) {
        float fi = float(i) * 0.785398;
        vec4 s = vec4(
            sin(fi + u_time)        * 0.5 + 0.5,
            cos(fi * 1.3 + u_time)  * 0.5 + 0.5,
            sin(fi * 0.7 + 1.0)     * 0.5 + 0.5,
            0.5 + 0.5 * sin(fi * 2.0 + u_time));
        c = mix(c, s, s.a * 0.5);
    }
    c.rgb *= c.a + 0.1;
    fragColor = clamp(c + vec4(0.005), 0.0, 1.0);
}
"""

# Phase 2 — 32-iteration trig/sqrt + texture + per-pixel lighting
_FRAG_SHADER = """
#version 330
in  vec2      v_uv;
out vec4      fragColor;
uniform float     u_time;
uniform sampler2D u_tex;
void main() {
    float v = 0.0;
    for (int i = 0; i < 32; i++) {
        float fi = float(i);
        v += sin(v_uv.x * fi * 6.28318 + u_time) *
             cos(v_uv.y * fi * 4.18879 + u_time * 0.73);
        v += sqrt(abs(sin(fi * 1.61803 + v * 0.1)));
    }
    vec4 tex  = texture(u_tex, fract(v_uv + v * 0.005));
    vec2 p    = v_uv * 2.0 - 1.0;
    float len = dot(p, p);
    vec3 N    = normalize(vec3(p, sqrt(max(0.001, 1.0 - len))));
    vec3 L    = normalize(vec3(cos(u_time * 0.5), sin(u_time * 0.7), 1.0));
    vec3 H    = normalize(L + vec3(0.0, 0.0, 1.0));
    float diff = max(dot(N, L), 0.0);
    float spec = pow(max(dot(N, H), 0.0), 64.0);
    fragColor  = vec4(tex.rgb * diff + vec3(spec * 0.4) + abs(v) * 0.0005, 1.0);
}
"""

# Phase 3 — simple blit with tiny UV jitter (prevents full cache hits)
_FRAG_BLIT = """
#version 330
in  vec2      v_uv;
out vec4      fragColor;
uniform sampler2D u_tex;
uniform float     u_jitter;
void main() {
    fragColor = texture(u_tex, fract(v_uv + vec2(u_jitter * 0.0001)));
}
"""


# ── Utilities ─────────────────────────────────────────────────────────────────
def _get_gpu_info():
    """Return (temp_c, clock_mhz) from nvidia-smi, or (None, None)."""
    try:
        flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
        r = subprocess.run(
            ['nvidia-smi',
             '--query-gpu=temperature.gpu,clocks.current.graphics',
             '--format=csv,noheader,nounits'],
            capture_output=True, text=True, timeout=3,
            creationflags=flags,
        )
        if r.returncode == 0:
            parts = r.stdout.strip().split(',')
            if len(parts) >= 2:
                return float(parts[0].strip()), int(parts[1].strip())
    except Exception:
        pass
    return None, None


def _write_progress(phase, phase_pct, overall_pct, score,
                    peak_temp, avg_clock, fps, elapsed, status, reason=None):
    data = {
        'phase':            phase,
        'phase_progress':   max(0, min(100, round(phase_pct))),
        'overall_progress': max(0, min(100, round(overall_pct))),
        'current_score':    int(score),
        'peak_temp_c':      round(peak_temp, 1) if peak_temp is not None else None,
        'avg_clock_mhz':    int(avg_clock)      if avg_clock is not None  else None,
        'fps':              round(fps, 1),
        'elapsed_s':        int(elapsed),
        'status':           status,
    }
    if reason:
        data['abort_reason'] = reason
    tmp = PROGRESS_FILE + '.tmp'
    try:
        with open(tmp, 'w') as f:
            json.dump(data, f)
        os.replace(tmp, PROGRESS_FILE)
    except Exception:
        pass


def _calc_score(fill_fps, shader_fps, bw_gbps):
    fill_pts   = min((fill_fps   / FILL_REF_FPS)   * FILL_REF_PTS,   FILL_MAX_PTS)
    shader_pts = min((shader_fps / SHADER_REF_FPS) * SHADER_REF_PTS, SHADER_MAX_PTS)
    bw_pts     = min((bw_gbps    / BW_REF_GBPS)    * BW_REF_PTS,     BW_MAX_PTS)
    return int(fill_pts + shader_pts + bw_pts), int(fill_pts), int(shader_pts), int(bw_pts)


def _avg(samples):
    return int(sum(samples) / len(samples)) if samples else None


# ── Main benchmark ─────────────────────────────────────────────────────────────
def run_bench(tier: str, run_id: str) -> dict:
    # Import here so we get a clean error if moderngl is missing
    try:
        import moderngl
    except ImportError as exc:
        msg = f'ModernGL not installed — cannot run GPU stress test ({exc})'
        _write_progress('error', 0, 0, 0, None, None, 0, 0, 'error', msg)
        sys.exit(1)

    try:
        ctx = moderngl.create_standalone_context()
    except Exception as exc:
        msg = f'OpenGL context creation failed: {exc}'
        _write_progress('error', 0, 0, 0, None, None, 0, 0, 'error', msg)
        sys.exit(1)

    # Require OpenGL 3.3+
    if ctx.version_code < 330:
        major, minor = ctx.version_code // 100, (ctx.version_code % 100) // 10
        msg = f'OpenGL 3.3 required; detected {major}.{minor}'
        _write_progress('error', 0, 0, 0, None, None, 0, 0, 'error', msg)
        ctx.release()
        sys.exit(1)

    durations = TIER_DURATIONS[tier]
    total_dur = sum(durations.values())

    # ── Shared geometry (triangle strip: BL BR TL TR) ─────────────────────────
    vbo = ctx.buffer(struct.pack('8f', -1.0, -1.0,  1.0, -1.0,  -1.0, 1.0,  1.0, 1.0))

    # ── Programs ──────────────────────────────────────────────────────────────
    prog_fill   = ctx.program(vertex_shader=_VERT, fragment_shader=_FRAG_FILL)
    prog_shader = ctx.program(vertex_shader=_VERT, fragment_shader=_FRAG_SHADER)
    prog_blit   = ctx.program(vertex_shader=_VERT, fragment_shader=_FRAG_BLIT)

    vao_fill   = ctx.vertex_array(prog_fill,   [(vbo, '2f', 'in_vert')])
    vao_shader = ctx.vertex_array(prog_shader, [(vbo, '2f', 'in_vert')])
    vao_blit   = ctx.vertex_array(prog_blit,   [(vbo, '2f', 'in_vert')])

    # ── Framebuffers ──────────────────────────────────────────────────────────
    fbo_main = ctx.framebuffer(
        color_attachments=[ctx.texture((RENDER_W, RENDER_H), 4)])

    # Ping-pong FBOs for bandwidth phase — pre-seed with noise
    seed = os.urandom(BW_SIZE * BW_SIZE * 4)
    tex_ping = ctx.texture((BW_SIZE, BW_SIZE), 4, seed)
    tex_pong = ctx.texture((BW_SIZE, BW_SIZE), 4, seed)
    fbo_ping = ctx.framebuffer(color_attachments=[tex_ping])
    fbo_pong = ctx.framebuffer(color_attachments=[tex_pong])

    # Noise texture for shader phase
    tex_noise = ctx.texture((256, 256), 4, os.urandom(256 * 256 * 4))

    # ── Telemetry ─────────────────────────────────────────────────────────────
    peak_temp     = None
    clock_samples = []
    start_t       = time.perf_counter()

    def _poll():
        nonlocal peak_temp
        temp, clk = _get_gpu_info()
        if temp is not None:
            peak_temp = max(peak_temp or 0.0, temp)
        if clk is not None:
            clock_samples.append(clk)
        return temp

    # ── Phase 1: Fill Rate ─────────────────────────────────────────────────────
    _write_progress('fill_rate', 0, 0, 0, None, None, 0, 0, 'running')

    fill_dur   = durations['fill']
    fill_end   = time.perf_counter() + fill_dur
    fill_start = time.perf_counter()
    frames_fill = 0
    last_report = fill_start
    last_temp   = fill_start

    fbo_main.use()
    ctx.disable(moderngl.BLEND)

    while time.perf_counter() < fill_end:
        now = time.perf_counter()
        prog_fill['u_time'].value = now - start_t
        vao_fill.render(moderngl.TRIANGLE_STRIP)
        ctx.finish()
        frames_fill += 1

        if now - last_temp >= 5.0:
            temp = _poll()
            last_temp = now
            if temp is not None and temp >= TEMP_LIMIT_C:
                reason = f'GPU temp {temp:.0f}°C — exceeded {TEMP_LIMIT_C:.0f}°C limit'
                _write_progress('fill_rate', 100, 100, 0, peak_temp, _avg(clock_samples),
                                frames_fill / max(now - fill_start, 1e-9), int(now - start_t),
                                'aborted', reason)
                ctx.release()
                sys.exit(2)

        if now - last_report >= 2.0:
            elapsed_p = now - fill_start
            fps_now   = frames_fill / max(elapsed_p, 1e-9)
            s, _, _, _ = _calc_score(fps_now, 0, 0)
            _write_progress('fill_rate',
                            min(100, elapsed_p / fill_dur * 100),
                            min(100, elapsed_p / total_dur * 100),
                            s, peak_temp, _avg(clock_samples),
                            fps_now, int(now - start_t), 'running')
            last_report = now

    fill_fps = frames_fill / max(time.perf_counter() - fill_start, 1e-9)

    # ── Phase 2: Shader Complexity ─────────────────────────────────────────────
    shader_off   = fill_dur
    shader_dur   = durations['shader']
    shader_end   = time.perf_counter() + shader_dur
    shader_start = time.perf_counter()
    frames_sh    = 0
    last_report  = shader_start
    last_temp    = shader_start

    fbo_main.use()
    tex_noise.use(location=0)
    prog_shader['u_tex'].value = 0

    s0, _, _, _ = _calc_score(fill_fps, 0, 0)
    _write_progress('shader', 0, min(100, shader_off / total_dur * 100),
                    s0, peak_temp, _avg(clock_samples), fill_fps, int(time.perf_counter() - start_t), 'running')

    while time.perf_counter() < shader_end:
        now = time.perf_counter()
        prog_shader['u_time'].value = now - start_t
        vao_shader.render(moderngl.TRIANGLE_STRIP)
        ctx.finish()
        frames_sh += 1

        if now - last_temp >= 5.0:
            temp = _poll()
            last_temp = now
            if temp is not None and temp >= TEMP_LIMIT_C:
                reason = f'GPU temp {temp:.0f}°C — exceeded {TEMP_LIMIT_C:.0f}°C limit'
                _write_progress('shader', 100, 100, s0, peak_temp, _avg(clock_samples),
                                frames_sh / max(now - shader_start, 1e-9), int(now - start_t),
                                'aborted', reason)
                ctx.release()
                sys.exit(2)

        if now - last_report >= 2.0:
            elapsed_p  = now - shader_start
            fps_now    = frames_sh / max(elapsed_p, 1e-9)
            s, _, _, _ = _calc_score(fill_fps, fps_now, 0)
            _write_progress('shader',
                            min(100, elapsed_p / shader_dur * 100),
                            min(100, (shader_off + elapsed_p) / total_dur * 100),
                            s, peak_temp, _avg(clock_samples),
                            fps_now, int(now - start_t), 'running')
            last_report = now

    shader_fps = frames_sh / max(time.perf_counter() - shader_start, 1e-9)

    # ── Phase 3: VRAM Bandwidth ────────────────────────────────────────────────
    bw_off    = fill_dur + shader_dur
    bw_dur    = durations['bandwidth']
    bw_end    = time.perf_counter() + bw_dur
    bw_start  = time.perf_counter()
    frames_bw = 0
    last_report = bw_start
    last_temp   = bw_start
    ping        = True  # which FBO to write to

    s1, _, _, _ = _calc_score(fill_fps, shader_fps, 0)
    _write_progress('bandwidth', 0, min(100, bw_off / total_dur * 100),
                    s1, peak_temp, _avg(clock_samples), shader_fps, int(time.perf_counter() - start_t), 'running')

    while time.perf_counter() < bw_end:
        now = time.perf_counter()
        if ping:
            fbo_pong.use()
            tex_ping.use(location=0)
        else:
            fbo_ping.use()
            tex_pong.use(location=0)
        prog_blit['u_tex'].value    = 0
        prog_blit['u_jitter'].value = (now - start_t) * 127.3
        vao_blit.render(moderngl.TRIANGLE_STRIP)
        ctx.finish()
        frames_bw += 1
        ping = not ping

        if now - last_temp >= 5.0:
            temp = _poll()
            last_temp = now
            if temp is not None and temp >= TEMP_LIMIT_C:
                bw_so_far = (frames_bw / max(now - bw_start, 1e-9)) * BW_SIZE * BW_SIZE * 4 * 2 / 1e9
                reason = f'GPU temp {temp:.0f}°C — exceeded {TEMP_LIMIT_C:.0f}°C limit'
                _write_progress('bandwidth', 100, 100,
                                _calc_score(fill_fps, shader_fps, bw_so_far)[0],
                                peak_temp, _avg(clock_samples),
                                frames_bw / max(now - bw_start, 1e-9), int(now - start_t),
                                'aborted', reason)
                ctx.release()
                sys.exit(2)

        if now - last_report >= 2.0:
            elapsed_p = now - bw_start
            fps_now   = frames_bw / max(elapsed_p, 1e-9)
            bw_now    = fps_now * BW_SIZE * BW_SIZE * 4 * 2 / 1e9
            s, _, _, _ = _calc_score(fill_fps, shader_fps, bw_now)
            _write_progress('bandwidth',
                            min(100, elapsed_p / bw_dur * 100),
                            min(100, (bw_off + elapsed_p) / total_dur * 100),
                            s, peak_temp, _avg(clock_samples),
                            fps_now, int(now - start_t), 'running')
            last_report = now

    blit_fps = frames_bw / max(time.perf_counter() - bw_start, 1e-9)
    bw_gbps  = blit_fps * BW_SIZE * BW_SIZE * 4 * 2 / 1e9

    ctx.release()

    # ── Final score ────────────────────────────────────────────────────────────
    _poll()
    elapsed_total = time.perf_counter() - start_t
    score, fill_pts, shader_pts, bw_pts = _calc_score(fill_fps, shader_fps, bw_gbps)
    avg_clk = _avg(clock_samples)

    _write_progress('done', 100, 100, score,
                    peak_temp, avg_clk, 0, int(elapsed_total), 'complete')

    result = {
        'run_id':        run_id,
        'tier':          tier,
        'ts':            int(time.time()),
        'overall_score': score,
        'fill_fps':      round(fill_fps, 1),
        'shader_fps':    round(shader_fps, 1),
        'bw_gbps':       round(bw_gbps, 2),
        'fill_pts':      fill_pts,
        'shader_pts':    shader_pts,
        'bw_pts':        bw_pts,
        'peak_temp_c':   round(peak_temp, 1) if peak_temp is not None else None,
        'avg_clock_mhz': avg_clk,
        'elapsed_s':     int(elapsed_total),
    }

    tmp = RESULTS_FILE + '.tmp'
    try:
        with open(tmp, 'w') as f:
            json.dump(result, f)
        os.replace(tmp, RESULTS_FILE)
    except Exception:
        pass

    return result


def main():
    parser = argparse.ArgumentParser(description='KAM GPU Bench — headless OpenGL stress test')
    parser.add_argument('--tier', default='standard', choices=['quick', 'standard', 'extended'],
                        help='Benchmark duration tier')
    parser.add_argument('--run-id', default='manual', help='Run identifier')
    args = parser.parse_args()

    result = run_bench(args.tier, args.run_id)
    total_s = sum(TIER_DURATIONS[args.tier].values())
    print(f"\nKAM GPU Bench — {args.tier} ({total_s}s)")
    print(f"  Overall Score : {result['overall_score']:>8,}")
    print(f"  Fill Rate     : {result['fill_fps']:>8,.0f} fps  ({result['fill_pts']:,} pts)")
    print(f"  Shader        : {result['shader_fps']:>8,.0f} fps  ({result['shader_pts']:,} pts)")
    print(f"  VRAM BW       : {result['bw_gbps']:>8.1f} GB/s ({result['bw_pts']:,} pts)")
    if result['peak_temp_c']:
        print(f"  Peak Temp     : {result['peak_temp_c']:>8.1f} °C")
    if result['avg_clock_mhz']:
        print(f"  Avg Clock     : {result['avg_clock_mhz']:>8,} MHz")


if __name__ == '__main__':
    main()
