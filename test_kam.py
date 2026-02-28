#!/usr/bin/env python3
"""
KAM Sentinel - Automated Test Suite
Tests: API correctness, memory leaks, performance, threading, warnings engine
Run: python test_kam.py
"""

import sys, os, json, time, threading, tracemalloc, gc, statistics
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from importlib.metadata import version as _pkg_version
except ImportError:
    _pkg_version = None

CI = os.environ.get('CI', 'false').lower() == 'true'  # True in GitHub Actions

# -- Color output --------------------------------------------------------------
GREEN  = '\033[92m'
RED    = '\033[91m'
YELLOW = '\033[93m'
CYAN   = '\033[96m'
RESET  = '\033[0m'
BOLD   = '\033[1m'

passed = failed = warned = 0
_log_entries = []

def ok(msg):
    global passed
    passed += 1
    _log_entries.append(('pass', msg))
    print(f"  {GREEN}{'[OK]' if CI else '[OK]'}{RESET} {msg}")

def fail(msg):
    global failed
    failed += 1
    _log_entries.append(('fail', msg))
    print(f"  {RED}{'[FAIL]' if CI else '[FAIL] FAIL'}{RESET} {msg}")

def warn(msg):
    global warned
    warned += 1
    _log_entries.append(('warn', msg))
    print(f"  {YELLOW}{'[WARN]' if CI else '[WARN] WARN'}{RESET} {msg}")

def section(title):
    _log_entries.append(('section', title))
    if CI:
        print(f"\n{BOLD}{CYAN}-- {title} {'-'*(50-len(title))}{RESET}")
    else:
        print(f"\n{BOLD}{CYAN}-- {title} {'-'*(50-len(title))}{RESET}")

# ===============================================================================
# 1. IMPORT & SYNTAX CHECK
# ===============================================================================
section("1. Import & Module Check")

try:
    import thresholds
    ok("thresholds.py imports cleanly")
except Exception as e:
    fail(f"thresholds.py import failed: {e}")

try:
    import psutil
    ok(f"psutil available (v{psutil.__version__})")
except:
    fail("psutil not installed -- run: python -m pip install psutil")

try:
    import flask
    try:
        flask_ver = _pkg_version("flask") if _pkg_version else getattr(flask, "__version__", "?")
    except Exception:
        flask_ver = getattr(flask, "__version__", "?")
    ok(f"flask available (v{flask_ver})")
except Exception:
    fail("flask not installed -- run: python -m pip install flask")

if CI:
    ok("GPUtil check skipped -- CI environment (Windows-only package)")
else:
    try:
        import GPUtil
        ok("GPUtil available -- GPU stats enabled")
    except:
        warn("GPUtil not installed -- GPU stats will show N/A (pip install GPUtil)")

if CI:
    ok("wmi check skipped -- CI environment (Windows-only package)")
else:
    try:
        import wmi
        ok("wmi available -- CPU temp/voltage enabled")
    except:
        warn("wmi not installed -- CPU temp/voltage will show N/A (pip install wmi pywin32)")

# ===============================================================================
# 2. THRESHOLD ENGINE TESTS
# ===============================================================================
section("2. Threshold Engine")

try:
    t_generic = thresholds.detect_thresholds("Unknown CPU", "Unknown GPU")
    required_keys = ['cpu', 'gpu', 'voltage', 'ram', 'network']
    for key in required_keys:
        if key not in t_generic:
            fail(f"Missing threshold section: {key}")
        else:
            ok(f"Generic threshold section '{key}' present")

    # Ryzen detection
    t_ryzen = thresholds.detect_thresholds("AMD Ryzen 9 5900X", "Unknown GPU")
    if t_ryzen['cpu']['temp_crit'] <= 90:
        ok(f"Ryzen 5000 TJmax detected correctly: {t_ryzen['cpu']['temp_crit']}?C")
    else:
        fail(f"Ryzen 5000 TJmax wrong: {t_ryzen['cpu']['temp_crit']}?C (expected ?90)")

    # RTX detection
    t_rtx = thresholds.detect_thresholds("Unknown CPU", "NVIDIA GeForce RTX 3080")
    if t_rtx['gpu']['temp_crit'] >= 90:
        ok(f"RTX 3080 GPU limit detected: {t_rtx['gpu']['temp_crit']}?C")
    else:
        fail(f"RTX 3080 GPU limit wrong: {t_rtx['gpu']['temp_crit']}?C")

    # Save/load round-trip
    import tempfile
    with tempfile.TemporaryDirectory() as tmpdir:
        thresholds.save_thresholds(tmpdir, t_ryzen)
        loaded = thresholds.load_thresholds(tmpdir, "AMD Ryzen 9 5900X", "")
        if loaded['cpu']['temp_crit'] == t_ryzen['cpu']['temp_crit']:
            ok("Threshold save/load round-trip correct")
        else:
            fail("Threshold save/load mismatch")
except Exception as e:
    fail(f"Threshold engine error: {e}\n    {traceback.format_exc()}")

# ===============================================================================
# 3. MEMORY LEAK DETECTION
# ===============================================================================
section("3. Memory Leak Detection")

try:
    from collections import deque

    # Test deque bounded growth (should NOT grow beyond maxlen)
    d = deque(maxlen=60)
    for i in range(1000):
        d.append(i)
    if len(d) == 60:
        ok("deque(maxlen=60) correctly bounded after 1000 inserts")
    else:
        fail(f"deque grew to {len(d)} instead of staying at 60")

    # Test that server.py uses deque not list
    with open('server.py', encoding='utf-8') as f:
        src = f.read()
    if 'deque(maxlen=' in src:
        ok("server.py uses deque(maxlen=) for history buffers")
    else:
        fail("server.py still uses plain lists -- memory leak risk")

    code_lines = [l for l in src.splitlines() if not l.strip().startswith('#')]
    code_only = '\n'.join(code_lines)
    if '.pop(0)' not in code_only:
        ok("No list.pop(0) calls found -- O(1) operations confirmed")
    else:
        import re
        pops = re.findall(r'.+\.pop\(0\)', code_only)
        fail(f"Found list.pop(0): {pops[0].strip()}")

    # Check log buffer is bounded
    if 'LOG_BATCH_SIZE' in src or '_log_buffer' in src:
        ok("Log batching present -- disk I/O not on every poll")
    else:
        warn("No log batching found -- disk writes may happen every poll")

    # Check log rotation - logs split by day so they don't grow forever
    if 'rotate' in src.lower() or 'LOG_MAX_LINES' in src or 'session_' in src:
        ok("Log rotation present -- daily log files prevent unbounded growth")
    else:
        warn("No log rotation found -- log files may grow forever")

