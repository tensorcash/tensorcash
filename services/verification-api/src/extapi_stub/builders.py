# SPDX-License-Identifier: Apache-2.0
"""
FlatBuffers conversion helpers for the External Verification API.

This module provides pure functions to convert between MiningResponse
and ValidationRequest FlatBuffers, extract identifiers, and map enum
values to string representations.
"""

import hashlib
import struct
from typing import Dict, Optional, Tuple
import flatbuffers

# Import generated FlatBuffers modules
# In production, these are generated during Docker build
try:
    from utils.proof import (
        MiningResponse, Proof, ValidationRequest, ValidationType,
        ValidationUnion, BlockValidation, ModelValidation, ResponseValue,
        FloatArray, UIntArray,
    )
except ImportError:
    # Fallback for local development
    import sys
    import os
    sys.path.append(os.path.join(os.path.dirname(__file__), '../../../../shared-utils/fb-schemas'))
    from proof import (
        MiningResponse, Proof, ValidationRequest, ValidationType,
        ValidationUnion, BlockValidation, ModelValidation, ResponseValue,
        FloatArray, UIntArray,
    )


def _vec_u8(builder: flatbuffers.Builder, data: bytes) -> int:
    """Helper to create a vector of ubytes in FlatBuffers."""
    if not data:
        return 0
    # Use the simpler CreateByteVector method
    return builder.CreateByteVector(data)


def _vec_u32(builder: flatbuffers.Builder, data: list) -> int:
    """Create a uint32 vector."""
    if not data:
        return 0
    builder.StartVector(4, len(data), 4)
    for i in reversed(range(len(data))):
        builder.PrependUint32(data[i])
    return builder.EndVector()


def _vec_f32(builder: flatbuffers.Builder, data: list) -> int:
    """Create a float32 vector."""
    if not data:
        return 0
    builder.StartVector(4, len(data), 4)
    for i in reversed(range(len(data))):
        builder.PrependFloat32(data[i])
    return builder.EndVector()


def _vec_bool(builder: flatbuffers.Builder, data: list) -> int:
    """Create a bool vector."""
    if not data:
        return 0
    builder.StartVector(1, len(data), 1)
    for i in reversed(range(len(data))):
        builder.PrependBool(data[i])
    return builder.EndVector()


def _get_bytes_from_vector(fb_vector, length: int = 32) -> bytes:
    """Extract bytes from a FlatBuffers vector."""
    if fb_vector is None:
        return b'\x00' * length
    
    # Python FlatBuffers uses AsNumpy() for byte vectors
    try:
        import numpy as np
        # AsNumpy() returns a numpy array
        np_array = fb_vector.AsNumpy()
        return bytes(np_array.tobytes())
    except (AttributeError, ImportError):
        # Fallback to manual extraction if numpy not available
        actual_length = fb_vector.Length()
        result = bytearray(min(length, actual_length))
        for i in range(min(length, actual_length)):
            result[i] = fb_vector.Get(i)
        # Pad with zeros if needed
        if actual_length < length:
            result.extend(b'\x00' * (length - actual_length))
        return bytes(result)


def _get_vector_values(length: int, numpy_getter, value_getter) -> list:
    """Extract scalar vector contents while preserving the element type."""
    if length <= 0:
        return []

    try:
        values = numpy_getter()
        if values is not None:
            return values.tolist()
    except Exception:
        pass

    return [value_getter(i) for i in range(length)]


def _build_float_array(builder: flatbuffers.Builder, source) -> int:
    values = _get_vector_values(
        source.ValuesLength(),
        source.ValuesAsNumpy,
        source.Values,
    )
    values_vec = _vec_f32(builder, values) if values else 0
    FloatArray.FloatArrayStart(builder)
    if values_vec:
        FloatArray.FloatArrayAddValues(builder, values_vec)
    return FloatArray.FloatArrayEnd(builder)


def _build_uint_array(builder: flatbuffers.Builder, source) -> int:
    values = _get_vector_values(
        source.ValuesLength(),
        source.ValuesAsNumpy,
        source.Values,
    )
    values_vec = _vec_u32(builder, values) if values else 0
    UIntArray.UIntArrayStart(builder)
    if values_vec:
        UIntArray.UIntArrayAddValues(builder, values_vec)
    return UIntArray.UIntArrayEnd(builder)


