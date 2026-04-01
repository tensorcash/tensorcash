# SPDX-License-Identifier: Apache-2.0
"""Shared Proof-of-Work utilities for both V0 and V1 sampling."""

import torch
import hashlib
from dataclasses import dataclass, field
import math
import os
import time
import uuid
import json
from typing import Optional, Dict, Any
import flatbuffers
import sys
# Import generated FlatBuffers modules. Prefer environment/PYTHONPATH; if missing,
# fall back to repo-relative fb-schemas so local runs don’t depend on absolute paths.
try:
    from proof import Proof, FloatArray, UIntArray
except Exception:
    from pathlib import Path
    here = Path(__file__).resolve()
    for parent in here.parents:
        fb = parent / "fb-schemas"
        if fb.exists():
            sys.path.insert(0, str(fb))
            break
    from proof import Proof, FloatArray, UIntArray
import base64
import numpy as np 
from collections import deque
from pprint import pformat
import binascii

try:
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

POW_WINDOW_SIZE = 256          # length (tokens) of the sliding window

@dataclass
class SequenceCache:
    """All state that lives as long as a seq_id is alive."""
    # grow-only archive, CPU side
    archive: list[int] = field(default_factory=list)      # full prompt + gens
    # fast rolling window, GPU side
    ring: torch.Tensor | None = None       # (POW_WINDOW_SIZE,) int32 CUDA
    ring_pos: int = 0                      # write cursor 0…POW_WINDOW_SIZE-1

@dataclass
class PowState:
    # immutable --------------------------------------------------------
    target: torch.ByteTensor            # (32,) uint8 - 256-bit target
    h_b:   torch.ByteTensor            # (32,) uint8 - block hash (legacy)
    v:     torch.ByteTensor            # (32,) uint8 - VDF
    T:     int                         # python int - tick
    header_prefix: torch.ByteTensor = None  # (76,) uint8 - block header prefix (optional)

    # rolling 256-step ring buffers  ----------------------------------
    topk_logits:   torch.FloatTensor = None   # (256, B, 50) - fp32 for reproducibility
    topk_indices:  torch.IntTensor  = None   # (256, B, 50)
    chosen_probs:  torch.FloatTensor = None   # (256, B)
    chosen_tokens: torch.LongTensor  = None   # (256, B)
    attention_mask:torch.BoolTensor = None   # (256, B)
    steps:         torch.IntTensor  = None   # (B,)
    window_pos:    int              = 0      # python int

    # For probability reconstruction -----------------------
    sampling_u:    torch.FloatTensor = None   # (256, B) - random values used for sampling

    # These are scalar per sequence, not per token - storing once when updated
    temperature_by_seq: dict[int, float] = field(default_factory=dict)
    top_p_by_seq: dict[int, float] = field(default_factory=dict)
    top_k_by_seq: dict[int, int] = field(default_factory=dict)
    rep_penalty_by_seq: dict[int, float] = field(default_factory=dict)
    softmax_normalizers: torch.FloatTensor = None # (256, B) - softmax denominators (fp32)
    seq_cache: dict[int, SequenceCache] = field(default_factory=dict)

# Helper for logging
class Logger:
    def __init__(self, log_dir=None):
        if not log_dir:
            log_dir = os.environ.get("POW_LOG_DIR", "/data/miner_logs")
        os.makedirs(log_dir, exist_ok=True)
        self.log_file_path = os.path.join(log_dir, "pow_sampler.log")

    # @pow_profiler
    def log(self, message, level="INFO"):
        try:
            timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
            with open(self.log_file_path, "a") as f:
                f.write(f"[{timestamp}] [{level}] {message}\n")
        except Exception:
            pass  # Silent failure

# Row manager for efficient assignment of buffer rows to sequences
class RowManager:
    def __init__(self, max_rows):
        self.max_rows = max_rows
        self.seqid_to_row = {}
        # deque gives O(1) pops on both ends
        self.free_rows = deque(range(max_rows))
        # Track allocation order for FIFO tie-breaking
        self.allocation_order = {}
        self.next_allocation_id = 0

    def get_row(self, seq_id):
        """Return the row currently assigned to seq_id, or None."""
        return self.seqid_to_row.get(seq_id)

    def allocate_row(self, seq_id):
        """
        Allocate a row for seq_id.
        - If already allocated, returns existing row.
        - If no free rows remain, returns None.
        - Otherwise, pops from the left (FIFO) and assigns it.
        """
        if seq_id in self.seqid_to_row:
            return self.seqid_to_row[seq_id]

        if not self.free_rows:
            return None  # out of slots

        row = self.free_rows.popleft()  # FIFO: oldest freed row
        self.seqid_to_row[seq_id] = row
        # Track allocation order for FIFO eviction
        self.allocation_order[seq_id] = self.next_allocation_id
        self.next_allocation_id += 1
        return row

    def free_row(self, seq_id):
        """
        Release the row for seq_id (if any), returning it to the free pool.
        Returns the freed row index, or None if seq_id was not allocated.
        """
        row = self.seqid_to_row.pop(seq_id, None)
        if row is not None:
            self.free_rows.append(row)  # goes to the right, newest free
            # Clean up allocation order tracking
            self.allocation_order.pop(seq_id, None)
        return row
    
    @pow_profiler
    def get_oldest_sequence(self, steps):
        if not self.seqid_to_row:
            return None, None
        
        # Get all rows and their steps as tensors
        seq_ids = list(self.seqid_to_row.keys())
        rows = [self.seqid_to_row[sid] for sid in seq_ids]
        
        # Get steps for all rows at once
        rows_tensor = torch.tensor(rows, device=steps.device)
        steps_values = steps[rows_tensor]
        
        # Find max using torch operations
        max_steps = steps_values.max()
        
        # Find all sequences with max steps
        max_mask = steps_values == max_steps
        
        # Among ties, find earliest allocation
        tied_indices = torch.where(max_mask)[0]
        
        # Get allocation orders for tied sequences
        tied_allocs = torch.tensor([
            self.allocation_order[seq_ids[i]] for i in tied_indices.cpu().numpy()
        ])
        
        # Find minimum allocation (earliest)
        earliest_idx = tied_indices[tied_allocs.argmin()]
        
        return seq_ids[earliest_idx], rows[earliest_idx]

