"""Byte-identity test for the fast topk-threshold top-k mask.

The production `apply_topk_topp_mask` uses a topk(50)-threshold mask for the
top_p == 1.0 case instead of the legacy full-vocab sort+scatter. This asserts the
two produce a BYTE-IDENTICAL masked tensor on CPU and (when available) CUDA, across
random logits, mixed per-row k, and ties straddling the k-th-largest threshold.
"""
import pytest
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import torch
from pow_utils import apply_topk_topp_mask

DEVICES = ["cpu"] + (["cuda"] if torch.cuda.is_available() else [])
V = 151936  # Qwen3 vocab


def _assert_byte_identical(logits, k, p, device):
    logits = logits.to(device)
    k = k.to(device)
    p = p.to(device) if p is not None else None
    legacy = apply_topk_topp_mask(logits, k, p, fast=False)
    fast = apply_topk_topp_mask(logits, k, p, fast=True)
    assert torch.equal(legacy, fast), f"fast mask != legacy mask on {device}"
    # surviving support must match exactly too
    assert torch.equal(torch.isfinite(legacy).sum(-1),
                       torch.isfinite(fast).sum(-1))


@pytest.mark.parametrize("device", DEVICES)
def test_random(device):
    torch.manual_seed(0)
    _assert_byte_identical(torch.randn(8, V) * 8,
                           torch.full((8,), 50, dtype=torch.long),
                           torch.ones(8), device)


@pytest.mark.parametrize("device", DEVICES)
def test_mixed_k(device):
    torch.manual_seed(1)
    k = torch.tensor([1, 5, 10, 25, 40, 49, 50, 50], dtype=torch.long)
    _assert_byte_identical(torch.randn(8, V) * 8, k, torch.ones(8), device)


@pytest.mark.parametrize("device", DEVICES)
def test_kth_boundary_ties(device):
    torch.manual_seed(2)
    x = torch.randn(4, V) * 8
    x[:, :49] = torch.linspace(40, 30, 49)            # ranks 1-49 distinct & highest
    x[:, 49:70] = 5.0                                 # tie straddling rank-50 threshold
    x[:, 70:] = torch.clamp(x[:, 70:], max=4.0)       # everything else strictly lower
    _assert_byte_identical(x, torch.full((4,), 50, dtype=torch.long),
                           torch.ones(4), device)


@pytest.mark.parametrize("device", DEVICES)
def test_p_none(device):
    torch.manual_seed(3)
    _assert_byte_identical(torch.randn(8, V) * 8,
                           torch.full((8,), 50, dtype=torch.long), None, device)


@pytest.mark.skipif(not torch.cuda.is_available(),
                    reason="CUDA not available — GPU byte-identity path not exercised")
def test_cuda_was_exercised():
    assert "cuda" in DEVICES