def _vec_offsets(builder: flatbuffers.Builder, start_vector, offsets: list[int]) -> int:
    if not offsets:
        return 0
    start_vector(builder, len(offsets))
    for offset in reversed(offsets):
        builder.PrependUOffsetTRelative(offset)
    return builder.EndVector()


def _dsha256(data: bytes) -> bytes:
    return hashlib.sha256(hashlib.sha256(data).digest()).digest()


def _compact_size(size: int) -> bytes:
    if size < 0:
        raise ValueError("negative compact size")
    if size < 253:
        return struct.pack("<B", size)
    if size <= 0xffff:
        return b"\xfd" + struct.pack("<H", size)
    if size <= 0xffffffff:
        return b"\xfe" + struct.pack("<I", size)
    return b"\xff" + struct.pack("<Q", size)


def _ser_bytes(data: bytes) -> bytes:
    return _compact_size(len(data)) + data


def _ser_string(value: bytes) -> bytes:
    return _ser_bytes(value)


def _ser_u32_vec(values: list[int]) -> bytes:
    return _compact_size(len(values)) + b"".join(
        struct.pack("<I", int(v) & 0xffffffff) for v in values
    )


def _ser_f32_vec(values: list[float]) -> bytes:
    return _compact_size(len(values)) + b"".join(
        struct.pack("<f", float(v)) for v in values
    )


def _ser_nested(rows: list[list], row_serializer) -> bytes:
    return _compact_size(len(rows)) + b"".join(row_serializer(row) for row in rows)


def _proof_bytes_field(proof: Proof.Proof, name: str) -> bytes:
    length = getattr(proof, f"{name}Length")()
    if length <= 0:
        return b""
    try:
        return bytes(getattr(proof, f"{name}AsNumpy")().tobytes())
    except Exception:
        getter = getattr(proof, name)
        return bytes(getter(i) for i in range(length))


def _proof_string_field(proof: Proof.Proof, name: str) -> bytes:
    value = getattr(proof, name)()
    if not value:
        return b""
    if isinstance(value, bytes):
        return value
    return str(value).encode("utf-8")


def _proof_u32_vec(proof: Proof.Proof, name: str) -> list[int]:
    return [
        int(v) & 0xffffffff
        for v in _get_vector_values(
            getattr(proof, f"{name}Length")(),
            getattr(proof, f"{name}AsNumpy"),
            getattr(proof, name),
        )
    ]


def _proof_f32_vec(proof: Proof.Proof, name: str) -> list[float]:
    return [
        float(v)
        for v in _get_vector_values(
            getattr(proof, f"{name}Length")(),
            getattr(proof, f"{name}AsNumpy"),
            getattr(proof, name),
        )
    ]


def _proof_matrix(proof: Proof.Proof, name: str, value_type: str) -> list[list]:
    length_fn = getattr(proof, f"{name}Length", None)
    row_fn = getattr(proof, name, None)
    if length_fn is None or row_fn is None:
        return []
    rows = []
    for i in range(length_fn()):
        row = row_fn(i)
        if row is None:
            rows.append([])
            continue
        values = _get_vector_values(
            row.ValuesLength(),
            row.ValuesAsNumpy,
            row.Values,
        )
        if value_type == "u32":
            rows.append([int(v) & 0xffffffff for v in values])
        else:
            rows.append([float(v) for v in values])
    return rows


