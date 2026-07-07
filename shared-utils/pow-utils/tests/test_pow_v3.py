"""Tests for pow_v3 — V3 prompt-binding / admission helpers (TIP-0003).

Torch-free except the explicit equivalence tests against pow_utils' tensor
paths, which are skipped when torch is unavailable.
"""

import json
import math
import os
import random
import sys
from hashlib import sha256

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pow_v3
from pow_v3 import (
    ADMISSION_NONCE_BYTES,
    ATOL,
    ATOL_Q63_CEIL,
    BCRED_N_MAX,
    BCRED_Q_ONE,
    BCRED_R,
    B_FLOOR_UNITS,
    B_FREE_UNITS,
    TIER_ADMISSION,
    TIER_FREE,
    TIER_INVALID,
    UINT256_MAX,
    admission_expected_tries,
    admission_message,
    admission_target,
    admission_valid,
    b_cred_units_from_bounds,
    build_step_message,
    canonical_json,
    credit_units_for_step,
    extract_admission_nonce,
    is_valid_admission_nonce_hex,
    mass_q63_for_step,
    merge_extra_flags_v3,
    step_u_from_message,
    tier_for_b_cred_units,
)

try:
    import torch  # noqa: F401
    HAVE_TORCH = True
except ImportError:
    HAVE_TORCH = False

try:
    from argon2.low_level import hash_secret_raw  # noqa: F401
    HAVE_ARGON2 = True
except ImportError:
    HAVE_ARGON2 = False

NONCE = bytes(range(32))
NONCE_HEX = NONCE.hex()


# --------------------------------------------------------------------------- #
# §3 — extra_flags carrier
# --------------------------------------------------------------------------- #

