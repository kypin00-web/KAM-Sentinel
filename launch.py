#!/usr/bin/env python3
"""
KAM Sentinel v1.5.27 - Portable EXE Launcher
Entry point for the compiled .exe — bundles Flask server + dashboard into one file.
"""

import sys
import os
import threading
import time
import webbrowser
import signal

# When running as a PyInstaller bundle, adjust paths
if getattr(sys, 'frozen', False):
    BASE_DIR = sys._MEIPASS
    WORK_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    WORK_DIR = BASE_DIR

# Change to work directory so backups/logs/profiles sit next to the .exe
os.chdir(WORK_DIR)
sys.path.insert(0, BASE_DIR)

# ── Crash logging (same APPDATA logic as server.py DATA_DIR) ──────────────────
if getattr(sys, 'frozen', False):
    _CRASH_DIR = os.path.join(
        os.environ.get('APPDATA', os.path.expanduser('~')), 'KAM Sentinel', 'logs'
    )
else:
    _CRASH_DIR = os.path.join(WORK_DIR, 'logs')
_CRASH_LOG  = os.path.join(_CRASH_DIR, 'crashes.jsonl')
_CRASH_FLAG = os.path.join(_CRASH_DIR, 'crash.flag')


def _write_crash(exc):
    """Write crash entry to crashes.jsonl and drop crash.flag for Eve to find on next launch."""
    import traceback, json, datetime
    try:
        os.makedirs(_CRASH_DIR, exist_ok=True)
        entry = {
            'ts':        time.time(),
            'date':      datetime.datetime.now().isoformat(),
            'error':     type(exc).__name__ + ': ' + str(exc),
            'traceback': traceback.format_exc(),
            'version':   '1.5.31',
            'os':        sys.platform,
            'username':  (os.environ.get('USERNAME') or os.environ.get('USER') or ''),
        }
        with open(_CRASH_LOG, 'a', encoding='utf-8') as f:
            f.write(json.dumps(entry) + '\n')
        with open(_CRASH_FLAG, 'w', encoding='utf-8') as f:
            json.dump(entry, f, indent=2)
    except Exception:
        pass  # If we can't write the crash log, there's nothing we can do


# ── Global exception hooks — catch ALL unhandled exceptions ───────────────────
def _excepthook(exc_type, exc_value, exc_tb):
    """Catch unhandled exceptions on the main thread."""
    if issubclass(exc_type, (KeyboardInterrupt, SystemExit)):
        sys.__excepthook__(exc_type, exc_value, exc_tb)
        return
    _write_crash(exc_value)
    sys.__excepthook__(exc_type, exc_value, exc_tb)

sys.excepthook = _excepthook

def _thread_excepthook(args):
    """Catch unhandled exceptions in background threads (Python 3.8+)."""
    if args.exc_type in (KeyboardInterrupt, SystemExit):
        return
    if args.exc_value is not None:
        _write_crash(args.exc_value)

if hasattr(threading, 'excepthook'):  # Python 3.8+
    threading.excepthook = _thread_excepthook


# ── Startup file-access check ─────────────────────────────────────────────────
def _check_required_files():
    """Verify all bundled files are readable before Flask starts.

    Antivirus tools (Avast CyberCapture, Defender) can lock files in the
    PyInstaller temp dir mid-run.  This catches that early and shows a
    friendly Eve message instead of a silent crash.
    Only meaningful when running as a frozen exe.
    """
    if not getattr(sys, 'frozen', False):
        return  # Dev mode — files always sit next to source

    required = [
        (os.path.join(BASE_DIR, 'dashboard.html'), 'dashboard.html'),
        (os.path.join(BASE_DIR, 'thresholds.py'),  'thresholds.py'),
    ]
    blocked = []

    for path, label in required:
        try:
            with open(path, 'rb') as f:
                f.read(16)   # minimal read to confirm OS grants access
        except Exception as e:
            blocked.append((label, path, str(e)))

    # Also verify the log/data dir is writable (catches quarantine on DATA_DIR)
    test_path = os.path.join(_CRASH_DIR, '_access_test.tmp')
    try:
        os.makedirs(_CRASH_DIR, exist_ok=True)
        with open(test_path, 'w', encoding='utf-8') as f:
            f.write('ok')
        os.remove(test_path)
    except Exception as e:
        blocked.append(('log directory', _CRASH_DIR, str(e)))

    if not blocked:
        return

    # Something is blocked — log the crash and guide the user
    detail = '; '.join(f'{lbl}: {err}' for lbl, _, err in blocked)

    class _FileAccessError(OSError):
        pass

    exc = _FileAccessError(f'Cannot access required files — {detail}')
    _write_crash(exc)

    print('\n  ┌─────────────────────────────────────────────────────────┐')
    print('  │  EVE: I can\'t access files needed to run KAM Sentinel.  │')
    print('  └─────────────────────────────────────────────────────────┘')
    for lbl, path, err in blocked:
        print(f'    Blocked: {lbl}')
        print(f'      Path : {path}')
        print(f'      Error: {err}')
    print()
    print('  This is almost always an antivirus (Avast, Defender, etc.)')
    print('  scanning or quarantining files while the app is running.')
    print()
    print('  FIX OPTIONS:')
    print('    1. Add KAM Sentinel to your antivirus exclusions, then relaunch.')
    print('    2. Temporarily disable real-time protection, relaunch once,')
    print('       then re-enable it (CyberCapture will whitelist the app).')
    print('    3. Run as Administrator.')
    print()
    print(f'  Crash details saved to: {_CRASH_LOG}')
    time.sleep(10)
    sys.exit(1)


