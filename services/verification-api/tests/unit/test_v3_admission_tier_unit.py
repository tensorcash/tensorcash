"""Unit tests for the v3 tier/admission gate in proof_verifier
(TIP-0003, §4-§6).

The gate is exercised as an unbound method on a lightweight stub so no model,
GPU, or pfunpack blob is needed; proof_verifier's module import still requires
the service environment (torch, pfunpack), so the whole file skips outside it.
"""

import os
import sys

import pytest

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

pv_mod = pytest.importorskip("proof_verifier")
pow_v3 = pytest.importorskip("utils.pow_v3")
torch = pytest.importorskip("torch")

HEADER_HEX = "cc" * 76
VDF_HEX = "aa" * 32
NONCE = bytes(range(32))


class _Logger:
    def __init__(self):
        self.errors = []
        self.warnings = []

    def error(self, msg, **kw):
        self.errors.append((msg, kw.get("failure_type")))

    def warning(self, msg, **kw):
        self.warnings.append(msg)

    def debug(self, msg, **kw):
        pass


# Default difficulty is >0 so the stub is v3-ACTIVE (option 2: the gate runs
# only when proof_version>=3 AND registered difficulty>0). 5e13 => expected
# admission tries == 1, so any present nonce is trivially admissible unless a
# test overrides difficulty to force inadmissibility.
def _stub(*, version=3, nonce=None, difficulty=5 * 10**13,
          temperature=1.0, top_p=1.0, top_k=50, repetition_penalty=1.0):
    class _S:
        pass
    s = _S()
    s.proof_version = version
    s._v3_nonce = nonce
    s._v3_difficulty = difficulty
    s.temperature = temperature
    s.top_p = top_p
    s.top_k = top_k
    s.repetition_penalty = repetition_penalty
    s.logger = _Logger()
    s.proof = {
        "header_prefix": HEADER_HEX,
        "vdf": VDF_HEX,
        "tick": 7,
        "hash": "00" * 32,
        "model_identifier": "test-model@commit",
    }
    s.prompt_tokens = torch.tensor([1, 2, 3], dtype=torch.long)
    s.pad_mask = torch.tensor([False, False, False])
    s.stated_precision = "fp16"
    return s


def _gate(s, lower, upper):
    return pv_mod.ProofVerifier._verify_v3_admission_tier(s, lower, upper)


# Bounds engineered per tier: mass 0.25 per step = exactly 2 bits/step.
def _bounds(bits_per_step, n=256):
    mass = 2.0 ** (-bits_per_step) - 2 * pow_v3.ATOL
    lower = [0.1] * n
    upper = [0.1 + mass] * n
    return lower, upper


FREE_BOUNDS = _bounds(2.0)          # 512 bits: free tier
# ~0.2 bits/step * 256 = ~51 bits: admission tier (45 <= B < 70)
MID_BOUNDS = _bounds(0.2)
LOW_BOUNDS = _bounds(0.1)           # ~25.6 bits: below floor


def _admissible_nonce(s, max_tries=200_000):
    """Grind a nonce admissible for the stub's difficulty (tiny targets only
    in tests — use difficulty high enough that expected_tries is small)."""
    msg_w = pow_v3.build_step_message(
        bytes.fromhex(HEADER_HEX), bytes.fromhex(VDF_HEX), 7, 0,
        s.prompt_tokens.tolist(), "fp16")
    commitment = pow_v3.prompt_commitment(s.prompt_tokens.tolist(),
                                          s.pad_mask.tolist())
    target = pow_v3.admission_target(int(s._v3_difficulty))
    for i in range(max_tries):
        candidate = i.to_bytes(32, "little")
        digest = pow_v3.argon2id_digest(
            pow_v3.admission_message(msg_w, s.proof["model_identifier"],
                                     candidate, commitment))
        if pow_v3.admission_valid(digest, target):
            return candidate
    raise AssertionError("no admissible nonce found in max_tries")


def test_v2_proof_is_noop():
    s = _stub(version=2, temperature=0.7)   # divergent sampler: still ok for v2
    assert _gate(s, *LOW_BOUNDS)


def test_free_tier_no_nonce_accepts():
    s = _stub()
    assert _gate(s, *FREE_BOUNDS)


def test_below_floor_rejects():
    s = _stub()
    assert not _gate(s, *LOW_BOUNDS)
    assert s.logger.errors[-1][1] == "v3_below_floor"


def test_admission_tier_without_nonce_rejects():
    s = _stub()
    assert not _gate(s, *MID_BOUNDS)
    assert s.logger.errors[-1][1] == "v3_admission_missing"


def test_sampler_profile_exact_equality():
    for field, bad in (("temperature", 0.99), ("top_p", 0.9),
                       ("top_k", 40), ("repetition_penalty", 1.1)):
        s = _stub(**{field: bad})
        assert not _gate(s, *FREE_BOUNDS)
        assert s.logger.errors[-1][1] == "v3_sampler_profile_mismatch"
    s = _stub(top_k=None)
    assert not _gate(s, *FREE_BOUNDS)


def test_invalid_bounds_reject():
    s = _stub()
    lower, upper = FREE_BOUNDS
    bad_upper = list(upper)
    bad_upper[7] = float("nan")
    assert not _gate(s, lower, bad_upper)
    assert s.logger.errors[-1][1] == "v3_bcred_failure"


def test_admission_nonce_present_verified_regardless_of_tier():
    # difficulty = 5e13 => expected_tries = 1 => any nonce admissible.
    s = _stub(nonce=NONCE, difficulty=5 * 10**13)
    assert _gate(s, *FREE_BOUNDS)

    # difficulty small => huge expected_tries => arbitrary nonce inadmissible,
    # rejected EVEN IN THE FREE TIER (§5 present => valid).
    s = _stub(nonce=NONCE, difficulty=1)
    assert not _gate(s, *FREE_BOUNDS)
    assert s.logger.errors[-1][1] == "v3_admission_inadmissible"


def test_admission_tier_with_admissible_nonce_accepts():
    s = _stub(difficulty=5 * 10**13)    # expected_tries = 1
    s._v3_nonce = _admissible_nonce(s)
    assert _gate(s, *MID_BOUNDS)


def test_no_difficulty_is_v2_replay_noop():
    # Option 2 (§6): difficulty absent/0 => NOT v3-active => the gate is a
    # no-op even for a version-3 proof, because consensus judges it under v2
    # (nonce not folded into u). Without difficulty the verifier must not
    # apply ANY v3 rule — not the tier floor, not the admission check.
    for diff in (None, 0):
        s = _stub(difficulty=diff)                    # below floor, no nonce
        assert _gate(s, *LOW_BOUNDS)
        assert not s.logger.errors                    # nothing enforced
        s = _stub(nonce=NONCE, difficulty=diff)       # nonce present, tiny B
        assert _gate(s, *MID_BOUNDS)                  # would reject if active
        assert not s.logger.errors


def test_below_floor_is_noop_when_inactive_but_rejects_when_active():
    # Same proof, two rulesets: active (difficulty>0) rejects below-floor;
    # inactive (difficulty=0) accepts it as a v2 replay.
    assert not _gate(_stub(difficulty=5 * 10**13), *LOW_BOUNDS)
    assert _gate(_stub(difficulty=0), *LOW_BOUNDS)