class TestExtraFlagsCarrier:
    def test_merge_into_empty(self):
        out = merge_extra_flags_v3(None, NONCE_HEX)
        assert json.loads(out) == {"v3": {"admission_nonce": NONCE_HEX}}
        # canonical: sorted keys, no spaces
        assert out == canonical_json(json.loads(out))

    def test_merge_preserves_existing_keys(self):
        existing = {"completion_id": "abc", "proof_purpose": "audit",
                    "dtype": "bf16"}
        out = json.loads(merge_extra_flags_v3(existing, NONCE_HEX))
        assert out["completion_id"] == "abc"
        assert out["proof_purpose"] == "audit"
        assert out["dtype"] == "bf16"
        assert out["v3"]["admission_nonce"] == NONCE_HEX

    def test_merge_from_json_string(self):
        out = json.loads(
            merge_extra_flags_v3('{"completion_id":"x"}', NONCE_HEX))
        assert out["completion_id"] == "x"
        assert out["v3"]["admission_nonce"] == NONCE_HEX

    def test_merge_preserves_non_json_blob(self):
        # Legacy pformat blob (single quotes: not JSON) must not be dropped.
        blob = "{'completion_id': 'x'}"
        out = json.loads(merge_extra_flags_v3(blob, NONCE_HEX))
        assert out["_diff"] == blob
        assert out["v3"]["admission_nonce"] == NONCE_HEX

    def test_merge_is_idempotent_and_keeps_other_v3_keys(self):
        first = merge_extra_flags_v3({"v3": {"other": 1}}, "a" * 64)
        second = json.loads(merge_extra_flags_v3(first, NONCE_HEX))
        assert second["v3"]["admission_nonce"] == NONCE_HEX
        assert second["v3"]["other"] == 1

    def test_merge_rejects_bad_nonce(self):
        for bad in ("A" * 64, "a" * 63, "a" * 65, "g" * 64, "", None, 5):
            with pytest.raises((ValueError, TypeError)):
                merge_extra_flags_v3({}, bad)

    def test_extract_roundtrip(self):
        assert extract_admission_nonce(
            merge_extra_flags_v3({"k": "v"}, NONCE_HEX)) == NONCE

    def test_extract_no_claim(self):
        # §3 parser bounds: ANY violation => no nonce claimed (None), never a
        # raise. Absent v3 / absent key / unparseable / non-object / bad shape.
        for flags in (None, "", "   ", "not json at all",
                      "{'pformat': 'blob'}",       # python literal, not JSON
                      "[1,2,3]", '"a string"', "{}",
                      '{"other":1}',
                      '{"v3":{}}',                 # v3 object, key absent
                      '{"v3":[1]}', '{"v3":"str"}', '{"v3":null}'):
            assert extract_admission_nonce(flags) is None

    def test_extract_malformed_present_key_means_no_claim(self):
        # Key PRESENT but wrong shape => no nonce claimed (§3 parser bounds);
        # the tier rule then rejects below B_FREE, accepts in the free tier.
        for bad in ('"' + "A" * 64 + '"', '"' + "a" * 63 + '"',
                    '"' + "a" * 65 + '"', '"' + "g" * 64 + '"',
                    "123", "null", "[]", '{"x":1}'):
            flags = '{"v3":{"admission_nonce":' + bad + '}}'
            assert extract_admission_nonce(flags) is None

    def test_extract_parser_bounds(self):
        good = '{"v3":{"admission_nonce":"%s"}}' % NONCE_HEX
        # size cap
        padded = '{"pad":"%s","v3":{"admission_nonce":"%s"}}' % (
            "x" * pow_v3.EXTRA_FLAGS_MAX_BYTES, NONCE_HEX)
        assert extract_admission_nonce(padded) is None
        # duplicate keys (top level and nested)
        assert extract_admission_nonce(
            '{"a":1,"a":2,"v3":{"admission_nonce":"%s"}}' % NONCE_HEX) is None
        assert extract_admission_nonce(
            '{"v3":{"admission_nonce":"%s","admission_nonce":"%s"}}'
            % (NONCE_HEX, NONCE_HEX)) is None
        # nesting depth cap
        deep = ('{"d":' * (pow_v3.EXTRA_FLAGS_MAX_DEPTH + 1) + "1"
                + "}" * (pow_v3.EXTRA_FLAGS_MAX_DEPTH + 1))
        merged = '{"deep":%s,"v3":{"admission_nonce":"%s"}}' % (deep, NONCE_HEX)
        assert extract_admission_nonce(merged) is None
        # invalid UTF-8 bytes input
        assert extract_admission_nonce(good.encode()[:-2] + b"\xff\x22}}") is None
        # bytes input with valid UTF-8 works
        assert extract_admission_nonce(good.encode()) == NONCE

    def test_extract_accepts_non_canonical_json(self):
        # Consensus does NOT enforce canonical JSON — only extraction + shape.
        flags = ('{  "v3" : { "admission_nonce" : "%s" } ,  "z": 1 }'
                 % NONCE_HEX)
        assert extract_admission_nonce(flags) == NONCE

    def test_nonce_hex_validator(self):
        assert is_valid_admission_nonce_hex("0" * 64)
        assert is_valid_admission_nonce_hex(NONCE_HEX)
        assert not is_valid_admission_nonce_hex("0" * 63)
        assert not is_valid_admission_nonce_hex("0" * 65)
        assert not is_valid_admission_nonce_hex("F" * 64)
        assert not is_valid_admission_nonce_hex(b"0" * 64)


# --------------------------------------------------------------------------- #
# §7 — step message / u derivation
# --------------------------------------------------------------------------- #

HEADER = bytes(range(76))
VDF = bytes(range(100, 132))
PRECISION = "fp16"