def _serialize_cproof_blob(proof: Proof.Proof) -> bytes:
    """Serialize Proof like bcore's CProofBlob::SERIALIZE_METHODS."""
    out = bytearray()
    out += struct.pack("<B", int(proof.Version() or 0) & 0xff)
    out += struct.pack("<Q", int(proof.Tick() or 0) & 0xffffffffffffffff)
    out += struct.pack("<Q", int(proof.Timestamp() or 0) & 0xffffffffffffffff)
    out += _ser_bytes(_proof_bytes_field(proof, "Target"))
    out += _ser_bytes(_proof_bytes_field(proof, "Vdf"))
    out += _ser_bytes(_proof_bytes_field(proof, "Hash"))
    out += _ser_bytes(_proof_bytes_field(proof, "BlockHash"))
    out += _ser_bytes(_proof_bytes_field(proof, "HeaderPrefix"))
    out += struct.pack("<B", 1 if proof.IsSolution() else 0)
    out += _ser_string(_proof_string_field(proof, "ModelIdentifier"))
    out += _ser_string(_proof_string_field(proof, "ComputePrecision"))
    out += _ser_string(_proof_string_field(proof, "IpfsCid"))
    out += _ser_string(_proof_string_field(proof, "ExtraFlags"))
    out += struct.pack("<f", float(proof.Temperature() or 0.0))
    out += struct.pack("<f", float(proof.TopP() or 0.0))
    out += struct.pack("<I", int(proof.TopK() or 0) & 0xffffffff)
    out += struct.pack("<f", float(proof.RepetitionPenalty() or 0.0))
    out += _ser_u32_vec(_proof_u32_vec(proof, "ChosenTokens"))
    out += _ser_f32_vec(_proof_f32_vec(proof, "ChosenProbs"))
    out += _ser_f32_vec(_proof_f32_vec(proof, "SamplingU"))
    out += _ser_f32_vec(_proof_f32_vec(proof, "SoftmaxNormalizers"))
    out += _ser_u32_vec(_proof_u32_vec(proof, "PromptTokens"))
    out += _ser_bytes(_proof_bytes_field(proof, "PadMask"))
    out += _ser_nested(
        _proof_matrix(proof, "TopkLogits", "f32"), _ser_f32_vec,
    )
    out += _ser_nested(
        _proof_matrix(proof, "TopkIndices", "u32"), _ser_u32_vec,
    )
    out += _ser_nested(
        _proof_matrix(proof, "LogsumexpStats", "f32"), _ser_f32_vec,
    )
    return bytes(out)


def _pow_leaf_hash(tag: int, data: bytes) -> bytes:
    return _dsha256(b"\xffPOW\x00" + struct.pack("<B", tag) + struct.pack("<I", len(data)) + data)


def _proof_model_hash(proof: Proof.Proof) -> bytes:
    model_identifier = _proof_string_field(proof, "ModelIdentifier")
    if b"@" not in model_identifier:
        return b"\x00" * 32
    return hashlib.sha256(model_identifier).digest()


def _canonical_pow_blob_hash(proof: Proof.Proof) -> bytes:
    """Return CProofBlob::GetMerkleRoot() for a Proof FlatBuffer."""
    l_tick = _pow_leaf_hash(
        0x01, struct.pack("<Q", int(proof.Tick() or 0) & 0xffffffffffffffff),
    )
    l_vdf = _pow_leaf_hash(0x02, _proof_bytes_field(proof, "Vdf"))
    l_meta = _pow_leaf_hash(
        0x03,
        struct.pack("<B", int(proof.Version() or 0) & 0xff)
        + _proof_model_hash(proof),
    )
    l_rest = _pow_leaf_hash(0x04, _serialize_cproof_blob(proof))
    return _dsha256(_dsha256(l_tick + l_vdf) + _dsha256(l_meta + l_rest))


def _bytes32_or_zero(data: Optional[bytes], field: str) -> bytes:
    if data is None:
        return b"\x00" * 32
    if len(data) != 32:
        raise ValueError(f"{field} must be 32 bytes")
    return data


def _compute_block_long_hash(
    *,
    version: int,
    prev_block_hash: bytes,
    merkle_root: bytes,
    timestamp: int,
    adjusted_bits: int,
    nonce: int,
    bits: int,
    pow_blob_hash: bytes,
) -> bytes:
    payload = (
        struct.pack("<i", int(version))
        + _bytes32_or_zero(prev_block_hash, "prev_block_hash")
        + _bytes32_or_zero(merkle_root, "merkle_root")
        + struct.pack("<I", int(timestamp) & 0xffffffff)
        + struct.pack("<I", int(adjusted_bits) & 0xffffffff)
        + struct.pack("<I", int(nonce) & 0xffffffff)
        + struct.pack("<I", int(bits) & 0xffffffff)
        + _bytes32_or_zero(pow_blob_hash, "pow_blob_hash")
    )
    return _dsha256(payload)


