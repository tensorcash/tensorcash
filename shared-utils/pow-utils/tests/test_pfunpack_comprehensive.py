# SPDX-License-Identifier: Apache-2.0
"""Comprehensive test of pfunpack packing and unpacking functionality."""

import pytest
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import torch
import numpy as np
from pow_utils import serialize_proof

# Try to import pfunpack
try:
    sys.path.insert(0, '.')  # For local pfunpack.so
    import pfunpack
    HAS_PFUNPACK = True
except ImportError:
    HAS_PFUNPACK = False


@pytest.mark.skipif(not HAS_PFUNPACK, reason="pfunpack not available")
class TestPfunpackComprehensive:
    """Comprehensive tests for pfunpack functionality."""
    
    def test_unpack_proof_all_fields(self):
        """Test that all fields in Proof schema are properly unpacked."""
        # Create proof with ALL fields from the schema
        proof_dict = {
            # Scalars
            'version': 2,
            'tick': 12345,
            'timestamp': 1234567890,
            'is_solution': True,
            'temperature': 0.85,
            'top_p': 0.92,
            'top_k': 50,
            'repetition_penalty': 1.2,
            
            # Binary fields (as hex strings)
            'target': "00" * 30 + "ffff",
            'vdf': "aa" * 32,
            'hash': "bb" * 32,
            'block_hash': "cc" * 32,
            'header_prefix': "dd" * 76,
            
            # String fields  
            'model_identifier': 'gpt-4-turbo',
            'compute_precision': 'fp16',
            'ipfs_cid': 'QmTestIPFSCID123456789',
            'extra_flags': 'flag1=value1,flag2=value2',
            'model_config_diff': 'temperature=0.8,top_p=0.95',
            
            # 1D arrays
            'chosen_tokens': [101, 102, 103, 104, 105],
            'chosen_probs': [0.1, 0.2, 0.3, 0.25, 0.15],
            'sampling_u': [0.123, 0.456, 0.789, 0.111, 0.999],
            'softmax_normalizers': [1.0, 1.1, 1.2, 1.3, 1.4],
            'prompt_tokens': list(range(20)),
            'pad_mask': [1] * 15 + [0] * 5,
            
            # 2D arrays
            'topk_logits': [[0.1 * i + 0.01 * j for j in range(50)] for i in range(5)],
            'topk_indices': [[i * 50 + j for j in range(50)] for i in range(5)],
            'logsumexp_stats': [[float(i), float(i+1), float(i+2), float(i+3), float(i+4), float(i+5)] for i in range(5)],
        }
        
        # Serialize
        proof_bytes = serialize_proof(proof_dict)
        
        # Unpack
        unpacked = pfunpack.unpack_proof(proof_bytes)
        
        # Check all scalar fields
        # Note: version is hardcoded to 2 in serialize_proof (v2 reuse-entropy rollout)
        assert unpacked['version'] == 2  # Always 2 in current implementation
        assert unpacked['tick'] == proof_dict['tick']
        assert unpacked['timestamp'] == proof_dict['timestamp']
        assert unpacked['is_solution'] == proof_dict['is_solution']
        
        # Check float fields (with tolerance)
        assert abs(unpacked['temperature'] - proof_dict['temperature']) < 1e-6
        assert abs(unpacked['top_p'] - proof_dict['top_p']) < 1e-6
        assert unpacked['top_k'] == proof_dict['top_k']
        assert abs(unpacked['repetition_penalty'] - proof_dict['repetition_penalty']) < 1e-6
        
        # Check string fields
        assert unpacked['model_identifier'] == proof_dict['model_identifier']
        assert unpacked['compute_precision'] == proof_dict['compute_precision']
        assert unpacked['ipfs_cid'] == proof_dict['ipfs_cid']
        # Note: model_config_diff is serialized as extra_flags
        assert unpacked['extra_flags'] == proof_dict['model_config_diff']
        
        # Check binary fields (returned as hex strings)
        assert unpacked['target'] == proof_dict['target']
        assert unpacked['vdf'] == proof_dict['vdf']
        assert unpacked['hash'] == proof_dict['hash']
        assert unpacked['block_hash'] == proof_dict['block_hash']
        assert unpacked['header_prefix'] == proof_dict['header_prefix']
        
        # Check 1D arrays
        assert list(unpacked['chosen_tokens']) == proof_dict['chosen_tokens']
        assert len(unpacked['prompt_tokens']) == len(proof_dict['prompt_tokens'])
        assert len(unpacked['pad_mask']) == len(proof_dict['pad_mask'])
        
        # Check 1D float arrays (with tolerance)
        for i, val in enumerate(proof_dict['chosen_probs']):
            assert abs(unpacked['chosen_probs'][i] - val) < 1e-6
        
        # Check 2D arrays shape
        if 'topk_logits' in unpacked:
            assert unpacked['topk_logits'].shape == (5, 50)
        if 'topk_indices' in unpacked:
            assert unpacked['topk_indices'].shape == (5, 50)
        if 'logsumexp_stats' in unpacked:
            assert unpacked['logsumexp_stats'].shape == (5, 6)
    
    def test_unpack_validation_request(self):
        """Test unpacking ValidationRequest messages."""
        # This would require creating a ValidationRequest using FlatBuffers
        # For now, we'll skip this as it requires the ValidationRequest builder
        pass
    
    def test_unpack_mining_response(self):
        """Test unpacking MiningResponse messages."""
        # MiningResponse contains a Proof, so we can test this by wrapping
        # a proof in a MiningResponse envelope
        # For now, we'll skip this as it requires the MiningResponse builder
        pass
    
    def test_edge_cases(self):
        """Test edge cases and error handling."""
        # Test empty arrays
        proof_dict = {
            'version': 2,
            'tick': 0,
            'timestamp': 0,
            'is_solution': False,
            'model_identifier': '',
            'compute_precision': '',
            'ipfs_cid': '',
            'model_config_diff': '',
            'temperature': 0.0,
            'top_p': 0.0,
            'top_k': 0,
            'repetition_penalty': 0.0,
            'target': '00' * 32,
            'vdf': '00' * 32,
            'hash': '00' * 32,
            'block_hash': '00' * 32,
            'header_prefix': '00' * 76,
            'chosen_tokens': [],
            'chosen_probs': [],
            'sampling_u': [],
            'softmax_normalizers': [],
            'prompt_tokens': [],
            'pad_mask': [],
            'topk_logits': [],
            'topk_indices': [],
            'logsumexp_stats': [],
        }
        
        proof_bytes = serialize_proof(proof_dict)
        unpacked = pfunpack.unpack_proof(proof_bytes)
        
        # Should handle empty arrays gracefully
        assert unpacked['version'] == 2
        assert unpacked['tick'] == 0
        assert len(unpacked['chosen_tokens']) == 0
    
    def test_large_data(self):
        """Test with large arrays to ensure no buffer overflows."""
        large_size = 1000
        proof_dict = {
            'version': 2,
            'tick': 999999,
            'timestamp': 9999999999,
            'is_solution': True,
            'model_identifier': 'x' * 100,
            'compute_precision': 'fp32',
            'ipfs_cid': 'Qm' + 'x' * 44,
            'model_config_diff': 'config=' + 'x' * 100,
            'temperature': 1.5,
            'top_p': 0.999,
            'top_k': 100,
            'repetition_penalty': 2.0,
            'target': 'ff' * 32,
            'vdf': 'ee' * 32,
            'hash': 'dd' * 32,
            'block_hash': 'cc' * 32,
            'header_prefix': 'bb' * 76,
            'chosen_tokens': list(range(large_size)),
            'chosen_probs': [0.001] * large_size,
            'sampling_u': [0.5] * large_size,
            'softmax_normalizers': [1.0] * large_size,
            'prompt_tokens': list(range(large_size)),
            'pad_mask': [1] * large_size,
            'topk_logits': [[0.1] * 100 for _ in range(10)],
            'topk_indices': [[i] * 100 for i in range(10)],
            'logsumexp_stats': [[1.0] * 6 for _ in range(10)],
        }
        
        proof_bytes = serialize_proof(proof_dict)
        unpacked = pfunpack.unpack_proof(proof_bytes)
        
        # Should handle large data
        assert unpacked['tick'] == 999999
        assert len(unpacked['chosen_tokens']) == large_size
        assert len(unpacked['prompt_tokens']) == large_size
    
    def test_special_characters_in_strings(self):
        """Test handling of special characters in string fields."""
        proof_dict = {
            'version': 2,
            'tick': 100,
            'timestamp': 123456,
            'is_solution': False,
            'model_identifier': 'model-with-émoji-😀',
            'compute_precision': 'fp16',
            'ipfs_cid': 'Qm-with-special-chars-!@#$%',
            'extra_flags': 'flag="value with spaces"',
            'model_config_diff': 'key1=value1\nkey2=value2\tkey3=value3',
            'temperature': 0.7,
            'top_p': 0.9,
            'top_k': 40,
            'repetition_penalty': 1.0,
            'target': 'ab' * 32,
            'vdf': 'cd' * 32,
            'hash': 'ef' * 32,
            'block_hash': '12' * 32,
            'header_prefix': '34' * 76,
            'chosen_tokens': [1, 2, 3],
            'chosen_probs': [0.3, 0.3, 0.4],
            'sampling_u': [0.5],
            'softmax_normalizers': [1.0],
            'prompt_tokens': [10, 20],
            'pad_mask': [1, 1],
            'topk_logits': [[0.1, 0.2]],
            'topk_indices': [[0, 1]],
            'logsumexp_stats': [[1.0, 2.0]],
        }
        
        proof_bytes = serialize_proof(proof_dict)
        unpacked = pfunpack.unpack_proof(proof_bytes)
        
        # Check special characters are preserved
        assert unpacked['model_identifier'] == proof_dict['model_identifier']
        assert unpacked['ipfs_cid'] == proof_dict['ipfs_cid']
        # Note: model_config_diff is serialized as extra_flags
        assert unpacked['extra_flags'] == proof_dict['model_config_diff']