# GPU-indexed pow params per row - eliminates CPU-side grouping and repeated hex decoding
class RingBuffers:
    def __init__(self, window_size, max_rows, device="cuda"):
        self.window_size = window_size
        self.max_rows = max_rows
        self.device = device

        # --- packed storage (same idea you implemented) ---
        # floats: [topk_logits:70 | chosen_prob:1 | sampling_u:1 | logZ:1 | lse_stats:6] = 79
        self._float_block = torch.zeros(
            (window_size, max_rows, 79), dtype=torch.float32, device=device
        )
        # ints: [topk_indices:70]
        self._int_block = torch.zeros(
            (window_size, max_rows, 70), dtype=torch.int32, device=device
        )
        # keep mask as bool to avoid surprises elsewhere
        self.attention_mask = torch.zeros(
            (window_size, max_rows), dtype=torch.bool, device=device
        )
        # IMPORTANT: keep chosen tokens as int64 to preserve byte layout for _tok_le_bytes
        self.chosen_tokens = torch.zeros(
            (window_size, max_rows), dtype=torch.int64, device=device
        )

        # steps stays separate (per-row)
        self.steps = torch.zeros(max_rows, dtype=torch.int32, device=device)

        # --- POW PARAMS per row (written once when sequence is allocated) ---
        # These are indexed by row, not by window position
        # Eliminates CPU-side grouping and repeated hex→bytes conversion
        self.pow_tick = torch.zeros(max_rows, dtype=torch.int64, device=device)
        self.pow_request_id = torch.zeros(max_rows, dtype=torch.int64, device=device)
        self.pow_header = torch.zeros((max_rows, 76), dtype=torch.uint8, device=device)  # 76-byte header prefix
        self.pow_vdf = torch.zeros((max_rows, 1024), dtype=torch.uint8, device=device)   # VDF can be up to 1024 bytes
        self.pow_vdf_len = torch.zeros(max_rows, dtype=torch.int32, device=device)       # actual VDF length
        self.pow_target = torch.zeros((max_rows, 32), dtype=torch.uint8, device=device)  # 32-byte target
        # Slice 11.4 — share target (model-adjusted, derived by the
        # broker / worker from base_share_target * N / D). Numerically
        # LARGER (easier) than ``pow_target``; mining-tier proofs that
        # meet share but not block are sub-block share emissions.
        # All-zero == "share emission disabled for this row"; the
        # sampler falls back to the legacy block-only check then.
        self.pow_share_target = torch.zeros((max_rows, 32), dtype=torch.uint8, device=device)
        self.pow_block_hash = torch.zeros((max_rows, 32), dtype=torch.uint8, device=device)  # 32-byte block hash
        self.pow_header_len = torch.zeros(max_rows, dtype=torch.int32, device=device)    # actual header length (32 or 76)
        self.pow_valid = torch.zeros(max_rows, dtype=torch.bool, device=device)          # whether pow params are set

        # --- legacy-compatible views on top of packed blocks ---
        # float views
        self.topk_logits         = self._float_block[..., :70]                # (W, R, 70) fp32
        self.chosen_probs        = self._float_block[..., 70]                 # (W, R)     fp32
        self.sampling_u          = self._float_block[..., 71]                 # (W, R)     fp32
        self.softmax_normalizers = self._float_block[..., 72]                 # (W, R)     fp32
        self.logsumexp_stats     = self._float_block[..., 73:79]              # (W, R, 6)  fp32

        # int views
        self.topk_indices        = self._int_block[..., :70]                  # (W, R, 70) int32

    def get_positions(self, rows):
        return self.steps[rows] % self.window_size

    def clear_row(self, row):
        if row is None:
            return
        self.topk_logits[:, row].zero_()
        self.topk_indices[:, row].zero_()
        self.chosen_probs[:, row].zero_()
        self.chosen_tokens[:, row].zero_()     # int64 legacy buffer
        self.attention_mask[:, row].zero_()
        self.sampling_u[:, row].zero_()
        self.softmax_normalizers[:, row].zero_()
        self.logsumexp_stats[:, row].zero_()
        self.steps[row] = 0
        # Clear pow params for this row
        self.pow_tick[row] = 0
        self.pow_request_id[row] = 0
        self.pow_header[row].zero_()
        self.pow_vdf[row].zero_()
        self.pow_vdf_len[row] = 0
        self.pow_target[row].zero_()
        self.pow_share_target[row].zero_()
        self.pow_block_hash[row].zero_()
        self.pow_header_len[row] = 0
        self.pow_valid[row] = False

    def write_pow_params(self, row: int, pow_snapshot: dict):
        """Write pow params to GPU arrays for a specific row.

        Called once when sequence is allocated. Params are then read
        via row indexing in batch_sample_tokens - no more CPU-side grouping.

        Args:
            row: The row index to write to
            pow_snapshot: Dict with tick, header_prefix, vdf, block_hash, target, request_id
        """
        if pow_snapshot is None:
            self.pow_valid[row] = False
            return

        vdf_hex = pow_snapshot.get("vdf", "")
        self.pow_tick[row] = pow_snapshot["tick"]
        self.pow_request_id[row] = pow_snapshot.get("request_id", 0)

        # Decode and write header
        header_hex = pow_snapshot.get("header_prefix", "")
        if header_hex:
            header_bytes = hex_to_bytes_tensor(header_hex, device=self.device)
            hlen = min(header_bytes.numel(), 76)
            self.pow_header[row, :hlen] = header_bytes[:hlen]
            self.pow_header_len[row] = hlen
        else:
            self.pow_header_len[row] = 0

        # Decode and write VDF (can be up to 1024 bytes - commonly 200/341 bytes)
        # vdf_hex already fetched above for debug print
        if vdf_hex:
            vdf_bytes = hex_to_bytes_tensor(vdf_hex, device=self.device)
            vlen = min(vdf_bytes.numel(), 1024)  # VDF can be up to 1024 bytes
            self.pow_vdf[row, :vlen] = vdf_bytes[:vlen]
            self.pow_vdf_len[row] = vlen

        # Decode and write target (32 bytes)
        target_hex = pow_snapshot.get("target", "")
        if target_hex:
            # Pad target to 32 bytes if needed
            if len(target_hex) < 64:
                target_hex = "0" * (64 - len(target_hex)) + target_hex
            elif len(target_hex) > 64:
                target_hex = target_hex[-64:]
            target_bytes = hex_to_bytes_tensor(target_hex, device=self.device)
            tlen = min(target_bytes.numel(), 32)
            self.pow_target[row, :tlen] = target_bytes[:tlen]

        # Slice 11.4 — share target (model-adjusted). Optional; rows
        # without a share_target keep ``pow_share_target`` zeroed and
        # the sampler falls back to block-only emission.
        share_target_hex = pow_snapshot.get("share_target", "") or ""
        if share_target_hex:
            if len(share_target_hex) < 64:
                share_target_hex = "0" * (64 - len(share_target_hex)) + share_target_hex
            elif len(share_target_hex) > 64:
                share_target_hex = share_target_hex[-64:]
            share_bytes = hex_to_bytes_tensor(share_target_hex, device=self.device)
            slen = min(share_bytes.numel(), 32)
            self.pow_share_target[row, :slen] = share_bytes[:slen]

        # Decode and write block_hash (32 bytes)
        block_hash_hex = pow_snapshot.get("block_hash", "")
        if block_hash_hex:
            bh_bytes = hex_to_bytes_tensor(block_hash_hex, device=self.device)
            bhlen = min(bh_bytes.numel(), 32)
            self.pow_block_hash[row, :bhlen] = bh_bytes[:bhlen]

        # If no header_prefix, use block_hash as header
        if self.pow_header_len[row] == 0 and block_hash_hex:
            self.pow_header[row, :bhlen] = self.pow_block_hash[row, :bhlen]
            self.pow_header_len[row] = bhlen

        self.pow_valid[row] = True

    def clear_rows(self, rows):
        if not rows:
            return
        r = torch.as_tensor(rows, device=self.device, dtype=torch.long)
        self.topk_logits[:, r].zero_()
        self.topk_indices[:, r].zero_()
        self.chosen_probs[:, r].zero_()
        self.chosen_tokens[:, r].zero_()
        self.attention_mask[:, r].zero_()
        self.sampling_u[:, r].zero_()
        self.softmax_normalizers[:, r].zero_()
        self.logsumexp_stats[:, r].zero_()
        self.steps[r] = 0
        # Clear pow params for these rows
        self.pow_tick[r] = 0
        self.pow_request_id[r] = 0
        self.pow_header[r].zero_()
        self.pow_vdf[r].zero_()
        self.pow_vdf_len[r] = 0
        self.pow_target[r].zero_()
        self.pow_block_hash[r].zero_()
        self.pow_header_len[r] = 0
        self.pow_valid[r] = False

    def _write_buffers(self, pos, rows, logits, indices, tokens, probs, u, mask, normalizers, stats):
        # pos, rows are 1D tensors on device (your caller already does this)
        # tokens must be int64 (caller already ensures this); others are fp32/i32/bool
        self.chosen_tokens[pos, rows]         = tokens                      # int64
        self.chosen_probs[pos, rows]          = probs                       # fp32
        self.topk_logits[pos, rows]           = logits                      # fp32[...70]
        self.sampling_u[pos, rows]            = u                           # fp32
        self.topk_indices[pos, rows]          = indices.to(torch.int32)     # int32[...70]
        self.softmax_normalizers[pos, rows]   = normalizers                 # fp32
        self.logsumexp_stats[pos, rows]       = stats                       # fp32[...6]
        self.attention_mask[pos, rows]        = mask                        # bool

    def increment_steps(self, rows):
        if len(rows) == 0:
            return
        r = torch.as_tensor(rows, device=self.device, dtype=torch.long)
        self.steps[r] += 1

    def get_window(self, row):
        pos = self.steps[row] % self.window_size
        idx = (torch.arange(self.window_size, device=self.device) + pos) % self.window_size
        return {
            "tokens":              self.chosen_tokens[idx, row],          # int64 (unchanged)
            "probs":               self.chosen_probs[idx, row],
            "topk_logits":         self.topk_logits[idx, row],
            "topk_indices":        self.topk_indices[idx, row],
            "attention_mask":      self.attention_mask[idx, row],
            "sampling_u":          self.sampling_u[idx, row],
            "softmax_normalizers": self.softmax_normalizers[idx, row],
            "logsumexp_stats":     self.logsumexp_stats[idx, row],
        }
            
