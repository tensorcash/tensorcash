# SPDX-License-Identifier: Apache-2.0
# --------------------------------------------------------------------------- #
#                          Imports
# --------------------------------------------------------------------------- #

#------ general utils
from __future__ import annotations
import os
import time
import uuid
import json
import math
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
from collections import defaultdict
import sys
import struct
from pathlib import Path
from einops import rearrange
from dataclasses import dataclass, field
import re
HEX_PATTERN = re.compile(r'^[0-9A-Fa-f]+$')

#------ specific utils
import flatbuffers
from tqdm.auto import tqdm
import matplotlib.pyplot as plt

#------ torch
import torch
from huggingface_hub import list_repo_files, hf_hub_download
from torch.distributions.normal import Normal   
from torch.distributions import StudentT

#------ numpy / sklearn / scipy
import numpy as np
import scipy.stats as st
from scipy import stats
import scipy.stats as stats
from scipy.stats import chi2
from sklearn.covariance import ledoit_wolf
from sklearn.decomposition import PCA

#------ modules
from proof import Proof
from proof import FloatArray
from proof import UIntArray
from proof import MiningResponse
from config.constants import *
import chiavdf
import config.constants as constants


# --------------------------------------------------------------------------- #
#                               Helper Functions                              #
# --------------------------------------------------------------------------- #

def validate_by_quantiles(arr, quantile_thresholds, two_sided = True):
    """
    Validate that |arr| sits within given thresholds at specified quantiles.

    Parameters
    ----------
    arr : array-like
        1D array of samples (μ‘s or noise‘s).
    quantile_thresholds : sequence of (q, t) pairs
        For each (q, t), require np.quantile(|arr|, q) ≤ t.

    Returns
    -------
    bool
        True if all quantile checks pass.
    """
    if two_sided:
        a = np.abs(arr)
    else:
        a = arr
    for q, t in quantile_thresholds:
        if np.quantile(a, q) > t:
            return False
    return True

def validate_by_quantiles_higher(arr, quantile_thresholds, two_sided = False):
    """
    """
    if two_sided:
        a = np.abs(arr)
    else:
        a = arr
    for q, t in quantile_thresholds:
        if (a>q).sum()>t*a.shape[0]:
            return False

    return True

def validate_by_quantiles_lower(arr, quantile_thresholds, two_sided = False):
    """
    """
    if two_sided:
        a = np.abs(arr)
    else:
        a = arr
    for q, t in quantile_thresholds:
        if (a<q).sum()>t*a.shape[0]:
            return False
    return True

def proof_to_dict(pf: Proof.Proof) -> dict:
    """
    Convert a flatbuffers Proof object into a Python dict
    compatible with your original JSON layout (hex strings, lists, etc.).
    """
    # Scalars
    out = {
        "tick":       pf.Tick(),
        "timestamp":  pf.Timestamp(),
        "is_solution": bool(pf.IsSolution()),
    }

    # Byte‐vectors → hex strings
    def bytes_to_hex(get_len, get_byte):
        return bytes(get_byte(i) for i in range(get_len())).hex()

    out["version"]        = pf.Version()

    out["target"] = bytes_to_hex(pf.TargetLength, pf.Target)
    out["vdf"]    = bytes_to_hex(pf.VdfLength,    pf.Vdf)
    out["hash"]   = bytes_to_hex(pf.HashLength,   pf.Hash)
    out["block_hash"]   = bytes_to_hex(pf.BlockHashLength,   pf.BlockHash)
    out["model_identifier"]   = pf.ModelIdentifier().decode('utf-8') if pf.ModelIdentifier() else ""
    out["compute_precision"]  = pf.ComputePrecision().decode('utf-8') if pf.ComputePrecision() else ""
    out["ipfs_cid"]           = pf.IpfsCid().decode('utf-8') if pf.IpfsCid() else ""
    out["extra_flags"]        = pf.ExtraFlags().decode('utf-8') if pf.ExtraFlags() else ""
    out["temperature"]        = pf.Temperature()
    out["top_p"]              = pf.TopP()
    out["top_k"]              = pf.TopK()
    out["repetition_penalty"] = pf.RepetitionPenalty()
    
    out["header_prefix"] = bytes_to_hex(pf.HeaderPrefixLength, pf.HeaderPrefix)

    # 1D vectors
    out["chosen_tokens"]      = [pf.ChosenTokens(i)    for i in range(pf.ChosenTokensLength())]
    out["chosen_probs"]       = [pf.ChosenProbs(i)     for i in range(pf.ChosenProbsLength())]
    out["sampling_u"]         = [pf.SamplingU(i)       for i in range(pf.SamplingULength())]
    out["softmax_normalizers"]= [pf.SoftmaxNormalizers(i) for i in range(pf.SoftmaxNormalizersLength())]
    out["prompt_tokens"]      = [pf.PromptTokens(i)    for i in range(pf.PromptTokensLength())]
    out["pad_mask"]           = [pf.PadMask(i)         for i in range(pf.PadMaskLength())]

    # 2D tensors via wrapper tables
    def unwrap_2d(vec_len, getter):
        outer = []
        for i in range(vec_len()):
            tbl = getter(i)
            inner = [tbl.Values(j) for j in range(tbl.ValuesLength())]
            outer.append(inner)
        return outer

    out["topk_logits"]  = unwrap_2d(pf.TopkLogitsLength,  pf.TopkLogits)
    out["topk_indices"] = unwrap_2d(pf.TopkIndicesLength, pf.TopkIndices)
    out["logsumexp_stats"] = unwrap_2d(pf.LogsumexpStatsLength, pf.LogsumexpStats)

    return out

