# SPDX-License-Identifier: Apache-2.0
"""
FastAPI application for the TensorCash external verification API.

This is the open-source entrypoint that wires up the extapi_stub router
with ZMQ backend, static-key auth, and token-bucket rate limiting.

    uvicorn extapi_stub.app:create_app --factory --host 0.0.0.0 --port 9000

Environment variables — see each module for full list:
    VERIFY_ZMQ_PUSH_ENDPOINT    tcp://localhost:6001
    VERIFY_ZMQ_PULL_BIND        tcp://*:7001
    VERIFY_AUTH_ENABLED          false
    VERIFY_API_KEYS              key1,key2
    VERIFY_RATE_LIMIT_PER_MIN   5000
"""

import asyncio
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from .router import router
from .zmq_client import ZmqVerifyClient
from .auth import StaticKeyAuthMiddleware
from .ratelimit import RateLimitMiddleware

logger = logging.getLogger(__name__)

# Global ZMQ client — shared by all request handlers
_zmq_client: ZmqVerifyClient | None = None


def get_zmq_client() -> ZmqVerifyClient:
    """Return the singleton ZMQ client (available after startup)."""
    if _zmq_client is None:
        raise RuntimeError("ZMQ client not initialised — app not started?")
    return _zmq_client


@asynccontextmanager
async def _lifespan(app: FastAPI):
    global _zmq_client

    push_ep = os.environ.get("VERIFY_ZMQ_PUSH_ENDPOINT", "tcp://localhost:6001")
    pull_bind = os.environ.get("VERIFY_ZMQ_PULL_BIND", "tcp://*:7001")
    timeout = int(os.environ.get("VERIFY_ZMQ_TIMEOUT_MS", "60000"))

    _zmq_client = ZmqVerifyClient(
        push_endpoint=push_ep,
        pull_bind=pull_bind,
        recv_timeout_ms=timeout,
    )
    # Wire orphan response callback: when a model terminal result arrives
    # after the original future expired (operator approved hours later),
    # cache it so HTTP status polls pick it up.
    def _handle_orphan_response(hash_id_hex_be: str, status_str: str, enum_val: int):
        """Cache terminal model results that arrive after the original future expired.

        hash_id_hex_be is big-endian hex from the raw ZMQ response.
        The cache uses little-endian hex (matching _hash_hex_le in router.py),
        so we must reverse before caching.
        """
        from .cache import get_cache, make_cache_key
        # Convert big-endian hex → bytes → little-endian hex
        try:
            hash_bytes = bytes.fromhex(hash_id_hex_be)
            hash_id_hex = hash_bytes[::-1].hex()
        except (ValueError, TypeError):
            hash_id_hex = hash_id_hex_be  # Fallback — use as-is
        cache = get_cache()
        cache_key = make_cache_key("model", hash_id_hex)
        result = {
            "status": status_str,
            "hash_id": hash_id_hex,
            "elapsed_ms": 0,
            "cached": False,
        }
        cache.put(cache_key, result, hash_id=hash_id_hex)
        # clear_pending is called inside put() automatically
        logger.info(
            "Orphan model response cached: hash=%s status=%s",
            hash_id_hex[:16], status_str,
        )

    _zmq_client.on_orphan_response = _handle_orphan_response
    _zmq_client.start(asyncio.get_running_loop())
    logger.info("Verification gateway started (push=%s, pull=%s)", push_ep, pull_bind)

    yield

    _zmq_client.stop()
    _zmq_client = None
    logger.info("Verification gateway stopped")


def create_app() -> FastAPI:
    """Application factory."""
    app = FastAPI(
        title="TensorCash Verification API",
        description="Open-source HTTP gateway for the TensorCash proof verification engine.",
        version="1.0.0",
        lifespan=_lifespan,
    )

    # Middleware order: rate-limit first, then auth (outermost runs first)
    app.add_middleware(StaticKeyAuthMiddleware)
    app.add_middleware(RateLimitMiddleware)

    # Mount verification routes
    app.include_router(router)

    # Top-level health probes (outside /v1/verify prefix)
    @app.get("/health")
    async def health():
        return {"status": "healthy"}

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz():
        ready = _zmq_client is not None and _zmq_client.connected
        status_code = 200 if ready else 503
        return JSONResponse(
            status_code=status_code,
            content={"ready": ready},
        )

    return app


# Allow ``uvicorn extapi_stub.app:app`` without --factory
app = create_app()
