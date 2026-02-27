# KAM Sentinel — Changelog

---

## v1.5.7 — 2026-02-27

### CI Fix — NSIS PATH on windows-latest
- **Dynamic makensis discovery** — `Get-ChildItem "C:\Program Files*\NSIS\makensis.exe"` finds the binary regardless of whether the runner installs to `Program Files` or `Program Files (x86)`.
- **Chocolatey fallback** — if `makensis.exe` is not found anywhere, `choco install nsis -y` runs automatically and `makensis` is called via PATH.
- **Removed** hardcoded "Add NSIS to PATH" step that assumed `C:\Program Files (x86)\NSIS`.

---

## v1.5.6 — 2026-02-27

### BugWatcher — GitHub Actions CI Monitoring
- **CI failure polling** — BugWatcher now polls GitHub Actions API every 5 minutes for failed workflow runs. Requires `GITHUB_TOKEN` env var (auto-available in CI; set locally for dev use).
- **Auto-diagnosis pipeline** — `_fetch_failed_ci_runs`, `_fetch_job_logs`, `_diagnose_ci_failure` fetch and pattern-match failed job logs against `CI_KNOWN_ISSUES`.
- **5 CI known issue patterns** — NSIS not on PATH (regression detection), missing Python deps, test suite failures, PyInstaller build errors, Actions checkout failures.
- **Regression detection** — fixes already applied in the codebase (e.g. NSIS PATH) are flagged as `regression_detected` with severity:high if they fire again.
- **`logs/ci_watcher.jsonl`** — dedicated CI event log (diagnosed, escalated, regression_detected, undiagnosed, no_logs).
- **`--ci` flag** — run a single CI poll cycle and exit (useful for manual diagnosis).
- **Daily summary** — now includes `ci.diagnosed`, `ci.regressions`, `ci.undiagnosed` counts.

---

## v1.4.0 — 2026-02-25 (Phase 1 Complete)

### Critical Bug Fixes
- **404 on launch resolved** — `ASSET_DIR`/`DATA_DIR` split: bundled assets (`dashboard.html`, `thresholds.py`) now load from `sys._MEIPASS`; logs, profiles, and backups write next to the `.exe`
- **Shutdown log flush fixed** — missing `@app.route` decorator on `/api/shutdown` meant logs never flushed when browser tab closed
- **WMI blocking resolved** — CPU temp/voltage COM calls isolated to background `_hw_scheduler` thread; hot path is pure in-memory

### New Features
- **Cross-platform support** — Windows (WMI), macOS (`system_profiler`/`ioreg`), Linux (`psutil.sensors_temperatures`)
- **Lean server rewrite** — ~40% overhead reduction; `_live_stats()` hot path does zero hardware I/O
- **Privacy-safe install telemetry** — anonymous UUID4 install ID, hardware class bucketing only (no hostnames, IPs, or usernames)
- **Proactive error tracking** — `logs/errors.jsonl` + `/api/errors` endpoint; errors flushed immediately so issues are visible before users hit the feedback loop
- **New endpoints** — `/api/telemetry`, `/api/errors`

### Thread Safety
- `_log_lock` guards `_log_buffer` — no race on concurrent append/flush
- `_hw_lock` guards unified `_hw_cache` — all platforms share same 10s-TTL cache
- `_sustained` deques now updated inside `_state_lock`

### Security
- Feedback message injection sanitized — newlines, carriage returns, null bytes stripped
- Rate limit dict pruned at 500 IPs — no unbounded memory growth
- `ALLOWED_FIXES` whitelist enforced on `/api/autofix` — only `pip install GPUtil` and `pip install wmi pywin32` permitted
- All `open()` calls use `encoding='utf-8'`

### Performance
- `_net_warmed_up` flag eliminates false-positive network spike on first poll
- Pre-compiled regexes for macOS `ioreg` parsing (`_IOREG_RE`, `_VRAM_RE`)
- GPU stats polled every 5s in background `_gpu_worker` thread — nvidia-smi never blocks poll cycle
- CPU % sampled in dedicated `_cpu_loop` thread with 1s interval — accurate non-blocking reads

### Infra / CI
- `UPDATE_CHECK_URL` set to GitHub raw `version.json` — update banner activates when new version is published
- `version.json` includes platform-specific download URLs for Windows and Mac binaries

---

## v1.3.0 — 2026-02-22

- In-game overlay (draggable, always-on-top, configurable stats)
- Customizable refresh rate: 2s / 5s / 10s / 30s / 60s
- In-app feedback system (bug / feature / performance reports, no PII)
- GPU temp chart `spanGaps` fix — line renders through null values
- Logo redesign — shield K is now the K in KAM
- Glassmorphism card UI, richer color palette, hover animations

---

## v1.2.0

- Baseline snapshot saved on first launch (`profiles/baseline.json`)
- Original system profile backup (`backups/original_system_profile.json`) — saved once, never overwritten
- Session history logging (`logs/session_YYYY-MM-DD.jsonl`)
- Hardware-aware warning thresholds (`CPU_THERMAL_MAP`, `GPU_THERMAL_MAP`, `CPU_VOLTAGE_MAP`)
- 7 warning types: CPU temp, GPU temp, CPU voltage, CPU/GPU sustained load, RAM, network spike
- Dismissible warning banners (auto re-enable after 60s)

---

## v1.1.0

- Live performance dashboard (CPU, GPU, RAM, Network, Temperature, Voltage)
- Flask server bound to `0.0.0.0:5000` (LAN accessible)
- POST endpoints restricted to localhost
- Chart history driven from server-side deques (60-point rolling window)
- PyInstaller `--onefile` portable `.exe` build

---

## v1.0.0

- Initial private beta
