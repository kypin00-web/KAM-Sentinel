#!/usr/bin/env python3
"""
KAM Sentinel v1.4 — cross-platform (Windows / macOS / Linux)
Hot path: pure in-memory reads (<1 ms per /api/stats request)
All hardware I/O in background threads — server never blocks on sensors
"""
from flask import Flask, jsonify, send_from_directory, request
from collections import deque
import psutil, os, json, time, platform, datetime, threading, sys, subprocess, uuid, re

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

# ── Input validation ──────────────────────────────────────────────────────────
ALLOWED_THRESHOLD_KEYS = {'cpu','gpu','ram','voltage','network'}
ALLOWED_FIXES = {'pip install GPUtil':['GPUtil'], 'pip install wmi pywin32':['wmi','pywin32']}

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

VER               = '1.4.5'
UPDATE_CHECK_URL  = 'https://raw.githubusercontent.com/kypin00-web/KAM-Sentinel/main/version.json'
TELEMETRY_URL     = ''   # POST endpoint for proactive install/error events

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
             hostname=platform.node(), windows_dir=os.environ.get('SystemRoot','N/A'),
             cpu_name=platform.processor(), cpu_cores=psutil.cpu_count(logical=False),
             cpu_threads=psutil.cpu_count(logical=True))
    freq = psutil.cpu_freq()
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
        try:
            r = subprocess.run(['system_profiler','SPHardwareDataType'], capture_output=True, text=True, timeout=8)
            for ln in r.stdout.splitlines():
                ln = ln.strip()
                if 'Model Name:'  in ln: i['model']    = ln.split(':',1)[1].strip()
                if 'Chip:'        in ln: i['cpu_name'] = ln.split(':',1)[1].strip()
        except: pass
        if i['gpu_name'] == 'N/A':
            try:
                r = subprocess.run(['system_profiler','SPDisplaysDataType'], capture_output=True, text=True, timeout=6)
                for ln in r.stdout.splitlines():
                    if 'Chipset Model:' in ln: i['gpu_name'] = ln.split(':',1)[1].strip(); break
            except: pass

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
    cpu_freq = psutil.cpu_freq(); ram = psutil.virtual_memory()
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

@app.route('/api/system')
def api_system():   return jsonify(_sysinfo)

@app.route('/api/stats')
def api_stats():
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


if __name__ == '__main__':
    print(f'\n  KAM SENTINEL v{VER}  [{sys.platform}]')
    print('  http://localhost:5000')
    if not _GPU: print('  [!] pip install GPUtil  for GPU stats')
    if sys.platform == 'win32' and not _WMI: print('  [!] pip install wmi pywin32  for full Windows data')
    print()
    app.run(host='0.0.0.0', port=5000, debug=False, threaded=True)
