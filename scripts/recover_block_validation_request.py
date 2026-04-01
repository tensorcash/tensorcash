#!/usr/bin/env python3
"""Recover a verifier ValidationRequest from a raw TensorCash block hex dump."""

from __future__ import annotations

import argparse
import hashlib
import json
import struct
import sys
from pathlib import Path
from typing import Any


REPO = Path(__file__).resolve().parents[1]
FB_SCHEMA_DIR = REPO / "shared-utils" / "fb-schemas"
sys.path.insert(0, str(FB_SCHEMA_DIR))

import flatbuffers  # noqa: E402
from proof import (  # noqa: E402
    BlockValidation,
    FloatArray,
    Proof,
    UIntArray,
    ValidationRequest,
    ValidationType,
    ValidationUnion,
)


class Cursor:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.pos = 0

    def read(self, n: int) -> bytes:
        if self.pos + n > len(self.data):
            raise ValueError(f"short read at {self.pos}: need {n}, have {len(self.data) - self.pos}")
        out = self.data[self.pos : self.pos + n]
        self.pos += n
        return out

    def peek(self, n: int) -> bytes:
        return self.data[self.pos : self.pos + n]

    def u8(self) -> int:
        return self.read(1)[0]

    def u32(self) -> int:
        return struct.unpack("<I", self.read(4))[0]

    def i32(self) -> int:
        return struct.unpack("<i", self.read(4))[0]

    def u64(self) -> int:
        return struct.unpack("<Q", self.read(8))[0]

    def f32(self) -> float:
        return struct.unpack("<f", self.read(4))[0]

    def compact_size(self) -> int:
        first = self.u8()
        if first < 253:
            return first
        if first == 253:
            return struct.unpack("<H", self.read(2))[0]
        if first == 254:
            return self.u32()
        return self.u64()

    def bytes_vec(self) -> bytes:
        return self.read(self.compact_size())

    def str_vec(self) -> str:
        return self.bytes_vec().decode("utf-8", errors="replace")


def sha256d(data: bytes) -> bytes:
    return hashlib.sha256(hashlib.sha256(data).digest()).digest()


def skip_tx(cur: Cursor) -> None:
    cur.read(4)  # version
    has_witness = False
    if cur.peek(2)[:1] == b"\x00" and len(cur.peek(2)) == 2 and cur.peek(2)[1] != 0:
        cur.read(2)
        has_witness = True

    n_inputs = cur.compact_size()
    for _ in range(n_inputs):
        cur.read(32 + 4)
        cur.read(cur.compact_size())
        cur.read(4)

    n_outputs = cur.compact_size()
    for _ in range(n_outputs):
        cur.read(8)
        cur.read(cur.compact_size())

    if has_witness:
        for _ in range(n_inputs):
            n_items = cur.compact_size()
            for _ in range(n_items):
                cur.read(cur.compact_size())

    cur.read(4)  # locktime


def read_vec_u32(cur: Cursor) -> list[int]:
    return [cur.u32() for _ in range(cur.compact_size())]


def read_vec_f32(cur: Cursor) -> list[float]:
    return [cur.f32() for _ in range(cur.compact_size())]


def read_vec_vec_f32(cur: Cursor) -> list[list[float]]:
    return [read_vec_f32(cur) for _ in range(cur.compact_size())]


def read_vec_vec_u32(cur: Cursor) -> list[list[int]]:
    return [read_vec_u32(cur) for _ in range(cur.compact_size())]


def parse_block(raw: bytes) -> dict[str, Any]:
    cur = Cursor(raw)
    version_signed = cur.i32()
    header = {
        "version": version_signed & 0xFFFFFFFF,
        "prev": cur.read(32),
        "merkle": cur.read(32),
        "time": cur.u32(),
        "adj_bits": cur.u32(),
        "nonce": cur.u32(),
        "bits": cur.u32(),
        "hash_pow": cur.read(32),
        "flags": cur.u8(),
    }
    header_no_flags = raw[:116]
    short_header = raw[:80]
    header["long_hash"] = sha256d(header_no_flags)
    header["short_hash"] = sha256d(short_header)

    tx_count = cur.compact_size()
    for _ in range(tx_count):
        skip_tx(cur)

    proof = {
        "version": cur.u8(),
        "tick": cur.u64(),
        "timestamp": cur.u64(),
        "target": cur.bytes_vec(),
        "vdf": cur.bytes_vec(),
        "hash": cur.bytes_vec(),
        "block_hash": cur.bytes_vec(),
        "header_prefix": cur.bytes_vec(),
        "is_solution": bool(cur.u8()),
        "model_identifier": cur.str_vec(),
        "compute_precision": cur.str_vec(),
        "ipfs_cid": cur.str_vec(),
        "extra_flags": cur.str_vec(),
        "temperature": cur.f32(),
        "top_p": cur.f32(),
        "top_k": cur.u32(),
        "repetition_penalty": cur.f32(),
        "chosen_tokens": read_vec_u32(cur),
        "chosen_probs": read_vec_f32(cur),
        "sampling_u": read_vec_f32(cur),
        "softmax_normalizers": read_vec_f32(cur),
        "prompt_tokens": read_vec_u32(cur),
        "pad_mask": cur.bytes_vec(),
        "topk_logits": read_vec_vec_f32(cur),
        "topk_indices": read_vec_vec_u32(cur),
        "logsumexp_stats": read_vec_vec_f32(cur),
    }
    cumulative_tick = cur.u64()
    if cur.pos != len(raw):
        raise ValueError(f"unconsumed bytes: {len(raw) - cur.pos} at offset {cur.pos}")

    return {
        "header": header,
        "tx_count": tx_count,
        "proof": proof,
        "cumulative_tick": cumulative_tick,
    }


