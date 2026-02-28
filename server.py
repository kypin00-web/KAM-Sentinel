#!/usr/bin/env python3
"""
KAM Sentinel v1.4 — cross-platform (Windows / macOS / Linux)
Hot path: pure in-memory reads (<1 ms per /api/stats request)
All hardware I/O in background threads — server never blocks on sensors
"""
from flask import Flask, jsonify, send_from_directory, request
from collections import deque
from concurrent.futures import ThreadPoolExecutor
import psutil, os, json, time, platform, datetime, threading, sys, subprocess, uuid, re, struct, math, hashlib

# ── Paths ─────────────────────────────────────────────────────────────────────
if getattr(sys, 'frozen', False):
    ASSET_DIR = sys._MEIPASS
    DATA_DIR  = os.path.dirname(sys.executable)
else:
    ASSET_DIR = DATA_DIR = os.path.dirname(os.path.abspath(__file__))

app = Flask(__name__, static_folder=ASSET_DIR)

# ── Rate limiting ─────────────────────────────────────────────────────────────
_rl, _rl_lock = {}, threading.Lock()
RL_WIN, RL_MAX = 1.0, 10

def _rate_limited(ip):
    now = time.time()
    with _rl_lock:
        w = [t for t in _rl.get(ip, []) if now - t < RL_WIN]
        if len(w) >= RL_MAX: return True
        w.append(now); _rl[ip] = w
        if len(_rl) > 500:
            cut = now - RL_WIN
            for k in [k for k, v in _rl.items() if not any(t >= cut for t in v)]:
                del _rl[k]
    return False

@app.before_request
def _guard():
    ip = request.remote_addr
    if ip in ('127.0.0.1', '::1'): return
    if _rate_limited(ip):  return jsonify(error='rate limited'), 429
    if request.method == 'POST': return jsonify(error='forbidden'), 403

# ── Feedback rate limiting & dedup ────────────────────────────────────────────
_fb_rl, _fb_dedup, _fb_lock = {}, {}, threading.Lock()
FB_RL_WIN, FB_RL_MAX = 60.0, 5   # 5 submissions per 60 s per IP
FB_DEDUP_WIN = 3600.0             # same message rejected within 1 hour

def _fb_rate_limited(ip):
    """Return True if this IP has exceeded the feedback submission rate."""
    now = time.time()
    with _fb_lock:
        w = [t for t in _fb_rl.get(ip, []) if now - t < FB_RL_WIN]
        if len(w) >= FB_RL_MAX: return True
        w.append(now); _fb_rl[ip] = w
    return False

def _fb_duplicate(cat, msg):
    """Return True if an identical (category + message) was submitted within FB_DEDUP_WIN."""
    key = hashlib.md5(f"{cat}:{msg.strip().lower()}".encode()).hexdigest()
    now = time.time()
    with _fb_lock:
        if key in _fb_dedup and now - _fb_dedup[key] < FB_DEDUP_WIN:
            return True
        _fb_dedup[key] = now
        # prune stale entries to prevent unbounded growth
        if len(_fb_dedup) > 1000:
            cut = now - FB_DEDUP_WIN
            for k in [k for k, v in _fb_dedup.items() if v < cut]:
                del _fb_dedup[k]
    return False

# ── Input validation ──────────────────────────────────────────────────────────
ALLOWED_THRESHOLD_KEYS = {'cpu','gpu','ram','voltage','network'}
ALLOWED_FIXES = {'pip install GPUtil':['GPUtil'], 'pip install wmi pywin32':['wmi','pywin32']}

FAN_CURVES = {
    'SILENT':      [(30,0),(40,10),(50,20),(60,30),(70,45),(80,60),(90,80)],
    'BALANCED':    [(30,10),(40,20),(50,35),(60,50),(70,65),(80,80),(90,100)],
    'PERFORMANCE': [(30,20),(40,35),(50,50),(60,65),(70,80),(80,90),(90,100)],
    'FULL_SEND':   [(30,50),(40,60),(50,70),(60,80),(70,90),(80,100),(90,100)],
}

def _validate(d, depth=0):
    if depth > 3:               return False, 'too nested'
    if isinstance(d, dict):
        if len(d) > 20:         return False, 'too many keys'
        for k, v in d.items():
            if not isinstance(k, str) or len(k) > 50: return False, f'bad key: {k}'
            ok, e = _validate(v, depth+1)
            if not ok: return False, e
    elif isinstance(d, (int, float)):
        if not (0 <= d <= 10000): return False, f'out of range: {d}'
    elif isinstance(d, str):
        if len(d) > 100:        return False, 'string too long'
    elif d is not None and not isinstance(d, bool): return False, f'bad type: {type(d)}'
    return True, None

# ── Platform imports ──────────────────────────────────────────────────────────
# Suppress CMD flash from nvidia-smi in frozen .exe
if sys.platform == 'win32' and getattr(sys, 'frozen', False):
    _P = subprocess.Popen
    def _Q(*a, **kw):
        kw.setdefault('creationflags', subprocess.CREATE_NO_WINDOW)
        return _P(*a, **kw)
    subprocess.Popen = _Q

try: import GPUtil; _GPU = True
except ImportError: _GPU = False

_WMI = _wmi = None
if sys.platform == 'win32':
    try: import wmi; _wmi = wmi.WMI(); _WMI = True
    except: pass

# ── Directories ───────────────────────────────────────────────────────────────
BACKUP_DIR = os.path.join(DATA_DIR, 'backups')
LOG_DIR    = os.path.join(DATA_DIR, 'logs')
PROF_DIR   = os.path.join(DATA_DIR, 'profiles')
BASELINE   = os.path.join(PROF_DIR, 'baseline.json')
ORIG_PROFILE_FILE  = os.path.join(BACKUP_DIR, 'original_system_profile.json')
for d in (BACKUP_DIR, LOG_DIR, PROF_DIR): os.makedirs(d, exist_ok=True)

VER               = '1.5.10'
UPDATE_CHECK_URL  = 'https://raw.githubusercontent.com/kypin00-web/KAM-Sentinel/main/version.json'
TELEMETRY_URL     = ''   # POST endpoint for proactive install/error events

_FAN_CURVE_FILE = os.path.join(PROF_DIR, 'fan_curve.json')

def _load_active_preset():
    if os.path.exists(_FAN_CURVE_FILE):
        try:
            with open(_FAN_CURVE_FILE, encoding='utf-8') as f:
                return json.load(f).get('active', 'BALANCED')
        except: pass
    return 'BALANCED'

def _read_fan_rpms():
    """Read fan RPMs from LibreHardwareMonitor WMI namespace. Windows-only."""
    if sys.platform != 'win32':
        return []
    try:
        import wmi as _w
        lhm = _w.WMI(namespace='root/LibreHardwareMonitor')
        fans = []
        for sensor in lhm.Sensor():
            if sensor.SensorType == 'Fan':
                fans.append({'name': sensor.Name, 'rpm': round(float(sensor.Value))})
        return fans
    except:
        return []

