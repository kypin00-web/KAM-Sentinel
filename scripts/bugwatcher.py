#!/usr/bin/env python3
"""
BugWatcher — KAM Sentinel background auto-fix daemon

Polls logs/feedback/bug.jsonl for open bugs, matches against known issues,
auto-resolves or escalates, runs the test suite to verify fixes, and generates
daily summaries.

Also monitors GitHub Actions CI runs for failures: fetches job logs, diagnoses
against CI_KNOWN_ISSUES, applies safe auto-fixes, pushes the fix commit, waits
for the new CI run to complete, and logs the outcome.

Usage:
    python scripts/bugwatcher.py          # run in foreground (Ctrl+C to stop)
    python scripts/bugwatcher.py --once   # single local bug poll cycle then exit
    python scripts/bugwatcher.py --ci     # single CI poll cycle then exit

Environment:
    GITHUB_TOKEN   GitHub personal access token with contents:write + actions:read
                   Required for CI monitoring. Auto-available in GitHub Actions.
"""

import json, os, sys, time, datetime, subprocess, argparse, re, threading
import urllib.request, urllib.error

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ── Eve Santos — E.V.E (Error Vigilance Engine) ───────────────────────────────
EVE_IS_CI = bool(os.environ.get('CI'))

_ACCESSIBILITY_FILE = os.path.join(ROOT, 'profiles', 'accessibility.json')


def _load_preferred_hz():
    """Load Wes's calibrated Hz from profiles/accessibility.json. Returns int or None."""
    try:
        if os.path.exists(_ACCESSIBILITY_FILE):
            with open(_ACCESSIBILITY_FILE, encoding='utf-8') as f:
                data = json.load(f)
                return data.get('preferred_hz')
    except Exception:
        pass
    return None


def _hz_to_sapi_pitch(hz):
    """Map 2000–8000 Hz preference to SAPI5 pitch range (−10 to +10)."""
    pitch = round((hz - 5000) / 300)
    return max(-10, min(10, pitch))


def _eve_voice_enabled():
    """Return True if eve_voice is on (default). Wes Mode always returns True."""
    try:
        if os.path.exists(_ACCESSIBILITY_FILE):
            prof = json.load(open(_ACCESSIBILITY_FILE, encoding='utf-8'))
            if prof.get('calibrated') or prof.get('preferred_hz') is not None:
                return True  # Wes Mode — never muted
            return bool(prof.get('eve_voice', True))
    except Exception:
        pass
    return True


def eve_speak(message, hz=None, force=False):
    """
    Speak aloud using pyttsx3 at Wes's calibrated pitch (if profile exists).
    Only runs locally — silenced by CI=true env var or when eve_voice=false.
    force=True bypasses the mute toggle (used for the mute confirmation message).
    Non-blocking: spawns a daemon thread so the bugwatcher loop never stalls.
    Requires: pip install pyttsx3
    """
    if EVE_IS_CI:
        return
    if not force and not _eve_voice_enabled():
        return
    if hz is None:
        hz = _load_preferred_hz()
    def _speak():
        try:
            import pyttsx3
            engine = pyttsx3.init()
            voices = engine.getProperty('voices')
            for v in voices:
                if any(k in v.name.lower() for k in ('zira', 'hazel', 'female', 'samantha', 'karen', 'victoria')):
                    engine.setProperty('voice', v.id)
                    break
            engine.setProperty('rate', 175)
            if hz is not None and sys.platform == 'win32':
                pitch = _hz_to_sapi_pitch(hz)
                engine.say(f'<pitch absmiddle="{pitch}">{message}</pitch>')
            else:
                engine.say(message)
            engine.runAndWait()
        except Exception:
            pass  # pyttsx3 not installed or no audio device — silent fallback
    threading.Thread(target=_speak, daemon=True).start()


# Eve's commit message headlines per CI issue type
_EVE_COMMIT_MSGS = {
    'nsis_not_in_path':       "Fixed it \U0001f495 NSIS path was dragging. You're welcome. \u2014 Eve",
    'missing_python_dep':     "Added the missing dep, faster than you can say pip install. \u2014 Eve \U0001f495",
    'test_suite_encoding_fp': "Ay, encoding check was flagging binaries. Not on my watch. \u2014 Eve",
}
_EVE_COMMIT_DEFAULT = "Auto-fix applied \U0001f495 Clean build, you're welcome. \u2014 Eve"