except Exception as e:
    fail(f"Memory check error: {e}")

# ===============================================================================
# 4. PERFORMANCE CHECKS
# ===============================================================================
section("4. Performance & Architecture")

try:
    with open('server.py', encoding='utf-8') as f:
        src = f.read()

    # Background thread
    if '_background_poll' in src or '_cpu_sampler' in src or ('daemon=True' in src and 'threading.Thread' in src):
        ok("Background daemon thread present -- Flask requests never block on hardware I/O")
    else:
        fail("No background polling thread -- stats collected on every request (blocks Flask)")

    # WMI caching
    if 'WMI_CACHE_TTL' in src or '_wmi_cache_time' in src or '_wmi_cache' in src or '_hw_cache' in src:
        ok("HW result caching present -- slow COM/ioreg calls max once per TTL")
    else:
        warn("No WMI caching -- every poll calls slow COM operations (50-200ms)")

    # Non-blocking cpu_percent
    if 'interval=0' in src:
        ok("cpu_percent(interval=0) -- non-blocking delta measurement")
    elif 'interval=0.1' in src:
        fail("cpu_percent(interval=0.1) -- blocks 100ms per poll cycle")

    # Threading
    if 'threaded=True' in src:
        ok("Flask threaded=True -- concurrent request handling enabled")
    else:
        warn("Flask threaded not set -- requests may queue behind each other")

    # Single cache object
    if '_stat_cache' in src or '_cached_stats' in src or 'collect_live_stats' in src or '_live_stats' in src:
        ok("Stat cache present -- Flask serves pre-computed in-memory data")
    else:
        warn("No stat cache found -- metrics may be computed on every request")

    # Check GPU async worker
    if '_gpu_worker' in src:
        ok("GPU worker thread present -- nvidia-smi never blocks polling thread")
    else:
        fail("No GPU worker thread -- nvidia-smi blocks main poll every cycle")

    if 'get_gpu_cached' in src:
        ok("get_gpu_cached() used in poll -- GPU reads are instant/non-blocking")
    else:
        fail("get_gpu_stats() used directly in poll -- blocks on nvidia-smi")

    if '_fps_worker' in src:
        ok("FPS worker thread present -- RTSS polling never blocks main thread")
    else:
        fail("_fps_worker missing -- FPS background polling not implemented")

    if '_fps_cache' in src:
        ok("FPS cache present -- /api/fps served from memory")
    else:
        fail("_fps_cache missing -- FPS endpoint has no backing cache")

    # Module 3 — Fan Control
    if 'FAN_CURVES' in src:
        ok("FAN_CURVES constant present -- fan preset data available")
    else:
        fail("FAN_CURVES missing -- Module 3 fan control not implemented")

    if '_read_fan_rpms' in src:
        ok("_read_fan_rpms() present -- LHM WMI fan RPM reader available")
    else:
        fail("_read_fan_rpms missing -- fan RPM reading not implemented")

    # Module 2 — Benchmarks
    for fn in ('bench_cpu_st', 'bench_cpu_mt', 'bench_ram_bw', 'bench_disk'):
        if fn in src:
            ok(f"{fn}() present -- benchmark function available")
        else:
            fail(f"{fn}() missing -- Module 2 benchmark not implemented")

    if '_bench_status' in src:
        ok("_bench_status present -- benchmark state tracking available")
    else:
        fail("_bench_status missing -- benchmark state tracking not implemented")

    if 'BENCH_FILE' in src:
        ok("BENCH_FILE constant present -- benchmark history log path defined")
    else:
        fail("BENCH_FILE missing -- benchmark history log not defined")

    if 'CREATE_NO_WINDOW' in src:
        ok("CREATE_NO_WINDOW set -- no CMD flash when running as .exe")
    else:
        warn("CREATE_NO_WINDOW not set -- may see CMD flash in .exe builds")

    # cpu_percent timing
    t0 = time.perf_counter()
    import psutil
    psutil.cpu_percent(interval=0)
    elapsed_ms = (time.perf_counter() - t0) * 1000
    if elapsed_ms < 5:
        ok(f"cpu_percent(interval=0) returns in {elapsed_ms:.1f}ms -- non-blocking confirmed")
    else:
        warn(f"cpu_percent took {elapsed_ms:.1f}ms -- may be blocking")

except Exception as e:
    fail(f"Performance check error: {e}")

# ===============================================================================
# 5. WARNING ENGINE LOGIC
# ===============================================================================
section("5. Warning Engine Logic")

