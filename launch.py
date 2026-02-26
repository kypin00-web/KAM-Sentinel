#!/usr/bin/env python3
"""
KAM Sentinel v1.4.5 - Portable EXE Launcher
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
    print("  ║        KAM SENTINEL  v1.4.5          ║")
    print("  ║        Phase 1 — Sentinel Edition    ║")
    print("  ╚══════════════════════════════════════╝")
    print("  Starting server...")
    print("  Close the browser tab to stop KAM Sentinel\n")

    # Open browser after short delay
    threading.Thread(target=open_browser, args=(port,), daemon=True).start()

    # Watch for shutdown signal
    threading.Thread(target=watch_for_shutdown, args=(port,), daemon=True).start()

    # Handle Ctrl+C gracefully
    def _on_sigint(sig, frame):
        print("\n  Shutting down KAM Sentinel...")
        os._exit(0)
    signal.signal(signal.SIGINT, _on_sigint)

    from server import app
    print(f"  Open browser -> http://localhost:{port}")
    print("  Press Ctrl+C or close browser tab to stop\n")
    app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False, threaded=True)
