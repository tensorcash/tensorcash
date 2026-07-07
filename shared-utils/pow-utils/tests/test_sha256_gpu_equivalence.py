"""Byte-equivalence tests for sha256_many across all foreseeable hashing domains.

Authority is hashlib.sha256 (which is byte-identical to the C++/OpenSSL
`sha256_many` in pow_utils.cpp; cross-language parity is covered separately by
compare_cpp_python.py). The pure-torch vectorized kernel in
manual-tests/test_sha256_ragged.py is used only as an optional third comparison.

The test matrix is exhaustive over the only length-dependent branch in SHA-256
(padding): every length boundary class, several byte-content classes, a range
of batch shapes (incl. non-power-of-two and B==0), single + double SHA, and the
real `_build_msg` output. On a CUDA box with Triton available this validates the
GPU kernel; on CPU it validates the hashlib fallback and the wrapper's API
contract (contiguity, empty batch, dtype/shape).

Run on a GPU host (e.g. the H100 worker) to exercise the Triton path:
    pytest -v tests/test_sha256_gpu_equivalence.py
Force the CPU fallback even on GPU:
    POW_SHA256_DISABLE_TRITON=1 pytest -v tests/test_sha256_gpu_equivalence.py
"""

import hashlib
import os
import sys

import numpy as np
import pytest
import torch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pow_utils import (  # noqa: E402
    sha256_many,
    _sha256_use_triton,
    _build_msg,
    hex_to_bytes_tensor,
    _tok_le_bytes,
    _u32le,
    _str_bytes,
)

# Optional third oracle: the pure-torch vectorized reference.
try:
    _mt = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "manual-tests")
    sys.path.insert(0, _mt)
    from test_sha256_ragged import _sha256_torch_ragged  # noqa: E402
    HAVE_TORCH_REF = True
except Exception:
    HAVE_TORCH_REF = False


DEVICES = ["cpu"] + (["cuda"] if torch.cuda.is_available() else [])

# Length classes that exercise every SHA-256 padding boundary, plus multi-block.
# 55/56 = single->double block; 64 = exact block; 119/120 = 2->3 blocks; etc.
BOUNDARY_LENS = [0, 1, 2, 3, 31, 32, 54, 55, 56, 57, 63, 64, 65, 66,
                 119, 120, 127, 128, 129, 191, 192, 255, 256]
LARGE_LENS = [1000, 4096, 8192]

# Independent known-answer vectors from FIPS 180-2 / NIST SHAVS — hardcoded
# digests, NOT computed from hashlib, so they catch a bug that happened to be
# shared between the kernel and our hashlib oracle. (msg_bytes, expected_hex)
NIST_KAT = [
    (b"", "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"),
    (b"abc", "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"),
    # FIPS 180-2 App. B.2 — 448-bit (56-byte) single-block-after-pad message
    (b"abcdbcdecdefdefgefghfghighijhijkijkljklmklmnlmnomnopnopq",
     "248d6a61d20638b8e5c026930c3e6039a33ce45964ff2167f6ecedd419db06c1"),
    # FIPS 180-2 App. B.3 — 896-bit (112-byte) two-block message
    (b"abcdefghbcdefghicdefghijdefghijkefghijklfghijklmghijklmn"
     b"hijklmnoijklmnopjklmnopqklmnopqrlmnopqrsmnopqrstnopqrstu",
     "cf5b16a778af8380036ce59e7b0492370b249b11e8f07a51afac45037afee9d1"),
    # Classic NIST long-message KAT: one million 'a'
    (b"a" * 1_000_000,
     "cdc76e5c9914fb9281a1c7e284d73e67f1809a48a497200e046d39ccc7112cd0"),
]


def _hashlib_rows(arr: np.ndarray) -> np.ndarray:
    """Ground-truth (B,32) digests via hashlib, row by row over true L bytes."""
    out = np.empty((arr.shape[0], 32), dtype=np.uint8)
    for i in range(arr.shape[0]):
        out[i] = np.frombuffer(hashlib.sha256(arr[i].tobytes()).digest(),
                               dtype=np.uint8)
    return out