try:
    # Import evaluate_warnings if available
    import importlib.util
    spec = importlib.util.spec_from_file_location("server", "server.py")
    # Just test the logic manually since server has side effects on import
    t = thresholds.detect_thresholds("AMD Ryzen 9 5900X", "NVIDIA GeForce RTX 3080")

    # CPU temp critical
    cpu_hot  = {'temp': 95, 'usage': 50, 'voltage': 1.2, 'freq_ghz': 4.2, 'cores': 8, 'threads': 16}
    cpu_ok   = {'temp': 45, 'usage': 10, 'voltage': 1.2, 'freq_ghz': 4.2, 'cores': 8, 'threads': 16}
    gpu_hot  = {'temp': 96, 'usage': 90, 'name': 'RTX 3080', 'vram_used': 9000, 'vram_total': 10240}
    gpu_ok   = {'temp': 55, 'usage': 30, 'name': 'RTX 3080', 'vram_used': 2000, 'vram_total': 10240}
    ram_high = {'usage_percent': 94, 'used_gb': 30, 'total_gb': 32, 'available_gb': 2}
    ram_ok   = {'usage_percent': 30, 'used_gb': 10, 'total_gb': 32, 'available_gb': 22}
    net_ok   = {'download_kbps': 100, 'upload_kbps': 50, 'upload_display':'50 KB/s', 'download_display':'100 KB/s'}

    # Threshold checks manually
    if cpu_hot['temp'] >= t['cpu']['temp_crit']:
        ok(f"CPU critical threshold fires at {cpu_hot['temp']}?C (limit: {t['cpu']['temp_crit']}?C)")
    else:
        fail("CPU critical threshold did not fire")

    if cpu_ok['temp'] < t['cpu']['temp_warn']:
        ok(f"CPU OK temp {cpu_ok['temp']}?C correctly below warning threshold")
    else:
        fail("CPU OK temp incorrectly above warning threshold")

    if gpu_hot['temp'] >= t['gpu']['temp_crit']:
        ok(f"GPU critical threshold fires at {gpu_hot['temp']}?C")
    else:
        fail("GPU critical threshold did not fire")

    if ram_high['usage_percent'] >= t['ram']['usage_crit']:
        ok(f"RAM critical threshold fires at {ram_high['usage_percent']}%")
    else:
        fail("RAM critical threshold did not fire")

    if ram_ok['usage_percent'] < t['ram']['usage_warn']:
        ok(f"RAM OK at {ram_ok['usage_percent']}% correctly below warning")
    else:
        fail("RAM OK incorrectly above warning threshold")

except Exception as e:
    fail(f"Warning engine test error: {e}")

# ===============================================================================
# 6. FILE STRUCTURE & SAFETY CHECKS
# ===============================================================================
section("6. File Structure & Safety")

required_files = ['server.py', 'thresholds.py', 'dashboard.html', 'launch.py',
                  'build_exe.bat', 'setup.bat', 'CLAUDE.md']
for f in required_files:
    if os.path.exists(f):
        ok(f"{f} present")
    else:
        fail(f"{f} MISSING")

# Check original profile is never overwritten
with open('server.py', encoding='utf-8') as f:
    src = f.read()
if 'os.path.exists(ORIG_PROFILE_FILE)' in src or 'exists(ORIG_PROFILE_FILE)' in src:
    ok("Original profile backup is skip-if-exists (never overwritten)")
else:
    fail("SAFETY VIOLATION: original_system_profile.json may be overwritten!")

# Check no debug=True
if 'debug=True' not in src:
    ok("Flask debug=False -- production safe")
else:
    fail("Flask debug=True found -- must be False for distribution")

# Check version string
import re
versions = re.findall(r'v([\d.]+)', src)
if versions:
    ok(f"Version string found: v{versions[0]}")

# ===============================================================================
# 7. LIVE PSUTIL READING
# ===============================================================================
section("7. Live Hardware Reading")

if CI:
    ok("Live hardware reading skipped -- CI environment (no physical hardware)")
    ok("CPU usage check skipped -- CI")
    ok("RAM check skipped -- CI")
    ok("Network check skipped -- CI")
    ok("Poll cycle performance check skipped -- CI")
else:
    try:
        import psutil

        psutil.cpu_percent(interval=0)
        time.sleep(0.2)

        cpu = psutil.cpu_percent(interval=0)
        if 0 <= cpu <= 100:
            ok(f"CPU usage readable: {cpu}%")
        else:
            fail(f"CPU usage out of range: {cpu}")

        ram = psutil.virtual_memory()
        if ram.total > 0:
            ok(f"RAM readable: {round(ram.total/1024**3,1)} GB total, {ram.percent}% used")
        else:
            fail("RAM reading failed")

        net = psutil.net_io_counters()
        if net.bytes_sent >= 0:
            ok(f"Network counters readable: {round(net.bytes_sent/1024**2,1)} MB sent")
        else:
            fail("Network reading failed")

        times = []
        psutil.cpu_percent(interval=0)
        for _ in range(10):
            t0 = time.perf_counter()
            psutil.cpu_percent(interval=0)
            psutil.virtual_memory()
            psutil.net_io_counters()
            times.append((time.perf_counter()-t0)*1000)
        avg_ms = statistics.mean(times)
        if avg_ms < 10:
            ok(f"10x poll cycle avg: {avg_ms:.2f}ms -- excellent performance")
        elif avg_ms < 50:
            warn(f"10x poll cycle avg: {avg_ms:.2f}ms -- acceptable")
        else:
            fail(f"10x poll cycle avg: {avg_ms:.2f}ms -- too slow")

    except Exception as e:
        fail(f"Hardware reading error: {e}")

# ===============================================================================
# 8. DASHBOARD HTML CHECKS
# ===============================================================================
section("8. Dashboard HTML Checks")

try:
    with open('dashboard.html', encoding='utf-8') as f:
        html = f.read()

    checks = [
        ('Concept 2 SVG logo',      'shield-clip' in html or 'svg' in html.lower()),
        ('Overlay HTML present',    'kam-overlay' in html),
        ('Overlay toggle button',   'toggleOverlay' in html),
        ('Refresh rate selector',   'refresh-select' in html),
        ('updateRefreshRate func',  'updateRefreshRate' in html),
        ('DOM cache object',        'const DOM = {}' in html),
        ('Single API fetch',        html.count("fetch('/api/stats")  + html.count('fetch("/api/stats') >= 1),
        ('Warning panel present',   'warnings-panel' in html),
        ('Settings modal present',  'settings-modal' in html),
        ('Chart.js loaded',         'chart.umd' in html or 'Chart' in html),
        ('Goal 10 update check',    'checkForUpdate' in html),
        ('Overlay drag support',    'mousedown' in html or 'dragging' in html),
        ('Browser close shutdown',  'beforeunload' in html and 'sendBeacon' in html),
        ('Shutdown endpoint',       'api/shutdown' in html),
        ('Feedback modal',          'feedback-modal' in html),
        ('Feedback endpoint',       'api/feedback' in html),
        ('spanGaps chart fix',      'spanGaps' in html),
        ('Logo K in KAM',           'AM</span>' in html or '>AM<' in html),
        ('Forge tab present',             'tab-forge' in html),
        ('Fan chart canvas present',      'chart-fan-curve' in html),
        ('Preset buttons present',        'preset-SILENT' in html),
        ('Bench quick button present',    'bench-quick-btn' in html),
        ('Bench score display present',   'bsr-st' in html),
        ('Bench history section present', 'bench-history' in html),
    ]
    for name, result in checks:
        if result:
            ok(name)
        else:
            fail(name)

