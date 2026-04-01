# SPDX-License-Identifier: Apache-2.0
import os, sys, json, time, traceback
os.chdir('/app/tensorcash')                       # fix: config/correl.npy is cwd-relative
sys.path[:0] = ['/app/tensorcash', '/app']
import importlib.util
spec = importlib.util.spec_from_file_location('pv_patched', '/tmp/patched_proof_verifier.py')
mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)

orig = mod._prob_noise_bucket_decision
cap = {}
def wrap(*a, **k):
    r = orig(*a, **k); cap['last'] = r; return r
mod._prob_noise_bucket_decision = wrap

RESULTS = '/tmp/red_rerun_v2.jsonl'
def done():
    s = set()
    try:
        for line in open(RESULTS):
            try: s.add(json.loads(line)['hash'])
            except Exception: pass
    except FileNotFoundError: pass
    return s

red = json.load(open('/tmp/red_rerun_list.json'))
already = done()
todo = [(h, p) for (h, p) in red if h not in already][:int(os.environ.get('RERUN_CAP', '20'))]
print(f'[runner] {len(already)} done, processing {len(todo)}', flush=True)

v = mod.ProofVerifier()
out = open(RESULTS, 'a')
for i, (h, path) in enumerate(todo):
    cap.pop('last', None); t0 = time.time(); st = ''
    try:
        st = str(v.full_verify(open(path, 'rb').read()))
    except Exception as e:
        st = 'EXC:' + repr(e)[:110]
    dt = time.time() - t0
    b = cap.get('last') or {}
    rec = {'hash': h, 'status': st[:130], 'sec': round(dt, 1),
           'obs': b.get('obs_counts'), 'old_allowed': b.get('old_allowed'),
           'adaptive_allowed': b.get('adaptive_allowed'), 'loo_counts': b.get('loo_counts'),
           'legacy_valid': b.get('legacy_valid'), 'adaptive_valid': b.get('adaptive_valid'),
           'adaptive_available': b.get('adaptive_available')}
    out.write(json.dumps(rec) + '\n'); out.flush()
    print(f'[{i+1}/{len(todo)}] {h[:12]} {st[:30]} leg/adapt={b.get("legacy_valid")}/{b.get("adaptive_valid")} '
          f'obs={b.get("obs_counts")} adaptAllow={b.get("adaptive_allowed")} {dt:.0f}s', flush=True)
print('[runner] DONE', flush=True)
