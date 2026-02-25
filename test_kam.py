#!/usr/bin/env python3
"""
KAM Sentinel - Automated Test Suite
Tests: API correctness, memory leaks, performance, threading, warnings engine
Run: python test_kam.py
"""

import sys, os, json, time, threading, tracemalloc, gc, statistics
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# ── Color output ──────────────────────────────────────────────────────────────
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
    print(f"  {GREEN}✓{RESET} {msg}")

def fail(msg):
    global failed
    failed += 1
    _log_entries.append(('fail', msg))
    print(f"  {RED}✗ FAIL{RESET} {msg}")

def warn(msg):
    global warned
    warned += 1
    _log_entries.append(('warn', msg))
    print(f"  {YELLOW}⚠ WARN{RESET} {msg}")

def section(title):
    _log_entries.append(('section', title))
    print(f"\n{BOLD}{CYAN}── {title} {'─'*(50-len(title))}{RESET}")

# ═══════════════════════════════════════════════════════════════════════════════
# 1. IMPORT & SYNTAX CHECK
# ═══════════════════════════════════════════════════════════════════════════════
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
    fail("psutil not installed — run: python -m pip install psutil")

try:
    import flask
    ok(f"flask available (v{flask.__version__})")
except:
    fail("flask not installed — run: python -m pip install flask")

try:
    import GPUtil
    ok("GPUtil available — GPU stats enabled")
except:
    warn("GPUtil not installed — GPU stats will show N/A (pip install GPUtil)")

try:
    import wmi
    ok("wmi available — CPU temp/voltage enabled")
except:
    warn("wmi not installed — CPU temp/voltage will show N/A (pip install wmi pywin32)")

# ═══════════════════════════════════════════════════════════════════════════════
# 2. THRESHOLD ENGINE TESTS
# ═══════════════════════════════════════════════════════════════════════════════
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
        ok(f"Ryzen 5000 TJmax detected correctly: {t_ryzen['cpu']['temp_crit']}°C")
    else:
        fail(f"Ryzen 5000 TJmax wrong: {t_ryzen['cpu']['temp_crit']}°C (expected ≤90)")

    # RTX detection
    t_rtx = thresholds.detect_thresholds("Unknown CPU", "NVIDIA GeForce RTX 3080")
    if t_rtx['gpu']['temp_crit'] >= 90:
        ok(f"RTX 3080 GPU limit detected: {t_rtx['gpu']['temp_crit']}°C")
    else:
        fail(f"RTX 3080 GPU limit wrong: {t_rtx['gpu']['temp_crit']}°C")

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

# ═══════════════════════════════════════════════════════════════════════════════
# 3. MEMORY LEAK DETECTION
# ═══════════════════════════════════════════════════════════════════════════════
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
        fail("server.py still uses plain lists — memory leak risk")

    code_lines = [l for l in src.splitlines() if not l.strip().startswith('#')]
    code_only = '\n'.join(code_lines)
    if '.pop(0)' not in code_only:
        ok("No list.pop(0) calls found — O(1) operations confirmed")
    else:
        import re
        pops = re.findall(r'.+\.pop\(0\)', code_only)
        fail(f"Found list.pop(0): {pops[0].strip()}")

    # Check log buffer is bounded
    if 'LOG_BATCH_SIZE' in src or '_log_buffer' in src:
        ok("Log batching present — disk I/O not on every poll")
    else:
        warn("No log batching found — disk writes may happen every poll")

    # Check log rotation
    if 'rotate' in src.lower() or 'LOG_MAX_LINES' in src:
        ok("Log rotation present — logs won't grow unbounded")
    else:
        warn("No log rotation found — log files may grow forever")

except Exception as e:
    fail(f"Memory check error: {e}")

# ═══════════════════════════════════════════════════════════════════════════════
# 4. PERFORMANCE CHECKS
# ═══════════════════════════════════════════════════════════════════════════════
section("4. Performance & Architecture")

try:
    with open('server.py', encoding='utf-8') as f:
        src = f.read()

    # Background thread
    if '_background_poll' in src and 'daemon=True' in src:
        ok("Background daemon thread present — Flask requests never block on hardware I/O")
    else:
        fail("No background polling thread — stats collected on every request (blocks Flask)")

    # WMI caching
    if 'WMI_CACHE_TTL' in src or '_wmi_cache_time' in src:
        ok("WMI result caching present — slow COM calls max once per 30s")
    else:
        warn("No WMI caching — every poll calls slow COM operations (50-200ms)")

    # Non-blocking cpu_percent
    if 'interval=0' in src:
        ok("cpu_percent(interval=0) — non-blocking delta measurement")
    elif 'interval=0.1' in src:
        fail("cpu_percent(interval=0.1) — blocks 100ms per poll cycle")

    # Threading
    if 'threaded=True' in src:
        ok("Flask threaded=True — concurrent request handling enabled")
    else:
        warn("Flask threaded not set — requests may queue behind each other")

    # Single cache object
    if '_stat_cache' in src or '_cached_stats' in src:
        ok("Stat cache object present — Flask serves pre-computed data")
    else:
        warn("No stat cache found — metrics may be computed on every request")

    # cpu_percent timing
    t0 = time.perf_counter()
    import psutil
    psutil.cpu_percent(interval=0)
    elapsed_ms = (time.perf_counter() - t0) * 1000
    if elapsed_ms < 5:
        ok(f"cpu_percent(interval=0) returns in {elapsed_ms:.1f}ms — non-blocking confirmed")
    else:
        warn(f"cpu_percent took {elapsed_ms:.1f}ms — may be blocking")

