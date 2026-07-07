# SPDX-License-Identifier: Apache-2.0
# --------------------------------------------------------------------------- #
#                          Imports
# --------------------------------------------------------------------------- #

#------ general utils
from __future__ import annotations
import os


def _configure_hf_cache_home() -> None:
    """
    Ensure HuggingFace caches point to a writable location.

    Priority:
      1) Existing HF_HOME
      2) MODEL_CACHE_DIR
      3) /data/pow_proofs/cache/huggingface
      4) /tmp/huggingface
    """
    candidates = [
        os.environ.get("HF_HOME"),
        os.environ.get("MODEL_CACHE_DIR"),
        "/data/pow_proofs/cache/huggingface",
        "/tmp/huggingface",
    ]

    for base in candidates:
        if not base:
            continue
        try:
            hub_dir = os.path.join(base, "hub")
            os.makedirs(hub_dir, exist_ok=True)
            os.environ["HF_HOME"] = base
            os.environ.setdefault("HF_HUB_CACHE", hub_dir)
            os.environ.setdefault("HUGGINGFACE_HUB_CACHE", hub_dir)
            os.environ.setdefault("TRANSFORMERS_CACHE", hub_dir)
            return
        except Exception:
            continue


_configure_hf_cache_home()
import time
import uuid
import json
import math
import contextvars
import itertools
from typing import Dict, List, Tuple, Any, Optional, Union
import base64
import hashlib
import h5py
import inspect
import pickle
import shutil
import subprocess
import tempfile
import warnings
import psutil
from itertools import product
from collections import defaultdict, OrderedDict
import sys
import struct
from pathlib import Path
from einops import rearrange
from dataclasses import dataclass, field
from contextlib import contextmanager
from contextlib import nullcontext 
import logging
import copy 
import threading

#------ specific utils
import flatbuffers
from tqdm.auto import tqdm
import matplotlib.pyplot as plt

#------ Torch
import torch
import torch.nn.functional as F
import torch.nn as nn
from torch.distributions import Chi2
from torch.distributions.normal import Normal
from torch.nn.attention import sdpa_kernel, SDPBackend
from torch.backends.cuda import sdp_kernel as old_sdp  

#------ HF
from transformers import AutoConfig, AutoModelForCausalLM, AutoTokenizer, PreTrainedModel
from huggingface_hub import list_repo_files, hf_hub_download

#------ numpy / sklearn / scipy
import numpy as np
import scipy.stats as st
from scipy import stats
import scipy.stats as stats
from scipy.stats import chi2
from sklearn.covariance import ledoit_wolf
from sklearn.decomposition import PCA

#------ modules
from utils.proof import Proof
from utils.proof import FloatArray
from utils.proof import UIntArray
from utils.proof import ResponseValue
from config.constants import *
from config.constants import _DTYPE_BYTES, _NORMAL, _TWO_PI, ATOL
from utils.shared_utils import validate_by_quantiles, validate_by_quantiles_higher, validate_by_quantiles_lower, proof_to_dict, _snap, _ulp, _sigma_from_ulp, _bucket_means, chiavdf_verify, parse_safetensors_header, inspect_bin_dtype, get_native_dtype_from_commit, inspect_model_dtype, fit_nb_mom, right_tail_test, RunningMeanCov      
from utils.pow_utils import POW_WINDOW_SIZE, SequenceCache, PowState, Logger, RowManager, RingBuffers, PowHasher, ProofWriter, _to_bytes, serialize_proof, sha256_many, check_hash_against_target
from utils.pow_utils import _tok_le_bytes, _u32le, _str_bytes, _build_msg, _digest_to_u, hex_to_bytes_tensor, nbits_to_target, _has_pow, to_python_string
from utils.uint256_arithmetics import set_compact, get_compact
import pfunpack

from enhanced_logger import VerificationLogger, create_logger

_PRECISION_TO_DTYPE = {
    "fp16": torch.float16,
    "bf16": torch.bfloat16,
    "fp32": torch.float32,
    "int8": torch.int8,
}


def _torch_fp8_dtypes() -> tuple[torch.dtype, ...]:
    return tuple(
        dt for dt in (
            getattr(torch, "float8_e4m3fn", None),
            getattr(torch, "float8_e4m3", None),
            getattr(torch, "float8_e4m3fnuz", None),
            getattr(torch, "float8_e5m2", None),
            getattr(torch, "float8_e5m2fnuz", None),
        ) if dt is not None
    )


def _is_fp8_dtype(dtype: torch.dtype | None) -> bool:
    return dtype is not None and dtype in _torch_fp8_dtypes()


def _dtype_from_string(value: str | torch.dtype | None) -> torch.dtype | None:
    torch_dtype_type = getattr(torch, "dtype", None)
    if torch_dtype_type is not None and isinstance(value, torch_dtype_type):
        return value
    if not isinstance(value, str):
        return None
    normalized = value.lower().replace("torch.", "")
    aliases = {
        "float16": "fp16",
        "half": "fp16",
        "bfloat16": "bf16",
        "float32": "fp32",
        "float": "fp32",
    }
    return _PRECISION_TO_DTYPE.get(aliases.get(normalized, normalized))


def _model_config_diff_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except Exception:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _config_quantization_config(config: Any) -> Any:
    for obj in (config, getattr(config, "text_config", None)):
        if obj is None:
            continue
        qcfg = getattr(obj, "quantization_config", None)
        if qcfg is not None:
            return qcfg
        ccfg = getattr(obj, "compression_config", None)
        if ccfg is not None:
            return ccfg
    return None


def _contains_fp8_marker(value: Any) -> bool:
    if isinstance(value, str):
        lowered = value.lower()
        return any(marker in lowered for marker in ("fp8", "float8", "f8_"))
    if isinstance(value, dict):
        return any(
            _contains_fp8_marker(k) or _contains_fp8_marker(v)
            for k, v in value.items()
        )
    if isinstance(value, (list, tuple, set)):
        return any(_contains_fp8_marker(v) for v in value)
    if hasattr(value, "to_dict"):
        try:
            return _contains_fp8_marker(value.to_dict())
        except Exception:
            return False
    if hasattr(value, "__dict__") and not isinstance(value, type):
        return _contains_fp8_marker(vars(value))
    return False


def _config_is_fp8_quantized(config: Any) -> bool:
    return _contains_fp8_marker(_config_quantization_config(config))


def _resolve_fp8_replay_dtype(
    *,
    proof_config_diff: Any = None,
    hf_config: Any = None,
    fallback: torch.dtype = torch.bfloat16,
) -> torch.dtype:
    """Resolve the actual CausalLM replay dtype for FP8 proofs.

    `compute_precision="fp8"` is the PoW hash/model contract. It is not a
    PyTorch compute dtype. FP8 checkpoints execute with bf16/fp16 activations
    while quantized layers own the FP8 weights/scales and kernels.
    """
    diff = _model_config_diff_dict(proof_config_diff)
    for key in ("replay_compute_dtype", "activation_dtype", "torch_dtype"):
        dtype = _dtype_from_string(diff.get(key))
        if dtype in (torch.bfloat16, torch.float16, torch.float32):
            return dtype

    for attr in ("torch_dtype", "dtype"):
        dtype = _dtype_from_string(getattr(hf_config, attr, None))
        if dtype in (torch.bfloat16, torch.float16, torch.float32):
            return dtype

    return fallback


def _precision_replay_dtype(
    precision: str,
    *,
    proof_config_diff: Any = None,
    hf_config: Any = None,
) -> torch.dtype:
    if precision == "fp8":
        return _resolve_fp8_replay_dtype(
            proof_config_diff=proof_config_diff,
            hf_config=hf_config,
        )
    return _PRECISION_TO_DTYPE.get(precision, torch.float32)


def _env_flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def _env_int(name: str, default: int, *, minimum: int = 0) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, value)


def _env_float(name: str, default: float, *, minimum: float = 0.0, maximum: float = 1.0) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return min(max(value, minimum), maximum)


def _prob_noise_bucket_decision(
    noise_arr: np.ndarray,
    noise_qt: List[Tuple[float, float]],
    *,
    candidate_sampling_noise: Optional[Any] = None,
    adaptive_enabled: Optional[bool] = None,
    loo_slack: Optional[int] = None,
    loo_quantile: Optional[float] = None,
) -> Dict[str, Any]:
    """Validate probability-CDF residual buckets, optionally using LOO MCA null.

    ``candidate_sampling_noise`` is expected to be shaped (candidates, steps)
    where each value is ``candidate_upper_cdf - proof_upper_cdf``. Pairwise
    differences between candidates therefore cancel the proof term and estimate
    the local replay CDF spread under the same statistic as production.
    """
    arr = np.asarray(noise_arr, dtype=np.float64).ravel()
    n = int(arr.size)
    thresholds = [float(q) for q, _ in noise_qt]
    old_allowed = [int(math.floor(float(frac) * n + 1e-12)) for _, frac in noise_qt]
    obs_counts = [int((np.abs(arr) > q).sum()) for q in thresholds]

    final_allowed = list(old_allowed)
    empirical_allowed: Optional[List[int]] = None
    loo_counts: Optional[List[List[int]]] = None
    adaptive_used = False

    if adaptive_enabled is None:
        # LOO adaptive enforcement is ON by default: the legacy bucket null
        # over-rejects marginal-but-valid proofs (false REDs). Override with
        # POW_PROB_NOISE_ADAPTIVE_ENFORCE (or POW_PROB_NOISE_ADAPTIVE) = 0.
        adaptive_enabled = _env_flag(
            "POW_PROB_NOISE_ADAPTIVE_ENFORCE",
            _env_flag("POW_PROB_NOISE_ADAPTIVE", True),
        )
    if loo_slack is None:
        loo_slack = _env_int("POW_PROB_NOISE_LOO_SLACK", 1, minimum=0)
    if loo_quantile is None:
        loo_quantile = _env_float("POW_PROB_NOISE_LOO_QUANTILE", 1.0)

    if candidate_sampling_noise is not None and n > 0:
        cand = np.asarray(candidate_sampling_noise, dtype=np.float64)
        if cand.ndim == 2 and cand.shape[0] >= 2 and cand.shape[1] == n:
            per_anchor_counts: List[List[int]] = []
            for anchor in range(cand.shape[0]):
                diffs = np.abs(cand - cand[anchor:anchor + 1, :])
                diffs[anchor, :] = np.inf
                loo_resid = np.min(diffs, axis=0)
                if not np.isfinite(loo_resid).all():
                    continue
                per_anchor_counts.append([
                    int((loo_resid > threshold).sum())
                    for threshold in thresholds
                ])

            if per_anchor_counts:
                counts_np = np.asarray(per_anchor_counts, dtype=np.float64)
                empirical = np.ceil(
                    np.quantile(counts_np, loo_quantile, axis=0)
                ).astype(np.int64)
                empirical = np.minimum(n, empirical + int(loo_slack))
                empirical_allowed = [int(x) for x in empirical.tolist()]
                adaptive_allowed = [
                    max(base, empirical)
                    for base, empirical in zip(old_allowed, empirical_allowed)
                ]
                if adaptive_enabled:
                    final_allowed = list(adaptive_allowed)
                loo_counts = [[int(x) for x in row] for row in per_anchor_counts]
                adaptive_used = True

    valid = all(obs <= allowed for obs, allowed in zip(obs_counts, final_allowed))
    legacy_valid = all(obs <= allowed for obs, allowed in zip(obs_counts, old_allowed))
    adaptive_allowed = [
        max(base, empirical)
        for base, empirical in zip(old_allowed, empirical_allowed)
    ] if empirical_allowed is not None else list(old_allowed)
    adaptive_valid = all(
        obs <= allowed
        for obs, allowed in zip(obs_counts, adaptive_allowed)
    )
    return {
        "valid": bool(valid),
        "legacy_valid": bool(legacy_valid),
        "adaptive_valid": bool(adaptive_valid),
        "adaptive_available": bool(adaptive_used),
        "adaptive_enforced": bool(adaptive_enabled and adaptive_used),
        "adaptive_used": bool(adaptive_enabled and adaptive_used),
        "thresholds": thresholds,
        "obs_counts": obs_counts,
        "old_allowed": old_allowed,
        "empirical_allowed": empirical_allowed,
        "adaptive_allowed": adaptive_allowed,
        "final_allowed": final_allowed,
        "loo_counts": loo_counts,
        "loo_slack": int(loo_slack),
        "loo_quantile": float(loo_quantile),
    }


try:
    # profiler.py
    import time
    import atexit
    import functools

    # Global registry for all profiled methods
    _pow_profile_data: dict[str, dict[str, float]] = {}

    def pow_profiler(fn):
        """
        Decorator to profile how many times fn is called and 
        its total/min/max elapsed time.
        
        Attach it to any method with @profiler.
        """
        qual = fn.__qualname__
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            t0 = time.perf_counter()
            result = fn(*args, **kwargs)
            dt = time.perf_counter() - t0
            
            stats = _pow_profile_data.setdefault(qual, {
                "count": 0,
                "total": 0.0,
                "min": float("inf"),
                "max": 0.0,
            })
            stats["count"] += 1
            stats["total"] += dt
            stats["min"] = min(stats["min"], dt)
            stats["max"] = max(stats["max"], dt)
            
            return result
        return wrapper

    def _pow_print_profile_summary():
        if not _pow_profile_data:
            print("No profiling data collected.")
            return
        print("\n=== Profiling Summary ===")
        for qual, s in _pow_profile_data.items():
            avg = s["total"] / s["count"]
            print(
                f"{qual:50s} "
                f"calls={s['count']:5d}  "
                f"avg={avg*1e3:6.2f} ms  "
                f"min={s['min']*1e3:6.2f} ms  "
                f"max={s['max']*1e3:6.2f} ms"
            )
        print("=========================\n")

    # print automatically on normal exit
    atexit.register(_pow_print_profile_summary)
except:
    def profiler(fn):
        return fn    

from torch.profiler import profile, ProfilerActivity

import functools
def per_op_profiler(func):
    """Decorator for per-operation profiling with PyTorch profiler.
    
    Args:
        func: Function to profile
        
    Returns:
        Wrapped function that profiles CPU and CUDA operations
    """
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        with profile(activities=[ProfilerActivity.CPU, ProfilerActivity.CUDA], record_shapes=False) as prof:
            result = func(*args, **kwargs)
        print(prof.key_averages().table(sort_by="cuda_time_total", row_limit=10))
        return result
    return wrapper

# --------------------------------------------------------------------------- #
#                     MCA Noise injector (thread-safe version)                #
# --------------------------------------------------------------------------- #

# ---- public names / semantics preserved -------------------------------------
_IN_ATTN = contextvars.ContextVar("in_attention", default=False)
# Tracks whether attention-output noise was already applied within the current
# attention module scope (prevents double application when SDPA is used).
_ATTN_NOISE_APPLIED = contextvars.ContextVar("attn_noise_applied", default=False)

# Global toggle for GPU row-wise vs element-wise noise (same as before)
_GPU_ROWWISE = False

# Default chunk size for pools (~4MB elements → ~16MB fp32)
_POOL_CHUNK = 4 * 1024 * 1024

# ---- NEW: context-scoped controls (thread/task-safe) ------------------------
_MCA_ENABLED   = contextvars.ContextVar("mca_enabled", default=False)
_K_LIN_CV      = contextvars.ContextVar("mca_k_lin", default=1.5)
_K_ATTN_CV     = contextvars.ContextVar("mca_k_attn", default=8.0)
_TGT_DTYPE_CV  = contextvars.ContextVar("mca_target_dtype", default=None)
_POOLS_CV      = contextvars.ContextVar("mca_pools", default=None)  # per-thread/task pools

# ---- one-time patch machinery ----------------------------------------------
_INSTALL_LOCK = threading.Lock()
_PATCH_INSTALLED = False
_SDPA_ORIG = None
_LINEAR_ORIG = None

# ---- debug counters ---------------------------------------------------------
_MCA_DBG = {
    'sdpa_calls': 0,
    'sdpa_noised': 0,
    'linear_noised': 0,
    'attn_hook_noised': 0,
    'k_attn_recent': [],  # list[float]
    'k_lin_recent': [],   # list[float]
}
_MCA_DBG_LOCK = threading.Lock()

def _dbg_push(key: str, value: float, limit: int = 16):
    try:
        with _MCA_DBG_LOCK:
            arr = _MCA_DBG.get(key)
            if isinstance(arr, list):
                arr.append(float(value))
                if len(arr) > limit:
                    del arr[:len(arr)-limit]
    except Exception:
        pass

def _dbg_inc(key: str, by: int = 1):
    try:
        with _MCA_DBG_LOCK:
            _MCA_DBG[key] = int(_MCA_DBG.get(key, 0)) + by
    except Exception:
        pass

def mca_debug_snapshot() -> dict:
    """Return a shallow copy of current MCA debug counters."""
    with _MCA_DBG_LOCK:
        return {k: (v[:] if isinstance(v, list) else v) for k, v in _MCA_DBG.items()}

def mca_debug_reset():
    """Reset MCA debug counters."""
    with _MCA_DBG_LOCK:
        _MCA_DBG.update({
            'sdpa_calls': 0,
            'sdpa_noised': 0,
            'linear_noised': 0,
            'attn_hook_noised': 0,
            'k_attn_recent': [],
            'k_lin_recent': [],
            'hook_count': 0,
        })

def _sm_major_minor():
    """Compute capability of current CUDA device or (0,0) if CUDA unavailable."""
    if not torch.cuda.is_available():
        return (0, 0)
    d = torch.cuda.current_device()
    return torch.cuda.get_device_capability(d)

def _get_pools():
    """
    Returns per-thread/task pools:
      { 'cpu': {dtype: tensor}, 'gpu': { (device_idx, dtype): tensor } }
    """
    pools = _POOLS_CV.get()
    if pools is None:
        pools = {
            'cpu' : {torch.float32: torch.empty(0, dtype=torch.float32, device='cpu')},
            'gpu' : {}  # {(device.index or 0, dtype): tensor}
        }
        _POOLS_CV.set(pools)
    return pools

def _pool_mode_key() -> str:
    """Pool namespace key by autograd context."""
    return "inf" if torch.is_inference_mode_enabled() else "std"

def _pool_key(device, dtype):
    """Generate a unique key for tensor pools based on device and dtype.
    
    Args:
        device: PyTorch device object
        dtype: PyTorch data type
        
    Returns:
        Tuple representing the pool key
    """
    mode = _pool_mode_key()
    if device.type == "cpu":
        return ('cpu', mode, dtype)
    return (device.index if device.index is not None else 0, mode, dtype)

def _is_inference_tensor(t: torch.Tensor) -> bool:
    is_inference = getattr(t, "is_inference", None)
    if is_inference is None:
        return False
    try:
        return bool(is_inference())
    except Exception:
        return False

@contextmanager
def _mca_pool_write_mode():
    """Create and refill reusable MCA noise pools outside inference mode."""
    try:
        inference_enabled = bool(torch.is_inference_mode_enabled())
    except Exception:
        inference_enabled = False

    if inference_enabled:
        with torch.inference_mode(False):
            yield
    else:
        yield

def _ensure_pool(shape_elems, device, dtype):
    """Ensure a per-thread/task pool tensor exists with at least shape_elems elements."""
    pools = _get_pools()

    key = _pool_key(device, dtype)
    if device.type == "cpu":
        cpu_pools = pools['cpu']
        pool = cpu_pools.get(torch.float32, torch.empty(0, dtype=torch.float32, device='cpu'))
        if pool.numel() < shape_elems or _is_inference_tensor(pool):
            new_elems = max(_POOL_CHUNK, shape_elems)
            pool = torch.empty(new_elems, dtype=torch.float32, device='cpu')
            cpu_pools[key] = pool
        return pool
    else:
        gpu_pools = pools['gpu']
        pool = gpu_pools.get(key, torch.empty(0, dtype=dtype, device=device))
        if pool.numel() < shape_elems or _is_inference_tensor(pool):
            new_elems = max(_POOL_CHUNK, shape_elems)
            pool = torch.empty(new_elems, dtype=dtype, device=device)
            gpu_pools[key] = pool
        return pool

def _next_noise(shape, dtype, device):
    """
    Return a view of a workspace buffer filled with N(0,1) noise of `shape`.
    CPU: noise buffer is float32 (as before)
    GPU: noise buffer is in target dtype (as before)
    Pools are per-thread/task to avoid races.
    """
    # Never mutate inference tensors in-place: generate out-of-place noise.
    # This prevents "inference tensor outside InferenceMode" failures when
    # worker code mixes inference/no_grad paths.
    if torch.is_inference_mode_enabled():
        if device.type == "cpu":
            return torch.randn(shape, dtype=torch.float32, device='cpu')
        return torch.randn(shape, dtype=dtype, device=device)

    need = math.prod(shape)

    with _mca_pool_write_mode():
        if device.type == "cpu":
            pool = _ensure_pool(need, device, torch.float32)
            view = pool[:need]
            view.normal_()
            return view.view(shape)

        pool = _ensure_pool(need, device, dtype)
        view = pool[:need]
        view.normal_()
        return view.view(shape)

def _eps_for_dtype(dt):
    return {
        torch.int8: 4.88e-4,
        torch.float16: 4.88e-4,
        torch.bfloat16: 7.81e-3,
        torch.float32: 1.19e-7,
        torch.float64: 2.22e-16,
    }[dt]

def _fp_noise(t: torch.Tensor, *, k: float, target_dtype: torch.dtype = None) -> torch.Tensor:
    """
    Add zero-mean noise scaled by local ULP proxy.
    CPU: row-wise (as before).
    GPU: row-wise (broadcast) by default for speed; toggle via _GPU_ROWWISE.
    Noise is generated in t.dtype on GPU, fp32 on CPU (matches original semantics).
    """
    if k == 0.0:
        return t

    dtype_for_eps = target_dtype if target_dtype is not None else t.dtype
    eps = _eps_for_dtype(dtype_for_eps)

    if t.device.type == "cpu":
        # row-wise sigma: [*, 1] broadcast across last dim
        sigma = (k * eps) * t.abs().amax(dim=-1, keepdim=True)
        noise = _next_noise(sigma.shape, dtype=torch.float32, device=t.device)
        return t + noise.to(t.dtype) * sigma

    if _GPU_ROWWISE:
        sigma = (k * eps) * t.abs().amax(dim=-1, keepdim=True).to(t.dtype)
        noise = _next_noise(sigma.shape, dtype=t.dtype, device=t.device)
        return t.addcmul(noise, sigma, value=1.0)
    else:
        sigma = (k * eps) * t.abs()
        noise = _next_noise(t.shape, dtype=t.dtype, device=t.device)
        return t.addcmul(noise, sigma, value=1.0)

# ---- patched F.* shims that consult ContextVars -----------------------------
def _noisy_linear_impl(inp, weight, bias=None):
    out = _LINEAR_ORIG(inp, weight, bias)
    if not _MCA_ENABLED.get():
        return out

    # Disable linear noise on CPU (original behavior)
    k_lin = _K_LIN_CV.get()
    if inp.device.type == "cpu":
        k_lin = 0.0

    if _IN_ATTN.get() and k_lin != 0.0:
        out = _fp_noise(out, k=k_lin, target_dtype=_TGT_DTYPE_CV.get())
        _dbg_inc('linear_noised')
        _dbg_push('k_lin_recent', k_lin)
    return out

def _noisy_sdpa_impl(q, k, v, *a, **kw):
    _dbg_inc('sdpa_calls')
    if not _MCA_ENABLED.get():
        return _SDPA_ORIG(q, k, v, *a, **kw)

    # Track "in attention" using a ContextVar (thread/task-local)
    token = _IN_ATTN.set(True)
    # Within an attention forward, we might apply noise either here (SDPA path)
    # or later in the enclosing attention module post-hook. Initialize guard.
    tok_applied = _ATTN_NOISE_APPLIED.set(False)
    try:
        out = _SDPA_ORIG(q, k, v, *a, **kw)
        # k_attn is scaled by sqrt(token_cnt / seq_pivot) (seq_pivot=512 as before)
        token_cnt = q.size(-2)
        seq_pivot = 512.0
        k_eff = _K_ATTN_CV.get() * math.sqrt(max(token_cnt, 1) / seq_pivot)
        out = _fp_noise(out, k=k_eff, target_dtype=_TGT_DTYPE_CV.get())
        # Mark as applied so module hooks don't apply again
        _ATTN_NOISE_APPLIED.set(True)
        _dbg_inc('sdpa_noised')
        _dbg_push('k_attn_recent', k_eff)
        return out
    finally:
        # Reset guard and scope
        _ATTN_NOISE_APPLIED.reset(tok_applied)
        _IN_ATTN.reset(token)

def _install_global_patches_once():
    """
    Replace F.linear and F.scaled_dot_product_attention with thread-safe wrappers
    exactly once for the lifetime of the process.
    """
    global _PATCH_INSTALLED, _LINEAR_ORIG, _SDPA_ORIG
    if _PATCH_INSTALLED:
        return
    with _INSTALL_LOCK:
        if _PATCH_INSTALLED:
            return
        _LINEAR_ORIG = F.linear
        _SDPA_ORIG = F.scaled_dot_product_attention
        F.linear = _noisy_linear_impl
        F.scaled_dot_product_attention = _noisy_sdpa_impl
        _PATCH_INSTALLED = True

# ---- attention-module hooks (catch non-SDPA paths) --------------------------
def _is_attention_module(mod: nn.Module) -> bool:
    try:
        name = mod.__class__.__name__.lower()
    except Exception:
        return False
    # Cover common patterns across HF + custom models
    return ("attn" in name) or ("attention" in name)

def mca_attach_attn_hooks(model: nn.Module) -> None:
    """Attach forward pre/post hooks to attention modules to scope MCA noise.

    - Pre-hook sets in-attention flag and resets the per-scope noise guard.
    - Post-hook applies attention-output noise if SDPA did not already do it.
    """
    if not hasattr(model, "_mca_hook_handles"):
        model._mca_hook_handles = []  # type: ignore[attr-defined]

    def _pre(m: nn.Module, _inputs):
        if not _MCA_ENABLED.get():
            return
        stack = getattr(m, "_mca_tok_stack", None)
        if stack is None:
            stack = []
            setattr(m, "_mca_tok_stack", stack)
        tok_in = _IN_ATTN.set(True)
        tok_applied = _ATTN_NOISE_APPLIED.set(False)
        stack.append((tok_in, tok_applied))

    def _post(m: nn.Module, _inputs, out):
        stack = getattr(m, "_mca_tok_stack", None)
        tok_in = tok_applied = None
        if stack:
            try:
                tok_in, tok_applied = stack.pop()
            except Exception:
                tok_in = tok_applied = None
        try:
            if _MCA_ENABLED.get() and isinstance(out, torch.Tensor):
                # If SDPA path did not already apply attention noise, do it here.
                if not _ATTN_NOISE_APPLIED.get():
                    token_cnt = out.size(-2) if out.dim() >= 2 else 1
                    seq_pivot = 512.0
                    k_eff = _K_ATTN_CV.get() * math.sqrt(max(int(token_cnt), 1) / seq_pivot)
                    out = _fp_noise(out, k=k_eff, target_dtype=_TGT_DTYPE_CV.get())
                    _ATTN_NOISE_APPLIED.set(True)
                    _dbg_inc('attn_hook_noised')
                    _dbg_push('k_attn_recent', k_eff)
            return out
        finally:
            if tok_applied is not None:
                try:
                    _ATTN_NOISE_APPLIED.reset(tok_applied)
                except Exception:
                    pass
            if tok_in is not None:
                try:
                    _IN_ATTN.reset(tok_in)
                except Exception:
                    pass

    # Register hooks on all attention-ish submodules
    hook_count = 0
    for mod in model.modules():
        if _is_attention_module(mod):
            try:
                h1 = mod.register_forward_pre_hook(_pre, with_kwargs=False)
                h2 = mod.register_forward_hook(_post, with_kwargs=False)
                model._mca_hook_handles.append(h1)
                model._mca_hook_handles.append(h2)
                hook_count += 2
            except Exception:
                continue
    try:
        with _MCA_DBG_LOCK:
            _MCA_DBG['hook_count'] = hook_count
    except Exception:
        pass

def mca_detach_attn_hooks(model: nn.Module) -> None:
    """Remove previously attached MCA attention hooks."""
    handles = getattr(model, "_mca_hook_handles", None)
    if not handles:
        return
    for h in handles:
        try:
            h.remove()
        except Exception:
            pass
    model._mca_hook_handles = []  # type: ignore[attr-defined]

# ---- public context manager -------------------------------------------------
@contextmanager
def mca_noise(model: nn.Module, *, k_lin: float = 1.5, k_attn: float = 8.0, target_dtype: torch.dtype = None):
    """
    Thread/task-safe MCA noise context:
      k_lin  : σ-multiplier for FP16/BF16 Q/K/V mat-muls (disabled on CPU)
      k_attn : σ-multiplier for FP32 soft-max row reduction (≈√head_dim)
      target_dtype: EPS scaling dtype override (None = tensor dtype)
    """
    # Install global wrappers once
    _install_global_patches_once()

    # Enable noise for this thread/task and set parameters
    t0 = _MCA_ENABLED.set(True)
    t1 = _K_LIN_CV.set(k_lin)
    t2 = _K_ATTN_CV.set(k_attn)
    t3 = _TGT_DTYPE_CV.set(target_dtype)
    try:
        yield
    finally:
        _TGT_DTYPE_CV.reset(t3)
        _K_ATTN_CV.reset(t2)
        _K_LIN_CV.reset(t1)
        _MCA_ENABLED.reset(t0)

# --- runtime control helpers -------------------------------------------------
_UNCHANGED = object()

def mca_install():
    """Install wrappers once, without enabling noise."""
    _install_global_patches_once()

def mca_get_enabled() -> bool:
    return bool(_MCA_ENABLED.get())

def mca_set_enabled(enabled: bool):
    """Sticky (for this thread/task) until changed again."""
    _install_global_patches_once()
    _MCA_ENABLED.set(bool(enabled))

def mca_set_params(*, k_lin=_UNCHANGED, k_attn=_UNCHANGED, target_dtype=_UNCHANGED):
    """
    Sticky (for this thread/task). Only updates params you pass.
    Use k_lin=0.0 to disable linear noise while keeping attention noise.
    """
    _install_global_patches_once()
    if k_lin is not _UNCHANGED:     _K_LIN_CV.set(k_lin)
    if k_attn is not _UNCHANGED:    _K_ATTN_CV.set(k_attn)
    if target_dtype is not _UNCHANGED: _TGT_DTYPE_CV.set(target_dtype)

def mca_get_params() -> dict:
    """Return current thread/task MCA parameters and enable flag."""
    return {
        'enabled': bool(_MCA_ENABLED.get()),
        'k_lin': float(_K_LIN_CV.get()),
        'k_attn': float(_K_ATTN_CV.get()),
        'target_dtype': _TGT_DTYPE_CV.get(),
    }

@contextmanager
def mca_enabled(enabled: bool = True):
    """Temp toggle (auto-restores previous value)."""
    _install_global_patches_once()
    tok = _MCA_ENABLED.set(bool(enabled))
    try:
        yield
    finally:
        _MCA_ENABLED.reset(tok)