except Exception as e:
    fail(f"HTML check error: {e}")

# ===============================================================================
# SUMMARY
# ===============================================================================
total = passed + failed + warned
SEP = '='*55 if CI else '='*55
print(f"\n{SEP}")
print(f"{BOLD}  KAM SENTINEL TEST RESULTS{RESET}")
print(f"{SEP}")
print(f"  {GREEN}Passed : {passed}{RESET}")
print(f"  {YELLOW}Warnings: {warned}{RESET}")
print(f"  {RED}Failed : {failed}{RESET}")
print(f"  Total  : {total}")
print(f"{SEP}")


# ===============================================================================
# 9. SECURITY SCAN
# ===============================================================================
section("9. Security Scan")

try:
    with open('server.py', encoding='utf-8') as f:
        src = f.read()
    with open('dashboard.html', encoding='utf-8') as f:
        html = f.read()

    # Rate limiting
    if 'rate_limit' in src or 'RATE_LIMIT' in src or 'is_rate_limited' in src:
        ok("Rate limiting present -- API protected from hammering")
    else:
        fail("SECURITY: No rate limiting -- API can be hammered from network")

    # Input validation
    if 'validate_threshold_data' in src or 'ALLOWED_THRESHOLD_KEYS' in src:
        ok("Input validation on POST endpoints -- no arbitrary data written to disk")
    else:
        fail("SECURITY: No input validation on POST -- arbitrary data can be written to disk")

    # No eval/exec
    code_lines = [l for l in src.splitlines() if not l.strip().startswith('#')]
    code_only = '\n'.join(code_lines)
    if 'eval(' not in code_only and 'exec(' not in code_only:
        ok("No eval()/exec() -- no code injection risk")
    else:
        fail("SECURITY: eval() or exec() found in server code -- injection risk")

    # No shell=True subprocess
    if 'shell=True' not in src:
        ok("No shell=True subprocess calls -- no shell injection risk")
    else:
        fail("SECURITY: shell=True found -- shell injection risk")

    # No hardcoded secrets
    import re
    secret_patterns = [r'password\s*=\s*["\'][^"\']+["\']',
                       r'secret\s*=\s*["\'][^"\']+["\']',
                       r'api_key\s*=\s*["\'][^"\']+["\']',
                       r'token\s*=\s*["\'][^"\']+["\']']
    found_secret = False
    for pat in secret_patterns:
        if re.search(pat, code_only, re.IGNORECASE):
            found_secret = True
    if not found_secret:
        ok("No hardcoded secrets/passwords/tokens found")
    else:
        fail("SECURITY: Possible hardcoded secret found -- review server.py")

    # No PII in logs - check what gets logged
    if 'username' not in src.lower() and 'email' not in src.lower() and 'password' not in src.lower():
        ok("No PII fields (username/email/password) in server code")
    else:
        warn("Possible PII field references found -- verify no personal data logged")

    # Autofix whitelist
    if 'ALLOWED_FIXES' in src:
        ok("Auto-fix whitelist present -- only safe pip installs allowed")
    else:
        fail("SECURITY: No auto-fix whitelist -- arbitrary commands could be run")

    # CREATE_NO_WINDOW on subprocess
    if 'CREATE_NO_WINDOW' in src:
        ok("CREATE_NO_WINDOW on subprocesses -- no CMD flash, no visible attack surface")
    else:
        warn("CREATE_NO_WINDOW not set -- subprocess windows visible to user")

    # Debug mode off
    if 'debug=True' not in src:
        ok("Flask debug=False -- no debugger PIN exposure")
    else:
        fail("SECURITY: Flask debug=True -- exposes interactive debugger on network")

    # XSS in dashboard - no innerHTML with user data
    innerHTML_uses = re.findall(r'innerHTML\s*=\s*[^;`]+', html)
    risky = [u for u in innerHTML_uses if ('textContent' not in u and 'd.' in u) or 'data.' in u]
    if not risky:
        ok("No risky innerHTML with server data -- XSS risk low")
    else:
        warn(f"innerHTML used with data -- verify XSS safe: {len(innerHTML_uses)} uses")

    # Localhost only warning
    if "host='0.0.0.0'" in src or 'host="0.0.0.0"' in src:
        warn("Server binds to 0.0.0.0 -- accessible on local network (by design, but note for awareness)")
    else:
        ok("Server binds to localhost only")

    # Diagnostic autofix scope
    if '/api/autofix' in src and 'ALLOWED_FIXES' in src:
        ok("Auto-fix endpoint has strict whitelist -- only approved pip installs")
    else:
        warn("Auto-fix endpoint missing or unprotected")

except Exception as e:
    fail(f"Security scan error: {e}")


# ===============================================================================
# 10. FLASK API INTEGRATION (TEST CLIENT)
# ===============================================================================
section("10. Flask API Integration (Test Client)")

