#!/usr/bin/env python3
"""
BugWatcher — KAM Sentinel background auto-fix daemon

Polls logs/feedback/bug.jsonl for open bugs, matches against known issues,
auto-resolves or escalates, runs the test suite to verify fixes, and generates
daily summaries. Escalated items are the only ones surfaced to the user.

Usage:
    python scripts/bugwatcher.py          # run in foreground (Ctrl+C to stop)
    python scripts/bugwatcher.py --once   # single poll cycle then exit (CI/testing)
"""

import json, os, sys, time, datetime, subprocess, argparse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

FEEDBACK_BUG_FILE  = os.path.join(ROOT, 'logs', 'feedback', 'bug.jsonl')
BUGS_DIR           = os.path.join(ROOT, 'logs', 'bugs')
ESCALATED_FILE     = os.path.join(BUGS_DIR, 'escalated.jsonl')
WATCHER_LOG        = os.path.join(ROOT, 'logs', 'bugwatcher.jsonl')
DAILY_DIR          = os.path.join(ROOT, 'logs', 'bugwatcher_daily')
FEATURES_BACKLOG   = os.path.join(ROOT, 'logs', 'features', 'backlog.jsonl')

POLL_INTERVAL = 60  # seconds between cycles

# ── Known issue patterns ───────────────────────────────────────────────────────
# Each entry: patterns (list of substrings to match in lowercased message),
# action ('resolve' | 'resolve_if_old' | 'escalate'),
# versions_fixed (list, for resolve_if_old),
# fix_summary (human-readable resolution note).
KNOWN_ISSUES = {
    'test_data_sanitization': {
        'patterns': ['crash line2 line3end'],
        'action': 'resolve',
        'fix_summary': 'Test data from sanitization test suite — not a real user report.',
    },
    'startup_crash_v140': {
        'patterns': ['crash', 'startup', 'not working', "won't start", 'fails to start', 'won\'t launch'],
        'action': 'resolve_if_old',
        'versions_fixed': ['1.4.1'],
        'fix_summary': 'Fixed in v1.4.1: ASSET_DIR/DATA_DIR path split corrected for frozen .exe; '
                       'api/shutdown decorator restored; send_from_directory pointed at ASSET_DIR.',
    },
    'na_readings': {
        'patterns': ['n/a', 'not showing', 'blank', 'missing reading', 'missing sensor'],
        'action': 'resolve_if_old',
        'versions_fixed': ['1.4.0'],
        'fix_summary': 'Hardware sensor N/A is expected on machines without LibreHardwareMonitor '
                       'or where WMI sensors are unavailable. See /api/diagnostics for details.',
    },
}


# ── Helpers ────────────────────────────────────────────────────────────────────

def _ver_tuple(v):
    """Convert '1.4.0' → (1, 4, 0) for comparison."""
    try:
        return tuple(int(x) for x in str(v).strip().split('.')[:3])
    except Exception:
        return (0, 0, 0)


def _log(bug_id, action, result, tests_passing=None):
    os.makedirs(os.path.dirname(WATCHER_LOG), exist_ok=True)
    entry = {
        'ts': time.time(),
        'date': datetime.datetime.now().isoformat(),
        'bug_id': bug_id,
        'action': action,
        'result': result,
        'tests_passing': tests_passing,
    }
    with open(WATCHER_LOG, 'a', encoding='utf-8') as f:
        f.write(json.dumps(entry) + '\n')


def _run_tests():
    """Run test_kam.py, return number of passing tests or None on error."""
    try:
        result = subprocess.run(
            [sys.executable, '-u', 'test_kam.py'],
            capture_output=True, text=True, timeout=180, cwd=ROOT
        )
        for line in result.stdout.splitlines():
            stripped = line.strip()
            if stripped.startswith('Passed'):
                return int(stripped.split(':')[1].strip())
    except Exception:
        pass
    return None


