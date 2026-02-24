#!/usr/bin/env python3
"""
KAM Sentinel - Performance Dashboard Server v1.2
Phase 1: System Detection + Live Metrics + Baseline + Smart Warnings + Customizable Thresholds
"""

from flask import Flask, jsonify, send_from_directory, request
import psutil, os, json, time, platform, datetime, collections, threading

app = Flask(__name__, static_folder='.')

try:
    import GPUtil
    GPU_AVAILABLE = True
except ImportError:
    GPU_AVAILABLE = False

try:
    import wmi
    WMI_AVAILABLE = True
    _wmi = wmi.WMI()
except Exception:
    WMI_AVAILABLE = False
    _wmi = None

BASE_DIR          = os.path.dirname(os.path.abspath(__file__))
BACKUP_DIR        = os.path.join(BASE_DIR, 'backups')
LOG_DIR           = os.path.join(BASE_DIR, 'logs')
PROFILE_DIR       = os.path.join(BASE_DIR, 'profiles')
BASELINE_FILE     = os.path.join(PROFILE_DIR, 'baseline.json')
ORIG_PROFILE_FILE = os.path.join(BACKUP_DIR, 'original_system_profile.json')
VERSION_FILE      = os.path.join(BASE_DIR, 'version.json')

for d in [BACKUP_DIR, LOG_DIR, PROFILE_DIR]:
    os.makedirs(d, exist_ok=True)

_net_prev      = psutil.net_io_counters()
_net_time_prev = time.time()

MAX_HISTORY = 60
history = {
    'timestamps': collections.deque(maxlen=MAX_HISTORY),
    'cpu_usage':  collections.deque(maxlen=MAX_HISTORY),
    'cpu_temp':   collections.deque(maxlen=MAX_HISTORY),
    'gpu_usage':  collections.deque(maxlen=MAX_HISTORY),
    'gpu_temp':   collections.deque(maxlen=MAX_HISTORY),
    'ram_usage':  collections.deque(maxlen=MAX_HISTORY),
    'net_down':   collections.deque(maxlen=MAX_HISTORY),
    'net_up':     collections.deque(maxlen=MAX_HISTORY),
}

# Sustained-usage tracking (use deque for O(1) popleft instead of list-based removals)
_sustained = {'cpu': collections.deque(), 'gpu': collections.deque()}

# Network baseline for anomaly detection (deque for efficient popleft)
_net_baseline_samples = collections.deque()

# WMI cache (30s TTL) — WMI COM calls are slow (50–200ms), only re-run every 30s
_wmi_cache      = {}
_wmi_cache_time = 0.0
WMI_CACHE_TTL   = 30

# Background polling state
_cache_lock       = threading.Lock()
_cached_stats     = None
_log_buffer       = []
LOG_BATCH_SIZE    = 10   # flush to disk every N samples
LOG_MAX_LINES     = 5000 # rotate log file after this many lines

# ── Thresholds (loaded after system info) ────────────────────────────────────
from thresholds import load_thresholds, save_thresholds, detect_thresholds
_thresholds = None

# ═══════════════════════════════════════════════════════════════════════════════
# SYSTEM INFO
# ═══════════════════════════════════════════════════════════════════════════════

