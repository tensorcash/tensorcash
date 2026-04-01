# SPDX-License-Identifier: Apache-2.0
"""
TensorCash Verification SDK — Python client for the verification API.

Supports both sync and async usage::

    # Async
    async with TensorCashVerifier("http://verify.example.com", api_key="k") as v:
        result = await v.verify_full(proof_bytes)
        print(result.status, result.hash_id)

    # Sync (convenience wrapper)
    v = TensorCashVerifierSync("http://verify.example.com", api_key="k")
    result = v.verify_full(proof_bytes)
"""

from __future__ import annotations

import asyncio
import base64
import logging
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any

try:
    import aiohttp
except ImportError:
    aiohttp = None  # type: ignore[assignment]

try:
    import httpx
except ImportError:
    httpx = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------ #
# Result types
# ------------------------------------------------------------------ #

@dataclass
class BatchStatusResult:
    """Result from batch status polling."""
    completed: List["VerificationResult"] = field(default_factory=list)
    still_pending: List[Dict[str, Any]] = field(default_factory=list)
    waited_ms: int = 0
    server_ts: float = 0.0
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass
class VerificationResult:
    """Unified result from any verification endpoint."""
    status: str
    hash_id: str
    elapsed_ms: int
    cached: bool = False
    pow_blob_hash: Optional[str] = None
    model_identifier: Optional[str] = None
    cid: Optional[str] = None
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status in ("Full_Green", "Full_Amber", "Model_OK",
                               "Quick_OK", "Quick_OK_Smell_OK")

    @property
    def green(self) -> bool:
        return self.status == "Full_Green"


# ------------------------------------------------------------------ #
# Async client
# ------------------------------------------------------------------ #