@contextmanager
def mca_params(*, k_lin=_UNCHANGED, k_attn=_UNCHANGED, target_dtype=_UNCHANGED):
    """Temp param override (auto-restores)."""
    _install_global_patches_once()
    toks = []
    if k_lin is not _UNCHANGED:      toks.append((_K_LIN_CV, _K_LIN_CV.set(k_lin)))
    if k_attn is not _UNCHANGED:     toks.append((_K_ATTN_CV, _K_ATTN_CV.set(k_attn)))
    if target_dtype is not _UNCHANGED: toks.append((_TGT_DTYPE_CV, _TGT_DTYPE_CV.set(target_dtype)))
    try:
        yield
    finally:
        for cv, tok in reversed(toks):
            cv.reset(tok)

@contextmanager
def mca_active(*, k_lin=_UNCHANGED, k_attn=_UNCHANGED, target_dtype=_UNCHANGED):
    """Enable MCA with temporary parameter overrides, then restore thread state."""
    with mca_enabled(True):
        with mca_params(k_lin=k_lin, k_attn=k_attn, target_dtype=target_dtype):
            yield


# ----------------------------------------------------------------- #
#                        Generic Batched Helpers
# ----------------------------------------------------------------- #
def build_id_sorted_cdfs_vectorized(
    idx_sent_batch: torch.Tensor,     # [B, M] token ids, -1 is padding
    probs_base: torch.Tensor,         # [B, M] probs for the "base" (already masked) case
    expected_tokens: torch.Tensor,    # [B]
    u_batch: torch.Tensor,            # [B], uniform samples
    check_borderline: torch.Tensor,   # [B] bool
    # the following are for recomputing h/l borderline variants in a vectorized way
    vals_sorted_raw: torch.Tensor,    # [B, M] raw sorted-by-value logits BEFORE base mask
    idx_sorted: torch.Tensor,         # [B, M] indices that map sorted-by-value -> original columns
    mask_p_h_batch: torch.Tensor,     # [B, M] bool
    mask_p_l_batch: torch.Tensor,     # [B, M] bool
    atol: float = 1e-7,
):
    """
    Returns a dict of batched tensors:
        'pos'         : [B]  (int64, -1 if expected token not present)
        'lower'       : [B]  (float)
        'upper'       : [B]  (float)
        'cdf'         : [B, M]  (float; valid tail length per row is given by 'valid_counts')
        'sorted_idx'  : [B, M]  (int; padded with -1 at the tail)
        'valid_counts': [B] (number of valid tokens in each row)
    All heavy computation is vectorized on the current device.
    """
    device = idx_sent_batch.device
    B, M = idx_sent_batch.shape

    # 0) Valid mask & push padding to the END when sorting by token id
    valid_mask = (idx_sent_batch != -1)  # [B, M]
    pad_large = torch.iinfo(idx_sent_batch.dtype).max
    idx_for_sort = torch.where(valid_mask, idx_sent_batch, torch.full_like(idx_sent_batch, pad_large))
    order_by_id = torch.argsort(idx_for_sort, dim=1, descending=False)             # [B, M]
    valid_sorted_mask = torch.gather(valid_mask, 1, order_by_id)                   # [B, M]
    sorted_idx = torch.gather(idx_sent_batch, 1, order_by_id)                      # [B, M]
    # standardize padding to -1 in output
    sorted_idx = torch.where(valid_sorted_mask, sorted_idx, torch.full_like(sorted_idx, -1))

    # 1) Base (already-masked) distribution → sorted by token id
    sorted_probs_base = torch.gather(probs_base, 1, order_by_id) * valid_sorted_mask
    cdf_base = torch.cumsum(sorted_probs_base, dim=1)  # [B, M], trailing part flat where mask is 0

    # 2) Build h/l borderline distributions in one go (vectorized)
    # vals_* are in "sorted-by-value" space; we scatter them back to original columns
    neg_inf = torch.tensor(float('-inf'), device=device)
    vals_h = vals_sorted_raw.masked_fill(mask_p_h_batch, neg_inf)
    vals_l = vals_sorted_raw.masked_fill(mask_p_l_batch, neg_inf)

    # Back to original column order
    tmp_logits_h = torch.zeros_like(vals_h).scatter(-1, idx_sorted, vals_h)
    tmp_logits_l = torch.zeros_like(vals_l).scatter(-1, idx_sorted, vals_l)

    # Softmax (stay on device)
    probs_h = F.softmax(tmp_logits_h, dim=-1)
    probs_l = F.softmax(tmp_logits_l, dim=-1)

    # Sort both by token id and cumsum (masked)
    sorted_probs_h = torch.gather(probs_h, 1, order_by_id) * valid_sorted_mask
    sorted_probs_l = torch.gather(probs_l, 1, order_by_id) * valid_sorted_mask

    cdf_h = torch.cumsum(sorted_probs_h, dim=1)
    cdf_l = torch.cumsum(sorted_probs_l, dim=1)

    # 3) Locate expected token positions without per-example nonzero/loop
    #    eq map in sorted-by-id space
    eq = (sorted_idx == expected_tokens.view(-1, 1)) & valid_sorted_mask  # [B, M]
    has_pos = eq.any(dim=1)
    # argmax returns 0 if all False; fix with has_pos
    pos = torch.where(has_pos, torch.argmax(eq.to(torch.int64), dim=1), torch.full((B,), -1, device=device, dtype=torch.long))

    # Helper to gather lower/upper from a given CDF
    def gather_bounds(cdf: torch.Tensor):
        # upper = cdf[b, pos[b]] ; lower = cdf[b, pos[b]-1] or 0 if pos=0; 0 if pos=-1 (handled below)
        pos_clamped = torch.clamp(pos, min=0)                         # [-1 -> 0]
        upper = cdf.gather(1, pos_clamped.view(-1, 1)).squeeze(1)     # [B]
        lower_idx = torch.clamp(pos - 1, min=0)
        lower = cdf.gather(1, lower_idx.view(-1, 1)).squeeze(1)
        # fix edge cases
        upper = torch.where(pos >= 0, upper, torch.zeros_like(upper))
        lower = torch.where((pos > 0), lower, torch.zeros_like(lower))
        return lower, upper

    lower_b, upper_b = gather_bounds(cdf_base)
    lower_h, upper_h = gather_bounds(cdf_h)
    lower_l, upper_l = gather_bounds(cdf_l)

    # 4) Borderline selection (vectorized)
    #    Default to base; if check_borderline & not in (lower, upper], try h; if still not, use l.
    u = u_batch.view(-1)  # [B]
    within_b = (u > lower_b) & (u <= upper_b)
    need_fix = check_borderline & (~within_b)

    within_h = (u > lower_h) & (u <= upper_h)
    take_h = need_fix & within_h
    # if need_fix and not within_h → fallback to l
    take_l = need_fix & (~within_h)

    # Row-wise select the cdf and bounds
    # Start from base
    cdf_sel = cdf_base
    lower_sel = lower_b
    upper_sel = upper_b

    # Apply h where indicated
    if take_h.any():
        mask = take_h.view(-1, 1)
        cdf_sel = torch.where(mask, cdf_h, cdf_sel)
        lower_sel = torch.where(take_h, lower_h, lower_sel)
        upper_sel = torch.where(take_h, upper_h, upper_sel)

    # Apply l where indicated (remaining rows)
    if take_l.any():
        mask = take_l.view(-1, 1)
        cdf_sel = torch.where(mask, cdf_l, cdf_sel)
        lower_sel = torch.where(take_l, lower_l, lower_sel)
        upper_sel = torch.where(take_l, upper_l, upper_sel)

    # 5) Valid counts to help consumers slice per-row if they want tight 1D tensors
    valid_counts = valid_sorted_mask.sum(dim=1)  # [B]

    return pos, lower_sel, upper_sel, cdf_sel, sorted_idx, valid_counts
    # return {
    #     'pos': pos,                               # [B] int64
    #     'lower': lower_sel,                       # [B] float
    #     'upper': upper_sel,                       # [B] float
    #     'cdf': cdf_sel,                           # [B, M] float (masked tail stays flat)
    #     'sorted_idx': sorted_idx,                 # [B, M] int (tail is -1)
    #     'valid_counts': valid_counts,             # [B]
    # }

def format_results_legacy(batched, detach_to_cpu: bool = True):
    """
    Optional: converts batched tensors into your original list-of-dicts structure.
    Only formatting happens in Python; numeric work stays vectorized.
    """
    pos = batched['pos']
    lower = batched['lower']
    upper = batched['upper']
    cdf = batched['cdf']
    sorted_idx = batched['sorted_idx']
    valid_counts = batched['valid_counts']

    B = pos.shape[0]
    out = []
    for b in range(B):
        n = int(valid_counts[b].item())
        cdf_b = cdf[b, :n]
        idx_b = sorted_idx[b, :n]
        if detach_to_cpu:
            cdf_b = cdf_b.detach().cpu()
            idx_b = idx_b.detach().cpu()
        out.append({
            'pos': int(pos[b].item()),
            'lower': float(lower[b].item()),
            'upper': float(upper[b].item()),
            'cdf': cdf_b,                # 1D tensor of length n
            'sorted_idx': idx_b,         # 1D tensor of length n
        })
    return out

@torch.no_grad()
def dedupe_keep_max_dense(idx_raw: torch.Tensor,
                          logit_raw: torch.Tensor,
                          pad_id: int = -1,
                          pad_val: float = float('-inf')):
    """
    Dense case: idx_raw/logit_raw are [B, K0] with *no* padding entries.
    Merges duplicates per row by max(logit), left-packs uniques to the front,
    and fills the tail with pad_id / pad_val. Returns (idx, logits, n_uniq).
    """
    B, K0 = idx_raw.shape

    # 1) Sort by token id so duplicates are consecutive
    order = torch.argsort(idx_raw, dim=1, stable=True)             # [B, K0]
    ids   = torch.gather(idx_raw,   1, order)                      # [B, K0]
    vals  = torch.gather(logit_raw, 1, order)                      # [B, K0]

    # 2) Group boundaries where id changes
    change = torch.ones_like(ids, dtype=torch.bool)
    change[:, 1:] = ids[:, 1:] != ids[:, :-1]                      # [B, K0]
    gid = change.cumsum(dim=1) - 1                                 # 0..(G-1)

    # 3) Reduce logits by group (amax), and write the group token id
    out_logits = torch.full_like(vals, pad_val)
    out_logits.scatter_reduce_(1, gid, vals, reduce="amax", include_self=False)

    out_idx = torch.full_like(ids, pad_id)
    out_idx.scatter_(1, gid, ids)

    # 4) Number of unique tokens per row (how many left-packed positions are “real”)
    n_uniq = change.sum(dim=1)                                     # [B]

    return out_idx, out_logits, n_uniq


# --------------------------------------------------------------------------- #
#                          ProofVerifier Class                                #
# --------------------------------------------------------------------------- #

class ModelPlacementError(RuntimeError):
    """Raised when a model cannot be placed on the GPU.

    We refuse to silently run an 8B forward pass on CPU (which turns a ~50s
    verify into ~700s and trips probe timeouts). Callers should surface this
    as a retryable execution error so the job is failed fast / re-routed to a
    worker that can hold the model, instead of blocking on CPU inference.
    """