def extract_ids(buf: bytes) -> Dict[str, bytes]:
    """
    Extract hash_id and pow_blob_hash from a MiningResponse buffer.
    
    Args:
        buf: Raw bytes of a FlatBuffers MiningResponse
        
    Returns:
        Dictionary with 'hash_id' and 'pow_blob_hash' keys
    """
    try:
        mr = MiningResponse.MiningResponse.GetRootAs(buf, 0)
        pf = mr.PowBlob()
        
        # Prefer proof.hash as canonical hash_id
        hash_id = b''
        if pf and pf.HashLength() > 0:
            hash_id = _proof_bytes_field(pf, "Hash")
        
        # Fallback to pow_blob_hash if no proof hash
        if not hash_id and mr.PowBlobHashLength() > 0:
            try:
                hash_id = bytes(mr.PowBlobHashAsNumpy().tobytes())
            except Exception:
                hash_id = bytes(
                    mr.PowBlobHash(i) for i in range(mr.PowBlobHashLength())
                )
        
        # Final fallback: SHA256 of the entire buffer
        if not hash_id:
            hash_id = hashlib.sha256(buf).digest()
        
        # Get pow_blob_hash
        pow_blob_hash = b''
        if mr.PowBlobHashLength() > 0:
            try:
                pow_blob_hash = bytes(mr.PowBlobHashAsNumpy().tobytes())
            except Exception:
                pow_blob_hash = bytes(
                    mr.PowBlobHash(i) for i in range(mr.PowBlobHashLength())
                )
        
        return {
            'hash_id': hash_id,
            'pow_blob_hash': pow_blob_hash
        }
    except Exception as e:
        raise ValueError(f"Failed to extract IDs from MiningResponse: {e}")