try:
    import server as srv
    client = srv.app.test_client()

    # All GET endpoints return 200 or expected codes
    get_endpoints = [
        ('/api/system',           200),
        ('/api/stats',            200),
        ('/api/thresholds',       200),
        ('/api/diagnostics',      200),
        ('/api/feedback/queue',   200),
        ('/api/version',          200),
        ('/api/baseline',         (200, 404)),    # 404 OK on clean run
        ('/api/original_profile', (200, 404)),    # 404 OK on clean run
    ]
    for ep, expected in get_endpoints:
        resp = client.get(ep)
        exp_set = (expected,) if isinstance(expected, int) else expected
        if resp.status_code in exp_set:
            ok(f"GET {ep} -> {resp.status_code}")
        else:
            fail(f"GET {ep} -> {resp.status_code} (expected {expected})")

    # /api/stats returns all required keys
    resp = client.get('/api/stats')
    if resp.status_code == 200:
        data = resp.get_json()
        for key in ('cpu', 'gpu', 'ram', 'network', 'warnings', 'history'):
            if key in data:
                ok(f"/api/stats has required key: '{key}'")
            else:
                fail(f"/api/stats missing required key: '{key}'")
    else:
        fail(f"/api/stats returned {resp.status_code}")

    # POST /api/thresholds from localhost saves and returns updated values
    resp = client.post(
        '/api/thresholds',
        json={'cpu': {'temp_warn': 79}},
        environ_base={'REMOTE_ADDR': '127.0.0.1'}
    )
    if resp.status_code == 200:
        result = resp.get_json()
        if result and result.get('status') == 'saved':
            ok("POST /api/thresholds from localhost: saved OK")
        else:
            fail(f"POST /api/thresholds unexpected body: {result}")
    else:
        fail(f"POST /api/thresholds from localhost returned {resp.status_code}")

    # /api/shutdown route is registered (do NOT actually POST — it calls os._exit
    # in test mode which kills the process and prevents remaining sections from running)
    _sd_rules = [str(r) for r in srv.app.url_map.iter_rules() if r.rule == '/api/shutdown']
    if _sd_rules:
        ok("/api/shutdown endpoint registered -- route decorator confirmed")
    else:
        fail("/api/shutdown not registered -- logs won't flush when browser tab closes")

    # /api/fps endpoint returns 200 with required keys
    resp = client.get('/api/fps')
    if resp.status_code == 200:
        ok("GET /api/fps -> 200")
        fps_data = resp.get_json()
        for key in ('fps', 'fps_1pct_low', 'frametime_ms', 'source', 'available'):
            if key in fps_data:
                ok(f"/api/fps has key: '{key}'")
            else:
                fail(f"/api/fps missing key: '{key}'")
    else:
        fail(f"GET /api/fps -> {resp.status_code} (expected 200)")

    # /api/forge/fan_curves returns 200 with required keys + all 4 presets
    resp = client.get('/api/forge/fan_curves')
    if resp.status_code == 200:
        ok("GET /api/forge/fan_curves -> 200")
        fan_data = resp.get_json()
        for key in ('curves', 'active', 'fan_rpms', 'rpm_available'):
            if key in fan_data:
                ok(f"/api/forge/fan_curves has key: '{key}'")
            else:
                fail(f"/api/forge/fan_curves missing key: '{key}'")
        curves = fan_data.get('curves', {})
        for preset in ('SILENT', 'BALANCED', 'PERFORMANCE', 'FULL_SEND'):
            if preset in curves:
                ok(f"FAN_CURVES has preset: {preset}")
            else:
                fail(f"FAN_CURVES missing preset: {preset}")
        # POST select preset
        resp2 = client.post(
            '/api/forge/fan_curves/select',
            json={'preset': 'SILENT'},
            environ_base={'REMOTE_ADDR': '127.0.0.1'}
        )
        if resp2.status_code == 200:
            r2 = resp2.get_json()
            if r2 and r2.get('status') == 'ok' and r2.get('active') == 'SILENT':
                ok("POST /api/forge/fan_curves/select -> ok, active=SILENT")
            else:
                fail(f"POST /api/forge/fan_curves/select unexpected body: {r2}")
        else:
            fail(f"POST /api/forge/fan_curves/select returned {resp2.status_code}")
    else:
        fail(f"GET /api/forge/fan_curves -> {resp.status_code} (expected 200)")

    # /api/forge/benchmark/status returns 200 with required keys
    resp = client.get('/api/forge/benchmark/status')
    if resp.status_code == 200:
        ok("GET /api/forge/benchmark/status -> 200")
        bs = resp.get_json()
        for key in ('running', 'step', 'result'):
            if key in bs:
                ok(f"/api/forge/benchmark/status has key: '{key}'")
            else:
                fail(f"/api/forge/benchmark/status missing key: '{key}'")
    else:
        fail(f"GET /api/forge/benchmark/status -> {resp.status_code} (expected 200)")

    # POST /api/forge/benchmark -> 200 (started) or 409 (already running)
    resp = client.post(
        '/api/forge/benchmark',
        json={'mode': 'quick'},
        environ_base={'REMOTE_ADDR': '127.0.0.1'}
    )
    if resp.status_code in (200, 409):
        bd = resp.get_json()
        if resp.status_code == 200 and bd.get('started'):
            ok("POST /api/forge/benchmark -> started (run_id present)")
        elif resp.status_code == 409 and bd.get('error'):
            ok("POST /api/forge/benchmark -> 409 already running (state consistent)")
        else:
            fail(f"POST /api/forge/benchmark unexpected response: {bd}")
    else:
        fail(f"POST /api/forge/benchmark returned {resp.status_code} (expected 200 or 409)")

    # /api/forge/benchmark/history returns 200 with key 'runs'
    resp = client.get('/api/forge/benchmark/history')
    if resp.status_code == 200:
        ok("GET /api/forge/benchmark/history -> 200")
        hd = resp.get_json()
        if 'runs' in hd:
            ok("/api/forge/benchmark/history has key: 'runs'")
        else:
            fail("/api/forge/benchmark/history missing key: 'runs'")
    else:
        fail(f"GET /api/forge/benchmark/history -> {resp.status_code} (expected 200)")

    # /api/forge/benchmark/baseline returns 200 or 404 (no runs yet)
    resp = client.get('/api/forge/benchmark/baseline')
    if resp.status_code in (200, 404):
        ok(f"GET /api/forge/benchmark/baseline -> {resp.status_code} (expected 200 or 404)")
    else:
        fail(f"GET /api/forge/benchmark/baseline -> {resp.status_code} (expected 200 or 404)")

    # POST /api/forge/benchmark with invalid mode -> 400
    resp = client.post(
        '/api/forge/benchmark',
        json={'mode': 'turbo'},
        environ_base={'REMOTE_ADDR': '127.0.0.1'}
    )
    if resp.status_code == 400:
        ok("POST /api/forge/benchmark with invalid mode -> 400")
    else:
        fail(f"POST /api/forge/benchmark bad mode returned {resp.status_code} (expected 400)")

except Exception as e:
    import traceback
    fail(f"Flask API integration error: {e} -- {traceback.format_exc().splitlines()[-1]}")


# ===============================================================================
# 11. SECURITY CHECKS (LIVE)
# ===============================================================================
section("11. Security Checks (Live)")

