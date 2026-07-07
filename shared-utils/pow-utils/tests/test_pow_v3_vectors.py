"""Assert pow_v3 reproduces the golden cross-language vectors bit-exactly.

The same vectors file (tests/vectors/v3_vectors.json) is consumed by the C++
equivalence tests; regenerate ONLY on an intentional semantic change
(tests/gen_v3_vectors.py) and update both languages together.
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pow_v3

VECTORS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "vectors", "v3_vectors.json")

with open(VECTORS_PATH) as f:
    VECTORS = json.load(f)

try:
    import argon2  # noqa: F401
    HAVE_ARGON2 = True
except ImportError:
    HAVE_ARGON2 = False


def test_constants_pinned():
    c = VECTORS["constants"]
    assert c["pow_window_size"] == pow_v3.POW_WINDOW_SIZE
    assert c["b_floor_bits"] == pow_v3.B_FLOOR_BITS
    assert c["b_free_bits"] == pow_v3.B_FREE_BITS
    assert c["bcred_r"] == pow_v3.BCRED_R
    assert c["bcred_n_max"] == pow_v3.BCRED_N_MAX
    assert c["bcred_table_sha256"] == pow_v3.BCRED_TABLE_SHA256
    assert int(c["atol_q63_ceil"]) == pow_v3.ATOL_Q63_CEIL
    assert c["atol"] == pow_v3.ATOL


@pytest.mark.parametrize("case", VECTORS["step_hash"],
                         ids=[c["name"] for c in VECTORS["step_hash"]])
def test_step_hash_vectors(case):
    nonce = bytes.fromhex(case["admission_nonce"]) if case["admission_nonce"] else None
    msg = pow_v3.build_step_message(
        bytes.fromhex(case["header_prefix"]), bytes.fromhex(case["vdf"]),
        case["tick"], case["step"], case["context_tokens"], case["precision"],
        admission_nonce=nonce)
    u, digest = pow_v3.step_u_from_message(msg)
    assert len(msg) == case["message_len"]
    assert digest.hex() == case["message_sha256"]
    assert u == case["u_double"]


@pytest.mark.skipif(not HAVE_ARGON2, reason="argon2-cffi unavailable")
@pytest.mark.parametrize("case", VECTORS["admission"],
                         ids=[c["name"] for c in VECTORS["admission"]])
def test_admission_vectors(case):
    p = case["argon2_profile"]
    assert p["time_cost"] == pow_v3.ARGON2_TIME_COST
    assert p["memory_kib"] == pow_v3.ARGON2_MEMORY_KIB
    assert p["lanes"] == pow_v3.ARGON2_LANES
    assert p["salt"].encode("ascii") == pow_v3.ARGON2_SALT

    msg_w = pow_v3.build_step_message(
        bytes.fromhex(VECTORS["step_hash"][0]["header_prefix"]),
        bytes.fromhex(VECTORS["step_hash"][0]["vdf"]), 7, 0, [1, 2, 3], "fp16")
    commitment = pow_v3.prompt_commitment(case["prompt_tokens"],
                                          case["pad_mask"])
    assert commitment.hex() == case["prompt_commitment"]
    m = pow_v3.admission_message(msg_w, case["model_identifier"],
                                 bytes.fromhex(case["admission_nonce"]),
                                 commitment)
    assert len(m) == case["admission_message_len"]
    digest = pow_v3.argon2id_digest(m)
    assert digest.hex() == case["argon2id_digest"]
    assert int.from_bytes(digest, "little") == int(case["digest_uint256_le"])


@pytest.mark.parametrize("case", VECTORS["target_derivation"],
                         ids=[str(c["difficulty"]) for c in VECTORS["target_derivation"]])
def test_target_vectors(case):
    tries = pow_v3.admission_expected_tries(
        case["difficulty"], normalizer=case["normalizer"],
        decode_us_at_normalizer=case["decode_us_at_normalizer"],
        elig_alpha_num=case["elig_alpha"][0],
        elig_alpha_den=case["elig_alpha"][1],
        argon_ref_us=case["argon_ref_us"])
    assert tries == case["expected_tries"]
    target = pow_v3.admission_target(case["difficulty"])
    assert format(target, "064x") == case["admission_target_hex_be"]


@pytest.mark.parametrize("case", VECTORS["b_cred"],
                         ids=[c["name"] for c in VECTORS["b_cred"]])
def test_b_cred_vectors(case):
    units = pow_v3.b_cred_units_from_bounds(case["lower"], case["upper"])
    assert units == int(case["b_cred_units"])
    assert pow_v3.tier_for_b_cred_units(units) == case["tier"]