class TestStepMessage:
    def test_layout_without_nonce(self):
        ctx = [1, 2, 3]
        msg = build_step_message(HEADER, VDF, tick=7, step=9,
                                 context_tokens=ctx, precision=PRECISION)
        assert msg[:76] == HEADER
        assert msg[76:108] == VDF
        assert msg[108:112] == (7).to_bytes(4, "little")
        assert msg[112:116] == (9).to_bytes(4, "little")
        window = msg[116:116 + 256 * 8]
        # left-padded with zero tokens, last 3 slots = 1, 2, 3 (8B LE each)
        assert window[:253 * 8] == b"\x00" * (253 * 8)
        assert window[253 * 8:254 * 8] == (1).to_bytes(8, "little")
        assert window[255 * 8:] == (3).to_bytes(8, "little")
        assert msg[116 + 256 * 8:] == PRECISION.encode()

    def test_nonce_is_pure_suffix(self):
        base = build_step_message(HEADER, VDF, 1, 2, [5], PRECISION)
        with_nonce = build_step_message(HEADER, VDF, 1, 2, [5], PRECISION,
                                        admission_nonce=NONCE)
        assert with_nonce == base + NONCE

    def test_nonce_wrong_length_raises(self):
        with pytest.raises(ValueError):
            build_step_message(HEADER, VDF, 1, 2, [5], PRECISION,
                               admission_nonce=b"\x00" * 31)

    def test_context_truncates_to_rolling_window(self):
        long_ctx = list(range(1, 400))
        msg = build_step_message(HEADER, VDF, 1, 2, long_ctx, PRECISION)
        window = msg[116:116 + 256 * 8]
        # last 256 tokens of context: 144..399
        assert window[:8] == (144).to_bytes(8, "little")
        assert window[-8:] == (399).to_bytes(8, "little")

    def test_every_preimage_field_changes_u(self):
        # §10: changing any field (header, vdf, tick, context token,
        # precision, nonce) changes the digest.
        base_args = dict(header_prefix=HEADER, vdf=VDF, tick=1, step=2,
                         context_tokens=[5, 6], precision=PRECISION,
                         admission_nonce=NONCE)
        u0, d0 = step_u_from_message(build_step_message(**base_args))
        variants = [
            dict(base_args, header_prefix=b"\x01" + HEADER[1:]),
            dict(base_args, vdf=b"\x01" + VDF[1:]),
            dict(base_args, tick=2),
            dict(base_args, step=3),
            dict(base_args, context_tokens=[5, 7]),
            dict(base_args, precision="bf16"),
            dict(base_args, admission_nonce=bytes(32)),
        ]
        for v in variants:
            _, d = step_u_from_message(build_step_message(**v))
            assert d != d0

    def test_u_in_unit_interval_and_le(self):
        msg = build_step_message(HEADER, VDF, 1, 2, [5], PRECISION)
        u, digest = step_u_from_message(msg)
        assert 0.0 <= u < 1.0
        assert u == int.from_bytes(digest[:4], "little") / 2**32
        assert digest == sha256(msg).digest()

    @pytest.mark.skipif(not HAVE_TORCH, reason="torch unavailable")
    def test_equivalence_with_torch_build_msg(self):
        import torch
        from pow_utils import (_build_msg, _digest_to_u, _str_bytes,
                               _tok_le_bytes, _u32le, sha256_many)

        ctx_tokens = [11, 22, 33, 44]
        window = torch.zeros(256, dtype=torch.int64)
        window[-len(ctx_tokens):] = torch.tensor(ctx_tokens)

        header_t = torch.frombuffer(bytearray(HEADER), dtype=torch.uint8)
        vdf_t = torch.frombuffer(bytearray(VDF), dtype=torch.uint8)
        T8 = _u32le(torch.tensor([7], dtype=torch.uint32))
        j4 = _u32le(torch.tensor([9], dtype=torch.uint32))
        ctx_bytes = _tok_le_bytes(window.unsqueeze(0))
        pb = _str_bytes(PRECISION, batch_size=1)
        nonce_t = torch.frombuffer(bytearray(NONCE), dtype=torch.uint8)

        for nonce_arg, nonce_ref in ((None, None), (nonce_t, NONCE)):
            msg_t = _build_msg(header_t, vdf_t, T8, j4, ctx_bytes, pb,
                               nonce=nonce_arg)
            ref = build_step_message(HEADER, VDF, 7, 9, ctx_tokens, PRECISION,
                                     admission_nonce=nonce_ref)
            assert bytes(msg_t[0].tolist()) == ref
            digest_t = sha256_many(msg_t)
            u_ref, digest_ref = step_u_from_message(ref)
            assert bytes(digest_t[0].tolist()) == digest_ref
            # _digest_to_u accumulates in float32; the reference is exact
            # double. Agreement to float32 resolution is all that is required
            # (the consensus quantity is the digest; u tolerances are ATOL=1e-4).
            assert abs(_digest_to_u(digest_t)[0].item() - u_ref) < 2**-20