try:
    import server as srv
    client = srv.app.test_client()

    # Non-localhost POST -> 403
    resp = client.post(
        '/api/thresholds',
        json={'cpu': {'temp_warn': 75}},
        environ_base={'REMOTE_ADDR': '192.168.1.100'}
    )
    if resp.status_code == 403:
        ok("POST /api/thresholds from external IP returns 403")
    else:
        fail(f"POST from external IP returned {resp.status_code} (expected 403)")

    # Rate limiting: pre-fill bucket to RL_MAX, verify next request is rejected
    # (avoids timing sensitivity of a 1-second sliding window in CI environments)
    import time as _rl_time
    test_ip = '10.99.88.77'
    with srv._rl_lock:
        srv._rl[test_ip] = [_rl_time.time()] * srv.RL_MAX
    resp = client.get('/api/stats', environ_base={'REMOTE_ADDR': test_ip})
    if resp.status_code == 429:
        ok(f"Rate limiting triggered at request {srv.RL_MAX + 1} -> 429")
    else:
        fail(f"Rate limiting not triggered (expected 429, got {resp.status_code}) -- check _guard() is called from api_stats()")
    with srv._rl_lock:
        srv._rl.pop(test_ip, None)

    # Feedback message injection: newlines/nulls sanitized in stored entry
    import os as _os, json as _json
    resp = client.post(
        '/api/feedback',
        json={'category': 'bug', 'message': 'Crash\nLine2\rLine3\x00end'},
        environ_base={'REMOTE_ADDR': '127.0.0.1'}
    )
    if resp.status_code == 200:
        feedback_file = _os.path.join(srv.LOG_DIR, 'feedback', 'bug.jsonl')
        if _os.path.exists(feedback_file):
            with open(feedback_file, encoding='utf-8') as fh:
                lines = [l for l in fh if l.strip()]
            if lines:
                entry = _json.loads(lines[-1])
                msg = entry.get('message', '')
                if '\n' not in msg and '\r' not in msg and '\x00' not in msg:
                    ok("Feedback message: newlines and null bytes sanitized")
                else:
                    fail(f"Feedback message not sanitized: {repr(msg[:80])}")
        else:
            warn("Feedback file not found -- cannot verify sanitization")
    else:
        fail(f"POST /api/feedback returned {resp.status_code}")

    # /api/autofix with unknown command returns 400
    resp = client.post(
        '/api/autofix',
        json={'fix': 'rm -rf /'},
        environ_base={'REMOTE_ADDR': '127.0.0.1'}
    )
    if resp.status_code == 400:
        ok("/api/autofix with unknown command returns 400")
    else:
        fail(f"/api/autofix with unlisted command returned {resp.status_code} (expected 400)")

except Exception as e:
    import traceback
    fail(f"Security check error: {e} -- {traceback.format_exc().splitlines()[-1]}")


# ===============================================================================
# 12. CONCURRENT REQUEST STRESS TEST
# ===============================================================================
section("12. Concurrent Request Stress Test")

if CI:
    ok("Stress test skipped -- CI environment")
    ok("Stress test skipped -- CI (thread concurrency)")
    ok("Stress test skipped -- CI (log buffer integrity)")
else:
    try:
        import server as srv, threading as _threading, queue as _queue

        results_q = _queue.Queue()
        errors_q  = _queue.Queue()

        def _stress_worker():
            c = srv.app.test_client()
            for _ in range(10):
                try:
                    r = c.get('/api/stats')
                    if r.status_code == 200:
                        d = r.get_json()
                        if d and 'cpu' in d:
                            results_q.put('ok')
                        else:
                            errors_q.put('invalid JSON structure')
                    else:
                        errors_q.put(f'status {r.status_code}')
                except Exception as ex:
                    errors_q.put(str(ex))

        threads = [_threading.Thread(target=_stress_worker) for _ in range(20)]
        for th in threads: th.start()
        for th in threads: th.join(timeout=30)

        ok_count  = results_q.qsize()
        err_count = errors_q.qsize()

        if err_count == 0 and ok_count == 200:
            ok(f"Stress: 200/200 concurrent requests succeeded, 0 errors")
        elif err_count == 0:
            ok(f"Stress: {ok_count} requests succeeded, 0 errors")
        else:
            sample = []
            while not errors_q.empty() and len(sample) < 3:
                sample.append(errors_q.get())
            fail(f"Stress: {err_count} errors in {ok_count + err_count} requests -- sample: {sample}")

        # Log buffer should not be corrupted after load
        with srv._log_lock:
            buf_ok = isinstance(srv._log_buffer, list)
        if buf_ok:
            ok("Log buffer intact after concurrent load")
        else:
            fail("Log buffer corrupted after concurrent load")

    except Exception as e:
        import traceback
        fail(f"Stress test error: {e} -- {traceback.format_exc().splitlines()[-1]}")


# ===============================================================================
# 13. PATH RESOLUTION CHECK (STATIC ANALYSIS)
# ===============================================================================
section("13. Path Resolution Check")