# Helper for PoW-specific operations
class PowHasher:
    def __init__(self, device="cuda"):
        self.device = device

        # Initialize parameters
        self.h_b = torch.zeros(32, dtype=torch.uint8, device=device)
        self.v = torch.zeros(32, dtype=torch.uint8, device=device)
        self.target = torch.zeros(32, dtype=torch.uint8, device=device)
        self.target[-1] = 0xFF  # Default easy target
        self.share_target = None  # Optional model-adjusted share target
        self.tick = 0
        self.ipfs_cid = None 
        self.header_prefix = None  # Will store the 76-byte header prefix
        self.request_id = None
        self.difficulty = None
        self.prior_hb = ""
        self.prior_vdf = ""
        self.prior_target = ""
        self.prior_share_target = ""
        self.prior_header = ""
        self.prior_precision = ""

    # @pow_profiler
    def update_from_payload(self, payload):
        """Update parameters from a PoW payload."""
        if self.prior_hb != payload["block_hash"]:
            self.h_b = hex_to_bytes_tensor(payload["block_hash"], device=self.device)
            self.prior_hb = payload["block_hash"]

        if self.prior_vdf != payload["vdf"]:
            self.v = hex_to_bytes_tensor(payload["vdf"], device=self.device)
            self.prior_vdf = payload["vdf"]

        self.tick = int(payload["tick"])
        self.request_id = int(payload["request_id"])
        target_hex = payload["target"]
        
        if self.prior_target != target_hex:
            # Ensure target is padded to 32 bytes
            if len(target_hex) < 64:
                target_hex = "0" * (64 - len(target_hex)) + target_hex
            elif len(target_hex) > 64:
                target_hex = target_hex[-64:]
            self.target = hex_to_bytes_tensor(target_hex, device=self.device)
            self.prior_target = target_hex

        # Optional singleton share target. This keeps older vLLM forks from
        # needing a sampler dict-literal edit: update_from_payload already
        # receives the full pow payload, and check_share_solution falls back
        # to this value when a per-sequence pow_snapshot lacks share_target.
        share_target_hex = payload.get("share_target") or ""
        if share_target_hex:
            if len(share_target_hex) < 64:
                share_target_hex = "0" * (64 - len(share_target_hex)) + share_target_hex
            elif len(share_target_hex) > 64:
                share_target_hex = share_target_hex[-64:]
        if share_target_hex != self.prior_share_target:
            if not share_target_hex:
                self.share_target = None
            else:
                self.share_target = hex_to_bytes_tensor(share_target_hex, device=self.device)
            self.prior_share_target = share_target_hex

        self.difficulty = payload["difficulty"]

        # Decode header prefix if present
        if self.prior_header != payload["header_prefix"]:
            self.header_prefix = hex_to_bytes_tensor(payload["header_prefix"], device=self.device)
            self.prior_header = payload["header_prefix"]

        # Decode header prefix if present
        if "ipfs_cid" in payload:
            self.ipfs_cid = payload["ipfs_cid"]

    # @pow_profiler
    def batch_sample_tokens(self, contexts, steps, cdfs, compute_precision, ring_buffers=None, rows_tensor=None, pow_snapshot=None):
        """Sample tokens for multiple sequences in a batch.

        Args:
            contexts: Token context windows (B, W) int64
            steps: Step indices for each sequence (B,) int32
            cdfs: Cumulative distribution functions for sampling (B, V)
            compute_precision: Precision string (e.g., 'fp16')
            ring_buffers: RingBuffers instance with per-row pow params (preferred)
            rows_tensor: (B,) tensor of row indices to read pow params from ring_buffers
            pow_snapshot: DEPRECATED - Optional dict for backwards compatibility

        When ring_buffers and rows_tensor are provided, pow params are read directly
        from GPU arrays indexed by row - no CPU-side grouping or hex decoding needed.
        This is the preferred path for mixed-request batches under priority scheduling.
        """
        B = contexts.size(0)
        ctx_bytes = _tok_le_bytes(contexts)
        j4 = _u32le(steps.view(-1, 1))

        # NEW PATH: Read pow params from GPU arrays indexed by row
        if ring_buffers is not None and rows_tensor is not None:
            # Gather ticks for all rows - (B,)
            ticks = ring_buffers.pow_tick[rows_tensor]  # (B,) int64

            # Gather header data - need to handle variable header lengths
            header_lens = ring_buffers.pow_header_len[rows_tensor]  # (B,) int32
            headers = ring_buffers.pow_header[rows_tensor]  # (B, 76) uint8
            vdfs = ring_buffers.pow_vdf[rows_tensor]  # (B, 1024) uint8
            vdf_lens = ring_buffers.pow_vdf_len[rows_tensor]  # (B,) int32

            # Convert ticks to bytes - each row gets its own T8
            # T8 needs to be (B, 4) for per-row tick values
            T8_batch = _u32le(ticks.to(torch.uint32))  # (B, 4)

            # Check if we can use fast batched path (all same header and VDF lengths)
            unique_hlens = header_lens.unique()
            unique_vlens = vdf_lens.unique()

            if compute_precision != self.prior_precision:
                self.precision_bytes = _str_bytes(compute_precision, batch_size=1, device=self.device)
                self.prior_precision = compute_precision

            if len(unique_hlens) == 1 and len(unique_vlens) == 1:
                # All same lengths - fast batched path
                hlen = unique_hlens[0].item()
                vlen = unique_vlens[0].item()
                header_data = headers[:, :hlen]  # (B, hlen)
                vdf_data = vdfs[:, :vlen]  # (B, vlen)
                pb = self.precision_bytes.expand(B, -1)

                msg = torch.cat([
                    header_data,           # (B, hlen)
                    vdf_data,              # (B, vlen)
                    T8_batch,              # (B, 4)
                    j4,                    # (B, 4)
                    ctx_bytes,             # (B, L*8)
                    pb,                    # (B, precision_len)
                ], dim=1)

                digests = sha256_many(msg)
                us = _digest_to_u(digests)
                token_ids = torch.searchsorted(cdfs, us.unsqueeze(-1)).squeeze(-1).to(torch.int64)
                return token_ids, us, digests
            else:
                # Mixed header/VDF lengths - process per-row
                all_tokens = []
                all_us = []
                all_digests = []

                for i in range(B):
                    hlen = header_lens[i].item()
                    vlen = vdf_lens[i].item()
                    header_i = headers[i, :hlen].unsqueeze(0)  # (1, hlen)
                    vdf_i = vdfs[i, :vlen].unsqueeze(0)  # (1, vlen)
                    T8_i = T8_batch[i].unsqueeze(0)  # (1, 4)
                    j4_i = j4[i].unsqueeze(0)  # (1, 4)
                    ctx_i = ctx_bytes[i].unsqueeze(0)  # (1, L*8)
                    pb_i = self.precision_bytes  # (1, len)

                    msg_i = torch.cat([header_i, vdf_i, T8_i, j4_i, ctx_i, pb_i], dim=1)
                    digest_i = sha256_many(msg_i)
                    u_i = _digest_to_u(digest_i)
                    tok_i = torch.searchsorted(cdfs[i:i+1], u_i.unsqueeze(-1)).squeeze(-1)

                    all_tokens.append(tok_i)
                    all_us.append(u_i)
                    all_digests.append(digest_i)

                token_ids = torch.cat(all_tokens, dim=0).to(torch.int64)
                us = torch.cat(all_us, dim=0)
                digests = torch.cat(all_digests, dim=0)
                return token_ids, us, digests

        # LEGACY PATH: Use pow_snapshot dict or shared state
        if pow_snapshot is not None:
            tick = pow_snapshot["tick"]

            # Prefer precomputed tensors when present to avoid repeated hex decoding
            header_data = pow_snapshot.get("header_tensor")
            vdf_data = pow_snapshot.get("vdf_tensor")
            block_hash_tensor = pow_snapshot.get("block_hash_tensor")

            if header_data is None:
                header_hex = pow_snapshot.get("header_prefix")
                if header_hex:
                    header_data = hex_to_bytes_tensor(header_hex, device=self.device)

            if vdf_data is None:
                vdf_hex = pow_snapshot["vdf"]
                vdf_data = hex_to_bytes_tensor(vdf_hex, device=self.device)

            if block_hash_tensor is None:
                block_hash_hex = pow_snapshot["block_hash"]
                block_hash_tensor = hex_to_bytes_tensor(block_hash_hex, device=self.device)

            # Fall back to block_hash if no header_prefix
            if header_data is None or header_data.numel() == 0:
                header_data = block_hash_tensor
        else:
            tick = self.tick
            header_data = self.header_prefix if self.header_prefix is not None else self.h_b
            vdf_data = self.v

        # Convert tick to bytes (single tick for all rows in legacy path)
        T8 = _u32le(torch.tensor([tick], dtype=torch.uint32, device=self.device))

        # Convert precision to bytes
        if compute_precision != self.prior_precision:
            self.precision_bytes = _str_bytes(compute_precision, batch_size=1, device=self.device)
            self.prior_precision = compute_precision

        pb = self.precision_bytes.expand(ctx_bytes.size(0), -1)

        # Build message
        msg = _build_msg(header_data, vdf_data, T8, j4, ctx_bytes, pb)

        # Compute hash
        digests = sha256_many(msg)

        # Convert to uniform values
        us = _digest_to_u(digests)

        # Sample tokens
        token_ids = torch.searchsorted(cdfs, us.unsqueeze(-1)).squeeze(-1)
        token_ids = token_ids.to(torch.int64)

        return token_ids, us, digests

    def sample_token(self, context, step, cdf):
        """Sample a token using PoW hash."""
        # Convert context to bytes
        ctx_bytes = _tok_le_bytes(context)

        # Convert step to bytes
        j4 = _u32le(step.view(-1, 1))

        # Convert tick to bytes
        T8 = _u32le(torch.tensor([self.tick], dtype=torch.uint32, device=self.device))

        # Use header_prefix if available, otherwise fall back to h_b
        header_data = self.header_prefix if self.header_prefix is not None else self.h_b

        # Get precision bytes - create if not exists
        if not hasattr(self, 'precision_bytes'):
            compute_precision = getattr(self, 'compute_precision', 'fp16')
            self.precision_bytes = _str_bytes(compute_precision, batch_size=1, device=self.device)
        pb = self.precision_bytes.expand(ctx_bytes.size(0), -1)

        # Build message
        msg = _build_msg(header_data, self.v, T8, j4, ctx_bytes, pb)

        # Compute hash
        digest = sha256_many(msg)

        # Convert to uniform value
        u = _digest_to_u(digest)

        # Sample token
        token_id = torch.searchsorted(cdf, u.unsqueeze(-1)).squeeze(-1)

        return token_id, u, digest

    # @pow_profiler
    def check_share_solution(self, digest, pow_snapshot):
        """Slice 11.4 — companion to ``check_solution`` that gates on
        the EASIER share threshold instead of the block target.

        Returns a same-shape bool tensor: True iff the per-row
        canonical chain header hash satisfies
        ``pow_snapshot["share_target"]``. Used by
        ``common_sampler_helper`` to dual-classify each sampled
        digest:
          - meets block target → MineResult (existing path)
          - meets share but not block → MineShare (new sub-block path)
          - meets neither → discard (unless proxy_audit_enabled)

        Returns all-False when ``share_target`` is absent (no
        share-mode for this snapshot) — caller's existing
        block-only behaviour is preserved.
        """
        if pow_snapshot:
            header_hex = pow_snapshot.get("header_prefix")
            block_hash_hex = pow_snapshot.get("block_hash")
            header_data = (
                hex_to_bytes_tensor(header_hex, device=self.device)
                if header_hex else None
            )
            if header_data is None or header_data.numel() == 0:
                header_data = hex_to_bytes_tensor(block_hash_hex, device=self.device)

            share_target_hex = pow_snapshot.get("share_target") or ""
            if share_target_hex:
                # Pad/truncate to canonical 64 hex chars before tensoring.
                if len(share_target_hex) < 64:
                    share_target_hex = "0" * (64 - len(share_target_hex)) + share_target_hex
                elif len(share_target_hex) > 64:
                    share_target_hex = share_target_hex[-64:]
                share_target_data = hex_to_bytes_tensor(share_target_hex, device=self.device)
            else:
                share_target_data = self.share_target
        else:
            header_data = self.header_prefix if self.header_prefix is not None else self.h_b
            share_target_data = self.share_target

        if share_target_data is None:
            return torch.zeros(digest.size(0), dtype=torch.bool, device=self.device)

        B = digest.size(0)
        nonces = digest[:, :4]
        headers = torch.cat([
            header_data.unsqueeze(0).expand(B, -1),
            nonces,
        ], dim=1)
        first_hash = sha256_many(headers)
        header_hashes = sha256_many(first_hash)
        return check_hash_against_target(header_hashes, share_target_data)

    # @pow_profiler
    def check_solution(self, digest, pow_snapshot=None):
        """Check if digest represents a valid PoW solution.

        Args:
            digest: The hash digest to check
            pow_snapshot: Optional dict with frozen pow params (header_prefix, target, block_hash).
                         If provided, uses these instead of shared state to avoid cross-request
                         contamination under priority scheduling.
        """
        B = digest.size(0)

        # Use snapshot params if provided, otherwise fall back to shared state
        if pow_snapshot is not None:
            header_hex = pow_snapshot["header_prefix"]
            target_hex = pow_snapshot["target"]
            block_hash_hex = pow_snapshot["block_hash"]

            header_data = hex_to_bytes_tensor(header_hex, device=self.device) if header_hex else None
            target_data = hex_to_bytes_tensor(target_hex, device=self.device)

            # Fall back to block_hash if no header_prefix
            if header_data is None or header_data.numel() == 0:
                header_data = hex_to_bytes_tensor(block_hash_hex, device=self.device)
        else:
            header_data = self.header_prefix if self.header_prefix is not None else self.h_b
            target_data = self.target

        # Extract nonces (first 4 bytes of each digest)
        nonces = digest[:, :4]

        # Build complete 80-byte headers for all sequences
        headers = torch.cat([
            header_data.unsqueeze(0).expand(B, -1),
            nonces
        ], dim=1)

        # First SHA-256
        first_hash = sha256_many(headers)

        # Second SHA-256
        header_hashes = sha256_many(first_hash)

        # Check if header hashes meet target
        return check_hash_against_target(header_hashes, target_data)

