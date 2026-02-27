#!/usr/bin/env python3
"""
BugWatcher — KAM Sentinel background auto-fix daemon

Polls logs/feedback/bug.jsonl for open bugs, matches against known issues,
auto-resolves or escalates, runs the test suite to verify fixes, and generates
daily summaries. Also monitors GitHub Actions CI runs for failures and
attempts auto-diagnosis and fix logging.

Usage:
    python scripts/bugwatcher.py          # run in foreground (Ctrl+C to stop)
    python scripts/bugwatcher.py --once   # single local poll cycle then exit (CI/testing)
    python scripts/bugwatcher.py --ci     # single CI poll cycle then exit

Environment:
    GITHUB_TOKEN   GitHub personal access token (read:actions + contents scope)
                   Required for CI monitoring. Automatically available in CI.
"""

import json, os, sys, time, datetime, subprocess, argparse
import urllib.request, urllib.error

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

FEEDBACK_BUG_FILE  = os.path.join(ROOT, 'logs', 'feedback', 'bug.jsonl')
BUGS_DIR           = os.path.join(ROOT, 'logs', 'bugs')
ESCALATED_FILE     = os.path.join(BUGS_DIR, 'escalated.jsonl')
WATCHER_LOG        = os.path.join(ROOT, 'logs', 'bugwatcher.jsonl')
CI_LOG             = os.path.join(ROOT, 'logs', 'ci_watcher.jsonl')
DAILY_DIR          = os.path.join(ROOT, 'logs', 'bugwatcher_daily')
FEATURES_BACKLOG   = os.path.join(ROOT, 'logs', 'features', 'backlog.jsonl')

POLL_INTERVAL    = 60   # seconds between local bug poll cycles
CI_POLL_INTERVAL = 300  # seconds between GitHub Actions poll cycles (5 min)

GITHUB_TOKEN = os.environ.get('GITHUB_TOKEN', '')
GITHUB_REPO  = 'kypin00-web/KAM-Sentinel'


# ── Known local issue patterns ─────────────────────────────────────────────────
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


# ── Known CI failure patterns ──────────────────────────────────────────────────
# Matched against the concatenated job log text (lowercased).
# fix: 'already_applied' = known-fixed regression (log + escalate)
#      'log_and_escalate' = no safe auto-fix available (log + escalate)
CI_KNOWN_ISSUES = {
    'nsis_not_in_path': {
        'patterns': [
            "makensis' is not recognized",
            "makensis is not recognized",
            "'makensis' is not recognized",
            'makensis: command not found',
        ],
        'description': 'NSIS not on PATH in windows-latest runner',
        'fix': 'already_applied',
        'fix_summary': (
            'NSIS PATH fix already applied in deploy.yml (c5263f0): '
            '"C:\\Program Files (x86)\\NSIS" added to $env:GITHUB_PATH before makensis call. '
            'If this fires again, check for workflow file regression.'
        ),
    },
    'missing_python_dep': {
        'patterns': [
            'no module named',
            'modulenotfounderror',
            'importerror',
            'cannot import name',
        ],
        'description': 'Python dependency missing in CI environment',
        'fix': 'log_and_escalate',
        'fix_summary': (
            'CI is missing a Python dependency. '
            'Check the pip install line in deploy.yml and add the missing package.'
        ),
    },
    'test_suite_failed': {
        'patterns': [
            '[fail]',
            'assertion error',
            'assertionerror',
            'tests failed',
            'failed:',
            'error: test',
        ],
        'description': 'Test suite failure detected in CI',
        'fix': 'log_and_escalate',
        'fix_summary': (
            'Test suite failed in CI. '
            'Run `python test_kam.py` locally to reproduce and fix before pushing.'
        ),
    },
    'pyinstaller_build_failed': {
        'patterns': [
            'error: failed to',
            'build failed',
            'pyinstaller failed',
            'failed to collect',
            'cannot find',
        ],
        'description': 'PyInstaller build failure',
        'fix': 'log_and_escalate',
        'fix_summary': (
            'PyInstaller build failed in CI. '
            'Check --add-data and --hidden-import entries in deploy.yml for missing files or deps.'
        ),
    },
    'actions_checkout_failed': {
        'patterns': [
            'error: process completed with exit code',
            'error: the process',
            'checkout failed',
        ],
        'description': 'GitHub Actions checkout or runner step failed',
        'fix': 'log_and_escalate',
        'fix_summary': (
            'An Actions step failed (possibly checkout or runner issue). '
            'Check the run URL and re-trigger if transient.'
        ),
    },
}


# ── Local bug helpers ──────────────────────────────────────────────────────────

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


# ── Local poll cycle ───────────────────────────────────────────────────────────

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


# ── GitHub Actions CI monitoring ───────────────────────────────────────────────

def _log_ci(run_id, action, result):
    """Log a CI watcher event to logs/ci_watcher.jsonl."""
    os.makedirs(os.path.dirname(CI_LOG), exist_ok=True)
    entry = {
        'ts':     time.time(),
        'date':   datetime.datetime.now().isoformat(),
        'run_id': run_id,
        'action': action,
        'result': result,
        'source': 'ci_watcher',
    }
    with open(CI_LOG, 'a', encoding='utf-8') as f:
        f.write(json.dumps(entry) + '\n')