def _rebuild_proof(builder: flatbuffers.Builder, proof: Proof.Proof) -> int:
    """Rebuild a Proof table in a new builder."""
    # Collect all proof fields
    version = proof.Version() if proof.Version() else 0
    tick = proof.Tick() if proof.Tick() else 0
    timestamp = proof.Timestamp() if proof.Timestamp() else 0
    is_solution = proof.IsSolution() if proof.IsSolution() is not None else False
    temperature = proof.Temperature() if proof.Temperature() else 0.0
    top_p = proof.TopP() if proof.TopP() else 0.0
    top_k = proof.TopK() if proof.TopK() else 0
    repetition_penalty = proof.RepetitionPenalty() if proof.RepetitionPenalty() else 0.0
    
    # Create byte vectors using proper AsNumpy() access
    target_vec = 0
    if proof.TargetLength() > 0:
        target_vec = builder.CreateByteVector(_proof_bytes_field(proof, "Target"))
    
    vdf_vec = 0
    if proof.VdfLength() > 0:
        vdf_vec = builder.CreateByteVector(_proof_bytes_field(proof, "Vdf"))
    
    hash_vec = 0
    if proof.HashLength() > 0:
        hash_vec = builder.CreateByteVector(_proof_bytes_field(proof, "Hash"))
    
    block_hash_vec = 0
    if proof.BlockHashLength() > 0:
        block_hash_vec = builder.CreateByteVector(_proof_bytes_field(proof, "BlockHash"))
    
    header_prefix_vec = 0
    if proof.HeaderPrefixLength() > 0:
        header_prefix_vec = builder.CreateByteVector(
            _proof_bytes_field(proof, "HeaderPrefix")
        )
    
    # Create string fields
    model_id_str = 0
    if proof.ModelIdentifier():
        model_id_str = builder.CreateString(proof.ModelIdentifier())
    
    compute_prec_str = 0
    if proof.ComputePrecision():
        compute_prec_str = builder.CreateString(proof.ComputePrecision())
    
    ipfs_cid_str = 0
    if proof.IpfsCid():
        ipfs_cid_str = builder.CreateString(proof.IpfsCid())
    
    extra_flags_str = 0
    if proof.ExtraFlags():
        extra_flags_str = builder.CreateString(proof.ExtraFlags())

    # ALL vectors and sub-tables MUST be created before ProofStart —
    # flatbuffers forbids nested object construction (StartVector inside
    # an open table trips assertNotNested, whose exception stringifies
    # empty; production share verification failed exactly here).
    # Preserve the original numeric vectors instead of reinterpreting
    # them as bytes.
    chosen_tokens = _get_vector_values(
        proof.ChosenTokensLength(),
        proof.ChosenTokensAsNumpy,
        proof.ChosenTokens,
    )
    chosen_tokens_vec = _vec_u32(builder, chosen_tokens) if chosen_tokens else 0

    chosen_probs = _get_vector_values(
        proof.ChosenProbsLength(),
        proof.ChosenProbsAsNumpy,
        proof.ChosenProbs,
    )
    chosen_probs_vec = _vec_f32(builder, chosen_probs) if chosen_probs else 0

    sampling_u = _get_vector_values(
        proof.SamplingULength(),
        proof.SamplingUAsNumpy,
        proof.SamplingU,
    )
    sampling_u_vec = _vec_f32(builder, sampling_u) if sampling_u else 0

    softmax_normalizers = _get_vector_values(
        proof.SoftmaxNormalizersLength(),
        proof.SoftmaxNormalizersAsNumpy,
        proof.SoftmaxNormalizers,
    )
    softmax_normalizers_vec = _vec_f32(builder, softmax_normalizers) if softmax_normalizers else 0

    prompt_tokens = _get_vector_values(
        proof.PromptTokensLength(),
        proof.PromptTokensAsNumpy,
        proof.PromptTokens,
    )
    prompt_tokens_vec = _vec_u32(builder, prompt_tokens) if prompt_tokens else 0

    pad_mask = _get_vector_values(
        proof.PadMaskLength(),
        proof.PadMaskAsNumpy,
        proof.PadMask,
    )
    pad_mask_vec = _vec_bool(builder, pad_mask) if pad_mask else 0

    topk_logits_offsets = [
        _build_float_array(builder, proof.TopkLogits(i))
        for i in range(proof.TopkLogitsLength())
        if proof.TopkLogits(i) is not None
    ]
    topk_logits_vec = _vec_offsets(builder, Proof.ProofStartTopkLogitsVector, topk_logits_offsets)

    topk_indices_offsets = [
        _build_uint_array(builder, proof.TopkIndices(i))
        for i in range(proof.TopkIndicesLength())
        if proof.TopkIndices(i) is not None
    ]
    topk_indices_vec = _vec_offsets(builder, Proof.ProofStartTopkIndicesVector, topk_indices_offsets)

    logsumexp_offsets = [
        _build_float_array(builder, proof.LogsumexpStats(i))
        for i in range(proof.LogsumexpStatsLength())
        if proof.LogsumexpStats(i) is not None
    ]
    logsumexp_vec = _vec_offsets(builder, Proof.ProofStartLogsumexpStatsVector, logsumexp_offsets)

    # Build the Proof table
    Proof.ProofStart(builder)
    Proof.ProofAddVersion(builder, version)
    Proof.ProofAddTick(builder, tick)
    Proof.ProofAddTimestamp(builder, timestamp)
    
    if target_vec:
        Proof.ProofAddTarget(builder, target_vec)
    if vdf_vec:
        Proof.ProofAddVdf(builder, vdf_vec)
    if hash_vec:
        Proof.ProofAddHash(builder, hash_vec)
    if block_hash_vec:
        Proof.ProofAddBlockHash(builder, block_hash_vec)
    if header_prefix_vec:
        Proof.ProofAddHeaderPrefix(builder, header_prefix_vec)
    
    Proof.ProofAddIsSolution(builder, is_solution)
    
    if model_id_str:
        Proof.ProofAddModelIdentifier(builder, model_id_str)
    if compute_prec_str:
        Proof.ProofAddComputePrecision(builder, compute_prec_str)
    if ipfs_cid_str:
        Proof.ProofAddIpfsCid(builder, ipfs_cid_str)
    if extra_flags_str:
        Proof.ProofAddExtraFlags(builder, extra_flags_str)
    
    Proof.ProofAddTemperature(builder, temperature)
    Proof.ProofAddTopP(builder, top_p)
    Proof.ProofAddTopK(builder, top_k)
    Proof.ProofAddRepetitionPenalty(builder, repetition_penalty)

    # Add array field vectors to the proof (created above, before ProofStart)
    if chosen_tokens_vec:
        Proof.ProofAddChosenTokens(builder, chosen_tokens_vec)
    if chosen_probs_vec:
        Proof.ProofAddChosenProbs(builder, chosen_probs_vec)
    if sampling_u_vec:
        Proof.ProofAddSamplingU(builder, sampling_u_vec)
    if softmax_normalizers_vec:
        Proof.ProofAddSoftmaxNormalizers(builder, softmax_normalizers_vec)
    if prompt_tokens_vec:
        Proof.ProofAddPromptTokens(builder, prompt_tokens_vec)
    if pad_mask_vec:
        Proof.ProofAddPadMask(builder, pad_mask_vec)
    if topk_logits_vec:
        Proof.ProofAddTopkLogits(builder, topk_logits_vec)
    if topk_indices_vec:
        Proof.ProofAddTopkIndices(builder, topk_indices_vec)
    if logsumexp_vec:
        Proof.ProofAddLogsumexpStats(builder, logsumexp_vec)
    
    return Proof.ProofEnd(builder)