def vec_u32(builder: flatbuffers.Builder, values: list[int]) -> int:
    builder.StartVector(4, len(values), 4)
    for value in reversed(values):
        builder.PrependUint32(value)
    return builder.EndVector()


def vec_f32(builder: flatbuffers.Builder, values: list[float]) -> int:
    builder.StartVector(4, len(values), 4)
    for value in reversed(values):
        builder.PrependFloat32(float(value))
    return builder.EndVector()


def vec_offsets(builder: flatbuffers.Builder, offsets: list[int]) -> int:
    builder.StartVector(4, len(offsets), 4)
    for off in reversed(offsets):
        builder.PrependUOffsetTRelative(off)
    return builder.EndVector()


def float_array(builder: flatbuffers.Builder, values: list[float]) -> int:
    values_vec = vec_f32(builder, values)
    FloatArray.FloatArrayStart(builder)
    FloatArray.FloatArrayAddValues(builder, values_vec)
    return FloatArray.FloatArrayEnd(builder)


def uint_array(builder: flatbuffers.Builder, values: list[int]) -> int:
    values_vec = vec_u32(builder, values)
    UIntArray.UIntArrayStart(builder)
    UIntArray.UIntArrayAddValues(builder, values_vec)
    return UIntArray.UIntArrayEnd(builder)


