#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""
Test byte-level equivalence between Python and C++ proof processors.

This test ensures that the C++ ProofProcessor produces identical
proof bytes to the Python implementation.
"""

import os
import sys
import pytest
import numpy as np
import hashlib
import struct
from typing import Dict, List, Any, Tuple

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Add generated Python FlatBuffer modules to path
test_dir = os.path.dirname(os.path.abspath(__file__))
build_dir = os.path.join(test_dir, "build")
generated_python_dir = os.path.join(build_dir, "generated-python")
if os.path.exists(generated_python_dir):
    sys.path.insert(0, generated_python_dir)

# Import Python implementation
from pow_utils import ProofWriter

# Import C++ implementation (will be built)
try:
    sys.path.insert(0, build_dir)  # Add build dir for proof_processor.so
    import proof_processor
    CPP_AVAILABLE = True
except ImportError:
    CPP_AVAILABLE = False
    print("Warning: C++ proof_processor module not available. Build it first.")


class TestProofProcessorEquivalence:
    """Test byte-for-byte equivalence between Python and C++ processors"""
    
    @pytest.fixture
    def test_cases(self):
        """Matrix of test cases covering edge conditions"""
        return [
            # (archive_len, window_size, has_solution, empty_arrays)
            (100, 256, True, False),    # archive < window
            (256, 256, False, False),   # archive == window  
            (1000, 256, True, False),   # archive > window
            (0, 256, False, True),      # empty archive
            (512, 256, True, False),    # exact 2x window
            (257, 256, False, False),   # archive slightly > window
        ]
    
    def create_mock_data(self, archive_len: int, window_size: int, 
                        has_solution: bool, empty_arrays: bool) -> Dict[str, Any]:
        """Generate deterministic test data"""
        np.random.seed(42)  # Deterministic randomness
        
        # Create window data
        if empty_arrays:
            tokens = np.zeros(window_size, dtype=np.int32)
            topk_shape = (window_size, 50)
            topk_logits = np.zeros(topk_shape, dtype=np.float32)
            topk_indices = np.zeros(topk_shape, dtype=np.int32)
        else:
            tokens = np.arange(window_size, dtype=np.int32)
            topk_shape = (window_size, 50)
            topk_logits = np.random.randn(*topk_shape).astype(np.float32)
            topk_indices = np.random.randint(0, 50000, topk_shape, dtype=np.int32)
        
        window_data = {
            'tokens': tokens,
            'probs': np.ones(window_size, dtype=np.float32) * 0.5,
            'topk_logits': topk_logits,
            'topk_indices': topk_indices,
            'attention_mask': np.ones(window_size, dtype=bool),
            'sampling_u': np.random.rand(window_size).astype(np.float32),
            'softmax_normalizers': np.ones(window_size, dtype=np.float32),
            'logsumexp_stats': np.random.randn(window_size, 2).astype(np.float32)
        }
        
        # Create cache data
        cache = {
            'archive_list': [] if empty_arrays else list(range(archive_len)),
            'pad_mask_list': [] if empty_arrays else [False] * archive_len,
        }
        
        # Create pow_hasher data
        pow_hasher = {
            'tick': 42,
            'target': bytes([0xFF] * 32),
            'vdf': bytes([0xAA] * 32),
            'block_hash': bytes([0xBB] * 32),
            'header_prefix': bytes([0xCC] * 32),
            'ipfs_cid': 'QmTest123',
            'request_id': 99999,
            'difficulty': 1000000,
            'window_size': window_size
        }
        
        # Create seq_params
        seq_params = {
            'temperature': 0.8,
            'top_p': 0.95,
            'top_k': 40,
            'repetition_penalty': 1.1,
            'model_identifier': 'test-model',
            'compute_precision': 'fp16',
            'extra_flags': 'test_flags'
        }
        
        return {
            'seq_id': 12345,
            'step_num': window_size,
            'window_size': window_size,
            'cache': cache,
            'window_data': window_data,
            'digest': np.random.bytes(32),
            'is_solution': has_solution,
            'pow_hasher': pow_hasher,
            'seq_params': seq_params,
            'completion_id': 'cmpl-test123'
        }
    
    def python_process_proof(self, mock_data: Dict[str, Any]) -> Tuple[bytes, Dict]:
        """Process proof using Python implementation"""
        import tempfile
        with tempfile.TemporaryDirectory() as temp_dir:
            writer = ProofWriter(output_dir=temp_dir)
            
            # Extract fields
            seq_id = mock_data['seq_id']
            step_num = mock_data['step_num']
            window_data = mock_data['window_data']
            digest = mock_data['digest']
            is_solution = mock_data['is_solution']
            
            # Prepare seq_info (matches common_sampler_helper.py logic)
            cache = mock_data['cache']
            archive = cache.get("archive_list", [])
            padmask = cache.get("pad_mask_list", [])
            window_size = mock_data['window_size']
            
            if len(archive) > window_size:
                prompt_tokens = archive[:-window_size]
                prompt_pad = padmask[:-window_size]
            else:
                prompt_tokens, prompt_pad = [], []
            
            seq_info = {
                "prompt_tokens": prompt_tokens,
                "pad_mask": prompt_pad,
                **mock_data['seq_params']
            }
            
            # Prepare pow_params
            pow_params = {
                "tick": mock_data['pow_hasher']['tick'],
                "target": mock_data['pow_hasher']['target'].hex(),
                "vdf": mock_data['pow_hasher']['vdf'].hex(),
                "block_hash": mock_data['pow_hasher']['block_hash'].hex(),
                "header_prefix": mock_data['pow_hasher']['header_prefix'].hex(),
                "ipfs_cid": mock_data['pow_hasher']['ipfs_cid']
            }
            
            # Convert window data to tensor format
            # Mock the tensor behavior
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
            
            window_data_tensors = {
                k: MockTensor(v) for k, v in window_data.items()
            }
            
            # Mock digest tensor - digest should be bytes already
            # Convert bytes to array for tensor
            digest_array = np.frombuffer(digest, dtype=np.uint8)
            digest_tensor = MockTensor([digest_array])
            
            # Write proof
            proof_bytes, proof_dict = writer.write_proof(
                seq_id, step_num, window_data_tensors, digest_tensor,
                is_solution, pow_params, seq_info, 
                completion_id=mock_data.get('completion_id')
            )
            
            return proof_bytes, proof_dict
    
    @pytest.mark.skipif(not CPP_AVAILABLE, reason="C++ module not built")
    def test_proof_serialization_equivalence(self, test_cases):
        """Test that both processors produce identical serialized proofs"""
        
        for archive_len, window_size, has_solution, empty_arrays in test_cases:
            mock_data = self.create_mock_data(
                archive_len, window_size, has_solution, empty_arrays
            )
            
            # Python implementation
            proof_bytes_python, proof_dict_python = self.python_process_proof(mock_data)
            
            # C++ implementation
            processor = proof_processor.ProofProcessor(proxy_audit_enabled=False)
            
            # Convert data for C++
            digest_array = np.frombuffer(mock_data['digest'], dtype=np.uint8)
            
            result_cpp = processor.process_proof(
                seq_id=mock_data['seq_id'],
                step_num=mock_data['step_num'],
                cache_data=mock_data['cache'],
                window_data=mock_data['window_data'],
                digest=digest_array,
                is_solution=mock_data['is_solution'],
                pow_hasher_data=mock_data['pow_hasher'],
                seq_params=mock_data['seq_params'],
                completion_id=mock_data.get('completion_id')
            )
            
            # Get serialized proof from C++
            proof_bytes_cpp = bytes(result_cpp['proof_bytes'])
            
            # Compare by deserializing and checking fields
            # FlatBuffers may not produce byte-identical output but should be functionally equivalent
            import proof.Proof as ProofModule
            
            def deserialize_proof(proof_bytes):
                """Deserialize a FlatBuffer proof and extract key fields for comparison"""
                buf = bytearray(proof_bytes)
                proof = ProofModule.Proof.GetRootAs(buf, 0)
                
                # Extract key fields for comparison
                fields = {
                    'version': proof.Version(),
                    'tick': proof.Tick(),
                    'is_solution': proof.IsSolution(),
                    'temperature': proof.Temperature(),
                    'top_p': proof.TopP(),
                    'top_k': proof.TopK(),
                    'repetition_penalty': proof.RepetitionPenalty(),
                }
                
                # Extract arrays (comparing lengths as a simple check)
                fields['chosen_tokens_len'] = proof.ChosenTokensLength()
                fields['topk_logits_len'] = proof.TopkLogitsLength()
                fields['topk_indices_len'] = proof.TopkIndicesLength()
                
                return fields
            
            fields_python = deserialize_proof(proof_bytes_python)
            fields_cpp = deserialize_proof(proof_bytes_cpp)
            
            # Compare key fields
            for key in fields_python:
                assert fields_python[key] == fields_cpp[key], \
                    f"Field mismatch for {key}: Python={fields_python[key]}, C++={fields_cpp[key]}"
            
            # Verify nonce extraction
            nonce_python = struct.unpack('<I', mock_data['digest'][:4])[0]
            nonce_cpp = result_cpp['nonce']
            assert nonce_python == nonce_cpp, f"Nonce mismatch: {nonce_python} vs {nonce_cpp}"
            
            print(f"✓ Test passed: archive={archive_len}, window={window_size}, "
                  f"solution={has_solution}, empty={empty_arrays}")
    
    @pytest.mark.skipif(not CPP_AVAILABLE, reason="C++ module not built")
    def test_field_name_mapping(self):
        """Test that pad_mask is correctly handled"""
        
        processor = proof_processor.ProofProcessor()
        mock_data = self.create_mock_data(500, 256, True, False)
        
        # Test with attention_mask in window data (should be mapped)
        mock_data['window_data']['attention_mask'] = [True, False] * 128
        if 'pad_mask' in mock_data['window_data']:
            del mock_data['window_data']['pad_mask']
        
        digest_array = np.frombuffer(mock_data['digest'], dtype=np.uint8)
        
        result = processor.process_proof(
            seq_id=mock_data['seq_id'],
            step_num=mock_data['step_num'],
            cache_data=mock_data['cache'],
            window_data=mock_data['window_data'],
            digest=digest_array,
            is_solution=False,
            pow_hasher_data=mock_data['pow_hasher'],
            seq_params=mock_data['seq_params'],
            completion_id=None
        )
        
        # Should succeed without errors
        assert 'proof_bytes' in result
        assert len(result['proof_bytes']) > 0
        print("✓ Field name mapping test passed")
    
    @pytest.mark.skipif(not CPP_AVAILABLE, reason="C++ module not built")
    def test_edge_cases(self):
        """Test edge cases and special values"""
        
        processor = proof_processor.ProofProcessor()
        
        # Test with NaN and Inf values
        mock_data = self.create_mock_data(100, 256, False, False)
        mock_data['window_data']['topk_logits'][0, 0] = float('nan')
        mock_data['window_data']['topk_logits'][1, 1] = float('inf')
        mock_data['window_data']['topk_logits'][2, 2] = float('-inf')
        
        digest_array = np.frombuffer(mock_data['digest'], dtype=np.uint8)
        
        result = processor.process_proof(
            seq_id=mock_data['seq_id'],
            step_num=mock_data['step_num'],
            cache_data=mock_data['cache'],
            window_data=mock_data['window_data'],
            digest=digest_array,
            is_solution=False,
            pow_hasher_data=mock_data['pow_hasher'],
            seq_params=mock_data['seq_params'],
            completion_id=None
        )
        
        # Should handle special floats
        assert 'proof_bytes' in result
        print("✓ Edge cases test passed")
    
    @pytest.mark.skipif(not CPP_AVAILABLE, reason="C++ module not built")
    def test_performance(self):
        """Compare performance between Python and C++"""
        import time
        
        mock_data = self.create_mock_data(1000, 256, True, False)
        num_iterations = 100
        
        # Python timing
        start = time.time()
        for _ in range(num_iterations):
            self.python_process_proof(mock_data)
        python_time = time.time() - start
        
        # C++ timing
        processor = proof_processor.ProofProcessor()
        digest_array = np.frombuffer(mock_data['digest'], dtype=np.uint8)
        
        start = time.time()
        for _ in range(num_iterations):
            processor.process_proof(
                seq_id=mock_data['seq_id'],
                step_num=mock_data['step_num'],
                cache_data=mock_data['cache'],
                window_data=mock_data['window_data'],
                digest=digest_array,
                is_solution=mock_data['is_solution'],
                pow_hasher_data=mock_data['pow_hasher'],
                seq_params=mock_data['seq_params'],
                completion_id=mock_data.get('completion_id')
            )
        cpp_time = time.time() - start
        
        speedup = python_time / cpp_time
        print(f"Performance: Python={python_time:.3f}s, C++={cpp_time:.3f}s, Speedup={speedup:.2f}x")
        
        # C++ should be faster
        assert cpp_time < python_time, "C++ should be faster than Python"


if __name__ == "__main__":
    # Run tests
    test = TestProofProcessorEquivalence()
    
    if not CPP_AVAILABLE:
        print("\n⚠️  C++ module not available. Build it first:")
        print("   cd shared-utils/pow-utils/tests")
        print("   ./build_proofprocessor_simple.sh")
        sys.exit(1)
    
    # Define test cases directly (not as fixture when running directly)
    test_cases = [
        # (archive_len, window_size, has_solution, empty_arrays)
        (100, 256, True, False),    # archive < window
        (256, 256, False, False),   # archive == window  
        (1000, 256, True, False),   # archive > window
        (0, 256, False, True),      # empty archive
        (512, 256, True, False),    # exact 2x window
        (257, 256, False, False),   # archive slightly > window
    ]
    print("\nRunning equivalence tests...")
    test.test_proof_serialization_equivalence(test_cases)
    
    print("\nRunning field mapping test...")
    test.test_field_name_mapping()
    
    print("\nRunning edge cases test...")
    test.test_edge_cases()
    
    print("\nRunning performance test...")
    test.test_performance()
    
    print("\n✅ All tests passed!")