def _assert_matches_hashlib(msg: torch.Tensor):
    """sha256_many(msg) must equal hashlib byte-for-byte on every row."""
    got = sha256_many(msg)
    assert got.shape == (msg.shape[0], 32)
    assert got.dtype == torch.uint8
    assert got.device == msg.device
    got_np = got.cpu().numpy()
    exp = _hashlib_rows(msg.detach().cpu().contiguous().numpy())
    assert np.array_equal(got_np, exp), (
        f"SHA-256 mismatch on device={msg.device} shape={tuple(msg.shape)}; "
        f"first bad row {int(np.argmax(np.any(got_np != exp, axis=1)))}"
    )
    return got


def _rand_batch(B, L, device, kind="random", seed=0):
    g = torch.Generator().manual_seed(seed)
    if kind == "random":
        t = torch.randint(0, 256, (B, L), generator=g, dtype=torch.uint8)
    elif kind == "zeros":
        t = torch.zeros((B, L), dtype=torch.uint8)
    elif kind == "ones":
        t = torch.full((B, L), 0xFF, dtype=torch.uint8)
    elif kind == "pad80":
        # 0x80-saturated: collides with the SHA padding marker byte.
        t = torch.full((B, L), 0x80, dtype=torch.uint8)
    elif kind == "ramp":
        t = (torch.arange(L, dtype=torch.int64) % 256).to(torch.uint8)
        t = t.unsqueeze(0).expand(B, -1).contiguous()
    else:
        raise ValueError(kind)
    return t.to(device)


# --------------------------------------------------------------------------- #
# Known-answer vectors
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("device", DEVICES)
def test_known_vectors(device):
    cases = {
        b"": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        b"abc": "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
        b"a" * 55: hashlib.sha256(b"a" * 55).hexdigest(),
        b"a" * 56: hashlib.sha256(b"a" * 56).hexdigest(),
        b"a" * 64: hashlib.sha256(b"a" * 64).hexdigest(),
        bytes(range(256)): hashlib.sha256(bytes(range(256))).hexdigest(),
    }
    for raw, expect in cases.items():
        msg = torch.tensor(list(raw), dtype=torch.uint8, device=device).reshape(1, -1)
        got = sha256_many(msg)
        assert bytes(got[0].cpu().numpy()).hex() == expect, f"len={len(raw)}"


# --------------------------------------------------------------------------- #
# Exhaustive length-boundary sweep (the core guarantee)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("device", DEVICES)
@pytest.mark.parametrize("L", BOUNDARY_LENS + LARGE_LENS)
def test_length_sweep(device, L):
    msg = _rand_batch(B=17, L=L, device=device, kind="random", seed=L)
    _assert_matches_hashlib(msg)


# --------------------------------------------------------------------------- #
# Byte-content classes (stress padding-byte collisions, all-0/all-1)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("device", DEVICES)
@pytest.mark.parametrize("kind", ["zeros", "ones", "pad80", "ramp"])
@pytest.mark.parametrize("L", [0, 1, 55, 56, 64, 120, 200])
def test_content_classes(device, kind, L):
    msg = _rand_batch(B=8, L=L, device=device, kind=kind, seed=L)
    _assert_matches_hashlib(msg)


# --------------------------------------------------------------------------- #
# Batch shapes: single, non-power-of-two, large (>= one full ROWS tile)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("device", DEVICES)
@pytest.mark.parametrize("B", [1, 2, 7, 63, 64, 65, 257, 4096])
def test_batch_shapes(device, B):
    msg = _rand_batch(B=B, L=80, device=device, kind="random", seed=B)
    _assert_matches_hashlib(msg)