# Helper for writing proofs
class ProofWriter:
    def __init__(self, output_dir="/data/pow_proofs"):
        self.output_dir = output_dir
        os.makedirs(output_dir, exist_ok=True)
        self.submit_callback = None
        # Initialize model_identifier with default value
        self.model_identifier = None
        self.compute_precision = None
        self.model_config_diff = None
        self.sampling_params_diff = None
        self.ipfs_cid = None

    def set_callback(self, callback):
        """Set callback for when a solution is found."""
        self.submit_callback = callback

    def set_model_identifier(self, model_identifier):
        """Set the model_identifier to be included in proofs."""
        self.model_identifier = model_identifier

    def set_ipfs_cid(self, ipfs_cid):
        """Set the model_ifps_cid to be included in proofs."""
        self.ipfs_cid = ipfs_cid

    def set_model_config_diff(self, model_config_diff):
        """Set the model_config_diff to be included in proofs."""
        self.model_config_diff = model_config_diff

    def set_sampling_params_diff(self, sampling_params_diff):
        """Set the sampling_params_diff to be included in proofs."""
        self.sampling_params_diff = sampling_params_diff

    def set_compute_precision(self, precision: str):
        """Attach compute-precision (fp16 / int8-awq / …) to all proofs."""
        self.compute_precision = precision        

    def write_proof(self, seq_id, step_num, window_data, digest, 
                  is_solution, pow_params, seq_info, completion_id: str | None = None):
        """Write a proof to disk."""
        # Create proof dictionary
        proof = {
            "ipfs_cid": pow_params["ipfs_cid"],
            "model_config_diff": self.model_config_diff,
            "sampling_params_diff": self.sampling_params_diff,
            "tick": pow_params["tick"],
            "target": pow_params["target"],
            "block_hash": pow_params["block_hash"],
            "vdf": pow_params["vdf"],
            "sequence_id": seq_id,
            "steps": step_num,
            "chosen_tokens": window_data["tokens"].cpu().tolist(),
            "chosen_probs": window_data["probs"].cpu().tolist(),
            "topk_logits": window_data["topk_logits"].cpu().tolist(),
            "topk_indices": window_data["topk_indices"].cpu().tolist(),
            "attention_mask": window_data["attention_mask"].cpu().tolist(),
            "sampling_u": window_data["sampling_u"].cpu().tolist(),
            "softmax_normalizers": window_data["softmax_normalizers"].cpu().tolist(),
            "logsumexp_stats": window_data["logsumexp_stats"].cpu().tolist(),
            "hash": digest[0].cpu().numpy().tobytes().hex(),
            "is_solution": bool(is_solution),
            "timestamp": time.time()
        }

        # Add model_identifier if available
        if self.model_identifier:
            proof["model_identifier"] = self.model_identifier
        if self.compute_precision:
            proof["compute_precision"] = self.compute_precision

        # Add header_prefix if available
        if "header_prefix" in pow_params:
            proof["header_prefix"] = pow_params["header_prefix"]

        # Add sequence info
        proof.update(seq_info)

        # Embed completion_id into model_config_diff (serialized as extra_flags)
        try:
            # Ensure model_config_diff is a dict we can extend
            base_diff = {}
            if isinstance(self.model_config_diff, dict):
                base_diff = dict(self.model_config_diff)
            elif isinstance(self.model_config_diff, str) and self.model_config_diff.strip():
                try:
                    import json as _json
                    base_diff = _json.loads(self.model_config_diff)
                except Exception:
                    base_diff = {"_diff": self.model_config_diff}
            # merge completion_id if provided
            if completion_id:
                base_diff["completion_id"] = completion_id
            # also allow passing pre-baked diff via pow_params
            if "model_config_diff" in pow_params and pow_params["model_config_diff"]:
                # best-effort merge: if pow_params contains JSON string, merge keys
                _mcd = pow_params["model_config_diff"]
                if isinstance(_mcd, dict):
                    base_diff.update(_mcd)
                elif isinstance(_mcd, str):
                    try:
                        import json as _json
                        base_diff.update(_json.loads(_mcd))
                    except Exception:
                        base_diff["_mcd"] = _mcd
            self.model_config_diff = base_diff
        except Exception:
            # Fallback: simple string with completion id
            if completion_id:
                self.model_config_diff = {"completion_id": completion_id}

        # Generate filename
        filename = f"pow_proof_{seq_id}_{step_num}_{uuid.uuid4().hex[:8]}.json"
        filepath = os.path.join(self.output_dir, filename)

        # Write to disk (optional)
        if os.environ.get("POW_SAVE_TO_DISK", "0") in ("1", "true", "True"):
            try:
                with open(filepath, 'w') as f:
                    json.dump(proof, f, indent=2)
            except Exception:
                pass

        # Serialize and write to file
        data = serialize_proof(proof)
        filename = f"pow_proof_{seq_id}_{step_num}_{uuid.uuid4().hex[:8]}.bin"
        filepath = os.path.join(self.output_dir, filename)        
        if os.environ.get("POW_SAVE_TO_DISK", "0") in ("1", "true", "True"):
            try: 
                with open(filepath, "wb") as f:
                    f.write(data)
            except Exception:
                print("UNABLE TO SAVE FB")
                pass
        return data, proof