def mining_response_to_dict(mr: MiningResponse.MiningResponse) -> dict:
    """
    Convert a flatbuffers MiningResponse object into a Python dict,
    including the completion_id field.
    """
    out = {
        "req_id": mr.ReqId(),
        "nonce": mr.Nonce(),
        "adjusted_bits": mr.AdjustedBits(),
        "difficulty": mr.Difficulty(),
    }
    
    # Extract completion_id if present
    completion_id = mr.CompletionId()
    if completion_id:
        out["completion_id"] = completion_id.decode('utf-8') if isinstance(completion_id, bytes) else completion_id
    else:
        out["completion_id"] = ""
    
    # Extract pow_blob_hash as hex string
    def bytes_to_hex(get_len, get_byte):
        return bytes(get_byte(i) for i in range(get_len())).hex()
    
    out["pow_blob_hash"] = bytes_to_hex(mr.PowBlobHashLength, mr.PowBlobHash)
    
    # Extract nested Proof object if present
    if mr.PowBlob() is not None:
        out["pow_blob"] = proof_to_dict(mr.PowBlob())
    else:
        out["pow_blob"] = None
    
    return out

def _snap(x: torch.Tensor, ulp: torch.Tensor) -> torch.Tensor:
    """Round to nearest multiple of `ulp` (broadcast-safe)."""
    return ulp * torch.round(x / ulp)

def _ulp(x_fp32: torch.Tensor, dtype) -> torch.Tensor:
    if dtype is torch.float16:      mant = 10          # fp16: 10 mantissa bits
    elif dtype is torch.bfloat16:   mant = 7           # bf16: 7  mantissa bits
    else:                           return torch.ones_like(x_fp32)
    exp = torch.floor(torch.log2(torch.abs(x_fp32))).clamp(-126, 127)
    return torch.pow(2.0, exp - mant - 1)              # **half**-ULP

def _sigma_from_ulp(ulp: torch.Tensor, *, num_ops: int = 150) -> torch.Tensor:
    return ulp * (num_ops ** 0.5) / (6.0 ** 0.5)       # √k·ulp / √6

def _student_t_cdf(x: torch.Tensor, df: int | float):
    """
    CPU+vectorised exact CDF via SciPy, then `.to(device)` back.
    """
    return torch.from_numpy(
        t.cdf(x.detach().cpu().numpy(), df)
    ).to(x.device, x.dtype)

def _logp_quantised_gauss(delta: torch.Tensor,
                          ulp: torch.Tensor,
                          sigma: torch.Tensor) -> torch.Tensor:
    """log P{ snapped-Gaussian = δ } (broadcast-safe)."""
    z_hi = (delta + 0.5 * ulp) / sigma
    z_lo = (delta - 0.5 * ulp) / sigma
    c_hi = _NORMAL.cdf(z_hi)
    c_lo = _NORMAL.cdf(z_lo)
    return torch.log(torch.clamp(c_hi - c_lo, min=1e-38))

def _logp_quantised_studentt(delta: torch.Tensor,
                             ulp:    torch.Tensor,
                             sigma:  torch.Tensor,
                             df:     float | torch.Tensor = 4):
    """
    log P{ snapped-StudentT = δ }   (broadcast-safe, no .cdf() needed)
    """
    z_hi = (delta + 0.5 * ulp) / sigma          # upper edge of bin
    z_lo = (delta - 0.5 * ulp) / sigma          # lower edge

    c_hi = _student_t_cdf(z_hi, df)
    c_lo = _student_t_cdf(z_lo, df)

    return torch.log(torch.clamp(c_hi - c_lo, min=1e-38))

