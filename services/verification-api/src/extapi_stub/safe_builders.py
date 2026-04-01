# SPDX-License-Identifier: Apache-2.0
"""
Safe FlatBuffers builders with sidecar support.

This module provides safe wrappers around the FlatBuffers builders
that can use either direct parsing or sidecar process isolation.
"""

import os
import logging
from typing import Dict, Optional, Tuple
from .builders import (
    extract_ids as direct_extract_ids,
    mining_response_to_validation_request as direct_convert,
    proof_to_validation_request as direct_proof_convert,
    response_value_to_str
)

logger = logging.getLogger(__name__)

# Check if sidecar is available and should be used
# Try to read from settings first, then env, default to True for safety
try:
    from config import settings
    USE_SIDECAR = settings.FLATBUFFERS_USE_SIDECAR
except ImportError:
    USE_SIDECAR = os.environ.get('FLATBUFFERS_USE_SIDECAR', 'true').lower() == 'true'
_sidecar = None

if USE_SIDECAR:
    try:
        from .sidecar import get_sidecar, stop_sidecar
        _sidecar = get_sidecar()
        logger.info("FlatBuffers sidecar mode enabled")
    except ImportError:
        logger.warning("Sidecar requested but not available, using direct parsing")
        USE_SIDECAR = False


def safe_extract_ids(buf: bytes) -> Dict[str, bytes]:
    """
    Safely extract IDs from MiningResponse.
    
    Uses sidecar process if configured, otherwise direct parsing.
    
    Args:
        buf: Raw bytes of a FlatBuffers MiningResponse
        
    Returns:
        Dictionary with 'hash_id' and 'pow_blob_hash' keys
        
    Raises:
        ValueError: If extraction fails
    """
    if USE_SIDECAR and _sidecar:
        # Use sidecar for safe parsing
        result = _sidecar.extract_ids_safe(buf)
        if result:
            # Convert hex strings back to bytes
            return {
                'hash_id': bytes.fromhex(result['hash_id']),
                'pow_blob_hash': bytes.fromhex(result['pow_blob_hash'])
            }
        else:
            # Fallback to SHA256 for hash_id on parse failure
            import hashlib
            logger.warning("Sidecar parsing failed, using SHA256 fallback")
            return {
                'hash_id': hashlib.sha256(buf).digest(),
                'pow_blob_hash': b''
            }
    else:
        # Use direct parsing
        return direct_extract_ids(buf)


def safe_mining_response_to_validation_request(
    buf: bytes,
    validation_type: str,
    prev_block_hash: Optional[bytes] = None,
    merkle_root: Optional[bytes] = None,
    bits: Optional[int] = None
) -> bytes:
    """
    Safely convert MiningResponse to ValidationRequest.
    
    Uses sidecar process if configured, otherwise direct parsing.
    
    Args:
        buf: Raw bytes of a FlatBuffers MiningResponse
        validation_type: "full" or "model"
        prev_block_hash: Optional previous block hash (32 bytes)
        merkle_root: Optional merkle root (32 bytes)
        bits: Optional bits value
        
    Returns:
        Raw bytes of a FlatBuffers ValidationRequest
        
    Raises:
        ValueError: If conversion fails
    """
    if USE_SIDECAR and _sidecar:
        # Convert bytes to hex for sidecar
        result = _sidecar.convert_to_validation_safe(
            buf,
            validation_type,
            prev_block_hash.hex() if prev_block_hash else None,
            merkle_root.hex() if merkle_root else None,
            bits
        )
        
        if result:
            return result
        else:
            raise ValueError("Sidecar failed to convert MiningResponse to ValidationRequest")
    else:
        # Use direct parsing
        return direct_convert(buf, validation_type, prev_block_hash, merkle_root, bits)


def safe_proof_to_validation_request(buf: bytes, validation_type: str = "full") -> bytes:
    """
    Safely convert Proof (pow blob) to ValidationRequest.

    Uses sidecar process if configured, otherwise direct parsing.
    """
    if USE_SIDECAR and _sidecar:
        result = _sidecar.convert_proof_to_validation_safe(buf, validation_type)
        if result:
            return result
        raise ValueError("Sidecar failed to convert Proof to ValidationRequest")
    return direct_proof_convert(buf, validation_type)


def safe_parse_mining_response(buf: bytes) -> Optional[Dict[str, any]]:
    """
    Safely parse MiningResponse to extract fields.
    
    Uses sidecar process if configured, otherwise direct parsing.
    
    Args:
        buf: Raw bytes of a FlatBuffers MiningResponse
        
    Returns:
        Dictionary of parsed fields or None on error
    """
    if USE_SIDECAR and _sidecar:
        return _sidecar.parse_mining_response_safe(buf)
    else:
        # Direct parsing
        try:
            # Import FlatBuffers modules
            try:
                from utils.proof import MiningResponse
            except ImportError:
                import sys
                import os
                sys.path.append(os.path.join(os.path.dirname(__file__), '../../../shared-utils/fb-schemas'))
                from proof import MiningResponse
            
            mr = MiningResponse.MiningResponse.GetRootAs(buf, 0)
            pf = mr.PowBlob() if mr else None
            
            return {
                'has_proof': pf is not None,
                'model_identifier': pf.ModelIdentifier() if pf else None,
                'ipfs_cid': pf.IpfsCid() if pf else None,
                'version': pf.Version() if pf else 0,
                'timestamp': pf.Timestamp() if pf else 0,
                'nonce': mr.Nonce() if mr else 0,
                'difficulty': mr.Difficulty() if mr else 0,
                # CompletionId doesn't exist in MiningResponse schema
                # 'completion_id': mr.CompletionId() if mr else None,
            }
        except Exception as e:
            logger.error(f"Failed to parse MiningResponse: {e}")
            return None


def cleanup_sidecar():
    """Stop the sidecar if it's running."""
    global _sidecar
    if USE_SIDECAR and _sidecar:
        try:
            from .sidecar import stop_sidecar
            stop_sidecar()
            _sidecar = None
            logger.info("FlatBuffers sidecar stopped")
        except Exception as e:
            logger.error(f"Error stopping sidecar: {e}")


# Re-export response_value_to_str (doesn't need safety wrapper)
__all__ = [
    'safe_extract_ids',
    'safe_mining_response_to_validation_request', 
    'safe_parse_mining_response',
    'response_value_to_str',
    'cleanup_sidecar'
]
