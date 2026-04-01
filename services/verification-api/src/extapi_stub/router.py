# SPDX-License-Identifier: Apache-2.0
"""
FastAPI router for the TensorCash External Verification API.

All binary verification endpoints accept ValidationRequest (FlatBuffers)
bytes, matching the gateway contract.  JSON endpoints decode base64 and
delegate to the corresponding binary handler.

Intentional simplifications vs the production verification service:
  - No P0 security (signature/replay/gzip/JWT)
  - Static-key auth + global token-bucket rate limit
  - No durable storage fallback for /public/status
"""

import asyncio
import base64
import logging
import os
import time
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

from .builders import (
    mining_response_to_validation_request,
    proof_to_validation_request,
    response_value_to_str,
)
from .cache import get_cache, make_cache_key

logger = logging.getLogger(__name__)

_STATUS_TYPES = frozenset({"full", "model", "challenge", "quick_smell", "pow", "quick", "logits"})
_MAX_BODY_MB = int(os.environ.get("VERIFY_MAX_BODY_MB", "50"))
_MAX_BODY_BYTES = _MAX_BODY_MB * 1024 * 1024

_FULL_TO_QUICK = {
    "Full_Green": "Quick_OK_Smell_OK",
    "Full_Amber": "Quick_OK_Smell_OK",
}


# ------------------------------------------------------------------ #
# Pydantic models
# ------------------------------------------------------------------ #

class FullVerificationResponse(BaseModel):
    status: str
    hash_id: str
    elapsed_ms: int
    cached: bool = False
    pow_blob_hash: str = ""


class ModelVerificationResponse(BaseModel):
    status: str
    hash_id: str
    elapsed_ms: int
    cached: bool = False
    model_identifier: str = ""
    cid: str = ""


class Base64ProofRequest(BaseModel):
    proof_b64: str


class Base64PayloadRequest(BaseModel):
    """Used by /pow/json which takes payload_b64 (not proof_b64)."""
    payload_b64: str


class StatusBatchItem(BaseModel):
    hash_id: str = Field(..., min_length=1)
    verification_type: str = Field(...)

    @field_validator("verification_type")
    @classmethod
    def normalize_verification_type(cls, value: str) -> str:
        v = value.replace("-", "_").lower()
        if v not in _STATUS_TYPES:
            raise ValueError(f"Invalid verification_type: {value}")
        return v


class StatusBatchRequest(BaseModel):
    items: List[StatusBatchItem] = Field(...)
    wait_ms: int = Field(0, ge=0, le=10000)


# ------------------------------------------------------------------ #
# Shared helpers
# ------------------------------------------------------------------ #

def _get_zmq_client():
    from .app import get_zmq_client
    return get_zmq_client()


def _hash_hex_le(hash_id: bytes) -> str:
    """Convert hash_id bytes to little-endian hex string (matches gateway)."""
    if isinstance(hash_id, (bytes, bytearray)):
        return bytes(hash_id)[::-1].hex()
    return str(hash_id)


def _is_json_accepted(accept: Optional[str]) -> bool:
    if not accept:
        return True
    a = accept.lower()
    return any(x in a for x in ("application/json", "*/*", "application/vnd.api+json"))


def _parse_hex_header(name: str, value: Optional[str], length: int = 32) -> Optional[bytes]:
    if not value:
        return None
    try:
        b = bytes.fromhex(value)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid {name} format")
    if len(b) != length:
        raise HTTPException(status_code=400, detail=f"Invalid {name} format: expected {length} bytes")
    return b


def _parse_int_header(name: str, value: Optional[str]) -> Optional[int]:
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid {name} format")


def _check_accept_ct_size(request: Request, body: bytes, accept: Optional[str]):
    """Validate Accept, Content-Type, and body size."""
    if not _is_json_accepted(accept):
        raise HTTPException(status_code=406, detail="Only JSON responses are supported")
    ct = request.headers.get("content-type", "")
    if ct != "application/octet-stream":
        raise HTTPException(status_code=400, detail="Content-Type must be application/octet-stream")
    if len(body) > _MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail="Request body too large")


# ------------------------------------------------------------------ #
# ValidationRequest parsing
# ------------------------------------------------------------------ #

