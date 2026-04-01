# SPDX-License-Identifier: Apache-2.0
"""
FlatBuffers Sidecar Process for Safe Parsing

This module provides a separate process that handles FlatBuffers parsing
in isolation, protecting the main process from potential segfaults caused
by malformed or malicious buffers.

Architecture:
- Main process communicates with sidecar via multiprocessing Queue/Pipe
- Sidecar process can crash without affecting main process
- Automatic restart on crash with backoff
- Timeout protection for hung parsing operations
"""

import multiprocessing as mp
import queue
import time
import logging
import signal
import traceback
import hashlib
from typing import Optional, Dict, Any, Tuple
from dataclasses import dataclass
from enum import Enum
import pickle
import os
import sys

# Add path for FlatBuffers imports
sys.path.append(os.path.join(os.path.dirname(__file__), '../../../../shared-utils/fb-schemas'))

logger = logging.getLogger(__name__)


class ParseOperation(Enum):
    """Types of parsing operations."""
    EXTRACT_IDS = "extract_ids"
    CONVERT_TO_VALIDATION = "convert_to_validation"
    CONVERT_PROOF_TO_VALIDATION = "convert_proof_to_validation"
    PARSE_MINING_RESPONSE = "parse_mining_response"


@dataclass
class ParseRequest:
    """Request to parse FlatBuffers data."""
    request_id: str
    operation: ParseOperation
    data: bytes
    params: Dict[str, Any] = None


@dataclass
class ParseResponse:
    """Response from parsing operation."""
    request_id: str
    success: bool
    result: Any = None
    error: Optional[str] = None


def _setup_worker_signal_handlers():
    """Setup signal handlers for worker process."""
    # Ignore SIGINT in worker (parent will handle shutdown)
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    
    # Set alarm handler for timeout
    def timeout_handler(signum, frame):
        raise TimeoutError("Parsing operation timed out")
    signal.signal(signal.SIGALRM, timeout_handler)