def _to_bytes(x):
    if isinstance(x, (bytes, bytearray)):
        return bytes(x)
    if isinstance(x, str):
        s = x.strip()
        if not s:
            return b''
        try:
            return bytes.fromhex(s)
        except ValueError:
            return base64.b64decode(s)
    raise TypeError(f"Expected bytes or str, got {type(x)}")

# @pow_profiler
def serialize_proof(obj: dict) -> bytes:
    b = flatbuffers.Builder(1024)

    mid_off = b.CreateString(obj['model_identifier'])
    cp_off  = b.CreateString(obj['compute_precision'])
    ipfs_off  = b.CreateString(obj['ipfs_cid'])
    extra_off  = b.CreateString(to_python_string(obj['model_config_diff']))
    
    # — raw bytes —
    tgt_off  = b.CreateByteVector(_to_bytes(obj['target']))
    vdf_off  = b.CreateByteVector(_to_bytes(obj['vdf']))
    block_hash_off = b.CreateByteVector(_to_bytes(obj['block_hash']))
    hash_off = b.CreateByteVector(_to_bytes(obj['hash']))
    hdr_off = b.CreateByteVector(_to_bytes(obj['header_prefix']))

    temp = float(obj['temperature'])
    p    = float(obj['top_p'])
    k    = int(obj['top_k']) & 0xFFFFFFFF
    rp   = float(obj['repetition_penalty'])        
        
    # — typed 1D vectors —
    def make_vec_uint32(data):
        b.StartVector(4, len(data), 4)
        for v in reversed(data):
            # force into C uint32 range
            val = int(v) & 0xFFFFFFFF
            b.PrependUint32(val)
        return b.EndVector()

    #  8-bit unsigned ints
    def make_vec_uint8(data):
        # element size=1, alignment=1
        b.StartVector(1, len(data), 1)
        for v in reversed(data):
            val = int(v) & 0xFF
            b.PrependUint8(val)
        return b.EndVector(len(data))

    # For a bool vector (PadMask):
    def make_vec_bool(data):
        # element size=1 byte, alignment=1
        b.StartVector(1, len(data), 1)
        for v in reversed(data):
            # Cast to Python bool
            b.PrependBool(bool(v))
        return b.EndVector(len(data))
        
    def make_vec_float32(data):
        b.StartVector(4, len(data), 4)
        for v in reversed(data):
            # round to IEEE754 float32 exactly
            f32 = np.float32(v).item()
            b.PrependFloat32(f32)
        return b.EndVector()

    ctoks = make_vec_uint32(obj['chosen_tokens'])
    pp    = make_vec_float32(obj['chosen_probs'])
    su    = make_vec_float32(obj['sampling_u'])
    sn    = make_vec_float32(obj['softmax_normalizers'])
    pt    = make_vec_uint32(obj['prompt_tokens'])
    pm    = make_vec_bool(obj['pad_mask'])

    # — 2D via wrappers with same precision guarantees —
    def _wrap_float32(row):
        FloatArray.FloatArrayStartValuesVector(b, len(row))
        for v in reversed(row):
            f32 = np.float32(v).item()
            b.PrependFloat32(f32)
        vec = b.EndVector()
        FloatArray.FloatArrayStart(b)
        FloatArray.FloatArrayAddValues(b, vec)
        return FloatArray.FloatArrayEnd(b)

    def _wrap_uint32(row):
        UIntArray.UIntArrayStartValuesVector(b, len(row))
        for v in reversed(row):
            u32 = int(v) & 0xFFFFFFFF
            b.PrependUint32(u32)
        vec = b.EndVector()
        UIntArray.UIntArrayStart(b)
        UIntArray.UIntArrayAddValues(b, vec)
        return UIntArray.UIntArrayEnd(b)

    logits_offs = [_wrap_float32(r) for r in obj['topk_logits']]
    Proof.StartTopkLogitsVector(b, len(logits_offs))
    for off in reversed(logits_offs): 
        b.PrependUOffsetTRelative(off)
    topk_logits_off = b.EndVector()

    idx_offs = [_wrap_uint32(r) for r in obj['topk_indices']]
    Proof.StartTopkIndicesVector(b, len(idx_offs))
    for off in reversed(idx_offs): 
        b.PrependUOffsetTRelative(off)
    topk_indices_off = b.EndVector()

    lse_offs = [_wrap_float32(r) for r in obj['logsumexp_stats']]
    Proof.StartLogsumexpStatsVector(b, len(lse_offs))
    for off in reversed(lse_offs): 
        b.PrependUOffsetTRelative(off)
    lse_off = b.EndVector()
    
    # — root table with explicit uint64 masking —
    Proof.Start(b)
    Proof.AddVersion(b,      int(2) )
    Proof.AddTick(b,      int(obj['tick'])      & 0xFFFFFFFFFFFFFFFF)
    Proof.AddTimestamp(b, int(obj['timestamp']) & 0xFFFFFFFFFFFFFFFF)
    Proof.AddIsSolution(b, 1 if obj['is_solution'] else 0)
    Proof.AddModelIdentifier(b, mid_off)
    Proof.AddComputePrecision(b, cp_off)
    Proof.AddIpfsCid(b, ipfs_off)
    Proof.AddExtraFlags(b, extra_off)
    Proof.AddTemperature(b, temp)
    Proof.AddTopP(b, p)
    Proof.AddTopK(b, k)
    Proof.AddRepetitionPenalty(b, rp)    

    Proof.AddTarget(b,         tgt_off)
    Proof.AddVdf(b,            vdf_off)
    Proof.AddHash(b,           hash_off)
    Proof.AddBlockHash(b,           block_hash_off)
    Proof.AddHeaderPrefix(b, hdr_off)

    Proof.AddChosenTokens(b,      ctoks)
    Proof.AddChosenProbs(b,       pp)
    Proof.AddSamplingU(b,         su)
    Proof.AddSoftmaxNormalizers(b, sn)
    Proof.AddLogsumexpStats(b,    lse_off)
    Proof.AddPromptTokens(b,      pt)
    Proof.AddPadMask(b,           pm)
    Proof.AddTopkLogits(b,        topk_logits_off)
    Proof.AddTopkIndices(b,       topk_indices_off)

    root = Proof.End(b)
    b.Finish(root, file_identifier=b"PROF")
    return bytes(b.Output())

