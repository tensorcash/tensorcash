#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""
v1/v2 full-verification equivalence test on a real ValidationRequest fixture.

This test is intended for pre-production validation. It compares:
1) Final status returned by v1 and v2.
2) Final failure class/message family (ignoring numeric suffix noise).
3) Shape parity of internal vectors passed into _validate_final_results.

Optional strict metric drift thresholds can be enabled via env vars.
"""

from __future__ import annotations

import os
import sys
import time
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Tuple

import torch


# Make local src importable when run from repository root.
THIS_FILE = Path(__file__).resolve()
SRC_DIR = THIS_FILE.parent.parent
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

import pfunpack  # noqa: E402
from proof_verifier import (  # noqa: E402
    ProofVerifier,
    mca_debug_reset,
    mca_debug_snapshot,
    mca_get_params,
    mca_set_enabled,
    mca_set_params,
)


@dataclass
class RunResult:
    status: str
    message: str
    elapsed_s: float


def _canonical_reason(message: str) -> str:
    msg = str(message or "").strip()
    if not msg:
        return ""

    if msg.startswith("All test passed"):
        return "All test passed"
    if msg.startswith("Grid snap failed"):
        return "Grid snap failed"
    if msg.startswith("P values for mahanolobis distance are too far for tolerance"):
        return "P values for mahanolobis distance are too far for tolerance"
    if msg.startswith("P values for rank is severly above tolerance"):
        return "P values for rank is severly above tolerance"
    if msg.startswith("Prob noise too big"):
        return "Prob noise too big"
    if msg.startswith("Platform shift too big"):
        return "Platform shift too big"
    if msg.startswith("Mahanolobis distance quantiles are too close for comfort"):
        return "Mahanolobis distance quantiles are too close for comfort"
    if msg.startswith("Rank swaps are too high for a full pass"):
        return "Rank swaps are too high for a full pass"

    # Fallback strips numeric suffixes like ", noise mean 3.890e-05".
    return msg.split(",", 1)[0].strip()


def _to_float_list(values) -> List[float]:
    out: List[float] = []
    for v in values:
        if isinstance(v, torch.Tensor):
            if v.numel() == 1:
                out.append(float(v.detach().cpu().item()))
            else:
                # Should not happen for these lists, but keep deterministic.
                out.extend(float(x) for x in v.detach().cpu().flatten().tolist())
        else:
            out.append(float(v))
    return out


def _to_bool_list(values) -> List[bool]:
    out: List[bool] = []
    for v in values:
        if isinstance(v, torch.Tensor):
            if v.numel() == 1:
                out.append(bool(v.detach().cpu().item()))
            else:
                out.extend(bool(x) for x in v.detach().cpu().flatten().tolist())
        else:
            out.append(bool(v))
    return out


def _quantile(values: List[float], q: float) -> float:
    if not values:
        return 0.0
    if len(values) == 1:
        return float(values[0])
    t = torch.tensor(values, dtype=torch.float64)
    return float(torch.quantile(t, q).item())


def _abs_diff_stats(a: List[float], b: List[float]) -> Dict[str, float]:
    n = min(len(a), len(b))
    if n == 0:
        return {"n": 0, "mean": 0.0, "p50": 0.0, "p95": 0.0, "p99": 0.0, "max": 0.0}
    diffs = [abs(float(a[i]) - float(b[i])) for i in range(n)]
    return {
        "n": float(n),
        "mean": float(sum(diffs) / n),
        "p50": _quantile(diffs, 0.50),
        "p95": _quantile(diffs, 0.95),
        "p99": _quantile(diffs, 0.99),
        "max": float(max(diffs)),
    }


def _rel_diff_stats(a: List[float], b: List[float], eps: float = 1e-12) -> Dict[str, float]:
    n = min(len(a), len(b))
    if n == 0:
        return {"n": 0, "mean": 0.0, "p50": 0.0, "p95": 0.0, "p99": 0.0, "max": 0.0}
    diffs = []
    for i in range(n):
        va = float(a[i])
        vb = float(b[i])
        denom = max(abs(va), abs(vb), eps)
        diffs.append(abs(va - vb) / denom)
    return {
        "n": float(n),
        "mean": float(sum(diffs) / n),
        "p50": _quantile(diffs, 0.50),
        "p95": _quantile(diffs, 0.95),
        "p99": _quantile(diffs, 0.99),
        "max": float(max(diffs)),
    }


def _bool_disagreement(a: List[bool], b: List[bool]) -> Dict[str, float]:
    n = min(len(a), len(b))
    if n == 0:
        return {"n": 0.0, "mismatch_count": 0.0, "mismatch_rate": 0.0}
    mism = 0
    for i in range(n):
        if bool(a[i]) != bool(b[i]):
            mism += 1
    return {"n": float(n), "mismatch_count": float(mism), "mismatch_rate": float(mism / n)}


def _ks_statistic(a: List[float], b: List[float]) -> float:
    if not a or not b:
        return 0.0
    sa = sorted(float(x) for x in a)
    sb = sorted(float(x) for x in b)
    n = len(sa)
    m = len(sb)
    i = 0
    j = 0
    d = 0.0
    while i < n or j < m:
        va = sa[i] if i < n else float("inf")
        vb = sb[j] if j < m else float("inf")
        x = va if va <= vb else vb
        while i < n and sa[i] <= x:
            i += 1
        while j < m and sb[j] <= x:
            j += 1
        fa = i / n
        fb = j / m
        d = max(d, abs(fa - fb))
    return float(d)


def _value_stats(values: List[float]) -> Dict[str, float]:
    if not values:
        return {"n": 0.0, "mean": 0.0, "p50": 0.0, "p95": 0.0, "p99": 0.0, "min": 0.0, "max": 0.0}
    vals = [float(x) for x in values]
    return {
        "n": float(len(vals)),
        "mean": float(sum(vals) / len(vals)),
        "p50": _quantile(vals, 0.50),
        "p95": _quantile(vals, 0.95),
        "p99": _quantile(vals, 0.99),
        "min": float(min(vals)),
        "max": float(max(vals)),
    }


def _pearson_r(a: List[float], b: List[float], eps: float = 1e-12) -> float:
    n = min(len(a), len(b))
    if n < 2:
        return 1.0 if n == 1 else 0.0
    xa = torch.tensor([float(a[i]) for i in range(n)], dtype=torch.float64)
    xb = torch.tensor([float(b[i]) for i in range(n)], dtype=torch.float64)
    xa = xa - xa.mean()
    xb = xb - xb.mean()
    denom = torch.sqrt((xa * xa).sum()) * torch.sqrt((xb * xb).sum())
    if float(denom.item()) <= eps:
        return 1.0 if torch.allclose(xa, xb, atol=1e-12, rtol=0.0) else 0.0
    return float(((xa * xb).sum() / denom).item())


def _sign_agreement(a: List[float], b: List[float], eps: float = 0.0) -> Dict[str, float]:
    n = min(len(a), len(b))
    if n == 0:
        return {"n": 0.0, "agree_count": 0.0, "agree_rate": 0.0}
    agree = 0
    for i in range(n):
        va = float(a[i])
        vb = float(b[i])
        sa = 0 if abs(va) <= eps else (1 if va > 0 else -1)
        sb = 0 if abs(vb) <= eps else (1 if vb > 0 else -1)
        if sa == sb:
            agree += 1
    return {"n": float(n), "agree_count": float(agree), "agree_rate": float(agree / n)}


def _kendall_tau_distance(order_a: List[str], order_b: List[str]) -> float:
    n = min(len(order_a), len(order_b))
    if n < 2:
        return 0.0
    pos_b = {order_b[i]: i for i in range(n)}
    inv = 0
    total = 0
    for i in range(n):
        for j in range(i + 1, n):
            total += 1
            ai = order_a[i]
            aj = order_a[j]
            if pos_b[ai] > pos_b[aj]:
                inv += 1
    return float(inv / total) if total else 0.0


def _candidate_store(cand_labels: List[str]) -> Dict[str, Dict[str, List[Any]]]:
    out: Dict[str, Dict[str, List[Any]]] = {}
    for lbl in cand_labels:
        out[lbl] = {
            "p_value": [],
            "ulp_fail": [],
            "grid_fail": [],
            "R_obs": [],
            "sampling_noise": [],
            "delta_mu": [],
            "T_obs": [],
        }
    return out


def _get_env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return int(raw)


def _get_env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return float(raw)


def _get_env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return bool(default)
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _get_env_batches(name: str, default: List[int]) -> List[int]:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return [int(x.strip()) for x in raw.split(",") if x.strip()]


def _get_env_seed_list(name: str, default: List[int]) -> List[int]:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    out: List[int] = []
    for part in raw.replace(";", ",").split(","):
        p = part.strip()
        if not p:
            continue
        out.append(int(p))
    return out or default


def _mca_snapshot_to_ints(raw: Dict[str, Any]) -> Dict[str, int]:
    keys = [
        "sdpa_calls",
        "sdpa_noised",
        "attn_hook_noised",
        "linear_noised",
        "hook_count",
    ]
    out: Dict[str, int] = {}
    for k in keys:
        try:
            out[k] = int(raw.get(k, 0))
        except Exception:
            out[k] = 0
    return out


def _mca_snapshot_delta(before: Dict[str, int], after: Dict[str, int]) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for k in sorted(set(before.keys()) | set(after.keys())):
        out[k] = int(after.get(k, 0)) - int(before.get(k, 0))
    out["any_noise_events"] = int(max(out.get("sdpa_noised", 0), 0) + max(out.get("attn_hook_noised", 0), 0))
    return out


def run_v1_v2_equivalence(validation_request_path: Path, *, seed_override: int | None = None) -> Dict[str, object]:
    req = validation_request_path.read_bytes()
    unpacked = pfunpack.unpack_validation_request(req)
    pow_blob = unpacked["request"]["pow_blob"]

    bootstrap = _get_env_int("POW_EQ_BOOTSTRAP", 15_000)
    window_size = _get_env_int("POW_EQ_WINDOW_SIZE", 256)
    step_block = _get_env_int("POW_EQ_STEP_BLOCK", 64)
    bootstrap_block = _get_env_int("POW_EQ_BOOTSTRAP_BLOCK", 4096)
    batch_candidates = _get_env_batches("POW_EQ_BATCH_CANDIDATES", [2, 5, 10, 20])
    seed = int(seed_override if seed_override is not None else _get_env_int("POW_EQ_SEED", 1337))
    shared_forward = _get_env_bool("POW_EQ_SHARED_FORWARD", False)
    shared_forward_strict = _get_env_bool("POW_EQ_SHARED_FORWARD_STRICT", False)
    keyed_noise = _get_env_bool("POW_EQ_KEYED_NOISE", False)
    keyed_noise_seed = _get_env_int("POW_EQ_KEYED_NOISE_SEED", seed)
    shared_gauss = _get_env_bool("POW_EQ_SHARED_GAUSS", False)
    shared_gauss_seed = _get_env_int("POW_EQ_SHARED_GAUSS_SEED", seed)
    shared_gauss_strict = _get_env_bool("POW_EQ_SHARED_GAUSS_STRICT", False)
    strict_metrics = os.getenv("POW_EQ_STRICT_METRICS", "0").lower() in {"1", "true", "yes"}
    strict_intermediate = os.getenv("POW_EQ_STRICT_INTERMEDIATE", "0").lower() in {"1", "true", "yes"}
    allow_mismatch = os.getenv("POW_EQ_ALLOW_MISMATCH", "0").lower() in {"1", "true", "yes"}
    max_p99_diff = _get_env_float("POW_EQ_MAX_P99_PVALUE_DIFF", 0.25)
    max_noise_mean_diff = _get_env_float("POW_EQ_MAX_MEAN_NOISE_DIFF", 0.02)
    max_dmu_mean_diff = _get_env_float("POW_EQ_MAX_MEAN_DMU_DIFF", 0.005)
    max_ulp_disagree = _get_env_float("POW_EQ_MAX_ULP_FAIL_DISAGREE", 0.05)
    max_grid_disagree = _get_env_float("POW_EQ_MAX_GRID_FAIL_DISAGREE", 0.0)
    max_best_cand_disagree = _get_env_float("POW_EQ_MAX_BEST_CANDIDATE_DISAGREE", 0.1)
    max_cov_rel_p95 = _get_env_float("POW_EQ_MAX_COV_REL_P95", 0.5)

    # Match live worker context defaults.
    mca_set_enabled(True)
    mca_set_params(k_lin=1.5, k_attn=8.0, target_dtype=torch.float16)
    torch.manual_seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(seed)

    verifier = ProofVerifier()
    verifier.initialise(pow_blob)
    # Force model materialization once, then mirror runtime candidate coercion.
    verifier.reload(pow_blob)
    verifier._eq_keyed_noise = {
        "enabled": bool(keyed_noise),
        "seed": int(keyed_noise_seed),
    }
    verifier._eq_current_candidate = ""
    model_device = next(verifier.model.parameters()).device
    require_cuda = os.getenv("POW_EQ_REQUIRE_CUDA", "0").lower() in {"1", "true", "yes"}
    if require_cuda and model_device.type != "cuda":
        raise AssertionError(
            f"Equivalence run requires CUDA but model is on {model_device}. "
            "Set POW_EQ_REQUIRE_CUDA=0 to allow CPU fallback."
        )
    if model_device == torch.device("cpu"):
        # Mirror verifier runtime behavior to keep trace expectations aligned.
        batch_candidates = [2]

    cand_order = [1] + batch_candidates + ["spda"]
    cand_labels = [str(x) for x in cand_order]
    runtime_mca_noise = float(mca_get_params().get("k_attn", 8.0))
    if model_device == torch.device("cpu"):
        runtime_mca_noise = 12.0

    orig_compute_all_logits = verifier._compute_all_logits_parallel_prefix
    shared_forward_logits: Dict[Any, torch.Tensor] = {}

    if shared_forward:
        shared_forward_logits[1] = orig_compute_all_logits(
            window_size, batch_size=1, flash=False, as_tensor=True
        ).detach().clone()
        for bs in batch_candidates:
            shared_forward_logits[int(bs)] = orig_compute_all_logits(
                window_size,
                batch_size=int(bs),
                flash=False,
                mca_noise_value=runtime_mca_noise,
                as_tensor=True,
            ).detach().clone()
        shared_forward_logits["spda"] = orig_compute_all_logits(
            window_size,
            batch_size=1,
            flash=False,
            mca_noise_value=runtime_mca_noise,
            as_tensor=True,
        ).detach().clone()

        def shared_compute_all_logits_parallel_prefix(
            window_size_arg: int,
            batch_size: int = 1,
            flash: bool = False,
            enable_math: bool | None = None,
            enable_mem: bool | None = None,
            mca_noise_value: float | None = None,
            as_tensor: bool = False,
            compact_rows: bool = False,
        ):
            if int(window_size_arg) != int(window_size):
                if shared_forward_strict:
                    raise AssertionError(
                        f"Shared-forward expected window_size={window_size}, got {window_size_arg}"
                    )
                return orig_compute_all_logits(
                    window_size_arg,
                    batch_size=batch_size,
                    flash=flash,
                    enable_math=enable_math,
                    enable_mem=enable_mem,
                    mca_noise_value=mca_noise_value,
                    as_tensor=as_tensor,
                )

            key: Any
            if mca_noise_value is None and int(batch_size) == 1:
                key = 1
            elif mca_noise_value is not None and int(batch_size) == 1:
                key = "spda"
            elif mca_noise_value is not None:
                key = int(batch_size)
            else:
                key = int(batch_size)

            if key not in shared_forward_logits:
                if shared_forward_strict:
                    raise AssertionError(
                        "Shared-forward missing key "
                        f"{key} for call (batch_size={batch_size}, mca_noise_value={mca_noise_value})"
                    )
                return orig_compute_all_logits(
                    window_size_arg,
                    batch_size=batch_size,
                    flash=flash,
                    enable_math=enable_math,
                    enable_mem=enable_mem,
                    mca_noise_value=mca_noise_value,
                    as_tensor=as_tensor,
                )

            src = shared_forward_logits[key]
            if as_tensor:
                # Mirror the real compact_rows path: keep only rows 0 and -1 from
                # the SAME shared forward so v2 (full) and v2_streamed (compact)
                # derive byte-identical row0/row-1 from one forward.
                if compact_rows and src.size(1) > 2:
                    return src[:, [0, src.size(1) - 1], :].clone()
                return src.clone()

            out: Dict[int, torch.Tensor] = {}
            for i in range(int(window_size)):
                out[i] = src[i].clone()
            return out

        verifier._compute_all_logits_parallel_prefix = shared_compute_all_logits_parallel_prefix

    original_validate = verifier._validate_final_results
    active = {"name": None}
    captured_final: Dict[str, Dict[str, List[float] | int]] = {}
    v1_candidates = _candidate_store(cand_labels)
    v2_candidates = _candidate_store(cand_labels)
    holders: Dict[str, Any] = {
        "v1_cov_adjuster": [],
        "v2_cov_adjuster": [],
        "v2_cov_adjuster_from_calib": [],
        "v2_rank_error_calib": [],
        "v2_delta_mu_calib": [],
    }
    orig_cached_gauss = verifier._cached_gauss
    shared_gauss_shapes: List[Tuple[int, int]] = []
    shared_gauss_state: Dict[str, Any] = {
        "enabled": bool(shared_gauss),
        "seed": int(shared_gauss_seed),
        "strict": bool(shared_gauss_strict),
        "v1_calls": 0,
        "v2_calls": 0,
        "v2_extra_calls": 0,
        "shape_mismatch_calls": 0,
        "first_shape_mismatch": None,
    }

    if shared_gauss:
        def shared_cached_gauss(dim: int, B: int):
            version = str(active.get("name") or "")
            model_device = next(verifier.model.parameters()).device
            dim_i = int(dim)
            b_i = int(B)

            if version == "v1":
                call_idx = len(shared_gauss_shapes)
                shared_gauss_shapes.append((dim_i, b_i))
                shared_gauss_state["v1_calls"] += 1
            elif version == "v2":
                call_idx = int(shared_gauss_state["v2_calls"])
                if call_idx < len(shared_gauss_shapes):
                    rec_dim, rec_b = shared_gauss_shapes[call_idx]
                    if rec_dim != dim_i or rec_b != b_i:
                        shared_gauss_state["shape_mismatch_calls"] += 1
                        if shared_gauss_state["first_shape_mismatch"] is None:
                            shared_gauss_state["first_shape_mismatch"] = {
                                "call_index": int(call_idx),
                                "v1_shape": [int(rec_b), int(rec_dim)],
                                "v2_shape": [int(b_i), int(dim_i)],
                            }
                        if shared_gauss_strict:
                            raise AssertionError(
                                "Shared-gauss shape mismatch at call "
                                f"{call_idx}: v1=({rec_b},{rec_dim}) v2=({b_i},{dim_i})"
                            )
                else:
                    shared_gauss_state["v2_extra_calls"] += 1
                shared_gauss_state["v2_calls"] += 1
            else:
                # Calls outside v1/v2 windows keep original behavior.
                return orig_cached_gauss(dim, B)

            # Deterministic per-call seed shared across v1/v2 by call index.
            call_seed = int(shared_gauss_seed + call_idx)
            gen = torch.Generator(device=model_device)
            gen.manual_seed(call_seed)
            return torch.randn((b_i, dim_i), device=model_device, generator=gen)

        verifier._cached_gauss = shared_cached_gauss

    def capture_validate(
        p_values,
        rank_error_actual,
        rank_error_calib,
        all_sampling_noise,
        all_delta_mu,
        delta_mu_calib,
        *,
        charting=False,
        ref_sampling_noise=None,
        delta_raw=None,
        candidate_sampling_noise=None,
        candidate_labels=None,
    ):
        name = str(active["name"])
        captured_final[name] = {
            "p_values": _to_float_list(p_values),
            "rank_error_actual": _to_float_list(rank_error_actual),
            "rank_error_calib": _to_float_list(rank_error_calib),
            "sampling_noise": _to_float_list(all_sampling_noise),
            "delta_mu": _to_float_list(all_delta_mu),
            "delta_mu_calib": _to_float_list(delta_mu_calib),
            "steps": int(len(p_values)),
        }
        return original_validate(
            p_values=p_values,
            rank_error_actual=rank_error_actual,
            rank_error_calib=rank_error_calib,
            all_sampling_noise=all_sampling_noise,
            all_delta_mu=all_delta_mu,
            delta_mu_calib=delta_mu_calib,
            charting=charting,
            ref_sampling_noise=ref_sampling_noise,
            delta_raw=delta_raw,
            candidate_sampling_noise=candidate_sampling_noise,
            candidate_labels=candidate_labels,
        )

    verifier._validate_final_results = capture_validate

    def run_once(version: str) -> RunResult:
        if not shared_forward:
            verifier.reload(pow_blob)
        active["name"] = version
        mca_debug_reset()
        params_before = mca_get_params()
        dbg_before = _mca_snapshot_to_ints(mca_debug_snapshot())
        t0 = time.perf_counter()
        status = "UNKNOWN"
        message = ""
        if version == "v1":
            orig_step = verifier._verify_step_from_logits
            state = {"step": 0, "slot": 0, "base_step": None}

            def wrap_step(step_idx, full_logits, bootstrap=15_000, cov_adjuster=None, charting=False):
                if state["slot"] >= len(cand_labels):
                    raise AssertionError("v1 trace slot overflow")
                if state["base_step"] is None:
                    state["base_step"] = int(step_idx)
                expected_step = int(state["base_step"]) + state["step"]
                if int(step_idx) != int(expected_step):
                    raise AssertionError(
                        f"Unexpected v1 step order: got step_idx={step_idx}, expected={expected_step}"
                    )
                lbl = cand_labels[state["slot"]]
                verifier._eq_current_candidate = lbl
                try:
                    out = orig_step(
                        step_idx, full_logits, bootstrap=bootstrap, cov_adjuster=cov_adjuster, charting=charting
                    )
                finally:
                    verifier._eq_current_candidate = ""
                v1_candidates[lbl]["p_value"].append(float(out["p_value"]))
                v1_candidates[lbl]["ulp_fail"].append(bool(out["ulp_fail"]))
                v1_candidates[lbl]["grid_fail"].append(bool(out.get("grid_fail", False)))
                v1_candidates[lbl]["R_obs"].append(float(out["R_obs"]))
                v1_candidates[lbl]["sampling_noise"].append(float(out["sampling_noise"]))
                dm = out["delta_mu"]
                if isinstance(dm, torch.Tensor):
                    dm = float(dm.detach().cpu().item())
                v1_candidates[lbl]["delta_mu"].append(float(dm))
                v1_candidates[lbl]["T_obs"].append(float(out["T_obs"]))
                if (not holders["v1_cov_adjuster"]) and cov_adjuster is not None:
                    holders["v1_cov_adjuster"] = _to_float_list(
                        cov_adjuster.detach().cpu().flatten()
                        if isinstance(cov_adjuster, torch.Tensor)
                        else cov_adjuster
                    )
                state["slot"] += 1
                if state["slot"] == len(cand_labels):
                    state["slot"] = 0
                    state["step"] += 1
                return out

            verifier._verify_step_from_logits = wrap_step
            try:
                status, message = verifier.verify_full_sequence_adaptive_parallel_efficient(
                    window_size=window_size,
                    batch_candidates=batch_candidates,
                    bootstrap=bootstrap,
                    charting=False,
                )
            finally:
                verifier._verify_step_from_logits = orig_step
                # If no early-return path happened, we should have complete per-step traces.
                # In early-return cases (e.g., grid fail), this can be partial.
                if state["step"] == 0:
                    raise AssertionError("v1 trace captured zero steps")
        elif version == "v2":
            orig_steps = verifier._verify_steps_from_logits_vectorized
            orig_cov = verifier._estimate_cov_adjuster_vectorized
            state = {"call": 0}

            def wrap_cov(logits_base, logits_ref, logits_spda, saved_idx_all):
                cov_adj, rank_cal, dmu_cal = orig_cov(logits_base, logits_ref, logits_spda, saved_idx_all)
                holders["v2_cov_adjuster_from_calib"] = _to_float_list(
                    cov_adj.detach().cpu().flatten()
                    if isinstance(cov_adj, torch.Tensor)
                    else cov_adj
                )
                holders["v2_rank_error_calib"] = _to_float_list(rank_cal)
                holders["v2_delta_mu_calib"] = _to_float_list(dmu_cal)
                return cov_adj, rank_cal, dmu_cal

            def wrap_steps(
                steps_logits,
                step_indices=None,
                bootstrap=15_000,
                cov_adjuster=None,
                charting=False,
                step_block=64,
                bootstrap_block=4096,
                tail_refine=True,
                compute_sampling_noise=True,
            ):
                if state["call"] >= len(cand_labels):
                    raise AssertionError("v2 trace call overflow")
                lbl = cand_labels[state["call"]]
                verifier._eq_current_candidate = lbl
                try:
                    out = orig_steps(
                        steps_logits,
                        step_indices=step_indices,
                        bootstrap=bootstrap,
                        cov_adjuster=cov_adjuster,
                        charting=charting,
                        step_block=step_block,
                        bootstrap_block=bootstrap_block,
                        tail_refine=tail_refine,
                        compute_sampling_noise=compute_sampling_noise,
                    )
                finally:
                    verifier._eq_current_candidate = ""
                v2_candidates[lbl]["p_value"] = _to_float_list(out["p_value"])
                v2_candidates[lbl]["ulp_fail"] = _to_bool_list(out["ulp_fail"])
                v2_candidates[lbl]["grid_fail"] = _to_bool_list(out["grid_fail"])
                v2_candidates[lbl]["R_obs"] = _to_float_list(out["R_obs"])
                v2_candidates[lbl]["sampling_noise"] = _to_float_list(out["sampling_noise"])
                v2_candidates[lbl]["delta_mu"] = _to_float_list(out["delta_mu"])
                v2_candidates[lbl]["T_obs"] = _to_float_list(out["T_obs"])
                if (not holders["v2_cov_adjuster"]) and cov_adjuster is not None:
                    holders["v2_cov_adjuster"] = _to_float_list(
                        cov_adjuster.detach().cpu().flatten()
                        if isinstance(cov_adjuster, torch.Tensor)
                        else cov_adjuster
                    )
                state["call"] += 1
                return out

            verifier._estimate_cov_adjuster_vectorized = wrap_cov
            verifier._verify_steps_from_logits_vectorized = wrap_steps
            try:
                status, message = verifier.verify_full_sequence_adaptive_parallel_efficient_v2(
                    window_size=window_size,
                    batch_candidates=batch_candidates,
                    bootstrap=bootstrap,
                    charting=False,
                    step_block=step_block,
                    bootstrap_block=bootstrap_block,
                )
            finally:
                verifier._estimate_cov_adjuster_vectorized = orig_cov
                verifier._verify_steps_from_logits_vectorized = orig_steps
                if state["call"] == 0:
                    raise AssertionError("v2 trace captured zero candidate calls")
        else:
            raise ValueError(f"Unknown version: {version}")
        params_after = mca_get_params()
        dbg_after = _mca_snapshot_to_ints(mca_debug_snapshot())
        holders[f"{version}_mca"] = {
            "params_before": {
                "enabled": bool(params_before.get("enabled", False)),
                "k_lin": float(params_before.get("k_lin", 0.0)),
                "k_attn": float(params_before.get("k_attn", 0.0)),
                "target_dtype": str(params_before.get("target_dtype", "")),
            },
            "params_after": {
                "enabled": bool(params_after.get("enabled", False)),
                "k_lin": float(params_after.get("k_lin", 0.0)),
                "k_attn": float(params_after.get("k_attn", 0.0)),
                "target_dtype": str(params_after.get("target_dtype", "")),
            },
            "debug_before": dbg_before,
            "debug_after": dbg_after,
            "debug_delta": _mca_snapshot_delta(dbg_before, dbg_after),
        }
        return RunResult(status=status, message=message, elapsed_s=time.perf_counter() - t0)

    try:
        v1 = run_once("v1")
        v2 = run_once("v2")
    finally:
        verifier._compute_all_logits_parallel_prefix = orig_compute_all_logits
        verifier._cached_gauss = orig_cached_gauss
    for key in ("v1", "v2"):
        if key not in captured_final:
            # Some verifier branches can return early (e.g. grid failure) before
            # invoking _validate_final_results. Keep the comparison/report path alive.
            captured_final[key] = {
                "p_values": [],
                "rank_error_actual": [],
                "rank_error_calib": [],
                "sampling_noise": [],
                "delta_mu": [],
                "delta_mu_calib": [],
                "steps": 0,
            }
    if shared_gauss:
        holders["shared_gauss"] = {
            **shared_gauss_state,
            "recorded_calls": int(len(shared_gauss_shapes)),
            "v2_missing_calls": int(max(len(shared_gauss_shapes) - int(shared_gauss_state["v2_calls"]), 0)),
        }
    else:
        holders["shared_gauss"] = {"enabled": False}

    # Soft drift metrics (reported always, enforced only in strict mode).
    p1 = captured_final["v1"]["p_values"]
    p2 = captured_final["v2"]["p_values"]
    n1 = captured_final["v1"]["sampling_noise"]
    n2 = captured_final["v2"]["sampling_noise"]
    d1 = captured_final["v1"]["delta_mu"]
    d2 = captured_final["v2"]["delta_mu"]

    p_abs = [abs(a - b) for a, b in zip(p1, p2)]
    n_abs = [abs(a - b) for a, b in zip(n1, n2)]
    d_abs = [abs(a - b) for a, b in zip(d1, d2)]

    v1_cov = holders["v1_cov_adjuster"]
    v2_cov = holders["v2_cov_adjuster"] or holders["v2_cov_adjuster_from_calib"]

    cand_metrics: Dict[str, Dict[str, Any]] = {}
    for lbl in cand_labels:
        c1 = v1_candidates[lbl]
        c2 = v2_candidates[lbl]
        cand_metrics[lbl] = {
            "p_value": {
                **_abs_diff_stats(c1["p_value"], c2["p_value"]),
                "ks": _ks_statistic(c1["p_value"], c2["p_value"]),
                "pearson_r": _pearson_r(c1["p_value"], c2["p_value"]),
            },
            "sampling_noise": {
                **_abs_diff_stats(c1["sampling_noise"], c2["sampling_noise"]),
                "pearson_r": _pearson_r(c1["sampling_noise"], c2["sampling_noise"]),
                "sign_agreement": _sign_agreement(c1["sampling_noise"], c2["sampling_noise"]),
            },
            "delta_mu": {
                **_abs_diff_stats(c1["delta_mu"], c2["delta_mu"]),
                "pearson_r": _pearson_r(c1["delta_mu"], c2["delta_mu"]),
                "sign_agreement": _sign_agreement(c1["delta_mu"], c2["delta_mu"]),
            },
            "R_obs": {
                **_abs_diff_stats(c1["R_obs"], c2["R_obs"]),
                "pearson_r": _pearson_r(c1["R_obs"], c2["R_obs"]),
            },
            "T_obs": {
                **_abs_diff_stats(c1["T_obs"], c2["T_obs"]),
                "pearson_r": _pearson_r(c1["T_obs"], c2["T_obs"]),
            },
            "ulp_fail": _bool_disagreement(c1["ulp_fail"], c2["ulp_fail"]),
            "grid_fail": _bool_disagreement(c1["grid_fail"], c2["grid_fail"]),
            "v1_grid_fail_count": float(sum(1 for x in c1["grid_fail"] if x)),
            "v2_grid_fail_count": float(sum(1 for x in c2["grid_fail"] if x)),
        }

    best_steps = min(
        min((len(v1_candidates[l]["p_value"]) for l in cand_labels), default=0),
        min((len(v2_candidates[l]["p_value"]) for l in cand_labels), default=0),
    )
    best_mism = 0
    v1_best_counts = {lbl: 0 for lbl in cand_labels}
    v2_best_counts = {lbl: 0 for lbl in cand_labels}
    first_mismatch_steps: List[int] = []
    best_p_abs: List[float] = []
    min_abs_noise_abs: List[float] = []
    min_abs_dmu_abs: List[float] = []
    rank_tau_distance: List[float] = []
    top2_mismatch_count = 0
    for s in range(best_steps):
        pvals1 = {lbl: float(v1_candidates[lbl]["p_value"][s]) for lbl in cand_labels}
        pvals2 = {lbl: float(v2_candidates[lbl]["p_value"][s]) for lbl in cand_labels}
        noise1 = {lbl: float(v1_candidates[lbl]["sampling_noise"][s]) for lbl in cand_labels}
        noise2 = {lbl: float(v2_candidates[lbl]["sampling_noise"][s]) for lbl in cand_labels}
        dmu1 = {lbl: float(v1_candidates[lbl]["delta_mu"][s]) for lbl in cand_labels}
        dmu2 = {lbl: float(v2_candidates[lbl]["delta_mu"][s]) for lbl in cand_labels}

        v1_best = max(cand_labels, key=lambda lbl: v1_candidates[lbl]["p_value"][s])
        v2_best = max(cand_labels, key=lambda lbl: v2_candidates[lbl]["p_value"][s])

        order1 = sorted(cand_labels, key=lambda lbl: pvals1[lbl], reverse=True)
        order2 = sorted(cand_labels, key=lambda lbl: pvals2[lbl], reverse=True)
        top2_a = set(order1[:2])
        top2_b = set(order2[:2])

        best_p_abs.append(abs(pvals1[v1_best] - pvals2[v2_best]))
        min_abs_noise_abs.append(abs(min(abs(v) for v in noise1.values()) - min(abs(v) for v in noise2.values())))
        min_abs_dmu_abs.append(abs(min(abs(v) for v in dmu1.values()) - min(abs(v) for v in dmu2.values())))
        rank_tau_distance.append(_kendall_tau_distance(order1, order2))

        v1_best_counts[v1_best] += 1
        v2_best_counts[v2_best] += 1
        if top2_a != top2_b:
            top2_mismatch_count += 1
        if v1_best != v2_best:
            best_mism += 1
            if len(first_mismatch_steps) < 20:
                first_mismatch_steps.append(int(s))

    best_sel = {
        "steps_compared": float(best_steps),
        "mismatch_count": float(best_mism),
        "mismatch_rate": float((best_mism / best_steps) if best_steps else 0.0),
        "first_mismatch_steps": first_mismatch_steps,
        "v1_best_counts": v1_best_counts,
        "v2_best_counts": v2_best_counts,
        "top2_mismatch_count": float(top2_mismatch_count),
        "top2_mismatch_rate": float((top2_mismatch_count / best_steps) if best_steps else 0.0),
    }

    per_step = {
        "best_p_abs": _value_stats(best_p_abs),
        "min_abs_sampling_noise_abs": _value_stats(min_abs_noise_abs),
        "min_abs_delta_mu_abs": _value_stats(min_abs_dmu_abs),
        "p_value_rank_tau_distance": _value_stats(rank_tau_distance),
    }

    rank_cal_v1 = captured_final["v1"]["rank_error_calib"]
    rank_cal_v2 = captured_final["v2"]["rank_error_calib"]
    dmu_cal_v1 = captured_final["v1"]["delta_mu_calib"]
    dmu_cal_v2 = captured_final["v2"]["delta_mu_calib"]

    summary = {
        "validation_request": str(validation_request_path),
        "config": {
            "window_size": window_size,
            "bootstrap": bootstrap,
            "batch_candidates": batch_candidates,
            "seed": seed,
            "step_block": step_block,
            "bootstrap_block": bootstrap_block,
            "strict_metrics": strict_metrics,
            "strict_intermediate": strict_intermediate,
            "allow_mismatch": allow_mismatch,
            "shared_forward": bool(shared_forward),
            "shared_forward_strict": bool(shared_forward_strict),
            "keyed_noise": bool(keyed_noise),
            "keyed_noise_seed": int(keyed_noise_seed),
            "shared_gauss": bool(shared_gauss),
            "shared_gauss_seed": int(shared_gauss_seed),
            "shared_gauss_strict": bool(shared_gauss_strict),
        },
        "v1": {
            "status": v1.status,
            "message": v1.message,
            "elapsed_s": round(v1.elapsed_s, 3),
            "steps": int(captured_final["v1"]["steps"]),
            "p_lt_0_01": int(sum(1 for x in p1 if x < 0.01)),
            "p_lt_0_001": int(sum(1 for x in p1 if x < 0.001)),
            "mca": holders.get("v1_mca", {}),
        },
        "v2": {
            "status": v2.status,
            "message": v2.message,
            "elapsed_s": round(v2.elapsed_s, 3),
            "steps": int(captured_final["v2"]["steps"]),
            "p_lt_0_01": int(sum(1 for x in p2 if x < 0.01)),
            "p_lt_0_001": int(sum(1 for x in p2 if x < 0.001)),
            "mca": holders.get("v2_mca", {}),
        },
        "drift": {
            "pvalue_abs_p50": _quantile(p_abs, 0.50),
            "pvalue_abs_p95": _quantile(p_abs, 0.95),
            "pvalue_abs_p99": _quantile(p_abs, 0.99),
            "sampling_noise_abs_mean": (sum(n_abs) / len(n_abs)) if n_abs else 0.0,
            "delta_mu_abs_mean": (sum(d_abs) / len(d_abs)) if d_abs else 0.0,
        },
        "intermediate": {
            "cov_adjuster": {
                "v1_len": float(len(v1_cov)),
                "v2_len": float(len(v2_cov)),
                "abs": _abs_diff_stats(v1_cov, v2_cov),
                "rel": _rel_diff_stats(v1_cov, v2_cov),
            },
            "calibration": {
                "rank_error_calib_abs": _abs_diff_stats(rank_cal_v1, rank_cal_v2),
                "rank_error_calib_ks": _ks_statistic(rank_cal_v1, rank_cal_v2),
                "delta_mu_calib_abs": _abs_diff_stats(dmu_cal_v1, dmu_cal_v2),
                "delta_mu_calib_ks": _ks_statistic(dmu_cal_v1, dmu_cal_v2),
            },
            "candidate_metrics": cand_metrics,
            "best_candidate_selection": best_sel,
            "per_step_selection_drift": per_step,
        },
        "debug": {
            "shared_forward": {
                "enabled": bool(shared_forward),
                "strict": bool(shared_forward_strict),
            },
            "keyed_noise": {
                "enabled": bool(keyed_noise),
                "seed": int(keyed_noise_seed),
            },
            "shared_gauss": holders.get("shared_gauss", {}),
        },
    }

    # Hard equivalence checks.
    if not allow_mismatch:
        assert captured_final["v1"]["steps"] == captured_final["v2"]["steps"], (
            f"Step count mismatch: v1={captured_final['v1']['steps']}, v2={captured_final['v2']['steps']}"
        )
        assert v1.status == v2.status, (
            "Status mismatch: "
            f"v1={v1.status!r}, v2={v2.status!r}, "
            f"v1_msg={v1.message!r}, v2_msg={v2.message!r}, "
            f"drift={summary['drift']}"
        )
        assert _canonical_reason(v1.message) == _canonical_reason(v2.message), (
            "Failure-class mismatch: "
            f"v1={v1.message!r}, v2={v2.message!r}, "
            f"drift={summary['drift']}"
        )

    if strict_metrics:
        assert summary["drift"]["pvalue_abs_p99"] <= max_p99_diff, (
            "v1/v2 p-value drift too high: "
            f"{summary['drift']['pvalue_abs_p99']:.6f} > {max_p99_diff:.6f}"
        )
        assert summary["drift"]["sampling_noise_abs_mean"] <= max_noise_mean_diff, (
            "v1/v2 sampling-noise drift too high: "
            f"{summary['drift']['sampling_noise_abs_mean']:.6f} > {max_noise_mean_diff:.6f}"
        )
        assert summary["drift"]["delta_mu_abs_mean"] <= max_dmu_mean_diff, (
            "v1/v2 delta-mu drift too high: "
            f"{summary['drift']['delta_mu_abs_mean']:.6f} > {max_dmu_mean_diff:.6f}"
        )
    if strict_intermediate:
        assert summary["intermediate"]["cov_adjuster"]["rel"]["p95"] <= max_cov_rel_p95, (
            "Cov adjuster relative drift too high: "
            f"{summary['intermediate']['cov_adjuster']['rel']['p95']:.6f} > {max_cov_rel_p95:.6f}"
        )
        assert summary["intermediate"]["best_candidate_selection"]["mismatch_rate"] <= max_best_cand_disagree, (
            "Best-candidate mismatch too high: "
            f"{summary['intermediate']['best_candidate_selection']['mismatch_rate']:.6f} "
            f"> {max_best_cand_disagree:.6f}"
        )
        for lbl in cand_labels:
            ulp_rate = summary["intermediate"]["candidate_metrics"][lbl]["ulp_fail"]["mismatch_rate"]
            grid_rate = summary["intermediate"]["candidate_metrics"][lbl]["grid_fail"]["mismatch_rate"]
            assert ulp_rate <= max_ulp_disagree, (
                f"ULP-fail disagreement too high for candidate {lbl}: "
                f"{ulp_rate:.6f} > {max_ulp_disagree:.6f}"
            )
            assert grid_rate <= max_grid_disagree, (
                f"Grid-fail disagreement too high for candidate {lbl}: "
                f"{grid_rate:.6f} > {max_grid_disagree:.6f}"
            )

    enforce_mca_noise = os.getenv("POW_EQ_ENFORCE_MCA", "1").lower() in {"1", "true", "yes"}
    if enforce_mca_noise and not shared_forward:
        for ver in ("v1", "v2"):
            m = summary[ver].get("mca", {})
            params = m.get("params_before", {})
            dbg_delta = m.get("debug_delta", {})
            assert bool(params.get("enabled", False)), f"MCA not enabled in {ver} params_before"
            assert float(params.get("k_attn", 0.0)) > 0.0, f"MCA k_attn not set in {ver}"
            # At least one of SDPA or attention-hook noise paths must fire.
            assert int(dbg_delta.get("any_noise_events", 0)) > 0, (
                f"MCA noise counters show no activity in {ver}: {dbg_delta}"
            )

    return summary


def _aggregate_runs(runs: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not runs:
        return {
            "runs": 0.0,
            "status_match_rate": 0.0,
            "reason_match_rate": 0.0,
            "drift": {},
            "intermediate": {},
        }

    n = len(runs)
    status_match = 0
    reason_match = 0
    for run in runs:
        if run["v1"]["status"] == run["v2"]["status"]:
            status_match += 1
        if _canonical_reason(run["v1"]["message"]) == _canonical_reason(run["v2"]["message"]):
            reason_match += 1

    p99_p = [float(r["drift"]["pvalue_abs_p99"]) for r in runs]
    noise_mean = [float(r["drift"]["sampling_noise_abs_mean"]) for r in runs]
    dmu_mean = [float(r["drift"]["delta_mu_abs_mean"]) for r in runs]
    cov_rel_p95 = [float(r["intermediate"]["cov_adjuster"]["rel"]["p95"]) for r in runs]
    best_mismatch = [float(r["intermediate"]["best_candidate_selection"]["mismatch_rate"]) for r in runs]
    top2_mismatch = [float(r["intermediate"]["best_candidate_selection"]["top2_mismatch_rate"]) for r in runs]
    rank_ks = [float(r["intermediate"]["calibration"]["rank_error_calib_ks"]) for r in runs]
    dmu_ks = [float(r["intermediate"]["calibration"]["delta_mu_calib_ks"]) for r in runs]
    rank_tau = [float(r["intermediate"]["per_step_selection_drift"]["p_value_rank_tau_distance"]["mean"]) for r in runs]

    cand_keys = list(runs[0]["intermediate"]["candidate_metrics"].keys())
    cand_agg: Dict[str, Any] = {}
    for lbl in cand_keys:
        ulp_mis = [float(r["intermediate"]["candidate_metrics"][lbl]["ulp_fail"]["mismatch_rate"]) for r in runs]
        grid_mis = [float(r["intermediate"]["candidate_metrics"][lbl]["grid_fail"]["mismatch_rate"]) for r in runs]
        p_corr = [float(r["intermediate"]["candidate_metrics"][lbl]["p_value"]["pearson_r"]) for r in runs]
        cand_agg[lbl] = {
            "ulp_fail_mismatch_rate": _value_stats(ulp_mis),
            "grid_fail_mismatch_rate": _value_stats(grid_mis),
            "p_value_pearson_r": _value_stats(p_corr),
        }

    return {
        "runs": float(n),
        "status_match_rate": float(status_match / n),
        "reason_match_rate": float(reason_match / n),
        "drift": {
            "pvalue_abs_p99": _value_stats(p99_p),
            "sampling_noise_abs_mean": _value_stats(noise_mean),
            "delta_mu_abs_mean": _value_stats(dmu_mean),
        },
        "intermediate": {
            "cov_adjuster_rel_p95": _value_stats(cov_rel_p95),
            "best_candidate_mismatch_rate": _value_stats(best_mismatch),
            "top2_candidate_mismatch_rate": _value_stats(top2_mismatch),
            "rank_error_calib_ks": _value_stats(rank_ks),
            "delta_mu_calib_ks": _value_stats(dmu_ks),
            "p_value_rank_tau_mean": _value_stats(rank_tau),
            "candidate_metrics": cand_agg,
        },
    }


def run_v1_v2_equivalence_suite(validation_request_path: Path) -> Dict[str, Any]:
    default_seed = _get_env_int("POW_EQ_SEED", 1337)
    seeds = _get_env_seed_list("POW_EQ_SEEDS", [default_seed])
    runs: List[Dict[str, Any]] = []
    for seed in seeds:
        runs.append(run_v1_v2_equivalence(validation_request_path, seed_override=seed))

    aggregate = _aggregate_runs(runs)
    suite = {
        "validation_request": str(validation_request_path),
        "seeds": seeds,
        "runs": runs,
        "aggregate": aggregate,
    }

    strict_suite = os.getenv("POW_EQ_STRICT_SUITE", "0").lower() in {"1", "true", "yes"}
    if strict_suite:
        max_suite_best_mismatch_p95 = _get_env_float("POW_EQ_SUITE_MAX_BEST_MISMATCH_P95", 0.2)
        max_suite_rank_tau_p95 = _get_env_float("POW_EQ_SUITE_MAX_RANK_TAU_P95", 0.5)
        max_suite_cov_rel_p95_p95 = _get_env_float("POW_EQ_SUITE_MAX_COV_REL_P95_P95", 0.5)
        min_suite_p_corr_p50 = _get_env_float("POW_EQ_SUITE_MIN_P_CORR_P50", 0.5)

        assert aggregate["status_match_rate"] == 1.0, (
            f"Suite status-match rate below 1.0: {aggregate['status_match_rate']:.6f}"
        )
        assert aggregate["reason_match_rate"] == 1.0, (
            f"Suite reason-match rate below 1.0: {aggregate['reason_match_rate']:.6f}"
        )
        assert aggregate["intermediate"]["best_candidate_mismatch_rate"]["p95"] <= max_suite_best_mismatch_p95, (
            "Suite best-candidate mismatch p95 too high: "
            f"{aggregate['intermediate']['best_candidate_mismatch_rate']['p95']:.6f} > "
            f"{max_suite_best_mismatch_p95:.6f}"
        )
        assert aggregate["intermediate"]["p_value_rank_tau_mean"]["p95"] <= max_suite_rank_tau_p95, (
            "Suite p-value rank-tau mean p95 too high: "
            f"{aggregate['intermediate']['p_value_rank_tau_mean']['p95']:.6f} > "
            f"{max_suite_rank_tau_p95:.6f}"
        )
        assert aggregate["intermediate"]["cov_adjuster_rel_p95"]["p95"] <= max_suite_cov_rel_p95_p95, (
            "Suite cov-adjuster rel-p95 p95 too high: "
            f"{aggregate['intermediate']['cov_adjuster_rel_p95']['p95']:.6f} > "
            f"{max_suite_cov_rel_p95_p95:.6f}"
        )
        for lbl, stats in aggregate["intermediate"]["candidate_metrics"].items():
            p50 = stats["p_value_pearson_r"]["p50"]
            assert p50 >= min_suite_p_corr_p50, (
                f"Suite p-value correlation too low for candidate {lbl}: "
                f"{p50:.6f} < {min_suite_p_corr_p50:.6f}"
            )

    return suite


def test_v1_v2_equivalence_on_validation_request():
    """
    Heavy integration-like test.

    By default this test uses the full 256-step window and can take >1 minute.
    """
    req_path = THIS_FILE.parent / "validation_request.bin"
    assert req_path.exists(), f"Missing fixture: {req_path}"
    suite = run_v1_v2_equivalence_suite(req_path)
    summary = suite["runs"][0]
    # Keep a minimal final assert so pytest reports this test as assertion-based.
    assert summary["v1"]["status"] == summary["v2"]["status"]
    assert suite["aggregate"]["status_match_rate"] == 1.0
    assert suite["aggregate"]["reason_match_rate"] == 1.0


def run_v2_streamed_equivalence(
    validation_request_path: Path, *, seed_override: int | None = None
) -> Dict[str, Any]:
    """
    STRICT equivalence: verify_full_sequence_adaptive_parallel_efficient_v2 (reference)
    vs verify_full_sequence_adaptive_parallel_efficient_v2_streamed (storage/lifetime-only
    refactor: candidate logits compacted to (S,2,V), per-candidate logits freed after use).

    Because the streamed variant only changes tensor lifetime/compaction (rows 0 and -1 are
    the only rows any consumer reads), with shared forwards + keyed noise the two MUST produce
    bit-identical inputs into _validate_final_results. Asserts identical status/reason and
    max abs diff over every _validate_final_results vector <= POW_EQ_STREAMED_TOL (default 0).
    Also reports CUDA peak-allocated for each variant to quantify the memory win.

    Recommended invocation (deterministic parity):
      POW_EQ_SHARED_FORWARD=1 POW_EQ_KEYED_NOISE=1 POW_EQ_SHARED_GAUSS=1
    """
    req = validation_request_path.read_bytes()
    pow_blob = pfunpack.unpack_validation_request(req)["request"]["pow_blob"]

    bootstrap = _get_env_int("POW_EQ_BOOTSTRAP", 15_000)
    window_size = _get_env_int("POW_EQ_WINDOW_SIZE", 256)
    step_block = _get_env_int("POW_EQ_STEP_BLOCK", 64)
    bootstrap_block = _get_env_int("POW_EQ_BOOTSTRAP_BLOCK", 4096)
    batch_candidates = _get_env_batches("POW_EQ_BATCH_CANDIDATES", [2, 5, 10, 20])
    seed = int(seed_override if seed_override is not None else _get_env_int("POW_EQ_SEED", 1337))
    keyed_noise = _get_env_bool("POW_EQ_KEYED_NOISE", True)
    keyed_noise_seed = _get_env_int("POW_EQ_KEYED_NOISE_SEED", seed)
    shared_forward = _get_env_bool("POW_EQ_SHARED_FORWARD", True)
    shared_gauss = _get_env_bool("POW_EQ_SHARED_GAUSS", True)
    shared_gauss_seed = _get_env_int("POW_EQ_SHARED_GAUSS_SEED", seed)
    tol = _get_env_float("POW_EQ_STREAMED_TOL", 0.0)

    mca_set_enabled(True)
    mca_set_params(k_lin=1.5, k_attn=8.0, target_dtype=torch.float16)
    torch.manual_seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(seed)

    verifier = ProofVerifier()
    verifier.initialise(pow_blob)
    verifier.reload(pow_blob)
    verifier._eq_keyed_noise = {"enabled": bool(keyed_noise), "seed": int(keyed_noise_seed)}
    verifier._eq_current_candidate = ""
    model_device = next(verifier.model.parameters()).device
    if model_device == torch.device("cpu"):
        batch_candidates = [2]
    runtime_mca_noise = float(mca_get_params().get("k_attn", 8.0))
    if model_device == torch.device("cpu"):
        runtime_mca_noise = 12.0

    # ---- shared forward: compute each forward once, reuse for BOTH variants ----
    orig_compute = verifier._compute_all_logits_parallel_prefix
    shared_logits: Dict[Any, torch.Tensor] = {}
    if shared_forward:
        shared_logits[1] = orig_compute(
            window_size, batch_size=1, flash=False, as_tensor=True
        ).detach().clone()
        for bs in batch_candidates:
            shared_logits[int(bs)] = orig_compute(
                window_size, batch_size=int(bs), flash=False,
                mca_noise_value=runtime_mca_noise, as_tensor=True,
            ).detach().clone()
        shared_logits["spda"] = orig_compute(
            window_size, batch_size=1, flash=False,
            mca_noise_value=runtime_mca_noise, as_tensor=True,
        ).detach().clone()

        def shared_compute(window_size_arg, batch_size=1, flash=False, enable_math=None,
                           enable_mem=None, mca_noise_value=None, as_tensor=False,
                           compact_rows=False):
            if mca_noise_value is None and int(batch_size) == 1:
                key = 1
            elif int(batch_size) == 1:
                key = "spda"
            else:
                key = int(batch_size)
            src = shared_logits[key]
            if as_tensor:
                if compact_rows and src.size(1) > 2:
                    return src[:, [0, src.size(1) - 1], :].clone()
                return src.clone()
            return {i: src[i].clone() for i in range(int(window_size))}
        verifier._compute_all_logits_parallel_prefix = shared_compute

    # ---- shared gauss: deterministic bootstrap by call index (reset per run) ----
    orig_cached_gauss = verifier._cached_gauss
    gstate = {"calls": 0}
    if shared_gauss:
        def shared_cached_gauss(dim, B):
            md = next(verifier.model.parameters()).device
            call_idx = gstate["calls"]; gstate["calls"] += 1
            gen = torch.Generator(device=md); gen.manual_seed(int(shared_gauss_seed + call_idx))
            return torch.randn((int(B), int(dim)), device=md, generator=gen)
        verifier._cached_gauss = shared_cached_gauss

    # ---- capture _validate_final_results inputs ----
    orig_validate = verifier._validate_final_results
    captured: Dict[str, Dict[str, List[float]]] = {}
    active = {"name": None}

    def capture_validate(p_values, rank_error_actual, rank_error_calib, all_sampling_noise,
                         all_delta_mu, delta_mu_calib, *, charting=False, ref_sampling_noise=None,
                         delta_raw=None, candidate_sampling_noise=None, candidate_labels=None):
        flat_cand = []
        for row in (candidate_sampling_noise or []):
            flat_cand.extend(row if isinstance(row, (list, tuple)) else [row])
        flat_draw = []
        for t in (delta_raw or []):
            flat_draw.extend(
                t.detach().cpu().flatten().tolist() if isinstance(t, torch.Tensor) else list(t)
            )
        captured[active["name"]] = {
            "p_values": _to_float_list(p_values),
            "rank_error_actual": _to_float_list(rank_error_actual),
            "rank_error_calib": _to_float_list(rank_error_calib),
            "sampling_noise": _to_float_list(all_sampling_noise),
            "delta_mu": _to_float_list(all_delta_mu),
            "delta_mu_calib": _to_float_list(delta_mu_calib),
            "ref_sampling_noise": _to_float_list(ref_sampling_noise or []),
            "candidate_sampling_noise": _to_float_list(flat_cand),
            "delta_raw": flat_draw,
        }
        return orig_validate(
            p_values=p_values, rank_error_actual=rank_error_actual,
            rank_error_calib=rank_error_calib, all_sampling_noise=all_sampling_noise,
            all_delta_mu=all_delta_mu, delta_mu_calib=delta_mu_calib, charting=charting,
            ref_sampling_noise=ref_sampling_noise, delta_raw=delta_raw,
            candidate_sampling_noise=candidate_sampling_noise, candidate_labels=candidate_labels,
        )
    verifier._validate_final_results = capture_validate

    results: Dict[str, Dict[str, Any]] = {}

    def run(name, fn):
        active["name"] = name
        gstate["calls"] = 0   # identical per-call gauss seeds across both variants
        if model_device.type == "cuda":
            torch.cuda.reset_peak_memory_stats()
        t0 = time.perf_counter()
        st, msg = fn(
            window_size=window_size, batch_candidates=batch_candidates, bootstrap=bootstrap,
            charting=False, step_block=step_block, bootstrap_block=bootstrap_block,
        )
        peak = (torch.cuda.max_memory_allocated() / (1024 ** 3)) if model_device.type == "cuda" else None
        results[name] = {"status": st, "message": msg,
                         "peak_gib": peak, "elapsed_s": round(time.perf_counter() - t0, 2)}

    try:
        run("v2", verifier.verify_full_sequence_adaptive_parallel_efficient_v2)
        run("v2_streamed", verifier.verify_full_sequence_adaptive_parallel_efficient_v2_streamed)
    finally:
        verifier._compute_all_logits_parallel_prefix = orig_compute
        verifier._cached_gauss = orig_cached_gauss
        verifier._validate_final_results = orig_validate

    a, b = captured["v2"], captured["v2_streamed"]
    per_field: Dict[str, Dict[str, float]] = {}
    max_abs = 0.0
    for k in a:
        st = _abs_diff_stats(a[k], b[k])
        per_field[k] = st
        max_abs = max(max_abs, st["max"])

    summary = {
        "validation_request": str(validation_request_path),
        "config": {"window_size": window_size, "bootstrap": bootstrap,
                   "batch_candidates": batch_candidates, "seed": seed,
                   "keyed_noise": keyed_noise, "shared_forward": shared_forward,
                   "shared_gauss": shared_gauss, "tol": tol},
        "v2": results["v2"], "v2_streamed": results["v2_streamed"],
        "max_abs_diff": max_abs, "per_field": per_field,
        "peak_gib": {"v2": results["v2"]["peak_gib"],
                     "v2_streamed": results["v2_streamed"]["peak_gib"]},
    }

    assert results["v2"]["status"] == results["v2_streamed"]["status"], (
        f"status mismatch: v2={results['v2']['status']!r} "
        f"streamed={results['v2_streamed']['status']!r}"
    )
    assert _canonical_reason(results["v2"]["message"]) == _canonical_reason(
        results["v2_streamed"]["message"]
    ), (f"reason mismatch: v2={results['v2']['message']!r} "
        f"streamed={results['v2_streamed']['message']!r}")
    assert max_abs <= tol, (
        f"v2 vs streamed _validate_final_results max abs diff {max_abs:.3e} > tol {tol:.3e}; "
        f"per_field={per_field}"
    )
    return summary


def test_v2_streamed_equivalence_on_validation_request():
    """Strict equivalence of v2 vs its streamed/compact storage refactor."""
    req_path = THIS_FILE.parent / "validation_request.bin"
    assert req_path.exists(), f"Missing fixture: {req_path}"
    # Default to deterministic parity unless the caller overrode the flags.
    os.environ.setdefault("POW_EQ_SHARED_FORWARD", "1")
    os.environ.setdefault("POW_EQ_KEYED_NOISE", "1")
    os.environ.setdefault("POW_EQ_SHARED_GAUSS", "1")
    s = run_v2_streamed_equivalence(req_path)
    assert s["v2"]["status"] == s["v2_streamed"]["status"]
    assert s["max_abs_diff"] <= _get_env_float("POW_EQ_STREAMED_TOL", 0.0)


if __name__ == "__main__":
    req_path = THIS_FILE.parent / "validation_request.bin"
    if not req_path.exists():
        raise SystemExit(f"Missing fixture: {req_path}")
    suite = run_v1_v2_equivalence_suite(req_path)
    output_json = os.getenv("POW_EQ_OUTPUT_JSON", "").strip()
    if output_json:
        with open(output_json, "w", encoding="utf-8") as fp:
            json.dump(suite, fp, indent=2)
        print(f"wrote suite JSON: {output_json}")
    print("v1/v2 equivalence suite passed")
    for run in suite["runs"]:
        seed = run["config"]["seed"]
        print(f"[seed={seed}] v1: status={run['v1']['status']}, elapsed={run['v1']['elapsed_s']}s")
        print(f"[seed={seed}] v2: status={run['v2']['status']}, elapsed={run['v2']['elapsed_s']}s")
        print(
            f"[seed={seed}] intermediate:"
            f" best_candidate_mismatch={run['intermediate']['best_candidate_selection']['mismatch_rate']:.6f},"
            f" top2_mismatch={run['intermediate']['best_candidate_selection']['top2_mismatch_rate']:.6f},"
            f" rank_tau_mean={run['intermediate']['per_step_selection_drift']['p_value_rank_tau_distance']['mean']:.6f}"
        )
        print(
            f"[seed={seed}] mca:"
            f" v1_noise_events={run['v1'].get('mca', {}).get('debug_delta', {}).get('any_noise_events', 0)},"
            f" v2_noise_events={run['v2'].get('mca', {}).get('debug_delta', {}).get('any_noise_events', 0)},"
            f" v1_k_attn={run['v1'].get('mca', {}).get('params_before', {}).get('k_attn', 0.0)},"
            f" v2_k_attn={run['v2'].get('mca', {}).get('params_before', {}).get('k_attn', 0.0)}"
        )
    agg = suite["aggregate"]
    print(
        "aggregate:"
        f" seeds={suite['seeds']},"
        f" status_match_rate={agg['status_match_rate']:.6f},"
        f" reason_match_rate={agg['reason_match_rate']:.6f},"
        f" best_mismatch_p95={agg['intermediate']['best_candidate_mismatch_rate']['p95']:.6f},"
        f" rank_tau_p95={agg['intermediate']['p_value_rank_tau_mean']['p95']:.6f}"
    )
    summary = suite["runs"][0]
    print(
        "drift:"
        f" p99(p)={summary['drift']['pvalue_abs_p99']:.6f},"
        f" mean(|noise|)={summary['drift']['sampling_noise_abs_mean']:.6f},"
        f" mean(|delta_mu|)={summary['drift']['delta_mu_abs_mean']:.6f}"
    )
    print(
        "intermediate:"
        f" cov_rel_p95={summary['intermediate']['cov_adjuster']['rel']['p95']:.6f},"
        f" best_candidate_mismatch={summary['intermediate']['best_candidate_selection']['mismatch_rate']:.6f},"
        f" rank_calib_ks={summary['intermediate']['calibration']['rank_error_calib_ks']:.6f},"
        f" dmu_calib_ks={summary['intermediate']['calibration']['delta_mu_calib_ks']:.6f}"
    )