_active_fan_preset = _load_active_preset()

# ── Benchmark state ────────────────────────────────────────────────────────────
BENCH_FILE    = os.path.join(LOG_DIR, 'benchmarks.jsonl')
_bench_lock   = threading.Lock()
_bench_status = dict(running=False, step=None, run_id=None, result=None, noise_warn=False)

# ── Locks ─────────────────────────────────────────────────────────────────────
_state_lock = threading.Lock()
_hw_lock    = threading.Lock()   # guards unified HW cache (WMI + Mac sensors)
_log_lock   = threading.Lock()
_err_lock   = threading.Lock()

# ── Shared state ──────────────────────────────────────────────────────────────
MAX_HIST = 60
history  = {k: deque(maxlen=MAX_HIST) for k in
    ('timestamps','cpu_usage','cpu_temp','gpu_usage','gpu_temp','ram_usage','net_down','net_up')}
_sustained  = {'cpu': deque(maxlen=12), 'gpu': deque(maxlen=12)}

# Unified hardware cache — one structure for all platforms
_hw_cache   = {'cpu_temp': None, 'cpu_volt': None, 'ts': 0, 'ttl': 10}

_gpu_cache      = dict(usage=None, temp=None, name='N/A', vram_used=None, vram_total=None)
_gpu_lock       = threading.Lock()

_fps_history = deque(maxlen=60)
_fps_cache   = dict(fps=None, fps_1pct_low=None, frametime_ms=None,
                    source='rtss' if sys.platform == 'win32' else 'not_supported',
                    available=False)
_fps_lock    = threading.Lock()

_net_prev, _net_ts, _net_warmed_up = psutil.net_io_counters(), time.time(), False
_net_base       = deque(maxlen=36)

_cpu_cache      = 0.0          # written by background sampler, read on hot path
_log_buffer, _log_ts   = [], time.time()
_err_buf, _err_ts   = [], time.time()

# ── Background: CPU sampler ───────────────────────────────────────────────────
def _cpu_loop():
    global _cpu_cache
    while True:
        try: _cpu_cache = psutil.cpu_percent(interval=1.0)
        except: pass

threading.Thread(target=_cpu_loop, daemon=True).start()

# ── Privacy-safe telemetry helpers ───────────────────────────────────────────
_CPU_MAP = [('ryzen 9','AMD Ryzen 9'),('ryzen 7','AMD Ryzen 7'),('ryzen 5','AMD Ryzen 5'),
            ('ryzen 3','AMD Ryzen 3'),('ryzen','AMD Ryzen'),('core i9','Intel i9'),
            ('core i7','Intel i7'),('core i5','Intel i5'),('core i3','Intel i3'),
            ('apple m3','Apple M3'),('apple m2','Apple M2'),('apple m1','Apple M1'),
            ('arm64','Apple Silicon')]
_GPU_MAP = [('RTX 40','NVIDIA RTX 40xx'),('RTX 30','NVIDIA RTX 30xx'),
            ('RTX 20','NVIDIA RTX 20xx'),('GTX 16','NVIDIA GTX 16xx'),
            ('GTX 10','NVIDIA GTX 10xx'),('RX 7','AMD RX 7xxx'),('RX 6','AMD RX 6xxx')]

def _cpu_class(n):
    nl = n.lower()
    for k,v in _CPU_MAP:
        if k in nl: return v
    return 'AMD (other)' if 'amd' in nl else 'Intel (other)' if 'intel' in nl else 'Unknown'

def _gpu_class(n):
    nu = n.upper()
    for k,v in _GPU_MAP:
        if k in nu: return v
    if 'NVIDIA' in nu: return 'NVIDIA (other)'
    if 'AMD' in nu or 'RADEON' in nu: return 'AMD (other)'
    if 'APPLE' in nu or ' M1' in nu or ' M2' in nu or ' M3' in nu: return 'Apple GPU'
    if 'INTEL' in nu: return 'Intel GPU'
    return 'Unknown'

def _os_class():
    if sys.platform == 'win32':
        try: return 'Windows 11' if int(platform.version().split('.')[-1]) >= 22000 else 'Windows 10'
        except: return 'Windows'
    if sys.platform == 'darwin':
        v = platform.mac_ver()[0]
        try:
            m = int(v.split('.')[0])
            return {14:'macOS Sonoma',13:'macOS Ventura',12:'macOS Monterey',11:'macOS Big Sur'}.get(m, f'macOS {v}')
        except: return f'macOS {v}'
    return sys.platform

def _get_install_id():
    f = os.path.join(PROF_DIR, 'install_id.json')
    try:
        if os.path.exists(f):
            return json.load(open(f, encoding='utf-8'))['id']
    except: pass
    nid = str(uuid.uuid4())
    try: json.dump({'id': nid, 'created': datetime.datetime.now().isoformat()}, open(f,'w',encoding='utf-8'))
    except: pass
    return nid

def _post_telemetry(payload):
    if not TELEMETRY_URL: return
    try:
        import urllib.request as _u
        _u.urlopen(_u.Request(TELEMETRY_URL, json.dumps(payload).encode(),
            {'Content-Type':'application/json','User-Agent':f'KAMSentinel/{VER}'}), timeout=5)
    except: pass

def _telemetry_payload(event, err=None):
    si = _sysinfo if '_sysinfo' in globals() else {}
    gb = next((s for s in (4,8,16,24,32,48,64,128) if round(si.get('ram_total_gb',0)) <= s),
              round(si.get('ram_total_gb',0)))
    p = dict(install_id=_install_id, event=event, version=VER, platform=sys.platform,
             os=_os_class(), cpu_class=_cpu_class(si.get('cpu_name','')),
             cpu_cores=si.get('cpu_threads',0), ram_gb=gb,
             gpu_class=_gpu_class(si.get('gpu_name','N/A')), ts=int(time.time()))
    if err: p['error'] = str(err)[:200]
    return p

def _track(event, err=None):
    p = _telemetry_payload(event, err)
    try:
        tl = os.path.join(PROF_DIR,'telemetry.jsonl')
        with open(tl,'a',encoding='utf-8') as f: f.write(json.dumps(p)+'\n')
    except: pass
    threading.Thread(target=_post_telemetry, args=(p,), daemon=True).start()

# ── Error tracking ────────────────────────────────────────────────────────────
def _log_err(ctx, exc):
    e = dict(ts=int(time.time()), date=datetime.datetime.now().isoformat(),
             version=VER, platform=sys.platform,
             context=ctx[:100], error=str(exc)[:200], type=type(exc).__name__)
    with _err_lock: _err_buf.append(e)
    threading.Thread(target=_flush_errs, daemon=True).start()
    _track('error', exc)

