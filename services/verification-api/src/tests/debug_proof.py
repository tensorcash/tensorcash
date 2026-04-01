#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""
Debug script to test a specific proof file for U value mismatches.

Usage:
    python debug_proof.py /path/to/proof.bin
    python debug_proof.py /path/to/proof.bin --verbose
"""

import os
import sys
import argparse
import torch
import numpy as np

# Import FlatBuffer schemas
from utils.proof import Proof, ValidationRequest, ValidationUnion, BlockValidation
from utils.shared_utils import proof_to_dict
from utils.pow_utils import (
    _tok_le_bytes, _u32le, _str_bytes, _build_msg, _digest_to_u,
    hex_to_bytes_tensor, sha256_many
)

import pfunpack

def try_all_unpack_methods(buf, verbose=False):
    """Try all possible unpack methods and return the proof dict."""
    proof_dict = None
    method_used = None

    # Show file header info
    print(f"\n[DEBUG] File header (first 64 bytes):")
    print(f"    Hex: {buf[:64].hex()}")
    print(f"    Bytes 0-3 (size?): {int.from_bytes(buf[0:4], 'little')}")
    print(f"    Bytes 4-7 (ident): {buf[4:8]}")

    # Method 1: Try pfunpack.unpack_validation_request first (most common in production)
    print(f"\n[DEBUG] Trying pfunpack.unpack_validation_request...")
    try:
        result = pfunpack.unpack_validation_request(buf)
        if 'request' in result and 'pow_blob' in result['request']:
            proof_dict = result['request']['pow_blob']
            method_used = "pfunpack.unpack_validation_request"
            print(f"    ✓ Success!")
            return proof_dict, method_used
        else:
            print(f"    ✗ No pow_blob in result, keys: {result.keys()}")
    except Exception as e:
        print(f"    ✗ Failed: {e}")

    # Method 2: Try pfunpack.unpack_proof
    print(f"\n[DEBUG] Trying pfunpack.unpack_proof...")
    try:
        proof_dict = pfunpack.unpack_proof(buf)
        method_used = "pfunpack.unpack_proof"
        print(f"    ✓ Success!")
        return proof_dict, method_used
    except Exception as e:
        print(f"    ✗ Failed: {e}")

    # Method 3: Try Python FlatBuffer bindings for ValidationRequest
    print(f"\n[DEBUG] Trying Python FlatBuffers ValidationRequest...")
    try:
        request = ValidationRequest.ValidationRequest.GetRootAs(buf, 0)
        request_type = request.RequestType()
        print(f"    Request type: {request_type}")

        if request_type == ValidationUnion.ValidationUnion.BlockValidation:
            block = BlockValidation.BlockValidation()
            block.Init(request.Request().Bytes, request.Request().Pos)
            if block.PowBlob():
                proof_dict = proof_to_dict(block.PowBlob())
                method_used = "Python FlatBuffers (ValidationRequest->BlockValidation->PowBlob)"
                print(f"    ✓ Success!")
                return proof_dict, method_used
            else:
                print(f"    ✗ No PowBlob in BlockValidation")
        else:
            print(f"    ✗ Not a BlockValidation request")
    except Exception as e:
        print(f"    ✗ Failed: {e}")

    # Method 4: Try Python FlatBuffer bindings for raw Proof
    print(f"\n[DEBUG] Trying Python FlatBuffers Proof...")
    try:
        pf = Proof.Proof.GetRootAsProof(buf, 0)
        proof_dict = proof_to_dict(pf)
        method_used = "Python FlatBuffers (Proof)"
        print(f"    ✓ Success!")
        return proof_dict, method_used
    except Exception as e:
        print(f"    ✗ Failed: {e}")

    # Method 5: Try pfunpack.unpack_mining_response
    print(f"\n[DEBUG] Trying pfunpack.unpack_mining_response...")
    try:
        result = pfunpack.unpack_mining_response(buf)
        if 'pow_blob' in result:
            proof_dict = result['pow_blob']
            method_used = "pfunpack.unpack_mining_response"
            print(f"    ✓ Success!")
            return proof_dict, method_used
        else:
            print(f"    ✗ No pow_blob, keys: {result.keys()}")
    except Exception as e:
        print(f"    ✗ Failed: {e}")

    return None, None

def compute_u_single(header_prefix, vdf, tick, step_idx, context_tokens, precision, device='cuda'):
    """Compute U value for a single step (non-batched) - for verification."""
    ws = 256  # POW_WINDOW_SIZE

    # Build window tokens
    window_tokens = torch.zeros(1, ws, dtype=torch.int64, device=device)
    L = min(len(context_tokens), ws)
    window_tokens[0, -L:] = context_tokens[-L:]

    # Encode
    ctx_bytes = _tok_le_bytes(window_tokens)
    j4 = _u32le(torch.tensor([[step_idx]], dtype=torch.uint32, device=device))
    T8 = _u32le(torch.tensor([tick], dtype=torch.uint32, device=device))
    precision_bytes = _str_bytes(precision, batch_size=1, device=device)

    header_data = hex_to_bytes_tensor(header_prefix, device=device).contiguous()
    v = hex_to_bytes_tensor(vdf, device=device).contiguous()

    msg = _build_msg(header_data, v, T8, j4, ctx_bytes, precision_bytes)
    digest = sha256_many(msg)
    return _digest_to_u(digest)[0]


def debug_u_values(proof_dict, device='cuda', verbose=False):
    """Debug U value computation and comparison."""

    print("\n" + "="*60)
    print("  U VALUE DEBUG ANALYSIS")
    print("="*60)

    # Extract expected u values from proof - check multiple possible keys
    # MiningResponse uses 'sampling_u', ValidationRequest/Proof uses 'expected_u'
    expected_u_raw = proof_dict.get('expected_u', [])
    sampling_u_raw = proof_dict.get('sampling_u', [])

    print(f"\n[DEBUG] U value fields:")
    print(f"    expected_u: {type(expected_u_raw).__name__}, len={len(expected_u_raw) if hasattr(expected_u_raw, '__len__') else 'N/A'}")
    print(f"    sampling_u: {type(sampling_u_raw).__name__}, len={len(sampling_u_raw) if hasattr(sampling_u_raw, '__len__') else 'N/A'}")
    if len(sampling_u_raw) > 0:
        print(f"    sampling_u first 3: {sampling_u_raw[:3]}")

    if len(expected_u_raw) == 0 and len(sampling_u_raw) > 0:
        expected_u_raw = sampling_u_raw
        print(f"    (Using 'sampling_u' field from MiningResponse format)")

    print(f"\n[1] Expected U values from proof:")
    print(f"    Count: {len(expected_u_raw)}")
    if verbose and len(expected_u_raw) > 0:
        print(f"    First 5: {expected_u_raw[:5]}")
        print(f"    Last 5:  {expected_u_raw[-5:]}")

    # Get proof parameters
    window_size = len(expected_u_raw)
    prompt_tokens = torch.tensor(proof_dict.get('prompt_tokens', []), dtype=torch.long, device=device)
    chosen_tokens = torch.tensor(proof_dict.get('chosen_tokens', []), dtype=torch.long, device=device)
    header_prefix = proof_dict.get('header_prefix', '')
    vdf = proof_dict.get('vdf', '')
    tick = proof_dict.get('tick', 0)
    # MiningResponse uses 'compute_precision', others use 'stated_precision'
    stated_precision = proof_dict.get('stated_precision', proof_dict.get('compute_precision', 'fp16'))

    print(f"\n[2] Proof parameters:")
    print(f"    Window size: {window_size}")
    print(f"    Prompt tokens: {len(prompt_tokens)}")
    print(f"    Chosen tokens: {len(chosen_tokens)}")
    print(f"    Header prefix length: {len(header_prefix)//2} bytes")
    print(f"    VDF length: {len(vdf)//2} bytes")
    print(f"    Tick: {tick}")
    print(f"    Stated precision: {stated_precision}")
    if verbose:
        print(f"    Prompt tokens: {prompt_tokens.tolist()}")
        print(f"    First 20 chosen tokens: {chosen_tokens[:20].tolist()}")

    # If no expected U values, we can't compare but we can still compute
    if window_size == 0:
        print(f"\n⚠️  No expected U values in proof (sampling_u/expected_u is empty)")
        print(f"    This proof may be from a miner that doesn't store U values.")
        print(f"    Using chosen_tokens length as window_size: {len(chosen_tokens)}")
        window_size = len(chosen_tokens)
        expected_u_raw = None  # Mark that we have no expected values to compare

    # Compute U values
    print(f"\n[3] Computing U values...")

    if expected_u_raw is not None:
        expected_u = torch.tensor(expected_u_raw, dtype=torch.float64, device=device)
    else:
        expected_u = None

    all_contexts = []
    for i in range(window_size):
        context = torch.cat([prompt_tokens, chosen_tokens[:i]])
        all_contexts.append(context)

    # Build batched message (same as verify_sequence_light_vectorized)
    batch_size = len(all_contexts)
    ws = 256  # standard window size for context hashing (POW_WINDOW_SIZE)

    window_tokens = torch.zeros(batch_size, ws, dtype=torch.int64, device=device)
    for i, ctx in enumerate(all_contexts):
        L = min(len(ctx), ws)
        window_tokens[i, -L:] = ctx[-L:]

    ctx_bytes = _tok_le_bytes(window_tokens)
    step_indices = torch.arange(window_size, dtype=torch.long, device=device)
    j4 = _u32le(step_indices.view(-1, 1).to(torch.uint32))
    T8 = _u32le(torch.tensor([tick], dtype=torch.uint32, device=device))
    precision_bytes = _str_bytes(stated_precision, batch_size=batch_size, device=device)

    header_data = hex_to_bytes_tensor(header_prefix, device=device).contiguous()
    v = hex_to_bytes_tensor(vdf, device=device).contiguous()

    msg_batch = _build_msg(header_data, v, T8, j4, ctx_bytes, precision_bytes)
    digests = sha256_many(msg_batch)
    computed_u = _digest_to_u(digests)

    print(f"    Computed {len(computed_u)} U values")

    # Debug: compare msg bytes for matching vs mismatching steps
    if verbose and expected_u is not None:
        print(f"\n    [DEBUG] Message structure comparison:")
        print(f"      Header prefix: {header_prefix[:40]}... ({len(header_prefix)//2} bytes)")
        print(f"      VDF: {vdf[:40]}... ({len(vdf)//2} bytes)")
        print(f"      Tick (T8): {T8.tolist()}")
        print(f"      Precision: '{stated_precision}'")

        # Show context tokens for step 0 (match) and step 1 (mismatch)
        for step_i in [0, 1, 14, 15]:
            if step_i < window_size:
                ctx = all_contexts[step_i]
                # Last 10 tokens in the context
                print(f"\n      Step {step_i} context (len={len(ctx)}):")
                print(f"        Last 10 tokens: {ctx[-10:].tolist()}")
                print(f"        j4 (step bytes): {j4[step_i].tolist()}")
                print(f"        ctx_bytes shape: {ctx_bytes[step_i].shape}")
                print(f"        ctx_bytes last 80: {ctx_bytes[step_i, -80:].tolist()}")
    if verbose:
        print(f"    First 5 computed: {computed_u[:5].tolist()}")
        print(f"    Last 5 computed:  {computed_u[-5:].tolist()}")

    # Compare (if we have expected values)
    if expected_u is None:
        print(f"\n[4] No expected U values to compare - showing computed values only")
        print(f"    ⚠️  Cannot verify U values without expected_u/sampling_u in proof")
        print(f"\n    Computed U values sample:")
        for i in [0, 1, 2, window_size//2, window_size-2, window_size-1]:
            if i < len(computed_u):
                print(f"      Step {i}: u={computed_u[i].item():.10e}")
        return None  # Unknown - no comparison possible

    print(f"\n[4] Comparing U values:")
    diff = torch.abs(computed_u - expected_u)
    tolerance = 1e-7
    matches = diff <= tolerance

    # Check for off-by-one patterns
    print(f"    Checking for rotation/alignment issues...")
    if window_size > 1:
        shifted_matches_fwd = torch.abs(expected_u[:-1] - computed_u[1:]) <= tolerance
        shifted_matches_bwd = torch.abs(expected_u[1:] - computed_u[:-1]) <= tolerance
        print(f"    Normal alignment: {matches.sum().item()}/{window_size} matches")
        print(f"    Shifted +1: {shifted_matches_fwd.sum().item()}/{window_size-1}")
        print(f"    Shifted -1: {shifted_matches_bwd.sum().item()}/{window_size-1}")

        # Test all possible rotations to see if any gives perfect match
        best_rotation = 0
        best_match_count = matches.sum().item()
        for rot in range(1, window_size):
            rotated_expected = torch.roll(expected_u, -rot)
            rot_matches = torch.abs(rotated_expected - computed_u) <= tolerance
            count = rot_matches.sum().item()
            if count > best_match_count:
                best_match_count = count
                best_rotation = rot

        if best_rotation != 0:
            print(f"    ⚠️  ROTATION DETECTED: rotating expected_u by {best_rotation} gives {best_match_count}/{window_size} matches")
            rotated_expected = torch.roll(expected_u, -best_rotation)
            rot_matches = torch.abs(rotated_expected - computed_u) <= tolerance
            if not rot_matches.all():
                still_fail = (~rot_matches).nonzero(as_tuple=True)[0][:10]
                print(f"    Still failing after rotation: {still_fail.tolist()}")

        # Test if mismatch steps have U values that match OTHER computed steps
        # This would indicate step index mixup during batch processing
        print(f"\n    Checking for step index mixup (batch processing bug)...")
        mismatched_idx = (~matches).nonzero(as_tuple=True)[0]
        found_mixup = False
        for bad_step in mismatched_idx[:5]:
            bad_expected = expected_u[bad_step]
            # Search if this expected_u matches any other computed_u
            for other_step in range(window_size):
                if other_step == bad_step:
                    continue
                if torch.abs(bad_expected - computed_u[other_step]) <= tolerance:
                    print(f"      Step {bad_step.item()}: expected_u matches computed_u[{other_step}]!")
                    found_mixup = True
                    break
            else:
                # Check if computed_u[bad_step] matches any expected_u elsewhere
                for other_step in range(window_size):
                    if torch.abs(computed_u[bad_step] - expected_u[other_step]) <= tolerance:
                        print(f"      Step {bad_step.item()}: computed_u matches expected_u[{other_step}]!")
                        found_mixup = True
                        break
        if not found_mixup:
            print(f"      No step index mixup detected - U values don't match at any other position")

        # Non-batched verification to rule out batching bugs
        print(f"\n    Running NON-BATCHED U computation for mismatched steps...")
        for bad_step in mismatched_idx[:5]:
            i = bad_step.item()
            ctx = all_contexts[i]
            u_single = compute_u_single(header_prefix, vdf, tick, i, ctx, stated_precision, device)
            batched_u = computed_u[i].item()
            expected = expected_u[i].item()
            match_batched = abs(u_single.item() - batched_u) <= tolerance
            match_expected = abs(u_single.item() - expected) <= tolerance
            print(f"      Step {i}: single={u_single.item():.10e}, batched={batched_u:.10e}, expected={expected:.10e}")
            print(f"               single==batched: {match_batched}, single==expected: {match_expected}")

        # Check what U the miner WOULD have computed with wrong step index
        # Priority manager may have caused step_index to be computed with ANY value
        print(f"\n    Testing if miner used WRONG step index (WIDE RANGE search)...")
        print(f"    Searching step indices 0-10000 for each mismatched step...")

        # Batch compute U values for step indices 0-10000 with CORRECT context
        # This is much faster than single computation
        search_range = 10001

        for bad_step in mismatched_idx:
            i = bad_step.item()
            bad_expected = expected_u[i].item()
            ctx = all_contexts[i]

            # Build context for this step (same for all test indices)
            ws = 256
            window_tokens_single = torch.zeros(1, ws, dtype=torch.int64, device=device)
            L = min(len(ctx), ws)
            window_tokens_single[0, -L:] = ctx[-L:]
            ctx_bytes_single = _tok_le_bytes(window_tokens_single)

            # Batch over step indices
            batch_size = 1000
            found = False
            found_idx = None

            for start in range(0, search_range, batch_size):
                end = min(start + batch_size, search_range)
                n = end - start

                # Replicate context for batch
                ctx_bytes_batch = ctx_bytes_single.expand(n, -1).contiguous()

                # Step indices for this batch
                test_indices = torch.arange(start, end, dtype=torch.int64, device=device)
                j4 = _u32le(test_indices.view(-1, 1).to(torch.uint32))
                T8 = _u32le(torch.tensor([tick], dtype=torch.uint32, device=device))
                precision_bytes = _str_bytes(stated_precision, batch_size=n, device=device)

                header_data = hex_to_bytes_tensor(header_prefix, device=device).contiguous()
                v = hex_to_bytes_tensor(vdf, device=device).contiguous()

                msg_batch = _build_msg(header_data, v, T8, j4, ctx_bytes_batch, precision_bytes)
                digests = sha256_many(msg_batch)
                u_batch = _digest_to_u(digests)

                # Check for matches
                diffs = torch.abs(u_batch - bad_expected)
                matches_mask = diffs <= tolerance
                if matches_mask.any():
                    match_idx = matches_mask.nonzero(as_tuple=True)[0][0].item()
                    found_idx = start + match_idx
                    found = True
                    break

            if found:
                print(f"      Step {i}: miner's U matches step_index={found_idx} (expected {i}, diff={found_idx - i})")
            else:
                print(f"      Step {i}: NO MATCH in range 0-{search_range-1}")

        # Special analysis: Check if steps 1-14 form a coherent sequence from a different starting point
        print(f"\n    Analyzing if mismatch U values form coherent sequence...")
        mismatch_expected = expected_u[mismatched_idx].cpu().numpy()
        # Check if these match computed_u for any contiguous range
        for start_offset in range(-20, 21):
            if start_offset == 0:
                continue
            match_count = 0
            for j, bad_step in enumerate(mismatched_idx):
                test_step = bad_step.item() + start_offset
                if 0 <= test_step < window_size:
                    if abs(expected_u[bad_step].item() - computed_u[test_step].item()) <= tolerance:
                        match_count += 1
            if match_count > 5:
                print(f"      Offset {start_offset:+d}: {match_count}/{len(mismatched_idx)} matches")

        # Check if the miner's ring buffer had a different 'pos' value
        print(f"\n    Testing ring buffer position offset hypothesis...")
        # The miner might have had a different starting position in the ring buffer
        # This would shift which tokens are included in the context window
        for pos_offset in range(-20, 21):
            if pos_offset == 0:
                continue
            matches_with_offset = 0
            for bad_step in mismatched_idx[:5]:
                i = bad_step.item()
                bad_expected = expected_u[i].item()
                # Build context as if ring buffer position was offset
                effective_step = i + pos_offset
                if 0 <= effective_step < window_size:
                    offset_ctx = torch.cat([prompt_tokens, chosen_tokens[:effective_step]])
                    u_test = compute_u_single(header_prefix, vdf, tick, i, offset_ctx, stated_precision, device)
                    if abs(u_test.item() - bad_expected) <= tolerance:
                        matches_with_offset += 1
            if matches_with_offset > 0:
                print(f"      Position offset {pos_offset:+d}: {matches_with_offset}/5 matches")

    num_matches = matches.sum().item()
    num_mismatches = (~matches).sum().item()

    print(f"    Tolerance: {tolerance}")
    print(f"    Matches: {num_matches}/{window_size}")
    print(f"    Mismatches: {num_mismatches}/{window_size}")

    if num_mismatches > 0:
        mismatched_indices = (~matches).nonzero(as_tuple=True)[0]
        print(f"\n[5] MISMATCH DETAILS:")
        print(f"    Max difference: {diff.max().item():.10e}")
        print(f"    Mean difference: {diff.mean().item():.10e}")

        # Analyze mismatch pattern
        mismatch_list = mismatched_indices.tolist()
        if len(mismatch_list) > 0:
            first_mismatch = mismatch_list[0]
            last_mismatch = mismatch_list[-1]
            print(f"    Mismatch range: steps {first_mismatch} to {last_mismatch}")
            if last_mismatch - first_mismatch + 1 == len(mismatch_list):
                print(f"    Pattern: CONSECUTIVE mismatches from {first_mismatch} to {last_mismatch}")

        # Show first few mismatches
        print(f"\n    First {min(10, num_mismatches)} mismatches:")
        for idx in mismatched_indices[:10]:
            i = idx.item()
            print(f"      Step {i}: expected={expected_u[i].item():.10e}, "
                  f"computed={computed_u[i].item():.10e}, "
                  f"diff={diff[i].item():.10e}")
            if verbose:
                ctx = all_contexts[i]
                print(f"               context_len={len(ctx)}, last_5_tokens={ctx[-5:].tolist()}")

        if verbose and num_mismatches > 10:
            print(f"\n    Last 10 mismatches:")
            for idx in mismatched_indices[-10:]:
                i = idx.item()
                print(f"      Step {i}: expected={expected_u[i].item():.10e}, "
                      f"computed={computed_u[i].item():.10e}, "
                      f"diff={diff[i].item():.10e}")

        # Check if it's a pattern (e.g., all wrong, or specific steps)
        if num_mismatches == window_size:
            print("\n    ⚠️  ALL U values mismatch - likely a fundamental computation issue")
            print("       Check: header_prefix, vdf, tick, precision encoding")
        elif num_mismatches > window_size // 2:
            print(f"\n    ⚠️  >50% mismatch rate - likely systematic issue")

        # Debug: show raw bytes for first mismatch
        if verbose and len(mismatched_indices) > 0:
            first_mismatch = mismatched_indices[0].item()
            print(f"\n    Debug info for step {first_mismatch}:")
            print(f"      Context length: {len(all_contexts[first_mismatch])}")
            print(f"      Window tokens shape: {window_tokens[first_mismatch].shape}")
            print(f"      Tick: {tick}")
            print(f"      Stated precision: {stated_precision}")

        return False
    else:
        print("\n[5] ✓ All U values match!")
        return True

def main():
    parser = argparse.ArgumentParser(description="Debug proof U value verification")
    parser.add_argument("proof_file", help="Path to the proof .bin file")
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose output")
    parser.add_argument("--cpu", action="store_true", help="Use CPU instead of CUDA")
    args = parser.parse_args()

    device = 'cpu' if args.cpu else 'cuda'

    print("="*60)
    print("  PROOF U VALUE DEBUG TOOL")
    print("="*60)

    # Check file exists
    if not os.path.exists(args.proof_file):
        print(f"\n✗ File not found: {args.proof_file}")
        sys.exit(1)

    file_size = os.path.getsize(args.proof_file)
    print(f"\nFile: {args.proof_file}")
    print(f"Size: {file_size} bytes")

    # Read file
    with open(args.proof_file, 'rb') as f:
        buf = f.read()

    # Try all unpack methods
    proof_dict, method_used = try_all_unpack_methods(buf, verbose=args.verbose)

    if proof_dict is None:
        print(f"\n✗ Failed to unpack with any method")
        sys.exit(1)

    print(f"\n✓ Successfully unpacked using: {method_used}")

    # Show proof info
    print(f"\nProof info:")
    print(f"  Model: {proof_dict.get('model_identifier', 'N/A')}")
    print(f"  Hash: {proof_dict.get('hash', 'N/A')}")
    print(f"  Temperature: {proof_dict.get('temperature', 'N/A')}")
    print(f"  Is solution: {proof_dict.get('is_solution', 'N/A')}")
    print(f"  Window size: {len(proof_dict.get('expected_u', []))}")
    print(f"  Chosen tokens: {len(proof_dict.get('chosen_tokens', []))}")

    if args.verbose:
        print(f"  All keys: {list(proof_dict.keys())}")

    # Run U value debug
    result = debug_u_values(proof_dict, device=device, verbose=args.verbose)

    print("\n" + "="*60)
    if result is None:
        print("  RESULT: CANNOT VERIFY - No expected U values in proof")
    elif result:
        print("  RESULT: U VALUE VERIFICATION PASSED ✓")
    else:
        print("  RESULT: U VALUE VERIFICATION FAILED ✗")
    print("="*60)

    sys.exit(0 if result else 1)

if __name__ == "__main__":
    main()