def mining_response_to_validation_request(
    buf: bytes, 
    validation_type: str,
    prev_block_hash: Optional[bytes] = None,
    merkle_root: Optional[bytes] = None,
    bits: Optional[int] = None
) -> bytes:
    """
    Convert a MiningResponse buffer to a ValidationRequest buffer.
    
    Args:
        buf: Raw bytes of a FlatBuffers MiningResponse
        validation_type: "full", "quick", "quick_smell", or "model"
        prev_block_hash: Optional previous block hash (32 bytes)
        merkle_root: Optional merkle root (32 bytes)
        bits: Optional bits value

    Returns:
        Raw bytes of a FlatBuffers ValidationRequest
    """
    try:
        # Parse the MiningResponse
        mr = MiningResponse.MiningResponse.GetRootAs(buf, 0)
        pf = mr.PowBlob()

        if pf is None:
            raise ValueError("Missing Proof in MiningResponse")

        # Extract legacy identifiers. BlockValidation requests replace
        # this hash_id with bcore's canonical request-binding hash below;
        # model validation keeps the legacy extraction behaviour.
        ids = extract_ids(buf)
        hash_id = ids['hash_id']

        # Create a new builder
        builder = flatbuffers.Builder(2048)

        # Determine validation type enum. The quick tiers carry the SAME
        # BlockValidation payload as "full" — only the stamped
        # ValidationType differs; verify-service selects the lighter
        # check from the enum. Share-mode verification uses "quick_smell";
        # audit verification uses "logits" (no block sanity downstream).
        if validation_type.lower() in ("full", "quick", "quick_smell", "logits"):
            vt = validation_type_from_string(validation_type)
            union_type = ValidationUnion.ValidationUnion.BlockValidation
            
            # Build BlockValidation
            # Rebuild the proof blob
            proof_offset = _rebuild_proof(builder, pf)
            
            # Create vectors for BlockValidation using proper AsNumpy() access
            block_hash_bytes = b'\x00' * 32
            if pf.BlockHashLength() > 0:
                block_hash_bytes = _proof_bytes_field(pf, "BlockHash")
            elif pf.HashLength() > 0:
                block_hash_bytes = _proof_bytes_field(pf, "Hash")
            hash_vec = builder.CreateByteVector(block_hash_bytes)
            
            canonical_pow_blob_hash = _canonical_pow_blob_hash(pf)
            hash_id = _compute_block_long_hash(
                version=pf.Version() if pf else 1,
                prev_block_hash=prev_block_hash,
                merkle_root=merkle_root,
                timestamp=pf.Timestamp() if pf else 0,
                adjusted_bits=mr.AdjustedBits() if mr else 0,
                nonce=mr.Nonce() if mr else 0,
                bits=bits or 0,
                pow_blob_hash=canonical_pow_blob_hash,
            )

            prev_hash_vec = builder.CreateByteVector(
                _bytes32_or_zero(prev_block_hash, "prev_block_hash")
            )
            merkle_vec = builder.CreateByteVector(
                _bytes32_or_zero(merkle_root, "merkle_root")
            )
            pow_blob_hash_vec = builder.CreateByteVector(canonical_pow_blob_hash)
            
            # Build BlockValidation table
            BlockValidation.BlockValidationStart(builder)
            BlockValidation.BlockValidationAddVersion(builder, pf.Version() if pf else 1)
            BlockValidation.BlockValidationAddHash(builder, hash_vec)
            BlockValidation.BlockValidationAddPrevBlockHash(builder, prev_hash_vec)
            BlockValidation.BlockValidationAddMerkleRoot(builder, merkle_vec)
            BlockValidation.BlockValidationAddTimestamp(builder, pf.Timestamp() if pf else 0)
            BlockValidation.BlockValidationAddBits(builder, bits or 0)
            BlockValidation.BlockValidationAddNonce(builder, mr.Nonce() if mr else 0)
            BlockValidation.BlockValidationAddPowBlobHash(builder, pow_blob_hash_vec)
            BlockValidation.BlockValidationAddAdjustedBits(builder, mr.AdjustedBits() if mr else 0)
            BlockValidation.BlockValidationAddPowBlob(builder, proof_offset)
            validation_offset = BlockValidation.BlockValidationEnd(builder)
            
        elif validation_type.lower() == "model":
            vt = ValidationType.ValidationType.Model
            union_type = ValidationUnion.ValidationUnion.ModelValidation
            
            # Build ModelValidation
            model_name_str = builder.CreateString(pf.ModelIdentifier() if pf.ModelIdentifier() else "")
            model_commit_str = builder.CreateString("")  # Not available in current schema
            cid_str = builder.CreateString(pf.IpfsCid() if pf.IpfsCid() else "")
            extra_str = builder.CreateString(pf.ExtraFlags() if pf.ExtraFlags() else "")
            
            # Create byte vectors using proper AsNumpy() access
            txid_vec = builder.CreateByteVector(b'\x00' * 32)  # Not available, zero-fill
            
            block_hash_bytes = b'\x00' * 32
            if pf.BlockHashLength() > 0:
                block_hash_bytes = _proof_bytes_field(pf, "BlockHash")
            block_hash_vec = builder.CreateByteVector(block_hash_bytes)
            
            ModelValidation.ModelValidationStart(builder)
            ModelValidation.ModelValidationAddModelName(builder, model_name_str)
            ModelValidation.ModelValidationAddModelCommit(builder, model_commit_str)
            ModelValidation.ModelValidationAddDifficulty(builder, mr.Difficulty() if mr else 0)
            ModelValidation.ModelValidationAddCid(builder, cid_str)
            ModelValidation.ModelValidationAddExtra(builder, extra_str)
            ModelValidation.ModelValidationAddTxid(builder, txid_vec)
            ModelValidation.ModelValidationAddBlockHash(builder, block_hash_vec)
            ModelValidation.ModelValidationAddBlockHeight(builder, 0)  # Not available
            validation_offset = ModelValidation.ModelValidationEnd(builder)
            
        else:
            raise ValueError(f"Invalid validation_type: {validation_type}")
        
        # Build the ValidationRequest
        hash_id_vec = builder.CreateByteVector(hash_id)
        
        ValidationRequest.ValidationRequestStart(builder)
        ValidationRequest.ValidationRequestAddHashId(builder, hash_id_vec)
        ValidationRequest.ValidationRequestAddValidationType(builder, vt)
        ValidationRequest.ValidationRequestAddRequestType(builder, union_type)
        ValidationRequest.ValidationRequestAddRequest(builder, validation_offset)
        request = ValidationRequest.ValidationRequestEnd(builder)
        
        builder.Finish(request)
        return bytes(builder.Output())
        
    except Exception as e:
        raise ValueError(f"Failed to convert MiningResponse to ValidationRequest: {e}")


