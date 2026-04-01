# SPDX-License-Identifier: Apache-2.0
"""
Model verification with quantitative audit and difficulty validation.

ModelAuditor:   GPU-based quantitative analysis — FLOPs counting, saliency,
                validity tests, file-size sanity.
ModelVerifier:  Facade that runs the audit and validates the claimed difficulty
                against the measured FLOPs using an inverse compute scalar.

Difficulty semantics (bcore consensus):
    adj_target <= base_target * normalizer / difficulty

    Higher registered difficulty → smaller adj_target → harder hash PoW.
    More FLOPs per token → LOWER registered difficulty → easier hash PoW
    (compensates for harder inference).

    Formula:
        expected_difficulty = normalizer * genesis_fpt / model_fpt
"""

import gc
import glob
import json
import logging
import os
import time
import traceback
from collections import defaultdict
from typing import Any, Dict, Optional, Tuple

import numpy as np

logger = logging.getLogger(__name__)

EPS = 1e-9

# Lazy-loaded heavy modules — deferred to avoid import failures in test mode
# where torch is stubbed without torch.nn. Loaded on first ModelAuditor use.
torch = None
nn = None

def _ensure_torch():
    """Import torch and torch.nn on first use."""
    global torch, nn
    if torch is None:
        import torch as _torch
        import torch.nn as _nn
        torch = _torch
        nn = _nn

# ---------------------------------------------------------------------------
# Configuration from environment
# ---------------------------------------------------------------------------

# Chain-keyed genesis baselines: JSON map of "model_id" -> flops_per_token.
# Example: {"Qwen/Qwen3-8B@abc123": 1.5e9, "testModel@testModelCommit": 1.0}
GENESIS_BASELINES: Dict[str, float] = json.loads(
    os.getenv("GENESIS_BASELINES", "{}")
)
# Single-value fallback for simple deployments
GENESIS_FLOPS_PER_TOKEN: float = float(os.getenv("GENESIS_FLOPS_PER_TOKEN", "0"))
# Active chain genesis model identifier (e.g. "Qwen/Qwen3-8B@commit")
ACTIVE_CHAIN_GENESIS_MODEL: str = os.getenv("ACTIVE_CHAIN_GENESIS_MODEL", "")

MODEL_DIFFICULTY_NORMALIZER: int = int(os.getenv("MODEL_DIFFICULTY_NORMALIZER", "1000000"))
DIFFICULTY_TOLERANCE: float = float(os.getenv("DIFFICULTY_TOLERANCE", "0.05"))

def _get_precision_scale():
    """Build precision scale mapping after torch is loaded."""
    _ensure_torch()
    return {
        torch.float32: 1.0,
        torch.float16: 0.5,
        torch.bfloat16: 0.5,
        torch.int8: 0.25,
        getattr(torch, "float8_e4m3fn", torch.float16): 0.125,
    }


# ---------------------------------------------------------------------------
# Baseline prose for validity_scores — anything coherent works; these are
# synthesised passages chosen only to look like natural English across a
# range of tokenisers, so `permutation_perplexity_ratio` actually measures
# the model's sensitivity to token order rather than random-input noise.
# ---------------------------------------------------------------------------

_BASELINE_TEXTS = [
    "The quick brown fox jumps over the lazy dog each morning before the sun has finished climbing over the tall hills that border the meadow.",
    "In the narrow streets of the old town, merchants opened their shutters one by one, calling greetings across the square as the church bell tolled the hour.",
    "She opened the letter carefully, reading each line twice, because the handwriting was faint and she did not want to miss a single word of what he had written.",
    "Professors in the university argued for hours about the correct interpretation of the ancient text, citing translations and footnotes until the library finally closed.",
    "The long train pulled into the station at dusk, its windows glowing warmly against the cold platform where a handful of passengers waited with their coats drawn tight.",
    "After the storm passed, children ran down to the shore to collect shells and driftwood, marvelling at how the tide had rearranged the beach overnight while they slept.",
    "He spent the afternoon in the garden, pruning the rose bushes and turning the compost, pausing only to drink water and to listen to the distant song of the blackbirds.",
    "Across the valley, the old mill stood silent against the rising moon, its wheel rusted and still, the stream beneath it whispering softly through the tangled summer reeds.",
]


