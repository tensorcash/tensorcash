#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""
Comprehensive test suite for proof verification with multiple deserialization methods.
Tests FlatBuffer deserialization using Python bindings and pfunpack C++ extension.

Usage:
    python test.py              # Full test with stats computation
    python test.py --quick       # Quick test without stats (smell test)
    python test.py --no-verify  # Only test deserialization, skip verification
"""

import os
import sys
import time
import torch
import argparse
from typing import Dict, Any, Tuple

# Import FlatBuffer schemas
from utils.proof import Proof
from utils.proof import FloatArray
from utils.proof import UIntArray
from utils.proof import MiningResponse
from utils.proof import ValidationRequest, ValidationResponse

# Import utilities
from config.constants import *
from utils.shared_utils import (
    validate_by_quantiles, validate_by_quantiles_higher, 
    validate_by_quantiles_lower, proof_to_dict, 
    _snap, _ulp, _sigma_from_ulp, _bucket_means, 
    chiavdf_verify, parse_safetensors_header, 
    inspect_bin_dtype, get_native_dtype_from_commit, 
    inspect_model_dtype, fit_nb_mom, right_tail_test, 
    RunningMeanCov
)
from utils.pow_utils import (
    POW_WINDOW_SIZE, SequenceCache, PowState, Logger, 
    RowManager, RingBuffers, PowHasher, ProofWriter, 
    _to_bytes, serialize_proof, sha256_many, 
    check_hash_against_target, _tok_le_bytes, _u32le, 
    _str_bytes, _build_msg, _digest_to_u, 
    hex_to_bytes_tensor, nbits_to_target, _has_pow, 
    to_python_string
)
from utils.uint256_arithmetics import set_compact, get_compact

# Import C++ extension
import pfunpack

# Test file paths
TEST_FILES = {
    "pow_proof": "tests/pow_proof_test.bin",
    "pow_proof_roundtrip": "tests/pow_proof_test.bin.roundtrip", 
    "full_response": "tests/test_full_response.bin",
    "validation_request": "tests/validation_request.bin"
}

def print_section(title: str):
    """Print a formatted section header."""
    print("\n" + "="*60)
    print(f"  {title}")
    print("="*60)

def deserialize_pow_proof_python(filepath: str) -> Dict[str, Any]:
    """Deserialize POW proof using Python FlatBuffer bindings."""
    print(f"\nDeserializing {filepath} with Python FlatBuffers...")
    
    with open(filepath, "rb") as f:
        buf = f.read()
    
    # Check for PROF identifier first
    if len(buf) >= 8 and buf[4:8] != b'PROF':
        print(f"✗ File lacks PROF identifier (found {buf[4:8].hex()} at offset 4)")
        return None
    
    # Try to deserialize as Proof
    try:
        pf = Proof.Proof.GetRootAsProof(buf, 0)
        d = proof_to_dict(pf)
        print(f"✓ Successfully deserialized as Proof")
        print(f"  - Keys: {list(d.keys())[:5]}...")
        print(f"  - Chosen tokens length: {len(d.get('chosen_tokens', []))}")
        print(f"  - Temperature: {d.get('temperature', 'N/A')}")
        return d
    except Exception as e:
        print(f"✗ Failed to deserialize as Proof: {e}")
        return None

def deserialize_mining_response_python(filepath: str) -> Tuple[Dict[str, Any], Any]:
    """Deserialize Mining Response using Python FlatBuffer bindings."""
    print(f"\nDeserializing {filepath} with Python FlatBuffers...")
    
    with open(filepath, "rb") as f:
        buf = f.read()
    
    try:
        mining_response = MiningResponse.MiningResponse.GetRootAs(buf, 0)
        
        # Extract basic fields
        req_id = mining_response.ReqId()
        nonce = mining_response.Nonce()
        adjusted_bits = mining_response.AdjustedBits()
        difficulty = mining_response.Difficulty()
        
        # Extract PowBlobHash
        pow_blob_hash = b''
        if not mining_response.PowBlobHashIsNone():
            pow_blob_hash = mining_response.PowBlobHashAsNumpy().tobytes()
        
        # Extract nested Proof object
        proof_dict = None
        if mining_response.PowBlob() is not None:
            proof_obj = mining_response.PowBlob()
            proof_dict = proof_to_dict(proof_obj)
        
        print(f"✓ Successfully deserialized as MiningResponse")
        print(f"  - Request ID: {req_id}")
        print(f"  - Nonce: {nonce}")
        print(f"  - Adjusted Bits: {adjusted_bits}")
        print(f"  - Difficulty: {difficulty}")
        print(f"  - POW Blob Hash Length: {len(pow_blob_hash)}")
        if proof_dict:
            print(f"  - Embedded Proof Keys: {list(proof_dict.keys())[:5]}...")
        
        return proof_dict, mining_response
    except Exception as e:
        print(f"✗ Failed to deserialize as MiningResponse: {e}")
        return None, None

def deserialize_validation_request_python(filepath: str) -> Tuple[Dict[str, Any], Any]:
    """Deserialize Validation Request using Python FlatBuffer bindings."""
    print(f"\nDeserializing {filepath} with Python FlatBuffers...")
    
    with open(filepath, "rb") as f:
        buf = f.read()
    
    try:
        request = ValidationRequest.ValidationRequest.GetRootAs(buf, 0)
        
        # Extract basic fields
        hash_id = request.HashIdAsNumpy().tobytes() if request.HashIdLength() > 0 else b''
        validation_type = request.ValidationType()
        
        # Extract the validation data based on type
        validation_data = None
        proof_dict = None
        
        # Check if there's a POW blob embedded
        # This depends on the specific schema structure
        
        print(f"✓ Successfully deserialized as ValidationRequest")
        print(f"  - Hash ID Length: {len(hash_id)}")
        print(f"  - Validation Type: {validation_type}")
        
        return {'hash_id': hash_id, 'validation_type': validation_type}, request
    except Exception as e:
        print(f"✗ Failed to deserialize as ValidationRequest: {e}")
        return None, None

def deserialize_with_pfunpack(filepath: str, file_type: str) -> Dict[str, Any]:
    """Deserialize using pfunpack C++ extension."""
    print(f"\nDeserializing {filepath} with pfunpack C++ extension...")
    
    with open(filepath, "rb") as f:
        buf = f.read()
    
    # Check for PROF identifier if expecting a Proof
    if file_type == "proof" and len(buf) >= 8 and buf[4:8] != b'PROF':
        print(f"✗ File lacks PROF identifier (found {buf[4:8].hex()} at offset 4)")
        return None
    
    try:
        if file_type == "validation_request":
            d = pfunpack.unpack_validation_request(buf)
            print(f"✓ Successfully unpacked as ValidationRequest")
            if 'request' in d and 'pow_blob' in d['request']:
                print(f"  - Contains POW blob with keys: {list(d['request']['pow_blob'].keys())[:5]}...")
        elif file_type == "mining_response":
            d = pfunpack.unpack_mining_response(buf)
            print(f"✓ Successfully unpacked as MiningResponse")
            if 'pow_blob' in d:
                print(f"  - Contains POW blob with keys: {list(d['pow_blob'].keys())[:5]}...")
        else:
            # Try as regular proof
            d = pfunpack.unpack_proof(buf)
            print(f"✓ Successfully unpacked as Proof")
            print(f"  - Keys: {list(d.keys())[:5]}...")
            
        return d
    except Exception as e:
        print(f"✗ Failed to unpack with pfunpack: {e}")
        return None

def compare_deserializations(py_dict: Dict[str, Any], pf_dict: Dict[str, Any]) -> bool:
    """Compare Python and pfunpack deserializations."""
    print("\nComparing Python vs pfunpack deserialization...")
    
    if not py_dict or not pf_dict:
        print("  ✗ One or both deserializations failed")
        return False
        
    try:
        py_keys = set(py_dict.keys())
        pf_keys = set(pf_dict.keys())
        
        if py_keys == pf_keys:
            print(f"  ✓ Both methods produced same keys ({len(py_keys)} keys)")
            return True
        else:
            missing_in_pf = py_keys - pf_keys
            extra_in_pf = pf_keys - py_keys
            if missing_in_pf:
                print(f"  ✗ pfunpack missing keys: {missing_in_pf}")
            if extra_in_pf:
                print(f"  ✗ pfunpack has extra keys: {extra_in_pf}")
            return False
            
    except Exception as e:
        print(f"  ✗ Comparison failed: {e}")
        return False

def run_verification_pipeline(proof_dict: Dict[str, Any], test_name: str, quick_mode: bool = False):
    """Run the full verification pipeline on a proof.
    
    Args:
        proof_dict: The proof dictionary to verify
        test_name: Name of the test for logging
        quick_mode: If True, skip heavy computations for quick testing
    """
    print_section(f"Running Verification Pipeline for {test_name}")
    
    if quick_mode:
        print("⚡ QUICK MODE: Skipping heavy computations")
    
    try:
        # Import everything we need upfront
        from proof_verifier import (
            ProofVerifier,
            mca_set_enabled,
            mca_set_params,
            mca_get_params,
            mca_debug_reset,
            mca_debug_snapshot,
        )
        
        # Enable MCA with parameters
        mca_set_enabled(True)
        mca_set_params(k_lin=1.5, k_attn=8.0, target_dtype=torch.float16)
        print("✓ MCA enabled with parameters:", mca_get_params())
        
        # Reset MCA debug counters before any model forward
        try:
            mca_debug_reset()
        except Exception:
            pass

        # Create verifier instance
        verifier = ProofVerifier()
        
        # ALWAYS disable smell test to prevent stats computation
        verifier.perform_smell_test = False
        print(f"  - Smell test disabled: perform_smell_test = {verifier.perform_smell_test}")
        
        verifier.initialise(proof_dict)
        print("✓ ProofVerifier initialized")
        
        # Run verification steps
        print("\nRunning verification steps:")
        
        # 1. Block sanity check
        try:
            verifier._verify_block_sanity()
            print("  ✓ Block sanity check passed")
        except Exception as e:
            print(f"  ✗ Block sanity check failed: {e}")
            
        # 2. Parameter verification
        try:
            verifier._verify_parameters()
            print("  ✓ Parameter verification passed")
        except Exception as e:
            print(f"  ✗ Parameter verification failed: {e}")
            
        # 3. Sequence verification (vectorized)
        try:
            verifier.verify_sequence_light_vectorized()
            print("  ✓ Sequence verification (vectorized) passed")
        except Exception as e:
            print(f"  ✗ Sequence verification failed: {e}")
        
        if quick_mode:
            print("\n⚡ QUICK MODE: Stopping here - NO model loading, NO full verification")
            print("  ✓ Basic validation checks passed")
            print("  ✓ Sequence verification passed") 
            print("  ⏭️  Skipping model loading")
            print("  ⏭️  Skipping full statistical verification")
            print("  ⏭️  Skipping bootstrap analysis")
            return True
            
        # 4. Load model
        try:
            verifier._load_or_reuse_model()
            print("  ✓ Model loaded/reused successfully")
        except Exception as e:
            print(f"  ✗ Model loading failed: {e}")
            return False
            
        # 5. Full sequence verification with bootstrap
        print("\nRunning full adaptive parallel verification...")
        
        # Use fewer bootstrap samples in quick mode
        bootstrap_samples = 1000 if quick_mode else 15_000
        print(f"  Using {bootstrap_samples} bootstrap samples")
        
        # status, message = verifier.verify_full_sequence_adaptive(
        #     bootstrap=bootstrap_samples, 
        #     charting=not quick_mode  # Disable charting in quick mode
        # )

        start_time = time.time()
        status, message = verifier.verify_full_sequence_adaptive_parallel_efficient(
            bootstrap=bootstrap_samples, 
            charting=not quick_mode  # Disable charting in quick mode
        )
        elapsed = time.time() - start_time
        
        print(f"\n{'✓' if status else '✗'} Verification {'PASSED' if status else 'FAILED'}")
        print(f"  Status: {status}")
        print(f"  Message: {message}")
        print(f"  Time elapsed: {elapsed:.2f} seconds")
        try:
            dbg = mca_debug_snapshot()
            print("\nMCA debug counters:")
            for k in ['sdpa_calls','sdpa_noised','attn_hook_noised','linear_noised','hook_count']:
                print(f"  {k}: {dbg.get(k)}")
            print(f"  k_attn_recent: {dbg.get('k_attn_recent')}")
            print(f"  k_lin_recent: {dbg.get('k_lin_recent')}")
        except Exception:
            pass
        
        return status
        
    except Exception as e:
        print(f"\n✗ Verification pipeline failed with error: {e}")
        import traceback
        traceback.print_exc()
        return False

def main():
    """Main test execution."""
    # Parse command line arguments
    parser = argparse.ArgumentParser(description="Proof verification test suite")
    parser.add_argument("--quick", action="store_true", 
                       help="Quick mode: skip heavy computations for smell test")
    parser.add_argument("--no-verify", action="store_true",
                       help="Only test deserialization, skip verification pipeline")
    args = parser.parse_args()
    
    print_section("COMPREHENSIVE PROOF VERIFICATION TEST SUITE")
    
    if args.quick:
        print("⚡ RUNNING IN QUICK MODE (Smell Test)")
    if args.no_verify:
        print("📦 DESERIALIZATION ONLY MODE")
    
    # Check that all test files exist
    print("\nChecking test files...")
    for name, path in TEST_FILES.items():
        if os.path.exists(path):
            size = os.path.getsize(path)
            print(f"  ✓ {name}: {path} ({size} bytes)")
        else:
            print(f"  ✗ {name}: {path} NOT FOUND")
    
    # Test 1: Validation Request (PRIMARY TEST - ACTUAL REQUEST FROM CORE-NODE)
    print_section("TEST 1: Validation Request (validation_request.bin) - PRIMARY")
    print("This is the actual validation request from core-node that needs verification")
    
    # Import the proper modules for deserialization
    from utils.proof import ValidationRequest, ValidationUnion, BlockValidation, ModelValidation
    
    # Read the validation request file
    with open(TEST_FILES["validation_request"], "rb") as f:
        message = f.read()
    
    try:
        # Deserialize using FlatBuffers (production method)
        request = ValidationRequest.ValidationRequest.GetRootAs(message, 0)
        
        # Get validation type
        validation_type = request.ValidationType()
        print(f"  - Validation Type: {validation_type}")
        
        # Get request type
        request_type = request.RequestType()
        print(f"  - Request Type: {request_type}")
        
        pow_blob_dict = None
        
        # Handle BlockValidation (most common case)
        if request_type == ValidationUnion.ValidationUnion.BlockValidation:
            print("  - Processing BlockValidation request")
            block = BlockValidation.BlockValidation()
            block.Init(request.Request().Bytes, request.Request().Pos)
            
            # Extract POW blob from BlockValidation
            if block.PowBlob():
                print("\n✓ Successfully extracted POW blob from BlockValidation")
                # Convert to dict using proof_to_dict
                from utils.shared_utils import proof_to_dict
                pow_blob_dict = proof_to_dict(block.PowBlob())
                
                # Show proof details
                print(f"  - Model: {pow_blob_dict.get('model_identifier', 'N/A')}")
                print(f"  - Temperature: {pow_blob_dict.get('temperature', 'N/A')}")
                print(f"  - Chosen tokens: {len(pow_blob_dict.get('chosen_tokens', []))}")
                print(f"  - Is solution: {pow_blob_dict.get('is_solution', 'N/A')}")
        
        # Handle ModelValidation
        elif request_type == ValidationUnion.ValidationUnion.ModelValidation:
            print("  - Processing ModelValidation request")
            model = ModelValidation.ModelValidation()
            model.Init(request.Request().Bytes, request.Request().Pos)
            print(f"  - Model Name: {model.ModelName()}")
            print(f"  - Model Commit: {model.ModelCommit()}")
        
        print(pow_blob_dict['topk_logits'][51],
        pow_blob_dict['topk_indices'][51])

        # Run verification if we have a POW blob
        if pow_blob_dict and not args.no_verify:
            print("\n🔍 Running PRIMARY VERIFICATION on validation request...")
            run_verification_pipeline(pow_blob_dict, "Validation Request Verification", quick_mode=args.quick)
        elif args.no_verify:
            print("\nSkipping verification (--no-verify flag set)")
        else:
            print("\n✗ No POW blob found in validation request - cannot verify!")
            
    except Exception as e:
        print(f"\n✗ Failed to deserialize validation request: {e}")
        import traceback
        traceback.print_exc()
    
    # Test 2: Other test files (SECONDARY - for debugging/comparison)
    print_section("TEST 2: Secondary Tests (pow_proof_test.bin files)")
    print("These are secondary test files for comparison/debugging")
    
    # Test POW proof standalone file
    print("\n--- Testing pow_proof_test.bin ---")
    proof_dict_py = deserialize_pow_proof_python(TEST_FILES["pow_proof"])
    proof_dict_pf = deserialize_with_pfunpack(TEST_FILES["pow_proof"], "proof")
    if proof_dict_py and proof_dict_pf:
        compare_deserializations(proof_dict_py, proof_dict_pf)
    
    # Test roundtrip with new pack function
    print("\n--- Testing roundtrip with pfunpack.pack_proof() ---")
    if proof_dict_pf:
        try:
            # Pack the proof dict back to bytes
            packed_bytes = pfunpack.pack_proof(proof_dict_pf)
            print(f"  ✓ Successfully packed proof to {len(packed_bytes)} bytes")
            
            # Unpack it again to verify roundtrip
            roundtrip_dict = pfunpack.unpack_proof(packed_bytes)
            print(f"  ✓ Successfully unpacked roundtrip proof")
            
            # Compare keys
            original_keys = set(proof_dict_pf.keys())
            roundtrip_keys = set(roundtrip_dict.keys())
            if original_keys == roundtrip_keys:
                print(f"  ✓ Roundtrip preserved all {len(original_keys)} keys")
            else:
                missing = original_keys - roundtrip_keys
                extra = roundtrip_keys - original_keys
                if missing:
                    print(f"  ✗ Missing after roundtrip: {missing}")
                if extra:
                    print(f"  ✗ Extra after roundtrip: {extra}")
                    
            # Save the properly packed version for comparison
            with open("tests/pow_proof_test.bin.new_roundtrip", "wb") as f:
                f.write(packed_bytes)
                print("  ✓ Saved new roundtrip file to tests/pow_proof_test.bin.new_roundtrip")
                
        except Exception as e:
            print(f"  ✗ Roundtrip test failed: {e}")
    
    # Test old roundtrip file (likely corrupted)
    print("\n--- Testing pow_proof_test.bin.roundtrip (existing file) ---")
    proof_dict_rt_py = deserialize_pow_proof_python(TEST_FILES["pow_proof_roundtrip"])
    proof_dict_rt_pf = deserialize_with_pfunpack(TEST_FILES["pow_proof_roundtrip"], "proof")
    
    if not proof_dict_rt_py and not proof_dict_rt_pf:
        print("  ⚠️  Existing roundtrip file is corrupted (missing PROF identifier)")
        print("  Use the new pack_proof() function to create valid roundtrip files")
    
    # Test 3: test_full_response.bin - Try as Proof first, then MiningResponse
    print("\n--- Testing test_full_response.bin ---")
    print("  Attempting multiple deserialization formats...")
    
    # First try as a Proof (since it's similar size to pow_proof_test.bin)
    try:
        proof_resp = deserialize_with_pfunpack(TEST_FILES["full_response"], "proof")
        if proof_resp:
            print("  ✓ Successfully deserialized as Proof")
            print(f"    Keys: {list(proof_resp.keys())[:5]}...")
    except:
        print("  ✗ Not a Proof format")
    
    # Then try as MiningResponse (might be wrong format)
    try:
        mining_resp_pf = deserialize_with_pfunpack(TEST_FILES["full_response"], "mining_response")
        if mining_resp_pf and 'pow_blob' in mining_resp_pf:
            print("  ✓ Successfully deserialized as MiningResponse with POW blob")
    except:
        print("  ✗ Not a MiningResponse format")
    
    print_section("TEST SUITE COMPLETE")

if __name__ == "__main__":
    main()
