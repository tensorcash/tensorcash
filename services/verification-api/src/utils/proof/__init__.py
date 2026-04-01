# SPDX-License-Identifier: Apache-2.0
"""
Shim that exposes FlatBuffers-generated proof Python modules in a stable way.

Design goals:
- Never rely on absolute paths; use whatever "proof" package is present.
- Work with both styles of generation:
  * Package with submodules (proof/ValidationRequest.py, etc.)
  * Single module that re-exports classes via proof/__init__.py
- Avoid import-time failures if some submodules (e.g., Proof.py) are not present.
"""

from __future__ import annotations

import importlib
import sys
from types import ModuleType


def _maybe_module(name: str) -> ModuleType | None:
    """Try to import proof.<name> as a module; fall back to attr from proof.

    Returns a ModuleType exposing the expected class inside (e.g.,
    module.ValidationRequest) even if proof only provides a top-level class.
    """
    # First try submodule import (preferred)
    try:
        return importlib.import_module(f"proof.{name}")
    except Exception:
        pass

    # Fallback: try top-level package attr
    try:
        pkg = importlib.import_module("proof")
    except Exception:
        return None

    if hasattr(pkg, name):
        cls = getattr(pkg, name)
        # Create a lightweight module wrapper exposing the class by same name
        mod = ModuleType(f"proof.{name}")
        setattr(mod, name, cls)
        return mod

    return None


# Only import what verification-api uses by default. Optional ones are attempted lazily.
BlockValidation = _maybe_module("BlockValidation")
ModelValidation = _maybe_module("ModelValidation")
ValidationRequest = _maybe_module("ValidationRequest")
ValidationResponse = _maybe_module("ValidationResponse")
ValidationType = _maybe_module("ValidationType")
ValidationUnion = _maybe_module("ValidationUnion")
ResponseValue = _maybe_module("ResponseValue")

# Optional: present in some environments; ignore if missing
MiningResponse = _maybe_module("MiningResponse")
BlockHeader = _maybe_module("BlockHeader")
Proof = _maybe_module("Proof")
FloatArray = _maybe_module("FloatArray")
UIntArray = _maybe_module("UIntArray")

__all__ = [
    name for name, val in globals().items()
    if name in {
        "Proof",
        "FloatArray",
        "UIntArray",
        "MiningResponse",
        "BlockHeader",
        "BlockValidation",
        "ModelValidation",
        "ValidationRequest",
        "ValidationResponse",
        "ValidationType",
        "ValidationUnion",
        "ResponseValue",
    } and val is not None
]
