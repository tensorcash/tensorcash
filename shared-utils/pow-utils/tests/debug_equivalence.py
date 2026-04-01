#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
import sys
import os
import numpy as np
from pow_utils import ProofWriter
import proof_processor

# Create identical test data
np.random.seed(42)
window_size = 256
archive_len = 100

# Mock tensor class for Python
class MockTensor:
    def __init__(self, data):
        self.data = np.array(data)
    def cpu(self):
        return self
    def numpy(self):
        return self.data
    def tolist(self):
        return self.data.tolist()
    def tobytes(self):
        return self.data.tobytes()
    def __getitem__(self, idx):
        return MockTensor(self.data[idx])

# Shared test data
tokens = np.arange(window_size, dtype=np.int32)
topk_shape = (window_size, 50)
topk_logits = np.random.randn(*topk_shape).astype(np.float32)
topk_indices = np.random.randint(0, 50000, topk_shape, dtype=np.int32)
digest_bytes = np.random.bytes(32)

# Python implementation
writer = ProofWriter()
window_data_tensors = {
    "tokens": MockTensor(tokens),
    "probs": MockTensor(np.ones(window_size, dtype=np.float32) * 0.5),
    "topk_logits": MockTensor(topk_logits),
    "topk_indices": MockTensor(topk_indices),
    "attention_mask": MockTensor(np.ones(window_size, dtype=bool)),
    "sampling_u": MockTensor(np.random.rand(window_size).astype(np.float32)),
    "softmax_normalizers": MockTensor(np.ones(window_size, dtype=np.float32)),
    "logsumexp_stats": MockTensor(np.random.randn(window_size, 2).astype(np.float32))
}

archive = list(range(archive_len))
padmask = [False] * archive_len

seq_info = {
    "prompt_tokens": [],  # Since archive < window, prompt_tokens should be empty
    "pad_mask": [],
    "temperature": 0.8,
    "top_p": 0.95,
    "top_k": 40,
    "repetition_penalty": 1.1,
    "model_identifier": "test-model",
    "compute_precision": "fp16",
    "extra_flags": "test_flags"
}

pow_params = {
    "tick": 42,
    "target": "ff" * 32,
    "vdf": "aa" * 32,
    "block_hash": "bb" * 32,
    "header_prefix": "cc" * 32,
    "ipfs_cid": "QmTest123"
}

digest_tensor = MockTensor([np.frombuffer(digest_bytes, dtype=np.uint8)])

proof_bytes_python, proof_dict_python = writer.write_proof(
    seq_id=12345,
    step_num=window_size,
    window_data=window_data_tensors,
    digest=digest_tensor,
    is_solution=True,
    pow_params=pow_params,
    seq_info=seq_info,
    completion_id="cmpl-test123"
)

print(f"Python proof size: {len(proof_bytes_python)} bytes")

# C++ implementation
processor = proof_processor.ProofProcessor(proxy_audit_enabled=False)

# IMPORTANT: Use the same random seed for sampling_u and logsumexp_stats
np.random.seed(42)
# Skip the randn calls that were used for topk
_ = np.random.randn(*topk_shape)
_ = np.random.randint(0, 50000, topk_shape)

window_data_numpy = {
    "tokens": tokens,
    "probs": np.ones(window_size, dtype=np.float32) * 0.5,
    "topk_logits": topk_logits,
    "topk_indices": topk_indices,
    "attention_mask": np.ones(window_size, dtype=bool),
    "sampling_u": np.random.rand(window_size).astype(np.float32),  # Same seed as Python
    "softmax_normalizers": np.ones(window_size, dtype=np.float32),
    "logsumexp_stats": np.random.randn(window_size, 2).astype(np.float32)  # Same seed as Python
}

cache_data = {
    "archive_list": archive,
    "pad_mask_list": padmask
}

pow_hasher_data = {
    "tick": 42,
    "target": bytes([0xFF] * 32),
    "vdf": bytes([0xAA] * 32),
    "block_hash": bytes([0xBB] * 32),
    "header_prefix": bytes([0xCC] * 32),
    "ipfs_cid": "QmTest123",
    "request_id": 99999,
    "difficulty": 1000000,
    "window_size": window_size
}

seq_params = {
    "temperature": 0.8,
    "top_p": 0.95,
    "top_k": 40,
    "repetition_penalty": 1.1,
    "model_identifier": "test-model",
    "compute_precision": "fp16",
    "extra_flags": "test_flags"
}

digest_array = np.frombuffer(digest_bytes, dtype=np.uint8)
result_cpp = processor.process_proof(
    seq_id=12345,
    step_num=window_size,
    cache_data=cache_data,
    window_data=window_data_numpy,
    digest=digest_array,
    is_solution=True,
    pow_hasher_data=pow_hasher_data,
    seq_params=seq_params,
    completion_id="cmpl-test123"
)

proof_bytes_cpp = bytes(result_cpp["proof_bytes"])
print(f"C++ proof size: {len(proof_bytes_cpp)} bytes")

if proof_bytes_python == proof_bytes_cpp:
    print("✅ Proofs match!")
else:
    print(f"❌ Proofs differ")
    # Find first difference
    for i in range(min(len(proof_bytes_python), len(proof_bytes_cpp))):
        if proof_bytes_python[i] != proof_bytes_cpp[i]:
            print(f"First difference at byte {i}: Python={proof_bytes_python[i]:02x}, C++={proof_bytes_cpp[i]:02x}")
            print(f"Context around byte {i}:")
            start = max(0, i-10)
            end = min(len(proof_bytes_python), i+10)
            print(f"  Python: {proof_bytes_python[start:end].hex()}")
            print(f"  C++:    {proof_bytes_cpp[start:end].hex()}")
            break