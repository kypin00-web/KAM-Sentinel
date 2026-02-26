# KAM Sentinel — Roadmap

## Edition Roadmap
- **KAM Sentinel** — Phase 1: monitoring, warnings, baseline, backup
- **KAM Forge** — Phases 2–3: suggestions, thermal profiling, stress testing
- **KAM Apex** — Phases 4–5: automated BIOS changes, workload profiles, full suite

---

## Project Goals

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
| 10 | Update notifications + download flow | 1 | ✅ Complete — UPDATE_CHECK_URL live; version.json + GitHub Releases in place |
| 11 | In-game overlay (draggable, configurable, always-on-top) | 1 | ✅ Complete |
| 12 | Customizable refresh rate (2s/5s/10s/30s/60s) | 1 | ✅ Complete |
| 13 | Machine benchmarking — compare stats against other machines | 2 | 🔜 Next |
| 14 | Idiot-proof onboarding — detect missing deps, explain why, let user decide | 2 | 🔜 Next |
| 15 | Cross-platform support (Windows / macOS / Linux) | 1 | ✅ Complete (v1.4.0) |
| 16 | Privacy-safe install telemetry (anonymous, hardware class only) | 1 | ✅ Complete (v1.4.0) |
| 17 | Proactive error tracking (errors.jsonl, /api/errors) | 1 | ✅ Complete (v1.4.0) |

---

## v1.4.0 — Released 2026-02-25

All Phase 1 bugs resolved. Cross-platform. Lean rewrite. Beta 404 issue fixed.

Key fixes:
- `ASSET_DIR`/`DATA_DIR` split — no more 404 on launch from PyInstaller .exe
- Thread safety: `_log_lock`, `_hw_lock` guard all shared state
- `_net_warmed_up` — first-poll network spike false positive eliminated
- Missing `@app.route` on `/api/shutdown` — logs now flush on tab close
- WMI background isolation — COM calls never block the poll cycle

---

## Backlog

### Goal 10 — Update flow remaining work
The in-app update banner and GitHub Releases are now live. Remaining optional improvements:
1. In-app changelog modal when banner is clicked (currently links to release page)
2. One-click download button in the modal (download new `.exe`, rename old, relaunch)

### Phase 2 — KAM Forge
- Suggestions engine based on collected baseline data
- Thermal curve profiling over time
- Power draw tracking
- Machine benchmarking: compare stats against anonymized telemetry pool

### Phase 3 — Stress Testing
- Stepped load profiles (light → heavy → failure)
- Multiple benchmark types
- Pre/post comparison against baseline

### Phase 4 — Automated Changes (DANGER ZONE)
- Requires Phase 3 infrastructure proven first
- Every action needs explicit user confirmation
- Rollback profile saved before any change applied
- Will require elevated Windows permissions
