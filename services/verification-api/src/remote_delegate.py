# SPDX-License-Identifier: Apache-2.0
"""
Remote delegation helpers for trusting an external /v1/verify attestor.

Posts ValidationRequest bytes to the remote gateway endpoints and maps
the returned status to local ResponseValue enums.

Controlled by env in main.py:
- REMOTE_VERIFY_ENABLED (bool)
- REMOTE_VERIFY_BASE_URL (str)
- REMOTE_VERIFY_API_KEY (str, optional)
- REMOTE_VERIFY_TIMEOUT_SECONDS (float)
"""

import json
from urllib import request as _req
from urllib.error import URLError, HTTPError
from typing import Optional

from utils.proof import ResponseValue


def _post_bytes(url: str, data: bytes, api_key: Optional[str], timeout: float) -> dict:
    req = _req.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/octet-stream")
    req.add_header("Accept", "application/json")
    if api_key:
        req.add_header("Authorization", f"Bearer {api_key}")
    try:
        with _req.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            return json.loads(body.decode("utf-8"))
    except HTTPError as e:
        # Try to read error payload for diagnostics
        try:
            payload = e.read().decode("utf-8")
        except Exception:
            payload = str(e)
        raise RuntimeError(f"HTTP {e.code}: {payload}")
    except URLError as e:
        raise RuntimeError(f"Network error: {e}")


def _map_status_to_enum(status: str) -> int:
    s = (status or "").strip()
    mapping = {
        "Full_Green": ResponseValue.ResponseValue.Full_Green,
        "Full_Amber": ResponseValue.ResponseValue.Full_Amber,
        "Full_Red": ResponseValue.ResponseValue.Full_Red,
        "Model_OK": ResponseValue.ResponseValue.Model_OK,
        "Model_Fail": ResponseValue.ResponseValue.Model_Fail,
        "Challenge_OK": ResponseValue.ResponseValue.Challenge_OK,
        "Challenge_Fail": ResponseValue.ResponseValue.Challenge_Fail,
        # Non-terminal: remote gateway returned operator review pending
        "Model_Pending_Review": ResponseValue.ResponseValue.Model_Pending_Review,
        "pending_operator_review": ResponseValue.ResponseValue.Model_Pending_Review,
        "pending": ResponseValue.ResponseValue.Model_Pending_Review,
        # Accept quick codes defensively (shouldn't be returned here)
        "Quick_OK": ResponseValue.ResponseValue.Quick_OK,
        "Quick_Fail": ResponseValue.ResponseValue.Quick_Fail,
        "Quick_OK_Smell_OK": ResponseValue.ResponseValue.Quick_OK_Smell_OK,
        "Quick_OK_Smell_Fail": ResponseValue.ResponseValue.Quick_OK_Smell_Fail,
        "Quick_Fail_Smell_Fail": ResponseValue.ResponseValue.Quick_Fail_Smell_Fail,
    }
    if s in mapping:
        return mapping[s]
    raise RuntimeError(f"Unknown status from remote attestor: {s}")


def verify_full_remote(vreq_bytes: bytes, base_url: str, api_key: Optional[str], timeout: float) -> int:
    url = base_url.rstrip("/") + "/v1/verify/full/request"
    payload = _post_bytes(url, vreq_bytes, api_key, timeout)
    status = payload.get("status")
    return _map_status_to_enum(status)


def verify_model_remote(vreq_bytes: bytes, base_url: str, api_key: Optional[str], timeout: float) -> int:
    url = base_url.rstrip("/") + "/v1/verify/model/request"
    payload = _post_bytes(url, vreq_bytes, api_key, timeout)
    status = payload.get("status")
    return _map_status_to_enum(status)


def verify_challenge_remote(vreq_bytes: bytes, base_url: str, api_key: Optional[str], timeout: float) -> int:
    url = base_url.rstrip("/") + "/v1/verify/challenge/request"
    payload = _post_bytes(url, vreq_bytes, api_key, timeout)
    status = payload.get("status")
    return _map_status_to_enum(status)