@pytest.mark.skipif(not HAS_PFUNPACK, reason="pfunpack not available")
class TestPfunpackV3Carrier:
    """v3 admission carrier (TIP-0003): the nonce rides inside
    extra_flags; the packer preserves extra_flags verbatim across pack/unpack/
    repack, and rejects a SIDE admission_nonce (which would be a silent second
    v3 writer path). Exercises the LIVE pfunpack.pack_proof path (the repack
    path used by pack_mining_response_with_proof_bytes)."""

    def _full_proof_dict(self, *, version, model_config_diff):
        return {
            'version': version, 'tick': 100000, 'timestamp': 1700000000,
            'is_solution': True, 'temperature': 1.0, 'top_p': 1.0,
            'top_k': 50, 'repetition_penalty': 1.0,
            'target': "00" * 30 + "ffff", 'vdf': "aa" * 32, 'hash': "bb" * 32,
            'block_hash': "cc" * 32, 'header_prefix': "dd" * 76,
            'model_identifier': 'model@commit', 'compute_precision': 'fp16',
            'ipfs_cid': '', 'model_config_diff': model_config_diff,
            'chosen_tokens': [101, 102, 103], 'chosen_probs': [0.5, 0.3, 0.2],
            'sampling_u': [0.1, 0.2, 0.3], 'softmax_normalizers': [1.0, 1.1, 1.2],
            'prompt_tokens': [1, 2, 3], 'pad_mask': [1, 1, 0],
            'topk_logits': [[0.1, 0.2]], 'topk_indices': [[1, 2]],
            'logsumexp_stats': [[1.0, 2.0]],
        }

    def test_v3_nonce_in_extra_flags_survives_repack(self):
        import json
        nonce_hex = "ab" * 32
        carrier = json.dumps({"v3": {"admission_nonce": nonce_hex}},
                             separators=(",", ":"))
        proof_dict = self._full_proof_dict(version=3, model_config_diff=carrier)

        # Producer path: serialize_proof serializes model_config_diff (carrier)
        # into the FB extra_flags field; unpack surfaces it as the extra_flags
        # key with the nonce intact.
        u0 = pfunpack.unpack_proof(serialize_proof(proof_dict))
        assert u0['extra_flags'] == carrier
        assert json.loads(u0['extra_flags'])["v3"]["admission_nonce"] == nonce_hex

        # LIVE repack: pack_proof reads the extra_flags key and preserves it,
        # so the nonce survives an unpack -> pack -> unpack round-trip.
        u1 = pfunpack.unpack_proof(pfunpack.pack_proof(u0))
        assert u1['extra_flags'] == carrier
        assert json.loads(u1['extra_flags'])["v3"]["admission_nonce"] == nonce_hex

    def test_side_admission_nonce_rejected(self):
        # A caller passing admission_nonce as a SIDE field (instead of merging
        # it into extra_flags) must fail loudly, not silently emit a proof
        # missing the nonce.
        u0 = pfunpack.unpack_proof(
            serialize_proof(self._full_proof_dict(version=3, model_config_diff="{}")))
        u0['admission_nonce'] = bytes.fromhex("ab" * 32)
        with pytest.raises(Exception):
            pfunpack.pack_proof(u0)

    def test_v3_nonce_survives_nested_validation_request_unpack(self):
        # REGRESSION (testnet block 9710, 2026-07-14): unpack_validation_request
        # unpacks the nested Proof via unpack_proof_obj, which omitted
        # extra_flags — the verifier then replayed every nonce-bound proof
        # WITHOUT the nonce ("Sampling hash inconsistent with recomputation")
        # and rejected every consensus-valid admission proof. The standalone
        # unpack_proof path always surfaced extra_flags, so only the nested
        # BlockValidation/Challenge path was broken. Pin the nested path.
        import json
        nonce_hex = "ab" * 32
        carrier = json.dumps({"v3": {"admission_nonce": nonce_hex}},
                             separators=(",", ":"))
        proof_dict = self._full_proof_dict(version=3, model_config_diff="{}")
        proof_dict['extra_flags'] = carrier
        payload = pfunpack.pack_validation_request({
            'hash_id': 'h' * 32,
            'validation_type': 0,
            'request': {
                'kind': 'BlockValidation',
                'difficulty': 1_000_000,
                'prev_block_hash': 'p' * 32,
                'pow_blob': proof_dict,
            },
        })
        req = pfunpack.unpack_validation_request(payload)['request']
        assert req['difficulty'] == 1_000_000
        blob = req['pow_blob']
        assert blob['extra_flags'] == carrier
        assert json.loads(blob['extra_flags'])["v3"]["admission_nonce"] == nonce_hex
        assert blob['ipfs_cid'] == ''


if __name__ == "__main__":
    pytest.main([__file__, "-v"])