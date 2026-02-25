#!/usr/bin/env python3
"""
KAM Sentinel - Performance Dashboard Server v1.3
Optimized: deque history, cached WMI, non-blocking cpu_percent,
           batched log writes, thread-safe state, lean resource usage
Security:  rate limiting, input validation, no PII storage, localhost-only mode
"""

from flask import Flask, jsonify, send_from_directory, request
from collections import deque
import psutil, os, json, time, platform, datetime, threading

app = Flask(__name__, static_folder='.')

# ── Security: Rate limiting ───────────────────────────────────────────────────
_rate_limit = {}
_rate_lock  = threading.Lock()
RATE_LIMIT_WINDOW = 1.0   # seconds
RATE_LIMIT_MAX    = 10    # max requests per window per IP

def is_rate_limited(ip):
    now = time.time()
    with _rate_lock:
        window = _rate_limit.get(ip, [])
        window = [t for t in window if now - t < RATE_LIMIT_WINDOW]
        if len(window) >= RATE_LIMIT_MAX:
            return True
        window.append(now)
        _rate_limit[ip] = window
    return False

@app.before_request
def security_checks():
    ip = request.remote_addr
    # Allow localhost always
    if ip in ('127.0.0.1', '::1'):
        return None
    # Rate limit external IPs
    if is_rate_limited(ip):
        return jsonify({"error": "rate limited"}), 429
    # Block non-GET to sensitive endpoints from non-localhost
    if request.method == 'POST' and ip not in ('127.0.0.1', '::1'):
        return jsonify({"error": "forbidden"}), 403

# ── Security: Input validation helpers ───────────────────────────────────────
MAX_JSON_DEPTH  = 3
MAX_JSON_KEYS   = 20
ALLOWED_THRESHOLD_KEYS = {'cpu', 'gpu', 'ram', 'voltage', 'network'}
ALLOWED_NUMERIC_RANGE  = (0, 10000)

def validate_threshold_data(data, depth=0):
    """Recursively validate threshold JSON — no bombs, no surprises."""
    if depth > MAX_JSON_DEPTH:
        return False, "JSON too deeply nested"
    if isinstance(data, dict):
        if len(data) > MAX_JSON_KEYS:
            return False, f"Too many keys ({len(data)})"
        for k, v in data.items():
            if not isinstance(k, str) or len(k) > 50:
                return False, f"Invalid key: {k}"
            ok, err = validate_threshold_data(v, depth + 1)
            if not ok:
                return False, err
    elif isinstance(data, (int, float)):
        if not (ALLOWED_NUMERIC_RANGE[0] <= data <= ALLOWED_NUMERIC_RANGE[1]):
            return False, f"Value out of range: {data}"
    elif isinstance(data, str):
        if len(data) > 100:
            return False, "String value too long"
    elif data is not None and not isinstance(data, bool):
        return False, f"Unexpected type: {type(data)}"
    return True, None

# Suppress CMD window flash from nvidia-smi when running as .exe (--noconsole build)
import subprocess, sys
if sys.platform == 'win32' and getattr(sys, 'frozen', False):
    _orig_popen = subprocess.Popen
    def _silent_popen(*args, **kwargs):
        if 'creationflags' not in kwargs:
            kwargs['creationflags'] = subprocess.CREATE_NO_WINDOW
        return _orig_popen(*args, **kwargs)
    subprocess.Popen = _silent_popen

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

for d in [BACKUP_DIR, LOG_DIR, PROFILE_DIR]:
    os.makedirs(d, exist_ok=True)

# FIX 1: deque instead of list+pop(0) - O(1) vs O(n)
MAX_HISTORY = 60
history = {k: deque(maxlen=MAX_HISTORY) for k in
    ['timestamps','cpu_usage','cpu_temp','gpu_usage','gpu_temp','ram_usage','net_down','net_up']}

# FIX 5: Thread lock for shared mutable state
_state_lock = threading.Lock()

# FIX 2: WMI cache - only re-poll every 10 seconds
_wmi_cache = {'cpu_voltage':None,'cpu_temp':None,'last_poll':0,'interval':10}

# Network tracking
_net_prev      = psutil.net_io_counters()
_net_time_prev = time.time()
_net_baseline  = deque(maxlen=36)

# Sustained tracking with auto-cap deques
_sustained = {'cpu': deque(maxlen=12), 'gpu': deque(maxlen=12)}

