#!/usr/bin/env python3
"""OFFLINE-REPLAY SOAK for the v3 prompt-binding admission puzzle (TIP-0003).

Proves, WITHOUT torch/pfunpack/GPU, that a nonce ground the way a MINER grinds
it (real Argon2id grind to the admission target) is ACCEPTED by the exact
admission math the committed VERIFIER runs. proof_verifier._verify_v3_admission_tier
(services/verification-api, proof_verifier.py:3636-3667) delegates to precisely
these pow_v3 primitives — admission_message / argon2id_digest / admission_target /
admission_valid — so replaying through pow_v3.py here faithfully replays the
verifier's admission verdict.

For every case it:
  PRODUCE  build the admission preimage via pow_v3.build_admission_preimage
           (the single source of truth the native grinder consumes) and grind a
           pure-Python nonce loop until admission_valid holds.
  VERIFY   independently recompute admission_message + argon2id_digest +
           admission_target and assert admission_valid is True on the ground nonce
           (the verifier's exact acceptance).
  NEGATIVE flip one nonce byte and assert the verdict flips to False (retrying
           tampers until a rejecting one is found — a random tamper passing the
           puzzle is astronomically unlikely but the retry makes the control exact).
  CARRIER  merge_extra_flags_v3(nonce_hex) then extract_admission_nonce round-trips
           to the same 32 raw bytes (§3 producer<->consumer).

Emits tests/vectors/soak_grinded_cases.json so the C++ side (tests/soak_v3_cpp_check.cpp
via tests/gen_soak_cases_header.py) can independently verify the SAME ground nonces —
the Python==C++ proof on non-trivial, really-ground inputs, not just fixed vectors.

Standalone: run directly with your Python environment
    python3 tests/soak_v3_offline_replay.py
It is also import-clean for pytest (a thin test_ wrapper drives run_soak()).
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pow_v3

OUT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "vectors", "soak_grinded_cases.json")

# Safety cap: grinding is a geometric process with mean == expected_tries, so a
# 100x cap essentially never fires (P(miss) ~ e^-100); it only guards against a
# logic bug producing an unsatisfiable puzzle rather than an honest miss.
MAX_TRIES_FACTOR = 100

# Realistic proof contexts spanning difficulty tiers. `difficulty` is an INVERSE
# compute scalar (expected_tries = 6e7 / difficulty at the default constants):
#   750_000 -> 80, 1_000_000 -> 60 (the normalizer), 2_000_000 -> 30,
#   3_000_000 -> 20, 6_000_000 -> 10. Contexts vary every explicit input so a
# byte-swap anywhere would surface as a different msg_w / commitment.
CASES = [
    {
        "name": "normalizer_short_ctx",
        "difficulty": 1_000_000,
        "header_prefix": bytes(range(76)),
        "vdf": bytes(range(100, 132)),
        "tick": 7, "step": 0,
        "context_tokens": [1, 2, 3],
        "precision": "fp16",
        "prefix_tokens": [1, 2, 3],
        "prefix_pad_mask": [False, False, True],
        "model_identifier": "org/model@abcdef012345",
    },
    {
        "name": "low_diff_more_tries_bf16",
        "difficulty": 750_000,
        "header_prefix": bytes((i * 3 + 5) & 0xFF for i in range(76)),
        "vdf": bytes((i * 7 + 1) & 0xFF for i in range(32)),
        "tick": 123456789, "step": 200,
        "context_tokens": list(range(1, 300)),
        "precision": "bf16",
        "prefix_tokens": list(range(1, 400)),
        "prefix_pad_mask": [False] * 399,
        "model_identifier": "qwen/qwen3-8b@0011223344556677",
    },
    {
        "name": "mid_diff_full_window_fp8",
        "difficulty": 2_000_000,
        "header_prefix": bytes((255 - i) & 0xFF for i in range(76)),
        "vdf": bytes((i * 11) & 0xFF for i in range(32)),
        "tick": 2**32 - 1, "step": 255,
        "context_tokens": list(range(1000, 1256)),
        "precision": "fp8_e4m3",
        "prefix_tokens": list(range(500, 900)),
        "prefix_pad_mask": [(i % 3 == 0) for i in range(400)],
        "model_identifier": "org/model@abcdef012345",
    },
    {
        "name": "high_diff_empty_prefix",
        "difficulty": 3_000_000,
        "header_prefix": bytes((i ^ 0x5A) & 0xFF for i in range(76)),
        "vdf": bytes((i ^ 0xA5) & 0xFF for i in range(32)),
        "tick": 42, "step": 17,
        "context_tokens": [9, 8, 7, 6, 5],
        "precision": "fp16",
        "prefix_tokens": [],
        "prefix_pad_mask": [],
        "model_identifier": "",
    },
    {
        "name": "very_high_diff_few_tries",
        "difficulty": 6_000_000,
        "header_prefix": bytes((i * 13 + 2) & 0xFF for i in range(76)),
        "vdf": bytes((i * 5 + 9) & 0xFF for i in range(32)),
        "tick": 999, "step": 100,
        "context_tokens": list(range(2000, 2100)),
        "precision": "bf16",
        "prefix_tokens": list(range(3000, 3050)),
        "prefix_pad_mask": [True] * 50,
        "model_identifier": "org/another-model@deadbeefcafef00d",
    },
]


def _grind(msg_w, model_identifier, target, commitment, cap):
    """Pure-Python miner grind: increment a little-endian counter nonce (matches
    the native grinder's carry semantics) until argon2id_digest satisfies the
    target, or `cap` tries. Returns (nonce_bytes, digest_bytes, tries)."""
    nonce = 0
    for t in range(1, cap + 1):
        nonce_bytes = nonce.to_bytes(pow_v3.ADMISSION_NONCE_BYTES, "little")
        msg = pow_v3.admission_message(msg_w, model_identifier, nonce_bytes,
                                       commitment)
        digest = pow_v3.argon2id_digest(msg)
        if pow_v3.admission_valid(digest, target):
            return nonce_bytes, digest, t
        nonce += 1
    return None, None, cap


def _verify(msg_w, model_identifier, nonce_bytes, commitment, difficulty):
    """Independent verifier-side recomputation (mirrors the verifier delegating
    to pow_v3): returns (admission_valid_bool, digest_bytes, target_int)."""
    target = pow_v3.admission_target(difficulty)
    msg = pow_v3.admission_message(msg_w, model_identifier, nonce_bytes,
                                   commitment)
    digest = pow_v3.argon2id_digest(msg)
    return pow_v3.admission_valid(digest, target), digest, target


def _find_rejecting_tamper(msg_w, model_identifier, nonce_bytes, commitment,
                           difficulty):
    """Flip one byte of the accepted nonce and return the first tamper whose
    verdict is False, plus its digest. A random tamper re-satisfying the puzzle
    is ~1/expected_tries likely, so a handful of flips always yields a rejecter.
    Returns (tamper_bytes, tamper_digest, tamper_valid_bool)."""
    for i in range(pow_v3.ADMISSION_NONCE_BYTES):
        for delta in (1, 2, 4, 8, 16, 32, 64, 128):
            tampered = bytearray(nonce_bytes)
            tampered[i] ^= delta
            tb = bytes(tampered)
            if tb == nonce_bytes:
                continue
            valid, digest, _ = _verify(msg_w, model_identifier, tb, commitment,
                                       difficulty)
            if not valid:
                return tb, digest, valid
    # Exhausted single-byte single-bit flips without a rejecter — impossible in
    # practice; surface as a failure by returning the last (still-accepting) one.
    return tb, digest, valid


def run_soak(verbose=True):
    results = []
    n_accepted = 0
    n_tamper_rejected = 0
    n_carrier_ok = 0

    for c in CASES:
        difficulty = c["difficulty"]
        expected_tries = pow_v3.admission_expected_tries(difficulty)
        cap = expected_tries * MAX_TRIES_FACTOR

        # PRODUCE: the exact 5-tuple the native grinder consumes.
        msg_w, model_identifier, target_le, max_tries, commitment = \
            pow_v3.build_admission_preimage(
                header_prefix=c["header_prefix"], vdf=c["vdf"],
                tick=c["tick"], step=c["step"],
                context_tokens=c["context_tokens"], prefix_tokens=c["prefix_tokens"],
                prefix_pad_mask=c["prefix_pad_mask"], precision=c["precision"],
                difficulty=difficulty, model_identifier=c["model_identifier"])
        target_int = int.from_bytes(target_le, "little")
        assert target_int == pow_v3.admission_target(difficulty), \
            f"[{c['name']}] target_le disagrees with admission_target"
        assert max_tries == expected_tries, \
            f"[{c['name']}] build_admission_preimage max_tries {max_tries} != {expected_tries}"

        nonce_bytes, grind_digest, tries = _grind(
            msg_w, model_identifier, target_int, commitment, cap)
        assert nonce_bytes is not None, \
            f"[{c['name']}] NO admissible nonce within {cap} tries " \
            f"(expected ~{expected_tries}) — puzzle likely unsatisfiable"

        # VERIFY (independent recompute — the verifier's exact acceptance).
        valid, digest, vtarget = _verify(msg_w, model_identifier, nonce_bytes,
                                          commitment, difficulty)
        assert vtarget == target_int
        assert digest == grind_digest, f"[{c['name']}] verify digest != grind digest"
        assert valid is True, f"[{c['name']}] verifier REJECTED a ground nonce"
        n_accepted += 1

        # NEGATIVE control.
        tamper_bytes, tamper_digest, tamper_valid = _find_rejecting_tamper(
            msg_w, model_identifier, nonce_bytes, commitment, difficulty)
        assert tamper_valid is False, \
            f"[{c['name']}] could not find a rejecting single-bit tamper"
        n_tamper_rejected += 1

        # CARRIER round-trip: producer merge -> consumer extraction.
        nonce_hex = nonce_bytes.hex()
        merged = pow_v3.merge_extra_flags_v3(
            {"completion_id": "soak", "proof_purpose": "audit"}, nonce_hex)
        extracted = pow_v3.extract_admission_nonce(merged)
        assert extracted == nonce_bytes, \
            f"[{c['name']}] carrier round-trip lost the nonce"
        n_carrier_ok += 1

        results.append({
            "name": c["name"],
            "difficulty": difficulty,
            "expected_tries": expected_tries,
            "tries_used": tries,
            "header_prefix": bytes(c["header_prefix"]).hex(),
            "vdf": bytes(c["vdf"]).hex(),
            "tick": c["tick"],
            "step": c["step"],
            "context_tokens": list(c["context_tokens"]),
            "precision": c["precision"],
            "prefix_tokens": list(c["prefix_tokens"]),
            "prefix_pad_mask": [bool(b) for b in c["prefix_pad_mask"]],
            "model_identifier": c["model_identifier"],
            "msg_w": msg_w.hex(),
            "prompt_commitment": commitment.hex(),
            "admission_target_le": target_le.hex(),
            "admission_target_hex_be": format(target_int, "064x"),
            "admission_nonce": nonce_hex,
            "digest": digest.hex(),
            "digest_uint256_le": str(int.from_bytes(digest, "little")),
            "real_valid": bool(valid),
            "tamper_nonce": tamper_bytes.hex(),
            "tamper_digest": tamper_digest.hex(),
            "tamper_valid": bool(tamper_valid),
        })

        if verbose:
            print(f"  [{c['name']:<28}] diff={difficulty:>9} "
                  f"exp_tries={expected_tries:>4} used={tries:>4}  "
                  f"ACCEPT ok  tamper REJECT ok  carrier ok")

    out = {
        "_generator": "tests/soak_v3_offline_replay.py",
        "_spec": "TIP-0003 offline replay soak",
        "constants": {
            "argon2_time_cost": pow_v3.ARGON2_TIME_COST,
            "argon2_memory_kib": pow_v3.ARGON2_MEMORY_KIB,
            "argon2_lanes": pow_v3.ARGON2_LANES,
            "argon2_hash_len": pow_v3.ARGON2_HASH_LEN,
            "argon2_salt": pow_v3.ARGON2_SALT.decode("ascii"),
            "normalizer": pow_v3.MODEL_DIFFICULTY_NORMALIZER,
            "decode_us_at_normalizer": pow_v3.DECODE_US_AT_NORMALIZER,
            "argon_ref_us": pow_v3.ARGON_REF_US,
            "elig_alpha_num": pow_v3.ELIG_ALPHA_NUM,
            "elig_alpha_den": pow_v3.ELIG_ALPHA_DEN,
        },
        "cases": results,
    }
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w") as f:
        json.dump(out, f, indent=1, sort_keys=True)
        f.write("\n")

    n = len(results)
    all_accepted = n_accepted == n
    all_rejected = n_tamper_rejected == n
    all_carrier = n_carrier_ok == n
    ok = all_accepted and all_rejected and all_carrier
    print()
    print(f"wrote {OUT_PATH}")
    print(f"SOAK SUMMARY: {n} cases")
    print(f"  ground nonces Python-ACCEPTED : {n_accepted}/{n} "
          f"{'PASS' if all_accepted else 'FAIL'}")
    print(f"  tampers Python-REJECTED       : {n_tamper_rejected}/{n} "
          f"{'PASS' if all_rejected else 'FAIL'}")
    print(f"  carrier round-trip OK         : {n_carrier_ok}/{n} "
          f"{'PASS' if all_carrier else 'FAIL'}")
    print(f"  OVERALL: {'PASS' if ok else 'FAIL'}")
    return ok, out


if __name__ == "__main__":
    ok, _ = run_soak()
    sys.exit(0 if ok else 1)