def build_validation_request(block: dict[str, Any]) -> bytes:
    proof = block["proof"]
    header = block["header"]
    builder = flatbuffers.Builder(1024 * 1024)

    target = builder.CreateByteVector(proof["target"])
    vdf = builder.CreateByteVector(proof["vdf"])
    proof_hash = builder.CreateByteVector(proof["hash"])
    proof_block_hash = builder.CreateByteVector(proof["block_hash"])
    header_prefix = builder.CreateByteVector(proof["header_prefix"])
    model = builder.CreateString(proof["model_identifier"])
    precision = builder.CreateString(proof["compute_precision"])
    cid = builder.CreateString(proof["ipfs_cid"])
    flags = builder.CreateString(proof["extra_flags"])
    chosen_tokens = vec_u32(builder, proof["chosen_tokens"])
    chosen_probs = vec_f32(builder, proof["chosen_probs"])
    sampling_u = vec_f32(builder, proof["sampling_u"])
    softmax_normalizers = vec_f32(builder, proof["softmax_normalizers"])
    prompt_tokens = vec_u32(builder, proof["prompt_tokens"])
    pad_mask = builder.CreateByteVector(proof["pad_mask"])

    logits_rows = [float_array(builder, row) for row in proof["topk_logits"]]
    logits = vec_offsets(builder, logits_rows)
    index_rows = [uint_array(builder, row) for row in proof["topk_indices"]]
    indices = vec_offsets(builder, index_rows)
    stat_rows = [float_array(builder, row) for row in proof["logsumexp_stats"]]
    stats = vec_offsets(builder, stat_rows)

    Proof.ProofStart(builder)
    Proof.ProofAddVersion(builder, proof["version"])
    Proof.ProofAddTick(builder, proof["tick"])
    Proof.ProofAddTimestamp(builder, proof["timestamp"])
    Proof.ProofAddTarget(builder, target)
    Proof.ProofAddVdf(builder, vdf)
    Proof.ProofAddHash(builder, proof_hash)
    Proof.ProofAddBlockHash(builder, proof_block_hash)
    Proof.ProofAddHeaderPrefix(builder, header_prefix)
    Proof.ProofAddIsSolution(builder, proof["is_solution"])
    Proof.ProofAddModelIdentifier(builder, model)
    Proof.ProofAddComputePrecision(builder, precision)
    Proof.ProofAddIpfsCid(builder, cid)
    Proof.ProofAddExtraFlags(builder, flags)
    Proof.ProofAddTemperature(builder, proof["temperature"])
    Proof.ProofAddTopP(builder, proof["top_p"])
    Proof.ProofAddTopK(builder, proof["top_k"])
    Proof.ProofAddRepetitionPenalty(builder, proof["repetition_penalty"])
    Proof.ProofAddChosenTokens(builder, chosen_tokens)
    Proof.ProofAddChosenProbs(builder, chosen_probs)
    Proof.ProofAddSamplingU(builder, sampling_u)
    Proof.ProofAddSoftmaxNormalizers(builder, softmax_normalizers)
    Proof.ProofAddPromptTokens(builder, prompt_tokens)
    Proof.ProofAddPadMask(builder, pad_mask)
    Proof.ProofAddTopkLogits(builder, logits)
    Proof.ProofAddTopkIndices(builder, indices)
    Proof.ProofAddLogsumexpStats(builder, stats)
    proof_offset = Proof.ProofEnd(builder)

    short_hash = builder.CreateByteVector(header["short_hash"])
    prev_hash = builder.CreateByteVector(header["prev"])
    merkle = builder.CreateByteVector(header["merkle"])
    pow_hash = builder.CreateByteVector(header["hash_pow"])

    BlockValidation.BlockValidationStart(builder)
    BlockValidation.BlockValidationAddVersion(builder, header["version"])
    BlockValidation.BlockValidationAddHash(builder, short_hash)
    BlockValidation.BlockValidationAddPrevBlockHash(builder, prev_hash)
    BlockValidation.BlockValidationAddMerkleRoot(builder, merkle)
    BlockValidation.BlockValidationAddTimestamp(builder, header["time"])
    BlockValidation.BlockValidationAddBits(builder, header["bits"])
    BlockValidation.BlockValidationAddNonce(builder, header["nonce"])
    BlockValidation.BlockValidationAddPowBlobHash(builder, pow_hash)
    BlockValidation.BlockValidationAddAdjustedBits(builder, header["adj_bits"])
    BlockValidation.BlockValidationAddPowBlob(builder, proof_offset)
    block_validation = BlockValidation.BlockValidationEnd(builder)

    hash_id = builder.CreateByteVector(header["long_hash"])
    ValidationRequest.ValidationRequestStart(builder)
    ValidationRequest.ValidationRequestAddHashId(builder, hash_id)
    ValidationRequest.ValidationRequestAddValidationType(builder, ValidationType.ValidationType.Full)
    ValidationRequest.ValidationRequestAddRequestType(builder, ValidationUnion.ValidationUnion.BlockValidation)
    ValidationRequest.ValidationRequestAddRequest(builder, block_validation)
    request = ValidationRequest.ValidationRequestEnd(builder)

    builder.Finish(request)
    return bytes(builder.Output())


def display_hex(internal_hash: bytes) -> str:
    return internal_hash[::-1].hex()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--hex-file", required=True)
    parser.add_argument("--json-file")
    parser.add_argument("--out", required=True)
    parser.add_argument("--summary-out")
    args = parser.parse_args()

    raw_hex = Path(args.hex_file).read_text().strip()
    raw = bytes.fromhex(raw_hex)
    block = parse_block(raw)
    req = build_validation_request(block)
    Path(args.out).write_bytes(req)

    summary = {
        "block_hash": display_hex(block["header"]["long_hash"]),
        "short_hash": display_hex(block["header"]["short_hash"]),
        "pow_blob_hash": display_hex(block["header"]["hash_pow"]),
        "proof_hash_hex_bytes": block["proof"]["hash"].hex(),
        "proof_hash_display_guess": display_hex(block["proof"]["hash"]),
        "model_identifier": block["proof"]["model_identifier"],
        "compute_precision": block["proof"]["compute_precision"],
        "tick": block["proof"]["tick"],
        "timestamp": block["proof"]["timestamp"],
        "top_k": block["proof"]["top_k"],
        "steps": len(block["proof"]["chosen_tokens"]),
        "prompt_tokens": len(block["proof"]["prompt_tokens"]),
        "topk_rows": len(block["proof"]["topk_logits"]),
        "topk_width_first": len(block["proof"]["topk_logits"][0]) if block["proof"]["topk_logits"] else 0,
        "tx_count": block["tx_count"],
        "cumulative_tick": block["cumulative_tick"],
        "request_bytes": len(req),
    }
    if args.json_file:
        rpc = json.loads(Path(args.json_file).read_text())
        summary["rpc_height"] = rpc.get("height")
        summary["rpc_block_hash_matches"] = rpc.get("hash") == summary["block_hash"]
        summary["rpc_short_hash_matches"] = rpc.get("shortHash") == summary["short_hash"]
        summary["rpc_pow_blob_hash_matches"] = rpc.get("hashPoW") == summary["pow_blob_hash"]

    if args.summary_out:
        Path(args.summary_out).write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    else:
        print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
