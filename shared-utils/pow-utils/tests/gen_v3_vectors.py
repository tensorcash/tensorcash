#!/usr/bin/env python3
"""Generate the golden cross-language test vectors for pow_v3 (TIP-0003).

Run from shared-utils/pow-utils:
    python tests/gen_v3_vectors.py
writes tests/vectors/v3_vectors.json. The Python side asserts against these in
test_pow_v3_vectors.py; the C++ side must reproduce every value bit-exactly.

Deterministic by construction (fixed inputs, no clocks, no RNG seeds beyond
explicit constants) so regeneration is diff-clean unless semantics change.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pow_v3

HEADER = bytes(range(76))
VDF = bytes(range(100, 132))
NONCE = bytes(range(32))


def step_vectors():
    cases = [
        {"name": "short_ctx_no_nonce", "tick": 7, "step": 0,
         "context_tokens": [1, 2, 3], "precision": "fp16", "nonce": None},
        {"name": "short_ctx_with_nonce", "tick": 7, "step": 0,
         "context_tokens": [1, 2, 3], "precision": "fp16", "nonce": NONCE},
        {"name": "mid_window_with_nonce", "tick": 123456789, "step": 200,
         "context_tokens": list(range(1, 300)), "precision": "bf16",
         "nonce": NONCE},
        {"name": "full_window_boundary", "tick": 2**32 - 1, "step": 255,
         "context_tokens": list(range(1000, 1256)), "precision": "fp8_e4m3",
         "nonce": bytes(reversed(NONCE))},
    ]
    out = []
    for c in cases:
        msg = pow_v3.build_step_message(
            HEADER, VDF, c["tick"], c["step"], c["context_tokens"],
            c["precision"], admission_nonce=c["nonce"])
        u, digest = pow_v3.step_u_from_message(msg)
        out.append({
            "name": c["name"],
            "header_prefix": HEADER.hex(),
            "vdf": VDF.hex(),
            "tick": c["tick"],
            "step": c["step"],
            "context_tokens": c["context_tokens"],
            "precision": c["precision"],
            "admission_nonce": c["nonce"].hex() if c["nonce"] else None,
            "message_sha256": digest.hex(),
            "message_len": len(msg),
            "u_double": u,
        })
    return out


def admission_vectors():
    msg_w = pow_v3.build_step_message(HEADER, VDF, 7, 0, [1, 2, 3], "fp16")
    # Full model-visible prefix (§6): prompt_tokens + pad_mask as carried in
    # the proof; the commitment — not the raw prefix — enters the Argon input.
    base_prompt = list(range(1, 4))
    base_mask = [False, False, True]
    cases = [
        {"name": "ref_model", "model_identifier": "org/model@abcdef012345",
         "nonce": NONCE, "prompt_tokens": base_prompt, "pad_mask": base_mask},
        {"name": "empty_model_id", "model_identifier": "", "nonce": NONCE,
         "prompt_tokens": base_prompt, "pad_mask": base_mask},
        {"name": "other_nonce", "model_identifier": "org/model@abcdef012345",
         "nonce": bytes(32), "prompt_tokens": base_prompt,
         "pad_mask": base_mask},
        {"name": "long_prefix_beyond_window",
         "model_identifier": "org/model@abcdef012345", "nonce": NONCE,
         "prompt_tokens": list(range(1, 400)), "pad_mask": [False] * 399},
        {"name": "empty_prefix",
         "model_identifier": "org/model@abcdef012345", "nonce": NONCE,
         "prompt_tokens": [], "pad_mask": []},
    ]
    out = []
    for c in cases:
        commitment = pow_v3.prompt_commitment(c["prompt_tokens"], c["pad_mask"])
        m = pow_v3.admission_message(msg_w, c["model_identifier"], c["nonce"],
                                     commitment)
        digest = pow_v3.argon2id_digest(m)
        out.append({
            "name": c["name"],
            "window_first_step_message_sha256":
                pow_v3.step_u_from_message(msg_w)[1].hex(),
            "model_identifier": c["model_identifier"],
            "admission_nonce": c["nonce"].hex(),
            "prompt_tokens": c["prompt_tokens"],
            "pad_mask": c["pad_mask"],
            "prompt_commitment": commitment.hex(),
            "admission_message_len": len(m),
            "argon2_profile": {
                "time_cost": pow_v3.ARGON2_TIME_COST,
                "memory_kib": pow_v3.ARGON2_MEMORY_KIB,
                "lanes": pow_v3.ARGON2_LANES,
                "hash_len": pow_v3.ARGON2_HASH_LEN,
                "salt": pow_v3.ARGON2_SALT.decode("ascii"),
            },
            "argon2id_digest": digest.hex(),
            "digest_uint256_le": str(int.from_bytes(digest, "little")),
        })
    return out


def target_vectors():
    diffs = [1, 100_000, 500_000, 1_000_000, 3_000_000, 10**12, 10**15]
    out = []
    for d in diffs:
        tries = pow_v3.admission_expected_tries(d)
        target = pow_v3.admission_target(d)
        out.append({
            "difficulty": d,
            "normalizer": pow_v3.MODEL_DIFFICULTY_NORMALIZER,
            "decode_us_at_normalizer": pow_v3.DECODE_US_AT_NORMALIZER,
            "argon_ref_us": pow_v3.ARGON_REF_US,
            "elig_alpha": [pow_v3.ELIG_ALPHA_NUM, pow_v3.ELIG_ALPHA_DEN],
            "expected_tries": tries,
            "admission_target_hex_be": format(target, "064x"),
        })
    return out


def b_cred_vectors():
    cases = [
        {"name": "uniform_quarter_mass",
         "lower": [0.1] * 256,
         "upper": [0.35 - 2 * pow_v3.ATOL] * 256},   # exactly 2 bits/step
        {"name": "mixed_masses",
         "lower": [i / 1000.0 for i in range(256)],
         "upper": [i / 1000.0 + 0.001 + (i % 7) / 100.0 for i in range(256)]},
        {"name": "point_bounds_atol_only",
         "lower": [0.5] * 256,
         "upper": [0.5] * 256},                      # mass = 2*ATOL each
        {"name": "full_mass_zero_bits",
         "lower": [0.0] * 256,
         "upper": [1.0] * 256},
    ]
    out = []
    for c in cases:
        units = pow_v3.b_cred_units_from_bounds(c["lower"], c["upper"])
        out.append({
            "name": c["name"],
            "atol": pow_v3.ATOL,
            "lower": c["lower"],
            "upper": c["upper"],
            "b_cred_units": str(units),
            "b_cred_bits": pow_v3.b_cred_bits(units),
            "tier": pow_v3.tier_for_b_cred_units(units),
        })
    return out


def carrier_vectors():
    """extra_flags parser vectors (§3 parser bounds): input string ->
    extracted nonce hex or None ("no nonce claimed"). Never an error."""
    good = '{"v3":{"admission_nonce":"%s"}}' % NONCE.hex()
    cases = [
        ("canonical", good, NONCE.hex()),
        ("non_canonical_whitespace",
         '{ "z": 1 , "v3" : { "admission_nonce" : "%s" } }' % NONCE.hex(),
         NONCE.hex()),
        ("extra_keys_preserved",
         '{"completion_id":"x","proof_purpose":"audit","v3":{"admission_nonce":"%s"}}'
         % NONCE.hex(), NONCE.hex()),
        ("empty", "", None),
        ("pformat_blob", "{'completion_id': 'x'}", None),
        ("non_object", "[1,2,3]", None),
        ("v3_key_absent", '{"v3":{}}', None),
        ("v3_not_object", '{"v3":"str"}', None),
        ("uppercase_hex", '{"v3":{"admission_nonce":"%s"}}' % ("A" * 64), None),
        ("wrong_length_63", '{"v3":{"admission_nonce":"%s"}}' % ("a" * 63), None),
        ("wrong_length_65", '{"v3":{"admission_nonce":"%s"}}' % ("a" * 65), None),
        ("non_hex", '{"v3":{"admission_nonce":"%s"}}' % ("g" * 64), None),
        ("non_string_nonce", '{"v3":{"admission_nonce":123}}', None),
        ("duplicate_top_key",
         '{"a":1,"a":2,"v3":{"admission_nonce":"%s"}}' % NONCE.hex(), None),
        ("duplicate_nested_key",
         '{"v3":{"admission_nonce":"%s","admission_nonce":"%s"}}'
         % (NONCE.hex(), NONCE.hex()), None),
        ("oversized",
         '{"pad":"%s","v3":{"admission_nonce":"%s"}}'
         % ("x" * pow_v3.EXTRA_FLAGS_MAX_BYTES, NONCE.hex()), None),
        ("too_deep",
         '{"deep":%s,"v3":{"admission_nonce":"%s"}}'
         % ('{"d":' * (pow_v3.EXTRA_FLAGS_MAX_DEPTH + 1) + "1"
            + "}" * (pow_v3.EXTRA_FLAGS_MAX_DEPTH + 1), NONCE.hex()), None),
    ]
    out = []
    for name, flags, expected in cases:
        got = pow_v3.extract_admission_nonce(flags)
        got_hex = got.hex() if got else None
        assert got_hex == expected, f"carrier vector {name}: {got_hex} != {expected}"
        out.append({"name": name, "extra_flags": flags,
                    "admission_nonce": expected})
    return out


def main():
    vectors = {
        "_generator": "tests/gen_v3_vectors.py",
        "_spec": "TIP-0003",
        "constants": {
            "pow_window_size": pow_v3.POW_WINDOW_SIZE,
            "b_floor_bits": pow_v3.B_FLOOR_BITS,
            "b_free_bits": pow_v3.B_FREE_BITS,
            "bcred_r": pow_v3.BCRED_R,
            "bcred_n_max": pow_v3.BCRED_N_MAX,
            "bcred_table_sha256": pow_v3.BCRED_TABLE_SHA256,
            "atol": pow_v3.ATOL,
            "atol_q63_ceil": str(pow_v3.ATOL_Q63_CEIL),
            "extra_flags_max_bytes": pow_v3.EXTRA_FLAGS_MAX_BYTES,
            "extra_flags_max_depth": pow_v3.EXTRA_FLAGS_MAX_DEPTH,
            "prompt_ctx_tag": pow_v3.PROMPT_CTX_TAG.decode("ascii"),
        },
        "step_hash": step_vectors(),
        "admission": admission_vectors(),
        "target_derivation": target_vectors(),
        "b_cred": b_cred_vectors(),
        "carrier": carrier_vectors(),
    }
    out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vectors")
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, "v3_vectors.json")
    with open(out_path, "w") as f:
        json.dump(vectors, f, indent=1, sort_keys=True)
        f.write("\n")
    print(f"wrote {out_path}")


if __name__ == "__main__":
    main()