def _parse_validation_request_meta(body: bytes) -> dict:
    """
    Parse a ValidationRequest FlatBuffer extracting hash_id, request_type,
    pow_blob_hash (from BlockValidation), and model_identifier.
    """
    try:
        from utils.proof import (
            ValidationRequest as VR, ValidationUnion,
            BlockValidation, ModelValidation,
        )
    except ImportError:
        import sys, os as _os
        sys.path.append(
            _os.path.join(_os.path.dirname(__file__), "../../../../shared-utils/fb-schemas")
        )
        from proof import (
            ValidationRequest as VR, ValidationUnion,
            BlockValidation, ModelValidation,
        )

    vr_cls = VR.ValidationRequest if hasattr(VR, "ValidationRequest") else VR
    union_cls = ValidationUnion.ValidationUnion if hasattr(ValidationUnion, "ValidationUnion") else ValidationUnion
    block_cls = BlockValidation.BlockValidation if hasattr(BlockValidation, "BlockValidation") else BlockValidation
    model_cls = ModelValidation.ModelValidation if hasattr(ModelValidation, "ModelValidation") else ModelValidation

    vr = vr_cls.GetRootAs(body, 0)

    length = vr.HashIdLength()
    if length > 0:
        try:
            hash_id = bytes(vr.HashIdAsNumpy().tobytes())
        except (AttributeError, TypeError):
            hash_id = bytes(vr.HashId(i) for i in range(length))
    else:
        import hashlib
        hash_id = hashlib.sha256(body).digest()

    req_type = vr.RequestType()
    pow_blob_hash = b""
    model_identifier = None

    if req_type == union_cls.BlockValidation:
        request_type = "block"
        blk = block_cls()
        blk.Init(vr.Request().Bytes, vr.Request().Pos)
        if blk.PowBlobHashLength():
            try:
                pow_blob_hash = bytes(blk.PowBlobHashAsNumpy().tobytes())
            except (AttributeError, TypeError):
                pow_blob_hash = bytes(blk.PowBlobHash(i) for i in range(blk.PowBlobHashLength()))
    elif req_type == union_cls.ModelValidation:
        request_type = "model"
        mdl = model_cls()
        mdl.Init(vr.Request().Bytes, vr.Request().Pos)
        model_identifier = mdl.ModelName() if hasattr(mdl, "ModelName") else None
    else:
        request_type = "unknown"

    return {
        "hash_id": hash_id,
        "hash_id_hex": _hash_hex_le(hash_id),
        "request_type": request_type,
        "pow_blob_hash": pow_blob_hash.hex() if pow_blob_hash else "",
        "model_identifier": model_identifier,
    }


def enforce_precheck(body: bytes, allow_skip: bool = False) -> None:
    """
    Run C++ sidecar precheck to validate hash bindings.

    Requires the vr_precheck binary (built from sidecar/vr_precheck.cpp).
    Set VERIFY_PRECHECK_ENABLED=false to skip in dev/test environments.
    """
    if allow_skip:
        return
    from .vr_precheck import precheck_validation_request
    result = precheck_validation_request(body)
    if not result or not result.get("ok", False):
        raise HTTPException(status_code=400, detail="ValidationRequest precheck failed")
    if not result.get("hash_match", True) or not result.get("pow_match", True):
        raise HTTPException(status_code=400, detail="ValidationRequest hash mismatch")


def _coerce_pow_payload(body: bytes) -> bytes:
    """
    Accept MiningResponse, Proof, or ValidationRequest payloads for /pow.
    Always returns ValidationRequest bytes.
    """
    if len(body) < 8:
        raise HTTPException(status_code=400, detail="Payload too small")

    # Try as ValidationRequest first
    try:
        meta = _parse_validation_request_meta(body)
        if meta["request_type"] == "block":
            return body
    except Exception:
        pass

    # Try as Proof (file_identifier "PROF")
    try:
        return proof_to_validation_request(body, "full")
    except Exception:
        pass

    # Try as MiningResponse
    try:
        return mining_response_to_validation_request(body, "full")
    except Exception:
        pass

    raise HTTPException(status_code=400, detail="Unrecognised payload format")


# ------------------------------------------------------------------ #
# Core verification flow (all endpoints converge here)
# ------------------------------------------------------------------ #

