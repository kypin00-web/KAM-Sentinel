# KAM Sentinel — Claude Code Project Memory

## What This Project Is
KAM Sentinel is a **portable Windows PC performance monitoring dashboard** built for gaming and power users. It runs as a single `.exe` (no Python required on target machines), opens a local browser dashboard, and monitors CPU, GPU, RAM, temperature, voltage, and network in real time with smart hardware-aware warnings.

**Current build: Phase 1 — Sentinel Edition (v1.2)**

---

## Project Goals (All 10)

| # | Goal | Phase | Status |
|---|------|-------|--------|
| 1 | Live performance monitoring dashboard | 1 | ✅ Complete |
| 2 | Intelligent OC/tuning suggestions | 2 | Planned |
| 3 | Stress testing to failure | 3 | Planned |
| 4 | Automated BIOS/system changes | 4 | Planned |
| 5 | Stability testing post-change | 3 | Planned |
| 6 | Baseline & session history logging | 1 | ✅ Complete |
| 7 | Rollback & recovery (original profile backup) | 1 | ✅ Complete |
| 8 | Thermal & power profiling | 2 | Planned |
| 9 | Workload profiles (gaming, streaming, idle) | 5 | Planned |
| 10 | Update notifications (banner only — download flow not yet built) | 1 | ⚠️ Partial |
| 11 | In-game overlay (draggable, configurable, always-on-top) | 1 | ✅ Complete |
| 12 | Customizable refresh rate (2s/5s/10s/30s/60s) | 1 | ✅ Complete |
| 13 | Machine benchmarking — compare stats against other machines | 2 | 🔜 Next |
| 14 | Idiot-proof onboarding — detect missing deps, explain why, let user decide | 2 | 🔜 Next |

---

## Edition Roadmap
- **KAM Sentinel** — Phase 1: monitoring, warnings, baseline, backup
- **KAM Forge** — Phases 2–3: suggestions, thermal profiling, stress testing
- **KAM Apex** — Phases 4–5: automated BIOS changes, workload profiles, full suite

---

## File Structure

```
dashboard/
├── CLAUDE.md                  ← you are here
├── server.py                  ← Flask backend, background polling thread, warning engine
├── thresholds.py              ← hardware-aware threshold defaults, 15+ CPU/GPU models
├── dashboard.html             ← single-file frontend, dark UI, charts, warnings, overlay, settings modal
├── test_kam.py                ← automated test suite (bugs, leaks, performance, architecture)
├── launch.py                  ← PyInstaller entry point, auto-opens browser
├── build_exe.bat              ← builds KAM_Sentinel.exe via PyInstaller
├── setup.bat                  ← dev setup, installs pip deps, starts server
│
├── backups/                   ← created on first launch
│   └── original_system_profile.json   ← NEVER OVERWRITE — rollback anchor (Goal 7)
├── profiles/                  ← created on first launch
│   ├── baseline.json          ← Day 1 performance snapshot
│   └── thresholds.json        ← user-customized warning thresholds
├── logs/                      ← created on first launch
│   └── session_YYYY-MM-DD.jsonl  ← rotates at 5000 lines
└── version.json               ← created on first launch, used for Goal 10 update checks
```

---

## Architecture: How It Works

### Backend (server.py)
- **Background daemon thread** polls hardware every 4.5s — Flask serves cached data instantly, zero blocking
- **WMI calls cached 30s** — WMI COM operations are slow (50–200ms), only re-run every 30s
- **`collections.deque(maxlen=60)`** for all history buffers — O(1), no manual trimming
- **`cpu_percent(interval=0)`** — non-blocking delta measurement
- **Batched log writes** — disk I/O every 10 samples, not every poll
- **Flask `threaded=True`** — concurrent request handling

### Frontend (dashboard.html)
- **All 38 DOM refs cached** on init in `DOM{}` object — zero re-querying per render
- **Single `/api/stats` fetch** per 5s cycle — was 5 separate calls, now 1
- **Chart history** driven from server-side deque, not client-side pushes
- **`chart.update('none')`** — no re-animation cost on every update

