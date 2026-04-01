# SPDX-License-Identifier: Apache-2.0
import torch
import hashlib
import numpy as np
import time

# ======================================================================
# Core: Vectorized SHA-256 for ragged batches (internal use)
# ======================================================================
def _sha256_torch_ragged(messages: torch.ByteTensor,
                         lengths: torch.Tensor) -> torch.ByteTensor:
    """
    SHA-256 for ragged batches in PyTorch (GPU/CPU).
    messages: (B, Lmax) uint8 tensor; bytes past lengths[i] are ignored.
    lengths:  (B,) int tensor with per-row true lengths in bytes.
    Returns:  (B, 32) uint8 tensor (big-endian digests).
    """
    assert messages.dtype == torch.uint8 and messages.ndim == 2
    assert lengths.ndim == 1 and lengths.shape[0] == messages.shape[0]
    device = messages.device
    B, Lmax = messages.shape
    lengths = lengths.to(torch.int64).to(device)

    MASK = torch.tensor(0xFFFFFFFF, dtype=torch.int64, device=device)

    # SHA-256 round constants
    K = torch.tensor([
        0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
        0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
        0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
        0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
        0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
        0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
        0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
        0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2
    ], dtype=torch.int64, device=device)

    def rotr(x: torch.Tensor, n: int) -> torch.Tensor:
        return ((x >> n) | ((x << (32 - n)) & MASK)) & MASK

    # Initial hash state H for each row
    H = torch.tensor([
        0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a,
        0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19
    ], dtype=torch.int64, device=device).unsqueeze(0).expand(B, 8).clone()

    # Per-row block counts and bit lengths
    total_blocks = ((lengths + 9 + 63) // 64)  # >= 1
    max_blocks = int(total_blocks.max().item())
    bitlen = (lengths * 8).to(torch.int64)

    # Build the 64 bytes of a block for subset rows (active_idx) and block id b
    def make_block_bytes(active_idx: torch.Tensor, b: int) -> torch.Tensor:
        Li = lengths[active_idx]             # [B']
        Ti = total_blocks[active_idx]        # [B']
        last8_start = Ti * 64 - 8            # [B']

        idx = torch.arange(64, device=device, dtype=torch.int64)  # [64]
        idx = idx.unsqueeze(0).expand(active_idx.numel(), 64) + (b * 64)

        in_msg  = (idx < Li.unsqueeze(1))
        at_0x80 = (idx == Li.unsqueeze(1))
        in_tail = (idx >= last8_start.unsqueeze(1)) & (idx < (Ti * 64).unsqueeze(1))

        if Lmax > 0:
            idx_clamped = idx.clamp_min(0).clamp_max(max(Lmax - 1, 0))
            gather_src = messages[active_idx]  # (B', Lmax)
            msg_bytes = torch.gather(gather_src.to(torch.int64), 1, idx_clamped)
            msg_bytes = (msg_bytes & 0xFF).to(torch.uint8)
        else:
            msg_bytes = torch.zeros_like(idx, dtype=torch.uint8, device=device)

        out = torch.zeros_like(idx, dtype=torch.uint8, device=device)
        out = torch.where(in_msg, msg_bytes, out)
        out = torch.where(at_0x80, torch.tensor(0x80, dtype=torch.uint8, device=device), out)

        # big-endian 64-bit bit length in the last 8 bytes
        pos_in_tail = (idx - last8_start.unsqueeze(1)).clamp_min(0)
        shift = (7 - pos_in_tail) * 8
        len_byte = ((bitlen[active_idx].unsqueeze(1) >> shift) & 0xFF).to(torch.uint8)
        out = torch.where(in_tail, len_byte, out)
        return out  # (B', 64) uint8

    # Process blocks
    for b in range(max_blocks):
        active_mask = (b < total_blocks)
        if not torch.any(active_mask):
            continue
        active_idx = torch.nonzero(active_mask, as_tuple=False).squeeze(1)

        block_bytes = make_block_bytes(active_idx, b)  # (B', 64) uint8
        w = block_bytes.reshape(-1, 16, 4).to(torch.int64)
        W = torch.zeros((w.shape[0], 64), dtype=torch.int64, device=device)
        W[:, :16] = (((w[:, :, 0] << 24) | (w[:, :, 1] << 16) |
                      (w[:, :, 2] << 8) | w[:, :, 3]) & MASK)

        # Extend schedule
        for t in range(16, 64):
            s0 = (rotr(W[:, t-15], 7) ^ rotr(W[:, t-15], 18) ^ (W[:, t-15] >> 3)) & MASK
            s1 = (rotr(W[:, t-2], 17) ^ rotr(W[:, t-2], 19) ^ (W[:, t-2] >> 10)) & MASK
            W[:, t] = (W[:, t-16] + s0 + W[:, t-7] + s1) & MASK

        # Working vars
        a, b_, c, d, e, f, g, h_ = [H[active_idx, i].clone() for i in range(8)]

        # 64 rounds (vectorized across active rows)
        for t in range(64):
            S1 = (rotr(e, 6) ^ rotr(e, 11) ^ rotr(e, 25)) & MASK
            ch = (e & f) ^ (((~e) & MASK) & g)
            temp1 = (h_ + S1 + ch + K[t] + W[:, t]) & MASK

            S0 = (rotr(a, 2) ^ rotr(a, 13) ^ rotr(a, 22)) & MASK
            maj = ((a & b_) ^ (a & c) ^ (b_ & c)) & MASK
            temp2 = (S0 + maj) & MASK

            h_ = g
            g  = f
            f  = e
            e  = (d + temp1) & MASK
            d  = c
            c  = b_
            b_ = a
            a  = (temp1 + temp2) & MASK

        # Accumulate
        H[active_idx, 0] = (H[active_idx, 0] + a) & MASK
        H[active_idx, 1] = (H[active_idx, 1] + b_) & MASK
        H[active_idx, 2] = (H[active_idx, 2] + c) & MASK
        H[active_idx, 3] = (H[active_idx, 3] + d) & MASK
        H[active_idx, 4] = (H[active_idx, 4] + e) & MASK
        H[active_idx, 5] = (H[active_idx, 5] + f) & MASK
        H[active_idx, 6] = (H[active_idx, 6] + g) & MASK
        H[active_idx, 7] = (H[active_idx, 7] + h_) & MASK

    # Serialize big-endian
    out = torch.empty((B, 32), dtype=torch.uint8, device=device)
    for i in range(8):
        word = H[:, i] & MASK
        out[:, 4*i + 0] = ((word >> 24) & 0xFF).to(torch.uint8)
        out[:, 4*i + 1] = ((word >> 16) & 0xFF).to(torch.uint8)
        out[:, 4*i + 2] = ((word >>  8) & 0xFF).to(torch.uint8)
        out[:, 4*i + 3] = ( word        & 0xFF).to(torch.uint8)
    return out


# ======================================================================
# Public API: integrates ragged support into your sha256_many
# ======================================================================
def sha256_many(msg: torch.ByteTensor, lengths: torch.Tensor | None = None) -> torch.ByteTensor:
    """Compute SHA-256 hash for multiple messages in batch.

    Args:
        msg: (B, L) tensor of byte data (uint8). If messages are ragged,
             pass 'lengths' to indicate the true byte length of each row.
        lengths: Optional (B,) int tensor. If provided, bytes after lengths[i]
                 are ignored and SHA-256 padding is applied internally per row.

    Returns:
        (B, 32) tensor of SHA-256 hashes (uint8, big-endian)
    """
    assert msg.dtype == torch.uint8 and msg.ndim == 2
    B, L = msg.shape

    # If no lengths are given, treat all rows as length L (fast path)
    if lengths is None:
        lengths = torch.full((B,), L, dtype=torch.int64, device=msg.device)

    # GPU / CPU vectorized path
    if msg.is_cuda:
        return _sha256_torch_ragged(msg, lengths)

    # CPU fallback (hashlib) honoring lengths
    out = torch.empty((B, 32), dtype=torch.uint8, device=msg.device)
    msg_cpu = msg.cpu()
    lengths_cpu = lengths.cpu().tolist()
    for i in range(B):
        n = int(lengths_cpu[i])
        d = hashlib.sha256(bytes(msg_cpu[i, :n].tolist())).digest()
        out[i] = torch.tensor(list(d), dtype=torch.uint8, device=msg.device)
    return out


# ======================================================================
# Tests: unit tests vs hashlib (equal-length & ragged)
# ======================================================================
def _stack_bytes(seq):
    """Stack a list of bytes objects into (B, Lmax) uint8 and lengths."""
    if len(seq) == 0:
        return torch.zeros((0, 0), dtype=torch.uint8), torch.zeros((0,), dtype=torch.int64)
    Ls = [len(s) for s in seq]
    Lmax = max(Ls)
    X = torch.zeros((len(seq), Lmax), dtype=torch.uint8)
    for i, s in enumerate(seq):
        if Ls[i] > 0:
            X[i, :Ls[i]] = torch.tensor(list(s), dtype=torch.uint8)
    return X, torch.tensor(Ls, dtype=torch.int64)

def test_sha256_many():
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

    # ---- single / known vectors (ragged) ----
    cases = [
        b"", b"a", b"abc",
        b"abcdbcdecdefdefgefghfghighijhijkijkljklmklmnlmnomnopnopq",
        b"a"*55, b"a"*56, b"a"*64, b"a"*100,
        b"The quick brown fox jumps over the lazy dog",
        bytes(range(0, 200)),
    ]
    X, L = _stack_bytes(cases)
    X = X.to(device); L = L.to(device)
    got = sha256_many(X, L).cpu().numpy()
    for i, m in enumerate(cases):
        exp = np.frombuffer(hashlib.sha256(m).digest(), dtype=np.uint8)
        assert np.array_equal(got[i], exp), f"Mismatch at {i} (len={len(m)})"

    # ---- equal-length fast path (no lengths passed) ----
    batch = [b"msg%03d" % i for i in range(128)]
    Lfix = max(len(bi) for bi in batch)
    Y = torch.zeros((len(batch), Lfix), dtype=torch.uint8, device=device)
    for i, bi in enumerate(batch):
        Y[i, :len(bi)] = torch.tensor(list(bi), dtype=torch.uint8, device=device)
    # Here all true lengths == Lfix, so no ambiguity
    got2 = sha256_many(Y).cpu().numpy()
    for i, bi in enumerate(batch):
        exp = np.frombuffer(hashlib.sha256(bi.ljust(Lfix, b"\x00")).digest(), dtype=np.uint8)
        # NOTE: Without 'lengths', the function hashes the full L bytes.
        # For equal-length path, if you padded with zeros externally, hashlib must see the same zeros.
        assert np.array_equal(got2[i], exp), f"Equal-length mismatch at {i}"

    print("All unit tests passed.")


# ======================================================================
# Benchmark: compare GPU vectorized vs CPU hashlib
# ======================================================================
def benchmark():
    print("\nBenchmarking...")
    B = 4096
    # Ragged lengths between 0 and 256 bytes
    lengths = torch.randint(0, 257, (B,), dtype=torch.int64)
    Lmax = int(lengths.max().item())
    host = torch.randint(0, 256, (B, Lmax if Lmax > 0 else 1), dtype=torch.uint8)

    # CPU (hashlib, honoring lengths)
    t0 = time.time()
    out_cpu = sha256_many(host, lengths)  # falls back to CPU
    t1 = time.time()
    print(f"CPU hashlib (B={B}, Lmax={Lmax}): {(t1 - t0)*1000:.2f} ms")

    # GPU vectorized (if available)
    if torch.cuda.is_available():
        dev = torch.device("cuda")
        X = host.to(dev); L = lengths.to(dev)
        # Warmup
        for _ in range(10):
            _ = sha256_many(X, L)
        torch.cuda.synchronize()
        t2 = time.time()
        for _ in range(50):
            _ = sha256_many(X, L)
        torch.cuda.synchronize()
        t3 = time.time()
        print(f"GPU vectorized (B={B}, Lmax={Lmax}): {((t3 - t2)/50)*1000:.2f} ms per run")
    else:
        print("CUDA not available; GPU benchmark skipped.")


if __name__ == "__main__":
    test_sha256_many()
    benchmark()
