# SPDX-License-Identifier: Apache-2.0
import flatbuffers
import os
import random
from typing import Optional

# Import schema modules from utils.proof (alias provided by conftest)
from utils.proof import (
    BlockValidation,
    ModelValidation,
    ValidationRequest,
    ValidationType,
    ValidationUnion,
)


def _vec_u8(builder: flatbuffers.Builder, data: bytes) -> int:
    ValidationRequest.StartHashIdVector(builder, len(data))
    for b in reversed(data):
        builder.PrependUint8(b)
    return builder.EndVector()


def build_block_validation_request(
    *,
    hash_id: bytes,
    prev_hash: Optional[bytes] = None,
    validation_type: int,
) -> bytes:
    """Build a ValidationRequest with a BlockValidation payload.

    Only fields required by AsyncValidator are populated (hash_id and prev_block_hash).
    """
    assert isinstance(hash_id, (bytes, bytearray)) and len(hash_id) == 32
    if prev_hash is None:
        prev_hash = bytes(32)
    assert len(prev_hash) == 32

    builder = flatbuffers.Builder(256)

    # Build vectors for block fields
    blk_hash_vec = _vec_u8(builder, os.urandom(32))
    prev_hash_vec = _vec_u8(builder, prev_hash)
    merkle_vec = _vec_u8(builder, os.urandom(32))
    pow_blob_hash_vec = _vec_u8(builder, os.urandom(32))

    # Build BlockValidation table
    BlockValidation.Start(builder)
    BlockValidation.AddVersion(builder, 1)
    BlockValidation.AddHash(builder, blk_hash_vec)
    BlockValidation.AddPrevBlockHash(builder, prev_hash_vec)
    BlockValidation.AddMerkleRoot(builder, merkle_vec)
    BlockValidation.AddTimestamp(builder, random.randint(0, 2**31 - 1))
    BlockValidation.AddBits(builder, 0)
    BlockValidation.AddNonce(builder, 0)
    BlockValidation.AddPowBlobHash(builder, pow_blob_hash_vec)
    BlockValidation.AddAdjustedBits(builder, 0)
    blk = BlockValidation.End(builder)

    # hash_id vector
    hash_vec = _vec_u8(builder, hash_id)

    # Build ValidationRequest
    ValidationRequest.Start(builder)
    ValidationRequest.AddHashId(builder, hash_vec)
    ValidationRequest.AddValidationType(builder, validation_type)
    ValidationRequest.AddRequestType(builder, ValidationUnion.ValidationUnion.BlockValidation)
    ValidationRequest.AddRequest(builder, blk)
    req = ValidationRequest.End(builder)

    builder.Finish(req)
    return builder.Output()


def build_model_validation_request(
    *,
    hash_id: bytes,
    model_name: str = "m",
    model_commit: str = "deadbeef",
    difficulty: int = 1,
    cid: str = "cid",
    extra: str = "",
    txid: Optional[bytes] = None,
    block_hash: Optional[bytes] = None,
    block_height: int = 0,
) -> bytes:
    assert isinstance(hash_id, (bytes, bytearray)) and len(hash_id) == 32
    if txid is None:
        txid = bytes(32)
    if block_hash is None:
        block_hash = bytes(32)

    builder = flatbuffers.Builder(256)

    # Strings
    name_off = builder.CreateString(model_name)
    commit_off = builder.CreateString(model_commit)
    cid_off = builder.CreateString(cid)
    extra_off = builder.CreateString(extra)

    txid_vec = _vec_u8(builder, txid)
    bh_vec = _vec_u8(builder, block_hash)

    # Build ModelValidation
    ModelValidation.Start(builder)
    ModelValidation.AddModelName(builder, name_off)
    ModelValidation.AddModelCommit(builder, commit_off)
    ModelValidation.AddDifficulty(builder, difficulty)
    ModelValidation.AddCid(builder, cid_off)
    ModelValidation.AddExtra(builder, extra_off)
    ModelValidation.AddTxid(builder, txid_vec)
    ModelValidation.AddBlockHash(builder, bh_vec)
    ModelValidation.AddBlockHeight(builder, block_height)
    mdl = ModelValidation.End(builder)

    hash_vec = _vec_u8(builder, hash_id)

    # Build ValidationRequest
    ValidationRequest.Start(builder)
    ValidationRequest.AddHashId(builder, hash_vec)
    ValidationRequest.AddValidationType(builder, ValidationType.ValidationType.Model)
    ValidationRequest.AddRequestType(builder, ValidationUnion.ValidationUnion.ModelValidation)
    ValidationRequest.AddRequest(builder, mdl)
    req = ValidationRequest.End(builder)

    builder.Finish(req)
    return builder.Output()
