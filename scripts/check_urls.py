#!/usr/bin/env python3
"""
check_urls.py — KAM Sentinel live URL validator

Checks all public-facing URLs (GitHub Pages, releases, downloads) and
prints a GREEN/RED summary. Logs results to logs/url_checks.jsonl.

Usage:
    python scripts/check_urls.py
"""

import json, os, sys, time, datetime
import urllib.request, urllib.error

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG  = os.path.join(ROOT, 'logs', 'url_checks.jsonl')

GREEN  = '\033[92m'
RED    = '\033[91m'
YELLOW = '\033[93m'
RESET  = '\033[0m'
BOLD   = '\033[1m'

URLS = [
    {
        'label':    'GitHub Pages — landing page',
        'url':      'https://kypin00-web.github.io/KAM-Sentinel',
        'validate': None,
    },
    {
        'label':    'GitHub Pages — version.json',
        'url':      'https://kypin00-web.github.io/KAM-Sentinel/version.json',
        'validate': 'json',
    },
    {
        'label':    'GitHub Releases — latest page',
        'url':      'https://github.com/kypin00-web/KAM-Sentinel/releases/latest',
        'validate': None,
    },
    {
        'label':    'GitHub Releases — KAM_Sentinel_Setup.exe',
        'url':      'https://github.com/kypin00-web/KAM-Sentinel/releases/latest/download/KAM_Sentinel_Setup.exe',
        'validate': None,
    },
    {
        'label':    'GitHub Releases — KAM_Sentinel_Windows.exe',
        'url':      'https://github.com/kypin00-web/KAM-Sentinel/releases/latest/download/KAM_Sentinel_Windows.exe',
        'validate': None,
    },
    {
        'label':    'GitHub Releases — KAM_Sentinel_Mac',
        'url':      'https://github.com/kypin00-web/KAM-Sentinel/releases/latest/download/KAM_Sentinel_Mac',
        'validate': None,
    },
]


def check(url, validate=None, timeout=15):
    """
    HEAD the URL (fall back to GET if HEAD returns 405).
    If validate='json', read the body and parse as JSON.
    Returns dict: {status, code, error, json_valid, elapsed_ms}
    """
    start = time.time()
    try:
        req = urllib.request.Request(
            url,
            method='HEAD',
            headers={'User-Agent': 'KAM-Sentinel-URLCheck/1.0'},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            code = resp.getcode()
            body = None
    except urllib.error.HTTPError as e:
        if e.code == 405:
            # Server doesn't allow HEAD — fall back to GET
            req = urllib.request.Request(
                url,
                headers={'User-Agent': 'KAM-Sentinel-URLCheck/1.0'},
            )
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    code = resp.getcode()
                    body = resp.read(4096).decode('utf-8', errors='replace') if validate else None
            except urllib.error.HTTPError as e2:
                return {'status': 'error', 'code': e2.code, 'error': str(e2),
                        'json_valid': None, 'elapsed_ms': int((time.time()-start)*1000)}
        else:
            return {'status': 'error', 'code': e.code, 'error': str(e),
                    'json_valid': None, 'elapsed_ms': int((time.time()-start)*1000)}
    except Exception as exc:
        return {'status': 'error', 'code': None, 'error': str(exc),
                'json_valid': None, 'elapsed_ms': int((time.time()-start)*1000)}

    elapsed = int((time.time() - start) * 1000)

    # If we need JSON validation and only did HEAD, do a GET now
    if validate == 'json' and body is None:
        try:
            req2 = urllib.request.Request(
                url, headers={'User-Agent': 'KAM-Sentinel-URLCheck/1.0'}
            )
            with urllib.request.urlopen(req2, timeout=timeout) as resp2:
                body = resp2.read(32768).decode('utf-8', errors='replace')
        except Exception:
            body = None

    json_valid = None
    if validate == 'json':
        try:
            json.loads(body)
            json_valid = True
        except Exception:
            json_valid = False

    return {
        'status':     'ok' if code == 200 else 'error',
        'code':       code,
        'error':      None,
        'json_valid': json_valid,
        'elapsed_ms': elapsed,
    }


def _log_results(results):
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    entry = {
        'ts':      time.time(),
        'date':    datetime.datetime.now().isoformat(),
        'results': results,
    }
    with open(LOG, 'a', encoding='utf-8') as f:
        f.write(json.dumps(entry) + '\n')


def main():
    print(f'\n{BOLD}KAM Sentinel — Live URL Check{RESET}')
    print('=' * 56)

    results = []
    all_ok = True

    for item in URLS:
        label = item['label']
        url   = item['url']

        sys.stdout.write(f'  Checking {label}… ')
        sys.stdout.flush()

        r = check(url, validate=item.get('validate'))
        code = r['code']
        ok   = r['status'] == 'ok'
        if not ok:
            all_ok = False

        if ok:
            extra = ''
            if r['json_valid'] is True:
                extra = '  (JSON valid)'
            elif r['json_valid'] is False:
                extra = f'  {RED}(JSON INVALID){RESET}'
                all_ok = False
            print(f'{GREEN}[{code}] OK{RESET}  {r["elapsed_ms"]}ms{extra}')
        else:
            err = f'code={code}' if code else r.get('error', '?')
            print(f'{RED}[{code or "ERR"}] FAIL{RESET}  {err}')

        results.append({
            'label':      label,
            'url':        url,
            'code':       code,
            'ok':         ok,
            'json_valid': r['json_valid'],
            'elapsed_ms': r['elapsed_ms'],
            'error':      r['error'],
        })

    print('=' * 56)
    if all_ok:
        print(f'  {GREEN}{BOLD}All URLs returning 200 [OK]{RESET}')
    else:
        failed = [r for r in results if not r['ok']]
        print(f'  {RED}{BOLD}{len(failed)} URL(s) failed:{RESET}')
        for r in failed:
            print(f'    {RED}✗ [{r["code"] or "ERR"}]{RESET} {r["url"]}')
    print()

    _log_results(results)
    print(f'  Results logged → logs/url_checks.jsonl\n')

    return 0 if all_ok else 1


if __name__ == '__main__':
    sys.exit(main())