def _bucket_means(sorted_logits: torch.Tensor,
                  buckets, vocab_size: int) -> list[torch.Tensor]:
    means = []
    for lo, hi in buckets:
        hi = hi or vocab_size
        means.append(sorted_logits[..., lo:hi].mean(dim=-1))
    return means

# Alternative version with additional validation:
def chiavdf_verify(block_hash: str, vdf: str, tick: int) -> bool:
    """
    Enhanced VDF verification with additional input validation.
    """
    # Input validation
    if not isinstance(block_hash, str) or not isinstance(vdf, str):
        print("Error: block_hash and vdf must be strings")
        return False
    
    # # Debug input parameters
    # print(f"chiavdf_verify_robust called:")
    # print(f"  block_hash: {block_hash} (length: {len(block_hash)})")
    # print(f"  vdf: {vdf} (length: {len(vdf)})")
    # print(f"  tick: {int(tick)}")
    
    # Convert hex strings to bytes with validation
    try:
        # Clean and validate hex strings
        block_hash_clean = block_hash.strip().replace('0x', '')
        vdf_clean = vdf.strip().replace('0x', '')
        
        # Validate hex characters
        if not all(c in '0123456789abcdefABCDEF' for c in block_hash_clean):
            raise ValueError("Invalid hex characters in block_hash")
        
        if not all(c in '0123456789abcdefABCDEF' for c in vdf_clean):
            raise ValueError("Invalid hex characters in vdf")
        
        # Convert to bytes
        block_hash_bytes = bytes.fromhex(block_hash_clean)
        vdf_bytes = bytes.fromhex(vdf_clean)
        
        # Validate expected lengths (adjust as needed)
        if len(block_hash_bytes) != 32:
            print(f"Warning: block_hash has unexpected length {len(block_hash_bytes)}, expected 32 bytes")
        
        # print(f"Conversion successful:")
        # print(f"  block_hash_bytes: {len(block_hash_bytes)} bytes")
        # print(f"  vdf_bytes: {len(vdf_bytes)} bytes")
        
    except ValueError as e:
        print(f"Error converting hex strings to bytes: {e}")
        return False
    
    try:
        result = chiavdf.verify_from_hash(
            block_hash_bytes,
            vdf_bytes,
            constants.DISCRIMINANT_SIZE,
            tick,
            0
        )
        
        # print(f"VDF verification result: {result}")
        return result
    except Exception as e:
        print(f"Error during VDF verification: {e}")
        return False

def parse_safetensors_header(path: Path) -> Optional[torch.dtype]:
    """Parse safetensors header to extract dtype without loading tensor data."""
    try:
        with path.open('rb') as f:
            header_len = struct.unpack('<Q', f.read(8))[0]
            header = json.loads(f.read(header_len))
        
        # Skip any "__metadata__" key, find first tensor
        for k, info in header.items():
            if k == "__metadata__":
                continue
            code = info.get('dtype')
            return SF_DTYPES.get(code, None)
        return None
    except Exception as e:
        print(f"Error parsing safetensors header: {e}")
        return None

def inspect_bin_dtype(path: Path) -> Optional[torch.dtype]:
    """Inspect PyTorch .bin file dtype using memory mapping."""
    try:
        # Use mmap to avoid loading full checkpoint into memory
        checkpoint = torch.load(str(path), map_location='cpu', weights_only=True)
        if not checkpoint:
            return None
        first_tensor = next(iter(checkpoint.values()))
        return first_tensor.dtype
    except Exception as e:
        print(f"Error inspecting .bin file: {e}")
        return None

