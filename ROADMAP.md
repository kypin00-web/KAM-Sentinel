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
| 10 | Update notifications (banner only — download flow not yet built) | 1 | ⚠️ Partial — UPDATE_CHECK_URL constant added in v1.3; set URL to activate banner |
| 11 | In-game overlay (draggable, configurable, always-on-top) | 1 | ✅ Complete |
| 12 | Customizable refresh rate (2s/5s/10s/30s/60s) | 1 | ✅ Complete |
| 13 | Machine benchmarking — compare stats against other machines | 2 | 🔜 Next |
| 14 | Idiot-proof onboarding — detect missing deps, explain why, let user decide | 2 | 🔜 Next |

---

## Backlog

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