# --------------------------------------------------------------------------- #
# §6 — admission puzzle
# --------------------------------------------------------------------------- #

PROMPT = [10, 20, 30]
MASK = [False, False, True]
COMMIT = pow_v3.prompt_commitment(PROMPT, MASK)


class TestPromptCommitment:
    def test_layout(self):
        from hashlib import sha256 as _sha
        expected = _sha(
            pow_v3.PROMPT_CTX_TAG
            + len(PROMPT).to_bytes(4, "little")
            + b"".join(t.to_bytes(8, "little", signed=True) for t in PROMPT)
            + len(MASK).to_bytes(4, "little")
            + bytes([0, 0, 1])).digest()
        assert COMMIT == expected

    def test_binds_full_prefix(self):
        # tokens beyond any rolling window still change the commitment
        long_a = pow_v3.prompt_commitment(list(range(400)), [False] * 400)
        long_b = pow_v3.prompt_commitment([9] + list(range(1, 400)),
                                          [False] * 400)
        assert long_a != long_b
        # pad_mask is model-visible state: it must bind too
        assert pow_v3.prompt_commitment(PROMPT, [False, False, False]) != COMMIT
        # omitted mask canonicalizes to all-false for this prompt.
        assert pow_v3.prompt_commitment(PROMPT, None) == pow_v3.prompt_commitment(
            PROMPT, [False] * len(PROMPT))

    def test_rejects_mask_length_mismatch(self):
        with pytest.raises(ValueError, match="pad_mask length"):
            pow_v3.prompt_commitment(PROMPT, [])


class TestAdmission:
    def test_admission_message_layout(self):
        msg_w = build_step_message(HEADER, VDF, 1, 0, [5], PRECISION)
        mid = "org/model@abcdef"
        out = admission_message(msg_w, mid, NONCE, COMMIT)
        assert out[:len(msg_w)] == msg_w
        off = len(msg_w)
        assert out[off:off + 32] == COMMIT
        off += 32
        assert out[off:off + 2] == len(mid).to_bytes(2, "little")
        assert out[off + 2:off + 2 + len(mid)] == mid.encode()
        assert out[-32:] == NONCE

    def test_admission_message_rejects_bad_nonce_len(self):
        with pytest.raises(ValueError):
            admission_message(b"msg", "m", b"\x00" * 31, COMMIT)

    def test_admission_message_rejects_bad_commitment_len(self):
        with pytest.raises(ValueError):
            admission_message(b"msg", "m", NONCE, b"\x00" * 31)

    def test_expected_tries_integer_math(self):
        # defaults: alpha=4/100, decode_us=10_000_000, argon_ref_us=8_000,
        # normalizer=1_000_000. difficulty == normalizer => decode at
        # reference: tries = 0.04 * 10s / 8ms = 50.
        assert admission_expected_tries(1_000_000) == 50
        # difficulty = normalizer/2 => model is 2x the compute => 100 tries
        assert admission_expected_tries(500_000) == 100
        # tiny model, huge difficulty => clamp to 1
        assert admission_expected_tries(10**15) == 1
        # floor behaviour: difficulty 3e6 => 50/3 = 16.66 -> 16
        assert admission_expected_tries(3_000_000) == 16

    def test_expected_tries_rejects_nonpositive_difficulty(self):
        for bad in (0, -5):
            with pytest.raises(ValueError):
                admission_expected_tries(bad)

    def test_target_monotone_in_difficulty(self):
        # INVERSE scalar: lower difficulty => more FLOPs => more tries
        # => LOWER admission target.
        t_hard = admission_target(100_000)     # big model
        t_ref = admission_target(1_000_000)
        t_easy = admission_target(10**15)      # tiny model, tries=1
        assert t_hard < t_ref < t_easy
        assert t_easy == UINT256_MAX
        assert t_ref == UINT256_MAX // 50

    def test_admission_valid_little_endian_strict(self):
        # digest bytes little-endian: last byte is most significant.
        low = bytes(31) + b"\x00"
        high = bytes(31) + b"\xff"
        target = int.from_bytes(bytes(31) + b"\x80", "little")
        assert admission_valid(low, target)
        assert not admission_valid(high, target)
        # strict <: equal fails
        assert not admission_valid(bytes(31) + b"\x80", target)
        with pytest.raises(ValueError):
            admission_valid(b"\x00" * 31, target)

    @pytest.mark.skipif(not HAVE_ARGON2, reason="argon2-cffi unavailable")
    def test_argon2id_digest_deterministic_and_binding(self):
        msg_w = build_step_message(HEADER, VDF, 1, 0, [5], PRECISION)
        m1 = admission_message(msg_w, "m@c", NONCE, COMMIT)
        d1 = pow_v3.argon2id_digest(m1)
        assert len(d1) == 32
        assert pow_v3.argon2id_digest(m1) == d1
        # different nonce => different digest
        m2 = admission_message(msg_w, "m@c", bytes(32), COMMIT)
        assert pow_v3.argon2id_digest(m2) != d1
        # different model identifier => different digest (anti-amortization)
        m3 = admission_message(msg_w, "m@d", NONCE, COMMIT)
        assert pow_v3.argon2id_digest(m3) != d1
        # different FULL prefix (same rolling window) => different digest —
        # the inert-prefix admission-amortization fix (§6)
        other_commit = pow_v3.prompt_commitment(PROMPT + [999], MASK + [False])
        m4 = admission_message(msg_w, "m@c", NONCE, other_commit)
        assert pow_v3.argon2id_digest(m4) != d1

    @pytest.mark.skipif(not HAVE_ARGON2, reason="argon2-cffi unavailable")
    def test_length_prefix_disambiguates(self):
        # u16le prefix: ("ab", nonce) vs ("a", b"b"+nonce[:-1]...) must differ.
        msg_w = b"fixed"
        a = admission_message(msg_w, "ab", NONCE, COMMIT)
        b = admission_message(msg_w + b"\x00", "ab", NONCE, COMMIT)
        assert a != b