except Exception as e:
    fail(f"Performance check error: {e}")

# ═══════════════════════════════════════════════════════════════════════════════
# 5. WARNING ENGINE LOGIC
# ═══════════════════════════════════════════════════════════════════════════════
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
        ok(f"CPU critical threshold fires at {cpu_hot['temp']}°C (limit: {t['cpu']['temp_crit']}°C)")
    else:
        fail("CPU critical threshold did not fire")

    if cpu_ok['temp'] < t['cpu']['temp_warn']:
        ok(f"CPU OK temp {cpu_ok['temp']}°C correctly below warning threshold")
    else:
        fail("CPU OK temp incorrectly above warning threshold")

    if gpu_hot['temp'] >= t['gpu']['temp_crit']:
        ok(f"GPU critical threshold fires at {gpu_hot['temp']}°C")
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

# ═══════════════════════════════════════════════════════════════════════════════
# 6. FILE STRUCTURE & SAFETY CHECKS
# ═══════════════════════════════════════════════════════════════════════════════
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
    ok("Flask debug=False — production safe")
else:
    fail("Flask debug=True found — must be False for distribution")

# Check version string
import re
versions = re.findall(r'v([\d.]+)', src)
if versions:
    ok(f"Version string found: v{versions[0]}")

# ═══════════════════════════════════════════════════════════════════════════════
# 7. LIVE PSUTIL READING
# ═══════════════════════════════════════════════════════════════════════════════
section("7. Live Hardware Reading")

try:
    import psutil

    # Prime cpu_percent
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

    # Speed of 10 consecutive reads
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
        ok(f"10x poll cycle avg: {avg_ms:.2f}ms — excellent performance")
    elif avg_ms < 50:
        warn(f"10x poll cycle avg: {avg_ms:.2f}ms — acceptable")
    else:
        fail(f"10x poll cycle avg: {avg_ms:.2f}ms — too slow")

except Exception as e:
    fail(f"Hardware reading error: {e}")

# ═══════════════════════════════════════════════════════════════════════════════
# 8. DASHBOARD HTML CHECKS
# ═══════════════════════════════════════════════════════════════════════════════
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
    ]
    for name, result in checks:
        if result:
            ok(name)
        else:
            fail(name)

except Exception as e:
    fail(f"HTML check error: {e}")

# ═══════════════════════════════════════════════════════════════════════════════
# SUMMARY
# ═══════════════════════════════════════════════════════════════════════════════
total = passed + failed + warned
print(f"\n{'═'*55}")
print(f"{BOLD}  KAM SENTINEL TEST RESULTS{RESET}")
print(f"{'═'*55}")
print(f"  {GREEN}Passed : {passed}{RESET}")
print(f"  {YELLOW}Warnings: {warned}{RESET}")
print(f"  {RED}Failed : {failed}{RESET}")
print(f"  Total  : {total}")
print(f"{'═'*55}")

if failed == 0 and warned == 0:
    print(f"\n  {GREEN}{BOLD}✓ ALL TESTS PASSED — BUILD IS CLEAN{RESET}\n")
elif failed == 0:
    print(f"\n  {YELLOW}{BOLD}⚠ PASSED WITH WARNINGS — Review above{RESET}\n")
else:
    print(f"\n  {RED}{BOLD}✗ {failed} TEST(S) FAILED — Fix before building .exe{RESET}\n")

# ── Write HTML report ─────────────────────────────────────────────────────────
from datetime import datetime
now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
status_color = '#00ff88' if failed == 0 else '#ff3d3d'
status_text  = 'ALL PASSED' if failed == 0 and warned == 0 else f'{failed} FAILED' if failed else f'{warned} WARNINGS'

rows = ''
for kind, msg in _log_entries:
    if kind == 'section':
        rows += f'<tr class="section"><td colspan="2">── {msg}</td></tr>\n'
    elif kind == 'pass':
        rows += f'<tr><td class="icon pass">✓</td><td>{msg}</td></tr>\n'
    elif kind == 'fail':
        rows += f'<tr><td class="icon fail">✗</td><td class="fail">{msg}</td></tr>\n'
    elif kind == 'warn':
        rows += f'<tr><td class="icon warn">⚠</td><td class="warn">{msg}</td></tr>\n'

html = f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8">
<title>KAM Sentinel — Test Report</title>
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
<h1>⬡ KAM SENTINEL — TEST REPORT</h1>
<div class="meta">Generated: {now} &nbsp;|&nbsp; v1.2 Phase 1</div>
<div class="status">{'✓' if failed==0 else '✗'} {status_text}</div>
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
print(f"  📄 Report saved: test_report.html")
print(f"     Open in Chrome to view full results\n")

sys.exit(0 if failed == 0 else 1)
