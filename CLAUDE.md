# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Project Is
KAM Sentinel is a **portable cross-platform PC performance monitoring dashboard** built for gaming and power users. It runs as a single `.exe`/binary (no Python required on target machines), opens a local browser dashboard, and monitors CPU, GPU, RAM, temperature, voltage, and network in real time with smart hardware-aware warnings.

**Current build: Phase 1 — Sentinel Edition (v1.5.3)** — supports Windows, macOS, Linux

---

## Development Commands

```bat
# First-time setup: install deps and start server
setup.bat

# Run dev server directly (after setup). Optional port, default 5000
python server.py
python server.py 8080

# Run the full test suite
python test_kam.py

# Build portable .exe (runs tests first, then PyInstaller)
build_exe.bat
```

**Port:** Server and launcher accept an optional port argument; default is 5000. Examples: `python server.py 8080`, `python launch.py 8080`, `./run.sh 8080`.

**Virtual environment:** A `.venv` is present in the project root. Activate with `.venv\Scripts\activate` before running Python commands.

**macOS / Linux:** Use `./setup.sh` once to install dependencies and create dirs, then `./run.sh` or `./run.sh [PORT]` or `python3 server.py [PORT]`. Default port 5000; open http://localhost:5000 (or your port) in the browser.

**Test suite notes:**
- Set `CI=true` to skip hardware-dependent checks (GPUtil, WMI, live psutil reads)
- Generates `test_report.html` in the project root after every run
- Tests are static analysis + logic checks — no Flask server needs to be running

---

## Architecture: How It Works

### Threading Model (server.py)
Four daemon threads run at module load:
1. **`_cpu_loop`** — calls `psutil.cpu_percent(interval=1.0)` in a tight loop, caches to `_cpu_cache`. Hot path never blocks on CPU measurement.
2. **`_gpu_worker`** — calls `GPUtil.getGPUs()` (shells out to nvidia-smi) every 5s, caches to `_gpu_cache`. nvidia-smi never blocks the poll cycle.
3. **`_hw_scheduler`** — refreshes unified `_hw_cache` (CPU temp + voltage) every 10s for all platforms. Windows uses WMI; macOS uses `ioreg`; Linux uses `psutil.sensors_temperatures`.
4. **`watch_for_shutdown`** (in `launch.py`) — polls `/api/stats` every 3s; after 5 consecutive failures calls `os._exit(0)` to clean up when the browser tab closes.

**Locks:**
- **`_state_lock`** guards `history` deques (read + write)
- **`_log_lock`** guards `_log_buffer` (append + flush)
- **`_hw_lock`** guards `_hw_cache` (all platforms)
- **`_err_lock`** guards `_err_buffer` (error tracking)

### Backend (server.py)
- **`_live_stats()`** (hot path) — called on every `/api/stats` request; reads only from in-memory caches (`_cpu_cache`, `_gpu_cache`, `_hw_cache`). Zero hardware I/O on the Flask thread.
- **Unified `_hw_cache`** — replaces the Windows-only `_wmi_cache`. Dict: `{'cpu_temp': None, 'cpu_volt': None, 'ts': 0, 'ttl': 10}`
- **`get_cpu_temp_voltage()`** — hot path wrapper: just `with _hw_lock: return _hw_cache['cpu_temp'], _hw_cache['cpu_volt']`
- **`get_gpu_cached()`** — returns a snapshot copy of `_gpu_cache` under `_state_lock`
- **`collections.deque(maxlen=60)`** for all history buffers — no manual trimming needed
- **Batched log writes** — `_log_buffer` flushed every 60s (`LOG_FLUSH_SECS`), not per poll; also flushed on exit via `atexit`
- **Error tracking** — `_log_err(ctx, exc)` appends to `_err_buffer`; background flush writes to `logs/errors.jsonl`
- **`_net_warmed_up`** flag — first `_net_speed()` call returns zeros to prevent false-positive network spike
- **Startup sequence**: collects system info → loads/generates thresholds → saves original profile (once ever) → warms up CPU sampler (1.2s sleep) → saves baseline (once ever)