async def _perform_verification(
    body: bytes,
    verification_type: str,
    meta: dict,
    timeout_s: float = 60.0,
) -> dict:
    """
    Send a ValidationRequest to the engine via ZMQ.
    Handles cache, dedup, and pending lifecycle.

    Args:
        body: Raw ValidationRequest FlatBuffer bytes
        verification_type: "full", "model", "quick", "quick_smell", "pow", "logits"
        meta: Output of _parse_validation_request_meta()
    """
    t0 = time.monotonic()
    cache = get_cache()

    hash_id_hex = meta["hash_id_hex"]
    hash_id_bytes = meta["hash_id"]
    pow_hex = meta.get("pow_blob_hash", "")
    cache_key = make_cache_key(verification_type, hash_id_hex, pow_hex)

    # Cache hit
    cached = cache.get(cache_key)
    if cached is not None:
        return {**cached, "cached": True, "elapsed_ms": int((time.monotonic() - t0) * 1000)}

    # Dedup / coalesce
    loop = asyncio.get_running_loop()
    fut, is_owner = cache.get_or_create_inflight(cache_key, loop)

    if not is_owner:
        try:
            result = await asyncio.wait_for(asyncio.shield(fut), timeout=timeout_s)
            return {**result, "cached": True, "elapsed_ms": int((time.monotonic() - t0) * 1000)}
        except asyncio.TimeoutError:
            raise HTTPException(status_code=504, detail="Verification engine timeout")
        except Exception as e:
            raise HTTPException(status_code=503, detail=f"Verification engine error: {e}")

    cache.mark_computing(cache_key)

    # Send to ZMQ
    client = _get_zmq_client()
    try:
        zmq_fut = client.send(hash_id_bytes, body, request_kind=verification_type)
        status_str = await asyncio.wait_for(zmq_fut, timeout=timeout_s)
    except asyncio.TimeoutError:
        cache.reject_inflight(cache_key, TimeoutError("Verification engine timeout"))
        raise HTTPException(status_code=504, detail="Verification engine timeout")
    except Exception as e:
        cache.reject_inflight(cache_key, e)
        raise HTTPException(status_code=503, detail=f"Verification engine error: {e}")

    # Intercept non-terminal Model_Pending_Review before caching.
    # Resolve the in-flight future with a pending dict so coalesced
    # non-owner callers also get the pending response (not a cancellation error).
    if status_str == "Model_Pending_Review":
        pending_result = {
            "status": "pending",
            "state": "pending_operator_review",
            "hash_id": hash_id_hex,
            "verification_type": verification_type,
        }
        cache.mark_pending(cache_key, state="pending_operator_review")
        with cache._inflight_lock:
            inflight_fut = cache._inflight.pop(cache_key, None)
        if inflight_fut and not inflight_fut.done():
            inflight_fut.get_loop().call_soon_threadsafe(inflight_fut.set_result, pending_result)
        return pending_result

    elapsed_ms = int((time.monotonic() - t0) * 1000)
    result: dict = {
        "status": status_str,
        "hash_id": hash_id_hex,
        "elapsed_ms": elapsed_ms,
        "cached": False,
    }

    if verification_type in ("full", "pow", "quick", "quick_smell", "logits") and pow_hex:
        result["pow_blob_hash"] = pow_hex
    if verification_type == "model":
        result["model_identifier"] = meta.get("model_identifier") or ""

    cache.resolve_inflight(cache_key, result, hash_id=hash_id_hex)
    return result


def _pending_response(
    hash_id_hex: str,
    verification_type: str,
    pow_blob_hash: str = "",
    state: str = "pending",
    accepted_at: Optional[float] = None,
) -> dict:
    """Build a pending-lifecycle response matching the gateway shape."""
    payload: dict = {
        "status": "pending",
        "hash_id": hash_id_hex,
        "verification_type": verification_type,
        "state": state,
    }
    if pow_blob_hash:
        payload["pow_blob_hash"] = pow_blob_hash
    if accepted_at is not None:
        payload["accepted_at"] = accepted_at
    return payload


