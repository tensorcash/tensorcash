# SPDX-License-Identifier: Apache-2.0
import os, sys, json, time, traceback
os.environ.setdefault('OMP_NUM_THREADS', '4')      # avoid wide CPU fork/thread fans
os.chdir('/app/tensorcash')
sys.path[:0] = ['/app/tensorcash', '/app']
import torch
import importlib.util
spec = importlib.util.spec_from_file_location('pv_patched', '/tmp/patched_proof_verifier.py')
mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)

orig = mod._prob_noise_bucket_decision
cap = {}
def wrap(*a, **k):
    r = orig(*a, **k); cap['last'] = r; return r
mod._prob_noise_bucket_decision = wrap

def gpu_free_gb():
    try:
        f, _ = torch.cuda.mem_get_info(); return f / 2**30
    except Exception:
        return -1.0

RESULTS = '/tmp/red_rerun_gpu.jsonl'
def done():
    s = set()
    try:
        for l in open(RESULTS):
            try: s.add(json.loads(l)['hash'])
            except Exception: pass
    except FileNotFoundError: pass
    return s

red = json.load(open('/tmp/red_rerun_list.json'))
todo = [(h, p) for (h, p) in red if h not in done()][:int(os.environ.get('RERUN_CAP', '5'))]
print(f'[gpu] start free={gpu_free_gb():.1f}G processing {len(todo)}', flush=True)

v = mod.ProofVerifier()
out = open(RESULTS, 'a')
ABORT_FREE = float(os.environ.get('ABORT_FREE_GB', '10'))
for i, (h, path) in enumerate(todo):
    fg = gpu_free_gb()
    if 0 <= fg < ABORT_FREE:
        print(f'[gpu] ABORT: free={fg:.1f}G < {ABORT_FREE}G, stopping to protect live verifier', flush=True)
        break
    cap.pop('last', None); t0 = time.time(); st = ''
    try:
        st = str(v.full_verify(open(path, 'rb').read()))
    except Exception as e:
        st = 'EXC:' + repr(e)[:110]
    dt = time.time() - t0
    b = cap.get('last') or {}
    # assert we are actually on GPU (don't silently grind CPU)
    dev = str(getattr(getattr(v, 'model', None), 'device', '?')) if hasattr(v, 'model') else '?'
    rec = {'hash': h, 'status': st[:130], 'sec': round(dt, 1), 'gpu_free_gb': round(fg, 1), 'model_dev': dev,
           'obs': b.get('obs_counts'), 'old_allowed': b.get('old_allowed'),
           'adaptive_allowed': b.get('adaptive_allowed'), 'loo_counts': b.get('loo_counts'),
           'legacy_valid': b.get('legacy_valid'), 'adaptive_valid': b.get('adaptive_valid')}
    out.write(json.dumps(rec) + '\n'); out.flush()
    print(f'[{i+1}/{len(todo)}] {h[:12]} dev={dev} {st[:26]} leg/adapt={b.get("legacy_valid")}/{b.get("adaptive_valid")} '
          f'obs={b.get("obs_counts")} {dt:.0f}s freeAfter={gpu_free_gb():.1f}G', flush=True)
print('[gpu] DONE', flush=True)
