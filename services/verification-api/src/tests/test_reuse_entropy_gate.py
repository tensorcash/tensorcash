# SPDX-License-Identifier: Apache-2.0
"""
Functional tests for the version-keyed reuse-entropy consensus gate.

Everything is asserted through the REAL public verifier entry point
``ProofVerifier().quick_verify(blob)`` — no internal/private methods, no
bare-instance construction. We build proof blobs the same way the rest of the
suite does (``serialize_proof`` over a proof dict deserialized from the bundled
``pow_proof_test.bin``) and feed them straight to quick_verify.

Behaviour under test (the v1-grandfather / v2-gate refactor):
  * legacy proofs (version < REUSE_GATE_VERSION) are NEVER rejected for reuse
    entropy, no matter how grindable -> chain history is safe by version.
  * v2+ proofs ARE gated: reuse_score_q32 > REUSE_SCORE_CAP_Q32 -> rejected.

Golden reuse construction (independently verified with pure integer math):
  * "greedy" = every step's chosen token dominates its distribution, so the
    chosen bucket mass ~= 1, contributing 0 reuse bits -> E_reuse == 256
    forwards (256 * 2^32 q32), far above the p95 cap (~57.7) -> v2 must reject.
    Keeping chosen_tokens unchanged means sampling-u still routes into the
    (now ~[0,1]) chosen bucket, so sequence verification still passes; only the
    reuse score changes.
  * the bundled real proof has natural low reuse -> below cap -> v2 accepts.

Runs in the verification-api test env (needs torch + pfunpack), via run_tests.sh.
"""
import os
import copy

import pytest

from proof_verifier import ProofVerifier
from config.constants import REUSE_GATE_VERSION
from utils.pow_utils import serialize_proof
# Same ResponseValue access path the verifier itself uses.
from utils.proof import ResponseValue
# proof-blob -> dict helper already used elsewhere in the suite (import-safe;
# tests/test.py is guarded by __main__).
from tests.test import deserialize_pow_proof_python

QUICK_OK = ResponseValue.ResponseValue.Quick_OK
PROOF_BIN = os.path.join(os.path.dirname(__file__), "pow_proof_test.bin")


def _passed(result) -> bool:
    """quick_verify returns a ResponseValue; OK-family == accepted."""
    return result == QUICK_OK


@pytest.fixture(scope="module")
def verifier():
    return ProofVerifier()


@pytest.fixture(scope="module")
def base_dict():
    d = deserialize_pow_proof_python(PROOF_BIN)
    assert d is not None, "could not deserialize bundled proof"
    assert len(d.get("chosen_tokens", [])) > 0, "proof has no generation steps"
    return d


def _with_version(d, version):
    d = copy.deepcopy(d)
    d["version"] = int(version)
    return d


def _force_greedy(d):
    """Reshape every step so its chosen token dominates -> max reuse score.

    chosen_tokens are preserved, so the per-step sampling-u (re-derived from
    header+context, which we do not touch) still lands inside the chosen
    token's bucket — now ~[0,1] — and sequence verification still succeeds.
    """
    d = copy.deepcopy(d)
    n = len(d["chosen_tokens"])
    T = float(d.get("temperature", 1.0)) or 1.0
    for i in range(n):
        ch = int(d["chosen_tokens"][i])
        width = len(d["topk_indices"][i])
        others = [int(j) for j in d["topk_indices"][i] if int(j) != ch][: width - 1]
        d["topk_indices"][i] = [ch] + others
        d["topk_logits"][i] = [100.0] + [-100.0] * len(others)
        # logZ so that mass(chosen) = exp(eff_logit - logZ) ~= 1 (greedy).
        d["softmax_normalizers"][i] = 100.0 / T
    return d


def test_baseline_real_proof_still_verifies(verifier):
    """The refactor must not break verification of a valid (legacy) proof."""
    res = verifier.quick_verify(open(PROOF_BIN, "rb").read())
    assert _passed(res), f"baseline proof unexpectedly rejected: {res}"


def test_v2_low_reuse_accepted(verifier, base_dict):
    """A v2 proof whose natural reuse is below the cap is accepted."""
    res = verifier.quick_verify(serialize_proof(_with_version(base_dict, REUSE_GATE_VERSION)))
    assert _passed(res), f"v2 low-reuse proof should pass the gate: {res}"


def test_v1_high_reuse_grandfathered(verifier, base_dict):
    """A maximally grindable (E_reuse==256) proof at v1 is NOT rejected.

    This is the bifurcation-safety property: legacy versions bypass the gate, so
    no historical (v1) block can ever be orphaned by the new rule.
    """
    greedy_v1 = _with_version(_force_greedy(base_dict), REUSE_GATE_VERSION - 1)
    res = verifier.quick_verify(serialize_proof(greedy_v1))
    assert _passed(res), f"legacy high-reuse proof must be grandfathered: {res}"


def test_v2_high_reuse_rejected(verifier, base_dict):
    """The same maximally grindable proof at v2 IS rejected by the reuse gate."""
    greedy_v2 = _with_version(_force_greedy(base_dict), REUSE_GATE_VERSION)
    res = verifier.quick_verify(serialize_proof(greedy_v2))
    assert not _passed(res), "v2 high-reuse proof must be rejected (reuse > cap)"


@pytest.mark.parametrize(
    "version,enforced",
    [(0, False), (1, False), (REUSE_GATE_VERSION, True), (REUSE_GATE_VERSION + 1, True)],
)
def test_version_threshold_decides_enforcement(verifier, base_dict, version, enforced):
    """Only version >= REUSE_GATE_VERSION is gated; below it is grandfathered.

    Driven through quick_verify on a high-reuse proof: enforced versions must
    reject, grandfathered versions must accept.
    """
    greedy = _with_version(_force_greedy(base_dict), version)
    res = verifier.quick_verify(serialize_proof(greedy))
    if enforced:
        assert not _passed(res), f"version {version} should be gated"
    else:
        assert _passed(res), f"version {version} should be grandfathered"
