#!/usr/bin/env python3
"""
KAM Sentinel v1.2 - Portable EXE Launcher
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

def open_browser():
    time.sleep(2.5)
    webbrowser.open('http://localhost:5000')

def watch_for_shutdown():
    """Poll /api/stats — if server stops responding, exit cleanly."""
    import urllib.request
    time.sleep(10)  # Give server time to start
    consecutive_failures = 0
    while True:
        try:
            urllib.request.urlopen('http://localhost:5000/api/stats', timeout=3)
            consecutive_failures = 0
        except Exception:
            consecutive_failures += 1
            if consecutive_failures >= 3:
                # Server is gone — exit
                os._exit(0)
        time.sleep(3)

if __name__ == '__main__':
    print("\n  ╔══════════════════════════════════════╗")
    print("  ║        KAM SENTINEL  v1.2            ║")
    print("  ║        Phase 1 — Sentinel Edition    ║")
    print("  ╚══════════════════════════════════════╝")
    print("  Starting server...")
    print("  Close the browser tab to stop KAM Sentinel\n")

    # Open browser after short delay
    threading.Thread(target=open_browser, daemon=True).start()

    # Watch for shutdown signal
    threading.Thread(target=watch_for_shutdown, daemon=True).start()

    # Handle Ctrl+C gracefully
    def _on_sigint(sig, frame):
        print("\n  Shutting down KAM Sentinel...")
        os._exit(0)
    signal.signal(signal.SIGINT, _on_sigint)

    from server import app
    print("  Open browser -> http://localhost:5000")
    print("  Press Ctrl+C or close browser tab to stop\n")
    app.run(host='0.0.0.0', port=5000, debug=False, use_reloader=False, threaded=True)