class ProofVerifier:
    """Statistical Proof-of-Work verifier with comprehensive validation.
    
    This class provides complete verification of cryptographic proofs generated by
    neural network inference. It validates statistical properties, computational
    correctness, and cryptographic integrity of submitted proofs.
    
    Key features:
    - Monte Carlo Arithmetic (MCA) noise injection for verification
    - Statistical validation using multivariate continuous distributions
    - Model loading and caching with IPFS support
    - Comprehensive logging and error reporting
    - Vectorized batch processing for efficiency
    
    Attributes:
        device (str): Computation device ('cuda' or 'cpu')
        window_size (int): Context window size for processing
        cache_dir (str): Directory for model caching
        perform_smell_test (bool): Whether to perform initial validation
        use_flash_attn (bool): Use flash attention optimization
        use_kv_cache (bool): Use key-value caching
        logger (VerificationLogger): Structured logger instance
        model (PreTrainedModel): Currently loaded model
        tokenizer: Model tokenizer instance
        initialised (bool): Whether verifier is initialized with proof
    """
    
    # --------------------------------------------------------------------- #
    #                                Init                                  #
    # --------------------------------------------------------------------- #    
    
    def __init__(self,               
                 use_flash_attn: bool = False,
                 use_kv_cache : bool = False,
                 window_size: int = 256,
                 logger: Optional[VerificationLogger] = None):
        self.device: str = "cuda" if torch.cuda.is_available() else "cpu"
        self.window_size = window_size
        self.cache_dir: str = CACHE_DIR
        self.perform_smell_test = SMELL_TEST

        self.use_flash_attn = use_flash_attn
        self.use_kv_cache = use_kv_cache

        mca_install()

        # Setup logger
        if logger is None:
            self.logger = create_logger(
                log_level= logging.DEBUG,
                name="proof_verifier",
                log_file="proof_verifier.log",
                reports_dir="/data/pow_proofs/verification_reports"
            )
        else:
            self.logger = logger

        # Model registry for hot-caching (key -> PreTrainedModel), LRU-ordered
        # (oldest first). Unlike the old design this holds *parked* models on
        # the GPU whenever VRAM allows, so switching between a small set of
        # models (e.g. 0.6B + 8B) is a pointer swap, not a 16GB CPU<->GPU copy.
        # Models are only demoted to CPU / dropped under real VRAM pressure
        # (see _reclaim_gpu).
        self._model_registry: "OrderedDict[str, PreTrainedModel]" = OrderedDict()
        # Headroom (bytes) to keep free on the active GPU for activations/KV
        # after the active model is resident. Parked co-resident models are
        # evicted to preserve this margin. Tune per-GPU via env.
        self._gpu_activation_reserve_bytes = int(
            float(os.getenv("POW_GPU_ACTIVATION_RESERVE_GB", "8.0")) * (1 << 30)
        )
        # Allow CPU inference fallback (dev/CPU-only hosts). In production this
        # stays False so we never silently run an 8B forward on CPU.
        self._allow_cpu_inference = os.getenv(
            "POW_ALLOW_CPU_INFERENCE", "false"
        ).strip().lower() in {"1", "true", "yes"}
        # Inline smell-stats bake (the 21-52 min _collect_logits_stats pass) is
        # OFF on the load/verify path — the full-verify path never reads
        # self.stats, and the quick/share path is pre-baked by the warmup loop.
        # The warmup path sets this True to force the bake.
        self._allow_inline_stats_bake = False
        self.model: Optional[PreTrainedModel] = None
        self.tokenizer = None
        self.initialised = False     
        self.stats_loaded_filename = ''                
        self.stats_loaded = False       
        self.stats = None

        self.logger.info("ProofVerifier initialized", extra={
            'device': self.device,
            'window_size': window_size,
            'use_flash_attn': use_flash_attn,
            'use_kv_cache': use_kv_cache
        })
        
    def initialise(self, proof):
        try:
            self.proof: dict = proof
            # ------------------------------------------------------------------ #
            #                             Boot‑strap                             #
            # ------------------------------------------------------------------ #
            self._validate_proof_structure()
            self._load_io()
            self.initialised = True
            self.logger.debug("ProofVerifier successfully initialized with proof")
            
            # maj, minr = _sm_major_minor()
            # if self.precision == 'bf16' and (maj, minr) < (8, 0):
            #     self.logger.warning("BF16 requested on SM<80 → forcing fp16 for speed.")
            #     self.stated_precision = 'bf16'
            #     self.precision = 'fp16'
            #     self.dtype = (
            #         torch.float16  if self.precision == 'fp16' else
            #         torch.bfloat16 if self.precision == 'bf16' else
            #         torch.int8     if self.precision == 'int8' else
            #         torch.float32
            #     )                
            # else:
            #     self.stated_precision = self.precision
            
            self.stated_precision = self.precision
            self.stated_dtype = _precision_replay_dtype(
                self.stated_precision,
                proof_config_diff=getattr(self, "model_config_diff", {}),
            )

        except Exception as e:
            self.logger.error(
                f"Failed to initialize ProofVerifier: {e}",
                failure_type="initialization_failure",
                proof_data={'proof_keys': list(proof.keys()) if isinstance(proof, dict) else None}
            )
            raise
            
    # --------------------------------------------------------------------- #
    #                        Validation & I/O helpers                       #
    # --------------------------------------------------------------------- #
                
    def _validate_proof_structure(self):
        """Validate that proof contains all required fields."""
        required_fields = [
        'tick','target','vdf','hash','block_hash','header_prefix',
        'chosen_tokens','chosen_probs','topk_logits','topk_indices',
        'sampling_u','softmax_normalizers','logsumexp_stats','is_solution',
        'timestamp','model_identifier','compute_precision','prompt_tokens',
        'pad_mask','temperature','top_p','top_k','repetition_penalty'
        ]
        
        missing = [f for f in required_fields if f not in self.proof]
        if missing:
            raise ValueError(f"Missing required proof fields: {missing}")
        else:
            self.logger.debug("  ✅ Proof structure correct")   

    def _load_io(self):
        """Load I/O components from proof."""
        try:
            p = self.proof

            # Model identification
            self.model_name, self.commit_hash = p['model_identifier'].split('@')
            self.precision = p.get('compute_precision', 'fp16')
            self.model_config_diff = p.get('model_config_diff', {})
            self.dtype = _precision_replay_dtype(
                self.precision,
                proof_config_diff=self.model_config_diff,
            )

            # Bulk convert lists/arrays to tensors
            # Use as_tensor for zero-copy when possible
            self.prompt_tokens   = torch.as_tensor(p['prompt_tokens'],   dtype=torch.long,  device=self.device)
            self.chosen_tokens   = torch.as_tensor(p['chosen_tokens'],   dtype=torch.long,  device=self.device)
            self.pad_mask        = torch.as_tensor(p['pad_mask'],        dtype=torch.bool,  device=self.device)
            self.expected_probs  = torch.as_tensor(p['chosen_probs'],     dtype=torch.float32, device=self.device)
            self.expected_u      = torch.as_tensor(p['sampling_u'],      dtype=torch.float32, device=self.device)
            self.expected_norm   = torch.as_tensor(p['softmax_normalizers'], dtype=torch.float32, device=self.device)
            self.expected_lse    = torch.as_tensor(p['logsumexp_stats'],  dtype=torch.float32, device=self.device)

            # Top-k data (keep as Python lists or pre-convert later if needed)
            self.expected_topk_logits  = p['topk_logits']
            self.expected_topk_indices = p['topk_indices']

            # Hyperparameters
            self.temperature        = p['temperature']
            self.top_p              = p['top_p']
            self.top_k              = p['top_k']
            self.repetition_penalty = p['repetition_penalty']

            # Load logit correlation table
            filename = "config/correl.npy"
            self.logit_correll = torch.as_tensor(
                np.load(filename, allow_pickle=False),
                dtype=torch.float32,
                device=self.device
            )

            # Move to model device if model already loaded
            if hasattr(self, 'model') and self.model is not None:
                model_dev = next(self.model.parameters()).device
                for attr in [
                    'prompt_tokens', 'chosen_tokens', 'pad_mask',
                    'expected_probs', 'expected_u', 'expected_norm',
                    'expected_lse', 'logit_correll'
                ]:
                    setattr(self, attr, getattr(self, attr).to(model_dev))
                self.device = str(model_dev)

            self.logger.debug(f"Loaded proof – {len(self.chosen_tokens)} generated tokens")

            # Optionally load smell-test stats once
            if self.perform_smell_test and not hasattr(self, '_stats_loaded'):
                try:
                    self._load_stats()
                    self._stats_loaded = True
                except:
                    self.logger.warning("❌ Unable to load stats for smell test – will be computed at model loading")
                    self.stats = None
        except Exception as e:
            self.logger.error(
                f"Failed to load I/O components: {e}",
                failure_type="io_loading_failure",
                proof_data={'model_identifier': p.get('model_identifier')}
            )
            raise

    # --------------------------------------------------------------------- #
    #                            Reload logic                                #
    # --------------------------------------------------------------------- #

    def reload(self, new_proof: dict, force_model_reload: bool = False) -> None:
        """Swap in a new proof and handle model (re)loading accordingly."""
        # Store old model identifier BEFORE updating anything
        old_identifier = getattr(self, '_current_model_identifier', None)

        # Update proof first
        self.proof = new_proof

        # Validate structure before proceeding
        self._validate_proof_structure()

        # Extract new model info and update model_name/commit_hash
        new_identifier = new_proof["model_identifier"]
        self.model_name, self.commit_hash = new_identifier.split('@')

        same_model = new_identifier == old_identifier

        if not same_model:
            self.logger.debug(f"  🔄 Different model detected: {old_identifier} → {new_identifier}")

        # Clear KV caches and any model-specific cached data
        for attr in list(self.__dict__.keys()):
            if attr.startswith('_kv_cache') or attr.startswith('_cached_ctx'):
                delattr(self, attr)

        # Clear any cached statistics if model changes
        if not same_model and hasattr(self, 'stats'):
            delattr(self, 'stats')

        # Always reload IO after model operations
        self._load_io()

        # Decision logic:
        if not same_model:
            # Different model - park current (keep it hot on GPU if VRAM
            # allows) and load/reuse the new one.
            if self.model is not None:
                self._park_current_model()
            # Check if new model is already resident
            if new_identifier in self._model_registry:
                self.logger.debug(f"  ✅ New model {new_identifier} found in registry")
            else:
                self.logger.debug(f"  ⚠️  New model {new_identifier} not in registry - will need to download")
            self._load_or_reuse_model()
        elif force_model_reload:
            # Force reload of same model — discard the current instance
            # entirely (do NOT park/reuse) so we truly reload fresh.
            self.logger.debug(f"  🔄 Force reloading model: {new_identifier}")
            self._discard_current_model()
            self._model_registry.pop(new_identifier, None)
            self._load_or_reuse_model(force=True)
        elif self.model is None:
            # Same model but not currently loaded
            self.logger.debug(f"  ⚠️  Model {new_identifier} not currently loaded")
            self._load_or_reuse_model()
        else:
            # Same model already loaded
            self.logger.debug(f"  ✅ Model already loaded on {self.device}")

        # Update current model identifier tracker
        self._current_model_identifier = new_identifier
 
    # --------------------------------------------------------------------- #
    #                           Model handling                               #
    # --------------------------------------------------------------------- #

    def _clear_kv_caches(self) -> None:
        """Drop any per-model KV / context caches hanging off the instance."""
        for attr in list(self.__dict__.keys()):
            if attr.startswith('_kv_cache') or attr.startswith('_cached_ctx'):
                delattr(self, attr)

    def _park_current_model(self) -> None:
        """Retire the active model into the registry, keeping it HOT.

        Unlike the old _stash_current_model this does NOT move the model to
        CPU — it leaves it exactly where it is (ideally the GPU) and simply
        moves the reference into the LRU registry so a later switch back is a
        pointer swap. VRAM is only reclaimed lazily, on demand, by
        _reclaim_gpu when a new model actually needs the room.
        """
        if not self.model:
            return

        identifier = getattr(self, "_current_model_identifier", None)
        if not identifier:
            # Untracked model — we can't key it for reuse, so drop it.
            self.logger.debug("  ⚠️  Current model identifier not tracked — discarding")
            self._discard_current_model()
            return

        self._clear_kv_caches()

        if identifier in self._model_registry:
            # Already parked (shouldn't normally happen); keep the parked copy
            # and drop the duplicate active reference.
            self.model = None
            return

        self._model_registry[identifier] = self.model
        self._model_registry.move_to_end(identifier)  # most-recently-used
        self.model = None

    def _discard_current_model(self) -> None:
        """Free the active model entirely (GPU memory reclaimed)."""
        if self.model is None:
            return
        self._clear_kv_caches()
        del self.model
        self.model = None
        self._ensure_clean_switch()

    def _evict_registry_entry(self, identifier: str) -> None:
        """Drop a parked model from the registry, freeing its memory."""
        model = self._model_registry.pop(identifier, None)
        if model is None:
            return
        try:
            del model
        finally:
            self._ensure_clean_switch()

    def _reclaim_gpu(self, target_free_bytes: int) -> None:
        """Evict LRU *parked* GPU-resident models until `target_free_bytes` is
        free on some GPU, or nothing is left to evict.

        Parked models are demoted to CPU RAM when it fits (so switching back is
        a cheap copy, not a disk reload), otherwise dropped. The active model
        (self.model) is never touched here.
        """
        if not torch.cuda.is_available() or target_free_bytes <= 0:
            return

        def _max_free() -> int:
            mem = self._get_all_gpu_mem()
            return max(mem.values()) if mem else 0

        # Iterate LRU order (oldest first). Only GPU-resident parked models
        # help; CPU-parked ones already hold no GPU memory.
        for identifier in list(self._model_registry.keys()):
            if _max_free() >= target_free_bytes:
                return
            model = self._model_registry.get(identifier)
            if model is None:
                continue
            try:
                on_gpu = next(model.parameters()).device.type == "cuda"
            except StopIteration:
                on_gpu = False
            if not on_gpu:
                continue

            model_bytes = self._estimate_model_size(model)
            cpu_free = self._get_available_cpu_mem()
            if model_bytes < cpu_free - (512 << 20):
                self.logger.debug(f"  🔄 Demoting parked model {identifier} GPU→CPU to reclaim VRAM")
                model.to("cpu")
            else:
                self.logger.debug(f"  🗑️  Dropping parked model {identifier} (CPU RAM full) to reclaim VRAM")
                self._model_registry.pop(identifier, None)
                del model
            self._ensure_clean_switch()

    def _place_model_on_gpu(self, model: "PreTrainedModel", est_bytes: int) -> None:
        """Move `model` onto the best-fit GPU, evicting parked models if needed.

        Raises ModelPlacementError if the model cannot be made GPU-resident and
        CPU inference is not explicitly permitted.
        """
        if not torch.cuda.is_available():
            if not self._allow_cpu_inference:
                raise ModelPlacementError("CUDA unavailable and CPU inference disabled")
            self.device = "cpu"
            return

        needed = int(est_bytes * 1.05)
        self._reclaim_gpu(needed)

        gpu_mem = self._get_all_gpu_mem()
        best_gpu = max(
            (idx for idx, free in gpu_mem.items() if free > needed),
            key=gpu_mem.get,
            default=None,
        )
        if best_gpu is None:
            if self._allow_cpu_inference:
                self.logger.warning("  ⚠️  No GPU fits model; running on CPU (POW_ALLOW_CPU_INFERENCE)")
                model.to("cpu")
                self.device = "cpu"
                return
            raise ModelPlacementError(
                f"model needs ~{needed >> 20}MB but no GPU has room after eviction"
            )

        self.logger.debug(f"  🚀 Placing model on cuda:{best_gpu}")
        model.to(f"cuda:{best_gpu}")
        self.device = f"cuda:{best_gpu}"
        self._clear_kv_caches()

    def _trim_coresident_models(self) -> None:
        """Preserve an activation-memory margin on the active GPU by evicting
        parked co-resident models that would eat into the reserve."""
        if not torch.cuda.is_available():
            return
        self._reclaim_gpu(self._gpu_activation_reserve_bytes)

    def _assert_gpu_resident(self) -> None:
        """Fail closed if the active model ended up on CPU when it shouldn't."""
        if self.model is None or self._allow_cpu_inference or not torch.cuda.is_available():
            return
        try:
            dev = next(self.model.parameters()).device
        except StopIteration:
            return
        if dev.type != "cuda":
            raise ModelPlacementError(
                f"model {getattr(self, '_current_model_identifier', '?')} "
                f"is on {dev}, refusing CPU inference"
            )

    def _load_or_reuse_model(self, force: bool = False) -> None:
        """Load model, trying to reuse cached instance if present."""
        identifier = f"{self.model_name}@{self.commit_hash}"

        # Check if model is actually in registry AND not forced reload
        if not force and identifier in self._model_registry:
            self.logger.debug(f"  ♻️  Reusing resident model {identifier}")
            # Pop from registry and make it the active model
            self.model = self._model_registry.pop(identifier)

            # Ensure tokenizer is loaded before moving model
            self._ensure_tokenizer()

            # Promote to GPU if it was demoted to CPU under prior VRAM pressure;
            # otherwise it is already GPU-resident — just a pointer swap.
            dev = next(self.model.parameters()).device
            if dev.type == "cpu" and torch.cuda.is_available():
                self._place_model_on_gpu(self.model, self._estimate_model_size(self.model))
            else:
                self.device = str(dev)

            # Update tracker
            self._current_model_identifier = identifier

            # Refuse to run inference on CPU (turns 50s into 700s).
            self._assert_gpu_resident()

            # Ensure MCA attention hooks are attached (idempotent)
            try:
                mca_detach_attn_hooks(self.model)
                mca_attach_attn_hooks(self.model)
            except Exception as _e:
                self.logger.debug(f"MCA hooks attach skipped (reuse): {_e}")

            # Keep an activation-memory margin free on the active GPU.
            self._trim_coresident_models()

            # Smell-test stats: load if pre-baked, never bake inline here.
            self._load_or_defer_stats()
            return

        # No cached model found - need to load fresh
        self.logger.debug(f"  📥 Loading fresh model {identifier} (force={force}, in_registry={identifier in self._model_registry})")
        if not self._load_model():
            raise RuntimeError(
                f"Failed to load model {identifier} (model object unavailable or dtype mismatch)"
            )
        # Update tracker after successful load
        self._current_model_identifier = identifier

    def _load_or_defer_stats(self) -> None:
        """Load pre-baked smell-test stats if present; otherwise defer.

        The full-verify path never reads ``self.stats`` (only the quick/share
        smell path does), and the warmup loop is responsible for pre-baking the
        ``_stats.h5`` for every advertised model. So on the hot verify path we
        NEVER run the 21-52 min ``_collect_logits_stats`` bake inline — we load
        the cached stats or set ``self.stats = None`` and move on. Only the
        warmup path (``self._allow_inline_stats_bake = True``) pays the bake.
        """
        try:
            self._load_stats()
            return
        except Exception:
            pass

        if self.perform_smell_test and self._allow_inline_stats_bake:
            self.logger.warning(
                "Baking smell-test stats inline for %s@%s (warmup path) …",
                self.model_name, self.commit_hash,
            )
            self.stats = self._collect_logits_stats(
                seq_length=1, total_tokens=1_000_000,
                batch_size=40, inert_topk=2000, chunk_size=256,
            )
            self._save_stats(self.stats, force=True)
        else:
            if self.perform_smell_test:
                self.logger.warning(
                    "Smell-test stats missing for %s@%s — deferring bake to warmup "
                    "(full verify does not require stats)",
                    self.model_name, self.commit_hash,
                )
            self.stats = None

    def _estimate_model_size(self, model: Optional[PreTrainedModel] = None) -> int:
        """Estimate model size in bytes: #params × dtype bytes."""
        bytes_per_elem = _DTYPE_BYTES[self.dtype]

        if model is not None:
            numel = sum(p.numel() for p in model.parameters())
            return numel * bytes_per_elem

        # Estimate without loading full model
        config = AutoConfig.from_pretrained(
            self.model_name, revision=self.commit_hash, trust_remote_code=True
        )

        # Prefer explicit parameter count if available
        if hasattr(config, "num_parameters") and config.num_parameters:
            numel = int(config.num_parameters)
        else:
            # Rough approximation for decoder‑only transformer
            hs = getattr(config, "hidden_size", 0)
            nl = getattr(config, "num_hidden_layers", getattr(config, "n_layer", 0))
            vocab = getattr(config, "vocab_size", 0)
            numel = 12 * hs * hs * nl + vocab * hs
        return numel * bytes_per_elem * 1.15

    def _get_model_device(self) -> str:
        """Get the device where model currently resides."""
        if self.model is None:
            return "none"
        try:
            return str(next(self.model.parameters()).device)
        except StopIteration:
            return "cpu"

    @staticmethod
    def _get_available_cpu_mem() -> int:
        """Return free CPU memory in bytes."""
        return psutil.virtual_memory().available

    @staticmethod    
    def _get_all_gpu_mem() -> Dict[int, int]:
        """Return dict {gpu_idx: free_bytes}. Empty if CUDA unavailable."""
        if not torch.cuda.is_available():
            return {}
        mem = {}
        for idx in range(torch.cuda.device_count()):
            try:
                free, _ = torch.cuda.mem_get_info(idx)
                # Get reserved and allocated memory
                reserved = torch.cuda.memory_reserved(idx)
                allocated = torch.cuda.memory_allocated(idx)
                # Actual free memory includes reserved but unallocated space
                actual_free = free + (reserved - allocated)
                mem[idx] = actual_free
            except RuntimeError:
                continue
        return mem

    def _ensure_clean_switch(self) -> None:
        """Release freed GPU allocations so mem_get_info reflects reality.

        Deliberately lightweight — a synchronize + empty_cache is enough to let
        the caching allocator return freed blocks. The old version also looped
        reset_peak_memory_stats over every device and slept 100ms, which added
        pure latency to every model operation for no functional benefit.
        """
        if torch.cuda.is_available():
            torch.cuda.synchronize()
            torch.cuda.empty_cache()

    # ----------------------------------------------------------------- #
    #                        Model Load / Promote                       #
    # ----------------------------------------------------------------- #

    def _promote_cached_model_to_gpu(self) -> None:
        """Move cached model to GPU if memory allows."""
        if not (torch.cuda.is_available() and self.model):
            return

        # Clear caches and force memory release first
        torch.cuda.empty_cache()
        torch.cuda.synchronize()

        gpu_mem = self._get_all_gpu_mem()
        if not gpu_mem:
            return

        model_bytes = self._estimate_model_size(self.model)
        best_gpu = max(
            (idx for idx, free in gpu_mem.items() if free > model_bytes * 1.2),
            key=gpu_mem.get,
            default=None,
        )

        if best_gpu is not None:
            self.logger.debug(f"  🚀 Moving cached model to cuda:{best_gpu} …")
            self.model.to(f"cuda:{best_gpu}")
            self.device = f"cuda:{best_gpu}"

            # Clear any cached KV states after moving
            for attr in list(self.__dict__.keys()):
                if attr.startswith('_kv_cache') or attr.startswith('_cached_ctx'):
                    delattr(self, attr)
            return

        total_gpu_free = sum(gpu_mem.values())
        if total_gpu_free > model_bytes * 1.2 and torch.cuda.device_count() > 1:
            self.logger.debug("  🚀 Sharding cached model across multiple GPUs …")
            self.model.cuda()  # let PyTorch distribute
            self.device = "cuda"
        else:
            # Keep on CPU but update device
            self.device = "cpu"
            self.logger.debug("  ⚠️  Keeping model on CPU (insufficient GPU memory)")

    def _load_model(self) -> bool:
        """Load the model with resource‑aware placement and IPFS fallback."""
        config = AutoConfig.from_pretrained(
            self.model_name,
            revision=self.commit_hash,
            trust_remote_code=True,
        )
        if self.precision == "fp8":
            if not _config_is_fp8_quantized(config):
                self.logger.debug(
                    "❌ Proof precision is fp8 but model config has no FP8 "
                    "quantization marker"
                )
                return False
            self.dtype = _resolve_fp8_replay_dtype(
                proof_config_diff=getattr(self, "model_config_diff", {}),
                hf_config=config,
                fallback=self.dtype if self.dtype != torch.float32
                else torch.bfloat16,
            )
            dtype = self.dtype
            if _is_fp8_dtype(dtype):
                self.logger.debug(
                    "❌ Refusing torch_dtype=%s for FP8 proof replay; "
                    "FP8 proofs must replay with bf16/fp16/fp32 activations "
                    "and quantization config-driven FP8 kernels.",
                    dtype,
                )
                return False
            self.stated_dtype = dtype
            dtype_original = inspect_model_dtype(
                self.model_name, self.commit_hash, verbose=False)
            if dtype_original is not None and not _is_fp8_dtype(dtype_original):
                self.logger.warning(
                    "FP8 proof replay: first checkpoint tensor dtype is %s, "
                    "but config declares FP8 quantization. Continuing with "
                    "activation replay dtype %s.",
                    dtype_original, dtype,
                )
        else:
            dtype = self.dtype
            dtype_strg = _precision_replay_dtype(self.precision)

            # Model precision check
            dtype_original = inspect_model_dtype(self.model_name,self.commit_hash)
            if dtype_strg != dtype_original:
                print(dtype_strg,dtype_original)
                if dtype_strg == torch.float16 and dtype_original == torch.bfloat16:
                    self.logger.warning(f"⚠️ Proof dtype: {dtype}, Original dtype: {dtype_original} - falling back to {dtype_strg} but might create verification errors")
                else:
                    self.logger.debug(f"❌ Proof dtype: {dtype} != Original dtype: {dtype_original}")
                    return False

        model_bytes_est = None  # compute lazily if needed

        # -------------------------------------------------------------- #
        #                   Choose device map based on VRAM             #
        # -------------------------------------------------------------- #
        device_map: str | Dict | None = None
        if torch.cuda.is_available():
            model_bytes_est = self._estimate_model_size(None)
            # Evict LRU parked models first so a fresh load lands directly on
            # the GPU (avoids a CPU load + promote round-trip).
            self._reclaim_gpu(int(model_bytes_est * 1.05))
            gpu_mem = self._get_all_gpu_mem()
            # Pick best GPU with 20 % buffer
            best_gpu = max(
                (idx for idx, free in gpu_mem.items() if free > model_bytes_est * 1.05),
                key=gpu_mem.get,
                default=None,
            )
            if best_gpu is not None:
                device_map = {"": best_gpu}
                self.logger.debug(f"  🎯 Loading model directly on cuda:{best_gpu}")
        # Never silently load an 8B model on CPU and run a ~700s forward pass —
        # fail fast so the job is re-routed to a worker that can hold it.
        if device_map is None and torch.cuda.is_available() and not self._allow_cpu_inference:
            raise ModelPlacementError(
                f"model {self.model_name}@{self.commit_hash} needs "
                f"~{int(model_bytes_est or 0) >> 20}MB but no GPU has room after eviction"
            )
        # fallback to CPU only when CUDA is absent (or explicitly permitted)
        if device_map is None:
            self.logger.warning("  ⚠️  Loading model on CPU (GPU insufficient or absent)")
            device_map = {"": "cpu"}

        try:
            cpu = not torch.cuda.is_available()
            requested_dtype = (torch.float32 if cpu else dtype)
            self.model = AutoModelForCausalLM.from_pretrained(
                self.model_name,
                revision=self.commit_hash,
                config=config,
                torch_dtype=requested_dtype,
                device_map=device_map,
                trust_remote_code=True,
            ).eval()
        except Exception as hf_err:
            if self.ipfs_cid is None:
                raise
            warnings.warn(
                f"HF load failed ({hf_err}); attempting IPFS fallback…",
                RuntimeWarning,
            )
            self.model = self._load_model_from_ipfs(self.ipfs_cid, dtype=dtype)
        if self.model is None:
            self.logger.error("Model load returned None for %s@%s", self.model_name, self.commit_hash)
            return False

        self._ensure_tokenizer()
        # Attach MCA attention hooks to cover non-SDPA paths
        try:
            mca_detach_attn_hooks(self.model)
            mca_attach_attn_hooks(self.model)
        except Exception as _e:
            self.logger.debug(f"MCA hooks attach skipped: {_e}")
        self.logger.debug(f"  ✅ Model loaded – dtypes: {set(p.dtype for p in self.model.parameters())}")

        # Record where the model actually landed and refuse CPU inference.
        try:
            self.device = str(next(self.model.parameters()).device)
        except StopIteration:
            pass
        self._assert_gpu_resident()
        self._trim_coresident_models()

        # Smell-test stats: load if pre-baked, never bake inline on this path
        # (full verify does not read self.stats; the warmup loop bakes).
        self._load_or_defer_stats()
        return True
        
    def _ensure_tokenizer(self) -> None:
        """Lazy‑initialise tokenizer if absent."""
        if self.tokenizer is not None:
            return
        self.tokenizer = AutoTokenizer.from_pretrained(
            self.model_name, revision=self.commit_hash
        )

    def _load_model_from_ipfs(self, cid: str, *, dtype: torch.dtype,
                           storage_dir: str = "/opt/dlami/nvme/ipfs_models") -> PreTrainedModel:
        """
        Download model from IPFS using lightweight daemon via subprocess.
        Much more reliable than HTTPS for large files.
        
        Args:
            cid: IPFS CID (v0 or v1)
            dtype: Torch dtype for model weights
            storage_dir: Base path to store models by CID
            
        Returns:
            Loaded AutoModelForCausalLM in eval mode
        """
        # Clean CID
        cid_clean = cid.strip().rstrip('/')
        if '/ipfs/' in cid_clean:
            cid_clean = cid_clean.split('/ipfs/')[-1]
        cid_clean = cid_clean.split('/')[0]  # Take only CID part
        
        # Setup paths
        os.makedirs(storage_dir, exist_ok=True)
        model_path = os.path.join(storage_dir, cid_clean)
        
        # Return if already exists
        if os.path.isdir(model_path) and os.listdir(model_path):
            self.logger.debug(f"📂 Loading cached model at {model_path}")
            return AutoModelForCausalLM.from_pretrained(
                model_path, torch_dtype=dtype, device_map="auto", trust_remote_code=True
            ).eval()
        
        # Initialize lightweight IPFS client
        self._ensure_ipfs_client()
        
        # Start daemon for download
        daemon = self._start_ipfs_daemon()
        
        try:
            # Download via IPFS subprocess  
            self.logger.debug(f"📥 Downloading model {cid_clean} via IPFS...")
            self._download_from_ipfs(cid_clean, model_path)
            
            self.logger.debug(f"✅ Model downloaded to {model_path}")
            
            # Load the model
            return AutoModelForCausalLM.from_pretrained(
                model_path, torch_dtype=dtype, device_map="auto", trust_remote_code=True
            ).eval()
            
        except Exception as e:
            # Cleanup on failure
            if os.path.exists(model_path):
                shutil.rmtree(model_path, ignore_errors=True)
            raise RuntimeError(f"Failed to load model from IPFS: {e}")
        finally:
            # Always stop daemon
            daemon.terminate()
            try:
                daemon.wait(timeout=5)
            except subprocess.TimeoutExpired:
                daemon.kill()

    def _ensure_ipfs_client(self) -> None:
        """Initialize lightweight IPFS client for download-only usage."""
        ipfs_path = os.environ.get('IPFS_PATH', '/tmp/ipfs_client')
        
        # Check if already initialized
        if os.path.exists(os.path.join(ipfs_path, 'config')):
            return
            
        self.logger.debug("🔧 Initializing lightweight IPFS client...")
        
        try:
            # Initialize with minimal profile (no DHT server, no provider)
            subprocess.run([
                'ipfs', 'init', '--profile=lowpower'
            ], check=True, capture_output=True, text=True)
            
            # Minimal working config
            configs = [
                ('Routing.Type', '"dhtclient"'),
                # ('Discovery.MDNS.Enabled', 'false'),
                # ('Reprovider.Strategy', '"manual"'),
            ]
            
            for key, value in configs:
                subprocess.run([
                    'ipfs', 'config', '--json', key, value
                ], check=True, capture_output=True)
                
            self.logger.debug("✅ IPFS client configured for download-only")
            
        except subprocess.CalledProcessError as e:
            raise RuntimeError(f"Failed to initialize IPFS client: {e.stderr}")

    def _start_ipfs_daemon(self) -> subprocess.Popen:
        """Start IPFS daemon and wait for it to be ready."""
        self.logger.debug("🚀 Starting IPFS daemon...")
        
        daemon = subprocess.Popen([
            'ipfs', 'daemon', '--routing=dhtclient'
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        
        # Wait for daemon to be ready (up to 30s)
        for _ in range(30):
            try:
                result = subprocess.run([
                    'ipfs', 'version'
                ], capture_output=True, timeout=2)
                if result.returncode == 0:
                    self.logger.debug("✅ IPFS daemon ready")
                    break
            except:
                pass
            time.sleep(1)
        else:
            daemon.terminate()
            raise RuntimeError("IPFS daemon failed to start within 30s")
        
        # Wait for peer connections
        self.logger.debug("🔗 Waiting for peer connections...")
        for attempt in range(30):
            try:
                peers = subprocess.run(['ipfs', 'swarm', 'peers'], 
                                    capture_output=True, text=True, timeout=5)
                peer_count = len(peers.stdout.strip().splitlines()) if peers.stdout.strip() else 0
                if peer_count > 0:
                    self.logger.debug(f"✅ Connected to {peer_count} peers")
                    return daemon
                self.logger.debug(f"⏳ Attempt {attempt+1}/30: {peer_count} peers")
            except:
                pass
            time.sleep(2)
        
        self.logger.warning("⚠️ No peer connections established, continuing anyway")
        return daemon

    def _download_from_ipfs(self, cid: str, output_path: str) -> None:
        """Download content from IPFS using subprocess with progress tracking."""
        
        # Create temp directory for download
        temp_dir = f"{output_path}.tmp"
        os.makedirs(temp_dir, exist_ok=True)
        
        try:
            # Use ipfs get with progress (NO timeout for large models)
            cmd = [
                'ipfs', 'get', cid,
                '--output', temp_dir,
                '--progress'
            ]
            
            self.logger.debug(f"Running: {' '.join(cmd)}")
            
            # Run with real-time progress output
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                universal_newlines=True
            )
            
            # Track progress
            for line in process.stdout:
                line = line.strip()
                if line:
                    # IPFS shows progress like: "3.32 GiB / 3.32 GiB [===] 100.00%"
                    if any(unit in line for unit in ['MB', 'KB', 'GB', 'GiB', '%']):
                        self.logger.debug(f"📊 {line}")
                    elif any(word in line.lower() for word in ['saving', 'fetched']):
                        self.logger.debug(f"✅ {line}")
            
            process.wait()
            
            if process.returncode != 0:
                raise subprocess.CalledProcessError(process.returncode, cmd)
            
            # Files are downloaded directly to temp_dir (not in subdirectory)
            temp_contents = os.listdir(temp_dir)
            model_files = [f for f in temp_contents if f in [
                'config.json', 'pytorch_model.bin', 'model.safetensors', 
                'tokenizer.json', 'tokenizer_config.json', 'generation_config.json'
            ]]
            
            if not model_files:
                raise ValueError(f"No model files found in download: {temp_contents}")
            
            # Move all downloaded files to final location
            if os.path.exists(output_path):
                shutil.rmtree(output_path)
            os.makedirs(output_path, exist_ok=True)
            
            for item in temp_contents:
                src = os.path.join(temp_dir, item)
                dst = os.path.join(output_path, item)
                if os.path.isfile(src):
                    shutil.move(src, dst)
                elif os.path.isdir(src):
                    shutil.move(src, dst)
            
            # Verify we have essential model files
            required_files = ['config.json']
            missing = [f for f in required_files if not os.path.exists(os.path.join(output_path, f))]
            if missing:
                raise ValueError(f"Downloaded model missing required files: {missing}")
                
            self.logger.debug(f"✅ Model successfully downloaded and validated")
            
        except Exception as e:
            # Cleanup temp directory
            if os.path.exists(temp_dir):
                shutil.rmtree(temp_dir, ignore_errors=True)
            raise
        finally:
            # Always cleanup temp
            if os.path.exists(temp_dir):
                shutil.rmtree(temp_dir, ignore_errors=True)

    def _test_ipfs_connectivity(self) -> bool:
        """Test if IPFS can connect to the network."""
        try:
            # Try to fetch a small well-known hash
            result = subprocess.run([
                'ipfs', 'cat', 'QmQPeNsJPyVWPFDVHb77w8G42Fvo15z4bG2X8D2GhfbSXc',  # "hello world"
                '--timeout=30s'
            ], capture_output=True, text=True, timeout=35)
            
            return result.returncode == 0 and 'hello world' in result.stdout
        except:
            return False
            
    def preload_models_to_registry(self, model_identifiers: List[str], show_progress: bool = True, threaded: bool = True) -> Union[None, threading.Thread]:
        """
        Preload multiple models into CPU registry for fast switching.

        Args:
            model_identifiers: List of "model_name@commit_hash" strings
            show_progress: Whether to show progress bar
            threaded: Whether to run in a separate thread

        Returns:
            If threaded=False: None (models loaded directly to self._model_registry)
            If threaded=True: Thread object (join() to wait for completion)
        """
        def _preload_worker():
            # Save current model state
            current_model = self.model
            current_identifier = getattr(self, '_current_model_identifier', None)
            current_device = self.device if hasattr(self, 'device') else 'cpu'
            current_name = self.model_name if hasattr(self, 'model_name') else None
            current_hash = self.commit_hash if hasattr(self, 'commit_hash') else None

            # Setup progress bar if requested
            if show_progress:
                from tqdm import tqdm
                pbar = tqdm(model_identifiers, desc="Preloading models")
            else:
                pbar = model_identifiers

            success_count = 0
            already_count = 0
            failed_count = 0

            for identifier in pbar:
                try:
                    # Check if already in registry
                    if identifier in self._model_registry:
                        already_count += 1
                        if show_progress:
                            pbar.set_postfix({"status": "already loaded"})
                        continue

                    # Parse identifier
                    try:
                        model_name, commit_hash = identifier.split('@')
                    except ValueError:
                        failed_count += 1
                        print(f"  ❌ Invalid identifier format: {identifier}")
                        continue

                    if show_progress:
                        pbar.set_postfix({"loading": model_name.split('/')[-1]})

                    # Check if model would fit in CPU RAM
                    # Temporarily set model info for size estimation
                    self.model_name, self.commit_hash = model_name, commit_hash

                    model_bytes_est = self._estimate_model_size(None)
                    cpu_free = self._get_available_cpu_mem()

                    if model_bytes_est > cpu_free - (1 << 30):  # Leave 1GB margin
                        failed_count += 1
                        print(f"  ❌ Insufficient CPU RAM for {identifier} (need {model_bytes_est/(1<<30):.1f}GB)")
                        continue

                    # Load model directly to CPU
                    print(f"\n  📥 Preloading {identifier} to CPU...")

                    config = AutoConfig.from_pretrained(
                        model_name,
                        revision=commit_hash,
                        trust_remote_code=True,
                    )

                    # Load with CPU device map
                    model = AutoModelForCausalLM.from_pretrained(
                        model_name,
                        revision=commit_hash,
                        config=config,
                        torch_dtype=self.dtype,
                        device_map={"": "cpu"},
                        trust_remote_code=True,
                    ).eval()

                    # Add to registry
                    self._model_registry[identifier] = model
                    success_count += 1

                    print(f"  ✅ Successfully preloaded {identifier}")

                    # Clear any temporary GPU usage
                    self._ensure_clean_switch()

                except Exception as e:
                    failed_count += 1
                    print(f"  ❌ Failed to preload {identifier}: {e}")

            # Restore original model state
            self.model = current_model
            if current_identifier:
                self._current_model_identifier = current_identifier
            if current_name and current_hash:
                self.model_name = current_name
                self.commit_hash = current_hash
            self.device = current_device

            # Summary
            print(f"\n📊 Preload Summary: {success_count} loaded, {already_count} already cached, {failed_count} failed")

        if threaded:
            import threading
            thread = threading.Thread(target=_preload_worker, name="ModelPreloader")
            thread.start()
            return thread
        else:
            _preload_worker()
            return None
    
    # ----------------------------------------------------------------- #
    #                        Smell test
    # ----------------------------------------------------------------- #
            
    def _load_stats(self):
        """
        Load all datasets and file attributes from the HDF5 file located at
        os.path.join(self.cache_dir, '{model_name}_{commit_hash}_stats.h5').

        On success, datasets and file-level attributes are stored in self.stats.
        Each dataset's original dtype is read from its attribute and used to cast
        the numpy array before wrapping in a torch.Tensor.

        Raises:
            FileNotFoundError: if the stats file does not exist.
        """
        filename = os.path.join(
            self.cache_dir,
            f"{self.model_name.replace('/','_')}_{self.commit_hash}_stats.h5"
        )
        if not os.path.isfile(filename):
            raise FileNotFoundError(f"Stats file not found: {filename}")

        if not self.stats_loaded or self.stats_loaded_filename != filename: 
            self.stats = {}
            with h5py.File(filename, 'r') as f:
                # Load datasets with preserved dtype
                for name, ds in f.items():
                    arr = ds[...]
                    # get saved dtype string if present
                    dtype_str = ds.attrs.get('dtype', None)
                    torch_dtype = getattr(torch, dtype_str, None) if dtype_str else None
                    if dtype_str:
                        # NumPy can't represent bfloat16 — leave arr as the
                        # storage dtype (fp32) and let torch downcast it
                        # via the explicit dtype= below. For everything
                        # else, snap the numpy array back to the saved
                        # dtype before tensor construction.
                        try:
                            arr = arr.astype(dtype_str)
                        except (TypeError, ValueError):
                            pass
                    # wrap in torch tensor, preserving dtype
                    tensor = torch.tensor(arr, dtype=torch_dtype)
                    self.stats[name] = tensor

                # Load file-level attributes
                for attr_name, attr_val in f.attrs.items():
                    # skip per-dataset dtype recordings
                    if attr_name == 'dtype':
                        continue
                    self.stats[attr_name] = attr_val

            # Validate ownership + schema. If invalid, caller will regenerate.
            repo_attr = str(self.stats.get("repo", "") or "")
            commit_attr = str(self.stats.get("commit", "") or "")
            if repo_attr and repo_attr != self.model_name:
                raise ValueError(
                    f"stats file repo mismatch: expected '{self.model_name}', got '{repo_attr}'"
                )
            if commit_attr and commit_attr != self.commit_hash:
                raise ValueError(
                    f"stats file commit mismatch: expected '{self.commit_hash}', got '{commit_attr}'"
                )

            required_keys = {
                "S",
                "pi",
                "sigma",
                "gap_mean",
                "gap_cov",
                "chunk_counts_mean",
                "chunk_counts_cov",
                "confidence_level",
                "confidence_level_gap",
                "z_mean_cosine",
                "z_std_cosine",
                "confidence_level_cos",
                "threshold_joint",
                "emb_pca",
            }
            missing = sorted(k for k in required_keys if k not in self.stats)
            if missing:
                raise KeyError(
                    f"stats cache is missing required keys: {', '.join(missing)}"
                )
            self.stats_loaded_filename = filename                
            self.stats_loaded = True                
    
    def _save_stats(self, stats_data, force: bool = False):
        """
        Save named arrays or torch.Tensors to the HDF5 file in self.cache_dir.
        By default, existing datasets are not overwritten unless force=True.

        This method records each dataset's dtype in its own attribute,
        ensuring full fidelity on reload.

        Args:
            force (bool): if True, overwrite any existing datasets.
            **stats_data: named arrays or torch.Tensors to store; if empty, uses self.stats.
        """

        os.makedirs(self.cache_dir, exist_ok=True)
        filename = os.path.join(
            self.cache_dir,
            f"{self.model_name.replace('/','_')}_{self.commit_hash}_stats.h5"
        )

        # Write to a per-writer temp file then os.replace into final.
        # On a shared RWX cache, concurrent readers must never observe
        # a half-written h5 — atomic rename guarantees they see either
        # the prior file or the complete new one.
        tmp_filename = f"{filename}.tmp.{os.getpid()}.{int(time.time() * 1000)}"

        # Seed the temp file from the existing final (if any) so
        # force=False semantics still skip already-present datasets.
        if os.path.exists(filename):
            try:
                shutil.copy2(filename, tmp_filename)
            except Exception:
                # Fall back to a fresh file; loss of half-baked state
                # is preferable to crashing the bake.
                if os.path.exists(tmp_filename):
                    try:
                        os.remove(tmp_filename)
                    except Exception:
                        pass

        try:
            with h5py.File(tmp_filename, 'a') as f:
                # File-level metadata
                f.attrs['repo'] = self.model_name
                f.attrs['commit'] = self.commit_hash

                for name, data in stats_data.items():
                    if data is None:
                        continue

                    # Convert torch.Tensor to numpy. NumPy has no native bfloat16
                    # so .numpy() raises TypeError on bf16 tensors. Upcast to
                    # float32 for storage (bf16 ⊂ fp32, lossless) and stash the
                    # original dtype in the attr so _load_stats reconstructs it.
                    if isinstance(data, torch.Tensor):
                        dtype_str = str(data.dtype).split('.')[-1]
                        cpu_t = data.detach().cpu()
                        if cpu_t.dtype == torch.bfloat16:
                            cpu_t = cpu_t.to(torch.float32)
                        np_data = cpu_t.numpy()
                    else:
                        np_data = np.array(data)
                        dtype_str = np_data.dtype.name if isinstance(np_data, np.ndarray) else None

                    # replace existing if force
                    if name in f:
                        if not force:
                            continue
                        del f[name]

                    # Handle scalar vs array for chunking
                    if isinstance(np_data, np.ndarray) and np_data.ndim == 0:
                        ds = f.create_dataset(name, data=np_data)
                    else:
                        ds = f.create_dataset(
                            name,
                            data=np_data,
                            compression="gzip",
                            chunks=True
                        )

                    # persist dtype
                    if dtype_str:
                        ds.attrs['dtype'] = dtype_str
            os.replace(tmp_filename, filename)
        except Exception:
            if os.path.exists(tmp_filename):
                try:
                    os.remove(tmp_filename)
                except Exception:
                    pass
            raise

    def _collect_logits_stats(
        self,
        seq_length:   int  = 4,
        total_tokens: int  = 500_000,
        batch_size:   int  = 20,
        inert_topk:   int  = 75,
        chunk_size:   int  = 256,          # << 256-step “chunks”
        ):
        vocab_size  = self.model.config.vocab_size
        device      = next(self.model.parameters()).device
        top_ranks   = 50
        seq_length  = seq_length * chunk_size

        self.emb_pca = self._get_pca_embeddings(16)

        # ---------- token-selection bookkeeping ----------
        rank_counts = torch.zeros(vocab_size, top_ranks,
                                  dtype=torch.int64, device=device)
        gaps_per_chuck = []      # per-sequence full counts
        counts_per_chunk  = []      # per-chunk counts across all sequences
        cosine_per_chunk  = []      # per-chunk cosine similarity average

        # ---------- gap statistics ----------
        gap_cov_acc  = RunningMeanCov(dim=49, device=device)
        chunk_buffer = []           # will hold [chunk_size x bs x 49]
        cosine_means_similarity = []

        n_sequences = (total_tokens + seq_length - 1) // seq_length
        n_batches   = (n_sequences + batch_size  - 1) // batch_size
        print(f"Generating {n_sequences} seqs in {n_batches} batches")

        with mca_enabled(False):
            with torch.inference_mode():
                for batch_idx in tqdm(range(n_batches), desc="Batches", unit="batch"):
                    bs = min(batch_size, n_sequences - batch_idx * batch_size)
                    if bs <= 0:
                        break

                    input_ids = torch.randint(0, vocab_size, (bs, 1), device=device)
                    past = None

                    C_local = torch.zeros(bs, vocab_size, dtype=torch.long, device=device)
                    C_chunk = torch.zeros(bs, vocab_size, dtype=torch.long, device=device)
                    chunk_buffer.clear()
                    cosine_means_similarity.clear()

                    for step in range(seq_length):
                        out    = self.model(input_ids=input_ids,
                                            past_key_values=past, use_cache=True)
                        logits = out.logits[:, -1, :]                     # [bs, V]
                        past   = out.past_key_values


                        # ----- rank stats -----
                        top_vals, top_idx = torch.topk(logits, top_ranks, dim=-1)
                        ranks_flat = top_idx.view(-1)
                        rank_ids   = torch.arange(top_ranks, device=device).repeat(bs)
                        rank_counts.index_put_((ranks_flat, rank_ids),
                                               torch.ones_like(ranks_flat),
                                               accumulate=True)


                        # ----- embedding centroids -----
                        c     = F.embedding(top_idx, self.emb_pca)

                        # compute dispersion per sample in batch (vectorized)
                        cosine_means_similarity.append(self._compute_dispersion_metrics_batched(c))

                        # update counts
                        batch_idx_vec = torch.arange(bs, device=device).unsqueeze(-1).expand_as(top_idx)
                        C_local.index_put_((batch_idx_vec, top_idx), torch.ones_like(top_idx), accumulate=True)
                        C_chunk.index_put_((batch_idx_vec, top_idx), torch.ones_like(top_idx), accumulate=True)

                        # ----- gap vector -----
                        gaps = (top_vals[:, :-1] - top_vals[:, 1:]).to(torch.float64)  # [bs,49]
                        chunk_buffer.append(gaps)

                    if (step + 1) % chunk_size == 0:
                        # gap stats
                        chunk_mean = torch.stack(chunk_buffer, 0).mean(0)
                        for vec in chunk_mean:
                            gap_cov_acc.update(vec)
                        chunk_buffer.clear()

                        # save per-chunk token counts
                        counts_per_chunk.append(C_chunk.clone().cpu())
                        gaps_per_chuck.append(chunk_mean.clone().cpu())
                        C_chunk.zero_()

                        # cosine_per_chunk .........................................
                        cosine_chunk = torch.stack(cosine_means_similarity).mean(0)          # [bs]
                        cosine_per_chunk.append(cosine_chunk.clone().cpu())     # save raw means
                        cosine_means_similarity.clear()                    

                    # ----- sample next token -----
                    top100_v, top100_i = torch.topk(logits, 100, dim=-1)
                    probs = F.softmax(top100_v, dim=-1)
                    next_tok = torch.multinomial(probs, 1).squeeze(-1)
                    input_ids = top100_i.gather(-1, next_tok.unsqueeze(-1))


        # ------------------------------------------------------------------
        #  Variance-based inert-token selection  (proper scaling)
        # ------------------------------------------------------------------
        C = torch.cat(counts_per_chunk, dim=0).to(torch.float64)   # [P, V]
        slots_per_prompt = C.sum(1, keepdim=True).clamp_min(1)      # [P,1]
        P_mat = C / slots_per_prompt                                # [P, V]

        mu     = P_mat.mean(0)                                      # (V,)
        var    = P_mat.var(0, unbiased=False)                      # (V,)
        cv     = (var.sqrt() / mu.clamp_min(1e-12)).clamp(max=10)
        mask   = (mu > 0.001) & (mu < 0.05) #& (cv < 0.5)
        cand   = torch.where(mask)[0]
        info   = (mu * (1 - mu))[mask]
        topk   = torch.argsort(info, descending=True)[:inert_topk]
        S      = cand[topk]
        pi     = mu[S]
        sigma  = var.sqrt()[S]

        # ------------------------------------------------------------------
        #  Count statistics for selected inert tokens over chunks
        # ------------------------------------------------------------------
        # concatenate all chunks across batches into one big matrix
        C_chunks = torch.cat(counts_per_chunk, dim=0).to(torch.float64)  # [N, V]
        C_S      = C_chunks[:, S.cpu()]                     # [N, K]
        chunk_counts_mean = C_S.mean(0)               # (K,)
        # Ledoit–Wolf shrinkage for covariance
        cov_np, shrinkage = ledoit_wolf(C_S.cpu().numpy())
        chunk_counts_cov  = torch.from_numpy(cov_np).to(dtype=torch.float64)
        print(f"Ledoit–Wolf shrinkage λ={shrinkage:.4f}")

        # ------------------------------------------------------------------
        #  Mahalanobis distance-based confidence estimation
        # ------------------------------------------------------------------
        # Invert covariance and compute distances
        cov_inv = torch.linalg.inv(chunk_counts_cov)
        # Center data
        X = C_S - chunk_counts_mean.unsqueeze(0)
        # Mahalanobis distances: sqrt((x-mean)^T inv_cov (x-mean))
        mdists = torch.sqrt(torch.sum((X @ cov_inv) * X, dim=1))  # [N]
        # 99th percentile threshold
        threshold = torch.quantile(mdists, 0.99)
        print(f"Mahalanobis 99th quantile counts threshold: {threshold:.4f}")

        # ------------------------------------------------------------------
        #  Mahalanobis distance-based confidence estimation
        # ------------------------------------------------------------------
        miu = torch.cat(gaps_per_chuck, dim=0).to(torch.float64) 
        # Invert covariance and compute distances
        cov_inv = torch.linalg.inv(gap_cov_acc.covariance.cpu())
        # Center data
        X = miu - gap_cov_acc.mean.cpu().unsqueeze(0)
        # Mahalanobis distances: sqrt((x-mean)^T inv_cov (x-mean))
        mdists_gap = torch.sqrt(torch.sum((X @ cov_inv) * X, dim=1))  # [N]
        # 99th percentile threshold
        threshold_gap = torch.quantile(mdists_gap, 0.99)
        print(f"Mahalanobis 99th quantile gaps threshold: {threshold_gap:.4f}")

        # ------------------------------------------------------------------
        #  Cosine-similarity Mahalanobis (1-D)             ### NEW BLOCK ###
        # ------------------------------------------------------------------
        cos_means = torch.cat(cosine_per_chunk).to(torch.float64)    # [N]

        # Fisher transform:  (shift to [-1,1] then atanh)
        r = (cos_means * 2) - 1
        z = torch.atanh(r.clamp(-0.999_999, 0.999_999))              # avoid inf

        # Empirical mean / var
        z_mu  = z.mean()
        z_var = z.var(unbiased=False).clamp_min(1e-12)
        z_std = z_var.sqrt()

        # 1-D Mahalanobis  == |z-score|
        mdists_cos = torch.abs(z - z_mu) / z_std

        # 99-th-percentile threshold (equivalently, √χ²₁ .99 ≈ 2.326)
        threshold_cos = torch.quantile(mdists_cos, 0.99)

        print(f"Mahalanobis 99th quantile cosine-z threshold: {threshold_cos:.4f}")    


        # elementwise joint distance
        joint = torch.sqrt(mdists**2 + mdists_gap**2 + mdists_cos**2)
        threshold_joint = torch.quantile(joint, 0.99)

        # ------------------------------------------------------------------
        #  Package
        # ------------------------------------------------------------------
        stats = {
            "S":                    S.cpu(),
            "pi":                   pi.cpu(),
            "sigma":                sigma.cpu(),
            "gap_mean":             gap_cov_acc.mean.cpu(),
            "gap_cov":              gap_cov_acc.covariance.cpu(),
            "chunk_counts_mean":    chunk_counts_mean,
            "chunk_counts_cov":     chunk_counts_cov,
            # "mahalanobis_distances": mdists,
            "confidence_level":     threshold,
            # "mahalanobis_distances_gap": mdists_gap,
            "confidence_level_gap":     threshold_gap,
            "z_mean_cosine":          z_mu.cpu(),
            "z_std_cosine":           z_std.cpu(),
            # "mahalanobis_distances_cos": mdists_cos.cpu(),
            "confidence_level_cos":      threshold_cos.cpu(),        
            "threshold_joint":     threshold_joint,
            'emb_pca':              self.emb_pca
        }
        print(f"Saved fingerprint   |   {len(S)} inert tokens   |   "
              f"μ/Σ from {gap_cov_acc.n} chunk-means "
              f"(each = mean of {chunk_size} sequential gaps)")
        return stats
   
    def _validate_topk_batch(
        self,
        topk_logits:  torch.Tensor,   # [N, 50] – already sorted
        topk_indices: torch.Tensor,   # [N, 50]
        stats:        dict,
        ) -> dict:
        """
        • χ² aggregate of per-token Wald z-tests on presence of inert set S
        • Mahalanobis test on 49-gap mean
        • Fisher’s method to combine the two p-values
        """

        # ------------------ normalise shapes --------------------
        if topk_logits.ndim == 1:      # user passed a single row
            topk_logits  = topk_logits.unsqueeze(0)
            topk_indices = topk_indices.unsqueeze(0)

        assert topk_logits.shape == topk_indices.shape
        assert topk_logits.shape[1] == 50, "expect top-50 per position"

        device = topk_logits.device
        slots  = topk_logits.numel()                 # N × 50

        # ------------------ bounds check ------------------------
        emb_pca = stats["emb_pca"].to(device)                  # PCA-reduced embeddings [V, d]
        max_idx = int(topk_indices.max().item())
        if max_idx >= emb_pca.shape[0]:
            print(max_idx,  emb_pca.shape)
            return { 'pass': False }        
        
        g_ref   = stats["gap_mean"].to(torch.float64).to(device)
        cov_ref = stats["gap_cov"].to(torch.float64).to(device)
        chunk_counts_mean   = stats["chunk_counts_mean"].to(torch.float64).to(device)
        chunk_counts_cov = stats["chunk_counts_cov"].to(torch.float64).to(device)

        # make sure Σ is well-conditioned
        cov_inv = torch.linalg.inv(
            cov_ref + 5e-6 * torch.eye(49, dtype=torch.float64, device=device)
        )
        chunk_counts_cov_inv = torch.linalg.inv(
            chunk_counts_cov + 1e-3 * torch.eye(chunk_counts_cov.size(0), dtype=torch.float64, device=device)
        )

        # Get inert token counts
        S = stats["S"].to(device)
        pi = stats["pi"].to(device).double()
        K = len(S)

        # Create lookup table for fast counting
        vocab_size = int(max(topk_indices.max(), S.max())) + 1
        lut = torch.full((vocab_size,), -1, device=device, dtype=torch.long)
        lut[S] = torch.arange(K, device=device)

        # Count occurrences in current batch
        flat_indices = topk_indices.reshape(-1)
        in_S_mask = (lut[flat_indices] >= 0)
        counts_S = torch.bincount(lut[flat_indices][in_S_mask], minlength=K).double()

        # --------------------------------------------------------
        # 1. Presence Test
        # --------------------------------------------------------
        delta = counts_S - chunk_counts_mean
        maha = torch.einsum('i,ij,j->', delta, chunk_counts_cov_inv, delta).item()
        p_freq = (1.0 - Chi2(chunk_counts_cov.size(0)).cdf(torch.tensor(maha, device=device))).item()

        # --------------------------------------------------------
        # 2. Gap Test
        # --------------------------------------------------------
        gaps = (topk_logits[:, :-1] - topk_logits[:, 1:]).double()
        g_hat = gaps.mean(0)
        delta = g_hat - g_ref
        maha_gap = torch.einsum('i,ij,j->', delta, cov_inv, delta).item()
        p_gap = (1.0 - Chi2(49).cdf(torch.tensor(maha_gap, device=device))).item()   

        # --------------------------------------------------------
        # 3. Cosine-similarity Test       
        # --------------------------------------------------------
        c = F.embedding(topk_indices, emb_pca)                 # [N, 50, 16]

        # mean pairwise cosine per sample → then average across samples
        cos_mean = self._compute_dispersion_metrics_batched(c).mean()  # scalar

        # Fisher z-transform (to ℝ)
        r      = (cos_mean * 2) - 1
        z_val  = torch.atanh(r.clamp(-0.999_999, 0.999_999))

        z_mu   = stats["z_mean_cosine"].to(device)
        z_std  = stats["z_std_cosine"].to(device).clamp_min(1e-12)
        z_score = torch.abs(z_val - z_mu) / z_std              # |z|  ~ 𝒩(0,1)

        maha_cos = (z_score ** 2).item()                       # χ²₁
        p_cos = (1.0 - Chi2(1).cdf(torch.tensor(maha_cos, device=device))).item()


        # 4. Joint-distance threshold test     
        # --------------------------------------------------------
        joint = np.sqrt(maha + maha_gap + maha_cos)
        success = joint < stats["threshold_joint"]

        success =   ( np.sqrt(maha_cos) < stats["confidence_level_cos"]) & \
                    ( np.sqrt(maha_gap) < stats["confidence_level_gap"]) & \
                    ( np.sqrt(maha) < stats["confidence_level"])
        # if DEBUG:
        #     print(np.sqrt(maha_cos),stats["confidence_level_cos"])
        #     print(np.sqrt(maha_gap),stats["confidence_level_gap"])
        #     print(np.sqrt(maha),stats["confidence_level"])
        return {
            "p_freq":  p_freq,
            "p_gap":   p_gap,
            "p_cos":   p_cos,             # NEW
            "pass":    success.item(),
            "joint_distance": joint,
            "threshold99":    stats["threshold_joint"].item(),
        }

    def _compute_dispersion_metrics(self, embeddings: torch.Tensor):
        """
        Compute scalar dispersion metrics on a set of embeddings.

        Args:
            embeddings: Tensor of shape [N, D]

        Returns:
            pair_mean: mean of all pairwise cosine similarities
        """
        # Number of points
        N, D = embeddings.shape

        # Pairwise cosine similarities
        sims = F.cosine_similarity(
            embeddings.unsqueeze(1),  # [N, 1, D]
            embeddings.unsqueeze(0),  # [1, N, D]
            dim=-1                   # -> [N, N]
        )
        # take upper triangular (i<j)
        idx = torch.triu_indices(N, N, offset=1)
        sim_vals = sims[idx[0], idx[1]]
        pair_mean = sim_vals.mean()

        return pair_mean

    @staticmethod
    def _compute_dispersion_metrics_batched(embeddings_batch: torch.Tensor) -> torch.Tensor:
        """
        Vectorized dispersion: mean pairwise cosine similarity per sample.

        Args:
            embeddings_batch: Tensor of shape [B, K, D]

        Returns:
            Tensor of shape [B] — mean upper-triangular cosine similarity per sample.
        """
        # Normalise once: [B, K, D]
        normed = F.normalize(embeddings_batch, p=2, dim=-1)
        # Batched pairwise cosine via matmul: [B, K, K]
        sims = torch.bmm(normed, normed.transpose(1, 2))
        K = sims.size(1)
        # Upper-triangular mask (shared across batch)
        row, col = torch.triu_indices(K, K, offset=1, device=sims.device)
        # Extract upper triangle: [B, K*(K-1)/2]
        upper = sims[:, row, col]
        # Mean per sample: [B]
        return upper.mean(dim=-1)

    def _get_pca_embeddings(self, n_components: int = 16) -> torch.Tensor:
        """
        Extracts the model's token-embedding matrix, performs PCA to reduce
        it to `n_components` dimensions, and returns a tensor of shape
        (vocab_size, n_components) on the model's device.
        """
        # 1. Extract the embedding weights
        emb = self.model.get_input_embeddings().weight   # nn.Embedding weight, shape [vocab_size, emb_dim]

        # 2. Move to CPU and convert to numpy for sklearn.
        # NumPy does not reliably support torch.bfloat16 conversion in this runtime.
        emb_cpu_t = emb.detach()
        if emb_cpu_t.dtype == torch.bfloat16:
            emb_cpu_t = emb_cpu_t.to(torch.float32)
        emb_cpu = emb_cpu_t.cpu().numpy()

        # 3. Run PCA
        pca = PCA(n_components=n_components)
        emb_reduced = pca.fit_transform(emb_cpu)         # shape [vocab_size, n_components]

        # 4. Convert back to tensor, put back on model's device
        emb_pca = torch.from_numpy(emb_reduced).to(emb.device).type(emb.dtype)

        return emb_pca                    

    # ----------------------------------------------------------------- #
    #                        Fwd pass and sampling
    # ----------------------------------------------------------------- #
    @pow_profiler
    def _logits(
            self,
            ctx: torch.Tensor,              # 1D tensor of token-ids [L]
            batch_size: int,
            *,
            flash: bool | None = None,      
            kv: bool | None = None,
            enable_math: bool | None = None,      
            enable_mem: bool | None = None,      
            broadcast_cache: bool = False,   
            attn_mask: bool | None = None,
            mca_noise_value: float | None = None,
        ) -> torch.Tensor:                  
        # Ensure ctx is on the same device as the model
        model_device = next(self.model.parameters()).device
        ctx = ctx.to(model_device).contiguous()
        
        # Decide modes
        flash = self.use_flash_attn if flash is None else flash
        kv    = self.use_kv_cache   if kv    is None else kv
        impl  = "flash_attention_2" if flash else "eager"
        backend = SDPBackend.FLASH_ATTENTION if flash else SDPBackend.MATH
        enable_math = False if enable_math is None else enable_math
        enable_mem = False if enable_mem is None else enable_mem
        

        # Helper to call the model once
        def forward(input_ids, attn_mask, **kwargs):
            attn_mask = attn_mask.contiguous()
            input_ids = input_ids.contiguous()
            attn_mask = attn_mask.clone()
            kwargs['attention_mask'] = attn_mask
            # pick backend argument for new-style API
            new_arg = (SDPBackend.FLASH_ATTENTION,) if flash else (
                [SDPBackend.MATH, SDPBackend.EFFICIENT_ATTENTION]
            )

            # inspect signature to decide which context manager to use
            sig = inspect.signature(sdpa_kernel)
            if 'backends' in sig.parameters:
                # new-style: positional SDPBackend(s)
                cm = sdpa_kernel(*new_arg)
            else:
                # old-style fallback: keyword flags
                cm = old_sdp(
                    enable_flash=flash,
                    enable_math=enable_math,
                    enable_mem_efficient=enable_mem
                )

            # Configure MCA noise based on parameter
            if mca_noise_value is None:
                # Explicitly disable noise for this call, regardless of thread settings
                noise_cm = mca_enabled(False)
            elif isinstance(mca_noise_value, (int, float)):
                # Full-verifier MCA passes must be noisy even when the thread default is disabled.
                noise_cm = mca_active(k_attn=mca_noise_value, target_dtype=self.stated_dtype)
            else:
                # Use current thread MCA settings
                noise_cm = nullcontext()

            # run under the chosen context
            with noise_cm:
                out = self.model(input_ids, **kwargs)

            return out                

        if kv:
            # Check if we have a valid cache for ctx[:-1]
            cache_key = f"_kv_cache_b{batch_size}"
            cached_ctx_key = f"_cached_ctx_b{batch_size}"

            past = getattr(self, cache_key, None)
            cached_ctx = getattr(self, cached_ctx_key, None)

            # Determine if we can reuse the cache
            can_reuse = (
                past is not None and 
                cached_ctx is not None and
                len(ctx) > 1 and
                len(cached_ctx) == len(ctx) - 1 and
                torch.equal(cached_ctx, ctx[:-1])
            )

            if can_reuse:
                #print("reusing kv")
                # Just decode the last token using existing cache
                last = ctx[-1:].unsqueeze(0).expand(batch_size, -1)  # (batch_size, 1)
                attn_mask_last = attn_mask[-1:].unsqueeze(0).expand(batch_size, -1).contiguous()  # (batch_size, 1)
                with torch.no_grad():
                    dec_out = forward(last,attn_mask_last, past_key_values=past, use_cache=True)

                # Update cache for next call
                setattr(self, cache_key, dec_out.past_key_values)
                setattr(self, cached_ctx_key, ctx.clone())

                return dec_out.logits.squeeze(1)  # → (batch_size, vocab)

            else:
                #print("kv recompute")
                # Need to prefill from scratch
                pre_B = 1 if broadcast_cache else batch_size
                pre_input = ctx.unsqueeze(0).expand(pre_B, -1)  # shape (pre_B, L)
                attn_mask_pre = attn_mask.unsqueeze(0).expand(pre_B, -1).contiguous()  # (batch_size, 1)

                with torch.no_grad():
                    pre_out = forward(pre_input,attn_mask_pre, use_cache=True)
                past = pre_out.past_key_values

                # Expand cache if needed
                if broadcast_cache and batch_size > 1:
                    past = tuple(
                        tuple(t.expand(batch_size, *t.shape[1:]).contiguous() for t in layer)
                        for layer in past
                    )

                # Store cache for next call
                setattr(self, cache_key, past)
                setattr(self, cached_ctx_key, ctx.clone())

                # Return last token logits from prefill
                return pre_out.logits[:, -1]  # → (batch_size, vocab)

        else:
            # No cache: full forward on each batch
            inp = ctx.unsqueeze(0).expand(batch_size, -1)
            attn_mask_pre = attn_mask.unsqueeze(0).expand(batch_size, -1).contiguous()  # (batch_size, 1)
            with torch.no_grad():
                out = forward(inp,attn_mask_pre, use_cache=False)
            return out.logits[:, -1]  

    def _get_u(self, context_tokens, step_idx, hash_out=False):
        """Generate deterministic u value from context and step."""
        window_tokens = torch.zeros(self.window_size, dtype=torch.int64, device=self.device)
        context_len = min(len(context_tokens), self.window_size)
        window_tokens[-context_len:] = context_tokens[-context_len:]

        ctx_bytes = _tok_le_bytes(window_tokens.unsqueeze(0))
        j4 = _u32le(torch.tensor([step_idx], dtype=torch.uint32, device=self.device))
        T8 = _u32le(torch.tensor([self.proof['tick']], dtype=torch.uint32, device=self.device))
        precision_bytes = _str_bytes(self.stated_precision,   # e.g. 'fp16'
                                    batch_size=ctx_bytes.size(0),
                                    device=self.device
                                    )
        
        header_data = hex_to_bytes_tensor(self.proof['header_prefix'], device=self.device)
        v = hex_to_bytes_tensor(self.proof['vdf'], device=self.device)
        msg = _build_msg(header_data, v, T8, j4, ctx_bytes, precision_bytes)
        digest = sha256_many(msg)
        if hash_out:
            return digest[0].cpu().numpy().tobytes().hex()
        return _digest_to_u(digest).item()

    @staticmethod
    def _restore_argmax_if_empty(logits, pre_trunc_logits, token_ids):
        if torch.isfinite(logits).any():
            return logits

        valid = token_ids != -1
        if not valid.any():
            return logits

        max_val = pre_trunc_logits[valid].max()
        candidates = valid & (pre_trunc_logits == max_val)
        candidate_pos = candidates.nonzero(as_tuple=False).flatten()
        best = candidate_pos[torch.argmin(token_ids[candidate_pos])]

        out = logits.clone()
        out.fill_(-float("inf"))
        out[best] = pre_trunc_logits[best]
        return out

    @classmethod
    def _restore_argmax_if_empty_batch(cls, logits_batch, pre_trunc_batch,
                                      token_ids_batch):
        out = logits_batch.clone()
        for b in range(out.shape[0]):
            out[b] = cls._restore_argmax_if_empty(
                out[b], pre_trunc_batch[b], token_ids_batch[b])
        return out

    @staticmethod
    def _apply_stable_top_p_support(logits, token_ids, p):
        """Apply top-p over finite proof support using (logit desc, id asc)."""
        if p >= 1.0:
            return logits

        valid = (token_ids != -1) & torch.isfinite(logits)
        support = valid.nonzero(as_tuple=False).flatten()
        if support.numel() <= 1:
            return logits

        support_ids = token_ids[support]
        support_logits = logits[support]

        id_order = torch.argsort(support_ids, stable=True)
        support = support[id_order]
        support_logits = support_logits[id_order]

        logit_order = torch.argsort(-support_logits, stable=True)
        sorted_support = support[logit_order]
        sorted_logits = support_logits[logit_order]

        probs = torch.softmax(sorted_logits, dim=-1)
        prev_cum = torch.cumsum(probs, dim=-1) - probs
        keep = prev_cum < p
        keep[0] = True

        out = logits.clone()
        out[sorted_support[~keep]] = -float("inf")
        return out

    @classmethod
    def _apply_stable_top_p_support_batch(cls, logits_batch, token_ids_batch, p):
        if p >= 1.0:
            return logits_batch

        out = logits_batch.clone()
        for b in range(out.shape[0]):
            out[b] = cls._apply_stable_top_p_support(
                out[b], token_ids_batch[b], p)
        return out
    
    @pow_profiler
    def _sample(self, idx_sent, val_sent, context_tokens, u, expected_lse=None, query_token=None):
        """Sample from logits using temperature, repetition penalty, top-k/p, and u.
        
        Args:
            query_token: If provided, returns the CDF value for this specific token ID.
        """
    
        # 1) Temperature scale
        temp_logits = val_sent / self.temperature
        
        # 2) Apply repetition penalty
        rep_pen = getattr(self, 'repetition_penalty', 1.0)
        if rep_pen != 1.0:
            mask_rep = torch.isin(idx_sent, context_tokens)
            temp_logits[mask_rep] /= rep_pen

        # 3) Apply top-k and top-p exactly as sampler does
        pre_trunc_logits = temp_logits.clone()
        vals_sorted, idx_sorted = temp_logits.sort(dim=-1, descending=False)
        
        # # ––––––– Step A: reconstruct
        # triples = [(i, float(logit), int(tok)) for i, (logit, tok) in enumerate(zip(val_sent, idx_sent))]
        # triples.sort(key=lambda x: (x[1], x[2]))
        # orig_positions, sorted_logits, sorted_toks = zip(*triples)
        # vals_sorted    = torch.tensor(sorted_logits, dtype=torch.float64, device=self.device)
        # idx_logit_sorted       = torch.tensor(sorted_toks,    dtype=torch.long,   device=self.device)
        # idx_sorted  = torch.tensor(orig_positions, dtype=torch.long,   device=self.device)        
                 
        # Top-k: mask values strictly below the k-th largest
        k = getattr(self, 'top_k', None)
        if k is not None and vals_sorted.numel() > k:
            threshold = vals_sorted[-k]
            mask_k = vals_sorted <= threshold
            vals_sorted.masked_fill_(mask_k, -float('inf'))

        # Scatter back before top-p. Sampling CDF is token-id ordered; the
        # top-p trim itself uses canonical (logit desc, token id asc) order.
        temp_logits = torch.empty_like(vals_sorted).scatter(
            dim=-1, index=idx_sorted, src=vals_sorted)
        temp_logits = self._restore_argmax_if_empty(
            temp_logits, pre_trunc_logits, idx_sent)

        # Top-p: on the post-top-k finite proof support
        p = getattr(self, 'top_p', 1.0)
        check_for_borderline = False
        if p < 1.0:
            temp_logits = self._apply_stable_top_p_support(
                temp_logits, idx_sent, p)

        # 4) Final normalization
        log_Z = torch.logsumexp(temp_logits, dim=0)
        probs = torch.exp(temp_logits - log_Z)

        # 5) Build ID-sorted CDF in double for determinism
        order = torch.argsort(idx_sent)
        sorted_probs = probs[order]
        cdf = torch.cumsum(sorted_probs.cpu(), dim=0)
        
        # 6) Sample using u
        pos = (cdf >= u).nonzero(as_tuple=True)[0][0].item()
        sampled_token = idx_sent[order][pos].item()
        sampled_prob = cdf[pos].item()
        
        # Get CDF value for query token if requested
        query_cdf = None
        if query_token is not None:
            query_pos = (idx_sent[order] == query_token).nonzero(as_tuple=True)[0].cpu()
            lower = cdf[query_pos-1].item() if query_pos > 0 else 0.0
            upper = cdf[query_pos].item()        
            if len(query_pos) > 0:
                query_cdf = cdf[query_pos[0]].item()
            else:
                query_cdf = 0.0  # Token not in distribution after pruning
        else:
            lower = 0
            upper = 1

        lower = cdf[query_pos-1].item() if query_pos > 0 else 0.0
        upper = cdf[query_pos].item()        
        if not (lower < u <= upper) and check_for_borderline:
            vals_sorted_new = vals_sorted_raw.masked_fill_(mask_p_h, -float('inf'))
            temp_logits = torch.empty_like(vals_sorted_new).scatter(dim=-1, index=idx_sorted, src=vals_sorted_new)
            log_Z = torch.logsumexp(temp_logits, dim=0)
            probs = torch.exp(temp_logits - log_Z)
            order = torch.argsort(idx_sent)
            sorted_probs = probs[order]
            cdf = torch.cumsum(sorted_probs.cpu(), dim=0)
            lower = cdf[query_pos-1].item() if query_pos > 0 else 0.0
            upper = cdf[query_pos].item()        
            if (lower < u <= upper):
                return {
                    'token': sampled_token,
                    'prob': sampled_prob,
                    'cdf': cdf,
                    'idx_sent': idx_sent[order],
                    'log_Z': log_Z.item(),
                    'query_cdf': query_cdf
                }    
            else:
                vals_sorted_new = vals_sorted_raw.masked_fill_(mask_p_l, -float('inf'))
                temp_logits = torch.empty_like(vals_sorted_new).scatter(dim=-1, index=idx_sorted, src=vals_sorted_new)
                log_Z = torch.logsumexp(temp_logits, dim=0)
                probs = torch.exp(temp_logits - log_Z)
                order = torch.argsort(idx_sent)
                sorted_probs = probs[order]
                cdf = torch.cumsum(sorted_probs.cpu(), dim=0)
                lower = cdf[query_pos-1].item() if query_pos > 0 else 0.0
                upper = cdf[query_pos].item()                 
                return {
                    'token': sampled_token,
                    'prob': sampled_prob,
                    'cdf': cdf,
                    'idx_sent': idx_sent[order],
                    'log_Z': log_Z.item(),
                    'query_cdf': query_cdf
                }    
                            
        return {
            'token': sampled_token,
            'prob': sampled_prob,
            'cdf': cdf,
            'idx_sent': idx_sent[order],
            'log_Z': log_Z.item(),
            'query_cdf': query_cdf
        } 
   
    # ----------------------------------------------------------------- #
    #                        Lightweight verification
    # ----------------------------------------------------------------- #

    def _verify_block_sanity(self, target_override_hex: Optional[str] = None) -> bool:
        """Verify block-level proof-of-work.

        Args:
            target_override_hex: Slice 11 share-mode threshold
                override. When set, this replaces ``self.proof['target']``
                in the FINAL ``check_hash_against_target`` step and
                ONLY that step. Every other check — VDF, sampling
                hash recomputation, header_prefix integrity, nonce
                extraction — is run byte-identically. This keeps
                the proof bound to the chain/model-adjusted block
                difficulty in all upstream stages; the override
                only relaxes the acceptance threshold so a share
                that doesn't meet the full block target can still
                be accepted under share-mode.
        """
        with self.logger.verification_context(step="block_sanity_check"):
            try:
                # 1. Verify VDF
                vdf_valid = chiavdf_verify(
                    self.proof['block_hash'],
                    self.proof['vdf'],
                    self.proof['tick']
                )

                if not vdf_valid:
                    self.logger.error(
                        "VDF verification failed",
                        failure_type="vdf_verification_failure",
                        hash_id=self.proof.get('hash'),
                        proof_data={
                            'vdf': self.proof.get('vdf'),
                            'tick': self.proof.get('tick'),
                            'block_hash': self.proof.get('block_hash')
                        }
                    )
                    return False
                else:
                    self.logger.debug("VDF verification succeeded")


                # 2. Check header_prefix + hash[:32] < target
                header_bytes = hex_to_bytes_tensor(self.proof['header_prefix'])
                header_data = torch.tensor(list(header_bytes), dtype=torch.uint8)

                # Extract nonce from hash
                hash_bytes = hex_to_bytes_tensor(self.proof['hash'])
                hash_matches = self._verify_final_hash(self.proof['hash'])
                if not hash_matches:
                    self.logger.error(
                        "Sampling hash inconsistent with recomputation",
                        failure_type="hash_verification_failure",
                        hash_id=self.proof.get('hash'),
                        proof_data={
                            'provided_hash': self.proof.get('hash'),
                            'target': self.proof.get('target')
                        }
                    )
                    return False

                nonce = hash_bytes[:4]

                # Build complete header
                header = torch.cat([header_data, nonce])

                # Double SHA-256
                first_hash = sha256_many(header.unsqueeze(0))
                header_hash = sha256_many(first_hash)

                if target_override_hex:
                    # Share-mode: gate on the easier override target
                    # in lieu of the proof's embedded block target.
                    # Normalise the same way the verify-service
                    # cache key does so the worker and the service
                    # cannot disagree on capitalisation / 0x prefix.
                    normalised = target_override_hex.strip().lower()
                    if normalised.startswith("0x"):
                        normalised = normalised[2:]
                    target = hex_to_bytes_tensor(normalised)
                    effective_target_hex = normalised
                else:
                    target = hex_to_bytes_tensor(self.proof['target'])
                    effective_target_hex = self.proof['target']
                if not check_hash_against_target(header_hash, target)[0]:
                    self.logger.error(
                        "Header hash does not meet target",
                        failure_type="hash_verification_failure",
                        hash_id=self.proof.get('hash'),
                        proof_data={
                            'provided_hash': self.proof.get('hash'),
                            'target': effective_target_hex,
                            'target_override_applied': bool(target_override_hex),
                            'block_target': self.proof.get('target'),
                        }
                    )
                    return False

                self.logger.debug("  ✅ Block-level sanity checks passed")
                return True
            except Exception as e:
                self.logger.error(
                    f"Block sanity check failed with exception: {e}",
                    failure_type="block_sanity_exception",
                    hash_id=self.proof.get('hash')
                )
                return False                

    def _verify_final_hash(self,provided_hash) -> bool:
        context_tokens = torch.cat([self.prompt_tokens,self.chosen_tokens])
        recomputed_hash = self._get_u(context_tokens, 0, hash_out=True)
        return recomputed_hash == provided_hash

    def _verify_metadata(self) -> bool:
        """Verify metadata consistency."""
        # TODO: Implement C++ FFI checks
        # 1. Check nonce == hash[:32]
        # 2. Verify model exists and target calculation
        
        print("  ✅ Metadata consistency verified (placeholder)")
        return True
        
    def _verify_parameters(self) -> bool:
        """Verify proof parameters are within bounds."""
        with self.logger.verification_context(step="parameter_validation"):
            # Match the live C++ Quick verifier envelope: endpoints are valid
            # for mining proofs, so a Quick-accepted boundary proof must not be
            # rejected by Full only because Python used stricter inequalities.
            checks = [
                (TOPK_MIN <= self.top_k <= TOPK_MAX, f"top_k out of range: {self.top_k}"),
                (TOPP_MIN <= self.top_p <= TOPP_MAX, f"top_p out of range: {self.top_p}"),
                (TEMP_MIN <= self.temperature <= TEMP_MAX, f"temperature out of range: {self.temperature}"),
                (REP_PENALTY < self.repetition_penalty <= 1, f"repetition_penalty out of range: {self.repetition_penalty}"),
            ]
            failed_checks = []
            for check, msg in checks:
                if not check:
                    failed_checks.append(msg)   
            if failed_checks:
                self.logger.error(
                    f"Parameter validation failed: {failed_checks}",
                    failure_type="parameter_validation_failure",
                    hash_id=self.proof.get('hash'),
                    metrics={
                        'top_k': self.top_k,
                        'top_p': self.top_p,
                        'temperature': self.temperature,
                        'repetition_penalty': self.repetition_penalty,
                    },
                )
                return False

            self.logger.debug("  ✅ All parameters within bounds")
            return True

    @staticmethod
    def _conservative_reuse_bits(mass_upper: float) -> int:
        if mass_upper <= 0.0:
            return REUSE_SCORE_Q32_BITS
        if mass_upper >= 1.0:
            return 0

        bits = 0
        threshold = 0.5
        while bits < REUSE_SCORE_Q32_BITS and mass_upper <= threshold:
            bits += 1
            threshold *= 0.5
        return bits

    def _reuse_score_q32_from_bounds(self, lower, upper) -> int:
        lower_vals = lower.detach().cpu().tolist() if torch.is_tensor(lower) else list(lower)
        upper_vals = upper.detach().cpu().tolist() if torch.is_tensor(upper) else list(upper)
        if len(lower_vals) != len(upper_vals):
            raise ValueError("entropy bounds size mismatch")

        prefix_bits = 0
        reuse_score_q32 = 0
        for i, (lo, hi) in enumerate(zip(lower_vals, upper_vals)):
            lo = float(lo)
            hi = float(hi)
            if not math.isfinite(lo) or not math.isfinite(hi) or hi < lo:
                raise ValueError(f"invalid entropy bounds at step {i}: lower={lo} upper={hi}")

            mass_upper = min(1.0, max(0.0, (hi - lo) + 2.0 * ATOL))
            bits = self._conservative_reuse_bits(mass_upper)
            prefix_bits = min(REUSE_SCORE_Q32_BITS, prefix_bits + bits)
            reuse_score_q32 += (
                1 if prefix_bits >= REUSE_SCORE_Q32_BITS
                else REUSE_SCORE_Q32_ONE >> prefix_bits
            )
        return reuse_score_q32

    def _should_enforce_reuse_entropy(self) -> bool:
        # Version-keyed and stateless: legacy proofs (version < REUSE_GATE_VERSION)
        # are grandfathered; v2+ proofs are enforced. No height/chain context needed.
        # A missing/garbage version defaults to legacy (safe: never falsely rejects).
        try:
            version = int(self.proof.get('version', 0))
        except (TypeError, ValueError):
            version = 0
        return version >= REUSE_GATE_VERSION

    def _verify_reuse_entropy(self, lower, upper) -> bool:
        if not self._should_enforce_reuse_entropy():
            return True

        try:
            reuse_score_q32 = self._reuse_score_q32_from_bounds(lower, upper)
        except ValueError as exc:
            self.logger.error(
                f"Entropy score failed: {exc}",
                failure_type="entropy_score_failure",
                hash_id=self.proof.get('hash'),
            )
            return False

        if reuse_score_q32 > REUSE_SCORE_CAP_Q32:
            self.logger.error(
                "Expected reuse score too high",
                failure_type="entropy_score_failure",
                hash_id=self.proof.get('hash'),
                metrics={
                    'reuse_score_q32': reuse_score_q32,
                    'reuse_score_cap_q32': REUSE_SCORE_CAP_Q32,
                    'reuse_forwards_estimate': reuse_score_q32 / REUSE_SCORE_Q32_ONE,
                },
            )
            return False

        return True

    def _verify_parameters_audit(self) -> bool:
        """Sanity-only parameter validation for AUDIT (logits) proofs.

        The mining envelope (_verify_parameters) — top_k/top_p/temperature
        bounds and the realized reuse entropy gate — is an
        anti-grinding ECONOMIC rule for mining, not an authenticity check.
        Audit proofs bind the completion to the claimed model through the
        recorded top-k logit values replayed in sequence verification,
        which holds regardless of sampling entropy. Near-greedy inference
        (tool calls, low temperature, top_k=1) is therefore admissible
        here while staying invalid for mining. Exact greedy
        (temperature < ~1e-5) never reaches the PoW sampler at all, so a
        positive temperature is still required as a type/shape sanity
        bound, not an entropy bound.
        """
        with self.logger.verification_context(step="parameter_validation_audit"):
            checks = [
                (0 < self.top_k, f"top_k out of range: {self.top_k}"),
                (0 < self.top_p <= 1, f"top_p out of range: {self.top_p}"),
                (0 < self.temperature <= TEMP_MAX, f"temperature out of range: {self.temperature}"),
                (0 < self.repetition_penalty <= 2, f"repetition_penalty out of range: {self.repetition_penalty}"),
            ]
            failed_checks = [msg for check, msg in checks if not check]
            if failed_checks:
                self.logger.error(
                    f"Audit parameter validation failed: {failed_checks}",
                    failure_type="audit_parameter_validation_failure",
                    hash_id=self.proof.get('hash'),
                    metrics={
                        'top_k': self.top_k,
                        'top_p': self.top_p,
                        'temperature': self.temperature,
                        'repetition_penalty': self.repetition_penalty,
                    },
                )
                return False

            self.logger.debug("  ✅ All audit parameters sane")
            return True

    def _verify_step_light(self, step_idx: int):
        """Verify a single generation step with ID-ordered CDF lookup,
        including top-k and top-p pruning."""
        # Build context
        context_tokens = torch.cat([
            self.prompt_tokens,
            self.chosen_tokens[:step_idx]
        ])
        # Get expected values
        expected_token = self.chosen_tokens[step_idx].item()
        expected_prob = self.expected_probs[step_idx].item()
        expected_u = self.expected_u[step_idx].item()

        # Get u
        u = self._get_u(context_tokens, step_idx)
        if abs(u - expected_u) > 1e-7:
            print(f"  ⚠️  Step {step_idx}: u mismatch: {u} vs {expected_u}")

        # Gather support logits & ids (after dedupe)
        raw_idx = torch.tensor(self.expected_topk_indices[step_idx],
                               dtype=torch.long, device=self.device)
        raw_logits = torch.tensor(self.expected_topk_logits[step_idx],
                                  dtype=torch.float32, device=self.device)
        tok2logit: Dict[int, float] = {}
        for tok, logit in zip(raw_idx.tolist(), raw_logits.tolist()):
            if tok not in tok2logit or logit > tok2logit[tok]:
                tok2logit[tok] = logit
        idx_sent = torch.tensor(list(tok2logit.keys()),
                                dtype=torch.long, device=self.device)
        val_sent = torch.tensor(list(tok2logit.values()),
                                dtype=torch.float32, device=self.device)

        # Sample
        result = self._sample(idx_sent, val_sent, context_tokens, u, 
                             expected_lse=self.expected_lse[step_idx][0].item(),query_token=expected_token)
        
        # Verify
        pos = (result['idx_sent'] == expected_token).nonzero(as_tuple=True)[0].item()
        recon_p = float(result['cdf'][pos].item())
        
        # Check u consistency: u must lie in (cdf[pos-1], cdf[pos]]
        lower = result['cdf'][pos-1].item() if pos > 0 else 0.0
        upper = result['cdf'][pos].item()
        u_consistent = (lower-ATOL < u <= upper+ATOL)
        if not u_consistent:
            print(lower , u , upper, expected_u)
            print(pos)
            print(result['cdf'],result['idx_sent'])
        return u_consistent, lower, upper
    
    def verify_sequence_light(self) -> bool:
        """Verify all steps in the proof window and return True if every step's u is consistent."""
        all_passed = True
        lower_bounds = []
        upper_bounds = []
        for i in range(self.window_size):
            step_passed, lower, upper = self._verify_step_light(i)
            lower_bounds.append(lower)
            upper_bounds.append(upper)
            if not step_passed:
                print(i)
                all_passed = False
        if all_passed:
            print(f"✅ All {self.window_size} steps verified successfully.")
        else:
            print(f"❌ Verification failed on one or more steps.")
            
        if self.perform_smell_test:
            try:
                result = self._validate_topk_batch(
                                          torch.as_tensor(self.expected_topk_logits)[:,:50],
                                          torch.as_tensor(self.expected_topk_indices,dtype=torch.int32)[:,:50],
                                          self.stats)
                if result['pass']:
                    print(f"✅  Smell Test Passed")                    
                else:
                    print(f"❌ Smell Test Failed")
            except:
                print(f"❌ Unable to perform small test")
                
        if all_passed and not self._verify_reuse_entropy(lower_bounds, upper_bounds):
            all_passed = False

        return all_passed               

    # ----------------------------------------------------------------- #
    #                        Heavy Weight verification
    # ----------------------------------------------------------------- #
    
    @pow_profiler
    def _cached_gauss(self, dim: int, B: int):
        """Return a (B, dim) tensor of N(0,1) cached inside `self`."""
        # Get the current model device
        model_device = next(self.model.parameters()).device

        key = f"_rnd_{dim}"
        g = getattr(self, key, None)
        if g is None or g.shape[0] < B or g.device != model_device:
            g = torch.randn((B, dim), device=model_device)
            setattr(self, key, g)
        return g[:B]

    @pow_profiler
    def _keyed_gauss(self, step_ids, draws: int, tag: str, dim: int = 74) -> Optional[torch.Tensor]:
        """
        Optional deterministic Gaussian draws keyed by (candidate, step, pass-tag).
        Enabled only when self._eq_keyed_noise = {"enabled": True, "seed": <int>} is set.
        Returns shape (S, draws, dim) where S is number of requested steps.
        """
        cfg = getattr(self, "_eq_keyed_noise", None)
        if not isinstance(cfg, dict) or not bool(cfg.get("enabled", False)):
            return None

        model_device = next(self.model.parameters()).device
        base_seed = int(cfg.get("seed", 0))
        cand = str(getattr(self, "_eq_current_candidate", ""))

        # Cheap deterministic hash for candidate labels.
        cand_hash = 0
        for ch in cand:
            cand_hash = ((cand_hash * 131) + ord(ch)) & 0xFFFFFFFF

        tag_id = {"baseline": 1, "tail1": 2, "tail2": 3}.get(str(tag), 0)

        if isinstance(step_ids, torch.Tensor):
            steps = [int(x) for x in step_ids.detach().cpu().tolist()]
        elif isinstance(step_ids, (list, tuple)):
            steps = [int(x) for x in step_ids]
        else:
            steps = [int(step_ids)]

        draws_i = int(draws)
        dim_i = int(dim)
        out = torch.empty((len(steps), draws_i, dim_i), device=model_device, dtype=torch.float32)

        for row, step in enumerate(steps):
            local_seed = (
                base_seed
                + cand_hash * 1_000_003
                + int(step) * 9_176
                + int(tag_id) * 1_013_904_223
            ) & 0xFFFFFFFFFFFFFFFF
            gen = torch.Generator(device=model_device)
            gen.manual_seed(local_seed)
            out[row] = torch.randn((draws_i, dim_i), device=model_device, generator=gen, dtype=torch.float32)

        return out
    
    @pow_profiler
    def _sigma_from_logits(self, sorted_logits, logits_A, cov_adjuster = None):
        NUM_OPS = 5
        BUCKETS = ((0, 50), (50, 500), (500, 2000), (2000, None))

        ulp_full0 = _ulp(sorted_logits, self.dtype) * 2
        ulp_raw   = _ulp(logits_A,   self.dtype) * 2

        # compute average uLP-induced sigma
        avg_sigma = _sigma_from_ulp(ulp_full0, num_ops=NUM_OPS).mean()

        # per-logit induced sigma
        per_logit_sigma = _sigma_from_ulp(ulp_raw, num_ops=NUM_OPS)

        # floor at the highest of the two, recognizing prior layers’ contributions
        ulp_induced_sigma = torch.maximum(
            torch.full_like(ulp_raw, avg_sigma),
            per_logit_sigma
        )

        # apply a filter based on logit magnitude
        magnitude_floor = min(sorted_logits.std().item() * 0.005, 0.1)#*0.
        final_logit_sigma = torch.maximum(
            torch.full_like(ulp_raw, magnitude_floor),
            ulp_induced_sigma
        )

        # append mean sigma for each bucket
        for start, end in BUCKETS:
            if end is not None:
                new_sigma = final_logit_sigma.mean() / math.sqrt((end - start) / 4)
            else:
                new_sigma = final_logit_sigma.mean() / math.sqrt((ulp_full0.size(0) - start) / 4)
            final_logit_sigma = torch.cat([final_logit_sigma, torch.as_tensor(new_sigma).unsqueeze(0)], dim=0)
        # print(final_logit_sigma/cov_adjuster)
        
        if cov_adjuster is not None:
            final_logit_sigma = torch.maximum(
                final_logit_sigma,
                cov_adjuster
            )            

        # compute the error covariance
        model_device = next(self.model.parameters()).device
        self.logit_correll = self.logit_correll.to(model_device)  # Ensure on model device        
        self.sigma_err = torch.outer(final_logit_sigma, final_logit_sigma) * self.logit_correll
        return self.sigma_err
    
    @pow_profiler
    def _estimate_logit_errors(self, full_logits_A, saved_idx, full_logits_B):
        vocab_size = full_logits_B.size(-1)
        model_device = next(self.model.parameters()).device
        BUCKETS = ((0, 50), (50, 500), (500, 2000), (2000, None))
        
        if not isinstance(saved_idx, torch.Tensor):
            saved_idx = torch.as_tensor(saved_idx, device=model_device, dtype=torch.long)
        elif saved_idx.dtype != torch.long:
            saved_idx = saved_idx.to(dtype=torch.long)
        
        logits_A = full_logits_A[0, saved_idx]  
        logits_B = full_logits_B[0, saved_idx]  

        delta_mu = full_logits_A[0].mean() - full_logits_B[0].mean()
        delta_raw = (logits_A - logits_B) - delta_mu

        sorted_logitsA, _ = torch.sort(full_logits_A[0], descending=True)
        sorted_logitsB, _ = torch.sort(full_logits_B[0], descending=True)

        mean_A = torch.stack(_bucket_means(sorted_logitsA, BUCKETS,
                                           vocab_size), dim=-1).squeeze(0)  # (4,)            
        mean_B = torch.stack(_bucket_means(sorted_logitsB, BUCKETS,
                                           vocab_size), dim=-1).squeeze(0)  # (4,)    

        delta_mean = mean_A - mean_B - delta_mu  # (4,)

        # rank calculation
        inv_rank = torch.empty_like(_)
        inv_rank[_] = torch.arange(_.size(0), device=model_device)
        ranks = inv_rank[saved_idx[:50]]      
        rank_errors = ((ranks-torch.arange(50,device=model_device)).abs().sum()).item() 
        return {'delta_mu':delta_mu,
                'delta_raw':delta_raw,
                'delta_mean':delta_mean,
                'rank_errors':rank_errors
               }

    @pow_profiler
    def _test_quantile(T_null, T_obs):
        p01 = torch.quantile(T_null,0.01)
        p05 = torch.quantile(T_null,0.05)        
        p10 = torch.quantile(T_null,0.1)
        p50 = torch.quantile(T_null,0.5)
        if T_obs < p01 - 1*(p50-p01):
            return "hard_break"
        elif T_obs < p01 - 0.25*(p50-p01):
            return "one_break"
        elif T_obs < p05:
            return "95th"
        elif T_obs < p10:
            return "90th"
        else:
            return "fine" 
        
    def _sort_tensor_pairs_v2(self, tensor1, tensor2):
        """
        Alternative implementation using stable sort twice (more reliable).
        """
        device = tensor1.device
        tensor2 = tensor2.to(device)
        
        if tensor2.dtype != torch.long:
            tensor2 = tensor2.to(dtype=torch.long)
        
        indices = torch.arange(len(tensor1), device=device, dtype=torch.long)

        # First sort by secondary key (tensor2, ascending)
        _, temp_indices = torch.sort(tensor2[indices], stable=True)
        indices = indices[temp_indices]

        # Then sort by primary key (tensor1, descending) - use stable sort to preserve secondary order
        _, temp_indices = torch.sort(-tensor1[indices], stable=True)
        sort_indices = indices[temp_indices]

        return tensor1[sort_indices], tensor2[sort_indices], sort_indices

    @pow_profiler 
    def _verify_step_multivariate_continous(
            self, step_idx: int, debug: bool = False,
            batch_size: int = 1, bootstrap: int = 15_000,
            flash=False, 
            kv=False,
            enable_math: bool | None = None,      
            enable_mem: bool | None = None,             
            cov_adjuster= None,
            charting = False,
            padding = 0,
            mca_noise_value=None
        ):
        """

        """
        # ---------- constants ---------------------------------------------------
        BUCKETS = ((0, 50), (50, 500), (500, 2000), (2000, None))
        model_device = next(self.model.parameters()).device

        # ---------- full-vocab forward (platform B) -----------------------------
        ctx = torch.cat([self.prompt_tokens,
                        torch.ones(padding, dtype=self.prompt_tokens.dtype, device=self.prompt_tokens.device),
                         self.chosen_tokens[:step_idx]]).to(model_device)
                
        attn_mask = torch.cat([
            (~self.pad_mask).long(),  # Convert to 1s and 0s
            torch.zeros(padding, dtype=torch.long, device=self.pad_mask.device),
            torch.ones_like(self.chosen_tokens[:step_idx], dtype=torch.long)
        ]).to(model_device).contiguous()        

        full_logits = self._logits(ctx, batch_size, flash=flash, kv=kv, attn_mask=attn_mask, enable_math=enable_math, enable_mem=enable_mem, mca_noise_value=mca_noise_value)
        
        full_logits = full_logits.to(torch.float32)
        vocab_size = full_logits.size(-1)

        # ---------- persisted 70-logit snapshot (platform A) --------------------
        saved_idx = torch.as_tensor(self.expected_topk_indices[step_idx],
                                    device=model_device, dtype=torch.long).to(model_device)
        logits_A = torch.as_tensor(self.expected_topk_logits[step_idx],
                                   device=model_device, dtype=torch.float32).to(model_device)  # (70,)
        logits_A, saved_idx, perm = self._sort_tensor_pairs_v2(logits_A.clone(), saved_idx.clone())

        logits_B = full_logits[0, saved_idx]  # (70,)

        # ULP calculations
        sorted_logits0, _ = torch.sort(full_logits[0], descending=True)
        ulp_raw = _ulp(logits_A, self.dtype)*2  # (70,)
        ulp_rawB = _ulp(logits_B, self.dtype)*2  # (70,)

        # rank calculation
        inv_rank = torch.empty_like(_)
        inv_rank[_] = torch.arange(_.size(0), device=model_device)
        ranks = inv_rank[saved_idx[:50]]        

        # ---------- global mean offset ------------------------------------------
        mu_A = self.expected_lse[step_idx, -1].to(torch.float32).to(model_device)  # scalar
        delta_mu = mu_A - full_logits[0].mean()  # scalar

        # ---------- 70 raw deltas -----------------------------------------------
        delta_raw = (logits_A - logits_B) - delta_mu  # (70,)

        # ---------- four bucket means -------------------------------------------
        sorted_logits, _ = torch.sort(full_logits[0].unsqueeze(0),
                                      dim=-1, descending=True)  # (1,V)

        mean_B = torch.stack(_bucket_means(sorted_logits, BUCKETS,
                                           vocab_size), dim=-1).squeeze(0)  # (4,)
        mean_A = self.expected_lse[step_idx, 1:5].to(torch.float32).to(model_device)   # (4,)
        delta_mean = mean_A - mean_B - delta_mu  # (4,)


        # ========== P-VALUE COMPUTATION ========================================
        disc, cont = 70, 4
        Σ_err = self._sigma_from_logits(sorted_logits0,logits_B,cov_adjuster)
        self.Σ_err = Σ_err
        invΣ = torch.linalg.inv(Σ_err)  # Precompute once

        # Compute observed vector
        base_quant = _snap(logits_B, ulp_raw)  # Quantized base (70,)
        D_obs = logits_A - base_quant          # Observed discrete diff (70,)
        C_obs = delta_mean                     # Observed continuous diff (4,)
        v_obs = torch.cat([D_obs, C_obs])      # Full observed vector (74,)
        T_obs = v_obs @ invΣ @ v_obs           # Mahalanobis distance (scalar)
        R_obs = ((ranks-torch.arange(50,device=model_device)).abs().sum()).item()                # scalar

        # Generate null samples (correlated errors)
        L = torch.linalg.cholesky(Σ_err, upper=False)
        keyed = self._keyed_gauss(step_idx, bootstrap, "baseline", dim=74)
        if keyed is not None:
            samples = keyed[0] @ L.T
        else:
            samples = self._cached_gauss(74, bootstrap) @ L.T  # (B,74)

        # Compute discrete part for null samples
        x = logits_B - delta_mu + samples[:, :disc]  # (B,70)
        x_quant = _snap(x, ulp_raw)                  # Quantize with per-dim ULP
        D_null = base_quant - x_quant                # (B,70)

        if charting:
            plt.hist(D_null.cpu().numpy().flatten(), bins=30,density = 1)
            plt.hist(D_obs.cpu().numpy().flatten(), bins=30,density = 1)
            plt.show()

        # Continuous part is direct from samples
        C_null = samples[:, disc:disc+cont]  # (B,4)

        # Combine into full null vectors
        V_null = torch.cat([D_null, C_null], dim=1)  # (B,74)

        # Compute test statistics for null samples
        tmp = V_null @ invΣ  # (B,74)
        T_null = (tmp * V_null).sum(dim=1)  # (B,)

        if charting:
            plt.hist(np.sqrt(T_null.cpu().numpy().flatten()), bins=100,density = 1)
            plt.show()


        # Compute p-value (proportion of null samples more extreme than observed)
        p_value = (T_null >= T_obs).to(torch.float32).mean().item()

        # Improve p-value granularity if landing on the tail 
        if p_value < 0.01:
            samples = self._cached_gauss(74, bootstrap*10) @ L.T  # (B,74)
            x = logits_B - delta_mu + samples[:, :disc]  # (B,70)
            x_quant = _snap(x, ulp_raw)                  # Quantize with per-dim ULP
            D_null = base_quant - x_quant                # (B,70)
            C_null = samples[:, disc:disc+cont]  # (B,4)
            V_null = torch.cat([D_null, C_null], dim=1)  # (B,74)
            tmp = V_null @ invΣ  # (B,74)
            T_null = (tmp * V_null).sum(dim=1)  # (B,)

            # Compute p-value (proportion of null samples more extreme than observed)
            p_value = (T_null >= T_obs).to(torch.float32).mean().item()
            if p_value < 0.001:
                L = torch.linalg.cholesky(Σ_err*1.2, upper=False)
                samples = self._cached_gauss(74, bootstrap*2) @ L.T  # (B,74)                
                x = logits_B - delta_mu + samples[:, :disc]  # (B,70)
                x_quant = _snap(x, ulp_raw)                  # Quantize with per-dim ULP
                D_null = base_quant - x_quant                # (B,70)
                C_null = samples[:, disc:disc+cont]  # (B,4)
                V_null = torch.cat([D_null, C_null], dim=1)  # (B,74)
                tmp = V_null @ invΣ  # (B,74)
                T_null = (tmp * V_null).sum(dim=1)  # (B,)

                # Compute p-value (proportion of null samples more extreme than observed)
                p_value = (T_null >= T_obs).to(torch.float32).mean().item()

        u = self._get_u(ctx, step_idx)

        result = self._sample(
            torch.arange(vocab_size, device=model_device),
            full_logits[0],
            ctx,
            u,
            expected_lse=None,
            query_token=self.chosen_tokens[step_idx].item()
        )

        if charting and result['token'] != self.chosen_tokens[step_idx].item():
            print(f"Real logits sample: {result['token']} (expected: {self.chosen_tokens[step_idx].item()})")
            print(self.expected_probs[step_idx].item(),result['prob'],u,result['query_cdf'])

        # ========== RETURN RESULTS =============================================
        return {
            'p_value': p_value,
            'T_obs': T_obs.item(),
            'delta_raw': logits_A - logits_B,
            'delta_mean': delta_mean,
            'grid_fail': not torch.allclose(_snap(logits_A, _ulp(logits_A,self.stated_dtype)*2), logits_A, atol=1e-6),
            'ulp_fail': torch.any(torch.abs(delta_raw) >= 6 * torch.sqrt(torch.diag(Σ_err)).max()).item(),
            "sampling_noise": result['query_cdf']-self.expected_probs[step_idx].item(),
            'delta_mu': delta_mu,
            'full_logits':full_logits,
            "R_obs":R_obs,
            'sorted_logits':sorted_logits,
        }           

    @pow_profiler          
    def verify_full_sequence_adaptive(
            self,
            window_size: Optional[int] = None,
            batch_candidates: List[int] = [2, 5, 10, 20],
            debug: bool = False,
            flash = False,
            kv=True,
            bootstrap = 15_000,
            p_threshold = 0.01,
            charting=False
        ) -> None:
        """
        """
        # Respect thread-scoped MCA param if enabled; otherwise fallback default
        MCA_NOISE = float(_K_ATTN_CV.get()) if _MCA_ENABLED.get() else 8.0
        if window_size is None:
            window_size = self.window_size

        all_sampling_noise = []
        all_delta_mu = []
        delta_mu_calib = []
        rank_error_calib = []
        rank_error_actual = []
        p_values = []
        ulp_fails = []
        status = None

        #--- global running cov estimatores
        model_device = next(self.model.parameters()).device

        device=model_device
        logits_cov  = RunningMeanCov(dim=1, device=device)
        means_cov  = RunningMeanCov(dim=4, device=device)
        logits_cov_spda  = RunningMeanCov(dim=1, device=device)
        means_cov_spda  = RunningMeanCov(dim=4, device=device)

        #--- warm up covariance estimator
        for i in tqdm(range(40), desc="Steps", unit="step"): 
            out_base = self._verify_step_multivariate_continous(i,batch_size=1,bootstrap=bootstrap,debug=debug,kv=kv,flash=flash)
            out_batch = self._verify_step_multivariate_continous(i,batch_size=3,bootstrap=bootstrap,debug=debug,kv=kv,flash=flash)
            out_spda = self._verify_step_multivariate_continous(i,batch_size=1,bootstrap=bootstrap,debug=debug,kv=kv,flash=flash,mca_noise_value=MCA_NOISE)
            errors = self._estimate_logit_errors(out_base['full_logits'],torch.as_tensor(self.expected_topk_indices[i],device=model_device),out_batch['full_logits'])
            errors_spda = self._estimate_logit_errors(out_base['full_logits'],torch.as_tensor(self.expected_topk_indices[i],device=model_device),out_spda['full_logits'])
            for err in errors['delta_raw']:
                logits_cov.update(err) 
            means_cov.update(errors['delta_mean'])
            for err in errors_spda['delta_raw']:
                logits_cov_spda.update(err) 
            means_cov_spda.update(errors_spda['delta_mean'])
            
            #---- grid snap failure: immediate verification failure - exit before wasting time 
            if out_base.get('grid_fail', False):
                status = "RED"
                return (status, "Grid snap failed, there was tampering with stated precision")            

        #--- run actual test            
        for i in tqdm(range(window_size), desc="Steps", unit="step"): 
            #--- local running cov estimatores
            logits_cov_local  = RunningMeanCov(dim=1, device=device)
            means_cov_local  = RunningMeanCov(dim=4, device=device)

            cov_adjuster = torch.sqrt(torch.cat([torch.maximum(logits_cov.covariance,logits_cov_spda.covariance).repeat(70,1), 
                                                 torch.diag(torch.maximum(means_cov.covariance,means_cov_spda.covariance)).unsqueeze(1)], dim=0)).squeeze().to(torch.float32)                   

            #---- obtain baseline at batch size 1 
            out_base = self._verify_step_multivariate_continous(i,batch_size=1,bootstrap=bootstrap,debug=debug,kv=kv,flash=flash,cov_adjuster=cov_adjuster)
            sampling_noise, delta_mu = out_base["sampling_noise"], out_base["delta_mu"] 

            #---- grid snap failure: immediate verification failure - exit before wasting time 
            if out_base.get('grid_fail', False):
                status = "RED"
                return (status, "Grid snap failed, there was tampering with stated precision")

            #---- estimate operational driven error with different float operations                
            out_batch = self._verify_step_multivariate_continous(i,batch_size=3,bootstrap=bootstrap,debug=debug,kv=kv,flash=flash,cov_adjuster=cov_adjuster)
            out_spda = self._verify_step_multivariate_continous(i,batch_size=1,bootstrap=bootstrap,debug=debug,kv=kv,flash=flash,cov_adjuster=cov_adjuster,mca_noise_value=MCA_NOISE)
            
            sampling_noise = (out_batch["sampling_noise"] if abs(out_batch["sampling_noise"]) < sampling_noise else sampling_noise)
            delta_mu = (out_batch["delta_mu"] if abs(out_batch["delta_mu"]) < delta_mu else delta_mu)

            #---- retain the best output
            if out_base['p_value'] > out_batch['p_value']:
                best = out_base
                best_config = 1 
            else:
                best = out_batch
                best_config = 3 

            #---- add to covariance multiplier estimators
            errors = self._estimate_logit_errors(out_base['full_logits'],torch.as_tensor(self.expected_topk_indices[i],device=model_device),out_batch['full_logits'])
            for err in errors['delta_raw']:
                logits_cov.update(err) 
                logits_cov_local.update(err)             
            means_cov.update(errors['delta_mean'])
            means_cov_local.update(errors['delta_mean'])

            errors_spda = self._estimate_logit_errors(out_base['full_logits'],torch.as_tensor(self.expected_topk_indices[i],device=model_device),out_spda['full_logits'])
            for err in errors_spda['delta_raw']:
                logits_cov_spda.update(err) 
            means_cov_spda.update(errors_spda['delta_mean'])            
            rank_error_calib.append(errors_spda['rank_errors'])
            delta_mu_calib.append(errors_spda['delta_mu'])
            


            #---- low p-value: retry with more batch size options and estimate context specific mutliplier from sample
            trials = 0
            while best['p_value'] < p_threshold and trials <= len(batch_candidates)-1:
                trial = self._verify_step_multivariate_continous(i,batch_size=batch_candidates[trials],bootstrap=bootstrap,debug=debug,kv=kv,flash=flash,cov_adjuster=cov_adjuster)
                sampling_noise = (trial["sampling_noise"] if abs(trial["sampling_noise"]) < sampling_noise else sampling_noise)
                delta_mu = (trial["delta_mu"] if abs(trial["delta_mu"]) < delta_mu else delta_mu)

                if trial['p_value'] > best['p_value']:
                    best = trial
                    best_config = batch_candidates[trials] 
                trials += 1            

                #---- add to covariance multiplier estimators    
                errors = self._estimate_logit_errors(out_base['full_logits'],torch.as_tensor(self.expected_topk_indices[i],device=model_device),trial['full_logits'])
                # delta_mu_calib.append(errors['delta_mu'])
                rank_error_calib.append(errors['rank_errors'])
                for err in errors['delta_raw']:
                    logits_cov.update(err) 
                    logits_cov_local.update(err)
                means_cov.update(errors['delta_mean'])    
                means_cov_local.update(errors['delta_mean'])



            #---- all batch values have failed run best configuragtion with sigma bump based on context emprirical estimation 
            if trials == len(batch_candidates) and best['p_value'] < p_threshold:
                cov_adjuster_local = torch.sqrt(torch.cat([torch.maximum(logits_cov_local.covariance,logits_cov_spda.covariance).repeat(70,1), 
                                                     torch.diag(torch.maximum(means_cov_local.covariance,means_cov_spda.covariance)).unsqueeze(1)], dim=0)).squeeze().to(torch.float32)                   

                trial = self._verify_step_multivariate_continous(i,batch_size=best_config,bootstrap=bootstrap,debug=debug,kv=kv,flash=flash,cov_adjuster=cov_adjuster_local.to(torch.float32))
                sampling_noise = (trial["sampling_noise"] if abs(trial["sampling_noise"]) < sampling_noise else sampling_noise)
                delta_mu = (trial["delta_mu"] if abs(trial["delta_mu"]) < delta_mu else delta_mu)
                if trial['p_value'] > best['p_value']:
                    best = trial                

            #---- count ULP 6-sigma failures and rank swaps 
            p_values.append(best['p_value'])
            ulp_fails.append(best['ulp_fail'])
            rank_error_actual.append(best['R_obs'])

            #---- add sampling noise and platform shift 
            all_sampling_noise.append(sampling_noise)
            all_delta_mu.append(delta_mu)

        if charting:
            plt.hist(rank_error_calib,bins=30,alpha=0.3,density=1)
            plt.hist(rank_error_actual,bins=30,alpha=0.3,density=1)
            plt.show()
            plt.hist(p_values,bins=30,alpha=0.3,density=1)
            plt.show()

        return self._validate_final_results(
            p_values=p_values,
            rank_error_actual=rank_error_actual,
            rank_error_calib=rank_error_calib,
            all_sampling_noise=all_sampling_noise,
            all_delta_mu=all_delta_mu,
            delta_mu_calib=delta_mu_calib,
            charting=charting
        )

    def _validate_final_results(
        self,
        p_values: List[float],
        rank_error_actual: List[float],
        rank_error_calib: List[float],
        all_sampling_noise: List[float],
        all_delta_mu: List[torch.Tensor],
        delta_mu_calib: List[torch.Tensor],
        *,
        charting: bool = False,
        ref_sampling_noise = None,
        delta_raw=None,
        candidate_sampling_noise=None,
        candidate_labels=None,
    ) -> Tuple[str, str]:
        """
        Consolidated end-of-pipeline checks used by both the sequential and the
        efficient-parallel verification flows. Returns (status, message).
        """

        status = None
        msg = ""

        # Optional quick plots
        if charting:
            _ = plt.hist(rank_error_calib, bins=30, alpha=0.3, density=1)
            _ = plt.hist(rank_error_actual, bins=30, alpha=0.3, density=1)
            plt.show()
            _ = plt.hist(p_values, bins=30, alpha=0.3, density=1)
            plt.show()

        # ---- p-value quantile checks
        p_values = np.asarray(p_values)
        p_values_red_qt = [(0.5, 0.75),
                        (0.05, 0.1),
                        (0.01, 0.02),
                        (0.001, 1/256)]
        p_values_amber_qt = [(0.5, 0.60),
                            (0.05, 0.06),
                            (0.01, 0.01),
                            (0.001, 0)]

        if not validate_by_quantiles_lower(p_values, p_values_red_qt):
            charts_data = {
                'p_value_distribution': {
                    'type': 'histogram',
                    'values': p_values.tolist(),
                    'bins': 30,
                    'xlabel': 'P-values',
                    'thresholds': {
                        'critical_threshold': 0.01,
                        'warning_threshold': 0.05
                    }
                }
            }
            charts_data2 = {
                'error_distribution': {
                    'type': 'histogram',
                    'values': torch.stack(delta_raw).flatten().cpu().tolist(),
                    'bins': 50,
                    'xlabel': 'error',
                }
            }            
            self.logger.error(
                f"P values for mahanolobis distance are too far for tolerance",
                failure_type="full_sequence_verification_failure",
                hash_id=self.proof.get('hash'),
                metrics={
                    'status': status,
                    'total_steps': len(p_values),
                    'mean_pvalue': float(p_values.mean()),
                    'min_pvalue': float(p_values.min())
                },
                charts_data=charts_data
            )
            self.logger.error(
                f"P values for mahanolobis distance are too far for tolerance",
                failure_type="full_sequence_verification_failure",
                hash_id=self.proof.get('hash'),
                metrics={
                    'status': status,
                    'total_steps': len(p_values),
                    'mean_pvalue': float(p_values.mean()),
                    'min_pvalue': float(p_values.min())
                },
                charts_data=charts_data2
            )            
            status = "RED"
            return (status, f"P values for mahanolobis distance are too far for tolerance")

        if not validate_by_quantiles_lower(p_values, p_values_amber_qt):
            charts_data = {
                'p_value_distribution': {
                    'type': 'histogram',
                    'values': p_values.tolist(),
                    'bins': 30,
                    'xlabel': 'P-values',
                    'thresholds': {
                        'critical_threshold': 0.01,
                        'warning_threshold': 0.05
                    }
                }
            }
            self.logger.error(
                f"Full sequence verification AMBER",
                hash_id=self.proof.get('hash'),
                metrics={
                    'status': status,
                    'total_steps': len(p_values),
                    'mean_pvalue': float(p_values.mean()),
                    'min_pvalue': float(p_values.min())
                },
                charts_data=charts_data
            )
            status = "AMBER"
            msg = "Mahanolobis distance quantiles are too close for comfort"

        # ---- rank swap test (right-tail)
        rank_error_actual_np = np.asarray(rank_error_actual)
        rank_error_calib_np = np.asarray(rank_error_calib)
        sf, p_value_rank = right_tail_test(rank_error_calib_np // 2, rank_error_actual_np // 2)
        if p_value_rank < 0.0005:
            status = "RED"
            charts_data = {
                'rank_swap_distribution': {
                    'type': 'histogram',
                    'values': rank_error_actual_np.tolist(),
                    'values_ref': rank_error_calib_np.tolist(),
                    'bins': 30,
                    'xlabel': 'Rank swaps',
                }
            }
            self.logger.error(
                f"P values for rank is severly above tolerance",
                failure_type="full_sequence_verification_failure",
                hash_id=self.proof.get('hash'),
                metrics={
                    'status': status,
                    'total_steps': len(p_values),
                    'mean_pvalue': float(rank_error_actual_np.mean()),
                    'max_pvalue': float(rank_error_actual_np.max())
                },
                charts_data=charts_data
            )
            return (status, f"P values for rank is severly above tolerance")
        elif p_value_rank < 0.001:
            status = "AMBER"
            charts_data = {
                'rank_swap_distribution': {
                    'type': 'histogram',
                    'values': rank_error_actual_np.tolist(),
                    'values_ref': rank_error_calib_np.tolist(),
                    'bins': 30,
                    'xlabel': 'Rank swaps',
                }
            }
            self.logger.error(
                f"P values for rank is severly above tolerance",
                hash_id=self.proof.get('hash'),
                metrics={
                    'status': status,
                    'total_steps': len(p_values),
                    'mean_pvalue': float(rank_error_actual_np.mean()),
                    'max_pvalue': float(rank_error_actual_np.max())
                },
                charts_data=charts_data
            )
            msg = "Rank swaps are too high for a full pass"

        # ---- sampling-noise quantiles
        noise_arr = np.stack(all_sampling_noise).ravel()
        noise_qt = [(0.005, 0.50),
                    (0.01, 0.1),
                    (0.03, 0.01),
                    (0.05, 2/256),
                    (0.15, 1/256)]
        noise_bucket_result = _prob_noise_bucket_decision(
            noise_arr,
            noise_qt,
            candidate_sampling_noise=candidate_sampling_noise,
        )
        legacy_noise_valid = all(
            obs <= allowed
            for obs, allowed in zip(
                noise_bucket_result['obs_counts'],
                noise_bucket_result['old_allowed'],
            )
        )
        if noise_bucket_result["valid"] and noise_bucket_result["adaptive_used"] and not legacy_noise_valid:
            # NOTE: enhanced_logger.warning() forwards **kwargs straight to the
            # stdlib Logger (no structured-field support — only error() has it),
            # so the observability payload must be folded into the message.
            self.logger.warning(
                "Prob noise accepted by adaptive MCA bucket calibration "
                f"hash_id={self.proof.get('hash')} "
                f"steps={len(noise_arr)} max_abs={float(np.abs(noise_arr).max()):.4f} "
                f"thresholds={noise_bucket_result['thresholds']} "
                f"obs={noise_bucket_result['obs_counts']} "
                f"old_allowed={noise_bucket_result['old_allowed']} "
                f"empirical_allowed={noise_bucket_result['empirical_allowed']} "
                f"adaptive_allowed={noise_bucket_result['adaptive_allowed']} "
                f"final_allowed={noise_bucket_result['final_allowed']} "
                f"adaptive_enforced={noise_bucket_result['adaptive_enforced']} "
                f"loo_slack={noise_bucket_result['loo_slack']} "
                f"loo_quantile={noise_bucket_result['loo_quantile']} "
                f"candidates={candidate_labels or []}"
            )
        if not noise_bucket_result["valid"]:
            if charting:
                _ = plt.hist(noise_arr, bins=50)
                plt.show()
            status = "RED"
            charts_data = {
                'sampling_noise': {
                    'type': 'histogram',
                    'values': noise_arr.tolist(),
                    'values_ref': ref_sampling_noise if ref_sampling_noise is not None else [],
                    'bins': 50,
                    'xlabel': 'sampling_noise',
                    'thresholds': {
                        'values': noise_bucket_result['thresholds'],
                        'observed_counts': noise_bucket_result['obs_counts'],
                        'old_allowed_counts': noise_bucket_result['old_allowed'],
                        'empirical_allowed_counts': noise_bucket_result['empirical_allowed'],
                        'final_allowed_counts': noise_bucket_result['final_allowed'],
                    },
                }
            }
            if candidate_sampling_noise is not None:
                charts_data['candidate_sampling_noise_matrix'] = {
                    'type': 'raw_matrix',
                    'matrix': candidate_sampling_noise,
                    'labels': candidate_labels or [],
                    'shape': list(np.asarray(candidate_sampling_noise).shape),
                }
            self.logger.error(
                f"Prob noise too big",
                failure_type="full_sequence_verification_failure",
                hash_id=self.proof.get('hash'),
                proof_data=self.proof,
                metrics={
                    'status': status,
                    'total_steps': len(noise_arr),
                    'mean_pvalue': float(noise_arr.mean()),
                    'max_pvalue': float(np.abs(noise_arr).max()),
                    'bucket_thresholds': noise_bucket_result['thresholds'],
                    'bucket_observed_counts': noise_bucket_result['obs_counts'],
                    'bucket_old_allowed_counts': noise_bucket_result['old_allowed'],
                    'bucket_empirical_allowed_counts': noise_bucket_result['empirical_allowed'],
                    'bucket_final_allowed_counts': noise_bucket_result['final_allowed'],
                    'bucket_adaptive_used': noise_bucket_result['adaptive_used'],
                    'bucket_adaptive_available': noise_bucket_result['adaptive_available'],
                    'bucket_adaptive_enforced': noise_bucket_result['adaptive_enforced'],
                    'bucket_adaptive_valid': noise_bucket_result['adaptive_valid'],
                    'bucket_legacy_valid': noise_bucket_result['legacy_valid'],
                    'bucket_adaptive_allowed_counts': noise_bucket_result['adaptive_allowed'],
                    'bucket_loo_slack': noise_bucket_result['loo_slack'],
                    'bucket_loo_quantile': noise_bucket_result['loo_quantile'],
                    'candidate_labels': candidate_labels or [],
                },
                charts_data=charts_data
            )
            return (status, f"Prob noise too big, noise mean {noise_arr.mean():.3e}")

        # ---- platform shift μ quantiles
        mu_arr = torch.stack(all_delta_mu).cpu()
        σ = torch.std(torch.stack(delta_mu_calib)).cpu()
        mu_qt = [(σ * 1, 0.40),
                (σ * 2, 0.20),
                (σ * 5, 0.10),
                (σ * 10, 2/256),
                (σ * 20, 1/256)]
        valid_mu = validate_by_quantiles_higher(mu_arr, mu_qt, two_sided=True)
        if not valid_mu:
            if charting:
                _ = plt.hist(mu_arr, bins=50)
                plt.show()
            status = "RED"
            charts_data = {
                'valid_mu': {
                    'type': 'histogram',
                    'values': mu_arr.tolist(),
                    'bins': 50,
                    'xlabel': 'mu_arr',
                }
            }
            self.logger.error(
                f"Platform shift too big, noise mean",
                failure_type="full_sequence_verification_failure",
                hash_id=self.proof.get('hash'),
                metrics={
                    'status': status,
                    'total_steps': len(mu_arr),
                    'mean_pvalue': float(mu_arr.mean()),
                    'max_pvalue': float(np.abs(mu_arr).max())
                },
                charts_data=charts_data
            )
            return (status, f"Platform shift too big, noise mean {mu_arr.mean():.3e}")

        # ---- final status/message
        if status is not None:
            if status != "AMBER":
                status = "GREEN"
                msg = "All test passed"
        else:
            status = "GREEN"
            msg = "All test passed"
        return (status, msg)


    # ----------------------------------------------------------------- #
    #                        Platform error estimation
    # ----------------------------------------------------------------- #
    
    def _estimate_platform_error(
        self, 
        *, 
        debug: bool = False,
        batch_sizes: List[int] = [1, 2, 4, 8, 16],
        configs: List[Dict[str, bool]] = None,
        n_steps: int = 256,
        n_repeats: int = 3
        ) -> Dict[str, torch.Tensor]:
        """
        Estimate platform-induced error across different compute configurations.
        """
        if configs is None:
            configs = [
                {'flash': False, 'kv': True},   # KV only
            ]
        
        BUCKETS = ((0, 50), (50, 500), (500, 2000), (2000, None))
        
        # Sample steps uniformly across the sequence
        total_steps = len(self.chosen_tokens)
        step_indices = torch.linspace(0, total_steps-1, n_steps, dtype=torch.long)

        # FIRST: Compute reference logits for all steps
        if debug:
            tqdm.write("Computing reference logits...")

        reference_logits = {}
        for attr in list(self.__dict__.keys()):
            if attr.startswith('_kv_cache') or attr.startswith('_cached_ctx'):
                delattr(self, attr)

        for step_idx in step_indices:
            ctx = torch.cat([
                self.prompt_tokens,
                self.chosen_tokens[:step_idx]
            ]).to(self.device)
                           
            # Reference: batch_size=1, no optimizations
            ref = self._logits(ctx, batch_size=1, flash=False, kv=False)
            reference_logits[step_idx.item()] = ref[0].float()

        # SECOND: Test each configuration
        errors_by_config = {str(cfg): [] for cfg in configs}

        for config in configs:
            config_key = str(config)
            if debug:
                tqdm.write(f"\nTesting config: {config_key}")

            config_all_errors = []
            
            outer = tqdm(batch_sizes, desc=f"Config {config_key}", unit="batch", leave=False)

            for batch_size in outer:
                if debug:
                    tqdm.write(f"  Batch size: {batch_size}")

                batch_repeat_errors = []

                inner = tqdm(
                    total=n_repeats * len(step_indices),
                    desc=f"  Steps @ bs={batch_size}",
                    unit="step",
                    leave=False,
                    position=1
                )
                for repeat in range(n_repeats):
                    # Clear caches before each full sequence
                    for attr in list(self.__dict__.keys()):
                        if attr.startswith('_kv_cache') or attr.startswith('_cached_ctx'):
                            delattr(self, attr)

                    sequence_errors = []

                    # Process all steps in sequence (INNERMOST LOOP)
                    for step_idx in step_indices:
                        inner.update(1)
                        ctx = torch.cat([
                            self.prompt_tokens,
                            self.chosen_tokens[:step_idx]
                        ]).to(self.device)

                        # Get logits with current config
                        logits = self._logits(
                            ctx, 
                            batch_size=batch_size,
                            flash=config['flash'],
                            kv=config['kv']
                        )
                        
                        # Compare to reference
                        ref = reference_logits[step_idx.item()]
                        ref_mean = ref.mean()
                        
                        
                        test_logits = logits[0].float()
                        error = (test_logits - ref) - (test_logits.mean() - ref_mean)

                        sequence_errors.append(error)

                    batch_repeat_errors.append(torch.stack(sequence_errors))
                
                inner.close()
                # Average over repeats: [n_steps, vocab]
                mean_errors = torch.stack(batch_repeat_errors).mean(dim=0)
                config_all_errors.append(mean_errors)

            # Store all batch sizes for this config: [n_batch_sizes, n_steps, vocab]
            errors_by_config[config_key] = torch.stack(config_all_errors)
            outer.close()
        
        # return errors_by_config
        # Compute stats for each config
        results = {}
        for key, err_tensor in errors_by_config.items():
            # err_tensor: [n_batch x n_steps x vocab]
            flat = err_tensor.flatten()
            sigma = flat.std(unbiased=True)
            batch_sigmas = torch.tensor([err_tensor[i].flatten().std(unbiased=True).item()
                                         for i in range(len(batch_sizes))])
            # bucket means across all batches & steps
            bucket_means = []
            logits_values = []
            for lo, hi in BUCKETS:
                vals = []
                single_logits_vals = []
                for b in range(err_tensor.size(0)):
                    for si, step_idx in enumerate(step_indices):
                        saved_idx = torch.as_tensor(self.expected_topk_indices[step_idx],
                                                    device=self.device)
                        
                        ref = reference_logits[step_idx.item()]
                        order = torch.argsort(ref, descending=True)
                        idx = order[lo:hi] if hi is not None else order[lo:]
                        single_logits_vals.append(err_tensor[b, si, saved_idx])
                        vals.append(err_tensor[b, si, idx].mean())
                bucket_means.append(torch.stack(vals))
                logits_values.append(torch.stack(single_logits_vals))
                
            results[key] = {
                'sigma': sigma,
                'batch_sigmas': batch_sigmas,
                'bucket_means': bucket_means,
                'logits_values': logits_values,
                'max_abs_error': flat.abs().max(),
            }

        # Debug print
        if debug:
            print("\nPlatform Error Analysis:")
            print("-" * 60)
            for key, s in results.items():
                print(f"\nConfig: {key}")
                print(f"  σ: {s['sigma']:.6f}")
                # print(f"  MAD σ: {s['mad_sigma']:.6f}")
                print(f"  Max|err|: {s['max_abs_error']:.6f}")
                print(f"  Batch σs: {[f'{x:.6f}' for x in s['batch_sigmas'].tolist()]}" )

        self._platform_error_details = results
        return max(v['sigma'] for v in results.values()), results

    # ----------------------------------------------------------------- #
    #                        Lightweight BATCHED
    # ----------------------------------------------------------------- #
    # @pow_profiler
    def _get_u_batch(self, all_contexts, step_indices):
        """Generate deterministic u values for multiple steps at once."""
        batch_size = len(all_contexts)

        # --- 1) Build the windowed-token matrix [batch_size × window_size] ---
        window_tokens = torch.zeros(batch_size, self.window_size,
                                    dtype=torch.int64, device=self.device)
        for i, ctx in enumerate(all_contexts):
            L = min(len(ctx), self.window_size)
            window_tokens[i, -L:] = ctx[-L:]

        # --- 2) Encode everything exactly as in batch_sample_tokens ---
        # tokens → bytes [batch_size, ...]
        ctx_bytes = _tok_le_bytes(window_tokens)
        # steps → little-endian u32 [batch_size, 1→4]
        j4        = _u32le(step_indices.view(-1, 1).to(torch.uint32))
        # tick → a single u32 row [1,4], _build_msg will .view(1,-1).expand(B,-1)
        T8        = _u32le(torch.tensor([self.proof['tick']],
                                        dtype=torch.uint32,
                                        device=self.device))
        # precision string → [batch_size, len(precision)]
        precision_bytes = _str_bytes(self.stated_precision,
                                    batch_size=batch_size,
                                    device=self.device)

        # --- 3) Header and VDF are already 1D, contiguous byte tensors ---
        #    decode once, keep contiguity for .view()/expand() in _build_msg
        header_data = hex_to_bytes_tensor(self.proof['header_prefix'],
                                        device=self.device).contiguous()
        v           = hex_to_bytes_tensor(self.proof['vdf'],
                                        device=self.device).contiguous()

        # --- 4) Build the batched message, hash, and convert to u-values ---
        msg_batch = _build_msg(header_data,
                            v,
                            T8,
                            j4,
                            ctx_bytes,
                            precision_bytes)
        digests   = sha256_many(msg_batch)       # → [batch_size, digest_len]
        return _digest_to_u(digests)             # → [batch_size]

    # @pow_profiler    
    def _sample_batch(self, idx_sent_batch, val_sent_batch, context_tokens_batch, u_batch, 
                    expected_tokens, expected_lse=None):
        """Vectorized sampling for multiple steps at once.
        
        Args:
            idx_sent_batch: (batch_size, max_vocab) token indices
            val_sent_batch: (batch_size, max_vocab) logit values  
            context_tokens_batch: List of context tensors
            u_batch: (batch_size,) u values
            expected_tokens: (batch_size,) expected token ids
        """
        batch_size = idx_sent_batch.shape[0]
        max_vocab = idx_sent_batch.shape[1]
        
        # 1) Temperature scaling
        temp_logits = val_sent_batch / self.temperature
        
        # 2) Apply repetition penalty (vectorized)
        rep_pen = getattr(self, 'repetition_penalty', 1.0)
        if rep_pen != 1.0:
            for b in range(batch_size):
                mask_rep = torch.isin(idx_sent_batch[b], context_tokens_batch[b])
                temp_logits[b, mask_rep] /= rep_pen
        
        # 3) Apply top-k and top-p
        pre_trunc_logits = temp_logits.clone()
        vals_sorted, idx_sorted = temp_logits.sort(dim=-1, descending=False)
        
        # Top-k masking
        k = getattr(self, 'top_k', None)
        if k is not None:
            # Get k-th largest value for each batch
            thresholds = torch.kthvalue(vals_sorted, max(1, vals_sorted.shape[1] - k + 1), dim=-1).values
            mask_k = vals_sorted <= thresholds.unsqueeze(-1)
            vals_sorted.masked_fill_(mask_k, -float('inf'))
        
        # Scatter after top-k. top_p<1 is trimmed over the finite proof support
        # with token id as the secondary key, matching the miner path.
        temp_logits = torch.zeros_like(vals_sorted).scatter(
            dim=-1, index=idx_sorted, src=vals_sorted)
        temp_logits = self._restore_argmax_if_empty_batch(
            temp_logits, pre_trunc_logits, idx_sent_batch)

        # Top-p masking
        p = getattr(self, 'top_p', 1.0)
        check_borderline = torch.zeros(batch_size, dtype=torch.bool, device=self.device)
        mask_p_h_batch = torch.zeros_like(vals_sorted, dtype=torch.bool)
        mask_p_l_batch = torch.zeros_like(vals_sorted, dtype=torch.bool)
        vals_sorted_raw = vals_sorted.clone()
        
        if p < 1.0:
            temp_logits = self._apply_stable_top_p_support_batch(
                temp_logits, idx_sent_batch, p)
        
        # 4) Final normalization
        log_Z = torch.logsumexp(temp_logits, dim=-1)
        probs = torch.exp(temp_logits - log_Z.unsqueeze(-1))
        batched = build_id_sorted_cdfs_vectorized(
            idx_sent_batch=idx_sent_batch,
            probs_base=probs,                         # from your step (4)
            expected_tokens=expected_tokens,
            u_batch=u_batch,
            check_borderline=check_borderline,        # from your top‑p section
            vals_sorted_raw=vals_sorted_raw,
            idx_sorted=idx_sorted,
            mask_p_h_batch=mask_p_h_batch,
            mask_p_l_batch=mask_p_l_batch,
            atol=ATOL,
        )
        return  batched      

    # @pow_profiler
    def verify_sequence_light_vectorized(self) -> bool:
        """Vectorized verification of all steps in the proof window."""
        with self.logger.verification_context(step="sequence_verification"):
            try:
                window_size = self.window_size

                # # 1. Prepare all contexts
                all_contexts = []
                for i in range(window_size):
                    context = torch.cat([self.prompt_tokens, self.chosen_tokens[:i]])
                    all_contexts.append(context)

                # 2. Get all u values at once
                step_indices = torch.arange(window_size, dtype=torch.long, device=self.device)
                u_batch = self._get_u_batch(all_contexts, step_indices)
                
                # 3. Check u values
                expected_u = self.expected_u[:window_size]
                u_matches = torch.abs(u_batch - expected_u) <= 1e-7
                if not u_matches.all():
                    mismatched = (~u_matches).nonzero(as_tuple=True)[0]
                    # Collect chart data for u value analysis
                    charts_data = {
                        'u_value_differences': {
                            'type': 'histogram',
                            'values': (u_batch - expected_u).cpu().numpy(),
                            'bins': 50,
                            'xlabel': 'U Value Difference',
                            'thresholds': {'tolerance': 1e-7}
                        },
                        'u_value_comparison': {
                            'type': 'scatter',
                            'values': u_batch.cpu().numpy(),
                            'x_values': expected_u.cpu().numpy(),
                            'xlabel': 'Expected U',
                            'ylabel': 'Computed U'
                        }
                    }
                    
                    # Include full proof payload so we can pinpoint context/offset issues.
                    self.logger.error(
                        f"U value verification failed for {len(mismatched)} steps",
                        failure_type="u_value_verification_failure",
                        hash_id=self.proof.get('hash'),
                        proof_data=self.proof,
                        metrics={
                            'failed_steps': mismatched.cpu().tolist(),
                            'max_difference': torch.abs(u_batch - expected_u).max().item(),
                            'total_mismatched': len(mismatched)
                        },
                        charts_data=charts_data
                    )
                    return False

                # 4) Prepare batched logits and indices (vectorized + dedupe)
                expected_tokens = self.chosen_tokens[:window_size]  # [B]
                logits_idx_raw = torch.as_tensor(self.expected_topk_indices,
                                                dtype=torch.long, device=self.device)   # [B, K0]
                logits_raw     = torch.as_tensor(self.expected_topk_logits,
                                                dtype=torch.float32, device=self.device) # [B, K0]

                # Deduplicate per step (keep max logit per token), left-pack to fixed width K0
                idx_sent_batch, val_sent_batch, _uniq_counts = dedupe_keep_max_dense(logits_idx_raw, logits_raw)

                pos, lower, upper, cdf, sorted_idx, valid_counts = self._sample_batch(
                    idx_sent_batch,
                    val_sent_batch,
                    all_contexts,
                    u_batch,
                    expected_tokens
                )
                # 6) Vectorized verification (no Python loop)
                u = u_batch.view(-1)
                ok = (u > (lower - ATOL)) & (u <= (upper + ATOL))
                if bool(ok.all()):
                    if not self._verify_reuse_entropy(lower, upper):
                        return False
                    self.logger.debug(f"✅ All {window_size} steps verified successfully")
                    return True
                else:
                    bad = (~ok).nonzero(as_tuple=False).squeeze(1)
                    nbad = bad.numel()
                    self.logger.error(f"❌ Verification failed on {nbad} step(s): {bad[:16].tolist()} ...")
                    return False

            except Exception as e:
                self.logger.error(
                    f"Sequence verification failed with exception: {e}",
                    failure_type="sequence_verification_exception",
                    hash_id=self.proof.get('hash')
                )
                raise

    def verify_sequence_smell_test(self) -> bool:
        """Vectorized verification of all steps in the proof window."""
        with self.logger.verification_context(step="sequence_verification"):
            try:
                if not getattr(self, "stats", None):
                    self.logger.warning("⚠️  Smell test skipped: stats not available")
                    return True
                result = self._validate_topk_batch(
                    torch.as_tensor(self.expected_topk_logits)[:, :50],
                    torch.as_tensor(self.expected_topk_indices, dtype=torch.int32)[:, :50],
                    self.stats
                )
                if result['pass']:
                    self.logger.debug(f"✅  Smell Test Passed")
                    return True
                else:
                    self.logger.debug(f"❌ Smell Test Failed")
                    return False
            except Exception as e:
                self.logger.error(
                    f"❌ Unable to perform smell test: verification failed with exception: {e}",
                    failure_type="sequence_verification_exception",
                    hash_id=self.proof.get('hash')
                )
                self.logger.error(f"❌ Unable to perform smell test: {e}")
                raise

    # @pow_profiler
    def _sample_batch_legacy(self, idx_sent_batch, val_sent_batch, context_tokens_batch, u_batch, 
                    expected_tokens, expected_lse=None):
        """Vectorized sampling for multiple steps at once.
        
        Args:
            idx_sent_batch: (batch_size, max_vocab) token indices
            val_sent_batch: (batch_size, max_vocab) logit values  
            context_tokens_batch: List of context tensors
            u_batch: (batch_size,) u values
            expected_tokens: (batch_size,) expected token ids
        """
        batch_size = idx_sent_batch.shape[0]
        max_vocab = idx_sent_batch.shape[1]
        
        # 1) Temperature scaling
        temp_logits = val_sent_batch / self.temperature
        
        # 2) Apply repetition penalty (vectorized)
        rep_pen = getattr(self, 'repetition_penalty', 1.0)
        if rep_pen != 1.0:
            for b in range(batch_size):
                mask_rep = torch.isin(idx_sent_batch[b], context_tokens_batch[b])
                temp_logits[b, mask_rep] /= rep_pen
        
        # 3) Apply top-k and top-p
        pre_trunc_logits = temp_logits.clone()
        vals_sorted, idx_sorted = temp_logits.sort(dim=-1, descending=False)
        
        # Top-k masking
        k = getattr(self, 'top_k', None)
        if k is not None:
            # Get k-th largest value for each batch
            thresholds = torch.kthvalue(vals_sorted, max(1, vals_sorted.shape[1] - k + 1), dim=-1).values
            mask_k = vals_sorted <= thresholds.unsqueeze(-1)
            vals_sorted.masked_fill_(mask_k, -float('inf'))
        
        # Scatter after top-k. top_p<1 is trimmed over the finite proof support
        # with token id as the secondary key, matching the miner path.
        temp_logits = torch.zeros_like(vals_sorted).scatter(
            dim=-1, index=idx_sorted, src=vals_sorted)
        temp_logits = self._restore_argmax_if_empty_batch(
            temp_logits, pre_trunc_logits, idx_sent_batch)

        # Top-p masking
        p = getattr(self, 'top_p', 1.0)
        check_borderline = torch.zeros(batch_size, dtype=torch.bool, device=self.device)
        mask_p_h_batch = torch.zeros_like(vals_sorted, dtype=torch.bool)
        mask_p_l_batch = torch.zeros_like(vals_sorted, dtype=torch.bool)
        vals_sorted_raw = vals_sorted.clone()
        
        if p < 1.0:
            temp_logits = self._apply_stable_top_p_support_batch(
                temp_logits, idx_sent_batch, p)
        
        # 4) Final normalization
        log_Z = torch.logsumexp(temp_logits, dim=-1)
        probs = torch.exp(temp_logits - log_Z.unsqueeze(-1))
        
        # 5) Build ID-sorted CDFs
        results = []
        for b in range(batch_size):
            # Get valid tokens for this batch element
            valid_mask = idx_sent_batch[b] != -1  # Assuming -1 is padding
            valid_idx = idx_sent_batch[b, valid_mask]
            valid_probs = probs[b, valid_mask]
            
            # Sort by token ID
            order = torch.argsort(valid_idx)
            sorted_idx = valid_idx[order]
            sorted_probs = valid_probs[order]
            cdf = torch.cumsum(sorted_probs.cpu(), dim=0)
            
            # Find position for expected token
            query_pos = (sorted_idx == expected_tokens[b]).nonzero(as_tuple=True)[0]
            
            if len(query_pos) > 0:
                pos = query_pos[0].item()
                lower = cdf[pos-1].item() if pos > 0 else 0.0
                upper = cdf[pos].item()
                
                # Check if we need borderline adjustment
                if check_borderline[b] and not (lower < u_batch[b].item() <= upper):
                    # Try with mask_p_h
                    vals_sorted_new = vals_sorted_raw[b].masked_fill(mask_p_h_batch[b], -float('inf'))
                    temp_logits_new = torch.zeros_like(vals_sorted_new).scatter(
                        dim=-1, index=idx_sorted[b], src=vals_sorted_new
                    )
                    log_Z_new = torch.logsumexp(temp_logits_new, dim=-1)
                    probs_new = torch.exp(temp_logits_new - log_Z_new)
                    
                    valid_probs_new = probs_new[valid_mask]
                    sorted_probs_new = valid_probs_new[order]
                    cdf_new = torch.cumsum(sorted_probs_new.cpu(), dim=0)
                    
                    lower_new = cdf_new[pos-1].item() if pos > 0 else 0.0
                    upper_new = cdf_new[pos].item()
                    
                    if lower_new < u_batch[b].item() <= upper_new:
                        cdf = cdf_new
                        lower = lower_new
                        upper = upper_new
                    else:
                        # Try with mask_p_l
                        vals_sorted_new = vals_sorted_raw[b].masked_fill(mask_p_l_batch[b], -float('inf'))
                        temp_logits_new = torch.zeros_like(vals_sorted_new).scatter(
                            dim=-1, index=idx_sorted[b], src=vals_sorted_new
                        )
                        log_Z_new = torch.logsumexp(temp_logits_new, dim=-1)
                        probs_new = torch.exp(temp_logits_new - log_Z_new)
                        
                        valid_probs_new = probs_new[valid_mask]
                        sorted_probs_new = valid_probs_new[order]
                        cdf_new = torch.cumsum(sorted_probs_new.cpu(), dim=0)
                        
                        cdf = cdf_new
                        lower = cdf_new[pos-1].item() if pos > 0 else 0.0
                        upper = cdf_new[pos].item()
            else:
                pos = -1
                lower = 0.0
                upper = 0.0
            
            results.append({
                'pos': pos,
                'lower': lower,
                'upper': upper,
                'cdf': cdf,
                'sorted_idx': sorted_idx
            })
        
        return results
    
    # @pow_profiler
    def verify_sequence_light_vectorized_legacy(self) -> bool:
        """Vectorized verification of all steps in the proof window."""
        window_size = self.window_size
        
        # # 1. Prepare all contexts
        all_contexts = []
        for i in range(window_size):
            context = torch.cat([self.prompt_tokens, self.chosen_tokens[:i]])
            all_contexts.append(context)

        # 2. Get all u values at once
        step_indices = torch.arange(window_size, dtype=torch.long, device=self.device)
        u_batch = self._get_u_batch(all_contexts, step_indices)
        
        # 3. Check u values
        expected_u = self.expected_u[:window_size]
        u_matches = torch.abs(u_batch - expected_u) <= 1e-7
        if not u_matches.all():
            mismatched = (~u_matches).nonzero(as_tuple=True)[0]
            for idx in mismatched:
                print(f"  ⚠️  Step {idx}: u mismatch: {u_batch[idx].item()} vs {expected_u[idx].item()}")
        
        # # 4. Prepare batched logits and indices
        # # Find max vocabulary size across all steps
        max_vocab_size = max(len(self.expected_topk_indices[i]) for i in range(window_size))
        
        idx_sent_batch = torch.full((window_size, max_vocab_size), -1, dtype=torch.long, device=self.device)
        val_sent_batch = torch.full((window_size, max_vocab_size), -float('inf'), dtype=torch.float32, device=self.device)
        
        for i in range(window_size):
            # Deduplicate
            raw_idx = torch.tensor(self.expected_topk_indices[i], dtype=torch.long, device=self.device)
            raw_logits = torch.tensor(self.expected_topk_logits[i], dtype=torch.float32, device=self.device)
            
            tok2logit = {}
            for tok, logit in zip(raw_idx.tolist(), raw_logits.tolist()):
                if tok not in tok2logit or logit > tok2logit[tok]:
                    tok2logit[tok] = logit
            
            unique_toks = list(tok2logit.keys())
            unique_logits = list(tok2logit.values())
            
            idx_sent_batch[i, :len(unique_toks)] = torch.tensor(unique_toks, dtype=torch.long, device=self.device)
            val_sent_batch[i, :len(unique_logits)] = torch.tensor(unique_logits, dtype=torch.float32, device=self.device)
        
        # # 4) Prepare batched logits and indices (vectorized + dedupe)
        expected_tokens = self.chosen_tokens[:window_size]  # [B]

        results = self._sample_batch_legacy(
            idx_sent_batch, 
            val_sent_batch, 
            all_contexts, u_batch, expected_tokens)

        # 6. Verify all results
        all_passed = True
        for i, result in enumerate(results):
            u_val = u_batch[i].item()
            u_consistent = (result['lower'] - ATOL < u_val <= result['upper'] + ATOL)
            
            if not u_consistent:
                print(f"Step {i}: {result['lower']} < {u_val} <= {result['upper']}, expected_u: {expected_u[i].item()}")
                print(f"  pos: {result['pos']}")
                print(f"  cdf: {result['cdf']}, sorted_idx: {result['sorted_idx']}")
                all_passed = False
        
        if all_passed:
            if not self._verify_reuse_entropy(
                [result['lower'] for result in results],
                [result['upper'] for result in results],
            ):
                return False
            print(f"✅ All {window_size} steps verified successfully.")
        else:
            print(f"❌ Verification failed on one or more steps.")
        
        return all_passed    

    # ----------------------------------------------------------------- #
    #                        Heavy Weight verification BATCHED
    # ----------------------------------------------------------------- #

    @pow_profiler
    @torch.no_grad()
    def _compute_all_logits_parallel_prefix(
        self,
        window_size: int,
        batch_size: int = 1,
        flash: bool = False,
        enable_math: bool | None = None,
        enable_mem: bool | None = None,
        mca_noise_value: float | None = None,
        as_tensor: bool = False,                  # NEW: return (S,B,V) if True
        compact_rows: bool = False,               # NEW: with as_tensor, keep only rows 0 and -1 -> (S,2,V)
        ):
            """
            Compute logits for all verification steps in ONE forward pass.
            If as_tensor is True, returns a single tensor with shape (S,B,V).
            If compact_rows is also True and B>2, only rows 0 and -1 are kept,
            returning (S,2,V). Exact for the v2 path: _verify_steps_from_logits_vectorized
            reads only row 0 (ranks/mean/sampling) and row -1 (saved-index logits), and
            the covariance path (_estimate_logit_errors) reads only row 0. The full forward
            (all B rows, needed for MCA-noise diversity) is still computed transiently; only
            the *retained* tensor is compacted, cutting held memory ~B/2x.
            Else returns the original {step_idx: (B,V)} mapping.
            """
            model_device = next(self.model.parameters()).device

            # Build the full sequence for the longest context
            full_ctx = torch.cat([self.prompt_tokens, self.chosen_tokens[:window_size]]).to(model_device)

            # Proper causal mask
            full_attn_mask = torch.cat([
                (~self.pad_mask).long(),
                torch.ones_like(self.chosen_tokens[:window_size], dtype=torch.long)
            ]).to(model_device)

            with torch.no_grad():
                flash = self.use_flash_attn if flash is None else flash
                enable_math = False if enable_math is None else enable_math
                enable_mem = False  if enable_mem  is None else enable_mem

                # MCA control
                if mca_noise_value is None:
                    noise_cm = mca_enabled(False)
                elif isinstance(mca_noise_value, (int, float)):
                    noise_cm = mca_active(k_attn=mca_noise_value, target_dtype=self.stated_dtype)
                else:
                    noise_cm = nullcontext()

                batched_input = full_ctx.unsqueeze(0).expand(batch_size, -1)
                batched_mask  = full_attn_mask.unsqueeze(0).expand(batch_size, -1)

                with noise_cm:
                    outputs = self.model(
                        input_ids=batched_input,
                        attention_mask=batched_mask,
                        use_cache=False
                    )

            # Select logits at each step’s "next token" position
            prompt_len = len(self.prompt_tokens)
            # positions: predict token at prompt_len + i
            positions = (prompt_len - 1 + torch.arange(window_size, device=model_device))  # (S,)
            # Gather along sequence axis -> (B,S,V)
            sel = outputs.logits.index_select(dim=1, index=positions)                      # (B,S,V)

            if as_tensor:
                if compact_rows and sel.size(0) > 2:
                    # Keep only rows 0 and -1 BEFORE materializing the contiguous
                    # (S,B,V), so the retained tensor is (S,2,V). `outputs`/`sel`
                    # (the full forward) free on return.
                    sel = sel[[0, sel.size(0) - 1], :, :]                                  # (2,S,V)
                return sel.permute(1, 0, 2).contiguous()  # (S,B',V)

            # Backward‑compatible mapping {step_idx: (B,V)}
            step_logits = {}
            for i in range(window_size):
                step_logits[i] = sel[:, i, :].clone()
            return step_logits

    @pow_profiler
    def verify_full_sequence_adaptive_parallel_efficient(
        self,
        window_size: Optional[int] = None,
        batch_candidates: List[int] = [2, 5, 10, 20],
        debug: bool = False,
        flash = False,
        kv=False,  # Not needed for this approach
        bootstrap = 15_000,
        p_threshold = 0.01,
        charting=False
        ) -> Tuple[str, str]:
            """
            Efficient parallel verification using nested prefix property.
            """
            # Respect thread-scoped MCA param if enabled; otherwise fallback default
            MCA_NOISE = float(_K_ATTN_CV.get()) if _MCA_ENABLED.get() else 8.0
            if window_size is None:
                window_size = self.window_size

            model_device = next(self.model.parameters()).device
            self.logger.debug(f"Using device: {model_device}")
            if model_device == torch.device("cpu"):
                MCA_NOISE = 12.0
                batch_candidates = [2]
                self.logger.debug("Running on CPU, using MCA noise value 12.0 and batch size 2 only.")

            # Initialize covariance estimators
            logits_cov = RunningMeanCov(dim=1, device=model_device)
            means_cov = RunningMeanCov(dim=4, device=model_device)
            logits_cov_spda = RunningMeanCov(dim=1, device=model_device)
            means_cov_spda = RunningMeanCov(dim=4, device=model_device)

            # === MAIN VERIFICATION ===
            self.logger.debug("Computing all logits with efficient prefix method...")
            
            # Compute logits for all batch sizes (few forward passes total)
            all_logits = {}
            all_logits[1] = self._compute_all_logits_parallel_prefix(
                window_size, batch_size=1, flash=flash
            )
            for batch_size in batch_candidates:
                all_logits[batch_size] = self._compute_all_logits_parallel_prefix(
                    window_size, batch_size=batch_size, flash=flash, mca_noise_value=MCA_NOISE
                )
            
            # SPDA noise version
            all_logits_spda = self._compute_all_logits_parallel_prefix(
                window_size, batch_size=1, flash=flash, mca_noise_value=MCA_NOISE
            )
            all_logits['spda'] = all_logits_spda

            # Candidate evaluation order (baseline, candidates, SPDA)
            cand_order = [1] + batch_candidates + ['spda']

            self.logger.debug("Processing verification statistics...")

            # Results storage
            p_values = []
            ulp_fails = []
            rank_error_actual = []
            rank_error_calib = []
            all_sampling_noise = []
            all_delta_mu = []
            delta_mu_calib = []
            ref_sampling_noise = []
            delta_raw = []

            # (debug metrics removed)

            # Process steps for error calibration 
            for i in tqdm(range(window_size), desc="Estimating Error covariance"):
                # Update covariance estimators
                saved_idx = torch.as_tensor(self.expected_topk_indices[i], device=model_device)
                errors = self._estimate_logit_errors(
                    all_logits[1][i], saved_idx, all_logits[batch_candidates[-1]][i]
                )
                errors_spda = self._estimate_logit_errors(
                    all_logits[1][i], saved_idx, all_logits_spda[i]
                )
                for err in errors['delta_raw']:
                    logits_cov.update(err) 
                means_cov.update(errors['delta_mean'])
                for err in errors_spda['delta_raw']:
                    logits_cov_spda.update(err) 
                means_cov_spda.update(errors_spda['delta_mean'])                
                rank_error_calib.append(errors_spda['rank_errors'])
                delta_mu_calib.append(errors_spda['delta_mu'])

            # Covariance adjuster
            cov_adjuster = torch.sqrt(torch.cat([
                torch.maximum(logits_cov.covariance, logits_cov_spda.covariance).repeat(70, 1),
                torch.diag(torch.maximum(means_cov.covariance, means_cov_spda.covariance)).unsqueeze(1)
            ], dim=0)).squeeze().to(torch.float32)

            # cov_adjuster prepared
                        
            # Process each step
            for i in tqdm(range(window_size), desc="Processing steps"):                
                # Find best result across batch sizes
                best_result = None
                best_p_value = float("-inf")
                best_sampling_noise = float('inf')
                best_delta_mu = float('inf')

                for jj, batch_size in enumerate(cand_order):
                    # Extract logits for this step and batch size
                    step_logits = all_logits[batch_size][i]  # (batch_size, vocab_size)
                    
                    # Compute verification metrics
                    result = self._verify_step_from_logits(
                        i, step_logits, bootstrap=bootstrap, cov_adjuster=cov_adjuster
                    )
                    
                    # Track best sampling noise and delta_mu across batch sizes
                    if abs(result['sampling_noise']) < abs(best_sampling_noise):
                        best_sampling_noise = result['sampling_noise']
                    if abs(result['delta_mu']) < abs(best_delta_mu):
                        best_delta_mu = result['delta_mu']
                        
                    # Track best p-value
                    if result['p_value'] > best_p_value:
                        best_result = result
                        best_p_value = result['p_value']
                    if jj == 0: 
                        ref_sampling = copy.deepcopy(result['sampling_noise'])
                    if jj == 1: 
                        ref_sampling_noise.append(result['sampling_noise']-ref_sampling)

                # Early exit on grid failure
                if best_result.get('grid_fail', False):
                    return ("RED", "Grid snap failed, there was tampering with stated precision")
                
                # Store final results (using best values across batch sizes)
                p_values.append(best_result['p_value'])
                ulp_fails.append(best_result['ulp_fail'])
                rank_error_actual.append(best_result['R_obs'])
                all_sampling_noise.append(best_sampling_noise)
                all_delta_mu.append(best_delta_mu)
                delta_raw.append(best_result['delta_raw'].cpu())

            # Final validation (same as original)
            return self._validate_final_results(
                p_values, rank_error_actual, rank_error_calib,
                all_sampling_noise, all_delta_mu, delta_mu_calib,
                charting=charting,ref_sampling_noise=ref_sampling_noise,delta_raw=delta_raw
            )

    @pow_profiler
    def _verify_step_from_logits(
        self,
        step_idx: int,
        full_logits: torch.Tensor,  # (batch_size, vocab_size)
        bootstrap: int = 15_000,
        cov_adjuster: torch.Tensor = None,
        charting: bool = False
        ) -> Dict:
        """
        IDENTICAL statistics to _verify_step_multivariate_continous, but reuses
        precomputed full-vocab logits for the step.
        """
        # ---------- constants ---------------------------------------------------
        BUCKETS = ((0, 50), (50, 500), (500, 2000), (2000, None))
        model_device = next(self.model.parameters()).device
        tail1_p = float(os.getenv("POW_EQ_TAIL1_P", "0.01"))
        tail2_p = float(os.getenv("POW_EQ_TAIL2_P", "0.001"))

        # ---------- align dtypes/devices ----------------------------------------
        full_logits = full_logits.to(torch.float32)
        vocab_size = full_logits.size(-1)

        # ---------- persisted 70-logit snapshot (platform A) --------------------
        saved_idx = torch.as_tensor(self.expected_topk_indices[step_idx],
                                    device=model_device, dtype=torch.long)
        logits_A = torch.as_tensor(self.expected_topk_logits[step_idx],
                                device=model_device, dtype=torch.float32)

        # Sort A and reorder saved_idx to match (stable – same as sequential)
        logits_A, saved_idx, _perm = self._sort_tensor_pairs_v2(
            logits_A.clone(), saved_idx.clone()
        )

        # Use first batch row of the precomputed logits as "platform B"
        logits_B = full_logits[-1, saved_idx]  # (70,)

        # ---------- ULP calculations --------------------------------------------
        sorted_logits0, sort_idx_full = torch.sort(full_logits[0], descending=True)
        ulp_raw = _ulp(logits_A, self.dtype) * 2  # (70,)

        # ---------- rank calculation (same trick as sequential) ------------------
        inv_rank = torch.empty_like(sort_idx_full)
        inv_rank[sort_idx_full] = torch.arange(sort_idx_full.size(0), device=model_device)
        ranks = inv_rank[saved_idx[:50]]
        R_obs = ((ranks - torch.arange(50, device=model_device)).abs().sum()).item()

        # ---------- global mean offset ------------------------------------------
        mu_A = self.expected_lse[step_idx, -1].to(torch.float32).to(model_device)
        delta_mu = mu_A - full_logits[0].mean()

        # ---------- 70 raw deltas ------------------------------------------------
        delta_raw = (logits_A - logits_B) - delta_mu  # (70,)

        # ---------- four bucket means -------------------------------------------
        sorted_logits, _ = torch.sort(full_logits[0].unsqueeze(0), dim=-1, descending=True)  # (1,V)
        mean_B = torch.stack(_bucket_means(sorted_logits, BUCKETS, vocab_size), dim=-1).squeeze(0)  # (4,)
        mean_A = self.expected_lse[step_idx, 1:5].to(torch.float32).to(model_device)  # (4,)
        delta_mean = mean_A - mean_B - delta_mu  # (4,)

        # ========== P-VALUE COMPUTATION ==========================================
        disc, cont = 70, 4
        Σ_err = self._sigma_from_logits(sorted_logits0, logits_B, cov_adjuster)
        self.Σ_err = Σ_err
        invΣ = torch.linalg.inv(Σ_err)

        # Observed vector
        base_quant = _snap(logits_B, ulp_raw)    # (70,)
        D_obs = logits_A - base_quant            # (70,)
        C_obs = delta_mean                       # (4,)
        v_obs = torch.cat([D_obs, C_obs])        # (74,)
        T_obs = v_obs @ invΣ @ v_obs             # scalar

        # Correlated null samples
        L = torch.linalg.cholesky(Σ_err, upper=False)
        keyed = self._keyed_gauss(step_idx, bootstrap, "baseline", dim=74)
        if keyed is not None:
            samples = keyed[0] @ L.T
        else:
            samples = self._cached_gauss(74, bootstrap) @ L.T  # (B,74)

        # Discrete part for nulls
        x = logits_B - delta_mu + samples[:, :disc]     # (B,70)
        x_quant = _snap(x, ulp_raw)                     # quantize with per-dim ULP (B,70)
        D_null = base_quant - x_quant                   # (B,70)

        if charting:
            plt.hist(D_null.cpu().numpy().flatten(), bins=30, density=1)
            plt.hist(D_obs.cpu().numpy().flatten(), bins=30, density=1)
            plt.show()

        # Continuous part from samples
        C_null = samples[:, disc:disc+cont]             # (B,4)

        V_null = torch.cat([D_null, C_null], dim=1)     # (B,74)
        tmp = V_null @ invΣ                              # (B,74)
        T_null = (tmp * V_null).sum(dim=1)              # (B,)

        p_value = (T_null >= T_obs).to(torch.float32).mean().item()

        # Tail refinement (identical to sequential)
        if p_value < tail1_p:
            keyed = self._keyed_gauss(step_idx, bootstrap * 10, "tail1", dim=74)
            if keyed is not None:
                samples = keyed[0] @ L.T
            else:
                samples = self._cached_gauss(74, bootstrap * 10) @ L.T
            x = logits_B - delta_mu + samples[:, :disc]
            x_quant = _snap(x, ulp_raw)
            D_null = base_quant - x_quant
            C_null = samples[:, disc:disc+cont]
            V_null = torch.cat([D_null, C_null], dim=1)
            tmp = V_null @ invΣ
            T_null = (tmp * V_null).sum(dim=1)
            p_value = (T_null >= T_obs).to(torch.float32).mean().item()

            if p_value < tail2_p:
                L2 = torch.linalg.cholesky(Σ_err * 1.2, upper=False)
                keyed = self._keyed_gauss(step_idx, bootstrap * 2, "tail2", dim=74)
                if keyed is not None:
                    samples = keyed[0] @ L2.T
                else:
                    samples = self._cached_gauss(74, bootstrap * 2) @ L2.T
                x = logits_B - delta_mu + samples[:, :disc]
                x_quant = _snap(x, ulp_raw)
                D_null = base_quant - x_quant
                C_null = samples[:, disc:disc+cont]
                V_null = torch.cat([D_null, C_null], dim=1)
                tmp = V_null @ invΣ
                T_null = (tmp * V_null).sum(dim=1)
                p_value = (T_null >= T_obs).to(torch.float32).mean().item()

        # --- reproduce sequential diagnostics via identical sampling path ----
        ctx, _attn = self._rebuild_ctx_and_mask_for_step(step_idx)
        u = self._get_u(ctx, step_idx)
        result = self._sample(
            torch.arange(vocab_size, device=model_device),
            full_logits[0],            # same row used above
            ctx,
            u,
            expected_lse=None,
            query_token=self.chosen_tokens[step_idx].item()
        )
        sampling_noise = result['query_cdf'] - self.expected_probs[step_idx].item()

        return {
            'p_value': p_value,
            'T_obs': T_obs.item(),
            'delta_raw': logits_A - logits_B,
            'delta_mean': delta_mean,
            'grid_fail': not torch.allclose(_snap(logits_A, _ulp(logits_A, self.stated_dtype)*2), logits_A, atol=1e-6),
            'ulp_fail': torch.any(torch.abs((logits_A - logits_B) - delta_mu) >= 6 * torch.sqrt(torch.diag(Σ_err)).max()).item(),
            'sampling_noise': sampling_noise,
            'delta_mu': delta_mu,
            'R_obs': R_obs,
            'full_logits': full_logits,
        }

    @pow_profiler
    @torch.no_grad()
    def _verify_steps_from_logits_vectorized(
        self,
        steps_logits: torch.Tensor,         # (S,B,V) monolithic tensor, as from _compute_all_logits_parallel_prefix(..., as_tensor=True)
        step_indices: Optional[torch.Tensor] = None,  # (S,)
        bootstrap: int = 15_000,
        cov_adjuster: Optional[torch.Tensor] = None,  # (74,)
        charting: bool = False,
        step_block: int = 64,               # steps processed concurrently (controls memory)
        bootstrap_block: int = 4096,        # null samples processed concurrently (controls memory)
        tail_refine: bool = True,
        compute_sampling_noise: bool = True
        ) -> Dict[str, torch.Tensor]:
        """
        Vectorized equivalent of _verify_step_from_logits for all steps.
        Returns tensors with per-step results matching keys from the scalar version.
        """
        device = next(self.model.parameters()).device
        dtype  = torch.float32
        disc, cont = 70, 4
        BUCKETS = ((0, 50), (50, 500), (500, 2000), (2000, None))
        tail1_p = float(os.getenv("POW_EQ_TAIL1_P", "0.01"))
        tail2_p = float(os.getenv("POW_EQ_TAIL2_P", "0.001"))

        assert steps_logits.dim() == 3, "steps_logits must be (S,B,V)"
        S, B, V = steps_logits.shape
        assert B >= 1, "Need at least one row for full_logits[0]"
        # Match scalar path (_verify_step_from_logits), which promotes full_logits to fp32.
        # Only rows 0 and -1 of steps_logits are ever read below (row -1 -> the 70
        # saved-index logits; row 0 -> ranks, bucket means, and sampling diagnostics).
        # Promote ONLY those two rows to fp32 instead of copying the whole (S,B,V)
        # tensor. Mathematically identical (same fp32 values), ~B/2x less peak memory:
        # for bs=20 this is a (S,2,V) copy instead of (S,20,V) (~2.7 GiB saved).
        steps_row0 = steps_logits[:, 0,  :].to(torch.float32)   # (S,V) — ranks/mean/sampling
        steps_rowL = steps_logits[:, -1, :].to(torch.float32)   # (S,V) — saved-index logits
        # We follow your scalar code: B-1 (last row) is 'platform B' for the 70 saved indices.
        # And row 0 is used for ranking, mean, and sampling diagnostic paths.

        if step_indices is None:
            step_indices = torch.arange(S, device=device, dtype=torch.long)
        else:
            step_indices = step_indices.to(device=device, dtype=torch.long)
            assert step_indices.numel() == S

        # Expected artifacts (A‑platform snapshots)
        saved_idx_all = torch.as_tensor(self.expected_topk_indices, device=device, dtype=torch.long)[step_indices]   # (S,70)
        logits_A_all  = torch.as_tensor(self.expected_topk_logits,  device=device, dtype=dtype)[step_indices]        # (S,70)
        expected_lse  = torch.as_tensor(self.expected_lse,          device=device, dtype=dtype)[step_indices]        # (S,>=5)

        # Sort (logits_A, saved_idx) with the same semantics as _sort_tensor_pairs_v2:
        # 1) secondary key: token id ascending (stable)
        # 2) primary key: logit descending (stable)
        sec_order = torch.argsort(saved_idx_all, dim=1, stable=True)                          # (S,70)
        logits_sec = torch.gather(logits_A_all, 1, sec_order)                                 # (S,70)
        prim_order = torch.argsort(-logits_sec, dim=1, stable=True)                           # (S,70)
        sort_idx = torch.gather(sec_order, 1, prim_order)                                     # (S,70)
        logits_A_sorted = torch.gather(logits_A_all, 1, sort_idx)                             # (S,70)
        saved_idx_sorted = torch.gather(saved_idx_all, 1, sort_idx)                           # (S,70)

        # "Platform B" logits for these 70 indices come from the last row of steps_logits
        logits_B_70 = torch.gather(steps_rowL, 1, saved_idx_sorted)                           # (S,70)

        # First row sorted (used for ranks and bucket means)
        sorted_first0, sort_idx_full = torch.sort(steps_row0, dim=-1, descending=True)        # (S,V), (S,V)

        # Invert argsort to get ranks
        inv_rank = torch.empty_like(sort_idx_full)
        arange_V = torch.arange(V, device=device).unsqueeze(0).expand(S, V)
        inv_rank.scatter_(1, sort_idx_full, arange_V)
        ranks_top50 = torch.gather(inv_rank, 1, saved_idx_sorted[:, :50])                       # (S,50)
        R_obs = (ranks_top50 - torch.arange(50, device=device).view(1, -1)).abs().sum(dim=1)    # (S,)

        # Global mean offset
        mu_A     = expected_lse[:, -1]                                                          # (S,)
        delta_mu = mu_A - steps_row0.mean(dim=-1)                                               # (S,)

        # 70 raw deltas
        delta_raw = (logits_A_sorted - logits_B_70) - delta_mu.unsqueeze(-1)                    # (S,70)

        # 4 bucket means for row 0
        def bucket_mean(sorted_vals: torch.Tensor, lo: int, hi: Optional[int]) -> torch.Tensor:
            if hi is None or hi > V: hi = V
            lo = min(lo, V)
            if hi <= lo:
                return torch.zeros(sorted_vals.size(0), device=sorted_vals.device, dtype=sorted_vals.dtype)
            return sorted_vals[:, lo:hi].mean(dim=-1)

        mean_B = torch.stack([
            bucket_mean(sorted_first0, 0, 50),
            bucket_mean(sorted_first0, 50, 500),
            bucket_mean(sorted_first0, 500, 2000),
            bucket_mean(sorted_first0, 2000, None)
        ], dim=-1)                                                                              # (S,4)

        mean_A = expected_lse[:, 1:5]                                                           # (S,4)
        delta_mean = mean_A - mean_B - delta_mu.unsqueeze(-1)                                   # (S,4)

        # ULP and quantization
        ulp_raw   = _ulp(logits_A_sorted, self.dtype) * 2.0                                     # (S,70)
        base_quant= _snap(logits_B_70, ulp_raw)                                                 # (S,70)
        D_obs     = logits_A_sorted - base_quant                                                # (S,70)
        C_obs     = delta_mean                                                                   # (S,4)
        v_obs     = torch.cat([D_obs, C_obs], dim=1)                                            # (S,74)

        # grid_fail (same discrete grid check as scalar)
        grid_ref  = _snap(logits_A_sorted, _ulp(logits_A_sorted, self.stated_dtype) * 2.0)
        # Match scalar semantics: only explicit atol, default rtol behavior.
        grid_fail = ~torch.all(torch.isclose(grid_ref, logits_A_sorted, atol=1e-6), dim=1)  # (S,)

        # ---- Σ_err per step (batched) ---------------------------------------------------------
        # Try torch.vmap (PyTorch >= 2.x); fallback to small Python loop per block.
        try:
            from torch.func import vmap as _vmap
        except Exception:
            try:
                from functorch import vmap as _vmap
            except Exception:
                _vmap = None

        def _sigma_from_logits_batched(sorted0_blk: torch.Tensor,
                                    B70_blk: torch.Tensor) -> torch.Tensor:
            """
            Avoid vmap because _sigma_from_logits uses .item(). Tiny loop per step-block.
            """
            out = []
            for i in range(sorted0_blk.size(0)):
                Σ = self._sigma_from_logits(sorted0_blk[i], B70_blk[i], cov_adjuster)
                out.append(Σ.to(torch.float32))
            return torch.stack(out, dim=0)  # (sb,74,74)
        # ---- Outputs (S,...) ------------------------------------------------------------------
        T_obs          = torch.empty(S, device=device, dtype=dtype)
        p_value        = torch.zeros(S, device=device, dtype=dtype)
        ulp_fail       = torch.empty(S, device=device, dtype=torch.bool)
        sampling_noise = torch.zeros(S, device=device, dtype=dtype)

        # ---- Helper: accumulate p-values for a block of steps with optional Σ scaling ---------
        def _accumulate_block(
            idx_blk: torch.Tensor,
            Sigma_blk: torch.Tensor,
            draws: int,
            *,
            sample_scale: float = 1.0,
            pass_tag: str = "baseline",
        ):
            # Match scalar semantics: invSigma is always from the unscaled Sigma.
            invSigma = torch.linalg.inv(Sigma_blk)                  # (sb,74,74)
            Sigma_for_sampling = Sigma_blk if sample_scale == 1.0 else (Sigma_blk * sample_scale)
            L = torch.linalg.cholesky(Sigma_for_sampling, upper=False)

            v_obs_blk      = v_obs[idx_blk]                         # (sb,74)
            logits_B70_blk = logits_B_70[idx_blk]                   # (sb,70)
            delta_mu_blk   = delta_mu[idx_blk]                      # (sb,)
            ulp_raw_blk    = ulp_raw[idx_blk]                       # (sb,70)
            base_quant_blk = base_quant[idx_blk]                    # (sb,70)

            tmp = torch.einsum('s i, s i j -> s j', v_obs_blk, invSigma)
            T_obs_blk = (tmp * v_obs_blk).sum(dim=1)
            T_obs[idx_blk] = T_obs_blk

            exceed = torch.zeros(idx_blk.numel(), device=device, dtype=torch.long)
            total  = torch.zeros(idx_blk.numel(), device=device, dtype=torch.long)

            step_ids_blk = step_indices[idx_blk]
            keyed_all = self._keyed_gauss(step_ids_blk, draws, pass_tag, dim=74)
            if keyed_all is not None:
                # (sb, draws, 74): deterministic per-step/per-candidate/per-pass
                Z_all = keyed_all.to(device=device, dtype=torch.float32)
            else:
                # Use per-step independent draws (unlike shared Z across all steps).
                Z_all = self._cached_gauss(74, draws * idx_blk.numel()).to(
                    device=device, dtype=torch.float32
                ).view(idx_blk.numel(), draws, 74)

            for b0 in range(0, draws, bootstrap_block):
                b1 = min(b0 + bootstrap_block, draws)
                Z = Z_all[:, b0:b1, :]  # (sb,b,74)
                b_now = int(b1 - b0)
                # Match scalar path per-step: samples = Z @ L.T
                Y = torch.einsum('s b j, s i j -> s b i', Z, L)      # (sb,b,74)

                x       = logits_B70_blk.unsqueeze(1) - delta_mu_blk.unsqueeze(1).unsqueeze(-1) + Y[:, :, :disc]
                x_quant = _snap(x, ulp_raw_blk.unsqueeze(1))
                D_null  = base_quant_blk.unsqueeze(1) - x_quant
                C_null  = Y[:, :, disc:disc+cont]
                V_null  = torch.cat([D_null, C_null], dim=2)

                tmp     = torch.einsum('s b i, s i j -> s b j', V_null, invSigma)
                T_null  = (tmp * V_null).sum(dim=2)

                exceed += (T_null >= T_obs_blk.unsqueeze(1)).sum(dim=1)
                total  += b_now

            p_value[idx_blk] = exceed.to(torch.float32) / total.clamp_min(1).to(torch.float32)
            
        # ---- Main step pass in blocks ---------------------------------------------------------
        for s0 in range(0, S, step_block):
            s1      = min(s0 + step_block, S)
            idx_blk = torch.arange(s0, s1, device=device)

            Sigma_blk = _sigma_from_logits_batched(sorted_first0[idx_blk], logits_B_70[idx_blk])  # (sb,74,74)

            # ulp_fail criterion from scalar path
            diag_var     = torch.diagonal(Sigma_blk, dim1=-2, dim2=-1)                       # (sb,74)
            diag_std_max = torch.sqrt(diag_var).max(dim=1).values                             # (sb,)
            ulp_fail[idx_blk] = torch.any(
                torch.abs((logits_A_sorted[idx_blk] - logits_B_70[idx_blk]) - delta_mu[idx_blk].unsqueeze(-1))
                >= 6.0 * diag_std_max.unsqueeze(-1),
                dim=1
            )

            # Baseline accumulation
            _accumulate_block(idx_blk, Sigma_blk, draws=bootstrap, sample_scale=1.0, pass_tag="baseline")

        # ---- Tail refinement passes (same logic as scalar) ------------------------------------
        if tail_refine:
            mask1 = p_value < tail1_p
            if mask1.any():
                idx = torch.nonzero(mask1, as_tuple=False).squeeze(-1)
                for t0 in range(0, idx.numel(), step_block):
                    t1  = min(t0 + step_block, idx.numel())
                    blk = idx[t0:t1]
                    Sigma_blk = _sigma_from_logits_batched(sorted_first0[blk], logits_B_70[blk])
                    _accumulate_block(blk, Sigma_blk, draws=bootstrap * 10, sample_scale=1.0, pass_tag="tail1")

            mask2 = p_value < tail2_p
            if mask2.any():
                idx = torch.nonzero(mask2, as_tuple=False).squeeze(-1)
                for t0 in range(0, idx.numel(), step_block):
                    t1  = min(t0 + step_block, idx.numel())
                    blk = idx[t0:t1]
                    Sigma_blk = _sigma_from_logits_batched(sorted_first0[blk], logits_B_70[blk])
                    _accumulate_block(blk, Sigma_blk, draws=bootstrap * 2, sample_scale=1.2, pass_tag="tail2")

        # ---- Sampling noise (batched, using your helpers) -------------------------------------
        if compute_sampling_noise:
            # Build all contexts (list of tensors) once
            all_contexts = []
            for i in range(S):
                ctx = torch.cat([self.prompt_tokens, self.chosen_tokens[:step_indices[i]]]).to(device)
                all_contexts.append(ctx)

            u_batch = self._get_u_batch(all_contexts, step_indices)                            # (S,)
            expected_tokens = self.chosen_tokens[step_indices].to(device)                      # (S,)

            # Compute sampling CDF in step blocks over full vocab (idx = 0..V-1)
            ids_full = torch.arange(V, device=device)
            for s0 in range(0, S, step_block):
                s1 = min(s0 + step_block, S)
                idx_blk = slice(s0, s1)
                sb = s1 - s0

                idx_sent_batch = ids_full.unsqueeze(0).expand(sb, -1)                          # (sb, V)
                val_sent_batch = steps_row0[idx_blk, :]                                        # (sb, V)

                # _sample_batch requires a list of contexts; pass the sub-list
                pos, lower, upper, cdf, sorted_idx, valid_counts = self._sample_batch(
                    idx_sent_batch=idx_sent_batch,
                    val_sent_batch=val_sent_batch,
                    context_tokens_batch=all_contexts[s0:s1],
                    u_batch=u_batch[idx_blk],
                    expected_tokens=expected_tokens[idx_blk],
                    expected_lse=None
                )
                # Using your vectorized return: query_cdf is 'upper' per expected token
                # sampling_noise = result['query_cdf'] - expected_probs
                expected_probs_blk = torch.as_tensor(self.expected_probs, device=device, dtype=dtype)[step_indices[idx_blk]]
                sampling_noise[idx_blk] = (upper - expected_probs_blk)

        return {
            'p_value':        p_value,                # (S,)
            'T_obs':          T_obs,                  # (S,)
            'delta_raw':      (logits_A_sorted - logits_B_70),  # (S,70) (pre‑offset; matches scalar return)
            'delta_mean':     delta_mean,             # (S,4)
            'grid_fail':      grid_fail,              # (S,)
            'ulp_fail':       ulp_fail,               # (S,)
            'sampling_noise': sampling_noise,         # (S,)
            'delta_mu':       delta_mu,               # (S,)
            'R_obs':          R_obs.to(torch.int64),  # (S,)
        }

    @pow_profiler
    @torch.no_grad()
    def _estimate_cov_adjuster_vectorized(
        self,
        logits_base: torch.Tensor,    # (S, 1, V) — baseline (bs=1, no MCA)
        logits_ref: torch.Tensor,     # (S, Br, V) — reference (e.g., largest bs with MCA)
        logits_spda: torch.Tensor,    # (S, 1, V) — SPDA (bs=1 with MCA_NOISE)
        saved_idx_all: torch.Tensor,  # (S, 70)   — expected_topk_indices for each step
    ) -> Tuple[torch.Tensor, List[float], List[torch.Tensor]]:
        """
        Parity-first implementation of covariance calibration.
        Matches verify_full_sequence_adaptive_parallel_efficient step-for-step by
        reusing _estimate_logit_errors + RunningMeanCov updates in the same order.
        """
        device = next(self.model.parameters()).device
        S, B0, _ = logits_base.shape
        assert B0 >= 1 and logits_ref.size(0) == S and logits_spda.size(0) == S

        logits_cov = RunningMeanCov(dim=1, device=device)
        means_cov = RunningMeanCov(dim=4, device=device)
        logits_cov_spda = RunningMeanCov(dim=1, device=device)
        means_cov_spda = RunningMeanCov(dim=4, device=device)
        rank_error_calib: List[float] = []
        delta_mu_calib: List[torch.Tensor] = []

        for i in range(S):
            saved_idx = saved_idx_all[i]
            errors = self._estimate_logit_errors(logits_base[i], saved_idx, logits_ref[i])
            errors_spda = self._estimate_logit_errors(logits_base[i], saved_idx, logits_spda[i])

            for err in errors['delta_raw']:
                logits_cov.update(err)
            means_cov.update(errors['delta_mean'])

            for err in errors_spda['delta_raw']:
                logits_cov_spda.update(err)
            means_cov_spda.update(errors_spda['delta_mean'])

            rank_error_calib.append(errors_spda['rank_errors'])
            delta_mu_calib.append(errors_spda['delta_mu'])

        cov_adjuster = torch.sqrt(torch.cat([
            torch.maximum(logits_cov.covariance, logits_cov_spda.covariance).repeat(70, 1),
            torch.diag(torch.maximum(means_cov.covariance, means_cov_spda.covariance)).unsqueeze(1)
        ], dim=0)).squeeze().to(torch.float32)

        return cov_adjuster, rank_error_calib, delta_mu_calib


    @pow_profiler
    def verify_full_sequence_adaptive_parallel_efficient_v2(
        self,
        window_size: Optional[int] = None,
        batch_candidates: List[int] = [2, 5, 10, 20],
        debug: bool = False,
        flash = False,
        kv=False,
        bootstrap = 15_000,
        p_threshold = 0.01,
        charting=False,
        step_block: int = 64,             # propagate to vectorized core
        bootstrap_block: int = 4096,      # propagate to vectorized core
        ) -> Tuple[str, str]:

        # Respect thread-scoped MCA param if enabled; otherwise fallback default
        MCA_NOISE = float(_K_ATTN_CV.get()) if _MCA_ENABLED.get() else 8.0
        if window_size is None:
            window_size = self.window_size

        model_device = next(self.model.parameters()).device
        self.logger.debug(f"Using device: {model_device}")
        if model_device == torch.device("cpu"):
            MCA_NOISE = 12.0
            batch_candidates = [2]
            self.logger.debug("Running on CPU, using MCA noise value 12.0 and batch size 2 only.")

        # ---------------- Error covariance calibration (unchanged) -----------------
        logits_cov      = RunningMeanCov(dim=1, device=model_device)
        means_cov       = RunningMeanCov(dim=4, device=model_device)
        logits_cov_spda = RunningMeanCov(dim=1, device=model_device)
        means_cov_spda  = RunningMeanCov(dim=4, device=model_device)

        self.logger.debug("Computing all logits with efficient prefix method...")
        all_logits = {}

        # 1× baseline (no MCA)
        all_logits[1] = self._compute_all_logits_parallel_prefix(
            window_size, batch_size=1, flash=flash, as_tensor=True        # (S,1,V)
        )
        # candidate batch sizes (with MCA)
        for bs in batch_candidates:
            all_logits[bs] = self._compute_all_logits_parallel_prefix(
                window_size, batch_size=bs, flash=flash, mca_noise_value=MCA_NOISE, as_tensor=True  # (S,bs,V)
            )
        # SPDA noise (bs=1)
        all_logits['spda'] = self._compute_all_logits_parallel_prefix(
            window_size, batch_size=1, flash=flash, mca_noise_value=MCA_NOISE, as_tensor=True       # (S,1,V)
        )

        model_device = next(self.model.parameters()).device
        S = window_size
        step_indices = torch.arange(S, device=model_device)

        saved_idx_all = torch.as_tensor(self.expected_topk_indices,
                                        device=model_device, dtype=torch.long)[step_indices]

        # === Vectorized covariance calibration ===
        cov_adjuster, rank_error_calib, delta_mu_calib = self._estimate_cov_adjuster_vectorized(
            logits_base = all_logits[1],                       # (S,1,V)
            logits_ref  = all_logits[batch_candidates[-1]],    # (S,Br,V)
            logits_spda = all_logits['spda'],                  # (S,1,V)
            saved_idx_all = saved_idx_all
        )

        # === Vectorized heavy verification across steps (per candidate) ===
        results_by_cand = {}
        results_by_cand[1] = self._verify_steps_from_logits_vectorized(
            all_logits[1], step_indices, bootstrap, cov_adjuster,
            charting=charting, step_block=step_block, bootstrap_block=bootstrap_block, tail_refine=True
        )
        for bs in batch_candidates:
            results_by_cand[bs] = self._verify_steps_from_logits_vectorized(
                all_logits[bs], step_indices, bootstrap, cov_adjuster,
                charting=False, step_block=step_block, bootstrap_block=bootstrap_block, tail_refine=True
            )
        results_by_cand['spda'] = self._verify_steps_from_logits_vectorized(
            all_logits['spda'], step_indices, bootstrap, cov_adjuster,
            charting=False, step_block=step_block, bootstrap_block=bootstrap_block, tail_refine=True
        )

        # === Select per-step best-by-p and minima of |sampling_noise|, |delta_mu| ===
        cand_keys = [1] + batch_candidates + ['spda']
        stack = lambda k: torch.stack([results_by_cand[c][k] for c in cand_keys], dim=0)

        P     = stack('p_value')             # (C,S)
        ULP   = stack('ulp_fail')
        ROBS  = stack('R_obs')
        DMEAN = stack('delta_mean')          # (C,S,4)
        DRAW  = stack('delta_raw')           # (C,S,70)
        NOISE = stack('sampling_noise')      # (C,S)
        DMU   = stack('delta_mu')            # (C,S)
        GRID  = stack('grid_fail')

        best_c = torch.argmax(P, dim=0)      # (S,)
        # Candidate summary (debug removed)
        gather_cs = lambda T: T.gather(0, best_c.view(1, -1, *([1]*(T.dim()-2)))).squeeze(0)

        p_values        = gather_cs(P)                    # (S,)
        ulp_fails       = gather_cs(ULP)                  # (S,)
        rank_error_act  = gather_cs(ROBS)                 # (S,)
        delta_mean_best = gather_cs(DMEAN)                # (S,4)
        delta_raw_best  = gather_cs(DRAW)                 # (S,70)
        grid_fail_best  = gather_cs(GRID)                 # (S,)

        if grid_fail_best.any().item():
            return ("RED", "Grid snap failed, there was tampering with stated precision")

        # Min |sampling_noise| and |delta_mu|
        idx_min_noise = torch.argmin(NOISE.abs(), dim=0)  # (S,)
        idx_min_dmu   = torch.argmin(DMU.abs(),   dim=0)  # (S,)

        all_sampling_noise = NOISE.gather(0, idx_min_noise.unsqueeze(0)).squeeze(0)  # (S,)
        all_delta_mu       = DMU.gather(0, idx_min_dmu.unsqueeze(0)).squeeze(0)      # (S,)

        # Optional ref_sampling_noise: cand '2' versus baseline '1'
        ref_sampling_noise = None
        if 2 in results_by_cand:
            ref_sampling_noise = (results_by_cand[2]['sampling_noise'] - results_by_cand[1]['sampling_noise']).tolist()
        else:
            ref_sampling_noise = [0.0] * S

        # === Final validation — pass the types it expects ===
        return self._validate_final_results(
            p_values            = p_values.tolist(),               # List[float]
            rank_error_actual   = rank_error_act.tolist(),         # List[float/int]
            rank_error_calib    = rank_error_calib,                # List[float/int] (from vectorized calibration)
            all_sampling_noise  = all_sampling_noise.tolist(),     # List[float]
            all_delta_mu        = [all_delta_mu[i] for i in range(S)],  # List[torch.Tensor] (0‑dim each)
            delta_mu_calib      = delta_mu_calib,                  # List[torch.Tensor] (0‑dim each)
            charting=charting,
            ref_sampling_noise  = ref_sampling_noise,
            delta_raw           = [delta_raw_best[i].cpu() for i in range(S)],
            candidate_sampling_noise = NOISE.detach().cpu().tolist(),
            candidate_labels    = [str(c) for c in cand_keys],
        )

    @pow_profiler
    @torch.no_grad()
    def verify_full_sequence_adaptive_parallel_efficient_v2_streamed(
        self,
        window_size: Optional[int] = None,
        batch_candidates: List[int] = [2, 5, 10, 20],
        debug: bool = False,
        flash = False,
        kv=False,
        bootstrap = 15_000,
        p_threshold = 0.01,
        charting=False,
        step_block: int = 64,
        bootstrap_block: int = 4096,
        ) -> Tuple[str, str]:
        """
        Storage/lifetime-only refactor of verify_full_sequence_adaptive_parallel_efficient_v2.
        Mathematically identical — same forward ORDER [1, *candidates, spda] (so MCA-noise
        draw order is unchanged), same covariance inputs, same per-candidate verification,
        same selection and validation. The only differences are memory lifetime:
          - candidate logits are compacted to (S,2,V) at the source (only rows 0 and -1 are
            ever read: row 0 -> ranks/mean/sampling, row -1 -> saved-index logits; cov reads
            row 0). The full (all-B) forward is still computed transiently for MCA diversity.
          - each candidate's logits are freed immediately after its verification.
        The original v2 remains callable unchanged for A/B parity (POW_V2_STREAMED gate).
        """
        MCA_NOISE = float(_K_ATTN_CV.get()) if _MCA_ENABLED.get() else 8.0
        if window_size is None:
            window_size = self.window_size

        model_device = next(self.model.parameters()).device
        self.logger.debug(f"Using device: {model_device}")
        if model_device == torch.device("cpu"):
            MCA_NOISE = 12.0
            batch_candidates = [2]
            self.logger.debug("Running on CPU, using MCA noise value 12.0 and batch size 2 only.")

        self.logger.debug("Computing all logits with efficient prefix method (streamed/compact)...")
        all_logits = {}
        # SAME forward order as v2: baseline (bs=1, no MCA), candidates (MCA), spda.
        all_logits[1] = self._compute_all_logits_parallel_prefix(
            window_size, batch_size=1, flash=flash, as_tensor=True                 # (S,1,V)
        )
        for bs in batch_candidates:
            all_logits[bs] = self._compute_all_logits_parallel_prefix(
                window_size, batch_size=bs, flash=flash, mca_noise_value=MCA_NOISE,
                as_tensor=True, compact_rows=True                                  # (S,2,V)
            )
        all_logits['spda'] = self._compute_all_logits_parallel_prefix(
            window_size, batch_size=1, flash=flash, mca_noise_value=MCA_NOISE, as_tensor=True  # (S,1,V)
        )

        S = window_size
        step_indices = torch.arange(S, device=model_device)
        saved_idx_all = torch.as_tensor(self.expected_topk_indices,
                                        device=model_device, dtype=torch.long)[step_indices]

        # Covariance: base row0, ref=largest candidate row0 (of its compacted (S,2,V)), spda row0.
        cov_adjuster, rank_error_calib, delta_mu_calib = self._estimate_cov_adjuster_vectorized(
            logits_base = all_logits[1],
            logits_ref  = all_logits[batch_candidates[-1]],
            logits_spda = all_logits['spda'],
            saved_idx_all = saved_idx_all
        )

        # Per-candidate verification in the SAME order; free each candidate's logits after use.
        results_by_cand = {}
        results_by_cand[1] = self._verify_steps_from_logits_vectorized(
            all_logits[1], step_indices, bootstrap, cov_adjuster,
            charting=charting, step_block=step_block, bootstrap_block=bootstrap_block, tail_refine=True
        )
        for bs in batch_candidates:
            results_by_cand[bs] = self._verify_steps_from_logits_vectorized(
                all_logits[bs], step_indices, bootstrap, cov_adjuster,
                charting=False, step_block=step_block, bootstrap_block=bootstrap_block, tail_refine=True
            )
            all_logits[bs] = None                       # free compacted (S,2,V) right after verify
        results_by_cand['spda'] = self._verify_steps_from_logits_vectorized(
            all_logits['spda'], step_indices, bootstrap, cov_adjuster,
            charting=False, step_block=step_block, bootstrap_block=bootstrap_block, tail_refine=True
        )
        all_logits = None

        # ===== Selection + validation: byte-identical to v2 =====
        cand_keys = [1] + batch_candidates + ['spda']
        stack = lambda k: torch.stack([results_by_cand[c][k] for c in cand_keys], dim=0)

        P     = stack('p_value')
        ULP   = stack('ulp_fail')
        ROBS  = stack('R_obs')
        DMEAN = stack('delta_mean')
        DRAW  = stack('delta_raw')
        NOISE = stack('sampling_noise')
        DMU   = stack('delta_mu')
        GRID  = stack('grid_fail')

        best_c = torch.argmax(P, dim=0)
        gather_cs = lambda T: T.gather(0, best_c.view(1, -1, *([1]*(T.dim()-2)))).squeeze(0)

        p_values        = gather_cs(P)
        ulp_fails       = gather_cs(ULP)
        rank_error_act  = gather_cs(ROBS)
        delta_mean_best = gather_cs(DMEAN)
        delta_raw_best  = gather_cs(DRAW)
        grid_fail_best  = gather_cs(GRID)

        if grid_fail_best.any().item():
            return ("RED", "Grid snap failed, there was tampering with stated precision")

        idx_min_noise = torch.argmin(NOISE.abs(), dim=0)
        idx_min_dmu   = torch.argmin(DMU.abs(),   dim=0)
        all_sampling_noise = NOISE.gather(0, idx_min_noise.unsqueeze(0)).squeeze(0)
        all_delta_mu       = DMU.gather(0, idx_min_dmu.unsqueeze(0)).squeeze(0)

        ref_sampling_noise = None
        if 2 in results_by_cand:
            ref_sampling_noise = (results_by_cand[2]['sampling_noise'] - results_by_cand[1]['sampling_noise']).tolist()
        else:
            ref_sampling_noise = [0.0] * S

        return self._validate_final_results(
            p_values            = p_values.tolist(),
            rank_error_actual   = rank_error_act.tolist(),
            rank_error_calib    = rank_error_calib,
            all_sampling_noise  = all_sampling_noise.tolist(),
            all_delta_mu        = [all_delta_mu[i] for i in range(S)],
            delta_mu_calib      = delta_mu_calib,
            charting=charting,
            ref_sampling_noise  = ref_sampling_noise,
            delta_raw           = [delta_raw_best[i].cpu() for i in range(S)],
            candidate_sampling_noise = NOISE.detach().cpu().tolist(),
            candidate_labels    = [str(c) for c in cand_keys],
        )

    def _rebuild_ctx_and_mask_for_step(self, step_idx: int):
        model_device = next(self.model.parameters()).device
        ctx = torch.cat([self.prompt_tokens,
                        self.chosen_tokens[:step_idx]]).to(model_device)
        attn_mask = torch.cat([
            (~self.pad_mask).long(),
            torch.ones_like(self.chosen_tokens[:step_idx], dtype=torch.long)
        ]).to(model_device).contiguous()
        return ctx, attn_mask

    # ----------------------------------------------------------------- #
    #                        Exposed Interfaces
    # ----------------------------------------------------------------- #
    def quick_verify(self, proof, target_override_hex: Optional[str] = None):
        """Slice 11: ``target_override_hex`` is forwarded into
        ``_verify_block_sanity`` so a share-mode request can be
        accepted under an easier threshold. All non-PoW checks
        (params, sequence) run identically regardless of override."""
        try:
            d = pfunpack.unpack_validation_request(proof)['request']['pow_blob']
            self.initialise(d)

            with self.logger.verification_context(
                hash_id=d.get('hash'),
                model_identifier=d.get('model_identifier'),
                step="quick",
                verification_type="quick"
            ):
                if self._verify_block_sanity(target_override_hex=target_override_hex):
                    if self._verify_parameters():
                        if self.verify_sequence_light_vectorized():
                            self.logger.info("Quick verification passed")
                            return ResponseValue.ResponseValue.Quick_OK
                        else:
                            self.logger.error(
                                "Quick verification failed at sequence verification",
                                failure_type="quick_verification_sequence_failure"
                            )
                            return ResponseValue.ResponseValue.Quick_Fail
                    else:
                        self.logger.error(
                            "Quick verification failed at parameter validation",
                            failure_type="quick_verification_parameter_failure"
                        )
                        return ResponseValue.ResponseValue.Quick_Fail
                else:
                    self.logger.error(
                        "Quick verification failed at block sanity check",
                        failure_type="quick_verification_sanity_failure"
                    )
                    return ResponseValue.ResponseValue.Quick_Fail
        except Exception as e:
            self.logger.error(
                f"Quick verification failed with exception: {e}",
                failure_type="quick_verification_exception"
            )
            raise

    def logits_verify(self, proof):
        """Audit (logits-only) verification: sequence + logits replay
        against the claimed model, with NO block sanity and the AUDIT
        parameter check instead of the mining envelope — audit proofs
        from near-greedy inference must not be rejected for low entropy
        (the realized reuse gate belongs to mining only)."""
        try:
            d = pfunpack.unpack_validation_request(proof)['request']['pow_blob']
            # Normalize optional block fields for non-blockchain requests
            defaults = {
                "tick": 0,
                "target": "",
                "vdf": "",
                "hash": "",
                "block_hash": "",
                "header_prefix": "",
            }
            for key, val in defaults.items():
                if key not in d or d.get(key) is None:
                    d[key] = val

            self.initialise(d)

            with self.logger.verification_context(
                hash_id=d.get('hash'),
                verification_type="logits"
            ):
                if self._verify_parameters_audit():
                    if self.verify_sequence_light_vectorized():
                        self.logger.info("Logits verification passed")
                        return ResponseValue.ResponseValue.Logits_OK
                    else:
                        self.logger.error(
                            "Logits verification failed at sequence verification",
                            failure_type="logits_verification_sequence_failure"
                        )
                        return ResponseValue.ResponseValue.Logits_Fail
                else:
                    self.logger.error(
                        "Logits verification failed at parameter validation",
                        failure_type="logits_verification_parameter_failure"
                    )
                    return ResponseValue.ResponseValue.Logits_Fail
        except Exception as e:
            self.logger.error(
                f"Logits verification failed with exception: {e}",
                failure_type="logits_verification_exception"
            )
            raise

    def quick_verify_smell_test(self, proof, target_override_hex: Optional[str] = None):
        """Slice 11: same override contract as ``quick_verify`` —
        the override relaxes ONLY the final PoW threshold; smell
        test and sequence checks are unchanged."""
        try:
            d = pfunpack.unpack_validation_request(proof)['request']['pow_blob']
            self.initialise(d)

            with self.logger.verification_context(
                hash_id=d.get('hash'),
                model_identifier=d.get('model_identifier'),
                step="quick_smell",
                verification_type="quick_smell_test"
            ):
                if self._verify_block_sanity(target_override_hex=target_override_hex):
                    if self._verify_parameters():
                        if self.verify_sequence_light_vectorized():
                            if self.verify_sequence_smell_test():
                                self.logger.info("Quick verification passed")
                                return ResponseValue.ResponseValue.Quick_OK_Smell_OK
                            else:
                                self.logger.error(
                                    "Quick verification failed at smell test",
                                    failure_type="quick_verification_smell_test_failure"
                                )
                                return ResponseValue.ResponseValue.Quick_OK_Smell_Fail                              
                        else:
                            self.logger.error(
                                "Quick verification failed at sequence verification",
                                failure_type="quick_verification_sequence_failure"
                            )
                            return ResponseValue.ResponseValue.Quick_Fail_Smell_Fail
                    else:
                        self.logger.error(
                            "Quick verification failed at parameter validation",
                            failure_type="quick_verification_parameter_failure"
                        )
                        return ResponseValue.ResponseValue.Quick_Fail_Smell_Fail
                else:
                    self.logger.error(
                        "Quick verification failed at block sanity check",
                        failure_type="quick_verification_sanity_failure"
                    )
                    return ResponseValue.ResponseValue.Quick_Fail_Smell_Fail
        except Exception as e:
            self.logger.error(
                f"Quick verification failed with exception: {e}",
                failure_type="quick_verification_exception"
            )
            raise

    def full_verify(self, proof):
        try:
            d = pfunpack.unpack_validation_request(proof)['request']['pow_blob']
            proof_identifier = (d.get("model_identifier") or "").strip()
            if not proof_identifier:
                self.logger.error(
                    "Full verification failed: missing model_identifier in proof",
                    failure_type="full_verification_missing_model_identifier"
                )
                return "RED"

            # Optional safety switch for debugging/repro: force fresh load each full verify.
            # Disabled by default to avoid heavy reloads.
            force_reload_each_full = os.getenv(
                "POW_FORCE_MODEL_RELOAD_EACH_FULL", "false"
            ).strip().lower() in {"1", "true", "yes"}

            if self.initialised:
                self.reload(d, force_model_reload=force_reload_each_full)
            else:
                self.initialise(d)
                self.reload(d, force_model_reload=force_reload_each_full)

            bound_identifier = getattr(self, "_current_model_identifier", None)
            runtime_identifier = f"{self.model_name}@{self.commit_hash}"
            if bound_identifier != proof_identifier or runtime_identifier != proof_identifier:
                self.logger.warning(
                    "Detected model binding mismatch before full verification; forcing resync "
                    f"(proof={proof_identifier}, bound={bound_identifier}, runtime={runtime_identifier})"
                )
                # Self-heal: force model reload from proof identifier and continue.
                self.reload(d, force_model_reload=True)
                bound_identifier = getattr(self, "_current_model_identifier", None)
                runtime_identifier = f"{self.model_name}@{self.commit_hash}"
                if bound_identifier != proof_identifier or runtime_identifier != proof_identifier:
                    self.logger.error(
                        "Full verification failed: model binding mismatch after forced resync "
                        f"(proof={proof_identifier}, bound={bound_identifier}, runtime={runtime_identifier})",
                        failure_type="full_verification_model_binding_mismatch"
                    )
                    return "RED"

            with self.logger.verification_context(
                hash_id=d.get('hash'),
                model_identifier=d.get('model_identifier'),
                step="full",
                verification_type="full"
            ):
                verifier_version = os.getenv("POW_FULL_VERIFIER_VERSION", "v2").strip().lower()
                with mca_active(
                    k_lin=float(_K_LIN_CV.get()),
                    k_attn=float(_K_ATTN_CV.get()),
                    target_dtype=self.stated_dtype,
                ):
                    if verifier_version in {"v1", "1", "legacy"}:
                        self.logger.info("Full verification using v1 path")
                        status, message = self.verify_full_sequence_adaptive_parallel_efficient(
                            bootstrap=15_000,
                            charting=False,
                        )
                    else:
                        if verifier_version not in {"v2", "2", "batched"}:
                            self.logger.warning(
                                f"Unknown POW_FULL_VERIFIER_VERSION='{verifier_version}', defaulting to v2"
                            )
                        if _env_flag("POW_V2_STREAMED", False):
                            self.logger.info("Full verification using v2 path (streamed/compact)")
                            status, message = self.verify_full_sequence_adaptive_parallel_efficient_v2_streamed(
                                bootstrap=15_000,
                                charting=False,
                            )
                        else:
                            self.logger.info("Full verification using v2 path")
                            status, message = self.verify_full_sequence_adaptive_parallel_efficient_v2(
                                bootstrap=15_000,
                                charting=False,
                            )
                
                if status == "GREEN":
                    self.logger.info(f"Full verification passed: {message}")
                elif status == "AMBER":
                    self.logger.warning(f"Full verification amber: {message}")
                else:
                    self.logger.error(
                        f"Full verification failed: {message}",
                        failure_type="full_verification_failure"
                    )
                
                return status
                
        except Exception as e:
            self.logger.error(
                f"Full verification failed with exception: {e}",
                failure_type="full_verification_exception"
            )
            raise
