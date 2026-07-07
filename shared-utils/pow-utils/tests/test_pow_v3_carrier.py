"""Stage-2 carrier tests (TIP-0003, §10): the v3 admission nonce
rides extra_flags through every Python writer, survives the completion-id
mutation, and v2 emission is byte-untouched."""

import json
import os
import sys
import tempfile

import pytest

_POW_UTILS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _POW_UTILS_DIR)
# The real generated FlatBuffers package (fb-schemas/proof) must shadow the
# local `proof/` mock for MiningResponse parsing.
sys.path.insert(0, os.path.join(os.path.dirname(_POW_UTILS_DIR), "fb-schemas"))

# When another test module imported pow_utils first, the local `proof/` MOCK
# package is already in sys.modules (it has classes, not generated modules —
# detectable because real `proof.Proof` is a module containing class `Proof`).
# Evict it and the modules that bound it so our imports below re-resolve
# against fb-schemas (now first on sys.path).
import importlib

_p = sys.modules.get("proof")
if _p is not None and not hasattr(getattr(_p, "Proof", None), "Proof"):
    for _m in [m for m in list(sys.modules)
               if m == "proof" or m.startswith("proof.")]:
        sys.modules.pop(_m, None)
    for _m in ("pow_utils", "zmq_pow_writer"):
        sys.modules.pop(_m, None)
    importlib.invalidate_caches()

torch = pytest.importorskip("torch")

import pow_v3
from pow_utils import ProofWriter, serialize_proof
from proof import Proof as ProofModule
from proof import MiningResponse as MiningResponseModule
from zmq_pow_writer import MiningResponseWriter

NONCE = bytes(range(32))
WINDOW = 8  # keep tensors small; the carrier does not care about window size


def _window_data():
    return {
        "tokens": torch.arange(WINDOW, dtype=torch.int32),
        "probs": torch.full((WINDOW,), 0.5, dtype=torch.float32),
        "topk_logits": torch.randn(WINDOW, 5, dtype=torch.float32),
        "topk_indices": torch.randint(0, 100, (WINDOW, 5), dtype=torch.int32),
        "attention_mask": torch.ones(WINDOW, dtype=torch.bool),
        "sampling_u": torch.rand(WINDOW, dtype=torch.float32),
        "softmax_normalizers": torch.ones(WINDOW, dtype=torch.float32),
        "logsumexp_stats": torch.randn(WINDOW, 6, dtype=torch.float32),
    }


def _seq_info():
    return {
        "prompt_tokens": [1, 2, 3],
        "pad_mask": [False, False, False],
        "temperature": 1.0,
        "top_p": 1.0,
        "top_k": 50,
        "repetition_penalty": 1.0,
        "model_identifier": "test-model@commit",
        "compute_precision": "fp16",
    }


def _pow_params():
    return {
        "tick": 42,
        "target": "ff" * 32,
        "vdf": "aa" * 32,
        "block_hash": "bb" * 32,
        "header_prefix": "cc" * 76,
        "ipfs_cid": "QmTest123",
    }


def _digest():
    return torch.zeros(1, 32, dtype=torch.uint8)


def _write(writer, **kwargs):
    return writer.write_proof(
        seq_id=1, step_num=WINDOW, window_data=_window_data(),
        digest=_digest(), is_solution=False, pow_params=_pow_params(),
        seq_info=_seq_info(), **kwargs)


def _parse_proof(buf):
    p = ProofModule.Proof.GetRootAs(buf, 0)
    extra = p.ExtraFlags()
    if isinstance(extra, bytes):
        extra = extra.decode("utf-8")
    return p, extra


@pytest.fixture
def writer():
    with tempfile.TemporaryDirectory() as tmpdir:
        w = ProofWriter(output_dir=tmpdir)
        w.set_model_identifier("test-model@commit")
        w.set_compute_precision("fp16")
        yield w