async def _do_submit(
    body: bytes,
    expected_request_type: str,
    verification_type: str,
) -> JSONResponse:
    """
    Async submit: validate, check cache/inflight, dispatch background
    task, return 202.  Background task caches the result for polling.
    """
    cache = get_cache()

    try:
        meta = _parse_validation_request_meta(body)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid ValidationRequest: {e}")

    if meta["request_type"] != expected_request_type:
        raise HTTPException(
            status_code=400,
            detail=f"Request must be {'BlockValidation' if expected_request_type == 'block' else 'ModelValidation'}",
        )

    if expected_request_type == "block":
        enforce_precheck(body)

    hash_id_hex = meta["hash_id_hex"]
    hash_id_bytes = meta["hash_id"]
    pow_hex = meta.get("pow_blob_hash", "")
    cache_key = make_cache_key(verification_type, hash_id_hex, pow_hex)

    # Already completed
    cached = cache.get(cache_key)
    if cached is not None:
        cached["cached"] = True
        return JSONResponse(content=cached)

    # Already in-flight / pending
    if cache.is_pending_or_inflight(cache_key):
        pending_meta = cache.get_pending(cache_key)
        return JSONResponse(
            status_code=202,
            content=_pending_response(
                hash_id_hex, verification_type, pow_hex,
                state=pending_meta.get("state", "pending") if pending_meta else "pending",
                accepted_at=pending_meta.get("ts") if pending_meta else None,
            ),
        )

    # Dispatch
    now = time.time()
    cache.mark_pending(cache_key, accepted_at=now)
    client = _get_zmq_client()
    try:
        zmq_fut = client.send(hash_id_bytes, body, request_kind=verification_type)
    except Exception as e:
        cache.clear_pending(cache_key)
        raise HTTPException(status_code=503, detail=f"Verification engine error: {e}")

    cache.mark_computing(cache_key)

    async def _bg():
        try:
            status_str = await asyncio.wait_for(zmq_fut, timeout=120.0)
            # Non-terminal model review — keep pending lifecycle, don't cache as terminal
            if status_str == "Model_Pending_Review":
                cache.mark_pending(cache_key, state="pending_operator_review")
                return
            result = {
                "status": status_str,
                "hash_id": hash_id_hex,
                "elapsed_ms": 0,
                "cached": False,
            }
            if pow_hex:
                result["pow_blob_hash"] = pow_hex
            cache.put(cache_key, result, hash_id=hash_id_hex)
        except Exception as exc:
            logger.error("Background verify failed for %s: %s", hash_id_hex, exc)
            cache.clear_pending(cache_key)

    asyncio.create_task(_bg())

    return JSONResponse(
        status_code=202,
        content=_pending_response(hash_id_hex, verification_type, pow_hex, state="computing", accepted_at=now),
    )


# ------------------------------------------------------------------ #
# Router
# ------------------------------------------------------------------ #

router = APIRouter(prefix="/v1", tags=["verification"])


# ==================== Binary endpoints (accept ValidationRequest) =========

@router.post("/verify/full")
async def verify_full(
    request: Request,
    body: bytes = Body(..., media_type="application/octet-stream"),
    details: bool = Query(False, description="Include failure details in response"),
    accept: Optional[str] = Header(None),
    x_verify_details: Optional[str] = Header(None),
    x_prev_block_hash: Optional[str] = Header(None),
    x_merkle_root: Optional[str] = Header(None),
    x_bits: Optional[str] = Header(None),
):
    _check_accept_ct_size(request, body, accept)
    _parse_hex_header("X-Prev-Block-Hash", x_prev_block_hash)
    _parse_hex_header("X-Merkle-Root", x_merkle_root)
    _parse_int_header("X-Bits", x_bits)
    meta = _parse_validation_request_meta(body)
    if meta["request_type"] != "block":
        raise HTTPException(status_code=400, detail="Request must be BlockValidation")
    enforce_precheck(body)
    return JSONResponse(content=await _perform_verification(body, "full", meta))


@router.post("/verify/model")
async def verify_model(
    request: Request,
    body: bytes = Body(..., media_type="application/octet-stream"),
    details: bool = Query(False, description="Include failure details in response"),
    accept: Optional[str] = Header(None),
    x_verify_details: Optional[str] = Header(None),
    x_prev_block_hash: Optional[str] = Header(None),
    x_merkle_root: Optional[str] = Header(None),
    x_bits: Optional[str] = Header(None),
):
    _check_accept_ct_size(request, body, accept)
    _parse_hex_header("X-Prev-Block-Hash", x_prev_block_hash)
    _parse_hex_header("X-Merkle-Root", x_merkle_root)
    _parse_int_header("X-Bits", x_bits)
    meta = _parse_validation_request_meta(body)
    if meta["request_type"] != "model":
        raise HTTPException(status_code=400, detail="Request must be ModelValidation")
    result = await _perform_verification(body, "model", meta)
    status_code = 202 if result.get("status") == "pending" else 200
    return JSONResponse(content=result, status_code=status_code)