### API Endpoints
| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/` | GET | Serves dashboard.html |
| `/api/system` | GET | Static hardware info (called once) |
| `/api/stats` | GET | Live cached metrics snapshot |
| `/api/thresholds` | GET/POST | Read/write warning thresholds |
| `/api/thresholds/reset` | POST | Reset to smart hardware defaults |
| `/api/baseline` | GET | Day 1 baseline snapshot |
| `/api/original_profile` | GET | Original system profile |
| `/api/version` | GET | Version info for update notifications |

---

## Warning System

6 warning types, all hardware-aware and user-customizable:
- CPU temperature (model-specific TJmax lookup)
- GPU temperature (model-specific limits)
- CPU voltage (min/max range per CPU family)
- CPU sustained usage (rolling buffer, configurable window)
- GPU sustained usage (rolling buffer, configurable window)
- RAM usage %
- Network spike (Nx above rolling baseline)

Warnings are dismissible banners (yellow=warning, red=critical). Auto re-enable after 60s.
Settings panel accessible via ⚙ THRESHOLDS button — all thresholds live-editable.

---

## Safety Rules — NEVER VIOLATE THESE

1. **`backups/original_system_profile.json` is NEVER overwritten.** It is saved once on first launch and is the rollback anchor for all future phases. If it exists, skip saving.

2. **Goal 4 (automated BIOS changes) requires explicit user confirmation for every single action.** No automated changes are ever applied silently. This goal is not active until Phase 4.

3. **Never auto-update the .exe.** Goal 10 update flow is user-initiated only. The current build shows a notification banner only — the download/install flow has not been built yet.

4. **Stress testing (Goal 3) must be graceful.** Designed to find failure limits, not cause data loss or hardware damage.

5. **Phase 4 does not activate until Phase 3 stability infrastructure is proven.**

---

## Distribution Model
- Target: single `KAM_Sentinel.exe`, no Python needed
- Built with: `build_exe.bat` → PyInstaller `--onefile --noconsole`
- Bundles: `dashboard.html`, `thresholds.py` via `--add-data`
- First launch: creates `backups/`, `logs/`, `profiles/`, `version.json` next to the .exe
- Works on any Windows machine — hardware auto-detected fresh on every machine
- No hardcoded hardware values anywhere

---

## Development Setup (Windows)
```bat
# Install dependencies and start dev server
setup.bat

# Build portable .exe
build_exe.bat

# Output: dist/KAM_Sentinel.exe
```

Dependencies: `flask`, `psutil`, `GPUtil`, `wmi`, `pywin32`, `pyinstaller`

---

## What's Next (Backlog)

### Goal 10 — Complete the update flow (not yet built)
The notification banner exists. Still needed:
1. Modal showing changelog when banner is clicked
2. Download new `.exe` next to current one (rename old first — Windows can't overwrite running .exe)
3. Relaunch new `.exe`, close old one
4. Hosted `version.json` URL needs to be set in `server.py` → `UPDATE_CHECK_URL`

### Phase 2 — KAM Forge
- Suggestions engine based on collected baseline data
- Thermal curve profiling over time
- Power draw tracking

### Phase 3 — Stress Testing
- Stepped load profiles (light → heavy → failure)
- Multiple benchmark types
- Pre/post comparison against baseline

### Phase 4 — Automated Changes (DANGER ZONE)
- Requires Phase 3 infrastructure proven first
- Every action needs explicit user confirmation
- Rollback profile saved before any change applied
- Will require elevated Windows permissions

---

## Known Limitations / Notes
- `psutil.sensors_temperatures()` may return nothing on Windows — falls back to WMI cache
- GPU stats require `GPUtil` — graceful N/A display if not installed
- WMI requires `pywin32` — graceful fallback if not available
- First `.exe` launch on a new machine may be slow (Windows scanning new executable)
- Log files are `.jsonl` format — one JSON object per line, easy to parse
- `version.json` `UPDATE_CHECK_URL` field is empty string until a hosting URL is configured