class TensorCashVerifier:
    """
    Async HTTP client for the TensorCash verification API.

    Requires either ``aiohttp`` or ``httpx`` to be installed.
    """

    def __init__(
        self,
        base_url: str,
        api_key: Optional[str] = None,
        timeout: float = 120.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self._session: Optional[Any] = None
        self._owns_session = False

    # -- context manager --

    async def __aenter__(self):
        await self._ensure_session()
        return self

    async def __aexit__(self, *exc):
        await self.close()

    async def close(self):
        if self._session and self._owns_session:
            if aiohttp and isinstance(self._session, aiohttp.ClientSession):
                await self._session.close()
            elif httpx and isinstance(self._session, httpx.AsyncClient):
                await self._session.aclose()
        self._session = None

    # -- internals --

    def _headers(self) -> dict:
        h: dict = {"Accept": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    async def _ensure_session(self):
        if self._session is not None:
            return
        if aiohttp:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.timeout)
            )
        elif httpx:
            self._session = httpx.AsyncClient(timeout=self.timeout)
        else:
            raise ImportError(
                "Install either 'aiohttp' or 'httpx' to use the TensorCash SDK"
            )
        self._owns_session = True

    async def _post_binary(self, path: str, data: bytes) -> dict:
        await self._ensure_session()
        url = f"{self.base_url}{path}"
        headers = {**self._headers(), "Content-Type": "application/octet-stream"}

        if aiohttp and isinstance(self._session, aiohttp.ClientSession):
            async with self._session.post(url, data=data, headers=headers) as resp:
                body = await resp.json()
                if resp.status >= 400:
                    raise VerificationAPIError(resp.status, body)
                return body
        elif httpx and isinstance(self._session, httpx.AsyncClient):
            resp = await self._session.post(url, content=data, headers=headers)
            body = resp.json()
            if resp.status_code >= 400:
                raise VerificationAPIError(resp.status_code, body)
            return body
        raise RuntimeError("No HTTP session available")

    async def _post_json(self, path: str, payload: dict) -> dict:
        await self._ensure_session()
        url = f"{self.base_url}{path}"
        headers = {**self._headers(), "Content-Type": "application/json"}

        if aiohttp and isinstance(self._session, aiohttp.ClientSession):
            async with self._session.post(url, json=payload, headers=headers) as resp:
                body = await resp.json()
                if resp.status >= 400:
                    raise VerificationAPIError(resp.status, body)
                return body
        elif httpx and isinstance(self._session, httpx.AsyncClient):
            resp = await self._session.post(url, json=payload, headers=headers)
            body = resp.json()
            if resp.status_code >= 400:
                raise VerificationAPIError(resp.status_code, body)
            return body
        raise RuntimeError("No HTTP session available")

    async def _get(self, path: str) -> dict:
        await self._ensure_session()
        url = f"{self.base_url}{path}"
        headers = self._headers()

        if aiohttp and isinstance(self._session, aiohttp.ClientSession):
            async with self._session.get(url, headers=headers) as resp:
                body = await resp.json()
                if resp.status >= 400:
                    raise VerificationAPIError(resp.status, body)
                return body
        elif httpx and isinstance(self._session, httpx.AsyncClient):
            resp = await self._session.get(url, headers=headers)
            body = resp.json()
            if resp.status_code >= 400:
                raise VerificationAPIError(resp.status_code, body)
            return body
        raise RuntimeError("No HTTP session available")

    @staticmethod
    def _to_result(data: dict) -> VerificationResult:
        return VerificationResult(
            status=data.get("status", ""),
            hash_id=data.get("hash_id", ""),
            elapsed_ms=data.get("elapsed_ms", 0),
            cached=data.get("cached", False),
            pow_blob_hash=data.get("pow_blob_hash"),
            model_identifier=data.get("model_identifier"),
            cid=data.get("cid"),
            raw=data,
        )

    # -- public API --

    async def verify_full(self, proof: bytes) -> VerificationResult:
        """Submit a binary MiningResponse for full verification."""
        data = await self._post_binary("/v1/verify/full", proof)
        return self._to_result(data)

    async def verify_full_b64(self, proof_b64: str) -> VerificationResult:
        """Submit a base64-encoded MiningResponse for full verification."""
        data = await self._post_json("/v1/verify/full/json", {"proof_b64": proof_b64})
        return self._to_result(data)

    async def verify_model(self, proof: bytes) -> VerificationResult:
        """Submit a binary MiningResponse for model verification."""
        data = await self._post_binary("/v1/verify/model", proof)
        return self._to_result(data)

    async def verify_model_b64(self, proof_b64: str) -> VerificationResult:
        """Submit a base64-encoded MiningResponse for model verification."""
        data = await self._post_json("/v1/verify/model/json", {"proof_b64": proof_b64})
        return self._to_result(data)

    async def verify_pow(self, proof: bytes) -> VerificationResult:
        """Pow-blob-only verification. Accepts MiningResponse, Proof, or ValidationRequest."""
        data = await self._post_binary("/v1/verify/pow", proof)
        return self._to_result(data)

    async def verify_quick(self, proof: bytes) -> VerificationResult:
        """Quick verification (fast cryptographic checks only)."""
        data = await self._post_binary("/v1/verify/quick", proof)
        return self._to_result(data)

    async def verify_quick_smell(self, proof: bytes) -> VerificationResult:
        """Quick + smell test verification."""
        data = await self._post_binary("/v1/verify/quick-smell", proof)
        return self._to_result(data)

    async def verify_logits(self, proof: bytes) -> VerificationResult:
        """Logits-only verification."""
        data = await self._post_binary("/v1/verify/logits", proof)
        return self._to_result(data)

    async def submit_full(self, validation_request: bytes) -> dict:
        """Async submit a ValidationRequest for full verification. Returns immediately."""
        data = await self._post_binary("/v1/verify/full/request/submit", validation_request)
        return data

    async def submit_model(self, validation_request: bytes) -> dict:
        """Async submit a ValidationRequest for model verification."""
        data = await self._post_binary("/v1/verify/model/request/submit", validation_request)
        return data

    async def get_status(self, hash_id: str, verification_type: Optional[str] = None) -> Optional[VerificationResult]:
        """
        Poll for a previously submitted verification result.

        Returns None on 404, returns VerificationResult with status="pending"
        on 202, or the completed result on 200.
        """
        path = f"/v1/verify/status/{hash_id}"
        if verification_type:
            vt = verification_type.replace("_", "-")
            path = f"/v1/verify/status/{vt}/{hash_id}"
        try:
            data = await self._get(path)
            return self._to_result(data)
        except VerificationAPIError as e:
            if e.status_code == 404:
                return None
            if e.status_code == 202:
                return VerificationResult(
                    status="pending",
                    hash_id=hash_id,
                    elapsed_ms=0,
                    raw=e.body if isinstance(e.body, dict) else {},
                )
            raise

    async def get_public_status(self, hash_id: str, verification_type: Optional[str] = None) -> VerificationResult:
        """Public unauthenticated status lookup. Returns NAN if not found."""
        params = f"?verification_type={verification_type}" if verification_type else ""
        data = await self._get(f"/v1/public/status/{hash_id}{params}")
        return self._to_result(data)

    async def get_status_batch(
        self,
        items: List[Dict[str, str]],
        wait_ms: int = 0,
    ) -> "BatchStatusResult":
        """
        Poll multiple (hash_id, verification_type) pairs in a single request.

        Args:
            items: List of {"hash_id": "...", "verification_type": "full|model|..."}
            wait_ms: Server-side wait for first completion (0-10000)

        Returns:
            BatchStatusResult with completed, still_pending, waited_ms, server_ts
        """
        payload = {"items": items, "wait_ms": wait_ms}
        data = await self._post_json("/v1/verify/status/batch", payload)
        return BatchStatusResult(
            completed=[self._to_result(c) for c in data.get("completed", [])],
            still_pending=data.get("still_pending", []),
            waited_ms=data.get("waited_ms", 0),
            server_ts=data.get("server_ts", 0.0),
            raw=data,
        )

    async def health(self) -> dict:
        """Check service health."""
        return await self._get("/v1/verify/health")