# @pow_profiler
def sha256_many(msg: torch.ByteTensor) -> torch.ByteTensor:
    """Ship whole batch to CPU once, hash there, copy one (B,32) back."""
    assert msg.dtype == torch.uint8 and msg.ndim == 2
    device = msg.device
    B, L = msg.shape

    # 1) single hop to CPU (contiguous, no-gradient)
    host = msg.detach().contiguous().to('cpu')
    arr  = host.numpy()  # uint8[B, L], zero-copy view

    # 2) output on CPU (pinned if source was CUDA) + numpy view
    out_cpu = torch.empty((B, 32), dtype=torch.uint8, device='cpu',
                          pin_memory=msg.is_cuda)
    out_np = out_cpu.numpy()

    # 3) per-row hash w/o creating Python bytes every time
    for i in range(B):
        d = hashlib.sha256(memoryview(arr[i])).digest()
        out_np[i, :] = np.frombuffer(d, dtype=np.uint8)

    # 4) single hop back to original device
    return out_cpu.to(device, non_blocking=True)

# @pow_profiler
def check_hash_against_target(digest: torch.ByteTensor,
                              target: torch.ByteTensor) -> torch.BoolTensor:
    """
    Interpret digest & target as Core does (little-endian integers).
    digest: (B,32) raw SHA256^2 output.
    target: (32,) big-endian target bytes.
    """
    # Convert target to little-endian layout
    t_le = target.flip(0)            # now least-significant at index 0

    B = digest.size(0)
    decided = torch.zeros(B, dtype=torch.bool, device=digest.device)
    result  = torch.zeros(B, dtype=torch.bool, device=digest.device)

    # Iterate from most-significant byte (index 31) down to 0
    for i in range(31, -1, -1):
        lt = (digest[:, i] < t_le[i]) & (~decided)
        gt = (digest[:, i] > t_le[i]) & (~decided)
        result |= lt
        decided |= lt | gt
        if decided.all():
            break
    result |= ~decided
    return result