def proof_to_validation_request(buf: bytes, validation_type: str = "full") -> bytes:
    """
    Convert a Proof (pow blob) buffer to a ValidationRequest buffer.

    Args:
        buf: Raw bytes of a FlatBuffers Proof (root_type Proof, file_identifier "PROF")
        validation_type: "full" only (pow-only verification uses full verifier)

    Returns:
        Raw bytes of a FlatBuffers ValidationRequest
    """
    if validation_type.lower() != "full":
        raise ValueError(f"Invalid validation_type for proof payload: {validation_type}")

    try:
        proof = Proof.Proof.GetRootAs(buf, 0)
        try:
            if not Proof.ProofBufferHasIdentifier(buf, 0):
                raise ValueError("Invalid Proof buffer identifier")
        except Exception:
            # Identifier check may not be available in all environments
            pass

        # Prefer proof.hash as canonical hash_id
        hash_id = b""
        if proof.HashLength() > 0:
            hash_id = _proof_bytes_field(proof, "Hash")

        if not hash_id:
            hash_id = hashlib.sha256(buf).digest()

        builder = flatbuffers.Builder(2048)
        proof_offset = _rebuild_proof(builder, proof)

        # Zero-filled header fields (pow-only verification ignores blockchain header validity)
        zero32 = b"\x00" * 32
        hash_vec = builder.CreateByteVector(zero32)
        prev_hash_vec = builder.CreateByteVector(zero32)
        merkle_vec = builder.CreateByteVector(zero32)
        pow_blob_hash_vec = builder.CreateByteVector(hash_id if len(hash_id) == 32 else zero32)

        BlockValidation.BlockValidationStart(builder)
        BlockValidation.BlockValidationAddVersion(builder, proof.Version() if proof else 1)
        BlockValidation.BlockValidationAddHash(builder, hash_vec)
        BlockValidation.BlockValidationAddPrevBlockHash(builder, prev_hash_vec)
        BlockValidation.BlockValidationAddMerkleRoot(builder, merkle_vec)
        BlockValidation.BlockValidationAddTimestamp(builder, proof.Timestamp() if proof else 0)
        BlockValidation.BlockValidationAddBits(builder, 0)
        BlockValidation.BlockValidationAddNonce(builder, 0)
        BlockValidation.BlockValidationAddPowBlobHash(builder, pow_blob_hash_vec)
        BlockValidation.BlockValidationAddAdjustedBits(builder, 0)
        BlockValidation.BlockValidationAddPowBlob(builder, proof_offset)
        validation_offset = BlockValidation.BlockValidationEnd(builder)

        hash_id_vec = builder.CreateByteVector(hash_id if len(hash_id) == 32 else hashlib.sha256(buf).digest())

        ValidationRequest.ValidationRequestStart(builder)
        ValidationRequest.ValidationRequestAddHashId(builder, hash_id_vec)
        ValidationRequest.ValidationRequestAddValidationType(builder, ValidationType.ValidationType.Full)
        ValidationRequest.ValidationRequestAddRequestType(builder, ValidationUnion.ValidationUnion.BlockValidation)
        ValidationRequest.ValidationRequestAddRequest(builder, validation_offset)
        request = ValidationRequest.ValidationRequestEnd(builder)

        builder.Finish(request)
        return bytes(builder.Output())
    except Exception as e:
        raise ValueError(f"Failed to convert Proof to ValidationRequest: {e}")