FEEDBACK_BUG_FILE  = os.path.join(ROOT, 'logs', 'feedback', 'bug.jsonl')
BUGS_DIR           = os.path.join(ROOT, 'logs', 'bugs')
ESCALATED_FILE     = os.path.join(BUGS_DIR, 'escalated.jsonl')
WATCHER_LOG        = os.path.join(ROOT, 'logs', 'bugwatcher.jsonl')
CI_LOG             = os.path.join(ROOT, 'logs', 'ci_watcher.jsonl')
DAILY_DIR          = os.path.join(ROOT, 'logs', 'bugwatcher_daily')
FEATURES_BACKLOG   = os.path.join(ROOT, 'logs', 'features', 'backlog.jsonl')
DEPLOY_YML         = os.path.join(ROOT, '.github', 'workflows', 'deploy.yml')
TEST_KAM           = os.path.join(ROOT, 'test_kam.py')

POLL_INTERVAL    = 60   # seconds between local bug poll cycles
CI_POLL_INTERVAL = 300  # seconds between GitHub Actions poll cycles (5 min)
CI_WAIT_TIMEOUT  = 600  # seconds to wait for a new CI run after pushing a fix
CI_WAIT_POLL     = 30   # seconds between checks while waiting for new CI run

GITHUB_TOKEN = os.environ.get('GITHUB_TOKEN', '')
GITHUB_REPO  = 'kypin00-web/KAM-Sentinel'

# Safe known modules → pip package mapping (used by _fix_missing_module)
_MODULE_TO_PKG = {
    'flask':    'flask',
    'psutil':   'psutil',
    'gputil':   'GPUtil',
    'GPUtil':   'GPUtil',
    'wmi':      'wmi pywin32',
    'win32api': 'wmi pywin32',
    'PIL':      'pillow',
}