# @pow_profiler
def _tok_le_bytes(tok_i64: torch.Tensor) -> torch.ByteTensor:
    """
    View an (B, L) int64 tensor as little-endian bytes.
    Output : (B, L*8) uint8   (8 bytes per token)
    """
    # Make sure memory is contiguous before the byte-level view
    t = tok_i64.contiguous()
    # Re-interpret the underlying buffer as uint8
    return t.view(torch.uint8).view(t.size(0), -1)

# @pow_profiler
def _u32le(x: torch.Tensor) -> torch.ByteTensor:
    """
    Convert a 32-bit unsigned/int tensor of shape (B,) or (B,1)
    to little-endian byte view (B, 4) uint8.
    """
    x32 = x.reshape(-1).to(torch.uint32).contiguous()     # (B,)
    x8  = x32.view(torch.uint8)                           # reinterpret as bytes
    return x8.view(-1, 4)                                 # (B,4)

# @pow_profiler
def _str_bytes(s: str,
               batch_size: int,
               device: torch.device = torch.device('cpu'),
               encoding: str = 'utf-8') -> torch.ByteTensor:
    """
    Convert a Python string `s` into a (batch_size, len(s)) uint8 ByteTensor.

    Args:
      s: the input string (e.g. 'fp16', 'bf16', etc.)
      batch_size: how many times to replicate this byte sequence (your B)
      device: where the tensor should live
      encoding: text encoding (usually 'utf-8' or 'ascii')

    Returns:
      A ByteTensor of shape (batch_size, len(s)) with dtype=torch.uint8.
    """
    b = s.encode(encoding)               # bytes object
    arr = torch.tensor(list(b),
                       dtype=torch.uint8,
                       device=device)   # shape (len(s),)
    # replicate along batch dimension
    return arr.unsqueeze(0).expand(batch_size, -1)  # (B, len(s))