### Cross-Platform Hardware Monitoring
| Platform | CPU Temp | CPU Voltage | GPU |
|----------|----------|-------------|-----|
| Windows | WMI `MSAcpi_ThermalZoneTemperature` or LibreHardwareMonitor | WMI `Win32_Processor` | GPUtil (nvidia-smi) |
| macOS | `osx-cpu-temp` or `powermetrics` via subprocess | N/A | `system_profiler` + `ioreg` |
| Linux | `psutil.sensors_temperatures` | N/A | GPUtil (nvidia-smi) |

### Frontend (dashboard.html)
- **All DOM refs cached** on init in a `DOM{}` object — zero re-querying per render cycle
- **Single `/api/stats` fetch** per refresh cycle (configurable: 2s/5s/10s/30s/60s)
- **Chart history** driven from server-side deques, not client-side accumulation
- **`chart.update('none')`** — skips re-animation on every data update

### API Endpoints
| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/` | GET | Serves dashboard.html |
| `/api/system` | GET | Static hardware info (called once on page load) |
| `/api/stats` | GET | Live cached metrics snapshot + warnings + history |
| `/api/thresholds` | GET/POST | Read/write warning thresholds |
| `/api/thresholds/reset` | POST | Reset to smart hardware defaults |
| `/api/baseline` | GET | Day 1 baseline snapshot |
| `/api/original_profile` | GET | Original system profile |
| `/api/version` | GET | Version info for update notifications |
| `/api/feedback` | POST | Submit in-app feedback (auto-triaged by priority) |
| `/api/feedback/queue` | GET | Review open feedback items sorted by priority |
| `/api/diagnostics` | GET | Self-healing diagnostic info for N/A indicators (`?test=1` for mock) |
| `/api/autofix` | POST | Run whitelisted pip installs (strict `ALLOWED_FIXES` whitelist) |
| `/api/shutdown` | POST | Flush logs and shut down server (called by `beforeunload`) |
| `/api/telemetry` | GET | Anonymous install/launch stats (local only unless `TELEMETRY_URL` set) |
| `/api/errors` | GET | Recent error log entries from `logs/errors.jsonl` |
| `/api/eve/fix` | POST | Eve Santos 404 report: logs to `logs/bugs/eve_reported.jsonl`, triggers diagnosis, speaks locally |

---

## Warning System

7 warning types, all hardware-aware and user-customizable via `thresholds.py`:
- CPU temperature (model-specific TJmax from `CPU_THERMAL_MAP`)
- GPU temperature (model-specific limits from `GPU_THERMAL_MAP`)
- CPU voltage (min/max range per CPU family)
- CPU sustained usage (rolling `_sustained['cpu']` deque, configurable window)
- GPU sustained usage (rolling `_sustained['gpu']` deque, configurable window)
- RAM usage %
- Network spike (Nx above rolling `_net_baseline` average)

Warnings are dismissible banners (yellow=warning, red=critical). Auto re-enable after 60s.

---

## Safety Rules — NEVER VIOLATE THESE

1. **`backups/original_system_profile.json` is NEVER overwritten.** Saved once on first launch via `save_original_profile()` which checks `os.path.exists()` first. It is the rollback anchor for all future phases.

2. **Goal 4 (automated BIOS changes) requires explicit user confirmation for every single action.** Not active until Phase 4.

3. **Never auto-update the .exe.** The `/api/version` endpoint shows a notification banner only — the download/install flow has not been built.

4. **Flask `debug=False` always** — `debug=True` exposes an interactive debugger on the network.

5. **`/api/autofix` has a strict `ALLOWED_FIXES` whitelist** — only `pip install GPUtil` and `pip install wmi pywin32` are permitted. Never expand this without careful review.

---

## Distribution / PyInstaller Notes
- Entry point for the `.exe` is `launch.py`, not `server.py`
- `ASSET_DIR = sys._MEIPASS` — bundle temp dir where `--add-data` files land (`dashboard.html`, `thresholds.py`, `icon.ico`)
- `DATA_DIR = %APPDATA%\KAM Sentinel` when frozen — **not** next to the exe. Program Files is write-protected for non-admin users; `%APPDATA%` is always writable. Dev mode (`python server.py`) keeps `DATA_DIR` next to the source file.
- `dashboard.html` and `thresholds.py` are bundled via `--add-data` and accessed at runtime from `sys._MEIPASS`
- `subprocess.Popen` is monkey-patched at startup to add `CREATE_NO_WINDOW` when running frozen — prevents CMD window flash from nvidia-smi
- `backups/`, `logs/`, `profiles/` are auto-created under `DATA_DIR` on first launch via `os.makedirs(d, exist_ok=True)`

---

## Session Startup Ritual (every session)

Every Claude Code session on this repo MUST start with these steps before touching any code:

1. Run `git fetch --all` and `git log --all --oneline -10` — confirm branch/commit state
2. Run `python test_kam.py` — confirm all tests passing (currently 79/79)
3. Read `logs/feedback/` and `logs/bugs/` — print standup summary:
   - NEW feedback or bug reports since last session
   - Open/unresolved bugs
   - Feature requests not yet triaged
4. Check `logs/bugwatcher_daily/` for latest daily report
5. Report any escalated bugs (`logs/bugs/escalated.jsonl`) before starting new work

---

## Launching the App

**Windows (end users):**
- Double-click `launch_kam.bat` in the project folder
- Then open browser to: http://localhost:5000

**Developers:**
```bat
python server.py           # default port 5000
python server.py 8080      # custom port
```

---

## Hosting

- **GitHub Pages:** `docs/` folder, auto-deployed via `.github/workflows/pages.yml` on every push to `main`
- **URL:** https://kypin00-web.github.io/KAM-Sentinel
- **version.json:** `docs/version.json` (workflow copies root `version.json` → `docs/` on deploy)
- **Railway:** NOT set up yet — add when a backend feedback endpoint is needed
- **UPDATE_CHECK_URL** in `server.py` points to raw GitHub (already working); GitHub Pages URL is the public-facing landing page

---

## BugWatcher — Run by Eve Santos

BugWatcher is run by Eve Santos (E.V.E — Error Vigilance Engine). See `docs/eve.md` for her full persona. She speaks, she fixes, she does not tolerate 404s.

Background auto-fix daemon — runs silently, only surfaces escalated items.

- **Script:** `scripts/bugwatcher.py`
- **Start:** `python scripts/bugwatcher.py` (foreground) or `python scripts/bugwatcher.py --once` (single cycle / CI)
- **Poll interval:** 60 seconds
- **Logs all actions:** `logs/bugwatcher.jsonl` (timestamp, bug_id, action, result, tests_passing)
- **Daily summary:** `logs/bugwatcher_daily/YYYY-MM-DD.json` (generated at 23:55 local time) — printed in Eve's standup format
- **Escalated bugs:** `logs/bugs/escalated.jsonl` — ONLY these need human review
- **Eve-reported 404s:** `logs/bugs/eve_reported.jsonl` — from `/api/eve/fix` dashboard popup
- **Known issue patterns** live in `KNOWN_ISSUES` dict inside the script; expand as new patterns emerge
- **Criticality:** critical bugs → test suite runs after auto-resolve; high → fix within 3 cycles; medium/low → daily batch
- **Voice:** Eve speaks via pyttsx3 when running locally (`pip install pyttsx3`); silenced by `CI=true`

---

## Feedback Loop

- **Rate limiting:** 5 submissions per 60s per IP (`FB_RL_WIN`, `FB_RL_MAX` in `server.py`)
- **Dedup:** same category + message hash rejected within 1 hour (`FB_DEDUP_WIN = 3600`)
- **Feature requests** → `logs/features/backlog.jsonl` (triaged by BugWatcher or manually)
- **Bugs** → `logs/feedback/bug.jsonl` (auto-triaged by BugWatcher)
- Rate limit state: `_fb_rl`, `_fb_dedup`, `_fb_lock` — separate from the global `_rl` / `_rl_lock`

---

## Known Coding Constraints
- `psutil.sensors_temperatures()` returns nothing on most Windows machines — only WMI fallback (`MSAcpi_ThermalZoneTemperature`) and LibreHardwareMonitor (via WMI namespace `root/LibreHardwareMonitor`) provide real temps
- AMD Ryzen CPU temps specifically require LibreHardwareMonitor running as Administrator
- `GPUtil` is an optional dep — all GPU stats gracefully degrade to `None`/`"N/A"` if not installed
- `wmi`/`pywin32` are optional — voltage and some temp paths gracefully degrade
- Log files are `.jsonl` (one JSON object per line), stored in `logs/session_YYYY-MM-DD.jsonl`
- Server binds to `0.0.0.0:5000` by design (LAN accessible), but POST endpoints block non-localhost IPs
