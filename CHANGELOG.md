# KAM Sentinel — Changelog

---

## v1.5.14 — 2026-02-28

### Critical Installer Fix — Program Files Write Permission

**Root cause:** When the NSIS installer places `KAM_Sentinel_Windows.exe` in `C:\Program Files\KAM Sentinel\`, `DATA_DIR` was set to `os.path.dirname(sys.executable)` — the same write-protected directory. Standard Windows users can't write there without admin rights. `os.makedirs()` fires at module import time (before Flask starts), so with `--noconsole` the exe silently crashed. A brand new user downloading the installer would see nothing — no dashboard, no error, no explanation.

**Fix:** When running as a PyInstaller frozen exe, `DATA_DIR` now points to `%APPDATA%\KAM Sentinel\` — the standard location for Windows user data, always writable without elevation.

```
Before (broken for installed users):
  DATA_DIR = os.path.dirname(sys.executable)
  → C:\Program Files\KAM Sentinel\  (PermissionError on first launch)

After (correct):
  DATA_DIR = %APPDATA%\KAM Sentinel\
  → C:\Users\<name>\AppData\Roaming\KAM Sentinel\  (always writable)
```

- `logs/`, `profiles/`, `backups/` now created under `%APPDATA%\KAM Sentinel\` when installed
- Dev mode (running `python server.py`) is unchanged — DATA_DIR stays next to `server.py`
- Portable mode (running the exe from Downloads / USB) also uses `%APPDATA%` — user data is preserved even if the exe is moved or deleted

**Everything else confirmed correct** — a full installer audit found no other missing files:
- `dashboard.html`, `thresholds.py`, `assets/icon.ico` all bundled via `--add-data` ✓
- `server.py` bundled by PyInstaller import tracing from `launch.py` ✓
- All Python deps (flask, psutil, GPUtil, wmi, pywin32) bundled ✓
- `scripts/`, `assets/kam_logo.svg` — not needed at runtime ✓

---

## v1.5.13 — 2026-02-27

### Wes Mode — Auto-Tune Frequency Calibration + Identity Check

KAM Sentinel now recognises Wes Johnson and delivers a personal message from Kris, then walks him through a hearing calibration flow so Eve always speaks at the right pitch.

#### `dashboard.html` — Wes Identity Check

- **First-launch identity prompt** — on `DOMContentLoaded`, `wesCheckIdentity()` fetches `/api/eve/identity`. If the OS username doesn't match `wes`/`johnson`, Eve shows a centered glassmorphism modal: *"Hey! Quick question before we get started… Are you Wes Johnson? 💕"*
- **[Yes, that's me!]** — saves `wes_identity=yes` to `localStorage`, closes the prompt, calls `_wesDeliverKrisMsg()`, then launches hearing calibration after a 4.8 s delay.
- **[No, I'm someone else]** — saves `wes_identity=no`, skips all Wes Mode features.
- **Auto-detect** — if `/api/eve/identity` returns `is_wes: true` (username already matches), Wes Mode enables silently with no prompt.
- **Kris's message** — Eve speaks *"I might not always be right, but I'm damn good at some things — love you dad"* via `POST /api/eve/calibrate` and shows it in a pink glassmorphism bubble (bottom-right, 9 s display).
- **`_wesAutoLaunchCalib()`** — extracted from the old inline `DOMContentLoaded` block; now only runs when identity is confirmed as Wes.

#### `server.py` — `GET /api/eve/identity`

- Reads `os.environ['USERNAME']` (Windows) / `os.environ['USER']` (Unix).
- Returns `{ username, is_wes }` — `is_wes` is `True` if username contains `wes` or `johnson` (case-insensitive).

#### `test_kam.py` — Section 16 Extended

- 7 new checks: `wesCheckIdentity`, `wesConfirmYes`/`wesConfirmNo`, `wes-identity-modal`, `_wesDeliverKrisMsg`, `kris-msg-bubble`, `/api/eve/identity` endpoint, `is_wes` flag.
- Fixed `_ROOT16 = ROOT` → `os.path.dirname(os.path.abspath(__file__))` (ROOT is not in test scope).
- Fixed Unicode encode error: `→` in ok() message replaced with `to` (Windows cp1252 console).
- Fixed emoji encode error: `🎵` in ok() message removed (Windows cp1252 console).

---

## v1.5.12 — 2026-02-27

### Eve Santos — E.V.E (Error Vigilance Engine) is Live

KAM Sentinel now has an autonomous bug-fixing agent with a full personality, voice, and style.

#### `scripts/bugwatcher.py` — Eve's Voice
- **`eve_speak(message)`** — pyttsx3-powered TTS, female voice (prefers Zira/Hazel/Samantha), rate 175 WPM. Non-blocking daemon thread. Silenced by `CI=true`. Requires `pip install pyttsx3`.
- **Voice triggers:** startup, bug report received, bug fixed, escalated, CI fix pushed, CI green, CI escalated.
- **All print() calls** rewritten in Eve's voice — `[Eve]` and `[Eve/CI]` prefixes replace generic `[BugWatcher]`.
- **Commit messages** now in Eve's style — key-specific headlines via `_EVE_COMMIT_MSGS`:
  - `nsis_not_in_path` → `"Fixed it 💕 NSIS path was dragging. You're welcome. — Eve"`
  - `test_suite_encoding_fp` → `"Ay, encoding check was flagging binaries. Not on my watch. — Eve"`
  - `missing_python_dep` → `"Added the missing dep, faster than you can say pip install. — Eve 💕"`
- **Escalation notes** in `ci_watcher.jsonl` now include `eve_note` field with her full escalation message.
- **Daily standup** printed in Eve's format at 23:55; `eve_standup` field added to the daily JSON.

#### `server.py` — `POST /api/eve/fix`
- Accepts `{url, error_code, context}` — logs to `logs/bugs/eve_reported.jsonl` with `eve_reported: true`.
- Triggers `pyttsx3` voice in a background thread when running locally (not in CI).
- Returns `{ message: "On it! I'll have this fixed faster than you can say 'ayudame' 💕 — Eve", logged: true }`.

#### `dashboard.html` — Eve 404 Popup
- **Fetch interceptor** (`window.fetch` wrapper) catches any `/api/` 404 and triggers Eve's popup.
- **5-minute cooldown** between popups — no spam.
- **Popup:** bottom-right corner, glassmorphism dark card, pink/coral `#ff6b9d` border and glow.
- **Eve avatar:** inline SVG shield with "E" in `#ff6b9d` (parallel to the KAM K shield design).
- **Speech bubble:** `"Ay, that's not supposed to happen! Want me to fix that? 💕"`
- **Buttons:** `[Yes, fix it! 💕]` → calls `POST /api/eve/fix`; `[Dismiss]` → hides popup.
- **Slide-in animation:** `cubic-bezier(.34,1.56,.64,1)` — bouncy entrance.

