#!/usr/bin/env python3
import sys
import os
import re
from collections import defaultdict, Counter

ERR_PATTERNS = {
    # High-level framework errors
    'assertion_failed': re.compile(r'Assertion.*failed', re.I),
    'unexpected_exception': re.compile(r'Unexpected exception caught', re.I),
    'jsonrpc_exception': re.compile(r'JSONRPCException|JSONRPC error', re.I),
    'rpc_timeout': re.compile(r'took longer than .* seconds|TimeoutError', re.I),
    # Node-level errors in debug.log
    'bind_in_use': re.compile(r'Address already in use', re.I),
    'connectblock_failed': re.compile(r'ConnectBlock.*failed|ActivateBestChain failed', re.I),
    'acceptblock_failed': re.compile(r'AcceptBlock FAILED', re.I),
    'bad_reject': re.compile(r'bad-[a-z0-9_-]+', re.I),
    'policy_reject': re.compile(r'asset-[a-z0-9_-]+', re.I),
    'pow_mismatch': re.compile(r'bad-diffbits|high-hash|bad-adjusted-bits|time-too-old|time-too-new', re.I),
    'extapi_bind': re.compile(r'Could not bind the (API|validation API) pull socket', re.I),
}

def triage_test_dir(test_dir):
    result = Counter()
    details = defaultdict(list)

    tf = os.path.join(test_dir, 'test_framework.log')
    if os.path.isfile(tf):
        with open(tf, 'r', encoding='utf-8', errors='ignore') as f:
            log = f.read()
            for key, rx in ERR_PATTERNS.items():
                if rx.search(log):
                    result[key] += 1
                    details[key].append(('test_framework.log', None))

    # Scan node logs
    for entry in os.listdir(test_dir):
        if not entry.startswith('node'): continue
        npath = os.path.join(test_dir, entry, 'regtest', 'debug.log')
        if not os.path.isfile(npath): continue
        with open(npath, 'r', encoding='utf-8', errors='ignore') as f:
            for ln, line in enumerate(f, start=1):
                for key, rx in ERR_PATTERNS.items():
                    if rx.search(line):
                        result[key] += 1
                        details[key].append((npath, ln))

    return result, details

def main():
    if len(sys.argv) < 2:
        print('Usage: triage_functional_failures.py <run_dir>')
        sys.exit(2)
    run_dir = sys.argv[1]
    if not os.path.isdir(run_dir):
        print(f'Not a directory: {run_dir}')
        sys.exit(2)

    # Each subdir is a test dir
    summary = Counter()
    per_test = {}
    for name in sorted(os.listdir(run_dir)):
        tdir = os.path.join(run_dir, name)
        if not os.path.isdir(tdir):
            continue
        res, det = triage_test_dir(tdir)
        if res:
            summary.update(res)
            per_test[name] = (res, det)

    print('=== Failure Category Summary ===')
    for key, count in summary.most_common():
        print(f'{key:24s}: {count}')

    print('\n=== Top Offenders (first 10) ===')
    # Score tests by total matches
    ranked = sorted(per_test.items(), key=lambda kv: sum(kv[1][0].values()), reverse=True)[:10]
    for name, (res, _) in ranked:
        total = sum(res.values())
        cats = ', '.join([f'{k}:{v}' for k, v in res.most_common()])
        print(f'- {name}  (total={total})  [{cats}]')

    print('\nHint: pass a specific test dir to focus logs, e.g.:')
    print('  python3 scripts/tests/triage_functional_failures.py out/test_runner_XXX/feature_assets_basic_255')

if __name__ == '__main__':
    main()