# @pow_profiler
def _build_msg(header_prefix, v, T8, j4, ctx_bytes, precision):
    """Build the message for SHA-256 hashing.
    
    Args:
        header_prefix: (76,) ByteTensor of block header prefix (or (32,) for legacy block hash)
        v: (32,) ByteTensor of VDF
        T8: (8,) ByteTensor of tick
        j4: (B, 4) ByteTensor of step counter
        ctx_bytes: (B, L) ByteTensor of context tokens       
        precision: (8,) ByteTensor of precision
        
    Returns:
        (B, 76/32+32+8+4+L) ByteTensor of message
    """
    B = ctx_bytes.size(0)
    return torch.cat([
        header_prefix.view(1, -1).expand(B, -1),
        v.view(1, -1).expand(B, -1),
        T8.view(1, -1).expand(B, -1),
        j4,
        ctx_bytes,
        precision,
    ], dim=1)

def _digest_to_u(digest: torch.ByteTensor) -> torch.Tensor:
    """
    Map first 4 bytes of each hash to a float in [0,1).
    Properly handling unsigned values.
    """
    # Process each byte separately and combine with the right scaling
    b0 = digest[:, 0].to(torch.float32)
    b1 = digest[:, 1].to(torch.float32)
    b2 = digest[:, 2].to(torch.float32)
    b3 = digest[:, 3].to(torch.float32)

    # Combine with appropriate scaling for each byte position (little-endian)
    result = (b0 + b1 * 256 + b2 * 65536 + b3 * 16777216) / 4294967296.0
    return result

# @pow_profiler
def hex_to_bytes_tensor(hex_str: str, device="cpu") -> torch.ByteTensor:
    if not hex_str:
        return torch.empty(0, dtype=torch.uint8, device=device)
    # 1) Use binascii to decode hex to raw bytes
    raw = binascii.unhexlify(hex_str)
    # 2) Create a CPU uint8 tensor *without* building an intermediate Python list
    #    torch.frombuffer exists as of PyTorch 1.10
    t = torch.frombuffer(raw, dtype=torch.uint8)
    # 3) Only pay the copy cost if you actually need it on GPU
    if device != "cpu":
        # non_blocking=True lets it overlap with ops if you pin memory
        t = t.to(device, non_blocking=True)
    return t

def nbits_to_target(nbits: int) -> torch.ByteTensor:
    """Convert Bitcoin nBits compact format to 256-bit target tensor."""
    # Extract exponent and mantissa
    exponent = nbits >> 24
    mantissa = nbits & 0x00ffffff

    # Calculate target value
    if exponent <= 3:
        target_int = mantissa >> (8 * (3 - exponent))
    else:
        target_int = mantissa << (8 * (exponent - 3))

    # Convert to 32-byte tensor (big-endian)
    target_bytes = target_int.to_bytes(32, byteorder='big', signed=False)
    return torch.tensor(list(target_bytes), dtype=torch.uint8)

def _has_pow(metadata) -> bool:
    if metadata.extra_args is None:
        return False
    return any( (i in metadata.extra_args and metadata.extra_args[i].get("pow"))
                for i in range(len(metadata.output_token_ids)) )

def to_python_string(input_data):
    """
    Converts a Python dict or JSON string into a plain Python literal string.
    
    :param input_data: A dict/list or a JSON-formatted string.
    :return: A string containing the Python literal representation.
    """
    # If it's a string, try parsing as JSON
    if isinstance(input_data, str):
        try:
            obj = json.loads(input_data)
        except json.JSONDecodeError:
            # Not valid JSON; return the original string
            return input_data
    else:
        obj = input_data
    
    # Use pprint to get a nicely formatted Python literal string
    return pformat(obj)