class TestProofWriterCarrier:
    def test_v2_default_untouched(self, writer):
        writer.set_model_config_diff({"quantization": "awq"})
        data, _ = _write(writer, completion_id="cmpl-1")
        p, extra = _parse_proof(data)
        assert p.Version() == 2
        # v2 stays a pformat python-literal blob (consensus-opaque)
        assert extra == "{'quantization': 'awq'}"

    def test_v2_rejects_admission_nonce(self, writer):
        with pytest.raises(ValueError):
            _write(writer, admission_nonce=NONCE)

    def test_v3_no_nonce_canonical_json(self, writer):
        writer.set_proof_version(3)
        writer.set_model_config_diff({"quantization": "awq"})
        data, _ = _write(writer, completion_id="cmpl-1")
        p, extra = _parse_proof(data)
        assert p.Version() == 3
        parsed = json.loads(extra)
        assert extra == pow_v3.canonical_json(parsed)
        assert parsed["quantization"] == "awq"
        # ordering hazard regression: completion_id lands on THIS proof
        assert parsed["completion_id"] == "cmpl-1"
        assert pow_v3.extract_admission_nonce(extra) is None

    def test_v3_nonce_survives_completion_id_mutation(self, writer):
        writer.set_proof_version(3)
        writer.set_model_config_diff({"quantization": "awq"})
        data, proof_dict = _write(writer, completion_id="cmpl-2",
                                  admission_nonce=NONCE)
        p, extra = _parse_proof(data)
        assert p.Version() == 3
        parsed = json.loads(extra)
        assert parsed["completion_id"] == "cmpl-2"
        assert parsed["quantization"] == "awq"
        assert pow_v3.extract_admission_nonce(extra) == NONCE
        assert extra == pow_v3.canonical_json(parsed)

    def test_serialize_proof_version_passthrough(self, writer):
        writer.set_proof_version(3)
        _, proof_dict = _write(writer)
        assert proof_dict["version"] == 3
        # re-serializing the returned dict keeps version + extra_flags
        p, extra = _parse_proof(serialize_proof(proof_dict))
        assert p.Version() == 3
        assert json.loads(extra) is not None


class TestZmqWriterCarrier:
    def _proof_dict(self, version=2, **extra_keys):
        d = {
            "version": version,
            "tick": 42,
            "timestamp": 1234567890,
            "is_solution": False,
            "model_identifier": "test-model@commit",
            "compute_precision": "fp16",
            "ipfs_cid": "QmTest123",
            "model_config_diff": {"completion_id": "cmpl-9"},
            "temperature": 1.0,
            "top_p": 1.0,
            "top_k": 50,
            "repetition_penalty": 1.0,
            "target": "ff" * 32,
            "vdf": "aa" * 32,
            "block_hash": "bb" * 32,
            "hash": "00" * 32,
            "header_prefix": "cc" * 76,
            "chosen_tokens": [1, 2, 3],
            "chosen_probs": [0.1, 0.2, 0.3],
            "sampling_u": [0.4, 0.5, 0.6],
            "softmax_normalizers": [1.0, 1.0, 1.0],
            "prompt_tokens": [7, 8],
            "pad_mask": [False, False],
            "topk_logits": [[0.1, 0.2]] * 3,
            "topk_indices": [[1, 2]] * 3,
            "logsumexp_stats": [[0.0] * 6] * 3,
        }
        d.update(extra_keys)
        return d

    def _serialize(self, proof_dict):
        w = MiningResponseWriter()
        buf = w._serialize_response({
            "proof_dict": proof_dict,
            "pow_blob_hash": bytes(32),
            "req_id": 1,
            "nonce": 0,
            "adjusted_bits": 0,
            "difficulty": 1_000_000,
        })
        mr = MiningResponseModule.MiningResponse.GetRootAs(buf, 0)
        p = mr.PowBlob()
        extra = p.ExtraFlags()
        if isinstance(extra, bytes):
            extra = extra.decode("utf-8")
        return p, extra

    def test_v2_json_dumps_path_unchanged(self):
        p, extra = self._serialize(self._proof_dict(version=2))
        assert p.Version() == 2
        assert json.loads(extra)["completion_id"] == "cmpl-9"

    def test_v3_merges_admission_nonce_side_key(self):
        p, extra = self._serialize(
            self._proof_dict(version=3, admission_nonce=NONCE))
        assert p.Version() == 3
        parsed = json.loads(extra)
        assert parsed["completion_id"] == "cmpl-9"
        assert pow_v3.extract_admission_nonce(extra) == NONCE
        assert extra == pow_v3.canonical_json(parsed)

    def test_v3_hex_side_key_and_audit_marker_preserved(self):
        mcd = {"completion_id": "cmpl-9", "proof_purpose": "audit"}
        p, extra = self._serialize(self._proof_dict(
            version=3, model_config_diff=mcd, admission_nonce=NONCE.hex()))
        parsed = json.loads(extra)
        assert parsed["proof_purpose"] == "audit"
        assert pow_v3.extract_admission_nonce(extra) == NONCE

    def test_v3_without_nonce(self):
        p, extra = self._serialize(self._proof_dict(version=3))
        assert p.Version() == 3
        assert pow_v3.extract_admission_nonce(extra) is None
        assert extra == pow_v3.canonical_json(json.loads(extra))
