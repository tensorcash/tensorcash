#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""
PROVISIONAL entropy-gate audit / comparison tool.  NOT consensus code.

Goal: rigorously compare, over real proofs, the CURRENT entropy gate
    mean(chosen_probs) < ENTROPY_FILT (0.925)        # chosen_probs == id-CDF upper edge
against conservative realized-surprisal / prefix-cache metrics
    per-step bits = floor(-log2(bucket_mass_widened))   # mass = chosen-token prob
    prefix sums + expected cached-forward reuse.

It is model-free: per-step chosen-token mass is reconstructed purely from the
recorded fields (topk_logits + softmax_normalizers + sampling params), exactly
the data the quick-verifier already trusts.  No GPU, no model forward pass.

  python entropy_audit.py tests/pow_proof_test.bin
  python entropy_audit.py /path/to/proof_dir            # all *.bin recursively
  python entropy_audit.py /path/to/proof_dir --csv /tmp/entropy_rows.csv

Caveats (analysis only — float here is fine; the *consensus* scorer would use
the conservative-widened integer form):
  * topk_logits are the recorded "effective pre-temp" logits (top ~50 + 20
    probes).  We apply temperature then repetition penalty in the verifier's
    order, then mass = exp(eff_logit - logZ) with logZ = softmax_normalizers.
  * If the chosen token is outside the recorded top set, its exact logit is
    unavailable -> we give zero credit and flag it. That is conservative for
    a no-wire-change consensus gate.
