# SPDX-License-Identifier: Apache-2.0
"""
External API Stub for Verification Service

This package provides:
  - FlatBuffers conversion (MiningResponse → ValidationRequest)
  - A fully-plumbed FastAPI gateway (ZMQ backend, static auth, rate limit)
  - A Python SDK client for programmatic access

Quick start (gateway):
    uvicorn extapi_stub.app:app --host 0.0.0.0 --port 9000

Quick start (SDK):
    from extapi_stub.sdk import TensorCashVerifier
    v = TensorCashVerifier("http://localhost:9000", api_key="mykey")
    result = await v.verify_full(proof_bytes)
"""

from .builders import (
    mining_response_to_validation_request,
    proof_to_validation_request,
    extract_ids,
    response_value_to_str,
    validation_type_from_string,
)

__all__ = [
    "mining_response_to_validation_request",
    "proof_to_validation_request",
    "extract_ids",
    "response_value_to_str",
    "validation_type_from_string",
]