def get_system_info():
    info = {}
    info['os']           = platform.system()
    info['os_version']   = platform.version()
    info['os_release']   = platform.release()
    info['hostname']     = platform.node()
    info['windows_dir']  = os.environ.get('SystemRoot', 'N/A')
    info['cpu_name']     = platform.processor()
    info['cpu_cores']    = psutil.cpu_count(logical=False)
    info['cpu_threads']  = psutil.cpu_count(logical=True)
    freq = psutil.cpu_freq()
    info['cpu_max_ghz']     = round(freq.max/1000, 2) if freq else 'N/A'
    info['cpu_current_ghz'] = round(freq.current/1000, 2) if freq else 'N/A'
    ram = psutil.virtual_memory()
    info['ram_total_mb']      = round(ram.total/(1024**2))
    info['ram_total_gb']      = round(ram.total/(1024**3), 2)
    swap = psutil.swap_memory()
    info['pagefile_used_mb']  = round(swap.used/(1024**2))
    info['pagefile_total_mb'] = round(swap.total/(1024**2))
    info['gpu_name']    = 'N/A'
    info['gpu_vram_mb'] = 'N/A'
    if GPU_AVAILABLE:
        try:
            gpus = GPUtil.getGPUs()
            if gpus:
                info['gpu_name']    = gpus[0].name
                info['gpu_vram_mb'] = round(gpus[0].memoryTotal)
        except: pass
    info['manufacturer'] = 'N/A'
    info['model']        = 'N/A'
    info['bios_version'] = 'N/A'
    info['motherboard']  = 'N/A'
    info['directx']      = 'DirectX 12'
    if WMI_AVAILABLE and _wmi:
        try:
            for cs in _wmi.Win32_ComputerSystem():
                info['manufacturer'] = cs.Manufacturer
                info['model']        = cs.Model
        except: pass
        try:
            for b in _wmi.Win32_BIOS():
                info['bios_version'] = b.SMBIOSBIOSVersion or b.Version or 'N/A'
        except: pass
        try:
            for mb in _wmi.Win32_BaseBoard():
                info['motherboard'] = f"{mb.Manufacturer} {mb.Product}"
        except: pass
        try:
            import winreg
            key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\DirectX")
            ver, _ = winreg.QueryValueEx(key, "Version")
            info['directx'] = f"DirectX 12 (v{ver})"
        except: pass
    disks = []
    for part in psutil.disk_partitions():
        try:
            usage = psutil.disk_usage(part.mountpoint)
            disks.append({'device':part.device,'mountpoint':part.mountpoint,
                'total_gb':round(usage.total/(1024**3),1),'used_gb':round(usage.used/(1024**3),1),
                'free_gb':round(usage.free/(1024**3),1),'percent':usage.percent})
        except: pass
    info['disks'] = disks
    info['captured_at'] = datetime.datetime.now().isoformat()
    return info

# ═══════════════════════════════════════════════════════════════════════════════
# WMI CACHE
# ═══════════════════════════════════════════════════════════════════════════════

def _refresh_wmi_cache():
    """Re-query WMI and store results. Called at most every WMI_CACHE_TTL seconds."""
    global _wmi_cache, _wmi_cache_time
    cache = {}
    if WMI_AVAILABLE and _wmi:
        try:
            for t in _wmi.MSAcpi_ThermalZoneTemperature():
                c = round((t.CurrentTemperature / 10.0) - 273.15, 1)
                if 0 < c < 120:
                    cache['cpu_temp'] = c
                    break
        except: pass
        try:
            for v in _wmi.Win32_Processor():
                if hasattr(v, 'CurrentVoltage') and v.CurrentVoltage:
                    cache['cpu_voltage'] = round(v.CurrentVoltage / 10.0, 3)
                    break
        except: pass
    _wmi_cache = cache
    _wmi_cache_time = time.time()

def _get_wmi_cached():
    """Return cached WMI data, refreshing if TTL expired."""
    if time.time() - _wmi_cache_time >= WMI_CACHE_TTL:
        _refresh_wmi_cache()
    return _wmi_cache

# ═══════════════════════════════════════════════════════════════════════════════
# LIVE METRICS
# ═══════════════════════════════════════════════════════════════════════════════

def get_cpu_temp():
    try:
        temps = psutil.sensors_temperatures()
        if temps:
            for name, entries in temps.items():
                for e in entries:
                    if e.current and e.current > 0: return round(e.current, 1)
    except: pass
    return _get_wmi_cached().get('cpu_temp')

def get_cpu_voltage():
    return _get_wmi_cached().get('cpu_voltage')

def get_gpu_stats():
    base = {"usage":None,"temp":None,"name":"N/A","vram_used":None,"vram_total":None}
    if not GPU_AVAILABLE: return base
    try:
        gpus = GPUtil.getGPUs()
        if gpus:
            g = gpus[0]
            base.update({"usage":round(g.load*100,1),"temp":round(g.temperature,1),
                "name":g.name,"vram_used":round(g.memoryUsed),"vram_total":round(g.memoryTotal)})
    except: pass
    return base