def response_value_to_str(value: int) -> str:
    """
    Map ResponseValue enum to string representation.
    
    Args:
        value: ResponseValue enum integer
        
    Returns:
        String representation of the response value
    """
    mapping = {
        ResponseValue.ResponseValue.Quick_OK: "Quick_OK",
        ResponseValue.ResponseValue.Quick_Fail: "Quick_Fail",
        ResponseValue.ResponseValue.Quick_OK_Smell_OK: "Quick_OK_Smell_OK",
        ResponseValue.ResponseValue.Quick_OK_Smell_Fail: "Quick_OK_Smell_Fail",
        ResponseValue.ResponseValue.Quick_Fail_Smell_OK: "Quick_Fail_Smell_OK",
        ResponseValue.ResponseValue.Quick_Fail_Smell_Fail: "Quick_Fail_Smell_Fail",
        ResponseValue.ResponseValue.Full_Green: "Full_Green",
        ResponseValue.ResponseValue.Full_Amber: "Full_Amber",
        ResponseValue.ResponseValue.Full_Red: "Full_Red",
        ResponseValue.ResponseValue.Model_OK: "Model_OK",
        ResponseValue.ResponseValue.Model_Fail: "Model_Fail",
        ResponseValue.ResponseValue.Challenge_OK: "Challenge_OK",
        ResponseValue.ResponseValue.Challenge_Fail: "Challenge_Fail",
        ResponseValue.ResponseValue.Model_Pending_Review: "Model_Pending_Review",
        ResponseValue.ResponseValue.Logits_OK: "Logits_OK",
        ResponseValue.ResponseValue.Logits_Fail: "Logits_Fail",
    }
    return mapping.get(value, f"Unknown_{value}")


def validation_type_from_string(type_str: str) -> int:
    """
    Convert string validation type to enum value.
    
    Args:
        type_str: "quick", "quick_smell", "full", "model", or "challenge"
        
    Returns:
        ValidationType enum value
    """
    mapping = {
        "quick": ValidationType.ValidationType.Quick,
        "quick_smell": ValidationType.ValidationType.Quick_Smell,
        "full": ValidationType.ValidationType.Full,
        "model": ValidationType.ValidationType.Model,
        "challenge": ValidationType.ValidationType.Challenge,
        "logits": ValidationType.ValidationType.Logits,
    }
    
    value = mapping.get(type_str.lower())
    if value is None:
        raise ValueError(f"Invalid validation type: {type_str}")
    return value