# ------------------------------------------------------------------ #
# Sync wrapper
# ------------------------------------------------------------------ #

class TensorCashVerifierSync:
    """
    Synchronous wrapper around TensorCashVerifier.

    Uses an internal event loop. Not suitable for use inside an
    already-running asyncio loop — use the async client directly.
    """

    def __init__(self, base_url: str, api_key: Optional[str] = None, timeout: float = 120.0):
        self._async = TensorCashVerifier(base_url, api_key=api_key, timeout=timeout)
        self._loop = asyncio.new_event_loop()

    def close(self):
        self._loop.run_until_complete(self._async.close())
        self._loop.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def verify_full(self, proof: bytes) -> VerificationResult:
        return self._loop.run_until_complete(self._async.verify_full(proof))

    def verify_full_b64(self, proof_b64: str) -> VerificationResult:
        return self._loop.run_until_complete(self._async.verify_full_b64(proof_b64))

    def verify_model(self, proof: bytes) -> VerificationResult:
        return self._loop.run_until_complete(self._async.verify_model(proof))

    def verify_model_b64(self, proof_b64: str) -> VerificationResult:
        return self._loop.run_until_complete(self._async.verify_model_b64(proof_b64))

    def verify_pow(self, proof: bytes) -> VerificationResult:
        return self._loop.run_until_complete(self._async.verify_pow(proof))

    def verify_quick(self, proof: bytes) -> VerificationResult:
        return self._loop.run_until_complete(self._async.verify_quick(proof))

    def verify_quick_smell(self, proof: bytes) -> VerificationResult:
        return self._loop.run_until_complete(self._async.verify_quick_smell(proof))

    def verify_logits(self, proof: bytes) -> VerificationResult:
        return self._loop.run_until_complete(self._async.verify_logits(proof))

    def submit_full(self, validation_request: bytes) -> dict:
        return self._loop.run_until_complete(self._async.submit_full(validation_request))

    def submit_model(self, validation_request: bytes) -> dict:
        return self._loop.run_until_complete(self._async.submit_model(validation_request))

    def get_status(self, hash_id: str, verification_type: Optional[str] = None) -> Optional[VerificationResult]:
        return self._loop.run_until_complete(self._async.get_status(hash_id, verification_type))

    def get_public_status(self, hash_id: str, verification_type: Optional[str] = None) -> VerificationResult:
        return self._loop.run_until_complete(self._async.get_public_status(hash_id, verification_type))

    def get_status_batch(self, items: List[Dict[str, str]], wait_ms: int = 0) -> BatchStatusResult:
        return self._loop.run_until_complete(self._async.get_status_batch(items, wait_ms))

    def health(self) -> dict:
        return self._loop.run_until_complete(self._async.health())


# ------------------------------------------------------------------ #
# Exceptions
# ------------------------------------------------------------------ #

class VerificationAPIError(Exception):
    """Raised when the verification API returns an error status."""

    def __init__(self, status_code: int, body: Any = None):
        self.status_code = status_code
        self.body = body
        detail = ""
        if isinstance(body, dict):
            detail = body.get("detail", str(body))
        super().__init__(f"HTTP {status_code}: {detail}")