def _lhm_autostart():
    """Auto-manage LHM as a silent dependency on startup.

    Decision tree:
    1. If LHM is already running (any instance) → do nothing, leave it alone.
    2. If LHM is installed at the standard path (or saved pref path) → start it
       silently, mark _lhm_proc so we own it and can close it on exit.
    3. If not installed → do nothing; Eve's prompt will handle it.
    """
    if sys.platform != 'win32':
        return None
    try:
        import psutil as _ps
        # Step 1: already running? (psutil catches non-admin instances too)
        for _p in _ps.process_iter(['name']):
            if _p.info['name'] and 'LibreHardwareMonitor' in _p.info['name']:
                return None   # already up — don't touch it, _lhm_proc stays None
    except Exception:
        pass
    try:
        # WMI namespace fallback (catches admin instances psutil might miss)
        import wmi as _wm
        _wm.WMI(namespace='root/LibreHardwareMonitor')
        return None
    except:
        pass

    # Step 2: not running — find the exe
    import json, subprocess
    lhm_exe = None

    # Check standard install location first
    _std = os.path.join(
        os.environ.get('LOCALAPPDATA', os.path.expanduser('~')),
        'KAM Sentinel', 'LHM', 'LibreHardwareMonitor.exe'
    )
    if os.path.exists(_std):
        lhm_exe = _std

    # Fall back to saved pref path
    if not lhm_exe:
        try:
            pref_path = os.path.join(
                os.environ.get('APPDATA', os.path.expanduser('~')),
                'KAM Sentinel', 'profiles', 'preferences.json'
            )
            if os.path.exists(pref_path):
                with open(pref_path, encoding='utf-8') as f:
                    prefs = json.load(f)
                _saved = prefs.get('lhm_path', '')
                if _saved and os.path.exists(_saved):
                    lhm_exe = _saved
        except Exception:
            pass

    if not lhm_exe:
        return None   # Step 3: not installed — Eve prompt handles it

    # Start LHM minimized to tray, silently
    proc = subprocess.Popen(
        [lhm_exe, '/minimized'],
        creationflags=subprocess.CREATE_NO_WINDOW,
        close_fds=True,
    )
    try:
        import server as _srv
        _srv._lhm_proc = proc   # mark as owned — shutdown will close it
    except Exception:
        pass
    return proc

def _kill_existing_server(port):
    """If port is already bound, kill that process so we don't open a second instance."""
    try:
        import psutil
        for conn in psutil.net_connections(kind='inet'):
            if conn.laddr.port == port and conn.status == psutil.CONN_LISTEN:
                try:
                    proc = psutil.Process(conn.pid)
                    proc.terminate()
                    proc.wait(timeout=3)
                except psutil.TimeoutExpired:
                    try:
                        proc.kill()
                    except Exception:
                        pass
                except Exception:
                    pass
    except Exception:
        pass


def open_browser(port=5000):
    time.sleep(2.5)
    webbrowser.open(f'http://localhost:{port}')

def watch_for_shutdown(port=5000):
    """Poll /api/stats — if server stops responding, exit cleanly."""
    import urllib.request
    time.sleep(20)  # Give server plenty of time to start
    consecutive_failures = 0
    while True:
        try:
            urllib.request.urlopen(f'http://localhost:{port}/api/stats', timeout=5)
            consecutive_failures = 0
        except Exception:
            consecutive_failures += 1
            if consecutive_failures >= 5:  # 5 failures = 15s of no response
                try:
                    from server import _lhm_proc as _lp
                    if _lp is not None: _lp.terminate()
                except: pass
                os._exit(0)
        time.sleep(3)

if __name__ == '__main__':
    port = 5000
    if len(sys.argv) > 1:
        try:
            port = int(sys.argv[1])
            if port < 1 or port > 65535:
                raise ValueError('port out of range')
        except (ValueError, TypeError):
            print('  Usage: python launch.py [PORT]  (default: 5000)')
            sys.exit(1)

    print("\n  ╔══════════════════════════════════════╗")
    print("  ║        KAM SENTINEL  v1.5.31         ║")
    print("  ║        Phase 1 — Sentinel Edition    ║")
    print("  ╚══════════════════════════════════════╝")
    print("  Starting server...")
    print("  Close the browser tab to stop KAM Sentinel\n")

    # Verify all required bundled files are accessible before importing Flask
    _check_required_files()

    # Silently relaunch LHM if Eve installed it previously
    _lhm_autostart()

    # Open browser after short delay
    threading.Thread(target=open_browser, args=(port,), daemon=True).start()

    # Watch for shutdown signal
    threading.Thread(target=watch_for_shutdown, args=(port,), daemon=True).start()

    # Handle Ctrl+C gracefully
    def _on_sigint(sig, frame):
        print("\n  Shutting down KAM Sentinel...")
        os._exit(0)
    signal.signal(signal.SIGINT, _on_sigint)

    try:
        _kill_existing_server(port)
        from server import app
        print(f"  Open browser -> http://localhost:{port}")
        print("  Press Ctrl+C or close browser tab to stop\n")
        app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False, threaded=True)
    except Exception as _launch_exc:
        _write_crash(_launch_exc)
        print(f"\n  [ERROR] KAM Sentinel crashed: {_launch_exc}")
        print("  Eve will show a diagnosis when you restart.")
        time.sleep(5)
        sys.exit(1)