# ---------------------------------------------------------------------------
# ModelAuditor — quantitative analysis engine
# ---------------------------------------------------------------------------

class ModelAuditor:
    """
    Run a comprehensive quantitative audit of a causal language model.

    Produces a JSON-serialisable report with:
      - FLOPs (total, per-token, active ratio)
      - Salient weight count via occlusion
      - Validity tests (input sensitivity, perturbation, permutation)
      - File-size sanity check
    """

    def __init__(
        self,
        model_name_or_path: str,
        revision: Optional[str] = None,
        device: Optional[str] = None,
        dtype=None,
        trust_remote_code: bool = True,
    ):
        _ensure_torch()
        from transformers import AutoConfig, AutoModelForCausalLM, AutoTokenizer

        self._precision_scale = _get_precision_scale()
        self.device = device or ("cuda" if torch.cuda.is_available() else "cpu")
        self.revision = revision
        self.config = AutoConfig.from_pretrained(
            model_name_or_path,
            revision=revision,
            trust_remote_code=trust_remote_code,
        )
        if dtype is None:
            dtype = torch.float16 if torch.cuda.is_available() else torch.float32
        self.model = AutoModelForCausalLM.from_pretrained(
            model_name_or_path,
            revision=revision,
            torch_dtype=dtype,
            trust_remote_code=trust_remote_code,
            low_cpu_mem_usage=True,
            device_map="auto" if torch.cuda.is_available() else None,
        ).eval()
        self.tokenizer = AutoTokenizer.from_pretrained(
            model_name_or_path, trust_remote_code=trust_remote_code
        )
        self.vocab_size = self.config.vocab_size
        self.model_name = model_name_or_path

    def _input_device(self):
        """
        Return the device where token indices must live.
        For HF causal LMs this is the embedding weight device.
        """
        try:
            emb = self.model.get_input_embeddings()
            if emb is not None and hasattr(emb, "weight"):
                return emb.weight.device
        except Exception:
            pass
        return torch.device(self.device)

    # ------------------------------------------------------------------ #
    # 1. FLOPs counting via forward hooks (dtype & sparsity aware)
    # ------------------------------------------------------------------ #

    def _count_flops(self, seq_len: int) -> Dict[str, Any]:
        stats: Dict[nn.Module, Dict] = defaultdict(
            lambda: {
                "calls": 0, "tokens": 0, "active_tokens": 0,
                "flops": 0.0, "dtype_scale": 1.0, "sparsity": 1.0,
            }
        )
        hooks = []

        def _linear_hook(module, inp, out):
            x = self._first_tensor(inp)
            if x is None or x.dim() < 3:
                return
            b, t, i = x.shape
            o = out.shape[-1] if torch.is_tensor(out) else out[0].shape[-1]
            s = stats[module]
            s["calls"] += 1
            s["tokens"] += b * t
            act = (out.abs() > 1e-9).any(-1).float().sum().item()
            s["active_tokens"] += act
            s["dtype_scale"] = self._precision_scale.get(module.weight.dtype, 1.0)
            s["sparsity"] = (module.weight == 0).float().mean().item()
            s["flops"] += 2.0 * b * t * i * o * s["dtype_scale"] * (1 - s["sparsity"])

        def _attention_hook(module, inp, out):
            x = self._first_tensor(inp)
            if x is None or x.dim() < 3:
                return
            b, t, d = x.shape
            h = getattr(module, "num_heads", getattr(module, "num_attention_heads", 1))
            dh = getattr(module, "head_dim", d // h if d % h == 0 else d)
            s = stats[module]
            s["calls"] += 1
            s["tokens"] += b * t
            if torch.is_tensor(out):
                act = (out.abs() > 1e-9).any(-1).float().sum().item()
                s["active_tokens"] += act
            s["dtype_scale"] = self._precision_scale.get(x.dtype, 1.0)
            # QK^T + softmax + PV
            s["flops"] += (
                2 * b * h * t * t * dh + b * h * t * t + 2 * b * h * t * t * dh
            ) * s["dtype_scale"]

        for module in self.model.modules():
            if hasattr(module, "weight") and hasattr(module, "in_features"):
                hooks.append(module.register_forward_hook(_linear_hook))
            elif module.__class__.__name__.lower().endswith("attention"):
                hooks.append(module.register_forward_hook(_attention_hook))

        input_ids = torch.randint(
            0, self.vocab_size, (1, seq_len), device=self._input_device()
        )
        with torch.no_grad():
            self.model(input_ids)

        for h in hooks:
            h.remove()

        total_flops = sum(s["flops"] for s in stats.values())
        tokens_total = sum(s["tokens"] for s in stats.values())
        active_ratio = (
            sum(s["active_tokens"] for s in stats.values()) / (tokens_total + EPS)
        )

        return {
            "total_flops": total_flops,
            "flops_per_token": total_flops / seq_len,
            "active_ratio": active_ratio,
        }

    @staticmethod
    def _first_tensor(x):
        if torch.is_tensor(x):
            return x
        if isinstance(x, (tuple, list)):
            for y in x:
                t = ModelAuditor._first_tensor(y)
                if t is not None:
                    return t
        if isinstance(x, dict):
            for y in x.values():
                t = ModelAuditor._first_tensor(y)
                if t is not None:
                    return t
        return None

    # ------------------------------------------------------------------ #
    # 2. Saliency via occlusion (gradient-free, chunked rows)
    # ------------------------------------------------------------------ #

    def salient_weights_occlusion(
        self,
        seq_len: int = 128,
        passes: int = 32,
        chunk_rows: int = 1024,
        delta_thresh: float = 0.05,
    ) -> Tuple[int, int]:
        self.model.eval()
        ctxs = torch.randint(
            0, self.vocab_size, (passes, seq_len), device=self._input_device()
        )

        base_logits = []
        for i in range(passes):
            base_logits.append(self.model(ctxs[i : i + 1]).logits[:, -1, :])
        base_logits = torch.cat(base_logits, dim=0)
        base_top = torch.topk(base_logits, 50, dim=-1)
        base_probs = torch.softmax(base_top.values, dim=-1)

        salient_count = 0
        total_params = 0

        for name, param in self.model.named_parameters():
            if param.dim() != 2:
                continue
            total_params += param.numel()
            rows = param.shape[0]
            orig = param.data.clone()

            for start in range(0, rows, chunk_rows):
                end = min(start + chunk_rows, rows)
                param.data = orig.clone()
                param.data[start:end] = 0.0

                new_logits = []
                for i in range(passes):
                    new_logits.append(self.model(ctxs[i : i + 1]).logits[:, -1, :])
                new_logits = torch.cat(new_logits, dim=0)
                new_top = torch.topk(new_logits, 50, dim=-1)
                new_probs = torch.softmax(new_top.values, dim=-1)

                shift = torch.mean(
                    torch.sum(torch.abs(base_probs - new_probs), dim=-1)
                ).item()
                if shift >= delta_thresh:
                    salient_count += param.data[start:end].numel()

                param.data = orig

            del orig
            gc.collect()

        return salient_count, total_params

    # ------------------------------------------------------------------ #
    # 3. Validity tests (input sensitivity, perturbation, permutation)
    # ------------------------------------------------------------------ #

    def _kl_div(self, a, b):
        with torch.no_grad():
            pa = torch.log_softmax(a, dim=-1)
            pb = torch.log_softmax(b, dim=-1)
            return torch.sum(torch.exp(pa) * (pa - pb), dim=-1)

    def _encode_fixed_length(self, text: str, ctx_len: int) -> "torch.Tensor":
        """Tokenize `text` and return a [1, ctx_len] LongTensor on self.device.

        If tokenization yields fewer than ctx_len tokens, the text is repeated
        until the buffer is long enough, then truncated. Special tokens are
        skipped so the input is pure content.
        """
        ids = self.tokenizer(text, add_special_tokens=False)["input_ids"]
        if not ids:
            raise ValueError("tokenizer produced no tokens for baseline text")
        while len(ids) < ctx_len:
            ids = ids + ids
        ids = ids[:ctx_len]
        return torch.tensor([ids], dtype=torch.long, device=self.device)

    def validity_scores(
        self,
        ctx_len: int = 128,
        pairs: int = 32,
        seed: int = 20260418,
    ) -> Dict[str, float]:
        """Three input-response statistics against a real-prose baseline.

        Historically used uniform-random token IDs for A and B, which made
        `permutation_perplexity_ratio` a coin flip around 1.0 on small LMs —
        an ordered run of gibberish isn't measurably less surprising than a
        shuffled run of gibberish. A fixed English-prose baseline gives the
        permutation metric real signal. RNGs are seeded for reproducibility
        so this is a pass-once, pass-always gate rather than a flaky one.
        """
        self.model.eval()

        gen_dev = torch.Generator(device=self.device).manual_seed(seed)
        rng = np.random.default_rng(seed)

        encoded = [self._encode_fixed_length(t, ctx_len) for t in _BASELINE_TEXTS]
        sens, pert, perp_ratios = [], [], []

        for i in range(pairs):
            A = encoded[i % len(encoded)]
            B = encoded[(i + 1) % len(encoded)]
            la = self.model(A).logits[:, -1, :]
            lb = self.model(B).logits[:, -1, :]
            sens.append(self._kl_div(la, lb).item())

            C = A.clone()
            pos = int(rng.integers(ctx_len))
            new_token = int(rng.integers(self.vocab_size))
            while new_token == C[0, pos].item():
                new_token = int(rng.integers(self.vocab_size))
            C[0, pos] = new_token
            lc = self.model(C).logits[:, -1, :]
            pert.append(self._kl_div(la, lc).item())

            perm = torch.randperm(ctx_len, generator=gen_dev, device=self.device)
            logits_perm = self.model(A[:, perm]).logits
            logits_orig = self.model(A).logits
            loss_orig = nn.CrossEntropyLoss()(
                logits_orig[:, :-1].reshape(-1, self.vocab_size),
                A[:, 1:].reshape(-1),
            )
            loss_perm = nn.CrossEntropyLoss()(
                logits_perm[:, :-1].reshape(-1, self.vocab_size),
                A[:, perm][:, 1:].reshape(-1),
            )
            perp_ratios.append(
                torch.exp(loss_perm).item() / (torch.exp(loss_orig).item() + EPS)
            )

        return {
            "input_sensitivity_kl": float(np.mean(sens)),
            "single_token_kl": float(np.mean(pert)),
            "permutation_perplexity_ratio": float(np.mean(perp_ratios)),
        }

    # ------------------------------------------------------------------ #
    # 4. File-size sanity
    # ------------------------------------------------------------------ #

    def file_size_check(
        self, salient_count: int, bits_per_weight: Optional[float] = None,
    ) -> Dict[str, Any]:
        in_memory = sum(p.numel() * p.element_size() for p in self.model.parameters())
        total_params = sum(p.numel() for p in self.model.parameters())
        if bits_per_weight is None:
            bits_per_weight = 8 * in_memory / total_params
        expected_bytes = salient_count * (bits_per_weight / 8.0)

        root = getattr(self.model, "_name_or_path", None)
        on_disk = None
        if root and os.path.isdir(root):
            files = []
            for pattern in ("*.bin", "*.safetensors", "*.pt", "*.pth"):
                files += glob.glob(os.path.join(root, "**", pattern), recursive=True)
            if files:
                on_disk = sum(os.path.getsize(f) for f in files)

        ratio = (on_disk or in_memory) / (expected_bytes + EPS)

        return {
            "in_memory_bytes": in_memory,
            "on_disk_bytes": on_disk,
            "total_params": total_params,
            "bits_per_weight": bits_per_weight,
            "expected_bytes_from_salient": expected_bytes,
            "ratio_disk_to_expected": ratio,
        }

    # ------------------------------------------------------------------ #
    # 5. Full audit
    # ------------------------------------------------------------------ #

    def run_audit(
        self,
        ctx_len: int = 128,
        saliency_passes: int = 16,
        saliency_chunk_rows: int = 1024,
        saliency_delta: float = 0.05,
    ) -> Dict[str, Any]:
        logger.info("Auditing %s (ctx_len=%d)", self.model_name, ctx_len)

        flops_info = self._count_flops(ctx_len)
        logger.info(
            "FLOPs: %.3f G total, %.3f M/token",
            flops_info["total_flops"] / 1e9,
            flops_info["flops_per_token"] / 1e6,
        )

        salient, total = self.salient_weights_occlusion(
            seq_len=ctx_len,
            passes=saliency_passes,
            chunk_rows=saliency_chunk_rows,
            delta_thresh=saliency_delta,
        )
        logger.info(
            "Salient/total: %s / %s (%.2f%%)",
            f"{salient:,}", f"{total:,}", 100 * salient / total if total else 0,
        )

        validity = self.validity_scores(ctx_len=ctx_len)
        logger.info("Validity: %s", validity)

        file_info = self.file_size_check(salient)

        return {
            "model_name": self.model_name,
            "context_length": ctx_len,
            "flops": {
                "total_flops": flops_info["total_flops"],
                "flops_per_token": flops_info["flops_per_token"],
                "active_ratio": flops_info["active_ratio"],
            },
            "salient_weights": {
                "count": salient,
                "total": total,
                "percentage": 100.0 * salient / total if total else 0.0,
            },
            "validity": validity,
            "file_size_check": file_info,
        }


# ---------------------------------------------------------------------------
# ModelVerifier — facade with difficulty validation
# ---------------------------------------------------------------------------

class ModelVerifier:
    """
    Wraps ModelAuditor to run quantitative analysis and validate the
    claimed difficulty scalar against measured FLOPs.

    The difficulty field in bcore is an inverse compute scalar:
        adj_target <= base_target * normalizer / difficulty

    So: expected_difficulty = normalizer * genesis_fpt / model_fpt

    Returns ("pending_operator_review", report) on success — the operator
    must approve before the node gets Model_OK.
    Returns ("pending_operator_review", failure_report) if the audit cannot
    run at all. The operator still decides the final outcome.
    """

    def __init__(self, device: Optional[str] = None):
        # Defer torch import — only needed when validate() runs the auditor.
        # _validate_difficulty() is a pure-math staticmethod and never needs torch.
        # Keep None by default so ModelAuditor can auto-select CUDA when available.
        self.device = device

    def validate(
        self,
        raw_message: bytes,
        claimed_difficulty: int = 0,
        model_name: str = "",
        model_commit: str = "",
    ) -> Tuple[str, Dict[str, Any]]:
        """
        Run audit and difficulty validation.

        Returns:
            (status, report) where status is always "pending_operator_review"
        """
        if not model_name:
            return (
                "pending_operator_review",
                self._build_failure_report(
                    reason="empty_model_name",
                    error_message="Empty model_name in validation request",
                    model_name=model_name,
                    model_commit=model_commit,
                    claimed_difficulty=claimed_difficulty,
                    stage="precheck",
                ),
            )

        t0 = time.monotonic()
        try:
            auditor = ModelAuditor(
                model_name,
                revision=model_commit or None,
                device=self.device,
            )
            report = auditor.run_audit(
                ctx_len=128,
                saliency_passes=16,
                saliency_chunk_rows=1024,
                saliency_delta=0.05,
            )
        except Exception as e:
            logger.error("ModelAuditor failed for %s: %s", model_name, e, exc_info=True)
            return (
                "pending_operator_review",
                self._build_failure_report(
                    reason="audit_exception",
                    error_message=str(e),
                    model_name=model_name,
                    model_commit=model_commit,
                    claimed_difficulty=claimed_difficulty,
                    stage="audit_run",
                    traceback_text=traceback.format_exc(),
                ),
            )

        elapsed_ms = int((time.monotonic() - t0) * 1000)
        report["audit_elapsed_ms"] = elapsed_ms
        report["model_commit"] = model_commit

        # Difficulty validation
        measured_fpt = report["flops"]["flops_per_token"]
        genesis_fpt = self._resolve_genesis_fpt()
        difficulty_result = self._validate_difficulty(
            claimed_difficulty, measured_fpt, genesis_fpt,
        )
        report["difficulty_validation"] = difficulty_result

        logger.info(
            "Model %s audit complete in %dms — difficulty verdict: %s "
            "(claimed=%d, expected=%s, ratio=%.4f)",
            model_name,
            elapsed_ms,
            difficulty_result["verdict"],
            claimed_difficulty,
            difficulty_result.get("expected_difficulty", "N/A"),
            difficulty_result.get("ratio", 0),
        )

        return ("pending_operator_review", report)

    @staticmethod
    def _build_failure_report(
        *,
        reason: str,
        error_message: str,
        model_name: str,
        model_commit: str,
        claimed_difficulty: int,
        stage: str,
        traceback_text: str = "",
    ) -> Dict[str, Any]:
        return {
            "audit_completed": False,
            "requires_operator_decision": True,
            "failure_reason": reason,
            "failure_stage": stage,
            "error": error_message,
            "error_type": reason,
            "traceback": traceback_text,
            "model_name": model_name,
            "model_commit": model_commit,
            "claimed_difficulty": claimed_difficulty,
            "audit_timestamp": int(time.time()),
        }

    @staticmethod
    def _resolve_genesis_fpt() -> float:
        """Look up the genesis model FLOPs/token from chain-keyed baselines or fallback."""
        if ACTIVE_CHAIN_GENESIS_MODEL and ACTIVE_CHAIN_GENESIS_MODEL in GENESIS_BASELINES:
            return GENESIS_BASELINES[ACTIVE_CHAIN_GENESIS_MODEL]
        if GENESIS_FLOPS_PER_TOKEN > 0:
            return GENESIS_FLOPS_PER_TOKEN
        return 0.0

    @staticmethod
    def _validate_difficulty(
        claimed_difficulty: int,
        measured_fpt: float,
        genesis_fpt: float,
    ) -> Dict[str, Any]:
        """
        Validate claimed difficulty against measured FLOPs using the
        inverse compute scalar formula:

            expected_difficulty = normalizer * genesis_fpt / model_fpt

        Returns a dict with expected, claimed, ratio, tolerance check, and verdict.
        """
        if genesis_fpt <= 0:
            return {
                "verdict": "skip",
                "reason": "genesis baseline not configured",
                "claimed_difficulty": claimed_difficulty,
                "measured_flops_per_token": measured_fpt,
            }

        if measured_fpt <= 0:
            return {
                "verdict": "fail_zero_flops",
                "reason": "measured FLOPs/token is zero or negative",
                "claimed_difficulty": claimed_difficulty,
                "measured_flops_per_token": measured_fpt,
                "genesis_flops_per_token": genesis_fpt,
            }

        expected = MODEL_DIFFICULTY_NORMALIZER * genesis_fpt / measured_fpt
        ratio = claimed_difficulty / expected if expected > 0 else float("inf")
        # Use small epsilon to avoid float boundary failures (e.g., 0.050000000000000044)
        within_tolerance = abs(ratio - 1.0) <= DIFFICULTY_TOLERANCE + 1e-9

        return {
            "expected_difficulty": int(round(expected)),
            "claimed_difficulty": claimed_difficulty,
            "ratio": round(ratio, 6),
            "within_tolerance": within_tolerance,
            "tolerance": DIFFICULTY_TOLERANCE,
            "normalizer": MODEL_DIFFICULTY_NORMALIZER,
            "genesis_flops_per_token": genesis_fpt,
            "measured_flops_per_token": measured_fpt,
            "verdict": "pass" if within_tolerance else "fail_difficulty_mismatch",
        }