def _flush_errs():
    global _err_ts
    with _err_lock:
        if not _err_buf: return
        es, _err_buf[:] = list(_err_buf), []
        _err_ts = time.time()
    try:
        with open(os.path.join(LOG_DIR,'errors.jsonl'),'a',encoding='utf-8') as f:
            for e in es: f.write(json.dumps(e)+'\n')
    except: pass

# ── Hardware: CPU temp + voltage (all platforms, 10 s cache) ──────────────────
# Pre-compile Mac GPU regex once
_IOREG_RE = re.compile(r'"Device Utilization %"\s*=\s*(\d+)')
_VRAM_RE  = re.compile(r'(?:VRAM|Video RAM)[^:]*:\s*(\d+)\s*MB', re.IGNORECASE)

def _hw_read_cpu():
    """Platform-specific CPU temp + voltage. Called by background scheduler only."""
    temp = volt = None
    if sys.platform == 'win32':
        try:
            ts = psutil.sensors_temperatures()
            if ts:
                for es in ts.values():
                    for e in es:
                        if e.current > 0: temp = round(e.current, 1); break
                    if temp: break
        except: pass
        if _WMI and _wmi:
            if temp is None:
                try:
                    for t in _wmi.MSAcpi_ThermalZoneTemperature():
                        c = round((t.CurrentTemperature/10.0)-273.15, 1)
                        if 0 < c < 120: temp = c; break
                except: pass
            try:
                for v in _wmi.Win32_Processor():
                    if getattr(v,'CurrentVoltage',None):
                        volt = round(v.CurrentVoltage/10.0, 3); break
            except: pass
    elif sys.platform == 'darwin':
        try:
            ts = psutil.sensors_temperatures()
            if ts:
                for es in ts.values():
                    for e in es:
                        if 0 < e.current < 120: temp = round(e.current,1); break
                    if temp: break
        except: pass
        if temp is None:
            try:
                r = subprocess.run(['osx-cpu-temp'], capture_output=True, text=True, timeout=2)
                if r.returncode == 0:
                    m = re.search(r'(\d+\.?\d*)', r.stdout)
                    if m: temp = float(m.group(1))
            except: pass
    else:  # Linux
        try:
            ts = psutil.sensors_temperatures()
            if ts:
                for key in ('coretemp','k10temp','acpitz','cpu_thermal'):
                    if key in ts and ts[key]: temp = round(ts[key][0].current, 1); break
                if temp is None:
                    for es in ts.values():
                        for e in es:
                            if 0 < e.current < 120: temp = round(e.current,1); break
                        if temp: break
        except: pass
    return temp, volt

def _hw_scheduler():
    """Background: refresh HW cache every ttl seconds."""
    while True:
        try:
            temp, volt = _hw_read_cpu()
            with _hw_lock:
                _hw_cache.update(cpu_temp=temp, cpu_volt=volt, ts=time.time())
        except Exception as e:
            _log_err('hw_scheduler', e)
        time.sleep(_hw_cache['ttl'])

threading.Thread(target=_hw_scheduler, daemon=True).start()

def get_cpu_temp_voltage():
    with _hw_lock: return _hw_cache['cpu_temp'], _hw_cache['cpu_volt']

# ── Hardware: GPU (all platforms, background thread) ──────────────────────────
def _read_gpu():
    base = dict(usage=None, temp=None, name='N/A', vram_used=None, vram_total=None)
    if _GPU:
        try:
            gpus = GPUtil.getGPUs()
            if gpus:
                g = gpus[0]
                base.update(usage=round(g.load*100,1), temp=round(g.temperature,1),
                            name=g.name, vram_used=round(g.memoryUsed), vram_total=round(g.memoryTotal))
                return base
        except: pass
    if sys.platform == 'darwin':
        try:
            r = subprocess.run(['system_profiler','SPDisplaysDataType'],
                               capture_output=True, text=True, timeout=6)
            if r.returncode == 0:
                for ln in r.stdout.splitlines():
                    if 'Chipset Model:' in ln: base['name'] = ln.split(':',1)[1].strip()
                    m = _VRAM_RE.search(ln)
                    if m: base['vram_total'] = int(m.group(1))
        except: pass
        try:
            r = subprocess.run(['ioreg','-r','-c','IOAccelerator'],
                               capture_output=True, text=True, timeout=4)
            if r.returncode == 0:
                m = _IOREG_RE.search(r.stdout)
                if m: base['usage'] = int(m.group(1))
        except: pass
    return base

def _gpu_worker():
    while True:
        try:
            g = _read_gpu()
            with _gpu_lock: _gpu_cache.update(g)
        except Exception as e: _log_err('gpu_loop', e)
        time.sleep(5)

threading.Thread(target=_gpu_worker, daemon=True).start()
def get_gpu_cached():
    with _gpu_lock: return dict(_gpu_cache)

# ── FPS Counter (RTSS shared memory, Windows-only) ────────────────────────────
def _read_rtss_fps():
    """Read current FPS from RTSS shared memory. Returns float or None."""
    if sys.platform != 'win32':
        return None
    try:
        import mmap
        m = mmap.mmap(-1, 4096, tagname='RTSSSharedMemoryV2', access=mmap.ACCESS_READ)
        data = m.read(4096)
        m.close()
        if data[0:4] != b'RTSS':  # magic bytes — empty mapping means RTSS not running
            return None
        fps = struct.unpack_from('<f', data, 136)[0]
        return round(fps, 1) if fps > 0 else None
    except:
        return None

def _fps_worker():
    while True:
        try:
            fps_val = _read_rtss_fps()
            if fps_val is not None:
                _fps_history.append(fps_val)
                h = sorted(_fps_history)          # ascending: lowest FPS first
                n = max(1, len(h) // 100)         # 1% of samples, minimum 1
                low = round(sum(h[:n]) / n, 1)
                ft  = round(1000.0 / fps_val, 2)
                with _fps_lock:
                    _fps_cache.update(fps=fps_val, fps_1pct_low=low,
                                      frametime_ms=ft, available=True)
            else:
                with _fps_lock:
                    _fps_cache.update(fps=None, fps_1pct_low=None,
                                      frametime_ms=None, available=False)
        except Exception as e:
            _log_err('fps_worker', e)
        time.sleep(2)

threading.Thread(target=_fps_worker, daemon=True).start()

# ── Benchmark functions (pure Python, no external deps) ───────────────────────
def _bench_worker_fn(n):
    """Float math workload: sqrt+log loop. Used for both ST and MT CPU tests."""
    s = 0.0
    for i in range(1, n + 1):
        s += math.sqrt(float(i)) + math.log(float(i))
    return s

def bench_cpu_st(n=4_000_000):
    """Single-threaded CPU benchmark. Returns score (kOps/s) + elapsed."""
    t0 = time.perf_counter()
    _bench_worker_fn(n)
    elapsed = time.perf_counter() - t0
    return dict(score=round(n / elapsed / 1000), elapsed_s=round(elapsed, 2))

def bench_cpu_mt(n_per=4_000_000):
    """Multi-threaded CPU benchmark spread across physical cores."""
    cores = max(1, psutil.cpu_count(logical=False) or 2)
    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=cores) as ex:
        list(ex.map(_bench_worker_fn, [n_per] * cores))
    elapsed = time.perf_counter() - t0
    return dict(score=round(cores * n_per / elapsed / 1000),
                elapsed_s=round(elapsed, 2), cores=cores)