def _gh_get(path):
    """
    Authenticated GET request to GitHub API.
    Returns parsed JSON dict/list, or None on error.
    """
    url = f'https://api.github.com{path}'
    req = urllib.request.Request(url, headers={
        'Authorization':        f'Bearer {GITHUB_TOKEN}',
        'Accept':               'application/vnd.github+json',
        'X-GitHub-Api-Version': '2022-11-28',
        'User-Agent':           'KAM-Sentinel-BugWatcher/1.0',
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        _log_ci('SYSTEM', 'gh_api_error', f'HTTP {e.code} for {path}')
        return None
    except Exception as exc:
        _log_ci('SYSTEM', 'gh_api_error', str(exc))
        return None


def _gh_get_text(path):
    """
    Authenticated GET request that returns raw text (for job log downloads).
    GitHub redirects log URLs to a signed S3 URL — urllib follows automatically.
    """
    url = f'https://api.github.com{path}'
    req = urllib.request.Request(url, headers={
        'Authorization':        f'Bearer {GITHUB_TOKEN}',
        'Accept':               'application/vnd.github+json',
        'X-GitHub-Api-Version': '2022-11-28',
        'User-Agent':           'KAM-Sentinel-BugWatcher/1.0',
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read().decode('utf-8', errors='replace')
    except Exception as exc:
        _log_ci('SYSTEM', 'gh_log_error', str(exc))
        return None


def _fetch_failed_ci_runs(seen_run_ids):
    """
    Poll GitHub Actions API for recently failed workflow runs.
    Returns list of run dicts for runs not already in seen_run_ids.
    Updates seen_run_ids in-place.
    """
    data = _gh_get(f'/repos/{GITHUB_REPO}/actions/runs?status=failure&per_page=10')
    if not data:
        return []

    new_failures = []
    for run in data.get('workflow_runs', []):
        run_id = run.get('id')
        if run_id in seen_run_ids:
            continue
        seen_run_ids.add(run_id)
        new_failures.append({
            'run_id':   run_id,
            'run_name': run.get('name', '?'),
            'run_url':  run.get('html_url', ''),
            'head_sha': run.get('head_sha', ''),
            'branch':   run.get('head_branch', ''),
        })
    return new_failures


def _fetch_job_logs(run_id):
    """
    Fetch logs for all *failed* jobs in a workflow run.
    Returns concatenated log text, or '' if unavailable.
    """
    jobs_data = _gh_get(f'/repos/{GITHUB_REPO}/actions/runs/{run_id}/jobs')
    if not jobs_data:
        return ''

    all_logs = []
    for job in jobs_data.get('jobs', []):
        if job.get('conclusion') == 'failure':
            job_id   = job.get('id')
            job_name = job.get('name', '?')
            logs = _gh_get_text(f'/repos/{GITHUB_REPO}/actions/jobs/{job_id}/logs')
            if logs:
                all_logs.append(f'=== JOB: {job_name} ===\n{logs}')
    return '\n\n'.join(all_logs)


def _diagnose_ci_failure(logs_text):
    """
    Match CI log text against CI_KNOWN_ISSUES patterns.
    Returns (key, issue_dict) for the first match, or (None, None).
    """
    lower = logs_text.lower()
    for key, issue in CI_KNOWN_ISSUES.items():
        if any(p.lower() in lower for p in issue['patterns']):
            return key, issue
    return None, None


def ci_poll_cycle(seen_run_ids):
    """
    Poll GitHub Actions for failed runs, diagnose, and log.
    Returns list of run IDs processed.

    Auto-fix policy (conservative):
    - 'already_applied': fix is in codebase; log as regression warning, escalate.
    - 'log_and_escalate': no safe automated code change; log details for human review.
    All diagnosed failures are written to logs/ci_watcher.jsonl.
    """
    if not GITHUB_TOKEN:
        print('[BugWatcher/CI] GITHUB_TOKEN not set — skipping CI poll', flush=True)
        return []

    failures = _fetch_failed_ci_runs(seen_run_ids)
    if not failures:
        return []

    processed = []
    for run in failures:
        run_id   = run['run_id']
        run_name = run['run_name']
        run_url  = run['run_url']
        branch   = run['branch']

        print(f'[BugWatcher/CI] New failure detected: "{run_name}" on {branch} (run #{run_id})',
              flush=True)

        logs_text = _fetch_job_logs(run_id)
        if not logs_text:
            _log_ci(run_id, 'no_logs',
                    f'Could not fetch job logs for run #{run_id}. Review: {run_url}')
            print(f'[BugWatcher/CI] Could not fetch logs — logged for review: {run_url}',
                  flush=True)
            processed.append(run_id)
            continue

        key, issue = _diagnose_ci_failure(logs_text)

        if issue:
            fix_action = issue.get('fix', 'log_and_escalate')
            print(f'[BugWatcher/CI] Diagnosed: {key} ({fix_action})', flush=True)

            if fix_action == 'already_applied':
                # Known fix already in codebase — this is a regression
                _log_ci(run_id, 'regression_detected', json.dumps({
                    'issue_key':   key,
                    'description': issue['description'],
                    'fix_summary': issue['fix_summary'],
                    'run_url':     run_url,
                    'severity':    'high',
                }))
                print(f'[BugWatcher/CI] ⚠ REGRESSION: {key} — fix was already applied. '
                      f'Human review required. {run_url}', flush=True)

            else:  # log_and_escalate
                _log_ci(run_id, 'escalated', json.dumps({
                    'issue_key':   key,
                    'description': issue['description'],
                    'fix_summary': issue['fix_summary'],
                    'run_url':     run_url,
                }))
                print(f'[BugWatcher/CI] Escalated: {issue["fix_summary"]}', flush=True)

        else:
            # No pattern match — log raw summary for human review
            # Truncate logs to 2000 chars to keep log file sane
            snippet = logs_text[-2000:] if len(logs_text) > 2000 else logs_text
            _log_ci(run_id, 'undiagnosed', json.dumps({
                'run_url':     run_url,
                'log_snippet': snippet,
            }))
            print(f'[BugWatcher/CI] Undiagnosed failure — log snippet saved. Review: {run_url}',
                  flush=True)

        processed.append(run_id)

    return processed


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

    # Count today's CI events
    ci_diagnosed, ci_regressions, ci_undiagnosed = 0, 0, 0
    if os.path.exists(CI_LOG):
        with open(CI_LOG, encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                    if e.get('date', '').startswith(today):
                        a = e.get('action', '')
                        if a in ('diagnosed', 'escalated'):
                            ci_diagnosed += 1
                        elif a == 'regression_detected':
                            ci_regressions += 1
                        elif a == 'undiagnosed':
                            ci_undiagnosed += 1
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
        'date':           today,
        'fixed':          fixed,
        'escalated':      escalated,
        'still_open':     still_open,
        'tests_passing':  tests_passing,
        'ci': {
            'diagnosed':   ci_diagnosed,
            'regressions': ci_regressions,
            'undiagnosed': ci_undiagnosed,
        },
    }
    with open(out, 'w', encoding='utf-8') as f:
        json.dump(summary, f, indent=2)
    return summary


# ── Entry point ────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description='KAM Sentinel BugWatcher daemon')
    parser.add_argument('--once', action='store_true',
                        help='Run one local bug poll cycle then exit (for CI/testing)')
    parser.add_argument('--ci', action='store_true',
                        help='Run one CI poll cycle then exit')
    args = parser.parse_args()

    os.makedirs(BUGS_DIR, exist_ok=True)
    os.makedirs(DAILY_DIR, exist_ok=True)

    ci_enabled = bool(GITHUB_TOKEN)

    if args.ci:
        # Single CI poll and exit
        _log_ci('SYSTEM', 'started', 'CI poll (--ci mode)')
        seen_run_ids = set()
        processed = ci_poll_cycle(seen_run_ids)
        print(f'[BugWatcher/CI] Done — {len(processed)} run(s) processed.', flush=True)
        return

    _log('SYSTEM', 'started', 'BugWatcher daemon started')
    print(
        f'[BugWatcher] Started — local poll: {POLL_INTERVAL}s | '
        f'CI poll: {CI_POLL_INTERVAL}s | CI enabled: {ci_enabled} | --once: {args.once}',
        flush=True
    )

    seen_ids     = set()
    seen_run_ids = set()
    last_daily   = None
    last_ci_poll = 0.0  # epoch — forces an immediate first CI poll

    while True:
        # ── Local bug poll ────────────────────────────────────────────────────
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

        # ── CI poll (every CI_POLL_INTERVAL seconds) ──────────────────────────
        if ci_enabled and (time.time() - last_ci_poll) >= CI_POLL_INTERVAL:
            try:
                processed = ci_poll_cycle(seen_run_ids)
                if processed:
                    _log('SYSTEM', 'ci_cycle_complete',
                         f'CI runs processed: {processed}')
            except Exception as exc:
                _log_ci('SYSTEM', 'ci_poll_error', str(exc))
            last_ci_poll = time.time()

        # ── Daily summary at 23:55–23:59 ──────────────────────────────────────
        now   = datetime.datetime.now()
        today = datetime.date.today().isoformat()
        if now.hour == 23 and now.minute >= 55 and last_daily != today:
            try:
                s = daily_summary()
                last_daily = today
                _log('SYSTEM', 'daily_summary',
                     f'fixed={len(s["fixed"])} escalated={len(s["escalated"])} '
                     f'open={len(s["still_open"])} ci_regressions={s["ci"]["regressions"]}')
                print(f'[BugWatcher] Daily summary written → logs/bugwatcher_daily/{today}.json',
                      flush=True)
            except Exception as exc:
                _log('SYSTEM', 'daily_summary_error', str(exc))

        if args.once:
            break

        time.sleep(POLL_INTERVAL)


if __name__ == '__main__':
    main()
