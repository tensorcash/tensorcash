# SPDX-License-Identifier: Apache-2.0
"""
ValidationRequest precheck via C++ sidecar binary.

Validates FlatBuffer structure and checks hash/pow bindings:
  - For BlockValidation: recomputes merkle root from proof blob fields,
    computes the block long-hash, and verifies hash_id and pow_blob_hash
    match the computed values.
  - For ModelValidation: always passes.

The C++ binary (vr_precheck) must be built from sidecar/vr_precheck.cpp
and placed at $VR_PRECHECK_BIN (default: /app/bin/vr_precheck).

Set VERIFY_PRECHECK_ENABLED=false to skip (dev/test only).

Configuration:
    VR_PRECHECK_BIN             — path to binary (default: /app/bin/vr_precheck)
    VR_PRECHECK_TIMEOUT_SEC     — subprocess timeout (default: 2.0)
    VERIFY_PRECHECK_ENABLED     — enable/disable (default: true)
"""

import json
import logging
import os
import subprocess
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_BIN_PATH = os.environ.get("VR_PRECHECK_BIN", "/app/bin/vr_precheck")
_TIMEOUT_SEC = float(os.environ.get("VR_PRECHECK_TIMEOUT_SEC", "2.0"))
_ENABLED = os.environ.get("VERIFY_PRECHECK_ENABLED", "true").lower() in ("1", "true", "yes")

_bin_exists: Optional[bool] = None


def _check_bin() -> bool:
    global _bin_exists
    if _bin_exists is None:
        _bin_exists = os.path.isfile(_BIN_PATH) and os.access(_BIN_PATH, os.X_OK)
        if not _bin_exists:
            logger.warning(
                "VR precheck binary not found at %s — precheck will reject all block requests. "
                "Set VERIFY_PRECHECK_ENABLED=false to skip in dev.",
                _BIN_PATH,
            )
    return _bin_exists


def precheck_validation_request(payload: bytes) -> Optional[Dict[str, Any]]:
    """
    Run the C++ sidecar precheck on ValidationRequest bytes.

    Returns:
        dict with ok, hash_match, pow_match, etc. on success
        None on binary-not-found, timeout, or crash
    """
    if not _ENABLED:
        return {"ok": True, "hash_match": True, "pow_match": True, "skipped": True}

    if not payload:
        return None

    if not _check_bin():
        return None

    try:
        res = subprocess.run(
            [_BIN_PATH],
            input=payload,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=_TIMEOUT_SEC,
            check=False,
        )
    except subprocess.TimeoutExpired:
        logger.error("VR precheck timed out")
        return None
    except Exception as e:
        logger.error("VR precheck failed: %s", e)
        return None

    if res.returncode != 0:
        logger.error(
            "VR precheck error: rc=%s stderr=%s",
            res.returncode,
            res.stderr.decode("utf-8", errors="ignore"),
        )
        return None

    try:
        return json.loads(res.stdout.decode("utf-8"))
    except Exception as e:
        logger.error("VR precheck JSON parse failed: %s", e)
        return None