# --------------------------------------------------------------------------- #
# §4 — conservative B_cred
# --------------------------------------------------------------------------- #

class TestBCred:
    def test_mass_q63_basic(self):
        # point interval => mass == 2*ATOL_Q63_CEIL (exact integer widening).
        assert mass_q63_for_step(0.3, 0.3) == 2 * ATOL_Q63_CEIL
        # width 0.5 => ceil(0.7*2^63) - floor(0.2*2^63) + 2*ATOL, over-estimate.
        m = mass_q63_for_step(0.2, 0.7)
        assert m >= int(0.5 * BCRED_Q_ONE)
        assert m <= int(0.5 * BCRED_Q_ONE) + 2 * ATOL_Q63_CEIL + 2
        # full mass clamps to 2^63.
        assert mass_q63_for_step(0.0, 1.0) == BCRED_Q_ONE

    def test_mass_q63_rejects_invalid(self):
        for lo, hi in ((float("nan"), 0.5), (0.5, float("nan")),
                       (float("inf"), 0.5), (0.5, float("-inf")),
                       (0.7, 0.2)):
            with pytest.raises(ValueError):
                mass_q63_for_step(lo, hi)

    def test_exact_conversion_vs_fraction(self):
        # The endpoint quantisers must equal the exact rational floor/ceil of
        # the *double* value times 2^63 — no double*2^63 rounding.
        from fractions import Fraction

        def ref_floor(x):
            return (Fraction(x) * BCRED_Q_ONE).__floor__()

        def ref_ceil(x):
            fr = Fraction(x) * BCRED_Q_ONE
            return -((-fr).__floor__())

        rng = random.Random(99)
        xs = [i / 10000.0 for i in range(10001)]
        xs += [rng.random() for _ in range(20000)]
        xs += [2.0 ** -k for k in range(40)] + [1.0 - 2.0 ** -k for k in range(1, 40)]
        for x in xs:
            if not (0.0 <= x <= 1.0):
                continue
            assert pow_v3._f64_to_q63_floor(x) == ref_floor(x), x
            assert pow_v3._f64_to_q63_ceil(x) == ref_ceil(x), x

    def test_credit_edges(self):
        # §4: an invalid/garbage interval (mass_q63 == 0) never earns credit.
        with pytest.raises(ValueError):
            credit_units_for_step(0)
        # full mass credits 0 units.
        assert credit_units_for_step(mass_q63_for_step(0.0, 1.0)) == 0
        # exact power of two: mass 0.25 => 2 bits => 2*R units (with the 2*ATOL
        # widening the mass is 0.25 exactly only when width == 0.25-2*ATOL).
        assert credit_units_for_step(mass_q63_for_step(0.1, 0.35 - 2 * ATOL)) == 2 * BCRED_R
        # threshold[1 bit] == 2^62 => mass 2^62 credits exactly R units (1 bit).
        assert credit_units_for_step(1 << 62) == BCRED_R
        # tiny mass_q63 hits the per-step cap (table max index).
        assert credit_units_for_step(1) == BCRED_N_MAX

    def test_never_over_credit(self):
        # For every step, threshold[credit] >= mass_q63 and (cap or
        # threshold[credit+1] < mass_q63): the credit is the exact largest n
        # with 2^(-n/R) >= mass, so credited bits <= -log2(mass) — never over.
        th = pow_v3._thresholds_q63()
        rng = random.Random(7)
        for _ in range(50000):
            lo = rng.random()
            hi = lo + rng.random() * (1 - lo)
            mq = mass_q63_for_step(lo, hi)
            n = credit_units_for_step(mq)
            assert th[n] >= mq
            if n < BCRED_N_MAX:
                assert th[n + 1] < mq

    def test_boundary_at_threshold_plus_minus_one(self):
        th = pow_v3._thresholds_q63()
        for n in (1, BCRED_R, 5000, 20000, BCRED_N_MAX - 1):
            assert credit_units_for_step(th[n]) >= n
            assert credit_units_for_step(th[n] + 1) < n

    def test_sum_order_independent(self):
        rng = random.Random(1234)
        lowers = [rng.uniform(0, 0.5) for _ in range(256)]
        uppers = [lo + rng.uniform(0, 0.4) for lo in lowers]
        ref = b_cred_units_from_bounds(lowers, uppers)
        pairs = list(zip(lowers, uppers))
        for _ in range(5):
            rng.shuffle(pairs)
            lo2, hi2 = zip(*pairs)
            assert b_cred_units_from_bounds(lo2, hi2) == ref

    def test_sum_length_mismatch(self):
        with pytest.raises(ValueError):
            b_cred_units_from_bounds([0.1, 0.2], [0.3])

    def test_boundary_at_floor_and_free(self):
        # mass 0.25 => 2 bits/step. 256 steps * 2*R = 512*R units: free.
        lowers = [0.1] * 256
        uppers = [0.35 - 2 * ATOL] * 256
        u = b_cred_units_from_bounds(lowers, uppers)
        assert u == 512 * BCRED_R
        assert tier_for_b_cred_units(u) == TIER_FREE

        # Exactly at threshold: B_cred == B_FLOOR is NOT < floor => admission.
        assert tier_for_b_cred_units(B_FLOOR_UNITS) == TIER_ADMISSION
        assert tier_for_b_cred_units(B_FLOOR_UNITS - 1) == TIER_INVALID
        assert tier_for_b_cred_units(B_FREE_UNITS) == TIER_FREE
        assert tier_for_b_cred_units(B_FREE_UNITS - 1) == TIER_ADMISSION


class TestTierRule:
    def test_mapping(self):
        assert tier_for_b_cred_units(0) == TIER_INVALID
        assert tier_for_b_cred_units(44 * BCRED_R) == TIER_INVALID
        assert tier_for_b_cred_units(45 * BCRED_R) == TIER_ADMISSION
        assert tier_for_b_cred_units(69 * BCRED_R) == TIER_ADMISSION
        assert tier_for_b_cred_units(70 * BCRED_R) == TIER_FREE
        assert tier_for_b_cred_units(500 * BCRED_R) == TIER_FREE