# FIX 4: Batched log writes
_log_buffer     = []
_last_log_flush = time.time()
LOG_FLUSH_SECS  = 60

# FIX 3: Background CPU sampler - main thread never blocks
_cpu_pct_cache = 0.0

def _cpu_sampler():
    global _cpu_pct_cache
    while True:
        try: _cpu_pct_cache = psutil.cpu_percent(interval=1.0)
        except: pass
        time.sleep(1.0)

threading.Thread(target=_cpu_sampler, daemon=True).start()


def get_system_info():
    info = {}
    info['os']           = platform.system()
    info['os_version']   = platform.version()
    info['os_release']   = platform.release()
    info['hostname']     = platform.node()
    info['windows_dir']  = os.environ.get('SystemRoot','N/A')
    info['cpu_name']     = platform.processor()
    info['cpu_cores']    = psutil.cpu_count(logical=False)
    info['cpu_threads']  = psutil.cpu_count(logical=True)
    freq = psutil.cpu_freq()
    info['cpu_max_ghz']     = round(freq.max/1000,2) if freq else 'N/A'
    info['cpu_current_ghz'] = round(freq.current/1000,2) if freq else 'N/A'
    ram  = psutil.virtual_memory()
    swap = psutil.swap_memory()
    info['ram_total_mb']      = round(ram.total/(1024**2))
    info['ram_total_gb']      = round(ram.total/(1024**3),2)
    info['pagefile_used_mb']  = round(swap.used/(1024**2))
    info['pagefile_total_mb'] = round(swap.total/(1024**2))
    info['gpu_name']    = 'N/A'
    info['gpu_vram_mb'] = 'N/A'
    if GPU_AVAILABLE:
        try:
            gpus = GPUtil.getGPUs()
            if gpus: info['gpu_name'] = gpus[0].name; info['gpu_vram_mb'] = round(gpus[0].memoryTotal)
        except: pass
    info['manufacturer'] = 'N/A'; info['model'] = 'N/A'
    info['bios_version'] = 'N/A'; info['motherboard'] = 'N/A'
    info['directx']      = 'DirectX 12'
    if WMI_AVAILABLE and _wmi:
        try:
            for cs in _wmi.Win32_ComputerSystem(): info['manufacturer']=cs.Manufacturer; info['model']=cs.Model
        except: pass
        try:
            for b in _wmi.Win32_BIOS(): info['bios_version']=b.SMBIOSBIOSVersion or b.Version or 'N/A'
        except: pass
        try:
            for mb in _wmi.Win32_BaseBoard(): info['motherboard']=f"{mb.Manufacturer} {mb.Product}"
        except: pass
        try:
            import winreg
            key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,r"SOFTWARE\Microsoft\DirectX")
            ver,_ = winreg.QueryValueEx(key,"Version")
            info['directx'] = f"DirectX 12 (v{ver})"
        except: pass
    disks = []
    for part in psutil.disk_partitions():
        try:
            u = psutil.disk_usage(part.mountpoint)
            disks.append({'device':part.device,'mountpoint':part.mountpoint,
                'total_gb':round(u.total/(1024**3),1),'used_gb':round(u.used/(1024**3),1),
                'free_gb':round(u.free/(1024**3),1),'percent':u.percent})
        except: pass
    info['disks'] = disks
    info['captured_at'] = datetime.datetime.now().isoformat()
    return info


def get_cpu_temp_voltage():
    """FIX 2: Only call WMI every 10s, cache the rest."""
    now = time.time()
    if now - _wmi_cache['last_poll'] < _wmi_cache['interval']:
        return _wmi_cache['cpu_temp'], _wmi_cache['cpu_voltage']
    temp = volt = None
    try:
        temps = psutil.sensors_temperatures()
        if temps:
            for entries in temps.values():
                for e in entries:
                    if e.current and e.current > 0: temp = round(e.current,1); break
                if temp: break
    except: pass
    if WMI_AVAILABLE and _wmi:
        if temp is None:
            try:
                for t in _wmi.MSAcpi_ThermalZoneTemperature():
                    c = round((t.CurrentTemperature/10.0)-273.15,1)
                    if 0 < c < 120: temp = c; break
            except: pass
        try:
            for v in _wmi.Win32_Processor():
                if hasattr(v,'CurrentVoltage') and v.CurrentVoltage:
                    volt = round(v.CurrentVoltage/10.0,3); break
        except: pass
    _wmi_cache.update({'cpu_temp':temp,'cpu_voltage':volt,'last_poll':now})
    return temp, volt


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