# --------------------------------------------------------------------------- #
# B == 0 (empty batch) — easy place for a GPU wrapper to diverge
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("device", DEVICES)
@pytest.mark.parametrize("L", [0, 80])
def test_empty_batch(device, L):
    msg = torch.empty((0, L), dtype=torch.uint8, device=device)
    got = sha256_many(msg)
    assert got.shape == (0, 32)
    assert got.dtype == torch.uint8
    assert got.device == msg.device


# --------------------------------------------------------------------------- #
# Non-contiguous input — wrapper must .contiguous() (or reject), not corrupt
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("device", DEVICES)
def test_noncontiguous_input(device):
    # Build a wider buffer and take a strided (non-contiguous) view.
    wide = _rand_batch(B=12, L=160, device=device, kind="random", seed=99)
    view = wide[:, ::2]                      # (12, 80), non-contiguous
    assert not view.is_contiguous()
    _assert_matches_hashlib(view)

    # Row-sliced (also non-contiguous along batch) view.
    rows = wide[::3]                         # (4, 160)
    assert not rows.is_contiguous()
    _assert_matches_hashlib(rows)


# --------------------------------------------------------------------------- #
# Double SHA-256 (header path: 80 -> 32 -> 32) matches hashlib(hashlib(.))
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("device", DEVICES)
@pytest.mark.parametrize("L", [32, 80])
def test_double_sha(device, L):
    msg = _rand_batch(B=11, L=L, device=device, kind="random", seed=L + 7)
    once = sha256_many(msg)
    twice = sha256_many(once)
    arr = msg.detach().cpu().contiguous().numpy()
    exp = np.empty((msg.shape[0], 32), dtype=np.uint8)
    for i in range(arr.shape[0]):
        d = hashlib.sha256(hashlib.sha256(arr[i].tobytes()).digest()).digest()
        exp[i] = np.frombuffer(d, dtype=np.uint8)
    assert np.array_equal(twice.cpu().numpy(), exp)


# --------------------------------------------------------------------------- #
# Real message shape via _build_msg (the actual sampler domain)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("device", DEVICES)
@pytest.mark.parametrize("n_ctx", [0, 1, 5, 64])
def test_build_msg_domain(device, n_ctx):
    B = 6
    header = hex_to_bytes_tensor("ab" * 76, device=device)
    v = hex_to_bytes_tensor("cd" * 32, device=device)
    T8 = torch.zeros(8, dtype=torch.uint8, device=device)
    j4 = _u32le(torch.arange(B, dtype=torch.int64, device=device).view(-1, 1))
    ctx = torch.randint(0, 50000, (B, n_ctx), dtype=torch.int64, device=device)
    ctx_bytes = _tok_le_bytes(ctx)
    pb = _str_bytes("fp16", batch_size=B, device=device)
    msg = _build_msg(header, v, T8, j4, ctx_bytes, pb)
    _assert_matches_hashlib(msg)


# --------------------------------------------------------------------------- #
# Determinism: identical inputs -> identical bytes, repeated
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("device", DEVICES)
def test_determinism(device):
    msg = _rand_batch(B=64, L=137, device=device, kind="random", seed=3)
    a = sha256_many(msg)
    b = sha256_many(msg)
    assert torch.equal(a, b)


# --------------------------------------------------------------------------- #
# Fuzz: random (B, L, bytes) differential vs hashlib
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("device", DEVICES)
def test_fuzz_differential(device):
    rng = np.random.RandomState(1234)
    for _ in range(40):
        B = int(rng.randint(1, 130))
        L = int(rng.randint(0, 300))
        if L == 0:
            msg = torch.empty((B, 0), dtype=torch.uint8, device=device)
        else:
            host = torch.from_numpy(
                rng.randint(0, 256, size=(B, L)).astype(np.uint8))
            msg = host.to(device)
        _assert_matches_hashlib(msg)


