#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""
Test functional equivalence by deserializing and comparing fields.
FlatBuffers may not produce byte-identical output but should have
identical content when deserialized.
"""

import os
import sys
import numpy as np
import flatbuffers

# Add paths for imports
test_dir = os.path.dirname(os.path.abspath(__file__))
build_dir = os.path.join(test_dir, "build")
generated_python_dir = os.path.join(build_dir, "generated-python")

sys.path.insert(0, os.path.dirname(test_dir))  # For pow_utils
sys.path.insert(0, build_dir)  # For proof_processor.so
if os.path.exists(generated_python_dir):
    sys.path.insert(0, generated_python_dir)  # For FlatBuffer Python modules

from pow_utils import ProofWriter
import proof_processor
import proof.Proof as ProofModule
import proof.FloatArray as FloatArrayModule
import proof.UIntArray as UIntArrayModule

def deserialize_proof(proof_bytes):
    """Deserialize a FlatBuffer proof and extract all fields"""
    buf = bytearray(proof_bytes)
    proof = ProofModule.Proof.GetRootAs(buf, 0)
    
    # Extract all fields
    fields = {
        'version': proof.Version(),
        'tick': proof.Tick(),
        'timestamp': proof.Timestamp(),
        'is_solution': proof.IsSolution(),
        'model_identifier': proof.ModelIdentifier().decode() if proof.ModelIdentifier() else "",
        'compute_precision': proof.ComputePrecision().decode() if proof.ComputePrecision() else "",
        'ipfs_cid': proof.IpfsCid().decode() if proof.IpfsCid() else "",
        'extra_flags': proof.ExtraFlags().decode() if proof.ExtraFlags() else None,  # Python returns None
        'temperature': proof.Temperature(),
        'top_p': proof.TopP(),
        'top_k': proof.TopK(),
        'repetition_penalty': proof.RepetitionPenalty(),
    }
    
    # Extract byte arrays
    fields['target'] = bytes([proof.Target(i) for i in range(proof.TargetLength())])
    fields['vdf'] = bytes([proof.Vdf(i) for i in range(proof.VdfLength())])
    fields['hash'] = bytes([proof.Hash(i) for i in range(proof.HashLength())])
    fields['block_hash'] = bytes([proof.BlockHash(i) for i in range(proof.BlockHashLength())])
    fields['header_prefix'] = bytes([proof.HeaderPrefix(i) for i in range(proof.HeaderPrefixLength())])
    
    # Extract 1D arrays
    fields['chosen_tokens'] = [proof.ChosenTokens(i) for i in range(proof.ChosenTokensLength())]
    fields['chosen_probs'] = [proof.ChosenProbs(i) for i in range(proof.ChosenProbsLength())]
    fields['sampling_u'] = [proof.SamplingU(i) for i in range(proof.SamplingULength())]
    fields['softmax_normalizers'] = [proof.SoftmaxNormalizers(i) for i in range(proof.SoftmaxNormalizersLength())]
    fields['prompt_tokens'] = [proof.PromptTokens(i) for i in range(proof.PromptTokensLength())]
    fields['pad_mask'] = [bool(proof.PadMask(i)) for i in range(proof.PadMaskLength())]
    
    # Extract 2D arrays (topk_logits)
    topk_logits = []
    for i in range(proof.TopkLogitsLength()):
        row = proof.TopkLogits(i)
        topk_logits.append([row.Values(j) for j in range(row.ValuesLength())])
    fields['topk_logits'] = topk_logits
    
    # Extract 2D arrays (topk_indices)
    topk_indices = []
    for i in range(proof.TopkIndicesLength()):
        row = proof.TopkIndices(i)
        topk_indices.append([row.Values(j) for j in range(row.ValuesLength())])
    fields['topk_indices'] = topk_indices
    
    # Extract 2D arrays (logsumexp_stats)
    logsumexp_stats = []
    for i in range(proof.LogsumexpStatsLength()):
        row = proof.LogsumexpStats(i)
        logsumexp_stats.append([row.Values(j) for j in range(row.ValuesLength())])
    fields['logsumexp_stats'] = logsumexp_stats
    
    return fields

def compare_fields(fields1, fields2):
    """Compare two deserialized proofs field by field"""
    differences = []
    
    for key in fields1:
        if key not in fields2:
            differences.append(f"Field '{key}' missing in second proof")
            continue
            
        val1 = fields1[key]
        val2 = fields2[key]
        
        # Compare based on type
        if isinstance(val1, (int, bool, str, bytes)):
            if val1 != val2:
                differences.append(f"Field '{key}': {val1} != {val2}")
        elif isinstance(val1, float):
            if abs(val1 - val2) > 1e-6:
                differences.append(f"Field '{key}': {val1} != {val2}")
        elif isinstance(val1, list):
            if len(val1) != len(val2):
                differences.append(f"Field '{key}' length: {len(val1)} != {len(val2)}")
            else:
                # Check if it's a 2D array
                if val1 and isinstance(val1[0], list):
                    for i, (row1, row2) in enumerate(zip(val1, val2)):
                        if len(row1) != len(row2):
                            differences.append(f"Field '{key}' row {i} length: {len(row1)} != {len(row2)}")
                        else:
                            for j, (elem1, elem2) in enumerate(zip(row1, row2)):
                                if isinstance(elem1, float):
                                    if abs(elem1 - elem2) > 1e-6:
                                        differences.append(f"Field '{key}' [{i}][{j}]: {elem1} != {elem2}")
                                        if len(differences) > 10:  # Limit output
                                            differences.append("... (truncated)")
                                            return differences
                                elif elem1 != elem2:
                                    differences.append(f"Field '{key}' [{i}][{j}]: {elem1} != {elem2}")
                                    if len(differences) > 10:
                                        differences.append("... (truncated)")
                                        return differences
                else:
                    # 1D array
                    for i, (elem1, elem2) in enumerate(zip(val1, val2)):
                        if isinstance(elem1, float):
                            if abs(elem1 - elem2) > 1e-6:
                                differences.append(f"Field '{key}' [{i}]: {elem1} != {elem2}")
                                if len(differences) > 10:
                                    differences.append("... (truncated)")
                                    return differences
                        elif elem1 != elem2:
                            differences.append(f"Field '{key}' [{i}]: {elem1} != {elem2}")
                            if len(differences) > 10:
                                differences.append("... (truncated)")
                                return differences
    
    return differences

def test_equivalence():
    """Test functional equivalence between Python and C++ proof processors"""
    
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
    
    # Create test data
    np.random.seed(42)
    window_size = 256
    archive_len = 100
    
    tokens = np.arange(window_size, dtype=np.int32)
    topk_shape = (window_size, 50)
    topk_logits = np.random.randn(*topk_shape).astype(np.float32)
    topk_indices = np.random.randint(0, 50000, topk_shape, dtype=np.int32)
    digest_bytes = np.random.bytes(32)
    
    # Reset seed for consistent random data
    np.random.seed(42)
    _ = np.random.randn(*topk_shape)  # Skip for topk_logits
    _ = np.random.randint(0, 50000, topk_shape)  # Skip for topk_indices
    sampling_u = np.random.rand(window_size).astype(np.float32)
    logsumexp_stats = np.random.randn(window_size, 2).astype(np.float32)
    
    # Python implementation
    print("Generating Python proof...")
    import tempfile
    with tempfile.TemporaryDirectory() as temp_dir:
        writer = ProofWriter(output_dir=temp_dir)
    
    window_data_tensors = {
        "tokens": MockTensor(tokens),
        "probs": MockTensor(np.ones(window_size, dtype=np.float32) * 0.5),
        "topk_logits": MockTensor(topk_logits),
        "topk_indices": MockTensor(topk_indices),
        "attention_mask": MockTensor(np.ones(window_size, dtype=bool)),
        "sampling_u": MockTensor(sampling_u),
        "softmax_normalizers": MockTensor(np.ones(window_size, dtype=np.float32)),
        "logsumexp_stats": MockTensor(logsumexp_stats)
    }
    
    archive = list(range(archive_len))
    padmask = [False] * archive_len
    
    seq_info = {
        "prompt_tokens": [],  # Since archive < window
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
    
    proof_bytes_python, _ = writer.write_proof(
        seq_id=12345,
        step_num=window_size,
        window_data=window_data_tensors,
        digest=digest_tensor,
        is_solution=True,
        pow_params=pow_params,
        seq_info=seq_info,
        completion_id="cmpl-test123"
    )
    
    print(f"Python proof: {len(proof_bytes_python)} bytes")
    
    # C++ implementation
    print("Generating C++ proof...")
    processor = proof_processor.ProofProcessor(proxy_audit_enabled=False)
    
    window_data_numpy = {
        "tokens": tokens,
        "probs": np.ones(window_size, dtype=np.float32) * 0.5,
        "topk_logits": topk_logits,
        "topk_indices": topk_indices,
        "attention_mask": np.ones(window_size, dtype=bool),
        "sampling_u": sampling_u,
        "softmax_normalizers": np.ones(window_size, dtype=np.float32),
        "logsumexp_stats": logsumexp_stats
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
    print(f"C++ proof: {len(proof_bytes_cpp)} bytes")
    
    # Deserialize and compare
    print("\nDeserializing and comparing fields...")
    fields_python = deserialize_proof(proof_bytes_python)
    fields_cpp = deserialize_proof(proof_bytes_cpp)
    
    differences = compare_fields(fields_python, fields_cpp)
    
    if not differences:
        print("✅ All fields match! Proofs are functionally equivalent.")
        return True
    else:
        print("❌ Field differences found:")
        for diff in differences:
            print(f"  - {diff}")
        return False

if __name__ == "__main__":
    success = test_equivalence()
    sys.exit(0 if success else 1)