def bench_ram_bw(size_mb=256):
    """Sequential RAM write+read. Returns GB/s for each pass."""
    chunk = 1024 * 1024  # 1 MB
    filler = bytes(chunk)
    try:
        buf = bytearray(size_mb * chunk)
        mv = memoryview(buf)
        t0 = time.perf_counter()
        for i in range(size_mb):
            mv[i * chunk:(i + 1) * chunk] = filler
        w_el = time.perf_counter() - t0
        t1 = time.perf_counter()
        _ = bytes(buf)
        r_el = time.perf_counter() - t1
        del _; del buf
        return dict(write_gbps=round(size_mb / w_el / 1024, 2),
                    read_gbps=round(size_mb / r_el / 1024, 2))
    except MemoryError:
        return dict(write_gbps=0, read_gbps=0, error='MemoryError')

def bench_disk(size_mb=128):
    """Sequential disk write+read (128 MB temp file). Returns MB/s for each pass."""
    chunk = 4 * 1024 * 1024  # 4 MB chunks
    n = size_mb // 4
    data = bytes(chunk)
    tmp = os.path.join(DATA_DIR, f'_bench_{os.getpid()}.tmp')
    try:
        t0 = time.perf_counter()
        with open(tmp, 'wb') as f:
            for _ in range(n):
                f.write(data)
        w_el = time.perf_counter() - t0
        t1 = time.perf_counter()
        with open(tmp, 'rb') as f:
            while f.read(chunk):
                pass
        r_el = time.perf_counter() - t1
        return dict(write_mbps=round(size_mb / w_el),
                    read_mbps=round(size_mb / r_el))
    except Exception as e:
        return dict(write_mbps=0, read_mbps=0, error=str(e)[:100])
    finally:
        try: os.remove(tmp)
        except: pass

def _run_benchmark(mode, run_id):
    """Background thread: run benchmark suite, update _bench_status, save to jsonl."""
    global _bench_status

    def _upd(step):
        with _bench_lock:
            _bench_status['step'] = step

    try:
        noise_pct  = round(_cpu_cache, 1)
        noise_warn = noise_pct > 15

        temp_start, _ = get_cpu_temp_voltage()
        temp_peak = temp_start

        def _snap_peak():
            nonlocal temp_peak
            t, _ = get_cpu_temp_voltage()
            if t and (temp_peak is None or t > temp_peak):
                temp_peak = t

        _upd('CPU single-thread...')
        cpu_st = bench_cpu_st()
        _snap_peak()

        _upd('CPU multi-thread...')
        cpu_mt = bench_cpu_mt()
        _snap_peak()

        ram_result = disk_result = None
        if mode == 'full':
            _upd('RAM bandwidth...')
            ram_result = bench_ram_bw()
            _snap_peak()

            _upd('Disk I/O...')
            disk_result = bench_disk()
            _snap_peak()

        temp_end, _ = get_cpu_temp_voltage()

        # First run ever → baseline
        baseline = not os.path.exists(BENCH_FILE)

        result = dict(
            run_id=run_id,
            ts=int(time.time()),
            date=datetime.datetime.now().isoformat(),
            mode=mode,
            baseline=baseline,
            noise_pct=noise_pct,
            noise_warn=noise_warn,
            temp_start=temp_start,
            temp_peak=temp_peak,
            temp_end=temp_end,
            cpu_st=cpu_st,
            cpu_mt=cpu_mt,
            ram=ram_result,
            disk=disk_result,
        )

        try:
            with open(BENCH_FILE, 'a', encoding='utf-8') as f:
                f.write(json.dumps(result) + '\n')
        except Exception as e:
            _log_err('bench_save', e)

        with _bench_lock:
            _bench_status.update(running=False, step=None, result=result,
                                  noise_warn=noise_warn)
    except Exception as e:
        _log_err('benchmark_runner', e)
        with _bench_lock:
            _bench_status.update(running=False, step=None,
                                  result=dict(error=str(e)[:200]))

# ── Network speed ─────────────────────────────────────────────────────────────
def _net_speed():
    global _net_prev, _net_ts, _net_warmed_up
    try:
        c = psutil.net_io_counters(); now = time.time()
        if not _net_warmed_up:
            _net_prev, _net_ts, _net_warmed_up = c, now, True
            return dict(upload_kbps=0, download_kbps=0, upload_display='0 KB/s', download_display='0 KB/s')
        el = max(now - _net_ts, 0.001)
        up = (c.bytes_sent - _net_prev.bytes_sent) / el / 1024
        dn = (c.bytes_recv - _net_prev.bytes_recv) / el / 1024
        _net_prev, _net_ts = c, now
        fmt = lambda k: f'{k/1024:.2f} MB/s' if k > 1024 else f'{k:.0f} KB/s'
        return dict(upload_kbps=round(up,1), download_kbps=round(dn,1),
                    upload_display=fmt(up), download_display=fmt(dn))
    except:
        return dict(upload_kbps=0, download_kbps=0, upload_display='0 KB/s', download_display='0 KB/s')