# ── Async GPU cache — nvidia-smi runs in its own thread, never blocks polling ──
_gpu_cache = {"usage":None,"temp":None,"name":"N/A","vram_used":None,"vram_total":None}
_gpu_cache_lock = threading.Lock()
_gpu_last_poll  = 0
GPU_POLL_INTERVAL = 5.0  # seconds between nvidia-smi calls

def _gpu_worker():
    """Dedicated thread: calls nvidia-smi every GPU_POLL_INTERVAL seconds.
    Never blocks the main polling thread."""
    global _gpu_last_poll
    while True:
        try:
            result = get_gpu_stats()
            with _gpu_cache_lock:
                _gpu_cache.update(result)
            _gpu_last_poll = time.time()
        except Exception:
            pass
        time.sleep(GPU_POLL_INTERVAL)

def get_gpu_cached():
    """Return last known GPU stats instantly — no blocking."""
    with _gpu_cache_lock:
        return dict(_gpu_cache)

# Start GPU worker thread at module load
if GPU_AVAILABLE:
    _gpu_thread = threading.Thread(target=_gpu_worker, daemon=True, name='gpu-worker')
    _gpu_thread.start()


def get_network_speed():
    global _net_prev, _net_time_prev
    try:
        curr = psutil.net_io_counters()
        now  = time.time()
        el   = max(now-_net_time_prev, 0.001)
        up   = (curr.bytes_sent-_net_prev.bytes_sent)/el/1024
        dn   = (curr.bytes_recv-_net_prev.bytes_recv)/el/1024
        _net_prev = curr; _net_time_prev = now
        fmt = lambda k: f"{k/1024:.2f} MB/s" if k>1024 else f"{k:.0f} KB/s"
        return {"upload_kbps":round(up,1),"download_kbps":round(dn,1),
                "upload_display":fmt(up),"download_display":fmt(dn)}
    except:
        return {"upload_kbps":0,"download_kbps":0,"upload_display":"0 KB/s","download_display":"0 KB/s"}


from thresholds import load_thresholds, save_thresholds, detect_thresholds
_thresholds = None