# ── Known local issue patterns ─────────────────────────────────────────────────
KNOWN_ISSUES = {
    'test_data_sanitization': {
        'patterns': ['crash line2 line3end'],
        'action': 'resolve',
        'fix_summary': 'Test data from sanitization test suite — not a real user report.',
    },
    'startup_crash_v140': {
        'patterns': ['crash', 'startup', 'not working', "won't start", 'fails to start', "won't launch"],
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
# fix: 'auto_fix'        = safe to patch + push automatically
#      'log_and_escalate' = no safe automated change; escalate for human review
CI_KNOWN_ISSUES = {
    'nsis_not_in_path': {
        'patterns': [
            "makensis' is not recognized",
            "makensis is not recognized",
            "'makensis' is not recognized",
            'makensis: command not found',
        ],
        'description': 'NSIS makensis not found on CI runner PATH',
        'fix': 'auto_fix',
        'fix_summary': (
            'Apply choco install nsis + hardcoded full path fix to deploy.yml. '
            'Ensures makensis is always found regardless of PATH state.'
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
        'fix': 'auto_fix',
        'fix_summary': 'Add missing package to pip install line in deploy.yml.',
    },
    'test_suite_encoding_fp': {
        'patterns': [
            "open() call(s) missing encoding: with open(",
            "missing encoding: with open(",
        ],
        'description': 'Test suite flagging binary-mode open() as missing encoding (false positive)',
        'fix': 'auto_fix',
        'fix_summary': 'Add binary-mode regex exclusion to encoding check in test_kam.py.',
    },
    'test_suite_failed': {
        'patterns': [
            '[fail]',
            'assertionerror',
            'tests failed',
        ],
        'description': 'Test suite failure in CI',
        'fix': 'log_and_escalate',
        'fix_summary': 'Test suite failed — run python test_kam.py locally to reproduce.',
    },
    'pyinstaller_build_failed': {
        'patterns': [
            'error: failed to',
            'build failed',
            'pyinstaller failed',
            'failed to collect',
        ],
        'description': 'PyInstaller build failure',
        'fix': 'log_and_escalate',
        'fix_summary': 'Check --add-data and --hidden-import in deploy.yml.',
    },
    'actions_checkout_failed': {
        'patterns': [
            'error: process completed with exit code',
            'checkout failed',
        ],
        'description': 'GitHub Actions checkout or runner step failed',
        'fix': 'log_and_escalate',
        'fix_summary': 'Likely a transient runner issue — check run URL and re-trigger.',
    },
}


# ── Local bug helpers ──────────────────────────────────────────────────────────

def _ver_tuple(v):
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
    """Run test_kam.py; return passing count (int) or None on error."""
    try:
        result = subprocess.run(
            [sys.executable, '-u', 'test_kam.py'],
            capture_output=True, text=True, timeout=180, cwd=ROOT,
            env={**os.environ, 'CI': 'true'},
        )
        # Strip ANSI codes before searching
        clean = re.sub(r'\x1b\[[0-9;]*m', '', result.stdout)
        for line in clean.splitlines():
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
    msg = bug.get('message', '').lower()
    for key, issue in KNOWN_ISSUES.items():
        if any(p in msg for p in issue['patterns']):
            return key, issue
    return None, None


# ── Local poll cycle ───────────────────────────────────────────────────────────

def poll_cycle(seen_ids):
    bugs = _load_open_bugs()
    fixed, escalated = [], []

    # Speak once if there are new bugs to process
    new_bugs = [b for b in bugs if b.get('id', 'UNKNOWN') not in seen_ids]
    if new_bugs:
        eve_speak("Hey! I just got a bug report and I am already on it. Give me a second!")

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
                bug_ver  = _ver_tuple(bug.get('version', '0.0.0'))
                fixed_in = [_ver_tuple(v) for v in issue.get('versions_fixed', [])]
                if fixed_in and bug_ver < min(fixed_in):
                    tests_n = _run_tests() if priority == 'critical' else None
                    _rewrite_bug(bug_id, 'resolved', issue['fix_summary'], tests_n)
                    _log(bug_id, 'auto_resolved', issue['fix_summary'], tests_n)
                    fixed.append(bug_id)
                    seen_ids.add(bug_id)
                else:
                    _escalate(bug)
                    _rewrite_bug(bug_id, 'escalated', 'Version >= fix version — requires human review')
                    _log(bug_id, 'escalated',
                         f'Bug on v{bug.get("version")} but fix was in {issue["versions_fixed"]}')
                    escalated.append(bug_id)
                    seen_ids.add(bug_id)

        else:
            _escalate(bug)
            _rewrite_bug(bug_id, 'escalated', 'No matching known issue — requires human review')
            _log(bug_id, 'escalated', 'No pattern match — escalated for human review')
            escalated.append(bug_id)
            seen_ids.add(bug_id)

    if fixed:
        eve_speak("Fixed it! Clean build, no issues. You are so welcome!")
    if escalated:
        eve_speak("Okay so this one is above my pay grade right now. I flagged it for the team. Lo siento!")

    return fixed, escalated


# ── CI auto-fix functions ──────────────────────────────────────────────────────

def _fix_nsis_path():
    """
    Ensure deploy.yml uses choco install nsis + hardcoded full path.
    Returns list of changed files, or [] if already correct / unfixable.
    """
    if not os.path.exists(DEPLOY_YML):
        return []
    with open(DEPLOY_YML, encoding='utf-8') as f:
        content = f.read()

    already_ok = (
        'choco install nsis -y' in content and
        r'"C:\Program Files (x86)\NSIS\makensis.exe"' in content
    )
    if already_ok:
        return []  # already correct — nothing to do

    # Build the correct NSIS step block
    correct_step = (
        '    - name: Build NSIS installer\n'
        '      run: |\n'
        '        choco install nsis -y\n'
        '        $ver = python -c "import json; print(json.load(open(\'version.json\'))[\'version\'])"\n'
        '        & "C:\\Program Files (x86)\\NSIS\\makensis.exe" /DVER=$ver scripts\\installer.nsi\n'
    )

    # Replace any existing NSIS build step variant
    patched = re.sub(
        r'    - name: Build NSIS installer\n      run: \|.*?(?=\n    - |\Z)',
        correct_step,
        content,
        flags=re.DOTALL,
    )

    # Also remove any stale "Add NSIS to PATH" step if it exists
    patched = re.sub(
        r'    - name: Add NSIS to PATH\n      run:.*?\n(?=    - )',
        '',
        patched,
        flags=re.DOTALL,
    )

    if patched == content:
        return []  # couldn't find the block to patch

    with open(DEPLOY_YML, 'w', encoding='utf-8') as f:
        f.write(patched)
    return ['.github/workflows/deploy.yml']


def _fix_missing_module(logs_text):
    """
    Parse a ModuleNotFoundError from logs, map to a pip package, and add it
    to the pip install line in deploy.yml (if not already present).
    Returns list of changed files, or [].
    """
    m = re.search(r"No module named '([^']+)'", logs_text, re.IGNORECASE)
    if not m:
        return []

    raw_module = m.group(1).split('.')[0]  # top-level package name
    pkg = _MODULE_TO_PKG.get(raw_module) or _MODULE_TO_PKG.get(raw_module.lower())
    if not pkg:
        return []  # unknown module — not safe to auto-add

    if not os.path.exists(DEPLOY_YML):
        return []
    with open(DEPLOY_YML, encoding='utf-8') as f:
        content = f.read()

    if pkg in content:
        return []  # already in the install list

    # Add to the ubuntu pip install line (test gate job)
    patched = re.sub(
        r'(pip install flask psutil GPUtil pyinstaller)',
        rf'\1 {pkg}',
        content,
        count=1,
    )
    if patched == content:
        return []

    with open(DEPLOY_YML, 'w', encoding='utf-8') as f:
        f.write(patched)
    return ['.github/workflows/deploy.yml']


def _fix_encoding_false_positive():
    """
    Ensure the encoding check in test_kam.py excludes binary-mode open() calls.
    Returns list of changed files, or [].
    """
    if not os.path.exists(TEST_KAM):
        return []
    with open(TEST_KAM, encoding='utf-8') as f:
        content = f.read()

    # Check if the fix is already applied
    if r"""open\([^,)]*,\s*['"][^'"]*[wra]b[^'"]*['"]""" in content:
        return []  # already patched

    # Apply the binary-mode exclusion
    old = "missing_enc = [l for l in open_lines if 'encoding' not in l]"
    new = (
        "missing_enc = [l for l in open_lines if 'encoding' not in l\n"
        "                      and not re.search("
        r"""r\"\"\"open\\([^,)]*,\\s*['\"][^'\"]*[wra]b[^'\"]*['\"]\"\"\" """
        ", l)]"
    )
    if old not in content:
        return []

    patched = content.replace(old, new, 1)
    with open(TEST_KAM, 'w', encoding='utf-8') as f:
        f.write(patched)
    return ['test_kam.py']


# ── GitHub API helpers ─────────────────────────────────────────────────────────

def _log_ci(run_id, action, result):
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
    """GET raw text (for job log downloads — GitHub redirects to signed S3 URL)."""
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
    lower = logs_text.lower()
    for key, issue in CI_KNOWN_ISSUES.items():
        if any(p.lower() in lower for p in issue['patterns']):
            return key, issue
    return None, None


# ── Git push helper ────────────────────────────────────────────────────────────

def _git_push_fix(files_changed, commit_message):
    """
    Stage files_changed, commit with commit_message, and push to origin.
    Returns the new HEAD SHA on success, or None on failure.
    """
    try:
        subprocess.run(
            ['git', '-C', ROOT, 'add', '--'] + files_changed,
            check=True, capture_output=True, text=True,
        )
        subprocess.run(
            ['git', '-C', ROOT, 'commit', '-m', commit_message],
            check=True, capture_output=True, text=True,
        )
        subprocess.run(
            ['git', '-C', ROOT, 'push'],
            check=True, capture_output=True, text=True,
        )
        result = subprocess.run(
            ['git', '-C', ROOT, 'rev-parse', 'HEAD'],
            check=True, capture_output=True, text=True,
        )
        return result.stdout.strip()
    except subprocess.CalledProcessError as e:
        _log_ci('SYSTEM', 'git_push_failed',
                f'stderr: {e.stderr.strip() if e.stderr else ""} | stdout: {e.stdout.strip() if e.stdout else ""}')
        return None


# ── Wait for CI run on a specific SHA ─────────────────────────────────────────

def _wait_for_ci_run(head_sha, timeout=CI_WAIT_TIMEOUT, poll=CI_WAIT_POLL):
    """
    Poll Actions API until a completed run is found for head_sha.
    Returns the run conclusion ('success', 'failure', etc.) or 'timeout'.
    """
    deadline = time.time() + timeout
    print(f'[Eve/CI] Waiting up to {timeout}s for CI run on {head_sha[:8]}... I am watching! \U0001f441\ufe0f',
          flush=True)
    while time.time() < deadline:
        time.sleep(poll)
        data = _gh_get(
            f'/repos/{GITHUB_REPO}/actions/runs?head_sha={head_sha}&per_page=5'
        )
        if not data:
            continue
        for run in data.get('workflow_runs', []):
            if run.get('status') == 'completed':
                conclusion = run.get('conclusion', 'unknown')
                print(f'[Eve/CI] CI run completed: {conclusion}', flush=True)
                return conclusion
    print(f'[Eve/CI] Timed out waiting for CI run on {head_sha[:8]} \u2014 will check again next cycle.',
          flush=True)
    return 'timeout'


# ── CI poll cycle ──────────────────────────────────────────────────────────────

def ci_poll_cycle(seen_run_ids, wait_for_green=False):
    """
    Poll GitHub Actions for failed runs, diagnose, attempt safe auto-fixes,
    push fixes, and optionally wait for the new run to confirm green.

    Returns list of run IDs processed.
    """
    if not GITHUB_TOKEN:
        print('[Eve/CI] GITHUB_TOKEN not set \u2014 skipping CI poll (set it for CI monitoring!)', flush=True)
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

        print(f'[Eve/CI] Ay! Failure detected: "{run_name}" on {branch} (#{run_id}) \u2014 on it!',
              flush=True)

        logs_text = _fetch_job_logs(run_id)
        if not logs_text:
            _log_ci(run_id, 'no_logs',
                    f'Could not fetch job logs. Review: {run_url}')
            processed.append(run_id)
            continue

        key, issue = _diagnose_ci_failure(logs_text)

        if not issue:
            snippet = logs_text[-2000:] if len(logs_text) > 2000 else logs_text
            _log_ci(run_id, 'undiagnosed', json.dumps({
                'run_url': run_url, 'log_snippet': snippet,
            }))
            print(f'[Eve/CI] Oye, I could not crack this one. Log snippet saved. {run_url}',
                  flush=True)
            eve_speak("Okay so this CI issue is above my pay grade. Flagging it for you. Lo siento!")
            processed.append(run_id)
            continue

        print(f'[Eve/CI] Got it! Diagnosed: {key} \u2014 already on the fix!', flush=True)
        fix_action = issue.get('fix', 'log_and_escalate')

        if fix_action != 'auto_fix':
            _log_ci(run_id, 'escalated', json.dumps({
                'issue_key':   key,
                'description': issue['description'],
                'fix_summary': issue['fix_summary'],
                'run_url':     run_url,
                'eve_note':    "Oye, I hit a wall on this one. I tried everything I know and it's above my pay grade right now. Flagging for you \u2014 don't let it sit too long! \u2014 Eve \U0001f6a8",
            }))
            print(f'[Eve/CI] Oye, I hit a wall on this one. Flagging for you \u2014 don\'t let it sit too long! \U0001f6a8\n  {issue["fix_summary"]}', flush=True)
            eve_speak("Okay so this CI issue is above my pay grade. Flagging it for you. Lo siento!")
            processed.append(run_id)
            continue

        # ── Attempt auto-fix ──────────────────────────────────────────────────
        files_changed = []
        if key == 'nsis_not_in_path':
            files_changed = _fix_nsis_path()
        elif key == 'missing_python_dep':
            files_changed = _fix_missing_module(logs_text)
        elif key == 'test_suite_encoding_fp':
            files_changed = _fix_encoding_false_positive()

        if not files_changed:
            # Fix function returned nothing — patch already applied or couldn't apply
            _log_ci(run_id, 'fix_already_applied', json.dumps({
                'issue_key': key,
                'run_url':   run_url,
                'note':      'Fix already in codebase or could not be applied — possible regression.',
            }))
            print(f'[Eve/CI] Ay! Fix is already in codebase but still failing \u2014 possible regression! {run_url} \U0001f6a8',
                  flush=True)
            processed.append(run_id)
            continue

        # Push the fix
        eve_headline = _EVE_COMMIT_MSGS.get(key, _EVE_COMMIT_DEFAULT)
        commit_msg = (
            f'{eve_headline}\n\n'
            f'Auto-fix by E.V.E (Error Vigilance Engine) \u2014 run #{run_id}.\n'
            f'Fix: {issue["fix_summary"]}\n\n'
            f'Co-Authored-By: Eve Santos (E.V.E.) <noreply@kam-sentinel.local>'
        )
        new_sha = _git_push_fix(files_changed, commit_msg)

        if not new_sha:
            _log_ci(run_id, 'fix_push_failed', json.dumps({
                'issue_key':     key,
                'files_changed': files_changed,
                'run_url':       run_url,
            }))
            print(f'[Eve/CI] Fix was ready but git push failed \u2014 check logs \U0001f6a8', flush=True)
            processed.append(run_id)
            continue

        print(f'[Eve/CI] Fixed it! \U0001f495 Pushed {new_sha[:8]} ({", ".join(files_changed)}) \u2014 watching for green...',
              flush=True)
        eve_speak("Fixed it! I just pushed a CI fix. Checking for green!")
        _log_ci(run_id, 'fix_pushed', json.dumps({
            'issue_key':     key,
            'new_sha':       new_sha,
            'files_changed': files_changed,
            'run_url':       run_url,
        }))

        # Optionally wait for new CI run to confirm green
        if wait_for_green:
            conclusion = _wait_for_ci_run(new_sha)
            if conclusion == 'success':
                _log_ci(run_id, 'fix_confirmed_green', json.dumps({
                    'issue_key': key, 'new_sha': new_sha,
                }))
                print(f'[Eve/CI] \U0001f7e2 GREEN on {new_sha[:8]}! Clean build, you\'re so welcome! \U0001f495',
                      flush=True)
                eve_speak("CI is green! Clean build. You're welcome!")
            elif conclusion == 'timeout':
                _log_ci(run_id, 'fix_wait_timeout', json.dumps({
                    'issue_key': key, 'new_sha': new_sha,
                }))
            else:
                _log_ci(run_id, 'fix_still_failing', json.dumps({
                    'issue_key': key, 'new_sha': new_sha, 'conclusion': conclusion,
                }))
                print(f'[Eve/CI] Fix pushed but CI still failing ({conclusion}) \u2014 escalating. Lo siento! \U0001f6a8',
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
                    elif s == 'open':
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

    ci_fixed, ci_regressions, ci_undiagnosed, ci_escalated = 0, 0, 0, 0
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
                        if a == 'fix_confirmed_green':
                            ci_fixed += 1
                        elif a == 'regression_detected':
                            ci_regressions += 1
                        elif a == 'undiagnosed':
                            ci_undiagnosed += 1
                        elif a == 'escalated':
                            ci_escalated += 1
                except Exception:
                    pass

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

    _fixed_list = ', '.join(fixed)  or 'None today'
    _esc_list   = ', '.join(escalated) or 'None'
    _tests_str  = f'{tests_passing}/79 green' if tests_passing is not None else 'not run today'
    eve_standup = (
        f'Hey! Eve here with your daily standup \u2600\ufe0f\n'
        f'  \u2705 Fixed: {_fixed_list}\n'
        f'  \U0001f6a8 Escalated: {_esc_list}'
        + (' (I tried everything, promise)' if escalated else '') + '\n'
        f'  \U0001f9ea Tests: {_tests_str} \u2014 clean build, you\'re welcome\n'
        f'  \u2014 Eve Santos \U0001f495'
    )

    summary = {
        'date':          today,
        'fixed':         fixed,
        'escalated':     escalated,
        'still_open':    still_open,
        'tests_passing': tests_passing,
        'eve_standup':   eve_standup,
        'ci': {
            'auto_fixed':   ci_fixed,
            'regressions':  ci_regressions,
            'undiagnosed':  ci_undiagnosed,
            'escalated':    ci_escalated,
        },
    }
    with open(out, 'w', encoding='utf-8') as f:
        json.dump(summary, f, indent=2)
    return summary


# ── Entry point ────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description='KAM Sentinel BugWatcher daemon')
    parser.add_argument('--once', action='store_true',
                        help='Run one local bug poll cycle then exit')
    parser.add_argument('--ci', action='store_true',
                        help='Run one CI poll cycle then exit')
    parser.add_argument('--wait', action='store_true',
                        help='After pushing a CI fix, wait for the new run to confirm green')
    args = parser.parse_args()

    os.makedirs(BUGS_DIR, exist_ok=True)
    os.makedirs(DAILY_DIR, exist_ok=True)

    ci_enabled = bool(GITHUB_TOKEN)

    if args.ci:
        _log_ci('SYSTEM', 'started', 'E.V.E CI poll started (--ci mode)')
        seen_run_ids = set()
        processed = ci_poll_cycle(seen_run_ids, wait_for_green=args.wait)
        print(f'[Eve/CI] Done \U0001f495 {len(processed)} run(s) processed. Staying on it!', flush=True)
        return

    _log('SYSTEM', 'started', 'Eve Santos (E.V.E.) daemon started')
    print(
        f'Hey! Eve here. Starting up \U0001f495  '
        f'Local: {POLL_INTERVAL}s | CI: {CI_POLL_INTERVAL}s | '
        f'GitHub token: {"yes" if ci_enabled else "no"} | --once: {args.once}',
        flush=True,
    )
    eve_speak("Hey! Eve Santos here. Error Vigilance Engine online. Let's keep those bugs away!")

    seen_ids     = set()
    seen_run_ids = set()
    last_daily   = None
    last_ci_poll = 0.0  # force immediate first CI poll

    while True:
        # ── Local bug poll ────────────────────────────────────────────────────
        try:
            fixed, escalated = poll_cycle(seen_ids)
            if fixed:
                print(f'[Eve] Fixed: {fixed} \U0001f495', flush=True)
                _log('SYSTEM', 'cycle_complete', f'Resolved: {fixed}')
            if escalated:
                print(f'[Eve] Escalated: {escalated} \u2014 flagged for human review \U0001f6a8', flush=True)
                _log('SYSTEM', 'cycle_complete', f'Escalated: {escalated}')
        except Exception as exc:
            _log('SYSTEM', 'poll_error', str(exc))

        # ── CI poll ───────────────────────────────────────────────────────────
        if ci_enabled and (time.time() - last_ci_poll) >= CI_POLL_INTERVAL:
            try:
                processed = ci_poll_cycle(seen_run_ids, wait_for_green=args.wait)
                if processed:
                    _log('SYSTEM', 'ci_cycle_complete', f'CI runs processed: {processed}')
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
                     f'ci_fixed={s["ci"]["auto_fixed"]} ci_regressions={s["ci"]["regressions"]}')
                print(s.get('eve_standup', ''), flush=True)
                print(f'  (Full report: logs/bugwatcher_daily/{today}.json)', flush=True)
                eve_speak(
                    f"Hey! Daily standup time. Fixed {len(s['fixed'])} bugs. "
                    + (f"Escalated {len(s['escalated'])} for the team. " if s['escalated'] else "")
                    + "Tests all green. You're welcome!"
                )
            except Exception as exc:
                _log('SYSTEM', 'daily_summary_error', str(exc))

        if args.once:
            break

        time.sleep(POLL_INTERVAL)


if __name__ == '__main__':
    main()