def _parse_with_timeout(request: ParseRequest, timeout: int = 5) -> ParseResponse:
    """
    Parse FlatBuffers with timeout protection.
    
    This function runs in the sidecar process and may crash on malformed input.
    """
    # Set timeout alarm
    signal.alarm(timeout)
    
    try:
        if request.operation == ParseOperation.EXTRACT_IDS:
            # Import here to isolate potential import crashes
            from .builders import extract_ids
            result = extract_ids(request.data)
            
            # Convert bytes to hex for serialization
            result = {
                'hash_id': result['hash_id'].hex(),
                'pow_blob_hash': result['pow_blob_hash'].hex()
            }
            
        elif request.operation == ParseOperation.CONVERT_TO_VALIDATION:
            from .builders import mining_response_to_validation_request
            
            params = request.params or {}
            result = mining_response_to_validation_request(
                request.data,
                params.get('validation_type', 'full'),
                bytes.fromhex(params['prev_block_hash']) if params.get('prev_block_hash') else None,
                bytes.fromhex(params['merkle_root']) if params.get('merkle_root') else None,
                params.get('bits')
            )
            # Convert to hex for serialization
            result = result.hex()

        elif request.operation == ParseOperation.CONVERT_PROOF_TO_VALIDATION:
            from .builders import proof_to_validation_request

            params = request.params or {}
            result = proof_to_validation_request(
                request.data,
                params.get('validation_type', 'full')
            )
            result = result.hex()
            
        elif request.operation == ParseOperation.PARSE_MINING_RESPONSE:
            # Parse MiningResponse to extract model fields
            try:
                from utils.proof import MiningResponse
            except ImportError:
                from proof import MiningResponse
            
            mr = MiningResponse.MiningResponse.GetRootAs(request.data, 0)
            pf = mr.PowBlob() if mr else None
            
            result = {
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
        
        else:
            raise ValueError(f"Unknown operation: {request.operation}")
        
        # Cancel timeout
        signal.alarm(0)
        
        return ParseResponse(
            request_id=request.request_id,
            success=True,
            result=result
        )
        
    except TimeoutError as e:
        signal.alarm(0)
        return ParseResponse(
            request_id=request.request_id,
            success=False,
            error=f"Timeout: {str(e)}"
        )
    except Exception as e:
        signal.alarm(0)
        return ParseResponse(
            request_id=request.request_id,
            success=False,
            error=f"{type(e).__name__}: {str(e)}"
        )


def sidecar_worker(request_queue: mp.Queue, response_queue: mp.Queue, worker_id: int):
    """
    Sidecar worker process that handles FlatBuffers parsing.
    
    This process is designed to crash safely on malformed input.
    """
    _setup_worker_signal_handlers()
    
    logger.info(f"Sidecar worker {worker_id} started (PID: {os.getpid()})")
    
    while True:
        try:
            # Get request with timeout to allow periodic checks
            try:
                request = request_queue.get(timeout=1.0)
            except queue.Empty:
                continue
            
            # Check for shutdown signal
            if request is None:
                logger.info(f"Sidecar worker {worker_id} shutting down")
                break
            
            # Parse with timeout protection
            response = _parse_with_timeout(request)
            
            # Send response
            response_queue.put(response)
            
        except Exception as e:
            # Log unexpected errors but keep running
            logger.error(f"Sidecar worker {worker_id} error: {e}")
            # Send error response if we can identify the request
            if 'request' in locals():
                response_queue.put(ParseResponse(
                    request_id=request.request_id,
                    success=False,
                    error=f"Worker error: {str(e)}"
                ))


class FlatBuffersSidecar:
    """
    Manager for FlatBuffers sidecar processes.
    
    Provides safe parsing of potentially malicious FlatBuffers data
    by isolating parsing in separate processes that can crash safely.
    """
    
    def __init__(self, num_workers: int = 2, max_retries: int = 3):
        """
        Initialize the sidecar manager.
        
        Args:
            num_workers: Number of worker processes
            max_retries: Maximum retries for failed operations
        """
        self.num_workers = num_workers
        self.max_retries = max_retries
        self.request_queue = mp.Queue()
        self.response_queue = mp.Queue()
        self.workers = []
        self.pending_requests = {}
        self.request_counter = 0
        self.started = False
        
        # Statistics
        self.stats = {
            'requests': 0,
            'successes': 0,
            'failures': 0,
            'timeouts': 0,
            'crashes': 0,
            'restarts': 0
        }
    
    def start(self):
        """Start the sidecar workers."""
        if self.started:
            return
        
        logger.info(f"Starting FlatBuffers sidecar with {self.num_workers} workers")
        
        for i in range(self.num_workers):
            self._start_worker(i)
        
        self.started = True
    
    def _start_worker(self, worker_id: int):
        """Start a single worker process."""
        worker = mp.Process(
            target=sidecar_worker,
            args=(self.request_queue, self.response_queue, worker_id),
            daemon=True
        )
        worker.start()
        self.workers.append(worker)
        logger.info(f"Started sidecar worker {worker_id} (PID: {worker.pid})")
    
    def stop(self):
        """Stop all sidecar workers."""
        if not self.started:
            return
        
        logger.info("Stopping FlatBuffers sidecar")
        
        # Send shutdown signal to all workers
        for _ in self.workers:
            self.request_queue.put(None)
        
        # Wait for workers to finish
        for worker in self.workers:
            worker.join(timeout=5.0)
            if worker.is_alive():
                logger.warning(f"Force terminating worker {worker.pid}")
                worker.terminate()
                worker.join(timeout=1.0)
        
        self.workers.clear()
        self.started = False
    
    def _check_workers(self):
        """Check worker health and restart if needed."""
        for i, worker in enumerate(self.workers):
            if not worker.is_alive():
                logger.warning(f"Sidecar worker {i} crashed (exit code: {worker.exitcode})")
                self.stats['crashes'] += 1
                self.stats['restarts'] += 1
                
                # Restart worker
                self._start_worker(i)
                self.workers[i] = self.workers[-1]
                self.workers.pop()
    
    def parse_flatbuffers(
        self,
        operation: ParseOperation,
        data: bytes,
        params: Optional[Dict[str, Any]] = None,
        timeout: float = 10.0
    ) -> Tuple[bool, Any, Optional[str]]:
        """
        Parse FlatBuffers data safely in sidecar process.
        
        Args:
            operation: Type of parsing operation
            data: Raw FlatBuffers data
            params: Optional parameters for the operation
            timeout: Maximum time to wait for response
            
        Returns:
            Tuple of (success, result, error_message)
        """
        if not self.started:
            raise RuntimeError("Sidecar not started")
        
        # Check worker health
        self._check_workers()
        
        # Generate request ID
        self.request_counter += 1
        request_id = f"req_{self.request_counter}_{hashlib.sha256(data[:100]).hexdigest()[:8]}"
        
        # Create request
        request = ParseRequest(
            request_id=request_id,
            operation=operation,
            data=data,
            params=params
        )
        
        self.stats['requests'] += 1
        
        # Send request
        self.request_queue.put(request)
        
        # Wait for response
        start_time = time.time()
        while time.time() - start_time < timeout:
            try:
                # Check response queue
                response = self.response_queue.get(timeout=0.1)
                
                if response.request_id == request_id:
                    if response.success:
                        self.stats['successes'] += 1
                        return True, response.result, None
                    else:
                        self.stats['failures'] += 1
                        return False, None, response.error
                else:
                    # Put back responses for other requests
                    self.response_queue.put(response)
                    
            except queue.Empty:
                continue
        
        # Timeout
        self.stats['timeouts'] += 1
        return False, None, f"Timeout after {timeout}s"
    
    def extract_ids_safe(self, data: bytes) -> Optional[Dict[str, str]]:
        """
        Safely extract IDs from MiningResponse.
        
        Args:
            data: Raw MiningResponse bytes
            
        Returns:
            Dictionary with hash_id and pow_blob_hash as hex strings, or None on error
        """
        success, result, error = self.parse_flatbuffers(
            ParseOperation.EXTRACT_IDS,
            data
        )
        
        if success:
            return result
        else:
            logger.error(f"Failed to extract IDs: {error}")
            return None
    
    def convert_to_validation_safe(
        self,
        data: bytes,
        validation_type: str,
        prev_block_hash: Optional[str] = None,
        merkle_root: Optional[str] = None,
        bits: Optional[int] = None
    ) -> Optional[bytes]:
        """
        Safely convert MiningResponse to ValidationRequest.
        
        Args:
            data: Raw MiningResponse bytes
            validation_type: "full" or "model"
            prev_block_hash: Optional previous block hash (hex string)
            merkle_root: Optional merkle root (hex string)
            bits: Optional bits value
            
        Returns:
            ValidationRequest bytes or None on error
        """
        params = {
            'validation_type': validation_type,
            'prev_block_hash': prev_block_hash,
            'merkle_root': merkle_root,
            'bits': bits
        }
        
        success, result, error = self.parse_flatbuffers(
            ParseOperation.CONVERT_TO_VALIDATION,
            data,
            params
        )
        
        if success:
            # Convert back from hex
            return bytes.fromhex(result)
        else:
            logger.error(f"Failed to convert to validation request: {error}")
            return None

    def convert_proof_to_validation_safe(
        self,
        data: bytes,
        validation_type: str = "full"
    ) -> Optional[bytes]:
        """
        Safely convert Proof to ValidationRequest.
        """
        params = {
            'validation_type': validation_type
        }

        success, result, error = self.parse_flatbuffers(
            ParseOperation.CONVERT_PROOF_TO_VALIDATION,
            data,
            params
        )

        if success:
            return bytes.fromhex(result)
        else:
            logger.error(f"Failed to convert proof to validation request: {error}")
            return None
    
    def parse_mining_response_safe(self, data: bytes) -> Optional[Dict[str, Any]]:
        """
        Safely parse MiningResponse to extract fields.
        
        Args:
            data: Raw MiningResponse bytes
            
        Returns:
            Dictionary of parsed fields or None on error
        """
        success, result, error = self.parse_flatbuffers(
            ParseOperation.PARSE_MINING_RESPONSE,
            data
        )
        
        if success:
            return result
        else:
            logger.error(f"Failed to parse MiningResponse: {error}")
            return None
    
    def get_stats(self) -> Dict[str, int]:
        """Get sidecar statistics."""
        return self.stats.copy()


# Global sidecar instance
_sidecar: Optional[FlatBuffersSidecar] = None


def get_sidecar() -> FlatBuffersSidecar:
    """Get or create the global sidecar instance."""
    global _sidecar
    if _sidecar is None:
        _sidecar = FlatBuffersSidecar()
        _sidecar.start()
    return _sidecar


def stop_sidecar():
    """Stop the global sidecar instance."""
    global _sidecar
    if _sidecar:
        _sidecar.stop()
        _sidecar = None