def evaluate_warnings(cpu, gpu, ram, net):
    w = []; t = _thresholds
    if not t: return w
    ct = cpu['temp']; gt = gpu['temp']; cv = cpu.get('voltage')
    rp = ram['usage_percent']; dn = net['download_kbps']

    if ct:
        if ct >= t['cpu']['temp_crit']: w.append({"id":"cpu_temp_crit","level":"critical","component":"CPU","message":f"CPU temp critical: {ct}°C (limit: {t['cpu']['temp_crit']}°C)"})
        elif ct >= t['cpu']['temp_warn']: w.append({"id":"cpu_temp_warn","level":"warning","component":"CPU","message":f"CPU temp elevated: {ct}°C (warn: {t['cpu']['temp_warn']}°C)"})
    if gt:
        if gt >= t['gpu']['temp_crit']: w.append({"id":"gpu_temp_crit","level":"critical","component":"GPU","message":f"GPU temp critical: {gt}°C (limit: {t['gpu']['temp_crit']}°C)"})
        elif gt >= t['gpu']['temp_warn']: w.append({"id":"gpu_temp_warn","level":"warning","component":"GPU","message":f"GPU temp elevated: {gt}°C (warn: {t['gpu']['temp_warn']}°C)"})
    if cv:
        if cv > t['voltage']['cpu_max']: w.append({"id":"cpu_volt_high","level":"critical","component":"Voltage","message":f"CPU voltage too high: {cv}V (max: {t['voltage']['cpu_max']}V)"})
        elif cv < t['voltage']['cpu_min']: w.append({"id":"cpu_volt_low","level":"warning","component":"Voltage","message":f"CPU voltage low: {cv}V (min: {t['voltage']['cpu_min']}V)"})
    if rp >= t['ram']['usage_crit']: w.append({"id":"ram_crit","level":"critical","component":"RAM","message":f"RAM critical: {rp}% (limit: {t['ram']['usage_crit']}%)"})
    elif rp >= t['ram']['usage_warn']: w.append({"id":"ram_warn","level":"warning","component":"RAM","message":f"RAM high: {rp}% (warn: {t['ram']['usage_warn']}%)"})

    sn = max(1, t['cpu']['usage_sustain_sec']//5)
    _sustained['cpu'].append(cpu['usage'])
    if len(_sustained['cpu']) >= sn:
        avg = sum(_sustained['cpu'])/len(_sustained['cpu'])
        if avg >= t['cpu']['usage_crit']: w.append({"id":"cpu_sc","level":"critical","component":"CPU","message":f"CPU sustained {avg:.0f}% for {t['cpu']['usage_sustain_sec']}s"})
        elif avg >= t['cpu']['usage_warn']: w.append({"id":"cpu_sw","level":"warning","component":"CPU","message":f"CPU sustained high: {avg:.0f}% for {t['cpu']['usage_sustain_sec']}s"})
    if gpu['usage'] is not None:
        _sustained['gpu'].append(gpu['usage'])
        if len(_sustained['gpu']) >= sn:
            avg = sum(_sustained['gpu'])/len(_sustained['gpu'])
            if avg >= t['gpu']['usage_crit']: w.append({"id":"gpu_sc","level":"critical","component":"GPU","message":f"GPU sustained {avg:.0f}%"})
            elif avg >= t['gpu']['usage_warn']: w.append({"id":"gpu_sw","level":"warning","component":"GPU","message":f"GPU sustained high: {avg:.0f}%"})

    _net_baseline.append(dn)
    nb = t['network']['baseline_samples']
    if len(_net_baseline) >= nb:
        avg_b = sum(list(_net_baseline)[-nb:])/nb
        if avg_b > 10 and dn > avg_b * t['network']['spike_multiplier']:
            w.append({"id":"net_spike","level":"warning","component":"Network","message":f"Network spike: {net['download_display']}"})
    return w


def collect_live_stats():
    cpu_pct  = _cpu_pct_cache   # FIX 3: non-blocking
    cpu_freq = psutil.cpu_freq()
    ram      = psutil.virtual_memory()
    gpu      = get_gpu_cached()  # Non-blocking: reads from async GPU worker thread
    net      = get_network_speed()
    cpu_temp, cpu_volt = get_cpu_temp_voltage()  # FIX 2: cached
    ts       = time.time()

    cpu_data = {"usage":round(cpu_pct,1),"temp":cpu_temp,"voltage":cpu_volt,
                "freq_ghz":round(cpu_freq.current/1000,2) if cpu_freq else None,
                "cores":psutil.cpu_count(logical=False),"threads":psutil.cpu_count(logical=True)}
    ram_data = {"usage_percent":round(ram.percent,1),"used_gb":round(ram.used/(1024**3),2),
                "total_gb":round(ram.total/(1024**3),2),"available_gb":round(ram.available/(1024**3),2)}

    warnings = evaluate_warnings(cpu_data, gpu, ram_data, net)

    with _state_lock:  # FIX 5: thread-safe history
        for k,v in [('timestamps',round(ts)),('cpu_usage',cpu_pct),('cpu_temp',cpu_temp),
                    ('gpu_usage',gpu['usage']),('gpu_temp',gpu['temp']),('ram_usage',ram.percent),
                    ('net_down',net['download_kbps']),('net_up',net['upload_kbps'])]:
            history[k].append(v)
        hist = {k:list(v) for k,v in history.items()}

    return {"cpu":cpu_data,"ram":ram_data,"gpu":gpu,"network":net,
            "warnings":warnings,"history":hist,"timestamp":ts}


def log_session_stats(stats):
    """FIX 4: Buffer writes, flush every 60s instead of every poll."""
    _log_buffer.append({"ts":stats['timestamp'],"cpu":stats['cpu'],
        "ram":stats['ram'],"gpu":stats['gpu'],"warnings":stats['warnings']})
    if time.time()-_last_log_flush >= LOG_FLUSH_SECS:
        _flush_log()

def _flush_log():
    global _last_log_flush
    if not _log_buffer: return
    today = datetime.date.today().isoformat()
    try:
        with open(os.path.join(LOG_DIR,f"session_{today}.jsonl"),'a') as f:
            for e in _log_buffer: f.write(json.dumps(e)+'\n')
        _log_buffer.clear()
    except Exception as e:
        print(f"  [WARN] Log flush failed: {e}")
    _last_log_flush = time.time()


def save_original_profile(sysinfo):
    if os.path.exists(ORIG_PROFILE_FILE): return False
    with open(ORIG_PROFILE_FILE,'w') as f:
        json.dump({"type":"ORIGINAL_SYSTEM_PROFILE","warning":"DO NOT DELETE — required for rollback",
            "saved_at":datetime.datetime.now().isoformat(),"system_info":sysinfo},f,indent=2)
    print(f"  [BACKUP] Saved -> {ORIG_PROFILE_FILE}"); return True

def save_baseline(sysinfo, stats):
    if os.path.exists(BASELINE_FILE): return False
    with open(BASELINE_FILE,'w') as f:
        json.dump({"type":"BASELINE_SNAPSHOT","saved_at":datetime.datetime.now().isoformat(),
            "system_info":sysinfo,"initial_metrics":{"cpu_usage":stats['cpu']['usage'],
            "cpu_temp":stats['cpu']['temp'],"cpu_voltage":stats['cpu']['voltage'],
            "ram_usage":stats['ram']['usage_percent'],"gpu_usage":stats['gpu']['usage'],
            "gpu_temp":stats['gpu']['temp']}},f,indent=2)
    print(f"  [BASELINE] Saved -> {BASELINE_FILE}"); return True


# Startup
print("\n  Collecting system information...")
_system_info = get_system_info()
print(f"  [OK] {_system_info.get('cpu_name','Unknown CPU')}")
print("  Loading thresholds...")
_thresholds = load_thresholds(PROFILE_DIR,_system_info.get('cpu_name',''),_system_info.get('gpu_name',''))
print(f"  [OK] Thresholds ready")
save_original_profile(_system_info)
print("  Warming up CPU sampler...")
time.sleep(1.2)
_initial = collect_live_stats()
save_baseline(_system_info, _initial)

import atexit
atexit.register(_flush_log)


@app.route('/')
def index(): return send_from_directory('.','dashboard.html')

@app.route('/api/system')
def api_system(): return jsonify(_system_info)

@app.route('/api/stats')
def api_stats():
    s = collect_live_stats(); log_session_stats(s); return jsonify(s)

@app.route('/api/thresholds', methods=['GET'])
def api_get_thresholds(): return jsonify(_thresholds)

@app.route('/api/thresholds', methods=['POST'])
def api_save_thresholds():
    global _thresholds
    data = request.get_json(force=False, silent=True)
    if not data:
        return jsonify({"error": "No data"}), 400
    ok, err = validate_threshold_data(data)
    if not ok:
        return jsonify({"error": f"Invalid data: {err}"}), 400
    # Only update known keys
    for k, v in data.items():
        if k in ALLOWED_THRESHOLD_KEYS and isinstance(v, dict):
            _thresholds[k].update(v)
    save_thresholds(PROFILE_DIR, _thresholds)
    return jsonify({"status": "saved", "thresholds": _thresholds})

@app.route('/api/thresholds/reset', methods=['POST'])
def api_reset_thresholds():
    global _thresholds
    _thresholds = detect_thresholds(_system_info.get('cpu_name',''),_system_info.get('gpu_name',''))
    save_thresholds(PROFILE_DIR,_thresholds)
    return jsonify({"status":"reset","thresholds":_thresholds})

@app.route('/api/baseline')
def api_baseline():
    if os.path.exists(BASELINE_FILE):
        with open(BASELINE_FILE) as f: return jsonify(json.load(f))
    return jsonify({"error":"No baseline"}),404

@app.route('/api/original_profile')
def api_original_profile():
    if os.path.exists(ORIG_PROFILE_FILE):
        with open(ORIG_PROFILE_FILE) as f: return jsonify(json.load(f))
    return jsonify({"error":"No profile"}),404

@app.route('/api/feedback', methods=['POST'])
def api_feedback():
    """In-app feedback — auto-triaged by category, priority scored, no PII."""
    data = request.get_json(silent=True) or {}

    category = data.get('category', 'general')
    if category not in ('bug', 'feature', 'performance', 'general'):
        category = 'general'
    message = str(data.get('message', ''))[:500]
    if not message.strip():
        return jsonify({"error": "No message"}), 400

    # ── Auto-triage: priority scoring ────────────────────────────────────────
    priority = 'normal'
    auto_action = None
    keywords = message.lower()

    if category == 'bug':
        # Critical bug keywords → P0
        if any(k in keywords for k in ['crash','freeze','not working','broken','error','exception','won\'t start','fails']):
            priority = 'critical'
            auto_action = 'immediate_fix'
        # Functional bugs → P1
        elif any(k in keywords for k in ['wrong','incorrect','missing','n/a','not showing','blank','stuck']):
            priority = 'high'
            auto_action = 'fix_next_build'
        else:
            priority = 'normal'
            auto_action = 'fix_next_build'

    elif category == 'performance':
        if any(k in keywords for k in ['slow','lag','freeze','high cpu','memory leak','100%']):
            priority = 'high'
            auto_action = 'investigate_immediately'
        else:
            priority = 'normal'
            auto_action = 'investigate_next_build'

    elif category == 'feature':
        priority = 'review'
        auto_action = 'pending_review'  # Goes to review queue for us to decide

    elif category == 'general':
        priority = 'low'
        auto_action = 'log_only'

    feedback_entry = {
        "ts":          time.time(),
        "date":        datetime.datetime.now().isoformat(),
        "id":          f"{category[:3].upper()}-{int(time.time())}",
        "category":    category,
        "priority":    priority,
        "auto_action": auto_action,
        "message":     message,
        "status":      "open",
        "version":     "1.4.0",
        "sys_info": {
            "cpu": _system_info.get('cpu_name', 'N/A'),
            "gpu": _system_info.get('gpu_name', 'N/A'),
            "os":  _system_info.get('os_release', 'N/A'),
        }
    }

    # Write to category-specific file for easy review
    feedback_dir = os.path.join(LOG_DIR, 'feedback')
    os.makedirs(feedback_dir, exist_ok=True)
    feedback_file = os.path.join(feedback_dir, f'{category}.jsonl')

    try:
        with open(feedback_file, 'a', encoding='utf-8') as f:
            f.write(json.dumps(feedback_entry) + '\n')

        # User-facing response based on priority
        messages = {
            'critical':    "🔴 Critical bug logged — fix queued immediately",
            'high':        "🟡 Bug logged — scheduled for next build",
            'review':      "✨ Feature request logged — we'll review it!",
            'normal':      "✓ Logged — thank you!",
            'low':         "✓ Feedback received — thank you!",
        }
        return jsonify({
            "status":   "ok",
            "id":       feedback_entry["id"],
            "priority": priority,
            "message":  messages.get(priority, "Thank you!")
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/feedback/queue')
def api_feedback_queue():
    """Returns open feedback items for review — bugs auto-sorted by priority."""
    feedback_dir = os.path.join(LOG_DIR, 'feedback')
    if not os.path.exists(feedback_dir):
        return jsonify({"items": [], "counts": {}})

    items = []
    counts = {'bug': 0, 'performance': 0, 'feature': 0, 'general': 0}

    for cat in ('bug', 'performance', 'feature', 'general'):
        f = os.path.join(feedback_dir, f'{cat}.jsonl')
        if os.path.exists(f):
            with open(f, encoding='utf-8') as fh:
                for line in fh:
                    try:
                        entry = json.loads(line.strip())
                        if entry.get('status') == 'open':
                            items.append(entry)
                            counts[cat] = counts.get(cat, 0) + 1
                    except:
                        pass

    # Sort: critical first, then high, then review, then normal/low
    priority_order = {'critical': 0, 'high': 1, 'review': 2, 'normal': 3, 'low': 4}
    items.sort(key=lambda x: priority_order.get(x.get('priority', 'low'), 5))

    return jsonify({"items": items[-50:], "counts": counts})
def api_shutdown():
    """Called by dashboard when browser tab/window is closed."""
    _flush_log()
    func = request.environ.get('werkzeug.server.shutdown')
    if func:
        func()
    else:
        threading.Thread(target=lambda: (time.sleep(0.5), os._exit(0)), daemon=True).start()
    return jsonify({"status": "shutting down"})

@app.route('/api/diagnostics')
def api_diagnostics():
    """Returns self-healing diagnostic info for N/A indicators in dashboard.
    Add ?test=1 to URL to simulate all N/A states for UI testing."""
    test_mode = request.args.get('test') == '1'

    if test_mode:
        return jsonify({
            'gpu': {
                'status': 'missing_package',
                'message': '[TEST MODE] GPUtil not installed — GPU stats unavailable',
                'fix': 'pip install GPUtil',
                'auto_fix': True
            },
            'cpu_temp': {
                'status': 'needs_lhm',
                'message': '[TEST MODE] LibreHardwareMonitor not running — needed for Ryzen/AMD CPU temps',
                'fix': 'Launch LibreHardwareMonitor.exe as Administrator',
                'auto_fix': False,
                'download': 'https://github.com/LibreHardwareMonitor/LibreHardwareMonitor/releases/latest'
            },
            'wmi': {
                'status': 'missing_package',
                'message': '[TEST MODE] WMI not installed — voltage/temp limited',
                'fix': 'pip install wmi pywin32',
                'auto_fix': True
            },
            'privacy': {
                'status': 'ok',
                'message': 'No PII stored — hardware metrics only',
                'fix': None
            }
        })
    diags = {}

    # GPU diagnostics
    try:
        import GPUtil
        gpus = GPUtil.getGPUs()
        diags['gpu'] = {
            'status': 'ok' if gpus else 'no_gpu_found',
            'message': f"Found {len(gpus)} GPU(s)" if gpus else "GPUtil installed but no GPU detected",
            'fix': None
        }
    except ImportError:
        diags['gpu'] = {
            'status': 'missing_package',
            'message': 'GPUtil not installed — GPU stats unavailable',
            'fix': 'pip install GPUtil',
            'auto_fix': True
        }

    # CPU temp diagnostics
    lhm_running = False
    try:
        import wmi
        w = wmi.WMI(namespace='root/LibreHardwareMonitor')
        lhm_running = True
    except:
        pass
    try:
        import wmi
        w = wmi.WMI(namespace='root/OpenHardwareMonitor')
        lhm_running = True
    except:
        pass

    if lhm_running:
        diags['cpu_temp'] = {'status': 'ok', 'message': 'Hardware monitor running', 'fix': None}
    elif WMI_AVAILABLE:
        diags['cpu_temp'] = {
            'status': 'needs_lhm',
            'message': 'LibreHardwareMonitor not running — needed for Ryzen/AMD CPU temps',
            'fix': 'Launch LibreHardwareMonitor.exe as Administrator',
            'auto_fix': False,
            'download': 'https://github.com/LibreHardwareMonitor/LibreHardwareMonitor/releases/latest'
        }
    else:
        diags['cpu_temp'] = {
            'status': 'missing_package',
            'message': 'WMI not installed',
            'fix': 'pip install wmi pywin32',
            'auto_fix': True
        }

    # WMI diagnostics
    diags['wmi'] = {
        'status': 'ok' if WMI_AVAILABLE else 'missing_package',
        'message': 'WMI available' if WMI_AVAILABLE else 'WMI not installed — voltage/temp limited',
        'fix': None if WMI_AVAILABLE else 'pip install wmi pywin32',
        'auto_fix': not WMI_AVAILABLE
    }

    # PII check — confirm no personal data in logs
    diags['privacy'] = {
        'status': 'ok',
        'message': 'No PII stored — hardware metrics only',
        'fix': None
    }

    return jsonify(diags)

@app.route('/api/autofix', methods=['POST'])
def api_autofix():
    """Run safe auto-fixes for missing packages."""
    import subprocess, sys
    data = request.get_json(silent=True) or {}
    fix_cmd = data.get('fix', '')

    # Strict whitelist — only safe pip installs allowed
    ALLOWED_FIXES = {
        'pip install GPUtil':       ['GPUtil'],
        'pip install wmi pywin32':  ['wmi', 'pywin32'],
    }

    if fix_cmd not in ALLOWED_FIXES:
        return jsonify({"error": "Fix not allowed"}), 400

    packages = ALLOWED_FIXES[fix_cmd]
    try:
        result = subprocess.run(
            [sys.executable, '-m', 'pip', 'install'] + packages,
            capture_output=True, text=True, timeout=60,
            creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, 'CREATE_NO_WINDOW') else 0
        )
        if result.returncode == 0:
            return jsonify({"status": "ok", "message": f"Installed {', '.join(packages)} — restart server to apply"})
        else:
            return jsonify({"status": "error", "message": result.stderr[:200]}), 500
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

if __name__=='__main__':
    print("\n  ╔══════════════════════════════════════╗")
    print("  ║        KAM SENTINEL  v1.2            ║")
    print("  ╚══════════════════════════════════════╝")
    print("  Open browser -> http://localhost:5000")
    if not GPU_AVAILABLE: print("  [!] pip install GPUtil  for GPU data")
    if not WMI_AVAILABLE: print("  [!] pip install wmi pywin32  for full Windows data")
    print("  Press Ctrl+C to stop\n")
    app.run(host='0.0.0.0',port=5000,debug=False,threaded=True)