def get_native_dtype_from_commit(
    repo_id: str, 
    revision: Optional[str] = None,
    filename_hint: Optional[str] = None
    ) -> Optional[torch.dtype]:
    """
    Get native dtype from HF model repo at specific commit.
    
    Args:
        repo_id: HuggingFace repo identifier (e.g., "microsoft/DialoGPT-medium")
        revision: Git commit hash, branch name, or tag (e.g., "main", "v1.0", commit SHA)
        filename_hint: Specific checkpoint filename to inspect (optional)
    
    Returns:
        torch.dtype of the saved weights, or None if unable to determine
    """
    try:
        def _resolve_hf_cache_repo_dir(repo_id: str) -> Optional[Path]:
            cache_root = (
                os.environ.get("HF_HUB_CACHE")
                or os.environ.get("HUGGINGFACE_HUB_CACHE")
            )
            if not cache_root:
                hf_home = os.environ.get("HF_HOME")
                if hf_home:
                    cache_root = os.path.join(hf_home, "hub")
                else:
                    cache_root = os.path.join(
                        os.path.expanduser("~/.cache/huggingface"), "hub"
                    )
            repo_dir = Path(cache_root) / f"models--{repo_id.replace('/', '--')}"
            return repo_dir if repo_dir.is_dir() else None

        def _resolve_snapshot_dir(repo_dir: Path, revision: Optional[str]) -> Optional[Path]:
            def _read_ref(ref_name: str) -> Optional[str]:
                ref_path = repo_dir / "refs" / ref_name
                if ref_path.is_file():
                    return ref_path.read_text().strip()
                return None

            commit = None
            if revision:
                if (repo_dir / "snapshots" / revision).is_dir():
                    commit = revision
                else:
                    commit = _read_ref(revision)
            else:
                commit = _read_ref("main") or _read_ref("master")

            if not commit:
                return None
            snapshot_dir = repo_dir / "snapshots" / commit
            return snapshot_dir if snapshot_dir.is_dir() else None

        # Try local cache first to avoid network calls
        repo_dir = _resolve_hf_cache_repo_dir(repo_id)
        if repo_dir:
            snapshot_dir = _resolve_snapshot_dir(repo_dir, revision)
            if snapshot_dir:
                files = [p.name for p in snapshot_dir.iterdir() if p.is_file()]
                safetensor_files = sorted([f for f in files if f.endswith(".safetensors")])
                bin_files = sorted([f for f in files if f.endswith(".bin")])

                target_file = None
                parser_func = None

                if filename_hint:
                    if (snapshot_dir / filename_hint).is_file():
                        target_file = filename_hint
                        parser_func = (
                            parse_safetensors_header
                            if filename_hint.endswith(".safetensors")
                            else inspect_bin_dtype
                        )
                    else:
                        print(f"Warning: {filename_hint} not found in local cache")

                if not target_file:
                    if safetensor_files:
                        target_file = safetensor_files[0]
                        parser_func = parse_safetensors_header
                        print(f"Using cached safetensors file: {target_file}")
                    elif bin_files:
                        target_file = bin_files[0]
                        parser_func = inspect_bin_dtype
                        print(f"Using cached .bin file: {target_file}")

                if target_file and parser_func:
                    local_path = snapshot_dir / target_file
                    return parser_func(local_path)

        # List files at specific revision
        files = list_repo_files(repo_id, revision=revision)
        print(f"Found {len(files)} files in {repo_id} @ {revision or 'main'}")
        
        # Prioritize safetensors files (more efficient)
        safetensor_files = sorted([f for f in files if f.endswith('.safetensors')])
        bin_files = sorted([f for f in files if f.endswith('.bin')])
        
        # Choose file to inspect
        target_file = None
        parser_func = None
        
        if filename_hint:
            # User specified exact file
            if filename_hint in files:
                target_file = filename_hint
                parser_func = parse_safetensors_header if filename_hint.endswith('.safetensors') else inspect_bin_dtype
            else:
                print(f"Warning: {filename_hint} not found in repo")
                
        if not target_file:
            # Auto-select: prefer safetensors
            if safetensor_files:
                target_file = safetensor_files[0]
                parser_func = parse_safetensors_header
                print(f"Using safetensors file: {target_file}")
            elif bin_files:
                target_file = bin_files[0] 
                parser_func = inspect_bin_dtype
                print(f"Using .bin file: {target_file}")
            else:
                raise FileNotFoundError(f"No checkpoint files (.safetensors/.bin) found in {repo_id}")
        
        # Download the specific file at the specified revision
        local_path = hf_hub_download(
            repo_id=repo_id,
            filename=target_file,
            revision=revision,
            cache_dir=None  # Use default cache
        )
        
        print(f"Downloaded: {target_file}")
        
        # Parse dtype
        dtype = parser_func(Path(local_path))
        return dtype
        
    except Exception as e:
        print(f"Error getting dtype from {repo_id} @ {revision}: {e}")
        return None