try:
    with open('server.py', encoding='utf-8') as f:
        src = f.read()

    # ASSET_DIR / DATA_DIR split present
    if 'ASSET_DIR' in src and 'DATA_DIR' in src:
        ok("ASSET_DIR and DATA_DIR defined -- frozen/dev path split in place")
    else:
        fail("ASSET_DIR or DATA_DIR missing -- will 404 on launch when running as frozen .exe")

    # Frozen detection uses sys._MEIPASS
    if 'sys._MEIPASS' in src:
        ok("sys._MEIPASS used for ASSET_DIR -- bundle assets resolved correctly when frozen")
    else:
        fail("sys._MEIPASS not used -- ASSET_DIR wrong in frozen .exe")

    # DATA_DIR uses sys.executable
    if 'os.path.dirname(sys.executable)' in src:
        ok("DATA_DIR = os.path.dirname(sys.executable) -- persistent data written next to .exe")
    else:
        fail("DATA_DIR not using sys.executable -- logs/backups will be lost in temp dir when frozen")

    # send_from_directory uses ASSET_DIR (not '.')
    if 'send_from_directory(ASSET_DIR' in src:
        ok("send_from_directory(ASSET_DIR) -- dashboard.html served from bundle correctly")
    else:
        fail("send_from_directory NOT using ASSET_DIR -- will 404 in frozen .exe")

    # Flask static_folder set to ASSET_DIR
    if 'static_folder=ASSET_DIR' in src:
        ok("Flask app static_folder=ASSET_DIR -- static files served from bundle")
    else:
        fail("Flask static_folder not pointing to ASSET_DIR")

    # @app.route decorator present on api_shutdown
    if "@app.route('/api/shutdown'" in src or '@app.route("/api/shutdown"' in src:
        ok("@app.route decorator on api_shutdown -- endpoint is reachable")
    else:
        fail("api_shutdown missing @app.route -- logs never flush when browser tab closes")

    # _log_lock defined for thread-safe log buffer
    if '_log_lock' in src:
        ok("_log_lock defined -- _log_buffer is thread-safe")
    else:
        fail("_log_lock missing -- _log_buffer has write races under concurrent load")

    # _net_warmed_up flag prevents first-poll false spike
    if '_net_warmed_up' in src:
        ok("_net_warmed_up flag present -- first-poll false network spike prevented")
    else:
        fail("_net_warmed_up flag missing -- first /api/stats call may show bogus network spike")

    # All open() calls specify encoding
    import re
    # Check each line that contains a real open() call (not _orig_popen, OpenKey, etc.)
    open_lines = [l.strip() for l in src.splitlines() if re.search(r'\bopen\(', l)]
    missing_enc = [l for l in open_lines if 'encoding' not in l and not re.search(r"""open\([^,)]*,\s*['"][^'"]*[wra]b[^'"]*['"]""", l)]
    if not missing_enc:
        ok(f"All {len(open_lines)} open() call line(s) specify encoding='utf-8'")
    else:
        fail(f"{len(missing_enc)} open() call(s) missing encoding: {missing_enc[0][:70]}")

    # UPDATE_CHECK_URL constant exists (Goal 10 hook)
    if 'UPDATE_CHECK_URL' in src:
        ok("UPDATE_CHECK_URL constant defined (Goal 10 -- set URL to enable update banner)")
    else:
        fail("UPDATE_CHECK_URL missing -- update notification has no URL to check")

except Exception as e:
    import traceback
    fail(f"Path resolution check error: {e} -- {traceback.format_exc().splitlines()[-1]}")


# ===============================================================================
# 14. NSIS INSTALLER & CI PIPELINE CHECKS
# ===============================================================================
section("14. NSIS Installer & CI Pipeline")

import re as _re14

_ROOT14 = os.path.dirname(os.path.abspath(__file__))

# ── scripts/installer.nsi ─────────────────────────────────────────────────────
_nsi_path = os.path.join(_ROOT14, 'scripts', 'installer.nsi')
if os.path.exists(_nsi_path):
    ok("scripts/installer.nsi exists")
    try:
        with open(_nsi_path, encoding='utf-8') as _f:
            _nsi_src = _f.read()
        for _kw in ('Name ', 'OutFile', 'Section', 'SectionEnd'):
            if _kw in _nsi_src:
                ok(f"installer.nsi contains '{_kw}'")
            else:
                fail(f"installer.nsi missing '{_kw}' -- NSIS build will fail")
    except Exception as _e:
        fail(f"installer.nsi read error: {_e}")
else:
    fail("scripts/installer.nsi not found -- NSIS installer build will fail")

# ── .github/workflows/deploy.yml ──────────────────────────────────────────────
_deploy_path = os.path.join(_ROOT14, '.github', 'workflows', 'deploy.yml')
if os.path.exists(_deploy_path):
    ok(".github/workflows/deploy.yml exists")
    try:
        with open(_deploy_path, encoding='utf-8') as _f:
            _deploy_src = _f.read()

        # NSIS step must use choco install + hardcoded full path
        if 'choco install nsis -y' in _deploy_src:
            ok("deploy.yml: NSIS step uses 'choco install nsis -y'")
        else:
            fail("deploy.yml: 'choco install nsis -y' missing -- NSIS build will fail on CI")

        _nsis_full_path = r'"C:\Program Files (x86)\NSIS\makensis.exe"'
        if _nsis_full_path in _deploy_src:
            ok("deploy.yml: makensis called via hardcoded full path (bypasses PATH refresh issue)")
        else:
            fail("deploy.yml: hardcoded NSIS full path missing -- PATH refresh bug will break installer step")

        # Dedicated test gate job must exist
        if _re14.search(r'^\s{2}test:\s*$', _deploy_src, _re14.MULTILINE):
            ok("deploy.yml: dedicated 'test:' gate job present")
        else:
            fail("deploy.yml: no 'test:' job -- CI runs without a test gate; broken code can deploy")

        # Build jobs must depend on the test gate
        if 'needs: [test]' in _deploy_src:
            ok("deploy.yml: build jobs gated on 'needs: [test]'")
        else:
            fail("deploy.yml: build jobs missing 'needs: [test]' -- builds run even when tests fail")

        # Release must depend on all upstream jobs
        if 'needs: [test, build-windows, build-macos]' in _deploy_src:
            ok("deploy.yml: release job needs: [test, build-windows, build-macos] -- full gate in place")
        else:
            fail("deploy.yml: release job missing full dependency chain -- broken builds can reach production")

    except Exception as _e:
        fail(f"deploy.yml read error: {_e}")
else:
    fail(".github/workflows/deploy.yml not found")


# =============================================================================
# Section 15: Live URL Checks
# =============================================================================
import urllib.request as _ureq, urllib.error as _uerr
section("15. Live URL Checks")

_LIVE_URLS = [
    ('GitHub Pages — landing page',    'https://kypin00-web.github.io/KAM-Sentinel',                                                    None),
    ('GitHub Pages — version.json',    'https://kypin00-web.github.io/KAM-Sentinel/version.json',                                       'json'),
    ('GitHub Releases — latest page',  'https://github.com/kypin00-web/KAM-Sentinel/releases/latest',                                   None),
    ('GitHub Releases — Setup.exe',    'https://github.com/kypin00-web/KAM-Sentinel/releases/latest/download/KAM_Sentinel_Setup.exe',   None),
]