def get_network_speed():
    global _net_prev, _net_time_prev
    try:
        current = psutil.net_io_counters()
        now = time.time()
        elapsed = max(now-_net_time_prev, 0.001)
        up   = (current.bytes_sent-_net_prev.bytes_sent)/elapsed/1024
        down = (current.bytes_recv-_net_prev.bytes_recv)/elapsed/1024
        _net_prev = current; _net_time_prev = now
        def fmt(k): return f"{k/1024:.2f} MB/s" if k>1024 else f"{k:.0f} KB/s"
        return {"upload_kbps":round(up,1),"download_kbps":round(down,1),
                "upload_display":fmt(up),"download_display":fmt(down)}
    except:
        return {"upload_kbps":0,"download_kbps":0,"upload_display":"0 KB/s","download_display":"0 KB/s"}

# ═══════════════════════════════════════════════════════════════════════════════
# WARNING ENGINE
# ═══════════════════════════════════════════════════════════════════════════════

def evaluate_warnings(cpu, gpu, ram, net):
    """Evaluate all metrics against thresholds and return list of warnings."""
    warnings = []
    t = _thresholds
    if not t: return warnings

    # ── CPU Temperature
    if cpu['temp'] is not None:
        if cpu['temp'] >= t['cpu']['temp_crit']:
            warnings.append({"id":"cpu_temp_crit","level":"critical","component":"CPU",
                "message":f"CPU temperature critical: {cpu['temp']}°C (limit: {t['cpu']['temp_crit']}°C)",
                "value":cpu['temp'],"threshold":t['cpu']['temp_crit']})
        elif cpu['temp'] >= t['cpu']['temp_warn']:
            warnings.append({"id":"cpu_temp_warn","level":"warning","component":"CPU",
                "message":f"CPU temperature elevated: {cpu['temp']}°C (warn: {t['cpu']['temp_warn']}°C)",
                "value":cpu['temp'],"threshold":t['cpu']['temp_warn']})

    # ── GPU Temperature
    if gpu['temp'] is not None:
        if gpu['temp'] >= t['gpu']['temp_crit']:
            warnings.append({"id":"gpu_temp_crit","level":"critical","component":"GPU",
                "message":f"GPU temperature critical: {gpu['temp']}°C (limit: {t['gpu']['temp_crit']}°C)",
                "value":gpu['temp'],"threshold":t['gpu']['temp_crit']})
        elif gpu['temp'] >= t['gpu']['temp_warn']:
            warnings.append({"id":"gpu_temp_warn","level":"warning","component":"GPU",
                "message":f"GPU temperature elevated: {gpu['temp']}°C (warn: {t['gpu']['temp_warn']}°C)",
                "value":gpu['temp'],"threshold":t['gpu']['temp_warn']})

    # ── CPU Voltage
    if cpu.get('voltage') is not None:
        v = cpu['voltage']
        if v > t['voltage']['cpu_max']:
            warnings.append({"id":"cpu_volt_high","level":"critical","component":"CPU Voltage",
                "message":f"CPU voltage too high: {v}V (max safe: {t['voltage']['cpu_max']}V)",
                "value":v,"threshold":t['voltage']['cpu_max']})
        elif v < t['voltage']['cpu_min']:
            warnings.append({"id":"cpu_volt_low","level":"warning","component":"CPU Voltage",
                "message":f"CPU voltage low: {v}V (min: {t['voltage']['cpu_min']}V)",
                "value":v,"threshold":t['voltage']['cpu_min']})

    # ── RAM Usage
    if ram['usage_percent'] >= t['ram']['usage_crit']:
        warnings.append({"id":"ram_crit","level":"critical","component":"RAM",
            "message":f"RAM usage critical: {ram['usage_percent']}% (limit: {t['ram']['usage_crit']}%)",
            "value":ram['usage_percent'],"threshold":t['ram']['usage_crit']})
    elif ram['usage_percent'] >= t['ram']['usage_warn']:
        warnings.append({"id":"ram_warn","level":"warning","component":"RAM",
            "message":f"RAM usage high: {ram['usage_percent']}% (warn: {t['ram']['usage_warn']}%)",
            "value":ram['usage_percent'],"threshold":t['ram']['usage_warn']})

    # ── CPU Sustained Usage
    _sustained['cpu'].append(cpu['usage'])
    sustain_n = max(1, t['cpu']['usage_sustain_sec'] // 5)
    if len(_sustained['cpu']) > sustain_n:
        _sustained['cpu'].popleft()
    if len(_sustained['cpu']) >= sustain_n:
        avg_cpu = sum(_sustained['cpu'])/len(_sustained['cpu'])
        if avg_cpu >= t['cpu']['usage_crit']:
            warnings.append({"id":"cpu_sustain_crit","level":"critical","component":"CPU",
                "message":f"CPU sustained at {avg_cpu:.0f}% for {t['cpu']['usage_sustain_sec']}s",
                "value":round(avg_cpu,1),"threshold":t['cpu']['usage_crit']})
        elif avg_cpu >= t['cpu']['usage_warn']:
            warnings.append({"id":"cpu_sustain_warn","level":"warning","component":"CPU",
                "message":f"CPU sustained high usage: {avg_cpu:.0f}% for {t['cpu']['usage_sustain_sec']}s",
                "value":round(avg_cpu,1),"threshold":t['cpu']['usage_warn']})

    # ── GPU Sustained Usage
    if gpu['usage'] is not None:
        _sustained['gpu'].append(gpu['usage'])
        if len(_sustained['gpu']) > sustain_n:
            _sustained['gpu'].popleft()
        if len(_sustained['gpu']) >= sustain_n:
            avg_gpu = sum(_sustained['gpu'])/len(_sustained['gpu'])
            if avg_gpu >= t['gpu']['usage_crit']:
                warnings.append({"id":"gpu_sustain_crit","level":"critical","component":"GPU",
                    "message":f"GPU sustained at {avg_gpu:.0f}% for {t['gpu']['usage_sustain_sec']}s",
                    "value":round(avg_gpu,1),"threshold":t['gpu']['usage_crit']})
            elif avg_gpu >= t['gpu']['usage_warn']:
                warnings.append({"id":"gpu_sustain_warn","level":"warning","component":"GPU",
                    "message":f"GPU sustained high usage: {avg_gpu:.0f}% for {t['gpu']['usage_sustain_sec']}s",
                    "value":round(avg_gpu,1),"threshold":t['gpu']['usage_warn']})

    # ── Network Anomaly
    down = net['download_kbps']
    _net_baseline_samples.append(down)
    nb = t['network']['baseline_samples']
    if len(_net_baseline_samples) > nb*3:
        _net_baseline_samples.popleft()
    if len(_net_baseline_samples) >= nb:
        baseline_avg = sum(list(_net_baseline_samples)[-nb:])/nb
        mult = t['network']['spike_multiplier']
        if baseline_avg > 10 and down > baseline_avg * mult:
            warnings.append({"id":"net_spike","level":"warning","component":"Network",
                "message":f"Network spike: {net['download_display']} ({mult}x above baseline)",
                "value":down,"threshold":round(baseline_avg*mult,1)})

    return warnings

# ═══════════════════════════════════════════════════════════════════════════════
# COLLECT + BASELINE
# ═══════════════════════════════════════════════════════════════════════════════

def collect_live_stats():
    cpu_pct  = psutil.cpu_percent(interval=0)
    cpu_freq = psutil.cpu_freq()
    ram      = psutil.virtual_memory()
    gpu      = get_gpu_stats()
    net      = get_network_speed()
    cpu_temp = get_cpu_temp()
    cpu_volt = get_cpu_voltage()
    ts       = time.time()

    history['timestamps'].append(round(ts))
    history['cpu_usage'].append(cpu_pct)
    history['cpu_temp'].append(cpu_temp)
    history['gpu_usage'].append(gpu['usage'])
    history['gpu_temp'].append(gpu['temp'])
    history['ram_usage'].append(ram.percent)
    history['net_down'].append(net['download_kbps'])
    history['net_up'].append(net['upload_kbps'])

    cpu_data = {"usage":round(cpu_pct,1),"temp":cpu_temp,"voltage":cpu_volt,
                "freq_ghz":round(cpu_freq.current/1000,2) if cpu_freq else None,
                "cores":psutil.cpu_count(logical=False),"threads":psutil.cpu_count(logical=True)}
    ram_data = {"usage_percent":round(ram.percent,1),"used_gb":round(ram.used/(1024**3),2),
                "total_gb":round(ram.total/(1024**3),2),"available_gb":round(ram.available/(1024**3),2)}

    # Convert deques to lists for JSON serialisation
    history_snap = {k: list(v) for k, v in history.items()}

    active_warnings = evaluate_warnings(cpu_data, gpu, ram_data, net)

    return {"cpu":cpu_data,"ram":ram_data,"gpu":gpu,"network":net,
            "warnings":active_warnings,"history":history_snap,"timestamp":ts}

def save_original_profile(sysinfo):
    if os.path.exists(ORIG_PROFILE_FILE): return False
    with open(ORIG_PROFILE_FILE,'w') as f:
        json.dump({"type":"ORIGINAL_SYSTEM_PROFILE",
            "warning":"DO NOT DELETE — required for system rollback",
            "saved_at":datetime.datetime.now().isoformat(),"system_info":sysinfo},f,indent=2)
    print(f"  [BACKUP] Original profile saved -> {ORIG_PROFILE_FILE}")
    return True

def save_baseline(sysinfo, live_stats):
    if os.path.exists(BASELINE_FILE): return False
    with open(BASELINE_FILE,'w') as f:
        json.dump({"type":"BASELINE_SNAPSHOT","saved_at":datetime.datetime.now().isoformat(),
            "system_info":sysinfo,"initial_metrics":{
                "cpu_usage":live_stats['cpu']['usage'],"cpu_temp":live_stats['cpu']['temp'],
                "cpu_voltage":live_stats['cpu']['voltage'],"ram_usage":live_stats['ram']['usage_percent'],
                "gpu_usage":live_stats['gpu']['usage'],"gpu_temp":live_stats['gpu']['temp']}},f,indent=2)
    print(f"  [BASELINE] Baseline saved -> {BASELINE_FILE}")
    return True

def create_version_file():
    if os.path.exists(VERSION_FILE): return
    with open(VERSION_FILE, 'w') as f:
        json.dump({"version": "1.2",
                   "build_date": datetime.date.today().isoformat(),
                   "UPDATE_CHECK_URL": ""}, f, indent=2)
    print(f"  [VERSION] version.json created -> {VERSION_FILE}")

def _rotate_log_if_needed(log_file):
    """Rename log file with a timestamp suffix if it has reached LOG_MAX_LINES."""
    if not os.path.exists(log_file): return
    with open(log_file) as f:
        count = sum(1 for _ in f)
    if count >= LOG_MAX_LINES:
        ts   = datetime.datetime.now().strftime('%H%M%S')
        base = log_file.rsplit('.', 1)[0]
        os.rename(log_file, f"{base}_{ts}.jsonl")

def _flush_log_buffer():
    """Write all buffered log entries to disk in one shot."""
    global _log_buffer
    if not _log_buffer: return
    today    = datetime.date.today().isoformat()
    log_file = os.path.join(LOG_DIR, f"session_{today}.jsonl")
    _rotate_log_if_needed(log_file)
    with open(log_file, 'a') as f:
        for entry in _log_buffer:
            f.write(json.dumps(entry) + '\n')
    _log_buffer = []

# ═══════════════════════════════════════════════════════════════════════════════
# BACKGROUND POLLING THREAD
# ═══════════════════════════════════════════════════════════════════════════════

def _background_poll():
    """Daemon thread: polls hardware every 4.5s, caches result, batches log writes."""
    global _cached_stats, _log_buffer
    # Prime cpu_percent delta — first call always returns 0.0
    psutil.cpu_percent(interval=0)
    sample_count = 0
    while True:
        try:
            if _thresholds is not None:
                stats = collect_live_stats()
                with _cache_lock:
                    _cached_stats = stats
                _log_buffer.append({"ts": stats['timestamp'], "cpu": stats['cpu'],
                                    "ram": stats['ram'], "gpu": stats['gpu'],
                                    "warnings": stats['warnings']})
                sample_count += 1
                if sample_count >= LOG_BATCH_SIZE:
                    _flush_log_buffer()
                    sample_count = 0
        except Exception as e:
            print(f"  [POLL ERROR] {e}")
        time.sleep(4.5)

# ═══════════════════════════════════════════════════════════════════════════════
# STARTUP
# ═══════════════════════════════════════════════════════════════════════════════

print("\n  Collecting system information...")
_system_info = get_system_info()
print(f"  [OK] Detected: {_system_info.get('cpu_name','Unknown CPU')}")

print("  Loading smart warning thresholds...")
_thresholds = load_thresholds(PROFILE_DIR, _system_info.get('cpu_name',''), _system_info.get('gpu_name',''))
detected_from = _thresholds.get('_detected_from',{})
print(f"  [OK] Thresholds set for: {detected_from.get('cpu','Unknown')}")

print("  Saving original profile backup...")
save_original_profile(_system_info)

print("  Creating version file...")
create_version_file()

print("  Collecting baseline metrics...")
_initial_stats = collect_live_stats()
save_baseline(_system_info, _initial_stats)

# Seed cache so /api/stats is immediately available before first thread cycle
with _cache_lock:
    _cached_stats = _initial_stats

print("  Starting background polling thread...")
_poll_thread = threading.Thread(target=_background_poll, daemon=True)
_poll_thread.start()
print("  [OK] Background thread running (4.5s interval)\n")

# ═══════════════════════════════════════════════════════════════════════════════
# ROUTES
# ═══════════════════════════════════════════════════════════════════════════════

@app.route('/')
def index(): return send_from_directory('.', 'dashboard.html')

@app.route('/api/system')
def api_system(): return jsonify(_system_info)

@app.route('/api/stats')
def api_stats():
    with _cache_lock:
        stats = _cached_stats
    if stats is None:
        return jsonify({"error": "Warming up, please retry"}), 503
    return jsonify(stats)

@app.route('/api/thresholds', methods=['GET'])
def api_get_thresholds(): return jsonify(_thresholds)

@app.route('/api/thresholds', methods=['POST'])
def api_save_thresholds():
    global _thresholds
    data = request.get_json()
    if not data: return jsonify({"error":"No data"}), 400
    # Merge incoming changes into existing thresholds
    for section, vals in data.items():
        if section in _thresholds and isinstance(vals, dict):
            _thresholds[section].update(vals)
        else:
            _thresholds[section] = vals
    save_thresholds(PROFILE_DIR, _thresholds)
    return jsonify({"status":"saved","thresholds":_thresholds})

@app.route('/api/thresholds/reset', methods=['POST'])
def api_reset_thresholds():
    global _thresholds
    _thresholds = detect_thresholds(_system_info.get('cpu_name',''), _system_info.get('gpu_name',''))
    save_thresholds(PROFILE_DIR, _thresholds)
    return jsonify({"status":"reset","thresholds":_thresholds})

@app.route('/api/baseline')
def api_baseline():
    if os.path.exists(BASELINE_FILE):
        with open(BASELINE_FILE) as f: return jsonify(json.load(f))
    return jsonify({"error":"No baseline found"}), 404

@app.route('/api/original_profile')
def api_original_profile():
    if os.path.exists(ORIG_PROFILE_FILE):
        with open(ORIG_PROFILE_FILE) as f: return jsonify(json.load(f))
    return jsonify({"error":"No original profile found"}), 404

@app.route('/api/version')
def api_version():
    if os.path.exists(VERSION_FILE):
        with open(VERSION_FILE) as f: return jsonify(json.load(f))
    return jsonify({"version": "1.2", "UPDATE_CHECK_URL": ""})

if __name__ == '__main__':
    print("  +======================================+")
    print("  |        KAM SENTINEL  v1.2            |")
    print("  +======================================+")
    print("  Open browser -> http://localhost:5000")
    if not GPU_AVAILABLE: print("  [!] GPU stats: pip install GPUtil")
    if not WMI_AVAILABLE: print("  [!] Full Windows stats: pip install wmi pywin32")
    print("  Press Ctrl+C to stop\n")
    app.run(host='0.0.0.0', port=5000, debug=False, threaded=True)