def inspect_model_dtype(
    repo_id: str,
    commit_hash: Optional[str] = None,
    filename: Optional[str] = None,
    verbose: bool = True
    ) -> Optional[torch.dtype]:
    """
    Main utility function to inspect model dtype from repo and commit.
    
    Args:
        repo_id: HuggingFace model repository ID
        commit_hash: Specific commit hash (optional, defaults to main branch)
        filename: Specific checkpoint file to inspect (optional)
        verbose: Whether to print progress info
        
    Returns:
        torch.dtype of the model weights
        
    Example:
        # Latest version
        dtype = inspect_model_dtype("microsoft/DialoGPT-medium")
        
        # Specific commit
        dtype = inspect_model_dtype("microsoft/DialoGPT-medium", "abc123def456")
        
        # Specific file and commit  
        dtype = inspect_model_dtype("microsoft/DialoGPT-medium", "abc123", "pytorch_model.bin")
    """
    if not verbose:
        # Suppress print statements
        import sys
        from io import StringIO
        old_stdout = sys.stdout
        sys.stdout = StringIO()
        
    try:
        dtype = get_native_dtype_from_commit(repo_id, commit_hash, filename)
        return dtype
    finally:
        if not verbose:
            sys.stdout = old_stdout

def fit_nb_mom(x):
    """Method-of-moments NB fit. Returns (r, p)."""
    x = np.asarray(x)
    m = x.mean()
    v = x.var(ddof=1)
    if v <= m:          # Poisson or under-dispersed: fall back gracefully
        return np.inf, 1.0
    r = m**2 / (v - m)  # size (shape, dispersion)
    p = r / (r + m)     # success-prob
    return r, p

def right_tail_test(x_calib, y_test):
    """
    x_calib : length-256 array used for calibration
    y_test  : length-256 array to be tested
    returns (individual_sf, omnibus_p)
    """
    x_calib = np.asarray(x_calib)
    y_test = np.asarray(y_test)

    # --- 1) Winsorize calibration to defang single spikes --------------------
    if x_calib.size == 0 or y_test.size == 0:
        raise ValueError("right_tail_test requires non-empty inputs")
    cap = np.percentile(x_calib, 99)  # high-percentile cap
    x_clip = np.minimum(x_calib, cap)

    # --- 2) NB fit with lower bounds on dispersion/prob ----------------------
    r, p = fit_nb_mom(x_clip)
    R_MIN = 0.5
    P_MIN = 1e-3
    valid_fit = np.isfinite(r) and r > 0 and p > 0
    if valid_fit:
        r = max(r, R_MIN)
        p = max(p, P_MIN)

    # --- 3) Compute survival; fallback to empirical if fit is bad ------------
    try:
        if not valid_fit:
            raise ValueError("Invalid NB fit")
        sf = stats.nbinom.sf(y_test - 1, r, p)
    except Exception:
        # Empirical right-tail: P(X >= y) from calibration directly
        sf = (x_calib.reshape(1, -1) >= y_test.reshape(-1, 1)).mean(axis=1)

    # Avoid log(0) in Fisher combine
    sf = np.clip(sf, 1e-12, 1.0)

    # Fisher combination: -2 Σ ln(p_i) ~ χ²_{2k}
    chi2_stat = -2.0 * np.sum(np.log(sf))
    df = 2 * len(y_test)
    omnibus_p = stats.chi2.sf(chi2_stat, df)
    return sf, omnibus_p

def string_to_bytes(data):
    """Convert string to bytes, prioritizing hex over base64—and strip ALL whitespace."""
    if isinstance(data, bytes):
        return data

    if not isinstance(data, str):
        raise ValueError(f"Expected string or bytes, got {type(data)}")

    # 1) Remove _all_ whitespace (spaces, tabs, newlines) before anything else
    #    We keep the original for base64, but use cleaned for hex.
    raw = data.strip()
    cleaned = re.sub(r'\s+', '', raw)

    # 2) Try hex on the cleaned string
    if len(cleaned) % 2 == 0 and HEX_PATTERN.match(cleaned):
        try:
            return bytes.fromhex(cleaned)
        except ValueError:
            pass  # fall through to base64

    # 3) Otherwise try base64 on the raw string (base64 tolerates whitespace)
    try:
        return base64.b64decode(raw, validate=True)
    except Exception:
        pass

    # 4) Last‐ditch: hex again on cleaned
    try:
        return bytes.fromhex(cleaned)
    except Exception as e:
        raise ValueError(f"Failed to decode string as hex or base64: {e}")
        
class RunningMeanCov:
    def __init__(self, dim: int, device):
        self.n     = 0
        self.mean  = torch.zeros(dim,  dtype=torch.float64, device=device)
        self.M2    = torch.zeros(dim, dim, dtype=torch.float64, device=device)

    def update(self, x: torch.Tensor):          # x: [dim]
        self.n += 1
        delta   = x - self.mean
        self.mean += delta / self.n
        self.M2   += torch.outer(delta, x - self.mean)

    @property
    def covariance(self):
        return self.M2 / max(self.n - 1, 1)