# ── System info (called once at startup) ──────────────────────────────────────
def _get_sysinfo():
    i = dict(os=platform.system(), os_version=platform.version(), os_release=platform.release(),
             os_display=_os_class(),
             hostname=platform.node(),
             windows_dir=os.environ.get('SystemRoot', os.environ.get('HOME', 'N/A')) if sys.platform == 'win32' else os.environ.get('HOME', 'N/A'),
             system_dir_label='Windows Dir' if sys.platform == 'win32' else 'Home Dir',
             cpu_name=platform.processor(), cpu_cores=psutil.cpu_count(logical=False),
             cpu_threads=psutil.cpu_count(logical=True))
    freq = None
    if getattr(psutil, 'cpu_freq', None):
        try:
            freq = psutil.cpu_freq()
        except Exception:
            pass
    i['cpu_max_ghz']     = round(freq.max/1000,2)     if freq else 'N/A'
    i['cpu_current_ghz'] = round(freq.current/1000,2) if freq else 'N/A'
    ram = psutil.virtual_memory(); sw = psutil.swap_memory()
    i['ram_total_mb'] = round(ram.total/1024**2); i['ram_total_gb'] = round(ram.total/1024**3,2)
    i['pagefile_used_mb'] = round(sw.used/1024**2); i['pagefile_total_mb'] = round(sw.total/1024**2)
    i['gpu_name'] = 'N/A'; i['gpu_vram_mb'] = 'N/A'
    if _GPU:
        try:
            g = GPUtil.getGPUs()
            if g: i['gpu_name'] = g[0].name; i['gpu_vram_mb'] = round(g[0].memoryTotal)
        except: pass
    i.update(manufacturer='N/A', model='N/A', bios_version='N/A', motherboard='N/A', directx='N/A')
    i['graphics_api_label'] = 'DirectX'  # dashboard row label; overridden on macOS

    if sys.platform == 'win32':
        i['directx'] = 'DirectX 12'
        if _WMI and _wmi:
            try:
                for cs in _wmi.Win32_ComputerSystem(): i['manufacturer']=cs.Manufacturer; i['model']=cs.Model
            except: pass
            try:
                for b in _wmi.Win32_BIOS(): i['bios_version'] = b.SMBIOSBIOSVersion or b.Version or 'N/A'
            except: pass
            try:
                for mb in _wmi.Win32_BaseBoard(): i['motherboard'] = f'{mb.Manufacturer} {mb.Product}'
            except: pass
        try:
            import winreg; k = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r'SOFTWARE\Microsoft\DirectX')
            v,_ = winreg.QueryValueEx(k,'Version'); i['directx'] = f'DirectX 12 (v{v})'
        except: pass
    elif sys.platform == 'darwin':
        i['manufacturer'] = 'Apple'
        i['graphics_api_label'] = 'Metal'
        try:
            r = subprocess.run(['system_profiler','SPHardwareDataType'], capture_output=True, text=True, timeout=8)
            for ln in r.stdout.splitlines():
                ln = ln.strip()
                if 'Model Name:'  in ln: i['model']    = ln.split(':',1)[1].strip()
                if 'Chip:'        in ln: i['cpu_name'] = ln.split(':',1)[1].strip()
        except: pass
        try:
            r = subprocess.run(['system_profiler','SPDisplaysDataType'], capture_output=True, text=True, timeout=6)
            if r.returncode == 0:
                for ln in r.stdout.splitlines():
                    ln = ln.strip()
                    if 'Chipset Model:' in ln and i['gpu_name'] == 'N/A':
                        i['gpu_name'] = ln.split(':',1)[1].strip()
                    if 'Metal Support:' in ln:
                        i['directx'] = ln.split(':',1)[1].strip() or 'Metal'
                        break
        except: pass
        # VRAM: try JSON for _spdisplays_vram; Apple Silicon has unified memory so show friendly message
        if i['gpu_vram_mb'] == 'N/A':
            try:
                r = subprocess.run(['system_profiler','-json','SPDisplaysDataType'],
                                   capture_output=True, text=True, timeout=8)
                if r.returncode == 0:
                    data = json.loads(r.stdout)
                    for item in data.get('SPDisplaysDataType', []):
                        vram_str = item.get('_spdisplays_vram') or item.get('spdisplays_vram')
                        if vram_str:
                            m = re.search(r'([\d.]+)\s*GB', vram_str, re.I)
                            if m: i['gpu_vram_mb'] = int(float(m.group(1)) * 1024)
                            else:
                                m = re.search(r'([\d.]+)\s*MB', vram_str, re.I)
                                if m: i['gpu_vram_mb'] = int(float(m.group(1)))
                        break
            except: pass
        if i['gpu_vram_mb'] == 'N/A' and i['gpu_name'] != 'N/A':
            i['gpu_vram_display'] = 'Unified (shared with system)'

    disks = []
    for p in psutil.disk_partitions():
        try:
            u = psutil.disk_usage(p.mountpoint)
            disks.append(dict(device=p.device, mountpoint=p.mountpoint,
                total_gb=round(u.total/1024**3,1), used_gb=round(u.used/1024**3,1),
                free_gb=round(u.free/1024**3,1), percent=u.percent))
        except: pass
    i['disks'] = disks; i['captured_at'] = datetime.datetime.now().isoformat()
    return i

# ── Thresholds + warnings ─────────────────────────────────────────────────────
from thresholds import load_thresholds, save_thresholds, detect_thresholds
_thresh = None