@router.post("/verify/pow")
async def verify_pow(
    request: Request,
    body: bytes = Body(..., media_type="application/octet-stream"),
    details: bool = Query(False, description="Include failure details in response"),
    accept: Optional[str] = Header(None),
    x_verify_details: Optional[str] = Header(None),
):
    """Pow-blob-only verification.  Accepts MiningResponse, Proof, or ValidationRequest."""
    _check_accept_ct_size(request, body, accept)

    vr_body = _coerce_pow_payload(body)
    meta = _parse_validation_request_meta(vr_body)
    if meta["request_type"] != "block":
        raise HTTPException(status_code=400, detail="Request must be BlockValidation")
    return JSONResponse(content=await _perform_verification(vr_body, "pow", meta))


@router.post("/verify/quick")
async def verify_quick(
    request: Request,
    body: bytes = Body(..., media_type="application/octet-stream"),
    details: bool = Query(False, description="Include failure details in response"),
    accept: Optional[str] = Header(None),
    x_verify_details: Optional[str] = Header(None),
    x_prev_block_hash: Optional[str] = Header(None),
    x_merkle_root: Optional[str] = Header(None),
    x_bits: Optional[str] = Header(None),
):
    _check_accept_ct_size(request, body, accept)
    _parse_hex_header("X-Prev-Block-Hash", x_prev_block_hash)
    _parse_hex_header("X-Merkle-Root", x_merkle_root)
    _parse_int_header("X-Bits", x_bits)
    meta = _parse_validation_request_meta(body)
    if meta["request_type"] != "block":
        raise HTTPException(status_code=400, detail="Request must be BlockValidation")
    enforce_precheck(body)
    return JSONResponse(content=await _perform_verification(body, "quick", meta))


@router.post("/verify/quick-smell")
async def verify_quick_smell(
    request: Request,
    body: bytes = Body(..., media_type="application/octet-stream"),
    details: bool = Query(False, description="Include failure details in response"),
    accept: Optional[str] = Header(None),
    x_verify_details: Optional[str] = Header(None),
    x_prev_block_hash: Optional[str] = Header(None),
    x_merkle_root: Optional[str] = Header(None),
    x_bits: Optional[str] = Header(None),
):
    _check_accept_ct_size(request, body, accept)
    _parse_hex_header("X-Prev-Block-Hash", x_prev_block_hash)
    _parse_hex_header("X-Merkle-Root", x_merkle_root)
    _parse_int_header("X-Bits", x_bits)
    meta = _parse_validation_request_meta(body)
    if meta["request_type"] != "block":
        raise HTTPException(status_code=400, detail="Request must be BlockValidation")
    enforce_precheck(body)
    return JSONResponse(content=await _perform_verification(body, "quick_smell", meta))


@router.post("/verify/logits")
async def verify_logits(
    request: Request,
    body: bytes = Body(..., media_type="application/octet-stream"),
    details: bool = Query(False, description="Include failure details in response"),
    accept: Optional[str] = Header(None),
    x_verify_details: Optional[str] = Header(None),
):
    _check_accept_ct_size(request, body, accept)
    meta = _parse_validation_request_meta(body)
    if meta["request_type"] != "block":
        raise HTTPException(status_code=400, detail="Request must be BlockValidation")
    enforce_precheck(body, allow_skip=True)
    return JSONResponse(content=await _perform_verification(body, "logits", meta))


# ==================== JSON endpoints (decode + delegate to binary) =========

@router.post("/verify/full/json")
async def verify_full_json(
    request: Request,
    proof_b64: str = Body(..., embed=True),
    details: bool = Query(False),
    accept: Optional[str] = Header(None),
    x_verify_details: Optional[str] = Header(None),
    x_prev_block_hash: Optional[str] = Header(None),
    x_merkle_root: Optional[str] = Header(None),
    x_bits: Optional[str] = Header(None),
):
    try:
        body = base64.b64decode(proof_b64)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid base64 encoding")
    return await verify_full(
        request=request, body=body, details=details, accept=accept,
        x_verify_details=x_verify_details,
        x_prev_block_hash=x_prev_block_hash, x_merkle_root=x_merkle_root, x_bits=x_bits,
    )