def _load_open_bugs():
    bugs = []
    if not os.path.exists(FEEDBACK_BUG_FILE):
        return bugs
    with open(FEEDBACK_BUG_FILE, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                b = json.loads(line)
                if b.get('status') == 'open':
                    bugs.append(b)
            except Exception:
                pass
    return bugs


def _rewrite_bug(bug_id, status, fix_summary, tests_passing=None):
    """Update a single bug entry's status in-place by rewriting the file."""
    if not os.path.exists(FEEDBACK_BUG_FILE):
        return
    lines_out = []
    with open(FEEDBACK_BUG_FILE, encoding='utf-8') as f:
        for raw in f:
            raw = raw.rstrip('\n')
            if not raw.strip():
                continue
            try:
                b = json.loads(raw)
                if b.get('id') == bug_id:
                    b['status'] = status
                    b['resolved_at'] = datetime.datetime.now().isoformat()
                    b['fix_summary'] = fix_summary
                    if tests_passing is not None:
                        b['tests_passing'] = tests_passing
                    raw = json.dumps(b)
            except Exception:
                pass
            lines_out.append(raw)
    with open(FEEDBACK_BUG_FILE, 'w', encoding='utf-8') as f:
        for line in lines_out:
            f.write(line + '\n')


def _escalate(bug):
    os.makedirs(BUGS_DIR, exist_ok=True)
    bug = dict(bug)
    bug['escalated_at'] = datetime.datetime.now().isoformat()
    with open(ESCALATED_FILE, 'a', encoding='utf-8') as f:
        f.write(json.dumps(bug) + '\n')


def _match(bug):
    """Return (key, issue_dict) for the first matching known issue, else (None, None)."""
    msg = bug.get('message', '').lower()
    for key, issue in KNOWN_ISSUES.items():
        if any(p in msg for p in issue['patterns']):
            return key, issue
    return None, None


# ── Poll cycle ─────────────────────────────────────────────────────────────────

def poll_cycle(seen_ids):
    """
    One pass over open bugs.
    Returns (fixed_ids, escalated_ids).
    seen_ids is a set updated in-place to avoid re-processing.
    """
    bugs = _load_open_bugs()
    fixed, escalated = [], []

    for bug in bugs:
        bug_id   = bug.get('id', 'UNKNOWN')
        priority = bug.get('priority', 'normal')

        if bug_id in seen_ids:
            continue

        key, issue = _match(bug)

        if issue:
            action = issue.get('action', 'resolve')

            if action == 'resolve':
                tests_n = _run_tests() if priority == 'critical' else None
                _rewrite_bug(bug_id, 'resolved', issue['fix_summary'], tests_n)
                _log(bug_id, 'auto_resolved', issue['fix_summary'], tests_n)
                fixed.append(bug_id)
                seen_ids.add(bug_id)

            elif action == 'resolve_if_old':
                bug_ver   = _ver_tuple(bug.get('version', '0.0.0'))
                fixed_in  = [_ver_tuple(v) for v in issue.get('versions_fixed', [])]
                if fixed_in and bug_ver < min(fixed_in):
                    tests_n = _run_tests() if priority == 'critical' else None
                    _rewrite_bug(bug_id, 'resolved', issue['fix_summary'], tests_n)
                    _log(bug_id, 'auto_resolved', issue['fix_summary'], tests_n)
                    fixed.append(bug_id)
                    seen_ids.add(bug_id)
                else:
                    # Bug version >= fix version — may still be happening; escalate
                    _escalate(bug)
                    _rewrite_bug(bug_id, 'escalated', 'Version >= fix version — requires human review')
                    _log(bug_id, 'escalated',
                         f'Bug on v{bug.get("version")} but fix was in {issue["versions_fixed"]}')
                    escalated.append(bug_id)
                    seen_ids.add(bug_id)

        else:
            # No matching known issue — escalate
            _escalate(bug)
            _rewrite_bug(bug_id, 'escalated', 'No matching known issue — requires human review')
            _log(bug_id, 'escalated', 'No pattern match — escalated for human review')
            escalated.append(bug_id)
            seen_ids.add(bug_id)

    return fixed, escalated


# ── Daily summary ──────────────────────────────────────────────────────────────

def daily_summary():
    today = datetime.date.today().isoformat()
    os.makedirs(DAILY_DIR, exist_ok=True)
    out = os.path.join(DAILY_DIR, f'{today}.json')

    fixed, escalated, still_open = [], [], []

    if os.path.exists(FEEDBACK_BUG_FILE):
        with open(FEEDBACK_BUG_FILE, encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    b = json.loads(line)
                    s = b.get('status', 'open')
                    bid = b.get('id', '?')
                    if s == 'resolved' and b.get('resolved_at', '').startswith(today):
                        fixed.append(bid)
                    elif s in ('open',):
                        still_open.append(bid)
                    elif s == 'escalated' and b.get('escalated_at', '').startswith(today) if 'escalated_at' in b else False:
                        escalated.append(bid)
                except Exception:
                    pass

    if os.path.exists(ESCALATED_FILE):
        with open(ESCALATED_FILE, encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    b = json.loads(line)
                    if b.get('escalated_at', '').startswith(today):
                        bid = b.get('id', '?')
                        if bid not in escalated:
                            escalated.append(bid)
                except Exception:
                    pass

    # Latest test count from watcher log
    tests_passing = None
    if os.path.exists(WATCHER_LOG):
        with open(WATCHER_LOG, encoding='utf-8') as f:
            lines = f.readlines()
        for raw in reversed(lines):
            try:
                e = json.loads(raw.strip())
                if e.get('tests_passing') is not None:
                    tests_passing = e['tests_passing']
                    break
            except Exception:
                pass

    summary = {
        'date': today,
        'fixed': fixed,
        'escalated': escalated,
        'still_open': still_open,
        'tests_passing': tests_passing,
    }
    with open(out, 'w', encoding='utf-8') as f:
        json.dump(summary, f, indent=2)
    return summary


# ── Entry point ────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description='KAM Sentinel BugWatcher daemon')
    parser.add_argument('--once', action='store_true',
                        help='Run one poll cycle then exit (for CI/testing)')
    args = parser.parse_args()

    os.makedirs(BUGS_DIR, exist_ok=True)
    os.makedirs(DAILY_DIR, exist_ok=True)

    _log('SYSTEM', 'started', 'BugWatcher daemon started')
    print(f'[BugWatcher] Started — polling every {POLL_INTERVAL}s  (--once: {args.once})',
          flush=True)

    seen_ids  = set()
    last_daily = None

    while True:
        try:
            fixed, escalated = poll_cycle(seen_ids)
            if fixed:
                print(f'[BugWatcher] Auto-resolved: {fixed}', flush=True)
                _log('SYSTEM', 'cycle_complete', f'Resolved: {fixed}')
            if escalated:
                print(f'[BugWatcher] Escalated (needs review): {escalated}', flush=True)
                _log('SYSTEM', 'cycle_complete', f'Escalated: {escalated}')
        except Exception as exc:
            _log('SYSTEM', 'poll_error', str(exc))

        # Daily summary at 23:55–23:59
        now = datetime.datetime.now()
        today = datetime.date.today().isoformat()
        if now.hour == 23 and now.minute >= 55 and last_daily != today:
            try:
                s = daily_summary()
                last_daily = today
                _log('SYSTEM', 'daily_summary',
                     f'fixed={len(s["fixed"])} escalated={len(s["escalated"])} '
                     f'open={len(s["still_open"])}')
                print(f'[BugWatcher] Daily summary written → logs/bugwatcher_daily/{today}.json',
                      flush=True)
            except Exception as exc:
                _log('SYSTEM', 'daily_summary_error', str(exc))

        if args.once:
            break

        time.sleep(POLL_INTERVAL)


if __name__ == '__main__':
    main()