#### `docs/eve.md` — Eve's Full Persona
- Full character spec: role, experience, personality, zero-tolerance list, motto, languages.
- Voice trigger table, commit style examples, daily standup format, escalation note, API reference.

#### `docs/index.html` — "Meet Eve" Section
- New card above the footer: Eve's pink shield SVG, "Meet Eve — Your Bug-Fixing Agent" label, description.
- Nav version tag updated to `v1.5.12 STABLE`.

#### `CLAUDE.md` — Eve Santos section added to BugWatcher docs, `/api/eve/fix` added to endpoint table.

---

## v1.5.11 — 2026-02-27

### End-to-End URL Validation

- **`test_kam.py` Section 15 — Live URL Checks** — 4 URLs verified on every test run: GitHub Pages landing, GitHub Pages `version.json` (JSON-validated), GitHub Releases latest page, `KAM_Sentinel_Setup.exe` download. Skips gracefully with a single `[WARN]` if the machine has no internet (CI offline or firewall).
- **`scripts/check_urls.py`** — standalone validator; prints GREEN `[OK]` / RED `[FAIL]` per URL with HTTP code and response time. Logs results to `logs/url_checks.jsonl`. Checks all 6 public URLs (landing, version.json, releases page, Setup.exe, Windows.exe, Mac binary).

