# SPDX-License-Identifier: Apache-2.0
import torch
import hashlib
import struct
import time

def sha256_torch(messages: torch.ByteTensor) -> torch.ByteTensor:
    """
    Complete SHA-256 implementation in PyTorch that runs on GPU.
    Input: (batch_size, message_length) tensor of uint8
    Output: (batch_size, 32) tensor of uint8
    """
    device = messages.device
    batch_size, msg_len = messages.shape

    # SHA-256 constants (int64 is fine; we mask to 32 bits after ops)
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

    # Padding
    msg_bits = msg_len * 8
    if msg_len % 64 <= 55:
        padded_len = (msg_len // 64 + 1) * 64
    else:
        padded_len = (msg_len // 64 + 2) * 64

    padded = torch.zeros((batch_size, padded_len), dtype=torch.uint8, device=device)
    if msg_len > 0:
        padded[:, :msg_len] = messages
    padded[:, msg_len] = 0x80
    for i in range(8):
        padded[:, padded_len - 8 + i] = (msg_bits >> (8 * (7 - i))) & 0xFF

    outputs = torch.zeros((batch_size, 32), dtype=torch.uint8, device=device)

    for bi in range(batch_size):  # <-- batch index renamed
        # Initial hash values
        h = torch.tensor([
            0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a,
            0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19
        ], dtype=torch.int64, device=device)

        # Process 512-bit chunks
        for chunk_start in range(0, padded_len, 64):
            w = torch.zeros(64, dtype=torch.int64, device=device)
            chunk = padded[bi, chunk_start:chunk_start + 64].to(torch.int64)

            for i in range(16):
                w[i] = ((chunk[i*4] << 24) | (chunk[i*4+1] << 16) |
                        (chunk[i*4+2] << 8) | chunk[i*4+3]) & 0xFFFFFFFF

            for i in range(16, 64):
                s0 = ((torch.bitwise_right_shift(w[i-15], 7) | (w[i-15] << 25)) ^
                      (torch.bitwise_right_shift(w[i-15], 18) | (w[i-15] << 14)) ^
                      torch.bitwise_right_shift(w[i-15], 3)) & 0xFFFFFFFF
                s1 = ((torch.bitwise_right_shift(w[i-2], 17) | (w[i-2] << 15)) ^
                      (torch.bitwise_right_shift(w[i-2], 19) | (w[i-2] << 13)) ^
                      torch.bitwise_right_shift(w[i-2], 10)) & 0xFFFFFFFF
                w[i] = (w[i-16] + s0 + w[i-7] + s1) & 0xFFFFFFFF

            # Working vars; avoid name 'b'
            a, b_, c, d, e, f, g, h_val = h[0], h[1], h[2], h[3], h[4], h[5], h[6], h[7]

            for i in range(64):
                S1 = ((torch.bitwise_right_shift(e, 6) | (e << 26)) ^
                      (torch.bitwise_right_shift(e, 11) | (e << 21)) ^
                      (torch.bitwise_right_shift(e, 25) | (e << 7))) & 0xFFFFFFFF
                ch = (e & f) ^ ((~e) & g)
                temp1 = (h_val + S1 + ch + K[i] + w[i]) & 0xFFFFFFFF
                S0 = ((torch.bitwise_right_shift(a, 2) | (a << 30)) ^
                      (torch.bitwise_right_shift(a, 13) | (a << 19)) ^
                      (torch.bitwise_right_shift(a, 22) | (a << 10))) & 0xFFFFFFFF
                maj = (a & c) ^ (a & b_) ^ (b_ & c)
                temp2 = (S0 + maj) & 0xFFFFFFFF

                h_val = g
                g = f
                f = e
                e = (d + temp1) & 0xFFFFFFFF
                d = c
                c = b_
                b_ = a
                a = (temp1 + temp2) & 0xFFFFFFFF

            h[0] = (h[0] + a) & 0xFFFFFFFF
            h[1] = (h[1] + b_) & 0xFFFFFFFF
            h[2] = (h[2] + c) & 0xFFFFFFFF
            h[3] = (h[3] + d) & 0xFFFFFFFF
            h[4] = (h[4] + e) & 0xFFFFFFFF
            h[5] = (h[5] + f) & 0xFFFFFFFF
            h[6] = (h[6] + g) & 0xFFFFFFFF
            h[7] = (h[7] + h_val) & 0xFFFFFFFF

        # Output (big-endian)
        for i in range(8):
            outputs[bi, i*4]     = (h[i] >> 24) & 0xFF
            outputs[bi, i*4 + 1] = (h[i] >> 16) & 0xFF
            outputs[bi, i*4 + 2] = (h[i] >> 8)  & 0xFF
            outputs[bi, i*4 + 3] =  h[i]        & 0xFF

    return outputs

# Test function to verify it works
def test_sha256_torch():
    """Test that our implementation matches hashlib exactly."""
    # Test cases
    test_messages = [
        b"",
        b"abc",
        b"abcdbcdecdefdefgefghfghighijhijkijkljklmklmnlmnomnopnopq",
        b"a" * 55,  # Just under padding boundary
        b"a" * 56,  # At padding boundary
        b"a" * 64,  # Exactly one block
        b"a" * 100, # Multiple blocks
    ]
    
    for msg in test_messages:
        # Convert to tensor
        if len(msg) == 0:
            msg_tensor = torch.zeros((1, 1), dtype=torch.uint8).cuda()[:, :0]  # Empty tensor
        else:
            msg_tensor = torch.tensor([list(msg)], dtype=torch.uint8).cuda()
        
        # Our implementation
        our_hash = sha256_torch(msg_tensor)[0].cpu().numpy()
        
        # Hashlib
        expected = hashlib.sha256(msg).digest()
        expected_array = np.array(list(expected), dtype=np.uint8)
        
        # Compare
        if not np.array_equal(our_hash, expected_array):
            print(f"FAIL for message: {msg}")
            print(f"Expected: {expected.hex()}")
            print(f"Got:      {bytes(our_hash).hex()}")
            return False
        else:
            print(f"PASS: {msg[:20]}{'...' if len(msg) > 20 else ''} -> {bytes(our_hash).hex()}")
    
    print("\nAll tests passed!")
    return True

# Drop-in replacement for your sha256_many
def sha256_many(msg: torch.ByteTensor) -> torch.ByteTensor:
    """Direct replacement for your current function."""
    if msg.is_cuda:
        return sha256_torch(msg)
    else:
        # CPU fallback using hashlib
        B = msg.size(0)
        out = torch.empty((B, 32), dtype=torch.uint8, device=msg.device)
        for i, row in enumerate(msg):
            d = hashlib.sha256(row.numpy().tobytes()).digest()
            out[i] = torch.tensor(list(d), dtype=torch.uint8)
        return out

# Run the test
if __name__ == "__main__":
    import numpy as np
    test_sha256_torch()
    
    # Benchmark
    print("\nBenchmarking...")
    msg = torch.randint(0, 256, (100, 128), dtype=torch.uint8).cuda()
    
    # Warmup
    for _ in range(10):
        _ = sha256_torch(msg)
    
    # torch.cuda.synchronize()
    start = time.time()
    for _ in range(100):
        _ = sha256_torch(msg)
    # torch.cuda.synchronize()
    end = time.time()
    
    print(f"GPU SHA256: {(end - start) / 100 * 1000:.2f} ms for 100 messages")