@router.post("/verify/model/json")
async def verify_model_json(
    request: Request,
    proof_b64: str = Body(..., embed=True),
    details: bool = Query(False),
    accept: Optional[str] = Header(None),
    x_verify_details: Optional[str] = Header(None),
    x_prev_block_hash: Optional[str] = Header(None),
    x_merkle_root: Optional[str] = Header(None),
    x_bits: Optional[str] = Header(None),
):
    try:
        body = base64.b64decode(proof_b64)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid base64 encoding")
    return await verify_model(
        request=request, body=body, details=details, accept=accept,
        x_verify_details=x_verify_details,
        x_prev_block_hash=x_prev_block_hash, x_merkle_root=x_merkle_root, x_bits=x_bits,
    )


@router.post("/verify/pow/json")
async def verify_pow_json(
    request: Request,
    payload_b64: str = Body(..., embed=True),
    details: bool = Query(False),
    accept: Optional[str] = Header(None),
    x_verify_details: Optional[str] = Header(None),
):
    try:
        body = base64.b64decode(payload_b64)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid base64 encoding")
    return await verify_pow(
        request=request, body=body, details=details, accept=accept,
        x_verify_details=x_verify_details,
    )


@router.post("/verify/quick/json")
async def verify_quick_json(
    request: Request,
    proof_b64: str = Body(..., embed=True),
    details: bool = Query(False),
    accept: Optional[str] = Header(None),
    x_verify_details: Optional[str] = Header(None),
    x_prev_block_hash: Optional[str] = Header(None),
    x_merkle_root: Optional[str] = Header(None),
    x_bits: Optional[str] = Header(None),
):
    try:
        body = base64.b64decode(proof_b64)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid base64 encoding")
    return await verify_quick(
        request=request, body=body, details=details, accept=accept,
        x_verify_details=x_verify_details,
        x_prev_block_hash=x_prev_block_hash, x_merkle_root=x_merkle_root, x_bits=x_bits,
    )


@router.post("/verify/quick-smell/json")
async def verify_quick_smell_json(
    request: Request,
    proof_b64: str = Body(..., embed=True),
    details: bool = Query(False),
    accept: Optional[str] = Header(None),
    x_verify_details: Optional[str] = Header(None),
    x_prev_block_hash: Optional[str] = Header(None),
    x_merkle_root: Optional[str] = Header(None),
    x_bits: Optional[str] = Header(None),
):
    try:
        body = base64.b64decode(proof_b64)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid base64 encoding")
    return await verify_quick_smell(
        request=request, body=body, details=details, accept=accept,
        x_verify_details=x_verify_details,
        x_prev_block_hash=x_prev_block_hash, x_merkle_root=x_merkle_root, x_bits=x_bits,
    )


@router.post("/verify/logits/json")
async def verify_logits_json(
    request: Request,
    proof_b64: str = Body(..., embed=True),
    details: bool = Query(False),
    accept: Optional[str] = Header(None),
    x_verify_details: Optional[str] = Header(None),
):
    try:
        body = base64.b64decode(proof_b64)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid base64 encoding")
    return await verify_logits(
        request=request, body=body, details=details, accept=accept,
        x_verify_details=x_verify_details,
    )


# ==================== ValidationRequest delegation ========================

@router.post("/verify/full/request")
async def verify_full_request(
    request: Request,
    body: bytes = Body(..., media_type="application/octet-stream"),
    accept: Optional[str] = Header(None),
):
    _check_accept_ct_size(request, body, accept)
    meta = _parse_validation_request_meta(body)
    if meta["request_type"] != "block":
        raise HTTPException(status_code=400, detail="Request must be BlockValidation")
    enforce_precheck(body)
    return JSONResponse(content=await _perform_verification(body, "full", meta))


@router.post("/verify/model/request")
async def verify_model_request(
    request: Request,
    body: bytes = Body(..., media_type="application/octet-stream"),
    accept: Optional[str] = Header(None),
):
    _check_accept_ct_size(request, body, accept)
    meta = _parse_validation_request_meta(body)
    if meta["request_type"] != "model":
        raise HTTPException(status_code=400, detail="Request must be ModelValidation")
    result = await _perform_verification(body, "model", meta)
    status_code = 202 if result.get("status") == "pending" else 200
    return JSONResponse(content=result, status_code=status_code)