# --------------------------------------------------------------------------- #
# GPU-vs-CPU parity: the Triton path must equal the hashlib fallback exactly
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(not torch.cuda.is_available(), reason="needs CUDA")
def test_gpu_cpu_parity():
    if not _sha256_use_triton():
        pytest.skip("Triton path disabled in this runtime")
    for L in BOUNDARY_LENS + [1000]:
        host = _rand_batch(B=33, L=L, device="cpu", kind="random", seed=L + 1)
        gpu = sha256_many(host.to("cuda")).cpu()
        cpu = sha256_many(host)
        assert torch.equal(gpu, cpu), f"GPU/CPU mismatch at L={L}"


# --------------------------------------------------------------------------- #
# Optional third oracle: pure-torch vectorized reference agrees with hashlib
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(not HAVE_TORCH_REF, reason="pure-torch reference unavailable")
@pytest.mark.parametrize("device", DEVICES)
@pytest.mark.parametrize("L", [0, 55, 56, 64, 120, 200])
def test_pure_torch_reference(device, L):
    msg = _rand_batch(B=9, L=L, device=device, kind="random", seed=L + 5)
    lengths = torch.full((msg.shape[0],), L, dtype=torch.int64, device=device)
    ref = _sha256_torch_ragged(msg, lengths).cpu().numpy()
    exp = _hashlib_rows(msg.cpu().numpy())
    assert np.array_equal(ref, exp)


# --------------------------------------------------------------------------- #
# Independent NIST / FIPS 180-2 known-answer vectors (hardcoded digests)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("device", DEVICES)
@pytest.mark.parametrize("idx", range(len(NIST_KAT)))
def test_nist_kat(device, idx):
    raw, expect_hex = NIST_KAT[idx]
    expect = bytes.fromhex(expect_hex)
    # Self-consistency guard: a wrong hardcoded vector fails here (our typo),
    # not as a kernel mismatch below.
    assert hashlib.sha256(raw).digest() == expect, "bad hardcoded KAT vector"
    msg = torch.tensor(list(raw), dtype=torch.uint8, device=device).reshape(1, -1)
    got = sha256_many(msg)
    assert bytes(got[0].cpu().numpy()) == expect, f"KAT len={len(raw)}"


# --------------------------------------------------------------------------- #
# EXHAUSTIVE length sweep: every length 0..512 (no gaps left to fuzz)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("device", DEVICES)
def test_exhaustive_length_sweep(device):
    # Two independent content seeds per length so a length isn't judged on a
    # single random draw. Internal loop (not parametrized) to keep item count low.
    for seed in (0, 1):
        for L in range(0, 513):
            msg = _rand_batch(B=3, L=L, device=device, kind="random",
                              seed=seed * 10007 + L)
            got = sha256_many(msg).cpu().numpy()
            exp = _hashlib_rows(msg.detach().cpu().contiguous().numpy())
            assert np.array_equal(got, exp), f"len={L} seed={seed}"


# --------------------------------------------------------------------------- #
# Padding boundaries at HIGH block counts (length field lands in a later block)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("device", DEVICES)
@pytest.mark.parametrize("blocks", [4, 8, 16, 64, 128])
def test_high_block_boundary_pairs(device, blocks):
    base = blocks * 64
    # -1/0/+1 around an exact block multiple, and the 55/56/57 pad transition
    # (55 = last data byte still fits with 0x80+len; 56 forces an extra block).
    for d in (-1, 0, 1, 55, 56, 57):
        L = base + d
        if L < 0:
            continue
        msg = _rand_batch(B=5, L=L, device=device, kind="random", seed=L)
        _assert_matches_hashlib(msg)


# --------------------------------------------------------------------------- #
# Batch-position invariance: a fixed message hashes identically whether alone
# or embedded at any index of a large mixed batch (no grid/lane contamination)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("device", DEVICES)
@pytest.mark.parametrize("L", [1, 64, 137, 256])
def test_batch_position_invariance(device, L):
    g = torch.Generator().manual_seed(777)
    fixed = torch.randint(0, 256, (1, L), generator=g, dtype=torch.uint8).to(device)
    standalone = sha256_many(fixed)[0].cpu()

    B = 4096
    batch = torch.randint(0, 256, (B, L), generator=g, dtype=torch.uint8).to(device)
    idxs = [0, 1, 63, 64, 65, 1000, 4031, B - 1]
    for i in idxs:
        batch[i] = fixed[0]
    out = sha256_many(batch).cpu()
    for i in idxs:
        assert torch.equal(out[i], standalone), f"row {i} diverged (L={L})"


