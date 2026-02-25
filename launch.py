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

if __name__ == '__main__':
    print("\n  ╔══════════════════════════════════════╗")
    print("  ║        KAM SENTINEL  v1.2            ║")
    print("  ║        Phase 1 — Sentinel Edition    ║")
    print("  ╚══════════════════════════════════════╝")
    print("  Starting server...")

    t = threading.Thread(target=open_browser, daemon=True)
    t.start()

    from server import app
    print("  Open browser -> http://localhost:5000")
    print("  Press Ctrl+C or close this window to stop\n")
    app.run(host='0.0.0.0', port=5000, debug=False, use_reloader=False, threaded=True)