@router.post("/verify/challenge/request")
async def verify_challenge_request(
    request: Request,
    body: bytes = Body(..., media_type="application/octet-stream"),
    accept: Optional[str] = Header(None),
):
    _check_accept_ct_size(request, body, accept)
    meta = _parse_validation_request_meta(body)
    if meta["request_type"] != "block":
        raise HTTPException(status_code=400, detail="Request must be BlockValidation")
    enforce_precheck(body)
    result = await _perform_verification(body, "challenge", meta)
    status_code = 202 if result.get("status") == "pending" else 200
    return JSONResponse(content=result, status_code=status_code)


@router.post("/verify/full/request/submit")
async def verify_full_request_submit(
    request: Request,
    body: bytes = Body(..., media_type="application/octet-stream"),
    accept: Optional[str] = Header(None),
):
    _check_accept_ct_size(request, body, accept)
    return await _do_submit(body, "block", "full")


@router.post("/verify/model/request/submit")
async def verify_model_request_submit(
    request: Request,
    body: bytes = Body(..., media_type="application/octet-stream"),
    accept: Optional[str] = Header(None),
):
    _check_accept_ct_size(request, body, accept)
    return await _do_submit(body, "model", "model")


@router.post("/verify/challenge/request/submit")
async def verify_challenge_request_submit(
    request: Request,
    body: bytes = Body(..., media_type="application/octet-stream"),
    accept: Optional[str] = Header(None),
):
    _check_accept_ct_size(request, body, accept)
    return await _do_submit(body, "block", "challenge")


@router.post("/verify/quick-smell/request/submit")
async def verify_quick_smell_request_submit(
    request: Request,
    body: bytes = Body(..., media_type="application/octet-stream"),
    accept: Optional[str] = Header(None),
):
    _check_accept_ct_size(request, body, accept)
    return await _do_submit(body, "block", "quick_smell")


# ==================== Status polling ======================================

def _enrich_status(result: dict, verification_type: str) -> dict:
    """Add cached and verification_type fields to a status response."""
    return {**result, "cached": True, "verification_type": verification_type}


def _status_by_type(hash_id: str, verification_type: str):
    """Check cache → inflight/pending → None for a single type."""
    cache = get_cache()
    result = cache.get_by_hash_id(hash_id, verification_type=verification_type)
    if result is not None:
        return JSONResponse(content=_enrich_status(result, verification_type))
    # Check for pending lifecycle or in-flight future
    # Try to find a pending entry with lifecycle metadata
    candidate_key = make_cache_key(verification_type, hash_id)
    pending_meta = cache.get_pending(candidate_key)
    if pending_meta is not None:
        return JSONResponse(
            status_code=202,
            content=_pending_response(
                hash_id, verification_type,
                state=pending_meta.get("state", "pending"),
                accepted_at=pending_meta.get("ts"),
            ),
        )
    if cache.has_inflight_for_hash(hash_id, verification_type=verification_type):
        return JSONResponse(
            status_code=202,
            content=_pending_response(hash_id, verification_type, state="computing"),
        )
    return None


@router.get("/verify/status/{hash_id}")
async def get_verification_status(hash_id: str):
    """Search all types; return 200 (complete), 202 (pending), or 404."""
    for vtype in ("full", "quick_smell", "model", "challenge", "pow"):
        resp = _status_by_type(hash_id, vtype)
        if resp is not None:
            return resp
    raise HTTPException(status_code=404, detail="No cached result found for this hash_id")


@router.get("/verify/status/full/{hash_id}")
async def get_status_full(hash_id: str):
    resp = _status_by_type(hash_id, "full")
    if resp is not None:
        return resp
    raise HTTPException(status_code=404, detail="No cached result found")


@router.get("/verify/status/model/{hash_id}")
async def get_status_model(hash_id: str):
    resp = _status_by_type(hash_id, "model")
    if resp is not None:
        return resp
    raise HTTPException(status_code=404, detail="No cached result found")


@router.get("/verify/status/challenge/{hash_id}")
async def get_status_challenge(hash_id: str):
    resp = _status_by_type(hash_id, "challenge")
    if resp is not None:
        return resp
    raise HTTPException(status_code=404, detail="No cached result found")


@router.get("/verify/status/quick-smell/{hash_id}")
async def get_status_quick_smell(hash_id: str):
    resp = _status_by_type(hash_id, "quick_smell")
    if resp is not None:
        return resp
    raise HTTPException(status_code=404, detail="No cached result found")