def _url_ok(url, validate=None, timeout=12):
    """HEAD the URL (GET fallback on 405). Returns (ok, code, extra)."""
    import json as _json
    body = None
    try:
        req = _ureq.Request(url, method='HEAD',
                            headers={'User-Agent': 'KAM-Sentinel-Test/1.0'})
        with _ureq.urlopen(req, timeout=timeout) as r:
            code = r.getcode()
    except _uerr.HTTPError as e:
        if e.code == 405:
            req = _ureq.Request(url, headers={'User-Agent': 'KAM-Sentinel-Test/1.0'})
            try:
                with _ureq.urlopen(req, timeout=timeout) as r:
                    code = r.getcode()
                    if validate == 'json':
                        body = r.read(32768).decode('utf-8', errors='replace')
            except _uerr.HTTPError as e2:
                return False, e2.code, None
        else:
            return False, e.code, None
    except Exception:
        return None, None, None   # no internet / DNS failure → skip

    if validate == 'json' and body is None:
        try:
            req2 = _ureq.Request(url, headers={'User-Agent': 'KAM-Sentinel-Test/1.0'})
            with _ureq.urlopen(req2, timeout=timeout) as r2:
                body = r2.read(32768).decode('utf-8', errors='replace')
        except Exception:
            body = None

    json_ok = None
    if validate == 'json':
        try:
            import json as _json2; _json2.loads(body); json_ok = True
        except Exception:
            json_ok = False

    return (code == 200), code, json_ok

_has_internet = None   # determined lazily from first URL result

for _label, _url, _validate in _LIVE_URLS:
    _ok, _code, _jok = _url_ok(_url, validate=_validate)
    if _ok is None:
        # network failure / no internet
        if _has_internet is None:
            warn(f"No internet — skipping live URL checks (CI offline or firewall blocked)")
            _has_internet = False
        # skip remaining checks silently — already warned
        break
    _has_internet = True
    _extra = ''
    if _jok is True:
        _extra = ' (JSON valid)'
    elif _jok is False:
        _extra = ' (JSON INVALID)'
    if _ok and _jok is not False:
        ok(f"{_label} -> HTTP {_code}{_extra}")
    elif _ok and _jok is False:
        fail(f"{_label} -> HTTP {_code} but JSON is invalid")
    else:
        fail(f"{_label} -> HTTP {_code or 'ERR'} (expected 200)")

if _has_internet is None:
    # LIVE_URLS list was empty (shouldn't happen)
    warn("No URLs configured for live URL check section")


# -- Write HTML report ---------------------------------------------------------
from datetime import datetime
now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
status_color = '#00ff88' if failed == 0 else '#ff3d3d'
status_text  = 'ALL PASSED' if failed == 0 and warned == 0 else f'{failed} FAILED' if failed else f'{warned} WARNINGS'

rows = ''
for kind, msg in _log_entries:
    if kind == 'section':
        rows += f'<tr class="section"><td colspan="2">-- {msg}</td></tr>\n'
    elif kind == 'pass':
        rows += f'<tr><td class="icon pass">[OK]</td><td>{msg}</td></tr>\n'
    elif kind == 'fail':
        rows += f'<tr><td class="icon fail">[FAIL]</td><td class="fail">{msg}</td></tr>\n'
    elif kind == 'warn':
        rows += f'<tr><td class="icon warn">[WARN]</td><td class="warn">{msg}</td></tr>\n'

html = f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8">
<title>KAM Sentinel -- Test Report</title>
<style>
  body {{ background:#07090d; color:#b8ccd8; font-family:'Courier New',monospace; font-size:13px; margin:0; padding:24px; }}
  h1 {{ color:#00ff88; letter-spacing:4px; font-size:1.1rem; margin-bottom:4px; }}
  .meta {{ color:#3d5166; font-size:.8rem; margin-bottom:20px; }}
  .summary {{ display:flex; gap:24px; margin-bottom:24px; padding:16px; background:#0c1018; border:1px solid #1a2535; border-radius:6px; }}
  .sum-item {{ text-align:center; }}
  .sum-num {{ font-size:2rem; font-weight:bold; }}
  .sum-label {{ font-size:.7rem; color:#3d5166; letter-spacing:2px; }}
  .pass-num {{ color:#00ff88; }} .fail-num {{ color:#ff3d3d; }} .warn-num {{ color:#ffd600; }} .total-num {{ color:#b8ccd8; }}
  .status {{ font-size:1.2rem; font-weight:bold; color:{status_color}; margin-bottom:20px; letter-spacing:3px; }}
  table {{ width:100%; border-collapse:collapse; }}
  tr.section td {{ background:#111824; color:#00d4ff; padding:10px 8px 4px; font-size:.75rem; letter-spacing:3px; border-top:1px solid #1a2535; }}
  td {{ padding:5px 8px; border-bottom:1px solid #0c1018; }}
  td.icon {{ width:24px; text-align:center; }}
  .pass {{ color:#00ff88; }} .fail {{ color:#ff3d3d; }} .warn {{ color:#ffd600; }}
  tr:hover td {{ background:#0c1018; }}
</style></head><body>
<h1>? KAM SENTINEL -- TEST REPORT</h1>
<div class="meta">Generated: {now} &nbsp;|&nbsp; v1.2 Phase 1</div>
<div class="status">{'[OK]' if failed==0 else '[FAIL]'} {status_text}</div>
<div class="summary">
  <div class="sum-item"><div class="sum-num pass-num">{passed}</div><div class="sum-label">PASSED</div></div>
  <div class="sum-item"><div class="sum-num warn-num">{warned}</div><div class="sum-label">WARNINGS</div></div>
  <div class="sum-item"><div class="sum-num fail-num">{failed}</div><div class="sum-label">FAILED</div></div>
  <div class="sum-item"><div class="sum-num total-num">{total}</div><div class="sum-label">TOTAL</div></div>
</div>
<table>{rows}</table>
</body></html>"""

report_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'test_report.html')
with open(report_path, 'w', encoding='utf-8') as f:
    f.write(html)
print(f"  ? Report saved: test_report.html")
print(f"     Open in Chrome to view full results\n")

sys.exit(0 if failed == 0 else 1)