"""
import argparse
import csv
import glob
import math
import os
import random
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, os.path.join(REPO, "shared-utils", "fb-schemas"))

import numpy as np
from proof.Proof import Proof
from proof.ValidationRequest import ValidationRequest
from proof.BlockValidation import BlockValidation

# --- consensus-ish constants (mirror config/constants.py) -------------------
ENTROPY_FILT = 0.925
ATOL = 1e-4            # existing replay tolerance; widen mass one-sided by 2*ATOL
WINDOW = 256
TOPK_PROBE_CAP = 50    # entries 0..49 of topk_logits are the genuine top set

CHECKPOINTS = (24, 48, 68, 100, 256)


def _load_proof_table(buf):
    """Return a Proof table from either a raw Proof or a ValidationRequest blob."""
    try:
        p = Proof.GetRootAs(buf, 0)
        if p.ChosenTokensLength() > 0 or p.TopkLogitsLength() > 0:
            return p
    except Exception:
        pass
    vr = ValidationRequest.GetRootAs(buf, 0)
    tbl = vr.Request()                       # union member table
    # RequestType: 1 = BlockValidation (wraps Proof in pow_blob), else raw Proof
    if vr.RequestType() == 1:
        bv = BlockValidation()
        bv.Init(tbl.Bytes, tbl.Pos)
        return bv.PowBlob()                  # -> Proof table
    p = Proof()
    p.Init(tbl.Bytes, tbl.Pos)
    return p


def extract(buf):
    p = _load_proof_table(buf)
    d = {
        "temperature": p.Temperature() or 1.0,
        "top_k": p.TopK(),
        "top_p": p.TopP(),
        "repetition_penalty": p.RepetitionPenalty() or 1.0,
        "chosen_tokens": p.ChosenTokensAsNumpy() if not p.ChosenTokensIsNone() else np.array([], np.uint32),
        "chosen_probs": p.ChosenProbsAsNumpy() if not p.ChosenProbsIsNone() else None,
        "sampling_u": p.SamplingUAsNumpy() if not p.SamplingUIsNone() else None,
        "softmax_normalizers": p.SoftmaxNormalizersAsNumpy() if not p.SoftmaxNormalizersIsNone() else None,
        "prompt_tokens": p.PromptTokensAsNumpy() if not p.PromptTokensIsNone() else np.array([], np.uint32),
    }
    n = p.TopkLogitsLength()
    tl, ti = [], []
    for j in range(n):
        fa, ua = p.TopkLogits(j), p.TopkIndices(j)
        tl.append(fa.ValuesAsNumpy() if fa and not fa.ValuesIsNone() else np.array([], np.float32))
        ti.append(ua.ValuesAsNumpy() if ua and not ua.ValuesIsNone() else np.array([], np.uint32))
    d["topk_logits"], d["topk_indices"] = tl, ti
    return d


def step_mass(d, i, context_set):
    """Reconstruct chosen-token bucket mass at step i from recorded fields.
    Returns (mass, in_topset). mass=None if chosen logit not recoverable."""
    logits = d["topk_logits"][i]
    idx = d["topk_indices"][i]
    if logits.size == 0:
        return None, False
    chosen = int(d["chosen_tokens"][i])
    logZ = float(d["softmax_normalizers"][i])
    T = float(d["temperature"])
    rp = float(d["repetition_penalty"])
    top_set = idx[:TOPK_PROBE_CAP]
    pos = np.where(top_set == chosen)[0]
    if pos.size == 0:
        return None, False                    # chosen outside recorded top set
    raw = float(logits[pos[0]])
    eff = raw / T
    if rp != 1.0 and chosen in context_set:
        eff = eff / rp
    mass = math.exp(eff - logZ)
    return min(max(mass, 0.0), 1.0), True


def analyse(d):
    cps = d["chosen_probs"]
    n = len(d["chosen_tokens"])
    if n == 0:
        return None
    prompt = list(d["prompt_tokens"].tolist())
    chosen = d["chosen_tokens"].tolist()

    real_bits = []        # -log2(mass): realized surprisal
    cred_bits = []        # proposed conservative integer credit
    flags = []            # chosen outside top set
    for i in range(n):
        ctx = (prompt + chosen[:i])[-WINDOW:]
        ctx_set = set(ctx)
        mass, in_top = step_mass(d, i, ctx_set)
        if mass is None or mass <= 0:
            # No-wire-change conservative fallback: if we cannot recover the
            # chosen mass from committed fields, do not credit entropy.
            rb = 0.0
            cb = 0
            flags.append(i)
        else:
            rb = -math.log2(mass) if mass > 0 else 32.0
            width_hi = min(1.0, mass + 2 * ATOL)
            cb = max(0, math.floor(-math.log2(width_hi)))
        real_bits.append(rb)
        cred_bits.append(cb)

    real_bits = np.array(real_bits)
    cred_bits = np.array(cred_bits)

    real_prefix = np.cumsum(real_bits)
    cred_prefix = np.cumsum(cred_bits)

    def _checkpoint(prefix, D):
        if len(prefix) == 0:
            return 0.0
        return float(prefix[min(D, len(prefix)) - 1])

    def _pow2_neg(bits):
        if bits > 1074:
            return 0.0
        return 2.0 ** (-bits)

    def _reuse(prefix, D):
        limit = min(D, len(prefix))
        return float(sum(_pow2_neg(float(prefix[j])) for j in range(limit)))

    out = {
        "n": n,
        "params": {k: d[k] for k in ("temperature", "top_k", "top_p", "repetition_penalty")},
        "old_mean_chosen_probs": (float(np.mean(cps)) if cps is not None and len(cps) else None),
        "old_gate_fail": (bool(np.mean(cps) >= ENTROPY_FILT) if cps is not None and len(cps) else None),
        "real_total_bits": float(real_bits.sum()),
        "real_mean_bits": float(real_bits.mean()),
        "cred_total_bits": float(cred_bits.sum()),
        "cred_mean_bits": float(cred_bits.mean()),
        "prefix": {},
        "reuse_cred": {},
        "reuse_real": {},
        "outside_topset": len(flags),
    }
    for D in CHECKPOINTS:
        real = _checkpoint(real_prefix, D)
        cred = _checkpoint(cred_prefix, D)
        out["prefix"][D] = {
            "real": real,
            "cred": cred,
            "survive_real": _pow2_neg(real),
            "survive_cred": _pow2_neg(cred),
        }
        out["reuse_cred"][D] = _reuse(cred_prefix, D)
        out["reuse_real"][D] = _reuse(real_prefix, D)
    out["_real_bits"] = real_bits
    out["_cred_bits"] = cred_bits
    out["_cps"] = cps
    return out


def fmt(path, r):
    print(f"\n=== {path}")
    if r is None:
        print("  (no chosen_tokens — synthetic/empty proof, nothing to score)")
        return
    p = r["params"]
    print(f"  steps={r['n']}  T={p['temperature']:.3f} top_k={p['top_k']} "
          f"top_p={p['top_p']:.3f} rep_pen={p['repetition_penalty']:.3f}  "
          f"chosen_outside_topset={r['outside_topset']}")
    old = r["old_mean_chosen_probs"]
    print(f"  OLD gate : mean(chosen_probs/CDF-upper) = "
          f"{old if old is None else round(old,4)}  -> "
          f"{'FAIL(>=0.925)' if r['old_gate_fail'] else 'pass' if old is not None else 'n/a (no chosen_probs)'}")
    print(f"  REAL     : total realized surprisal = {r['real_total_bits']:.1f} bits "
          f"({r['real_mean_bits']:.2f} bits/step)")
    print(f"  CREDIT   : total conservative credited = {r['cred_total_bits']:.1f} bits "
          f"({r['cred_mean_bits']:.2f} bits/step)")
    for D in CHECKPOINTS:
        pr = r["prefix"][D]
        print(f"             prefix[:{D:>3}] real={pr['real']:6.1f} "
              f"cred={pr['cred']:6.1f}  survive_cred=2^-{pr['cred']:.1f}  "
              f"E[reuse<={D}]={r['reuse_cred'][D]:5.2f} fwd")
    # show the disconnect the user cares about: high CDF-upper vs real entropy, first 12 steps
    rb, cb, cps = r["_real_bits"], r["_cred_bits"], r["_cps"]
    print("   step :  CDFupper  realbits  credbits")
    for i in range(min(12, r["n"])):
        cu = f"{cps[i]:.4f}" if cps is not None and i < len(cps) else "  --  "
        print(f"    {i:>3} :   {cu}    {rb[i]:6.2f}   {int(cb[i]):>4}")


def q(values, p):
    vals = sorted(v for v in values if v is not None and math.isfinite(v))
    if not vals:
        return None
    if len(vals) == 1:
        return vals[0]
    pos = (len(vals) - 1) * p
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return vals[lo]
    return vals[lo] * (hi - pos) + vals[hi] * (pos - lo)


def fnum(v, nd=2):
    if v is None:
        return "n/a"
    return f"{v:.{nd}f}"


def summarize(rows, errors):
    print(f"\n--- corpus summary: scored={len(rows)} errors={errors}")
    if not rows:
        return
    old_scored = [r for _, r in rows if r["old_mean_chosen_probs"] is not None]
    if old_scored:
        old_fails = sum(1 for r in old_scored if r["old_gate_fail"])
        x = np.array([r["old_mean_chosen_probs"] for r in old_scored])
        y = np.array([r["real_mean_bits"] for r in old_scored])
        corr = None
        if len(old_scored) >= 3 and x.std() > 0 and y.std() > 0:
            corr = float(np.corrcoef(x, y)[0, 1])
        print(f"old gate: n={len(old_scored)} fail={old_fails} "
              f"fail_rate={old_fails / len(old_scored):.3f} "
              f"corr(mean_cdf_upper, real_mean_bits)={fnum(corr, 3)}")

    def line(label, key):
        vals = [r[key] for _, r in rows]
        print(f"{label:24s} p01={fnum(q(vals, .01))} p05={fnum(q(vals, .05))} "
              f"p25={fnum(q(vals, .25))} med={fnum(q(vals, .50))} "
              f"p75={fnum(q(vals, .75))} p95={fnum(q(vals, .95))} "
              f"p99={fnum(q(vals, .99))}")

    line("real_total_bits", "real_total_bits")
    line("cred_total_bits", "cred_total_bits")
    line("real_mean_bits", "real_mean_bits")
    line("cred_mean_bits", "cred_mean_bits")
    line("outside_topset", "outside_topset")

    print("\ncheckpoint metrics (conservative credit):")
    print("D    prefix_bits p05/med/p95       Pr(full D survives) med        E[reuse<=D] p05/med/p95")
    for D in CHECKPOINTS:
        prefix = [r["prefix"][D]["cred"] for _, r in rows]
        reuse = [r["reuse_cred"][D] for _, r in rows]
        med_bits = q(prefix, .50)
        print(f"{D:<4d} {fnum(q(prefix, .05), 1)}/{fnum(med_bits, 1)}/{fnum(q(prefix, .95), 1)}"
              f"{'':8s}2^-{fnum(med_bits, 1):<8s}"
              f"{fnum(q(reuse, .05), 2)}/{fnum(q(reuse, .50), 2)}/{fnum(q(reuse, .95), 2)}")

    reuse_total = [r["reuse_cred"][256] for _, r in rows]
    print(f"\nexpected cached-forward shortcut over full path: "
          f"p05={fnum(q(reuse_total, .05))} med={fnum(q(reuse_total, .50))} "
          f"p95={fnum(q(reuse_total, .95))} forwards out of 256")


def flatten_row(path, r):
    row = {
        "path": path,
        "steps": r["n"],
        "temperature": r["params"]["temperature"],
        "top_k": r["params"]["top_k"],
        "top_p": r["params"]["top_p"],
        "repetition_penalty": r["params"]["repetition_penalty"],
        "old_mean_chosen_probs": r["old_mean_chosen_probs"],
        "old_gate_fail": r["old_gate_fail"],
        "real_total_bits": r["real_total_bits"],
        "real_mean_bits": r["real_mean_bits"],
        "cred_total_bits": r["cred_total_bits"],
        "cred_mean_bits": r["cred_mean_bits"],
        "outside_topset": r["outside_topset"],
    }
    for D in CHECKPOINTS:
        row[f"prefix{D}_real_bits"] = r["prefix"][D]["real"]
        row[f"prefix{D}_cred_bits"] = r["prefix"][D]["cred"]
        row[f"survive{D}_cred_prob"] = r["prefix"][D]["survive_cred"]
        row[f"reuse{D}_real_forwards"] = r["reuse_real"][D]
        row[f"reuse{D}_cred_forwards"] = r["reuse_cred"][D]
    return row


def main(argv):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("target", help="Proof blob or directory containing *.bin files")
    parser.add_argument("--csv", help="Write per-proof metrics to this CSV path")
    parser.add_argument("--limit", type=int, help="Limit number of files after sorting/shuffle")
    parser.add_argument("--shuffle-seed", type=int, help="Shuffle files with this deterministic seed")
    parser.add_argument("--verbose", action="store_true", help="Print per-proof details")
    args = parser.parse_args(argv[1:])

    target = args.target
    files = ([target] if os.path.isfile(target)
             else sorted(glob.glob(os.path.join(target, "**", "*.bin"), recursive=True)))
    if args.shuffle_seed is not None:
        rng = random.Random(args.shuffle_seed)
        rng.shuffle(files)
    if args.limit is not None:
        files = files[:args.limit]
    if not files:
        print(f"no proof files found at {target}"); return 1
    rows = []
    errors = 0
    for f in files:
        try:
            with open(f, "rb") as fh:
                r = analyse(extract(bytearray(fh.read())))
            if args.verbose:
                fmt(f, r)
            if r:
                rows.append((f, r))
        except Exception as e:
            errors += 1
            if args.verbose:
                print(f"\n=== {f}\n  ERROR: {e!r}")
    if args.csv and rows:
        flat = [flatten_row(f, r) for f, r in rows]
        with open(args.csv, "w", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(flat[0].keys()))
            writer.writeheader()
            writer.writerows(flat)
    summarize(rows, errors)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