@router.post("/verify/status/batch")
async def get_status_batch(payload: StatusBatchRequest):
    """
    Batch status polling — matches gateway contract:
    {completed, still_pending, waited_ms, server_ts}.
    Only items with actual in-flight/lifecycle state appear in still_pending.
    """
    cache = get_cache()
    start = time.monotonic()

    if not payload.items:
        raise HTTPException(status_code=400, detail="items must not be empty")

    def _check():
        completed: list = []
        pending: list = []
        for item in payload.items:
            result = cache.get_by_hash_id(item.hash_id, verification_type=item.verification_type)
            if result is not None:
                completed.append(_enrich_status(result, item.verification_type))
            else:
                # If requesting a narrower type, check if a full result can satisfy it
                if item.verification_type in ("quick", "quick_smell") and not result:
                    full_result = cache.get_by_hash_id(item.hash_id, verification_type="full")
                    if full_result:
                        full_status = full_result.get("status", "")
                        mapped = _FULL_TO_QUICK.get(full_status)
                        if mapped:
                            completed.append({
                                "status": mapped,
                                "hash_id": item.hash_id,
                                "verification_type": item.verification_type,
                                "cached": True,
                                "elapsed_ms": full_result.get("elapsed_ms", 0),
                            })
                            continue
                if cache.has_inflight_for_hash(item.hash_id, verification_type=item.verification_type):
                    # Enrich pending item with lifecycle state so node can
                    # distinguish compute-pending from review-pending
                    pending_item = item.model_dump()
                    pending_key = make_cache_key(item.verification_type, item.hash_id)
                    pending_meta = cache.get_pending(pending_key)
                    if pending_meta:
                        pending_item["state"] = pending_meta.get("state", "pending")
                    pending.append(pending_item)
                # else: not found and not pending — omitted from response
        return completed, pending

    completed, pending = _check()

    if not completed and payload.wait_ms > 0:
        deadline = start + (payload.wait_ms / 1000.0)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            await asyncio.sleep(min(0.05, remaining))
            completed, pending = _check()
            if completed:
                break

    waited_ms = int((time.monotonic() - start) * 1000)
    return JSONResponse(content={
        "completed": completed,
        "still_pending": pending,
        "waited_ms": waited_ms,
        "server_ts": time.time(),
    })


# ==================== Public status (unauthenticated) =====================

@router.get("/public/status/{hash_id}")
async def get_public_status(
    hash_id: str,
    verification_type: Optional[str] = Query(None, description="Filter by type"),
):
    """
    Public read-only status. Returns {"status": "NAN"} if not found.
    Note: no durable-storage fallback in the open-source build.
    """
    cache = get_cache()
    types_to_check = [verification_type] if verification_type else ["full", "quick_smell", "model", "challenge", "pow"]

    for vtype in types_to_check:
        result = cache.get_by_hash_id(hash_id, verification_type=vtype)
        if result is not None:
            return JSONResponse(content={
                "status": result.get("status", "Unknown"),
                "hash_id": hash_id,
                "verification_type": vtype,
                "cached": True,
            })

    if verification_type and verification_type != "full":
        full_result = cache.get_by_hash_id(hash_id, verification_type="full")
        mapped = _FULL_TO_QUICK.get(full_result.get("status", "")) if full_result else None
        if mapped:
            return JSONResponse(content={
                "status": mapped,
                "hash_id": hash_id,
                "verification_type": verification_type,
                "cached": True,
            })

    # Check pending state before returning NAN — the node needs to know
    # about pending operator reviews even when no terminal result is cached.
    for vtype in types_to_check:
        pending_key = make_cache_key(vtype, hash_id)
        pending_meta = cache.get_pending(pending_key)
        if pending_meta and pending_meta.get("state") == "pending_operator_review":
            return JSONResponse(content={
                "status": "pending",
                "state": "pending_operator_review",
                "hash_id": hash_id,
                "verification_type": vtype,
            })

    return JSONResponse(content={"status": "NAN", "hash_id": hash_id})


# ==================== Health ==============================================

@router.get("/verify/health")
async def health_check():
    try:
        client = _get_zmq_client()
        zmq_ok = client.connected
    except Exception:
        zmq_ok = False

    return {
        "status": "healthy" if zmq_ok else "degraded",
        "service": "tensorcash-verification-api",
        "backend": {"zmq": "connected" if zmq_ok else "disconnected"},
        "cache": get_cache().stats(),
    }