---

## v1.5.10 — 2026-02-27

### BugWatcher as a CI Service
- **`.github/workflows/bugwatcher.yml`** — triggers automatically via `workflow_run` whenever `deploy.yml` completes with `conclusion: failure`. Runs `python scripts/bugwatcher.py --ci --wait` on GitHub's ubuntu-latest. No local daemon needed.
- Workflow has `contents: write` + `actions: read` permissions — allows BugWatcher to push auto-fix commits directly from the CI runner.
- `workflow_dispatch` trigger also included for manual on-demand diagnosis runs.
- Uploads `logs/ci_watcher.jsonl` as an artifact (7-day retention) for post-mortem review.

### Rate Limiting — Bug Fix
- **Root cause:** `_guard()` was defined but never called from any endpoint — the rate limiter existed in code but was completely inactive.
- **Fix:** `api_stats()` now calls `_guard()` at the top, properly enforcing 10 req/s per non-localhost IP.
- **Test fix:** The rate limit test no longer relies on timing (15 requests within a 1-second window is flaky in CI). Now pre-fills `srv._rl[test_ip]` to `RL_MAX` and makes exactly 1 more request to confirm 429 — deterministic regardless of request speed.

---

## v1.5.9 — 2026-02-27

### CI Hardening — Zero Broken Builds to Production

#### deploy.yml — Strict Job Ordering
- **Test gate job** (`ubuntu-latest`) runs `python test_kam.py` first. If it fails, the entire pipeline stops — no Windows build, no macOS build, no release.
- **`build-windows`** and **`build-macos`** now both declare `needs: [test]`.
- **`release`** now declares `needs: [test, build-windows, build-macos]` — all three must succeed before any release assets are published.
- **Test run removed from build jobs** — tests run once (in the gate job), not three times.

#### test_kam.py — Section 14: NSIS Installer & CI Pipeline
- 11 new checks: `scripts/installer.nsi` exists and contains `Name`, `OutFile`, `Section`, `SectionEnd`; `deploy.yml` contains `choco install nsis -y` and the hardcoded full makensis path; `test:` gate job exists; `needs: [test]` on build jobs; `needs: [test, build-windows, build-macos]` on release.
- **Shutdown test fix** — section 10's `/api/shutdown` test was actually POSTing to the endpoint, which started an `os._exit(0)` thread 0.5s later and killed the test process before sections 12-14 ever ran. Changed to route-registration check (`url_map.iter_rules`).

#### BugWatcher — Auto-Fix Pipeline
- `_fix_nsis_path()` — patches `deploy.yml` to use `choco install nsis -y` + hardcoded full path if the fix has regressed.
- `_fix_missing_module(logs_text)` — parses `ModuleNotFoundError`, maps to pip package via `_MODULE_TO_PKG` whitelist, adds to ubuntu install line.
- `_fix_encoding_false_positive()` — adds the binary-mode regex exclusion to `test_kam.py` if it has been removed.
- `_git_push_fix(files, commit_message)` — stages files, commits, pushes, returns new HEAD SHA.
- `_wait_for_ci_run(sha)` — polls Actions API every 30s for up to 10 min; returns `success`, `failure`, or `timeout`.
- `--wait` flag — enables wait-for-green confirmation after pushing an auto-fix.
- ANSI code stripping in `_run_tests()` — `Passed:` line is now correctly parsed regardless of terminal color output.

---

## v1.5.8 — 2026-02-27

### CI Fix — NSIS makensis not found after Chocolatey install
- **Root cause** — PowerShell doesn't refresh `$env:PATH` mid-script, so `makensis` isn't resolvable immediately after `choco install nsis -y`.
- **Fix** — `choco install nsis -y` unconditionally, then invoke via hardcoded full path `& "C:\Program Files (x86)\NSIS\makensis.exe"`. Chocolatey always installs NSIS to that location, so the path is reliable.

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
