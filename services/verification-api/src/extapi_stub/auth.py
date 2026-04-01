# SPDX-License-Identifier: Apache-2.0
"""
Static API-key authentication for the external verification API.

Keys are loaded from the VERIFY_API_KEYS environment variable as a
comma-separated list.  Each incoming request must carry a valid key
in the ``Authorization: Bearer <key>`` header.

When VERIFY_AUTH_ENABLED is false (the default for local dev), all
requests are allowed through.
"""

import os
import logging
from typing import Optional

from fastapi import Request, HTTPException
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response

logger = logging.getLogger(__name__)

# Paths that never require auth
_PUBLIC_PATHS = frozenset({
    "/health",
    "/healthz",
    "/readyz",
    "/v1/verify/health",
})

# Path prefixes that never require auth
_PUBLIC_PREFIXES = (
    "/v1/public/",
)


def _load_keys() -> frozenset:
    raw = os.environ.get("VERIFY_API_KEYS", "")
    keys = frozenset(k.strip() for k in raw.split(",") if k.strip())
    if keys:
        logger.info("Loaded %d static API key(s)", len(keys))
    return keys


_VALID_KEYS = _load_keys()
_AUTH_ENABLED = os.environ.get("VERIFY_AUTH_ENABLED", "false").lower() in ("1", "true", "yes")


def reload_keys():
    """Hot-reload keys (e.g. after config change)."""
    global _VALID_KEYS, _AUTH_ENABLED
    _VALID_KEYS = _load_keys()
    _AUTH_ENABLED = os.environ.get("VERIFY_AUTH_ENABLED", "false").lower() in ("1", "true", "yes")


class StaticKeyAuthMiddleware(BaseHTTPMiddleware):
    """Reject requests that lack a valid ``Authorization: Bearer <key>``."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if not _AUTH_ENABLED:
            return await call_next(request)

        path = request.url.path
        if path in _PUBLIC_PATHS:
            return await call_next(request)
        if any(path.startswith(p) for p in _PUBLIC_PREFIXES):
            return await call_next(request)

        auth: Optional[str] = request.headers.get("authorization")
        if not auth or not auth.lower().startswith("bearer "):
            raise HTTPException(status_code=401, detail="Missing or malformed Authorization header")

        token = auth[7:]  # strip "Bearer "
        if token not in _VALID_KEYS:
            raise HTTPException(status_code=403, detail="Invalid API key")

        return await call_next(request)