# --------------------------------------------------------------------------- #
# Exotic strides: transposed view and storage-offset slice (wrapper must
# .contiguous() and never corrupt). Covers cases beyond simple ::2 slicing.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("device", DEVICES)
def test_exotic_strides(device):
    g = torch.Generator().manual_seed(2024)
    # transposed: build (L, B) then .t() -> (B, L) non-contiguous, col-major
    L, B = 100, 24
    colmajor = torch.randint(0, 256, (L, B), generator=g, dtype=torch.uint8).to(device)
    tview = colmajor.t()
    assert not tview.is_contiguous()
    _assert_matches_hashlib(tview)

    # storage-offset slice: a window out of a wider buffer (non-zero offset)
    wide = torch.randint(0, 256, (16, 200), generator=g, dtype=torch.uint8).to(device)
    off = wide[:, 37:37 + 80]
    assert off.storage_offset() != 0
    _assert_matches_hashlib(off)


# --------------------------------------------------------------------------- #
# NIST SHAVS-style Monte-Carlo feedback chain: state = sha(state||state||state)
# iterated; exercises the 96-byte (2-block) message + state feedback repeatedly.
# Kernel-driven chain must equal hashlib-driven chain at every step.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("device", DEVICES)
def test_monte_carlo_feedback_chain(device):
    seed = bytes(range(32))
    # reference chain via hashlib
    ref = seed
    refs = []
    for _ in range(300):
        ref = hashlib.sha256(ref + ref + ref).digest()
        refs.append(ref)
    # kernel chain
    state = torch.tensor(list(seed), dtype=torch.uint8, device=device)
    for i in range(300):
        msg = torch.cat([state, state, state]).reshape(1, 96)
        state = sha256_many(msg)[0]
        assert bytes(state.cpu().numpy()) == refs[i], f"MC diverged at step {i}"


# --------------------------------------------------------------------------- #
# API contract: wrong dtype / wrong ndim must be rejected, not silently hashed
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("device", DEVICES)
def test_api_negative(device):
    with pytest.raises(AssertionError):
        sha256_many(torch.zeros((2, 3), dtype=torch.int32, device=device))
    with pytest.raises(AssertionError):
        sha256_many(torch.zeros((5,), dtype=torch.uint8, device=device))
    with pytest.raises(AssertionError):
        sha256_many(torch.zeros((2, 3, 4), dtype=torch.uint8, device=device))


# --------------------------------------------------------------------------- #
# 64-bit length field high word: a message > 2^32 bits (>512 MiB) forces the
# upper length bytes non-zero. GPU-only and memory-gated; the whole point is to
# exercise the kernel's high-word path, which production never reaches but which
# would otherwise be completely untested.
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(not torch.cuda.is_available(), reason="needs CUDA")
def test_length_field_high_word():
    if not _sha256_use_triton():
        pytest.skip("Triton path disabled")
    free, _total = torch.cuda.mem_get_info()
    L = 600_000_000  # 6.0e8 bytes -> 4.8e9 bits > 2^32 (4.29e9); high word = 1
    if free < int(2.8e9):
        pytest.skip(f"insufficient GPU memory ({free/1e9:.1f} GB free)")
    base = torch.arange(256, dtype=torch.uint8, device="cuda")
    msg = base.repeat((L + 255) // 256)[:L].reshape(1, L)
    got = sha256_many(msg)[0].cpu().numpy()
    exp = np.frombuffer(hashlib.sha256(msg.cpu().numpy().tobytes()).digest(),
                        dtype=np.uint8)
    assert np.array_equal(got, exp), "high-word length-field mismatch"
    del msg
    torch.cuda.empty_cache()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