def _warnings(cpu, gpu, ram, net):
    w = []; t = _thresh
    if not t: return w
    ct=cpu['temp']; gt=gpu['temp']; cv=cpu.get('voltage')
    rp=ram['usage_percent']; dn=net['download_kbps']

    if ct:
        if   ct >= t['cpu']['temp_crit']: w.append(dict(id='cpu_temp_crit',level='critical',component='CPU',   message=f'CPU temp critical: {ct}°C'))
        elif ct >= t['cpu']['temp_warn']: w.append(dict(id='cpu_temp_warn',level='warning', component='CPU',   message=f'CPU temp elevated: {ct}°C'))
    if gt:
        if   gt >= t['gpu']['temp_crit']: w.append(dict(id='gpu_temp_crit',level='critical',component='GPU',   message=f'GPU temp critical: {gt}°C'))
        elif gt >= t['gpu']['temp_warn']: w.append(dict(id='gpu_temp_warn',level='warning', component='GPU',   message=f'GPU temp elevated: {gt}°C'))
    if cv:
        if   cv > t['voltage']['cpu_max']: w.append(dict(id='cpu_volt_high',level='critical',component='Voltage',message=f'CPU voltage high: {cv}V'))
        elif cv < t['voltage']['cpu_min']: w.append(dict(id='cpu_volt_low', level='warning', component='Voltage',message=f'CPU voltage low: {cv}V'))
    if   rp >= t['ram']['usage_crit']: w.append(dict(id='ram_crit',level='critical',component='RAM',message=f'RAM critical: {rp}%'))
    elif rp >= t['ram']['usage_warn']: w.append(dict(id='ram_warn',level='warning', component='RAM',message=f'RAM high: {rp}%'))

    sn = max(1, t['cpu']['usage_sustain_sec']//5)
    with _state_lock:
        _sustained['cpu'].append(cpu['usage']); cs = list(_sustained['cpu'])
        gs = None
        if gpu['usage'] is not None: _sustained['gpu'].append(gpu['usage']); gs = list(_sustained['gpu'])
    if len(cs) >= sn:
        avg = sum(cs)/len(cs)
        if   avg >= t['cpu']['usage_crit']: w.append(dict(id='cpu_sc',level='critical',component='CPU',message=f'CPU sustained {avg:.0f}%'))
        elif avg >= t['cpu']['usage_warn']: w.append(dict(id='cpu_sw',level='warning', component='CPU',message=f'CPU sustained {avg:.0f}%'))
    if gs and len(gs) >= sn:
        avg = sum(gs)/len(gs)
        if   avg >= t['gpu']['usage_crit']: w.append(dict(id='gpu_sc',level='critical',component='GPU',message=f'GPU sustained {avg:.0f}%'))
        elif avg >= t['gpu']['usage_warn']: w.append(dict(id='gpu_sw',level='warning', component='GPU',message=f'GPU sustained {avg:.0f}%'))
    _net_base.append(dn)
    nb = t['network']['baseline_samples']
    if len(_net_base) >= nb:
        ab = sum(list(_net_base)[-nb:])/nb
        if ab > 10 and dn > ab * t['network']['spike_multiplier']:
            w.append(dict(id='net_spike',level='warning',component='Network',message=f'Network spike: {net["download_display"]}'))
    return w

def _live_stats():
    cpu_freq = None
    if getattr(psutil, 'cpu_freq', None):
        try:
            cpu_freq = psutil.cpu_freq()
        except Exception:
            pass
    ram = psutil.virtual_memory()
    gpu = get_gpu_cached(); net = _net_speed()
    ct, cv = get_cpu_temp_voltage(); ts = time.time()
    cpu = dict(usage=round(_cpu_cache,1), temp=ct, voltage=cv,
               freq_ghz=round(cpu_freq.current/1000,2) if cpu_freq else None,
               cores=psutil.cpu_count(logical=False), threads=psutil.cpu_count(logical=True))
    r   = dict(usage_percent=round(ram.percent,1), used_gb=round(ram.used/1024**3,2),
               total_gb=round(ram.total/1024**3,2), available_gb=round(ram.available/1024**3,2))
    warns = _warnings(cpu, gpu, r, net)
    with _state_lock:
        for k,v in (('timestamps',round(ts)),('cpu_usage',_cpu_cache),('cpu_temp',ct),
                    ('gpu_usage',gpu['usage']),('gpu_temp',gpu['temp']),('ram_usage',ram.percent),
                    ('net_down',net['download_kbps']),('net_up',net['upload_kbps'])):
            history[k].append(v)
        hist = {k: list(v) for k,v in history.items()}
    return dict(cpu=cpu, ram=r, gpu=gpu, network=net, warnings=warns, history=hist, timestamp=ts)

# ── Log flush ─────────────────────────────────────────────────────────────────
def _log_stats(s):
    global _log_ts
    with _log_lock:
        _log_buffer.append(dict(ts=s['timestamp'],cpu=s['cpu'],ram=s['ram'],gpu=s['gpu'],warnings=s['warnings']))
        flush = time.time()-_log_ts >= 60
    if flush: _flush_log()

def _flush_log():
    global _log_ts
    with _log_lock:
        if not _log_buffer: return
        es, _log_buffer[:] = list(_log_buffer), []; _log_ts = time.time()
    try:
        with open(os.path.join(LOG_DIR, f"session_{datetime.date.today().isoformat()}.jsonl"),'a',encoding='utf-8') as f:
            for e in es: f.write(json.dumps(e)+'\n')
    except Exception as e: print(f'  [WARN] Log flush: {e}')

# ── Profiles ──────────────────────────────────────────────────────────────────
def _save_orig(si):
    if os.path.exists(ORIG_PROFILE_FILE): return
    with open(ORIG_PROFILE_FILE,'w',encoding='utf-8') as f:
        json.dump(dict(type='ORIGINAL_SYSTEM_PROFILE',warning='DO NOT DELETE',
            saved_at=datetime.datetime.now().isoformat(), system_info=si), f, indent=2)
    print(f'  [BACKUP] {ORIG_PROFILE_FILE}')

def _save_baseline(si, s):
    if os.path.exists(BASELINE): return
    with open(BASELINE,'w',encoding='utf-8') as f:
        json.dump(dict(type='BASELINE_SNAPSHOT', saved_at=datetime.datetime.now().isoformat(),
            system_info=si, initial_metrics=dict(
                cpu_usage=s['cpu']['usage'],cpu_temp=s['cpu']['temp'],cpu_voltage=s['cpu']['voltage'],
                ram_usage=s['ram']['usage_percent'],gpu_usage=s['gpu']['usage'],gpu_temp=s['gpu']['temp']
            )), f, indent=2)
    print(f'  [BASELINE] {BASELINE}')

def _update_launch(success=True):
    sf = os.path.join(PROF_DIR, 'launch_stats.json')
    try:
        d = json.load(open(sf,encoding='utf-8')) if os.path.exists(sf) else {}
        d.update(launch_count=d.get('launch_count',0)+1, last_launch=datetime.datetime.now().isoformat(),
                 install_id=_install_id, version=VER)
        if success: d['last_success'] = d['last_launch']
        json.dump(d, open(sf,'w',encoding='utf-8'), indent=2)
    except: pass

# ═══════════════════════════════════════════════════════════════════════════════
# STARTUP
# ═══════════════════════════════════════════════════════════════════════════════
_install_id   = _get_install_id()
_is_new       = not os.path.exists(os.path.join(PROF_DIR, 'launch_stats.json'))

print('\n  Collecting system info...')
try:    _sysinfo = _get_sysinfo(); print(f'  [OK] {_sysinfo.get("cpu_name","?")} | {_os_class()}')
except Exception as e: _sysinfo = {}; _log_err('startup:sysinfo', e); print(f'  [WARN] {e}')

print('  Loading thresholds...')
try:    _thresh = load_thresholds(PROF_DIR, _sysinfo.get('cpu_name',''), _sysinfo.get('gpu_name','')); print('  [OK] Thresholds ready')
except Exception as e: _log_err('startup:thresholds', e); print(f'  [WARN] {e}')

_save_orig(_sysinfo)
print('  Warming CPU sampler...')
time.sleep(1.2)
try:    _init = _live_stats(); _save_baseline(_sysinfo, _init)
except Exception as e: _log_err('startup:live_stats', e)

_track('installed' if _is_new else 'launch')
_update_launch(success=True)

import atexit
atexit.register(_flush_log); atexit.register(_flush_errs)

# ═══════════════════════════════════════════════════════════════════════════════
# ROUTES
# ═══════════════════════════════════════════════════════════════════════════════
@app.route('/')
def index():        return send_from_directory(ASSET_DIR, 'dashboard.html')

@app.route('/assets/<path:filename>')
def serve_asset(filename):
    return send_from_directory(os.path.join(ASSET_DIR, 'assets'), filename)

@app.route('/api/system')
def api_system():   return jsonify(_sysinfo)

@app.route('/api/stats')
def api_stats():
    g = _guard()
    if g: return g
    try:    s = _live_stats(); _log_stats(s); return jsonify(s)
    except Exception as e: _log_err('api_stats', e); return jsonify(error='stats failed'), 500

@app.route('/api/thresholds', methods=['GET'])
def api_thresholds_get(): return jsonify(_thresh)

@app.route('/api/thresholds', methods=['POST'])
def api_thresholds_post():
    global _thresh
    d = request.get_json(silent=True)
    if not d: return jsonify(error='No data'), 400
    ok, e = _validate(d)
    if not ok: return jsonify(error=e), 400
    for k,v in d.items():
        if k in ALLOWED_THRESHOLD_KEYS and isinstance(v, dict): _thresh[k].update(v)
    save_thresholds(PROF_DIR, _thresh)
    return jsonify(status='saved', thresholds=_thresh)

@app.route('/api/thresholds/reset', methods=['POST'])
def api_thresholds_reset():
    global _thresh
    _thresh = detect_thresholds(_sysinfo.get('cpu_name',''), _sysinfo.get('gpu_name',''))
    save_thresholds(PROF_DIR, _thresh); return jsonify(status='reset', thresholds=_thresh)

@app.route('/api/baseline')
def api_baseline():
    return jsonify(json.load(open(BASELINE,encoding='utf-8'))) if os.path.exists(BASELINE) \
           else (jsonify(error='No baseline'), 404)

@app.route('/api/original_profile')
def api_orig_profile():
    return jsonify(json.load(open(ORIG_PROFILE_FILE,encoding='utf-8'))) if os.path.exists(ORIG_PROFILE_FILE) \
           else (jsonify(error='No profile'), 404)

@app.route('/api/version')
def api_version():
    return jsonify(version=VER, platform=sys.platform, update_check_url=UPDATE_CHECK_URL)

@app.route('/api/telemetry')
def api_telemetry():
    tf = os.path.join(PROF_DIR,'telemetry.jsonl'); sf = os.path.join(PROF_DIR,'launch_stats.json')
    evs = []
    if os.path.exists(tf):
        with open(tf,encoding='utf-8') as f:
            for ln in f:
                try: evs.append(json.loads(ln))
                except: pass
    ls = json.load(open(sf,encoding='utf-8')) if os.path.exists(sf) else {}
    return jsonify(install_id=_install_id, is_new=_is_new, launch_stats=ls,
                   event_count=len(evs), recent=evs[-10:])

@app.route('/api/errors')
def api_errors():
    ef = os.path.join(LOG_DIR,'errors.jsonl'); es = []
    if os.path.exists(ef):
        with open(ef,encoding='utf-8') as f:
            for ln in f:
                try: es.append(json.loads(ln))
                except: pass
    return jsonify(count=len(es), first=es[0]['date'] if es else None,
                   last=es[-1]['date'] if es else None, recent=es[-20:])

@app.route('/api/feedback', methods=['POST'])
def api_feedback():
    d = request.get_json(silent=True) or {}
    cat = d.get('category','general')
    if cat not in ('bug','feature','performance','general'): cat = 'general'
    msg = str(d.get('message',''))[:500].replace('\n',' ').replace('\r',' ').replace('\x00','')
    if not msg.strip(): return jsonify(error='No message'), 400

    ip = request.remote_addr or '127.0.0.1'
    if _fb_rate_limited(ip):
        return jsonify(error='Too many feedback submissions — please wait a minute.'), 429
    if _fb_duplicate(cat, msg):
        return jsonify(error='Duplicate feedback — this report was already received.'), 429

    pri, act = 'normal', None
    kw = msg.lower()
    if cat == 'bug':
        if any(k in kw for k in ('crash','freeze','not working','broken','error','exception',"won't start",'fails')):
            pri, act = 'critical', 'immediate_fix'
        elif any(k in kw for k in ('wrong','incorrect','missing','n/a','not showing','blank','stuck')):
            pri, act = 'high', 'fix_next_build'
        else: pri, act = 'normal', 'fix_next_build'
    elif cat == 'performance':
        pri, act = ('high','investigate_immediately') if any(k in kw for k in ('slow','lag','freeze','high cpu','100%')) \
                   else ('normal','investigate_next_build')
    elif cat == 'feature':  pri, act = 'review', 'pending_review'
    else:                   pri, act = 'low', 'log_only'

    entry = dict(ts=time.time(), date=datetime.datetime.now().isoformat(),
                 id=f'{cat[:3].upper()}-{int(time.time())}', category=cat, priority=pri,
                 auto_action=act, message=msg, status='open', version=VER,
                 sys_info=dict(cpu=_cpu_class(_sysinfo.get('cpu_name','')),
                               gpu=_gpu_class(_sysinfo.get('gpu_name','N/A')),
                               os=_os_class(), platform=sys.platform))
    fd = os.path.join(LOG_DIR,'feedback'); os.makedirs(fd, exist_ok=True)
    try:
        with open(os.path.join(fd,f'{cat}.jsonl'),'a',encoding='utf-8') as f:
            f.write(json.dumps(entry)+'\n')
        msgs = dict(critical='Critical bug logged -- fix queued immediately',
                    high='Bug logged -- next build', review="Feature logged -- we'll review!",
                    normal='Logged -- thank you!', low='Feedback received -- thank you!')
        return jsonify(status='ok', id=entry['id'], priority=pri, message=msgs.get(pri,'Thank you!'))
    except Exception as e: _log_err('api_feedback', e); return jsonify(error=str(e)), 500

@app.route('/api/feedback/queue')
def api_feedback_queue():
    fd = os.path.join(LOG_DIR,'feedback')
    if not os.path.exists(fd): return jsonify(items=[], counts={})
    items, counts = [], {c:0 for c in ('bug','performance','feature','general')}
    for cat in counts:
        fp = os.path.join(fd, f'{cat}.jsonl')
        if os.path.exists(fp):
            with open(fp,encoding='utf-8') as fh:
                for ln in fh:
                    try:
                        e = json.loads(ln)
                        if e.get('status') == 'open': items.append(e); counts[cat] += 1
                    except: pass
    items.sort(key=lambda x: {'critical':0,'high':1,'review':2,'normal':3,'low':4}.get(x.get('priority','low'),5))
    return jsonify(items=items[-50:], counts=counts)

@app.route('/api/shutdown', methods=['POST'])
def api_shutdown():
    _flush_log(); _flush_errs()
    fn = request.environ.get('werkzeug.server.shutdown')
    if fn: fn()
    else: threading.Thread(target=lambda: (time.sleep(0.5), os._exit(0)), daemon=True).start()
    return jsonify(status='shutting down')

@app.route('/api/diagnostics')
def api_diagnostics():
    if request.args.get('test') == '1':
        return jsonify(gpu=dict(status='missing_package',message='[TEST] GPUtil not installed',
                                fix='pip install GPUtil', auto_fix=True),
                       cpu_temp=dict(status='needs_lhm',message='[TEST] LibreHardwareMonitor not running',
                                     fix='Launch LibreHardwareMonitor.exe as Administrator',
                                     auto_fix=False, download='https://github.com/LibreHardwareMonitor/LibreHardwareMonitor/releases/latest'),
                       wmi=dict(status='missing_package',message='[TEST] WMI not installed',
                                fix='pip install wmi pywin32', auto_fix=True),
                       privacy=dict(status='ok',message='No PII stored',fix=None))
    diags = {}
    try: gpus=GPUtil.getGPUs(); diags['gpu']=dict(status='ok' if gpus else 'no_gpu_found',
                                                   message=f'Found {len(gpus)} GPU(s)' if gpus else 'No GPU detected',fix=None)
    except ImportError: diags['gpu']=dict(status='missing_package',message='GPUtil not installed',
                                           fix='pip install GPUtil', auto_fix=True)
    except Exception: diags['gpu']=dict(status='no_gpu_found',message='No NVIDIA GPU detected',fix=None)
    if sys.platform == 'win32':
        lhm = False
        try:
            import wmi as _w2
            for ns in ('root/LibreHardwareMonitor','root/OpenHardwareMonitor'):
                try: _w2.WMI(namespace=ns); lhm=True; break
                except: pass
        except: pass
        diags['cpu_temp'] = dict(status='ok',message='Hardware monitor running',fix=None) if lhm else \
                            dict(status='needs_lhm' if _WMI else 'missing_package',
                                 message='LibreHardwareMonitor needed' if _WMI else 'WMI not installed',
                                 fix='Launch LibreHardwareMonitor.exe as Administrator' if _WMI else 'pip install wmi pywin32',
                                 auto_fix=not _WMI,
                                 **({'download':'https://github.com/LibreHardwareMonitor/LibreHardwareMonitor/releases/latest'} if _WMI else {}))
        diags['wmi'] = dict(status='ok' if _WMI else 'missing_package',
                            message='WMI available' if _WMI else 'WMI not installed',
                            fix=None if _WMI else 'pip install wmi pywin32', auto_fix=not _WMI)
    else:
        ct = _hw_cache['cpu_temp']
        diags['cpu_temp'] = dict(status='ok' if ct else 'limited',
                                 message='Temp reading available' if ct else 'CPU temp N/A (install osx-cpu-temp for Mac)',
                                 fix=None)
        diags['wmi'] = dict(status='not_applicable', message=f'WMI is Windows-only (platform: {sys.platform})', fix=None)
    diags['privacy'] = dict(status='ok', message='No PII stored — hardware metrics only', fix=None)
    return jsonify(diags)

@app.route('/api/autofix', methods=['POST'])
def api_autofix():
    d = request.get_json(silent=True) or {}
    cmd = d.get('fix','')
    if cmd not in ALLOWED_FIXES: return jsonify(error='Fix not allowed'), 400
    try:
        flags = dict(creationflags=subprocess.CREATE_NO_WINDOW) if sys.platform=='win32' else {}
        r = subprocess.run([sys.executable,'-m','pip','install']+ALLOWED_FIXES[cmd],
                          capture_output=True, text=True, timeout=60, **flags)
        return jsonify(status='ok', message=f'Installed {", ".join(ALLOWED_FIXES[cmd])} -- restart to apply') \
               if r.returncode == 0 else (jsonify(status='error', message=r.stderr[:200]), 500)
    except Exception as e: return jsonify(status='error', message=str(e)), 500

@app.route('/api/fps')
def api_fps():
    with _fps_lock: return jsonify(dict(_fps_cache))

@app.route('/api/forge/fan_curves')
def api_forge_fan_curves():
    rpms = _read_fan_rpms()
    return jsonify(curves=FAN_CURVES, active=_active_fan_preset,
                   fan_rpms=rpms, rpm_available=bool(rpms))

@app.route('/api/forge/fan_curves/select', methods=['POST'])
def api_forge_fan_curves_select():
    global _active_fan_preset
    d = request.get_json(silent=True) or {}
    preset = d.get('preset', '')
    if preset not in FAN_CURVES:
        return jsonify(error=f'Unknown preset. Valid: {list(FAN_CURVES)}'), 400
    _active_fan_preset = preset
    try:
        with open(_FAN_CURVE_FILE, 'w', encoding='utf-8') as f:
            json.dump({'active': preset}, f)
    except Exception as e:
        _log_err('fan_curves_select', e)
    return jsonify(status='ok', active=_active_fan_preset)

@app.route('/api/forge/benchmark', methods=['POST'])
def api_forge_benchmark():
    global _bench_status
    # Validate mode before checking running state so bad mode always → 400
    d = request.get_json(silent=True) or {}
    mode = d.get('mode', 'quick')
    if mode not in ('quick', 'full'):
        return jsonify(error='mode must be "quick" or "full"'), 400
    with _bench_lock:
        if _bench_status['running']:
            return jsonify(error='Benchmark already running', running=True), 409
        run_id = datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
        _bench_status.update(running=True, step='Starting...', run_id=run_id,
                              result=None, noise_warn=False)
    threading.Thread(target=_run_benchmark, args=(mode, run_id), daemon=True).start()
    return jsonify(started=True, run_id=run_id, mode=mode)

@app.route('/api/forge/benchmark/status')
def api_forge_benchmark_status():
    with _bench_lock:
        return jsonify(dict(_bench_status))

@app.route('/api/forge/benchmark/history')
def api_forge_benchmark_history():
    runs = []
    if os.path.exists(BENCH_FILE):
        with open(BENCH_FILE, encoding='utf-8') as f:
            for ln in f:
                try: runs.append(json.loads(ln))
                except: pass
    return jsonify(runs=list(reversed(runs[-20:])))  # last 20, newest first

@app.route('/api/forge/benchmark/baseline')
def api_forge_benchmark_baseline():
    if not os.path.exists(BENCH_FILE):
        return jsonify(error='No benchmark runs yet'), 404
    # Return first run tagged as baseline, fall back to first run
    first = None
    with open(BENCH_FILE, encoding='utf-8') as f:
        for ln in f:
            try:
                r = json.loads(ln)
                if first is None: first = r
                if r.get('baseline'): return jsonify(r)
            except: pass
    return jsonify(first) if first else (jsonify(error='No valid runs'), 404)


if __name__ == '__main__':
    port = 5000
    if len(sys.argv) > 1:
        try:
            port = int(sys.argv[1])
            if port < 1 or port > 65535:
                raise ValueError('port out of range')
        except (ValueError, TypeError):
            print('  Usage: python server.py [PORT]  (default: 5000)')
            sys.exit(1)
    print(f'\n  KAM SENTINEL v{VER}  [{sys.platform}]')
    print(f'  http://localhost:{port}')
    if not _GPU: print('  [!] pip install GPUtil  for GPU stats')
    if sys.platform == 'win32' and not _WMI: print('  [!] pip install wmi pywin32  for full Windows data')
    print()
    app.run(host='0.0.0.0', port=port, debug=False, threaded=True)
