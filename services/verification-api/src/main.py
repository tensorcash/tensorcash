# SPDX-License-Identifier: Apache-2.0
import ipaddress
import json
import os
import zmq
import hashlib
import struct
import flatbuffers
import threading
import time
import queue
import logging
import math
import traceback
import numpy as np
import itertools
import torch
from collections import defaultdict
from typing import Dict, Optional, Tuple, Set
from utils.functional import *
from utils.proof import (
    BlockValidation, 
    ModelValidation,
    ValidationRequest,
    ValidationResponse,
    ValidationType,
    ValidationUnion,
    ResponseValue
)
import config.constants as constants
from model_verifier import ModelVerifier

# In test mode, avoid importing heavy proof_verifier and provide a lightweight stub
_TEST_MODE = os.getenv("TEST_MODE", "").lower() in {"1", "true", "yes"}
if _TEST_MODE:
    try:
        # Expose a stub ProofVerifier via the proof_verifier module name so tests that
        # import proof_verifier directly can still monkeypatch it.
        import types
        from utils.proof import ResponseValue as _RV

        class _StubProofVerifier:
            # **_kwargs accepts slice-11 target_override_hex forwarded by
            # AsyncValidator.validate_quick and validate_quick_smell. The
            # stub is used in test/CI environments that don't have the
            # full proof_verifier module (and its CUDA/PyTorch deps).
            def quick_verify(self, _buf, **_kwargs):
                return _RV.ResponseValue.Quick_OK

            def quick_verify_smell_test(self, _buf, **_kwargs):
                return _RV.ResponseValue.Quick_OK_Smell_OK

            def full_verify(self, _buf, **_kwargs):
                return "GREEN"

        # If a stub module hasn't been provided by tests, register one.
        if "proof_verifier" not in globals():
            import sys as _sys
            if "proof_verifier" not in _sys.modules:
                _mod = types.ModuleType("proof_verifier")
                _mod.ProofVerifier = _StubProofVerifier
                # no-op API used elsewhere
                _mod.mca_install = lambda *a, **k: None
                _mod.mca_set_enabled = lambda *a, **k: None
                _mod.mca_set_params = lambda *a, **k: None
                _sys.modules["proof_verifier"] = _mod
        from proof_verifier import ProofVerifier  # type: ignore
        from proof_verifier import mca_install, mca_set_enabled, mca_set_params  # type: ignore
    except Exception:
        # Fallback to local stubs if anything goes wrong
        from utils.proof import ResponseValue as _RV

        class ProofVerifier:  # type: ignore
            # **_kwargs accepts slice-11 target_override_hex forwarded by
            # AsyncValidator.validate_quick and validate_quick_smell —
            # see the equivalent _StubProofVerifier above.
            def quick_verify(self, _buf, **_kwargs):
                return _RV.ResponseValue.Quick_OK

            def quick_verify_smell_test(self, _buf, **_kwargs):
                return _RV.ResponseValue.Quick_OK_Smell_OK

            def full_verify(self, _buf, **_kwargs):
                return "GREEN"

        def mca_install(*_a, **_k):
            pass

        def mca_set_enabled(*_a, **_k):
            pass

        def mca_set_params(*_a, **_k):
            pass
else:
    # Production import of the heavy verifier (original behavior)
    from proof_verifier import ProofVerifier
    from proof_verifier import mca_install, mca_set_enabled, mca_set_params
from zmq_send_broker import ZmqSendBroker
from typing import Any

# Optional remote delegation (HTTP attestor)
REMOTE_VERIFY_ENABLED = os.getenv("REMOTE_VERIFY_ENABLED", "false").lower() in ("1", "true", "yes")
REMOTE_VERIFY_BASE_URL = os.getenv("REMOTE_VERIFY_BASE_URL")
REMOTE_VERIFY_API_KEY = os.getenv("REMOTE_VERIFY_API_KEY")
try:
    REMOTE_VERIFY_TIMEOUT = float(os.getenv("REMOTE_VERIFY_TIMEOUT_SECONDS", "60"))
except Exception:
    REMOTE_VERIFY_TIMEOUT = 60.0

try:
    from . import remote_delegate  # local module we add for HTTP delegation
except Exception:
    remote_delegate = None


class AsyncValidator:
    def __init__(self, pull_port=None, push_host=None, push_port=None):
        """
        Initialize the async validator with PULL/PUSH architecture
        
        Args:
            pull_port: Port for receiving validation requests (PULL socket)
            push_host: Host for sending validation responses (PUSH socket)
            push_port: Port for sending validation responses (PUSH socket)
        """
        # Setup logging
        self.logger = logging.getLogger(__name__)
        
        # Use provided ports or fall back to constants
        pull_port = pull_port or constants.ZMQ_VERIFY_PULL_PORT
        push_host = push_host or constants.ZMQ_VERIFY_PUSH_HOST
        push_port = push_port or constants.ZMQ_VERIFY_PUSH_PORT
        
        self.logger.info(
            f"Async validator initializing on port {pull_port}, "
            f"pushing results to {push_host} port {push_port}"
        )
        
        self.context = zmq.Context()
        
        # PULL socket for receiving requests
        self.pull_socket = self.context.socket(zmq.PULL)
        self.pull_socket.bind(f"tcp://*:{pull_port}")
        
        # OUTBOUND: use send broker (single owner thread for the network PUSH)
        endpoint = f"tcp://{push_host}:{push_port}"
        self.logger.info(f"Outbound sender endpoint: {endpoint}")
        self.sender = ZmqSendBroker(
            endpoint=endpoint,
            hwm=2000,
            max_queue=10000,
            drop_on_backpressure=True,
            io_threads=1,
        )

        self.logger.info(
            f"Async validator listening on port {pull_port}, "
            f"pushing results to {push_host} port {push_port}"
        )
        
        # Validation queues
        self.quick_queue = queue.PriorityQueue()
        self.quick_smell_queue = queue.PriorityQueue()  # Queue for Quick_Smell validation
        self.full_queue = queue.PriorityQueue()
        self.model_queue = queue.PriorityQueue()
        self.challenge_queue = queue.PriorityQueue()
        # Audit (logits-only) verification — no block sanity, audit
        # parameter envelope instead of the mining one.
        self.logits_queue = queue.PriorityQueue()
        # Monotonic counter to break priority ties in PriorityQueue
        self._pq_counter = itertools.count()
        
        # Deduplication trackers: track enqueued and processing hashes per phase
        # Phases: 'quick', 'smell', 'full', 'model'
        self._enqueued = {
            'quick': set(),
            'smell': set(),
            'full': set(),
            'model': set(),
            'challenge': set(),
            'logits': set(),
        }
        self._processing = {
            'quick': set(),
            'smell': set(),
            'full': set(),
            'model': set(),
            'challenge': set(),
            'logits': set(),
        }
        self._queue_lock = threading.RLock()
        
        # Track which hashes have an explicit Full request from the node
        self.full_requested: Set[bytes] = set()
        self.full_req_lock = threading.RLock()
        
        # Track validation status by hash_id with phase-aware structure
        # Each hash can have: 'quick', 'smell', 'full' phases
        # Note: Quick_Smell validation sets BOTH 'quick' and 'smell' phases since it's a superset
        self.validation_status = {}  # hash_id -> {'quick': result, 'smell': result, 'full': result, 'timestamp': ..., 'prev_hash': ...}
        self.status_lock = threading.RLock()  # Use RLock for recursive access
        
        # Track retry counts for re-enqueuing logic
        self.retry_counts = defaultdict(int)  # hash_id -> retry_count
        self.retry_lock = threading.RLock()
        self.full_execution_retries = int(os.getenv("FULL_EXECUTION_RETRIES", "2"))
        
        # Track block dependencies (prev_hash -> dependent_hashes)
        self.block_dependencies = defaultdict(set)
        self.dependency_lock = threading.RLock()  # Use RLock for safety
        
        # Event-based signaling for validation completion
        self.validation_events = {}  # hash_id -> Event
        self.events_lock = threading.Lock()
        
        # Initialize model validator
        self.model_validator = ModelVerifier()

        # Pending operator reviews: internal hash_id(bytes) -> review payload.
        # Populated when ModelAuditor completes; drained when operator approves/rejects.
        self.pending_reviews: Dict[bytes, dict] = {}
        # Resolved reviews: internal hash_id(bytes) -> terminal decision payload.
        # Kept for re-poll identification (node re-sends same hash_id after review).
        self.resolved_reviews: Dict[bytes, dict] = {}
        self.review_lock = threading.RLock()
        self.review_state_path = os.getenv(
            "OPERATOR_REVIEW_STATE_PATH",
            "/data/operator_reviews_state.json",
        )
        self._load_review_state()

        # Worker thread control
        self.running = True
        self.workers = []

        # Retention/cleanup
        # TTL defaults to 5 days unless overridden by env
        self.ttl_seconds = int(os.getenv("VALIDATION_TTL_SECONDS", str(5 * 24 * 60 * 60)))
        # How often to run cleanup (default: hourly)
        self.cleanup_interval_seconds = int(os.getenv("VALIDATION_CLEANUP_INTERVAL_SECONDS", "3600"))

    def _load_review_state(self):
        """Load pending/resolved operator reviews from disk if present."""
        path = self.review_state_path
        if not path:
            return
        if not os.path.exists(path):
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            self.logger.warning(f"Failed to load operator review state from {path}: {e}")
            return

        pending = data.get("pending_reviews", [])
        resolved = data.get("resolved_reviews", [])
        loaded_pending = 0
        loaded_resolved = 0
        with self.review_lock:
            for item in pending:
                hash_hex = item.get("hash_id_hex", "")
                try:
                    key = bytes.fromhex(hash_hex)
                except Exception:
                    continue
                self.pending_reviews[key] = item.get("payload", {})
                loaded_pending += 1

            for item in resolved:
                hash_hex = item.get("hash_id_hex", "")
                try:
                    key = bytes.fromhex(hash_hex)
                except Exception:
                    continue
                self.resolved_reviews[key] = item.get("payload", {})
                loaded_resolved += 1

        self.logger.info(
            "Loaded operator review state: pending=%d resolved=%d from %s",
            loaded_pending,
            loaded_resolved,
            path,
        )

    def _persist_review_state_locked(self):
        """Persist pending/resolved review dictionaries to disk atomically.

        Caller must hold self.review_lock.
        """
        path = self.review_state_path
        if not path:
            return
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
        except Exception:
            # Parent may be current directory or not creatable; continue best-effort.
            pass

        payload = {
            "version": 1,
            "saved_at": time.time(),
            "pending_reviews": [
                {
                    "hash_id_hex": key.hex(),
                    "payload": value,
                }
                for key, value in self.pending_reviews.items()
            ],
            "resolved_reviews": [
                {
                    "hash_id_hex": key.hex(),
                    "payload": value,
                }
                for key, value in self.resolved_reviews.items()
            ],
        }

        tmp_path = f"{path}.tmp"
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(_json_safe(payload), f, default=str, allow_nan=False)
            os.replace(tmp_path, path)
        except Exception as e:
            self.logger.warning(f"Failed to persist operator review state to {path}: {e}")
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except Exception:
                pass

    # Phase-aware status tracking helper methods
    def is_phase_done(self, hash_id: bytes, phase: str) -> bool:
        """Check if a specific validation phase is complete"""
        with self.status_lock:
            return phase in self.validation_status.get(hash_id, {})

    def set_phase_result(self, hash_id: bytes, phase: str, result: int, prev_hash: bytes = None):
        """Set result for a specific validation phase"""
        with self.status_lock:
            st = self.validation_status.setdefault(hash_id, {})
            # Record creation time once when we first see this hash
            if 'created_at' not in st:
                st['created_at'] = time.time()
            st[phase] = result
            st['timestamp'] = time.time()
            if prev_hash is not None:
                st['prev_hash'] = prev_hash

    def _clear_event(self, hash_id: bytes):
        """Clear validation event to prevent memory leaks"""
        with self.events_lock:
            self.validation_events.pop(hash_id, None)

    @staticmethod
    def _model_hash_hex(hash_id: bytes) -> str:
        """Canonical model hash hex (same ordering as node/GUI)."""
        return hash_id[::-1].hex()

    @staticmethod
    def _decode_review_identifier(identifier_hex: str) -> list:
        """Decode operator identifier into candidate internal hash_id values.

        Preferred input is canonical model_hash hex (big-endian).
        Legacy hash_id.hex() is accepted for backward compatibility.
        """
        try:
            raw = bytes.fromhex(identifier_hex)
        except ValueError:
            return []
        if len(raw) != 32:
            return []
        preferred = raw[::-1]
        legacy = raw
        return [preferred] if preferred == legacy else [preferred, legacy]
        
    def start(self):
        """Start all worker threads"""
        # Start sender first so it’s ready before workers begin enqueueing
        self.sender.start()
        # Start receiver thread
        receiver = threading.Thread(target=self.receive_requests, daemon=True)
        receiver.start()
        self.workers.append(receiver)
        
        # Start quick validation workers 
        for i in range(1):
            worker = threading.Thread(
                target=self.process_quick_validations, 
                daemon=True, 
                name=f"quick-worker-{i}"
            )
            worker.start()
            self.workers.append(worker)
        
        # Start quick smell validation workers
        for i in range(1):
            worker = threading.Thread(
                target=self.process_quick_smell_validations,
                daemon=True,
                name=f"quick-smell-worker-{i}"
            )
            worker.start()
            self.workers.append(worker)

        # Start logits (audit) validation worker
        for i in range(1):
            worker = threading.Thread(
                target=self.process_logits_validations,
                daemon=True,
                name=f"logits-worker-{i}"
            )
            worker.start()
            self.workers.append(worker)
        
        # Start full validation workers 
        for i in range(1):
            worker = threading.Thread(
                target=self.process_full_validations, 
                daemon=True,
                name=f"full-worker-{i}"
            )
            worker.start()
            self.workers.append(worker)
        
        # Start model validation worker
        model_worker = threading.Thread(
            target=self.process_model_validations,
            daemon=True,
            name="model-worker"
        )
        model_worker.start()
        self.workers.append(model_worker)

        # Start challenge validation worker
        challenge_worker = threading.Thread(
            target=self.process_challenge_validations,
            daemon=True,
            name="challenge-worker"
        )
        challenge_worker.start()
        self.workers.append(challenge_worker)

        # Start cleanup thread
        cleaner = threading.Thread(
            target=self._cleanup_loop,
            daemon=True,
            name="cleanup-worker"
        )
        cleaner.start()
        self.workers.append(cleaner)
        
        # Wait for workers
        try:
            for worker in self.workers:
                worker.join()
        except KeyboardInterrupt:
            self.logger.info("Shutting down validator...")
            self.shutdown()
    
    def shutdown(self):
        """Gracefully shutdown the validator"""
        self.logger.info("Validator shutdown initiated")
        self.running = False
        
        # Signal all waiting threads
        with self.events_lock:
            for event in self.validation_events.values():
                event.set()
        
        # Properly close sockets
        try:
            self.pull_socket.setsockopt(zmq.LINGER, 0)
            self.pull_socket.close()
        except Exception:
            pass

        # stop sender last
        try:
            self.sender.stop()
        except Exception:
            pass

        
# Give receiver thread a brief moment to observe running=False
        try:
            time.sleep(0.05)
        except Exception:
            pass

        # Prefer destroy(linger=0) to avoid teardown hangs
        try:
            self.context.destroy(linger=0)
        except Exception:
            try:
                self.context.term()
            except Exception:
                pass

    
    def _cleanup_loop(self):
        """Periodically clean up old validation status entries"""
        while self.running:
            try:
                # Sleep for the cleanup interval
                time.sleep(self.cleanup_interval_seconds)
                
                current_time = time.time()
                expired_hashes = []
                
                # Find expired entries
                with self.status_lock:
                    for hash_id, status in self.validation_status.items():
                        created_at = status.get('created_at', status.get('timestamp', 0))
                        if current_time - created_at > self.ttl_seconds:
                            expired_hashes.append(hash_id)
                    
                    # Remove expired entries
                    for hash_id in expired_hashes:
                        del self.validation_status[hash_id]
                        self.logger.debug(f"Cleaned up expired validation status for {hash_id.hex()}")
                
                # Also clean up any associated events
                with self.events_lock:
                    for hash_id in expired_hashes:
                        self.validation_events.pop(hash_id, None)
                
                if expired_hashes:
                    self.logger.info(f"Cleaned up {len(expired_hashes)} expired validation entries")
                    
            except Exception as e:
                self.logger.error(f"Error in cleanup loop: {e}")
                # Continue running even if cleanup fails
    
    def receive_requests(self):
        """Continuously receive and enqueue validation requests"""
        while self.running:
            try:
                # Non-blocking receive with timeout
                if self.pull_socket.poll(100):  # 100ms timeout
                    message = self.pull_socket.recv()
                    self.enqueue_request(message)
            except zmq.ZMQError as e:
                if e.errno != zmq.EAGAIN:
                    self.logger.error(f"Error receiving request: {e}")
            except Exception as e:
                self.logger.error(f"Unexpected error in receiver: {e}")
    
    def enqueue_request(self, message: bytes, retry_count: int = 0):
        """Parse and enqueue validation request"""
        hash_id = None
        try:
            request = ValidationRequest.ValidationRequest.GetRootAs(message, 0)
            
            hash_id_array = request.HashIdAsNumpy()
            hash_id = hash_id_array.tobytes() if hash_id_array is not None else None
            
            if hash_id is None:
                self.logger.error("Received request with no hash_id")
                return
                
            validation_type = request.ValidationType()
            
            # Priority based on timestamp (older time = smaller number)
            priority = int(time.time() * 1000)
            
            # Package request data
            request_data = {
                'hash_id': hash_id,
                'validation_type': validation_type,
                'request': request,
                'raw_message': message,
                'timestamp': time.time(),
                'retry_count': retry_count  # Track retry count
            }
            
            # Route to appropriate queue with idempotent semantics
            if validation_type == ValidationType.ValidationType.Quick:
                # If quick already completed, ignore duplicate (no resend for quick)
                with self.status_lock:
                    if 'quick' in self.validation_status.get(hash_id, {}):
                        self.logger.debug(f"Duplicate quick for {hash_id.hex()} ignored (already completed)")
                        return
                # If already enqueued or processing, ignore
                with self._queue_lock:
                    if hash_id in self._enqueued['quick'] or hash_id in self._processing['quick']:
                        self.logger.debug(f"Duplicate quick for {hash_id.hex()} ignored (already queued/processing)")
                        return
                    self._enqueued['quick'].add(hash_id)
                self.quick_queue.put((priority, next(self._pq_counter), request_data))
                self.logger.debug(f"Enqueued quick validation for {hash_id.hex()}")
                
            elif validation_type == ValidationType.ValidationType.Quick_Smell:
                # If smell already completed, ignore duplicate (no resend for smell)
                with self.status_lock:
                    if 'smell' in self.validation_status.get(hash_id, {}):
                        self.logger.debug(f"Duplicate quick_smell for {hash_id.hex()} ignored (already completed)")
                        return
                # If already enqueued or processing, ignore
                with self._queue_lock:
                    if hash_id in self._enqueued['smell'] or hash_id in self._processing['smell']:
                        self.logger.debug(f"Duplicate quick_smell for {hash_id.hex()} ignored (already queued/processing)")
                        return
                    self._enqueued['smell'].add(hash_id)
                self.quick_smell_queue.put((priority, next(self._pq_counter), request_data))
                self.logger.debug(f"Enqueued quick smell validation for {hash_id.hex()}")
                
            elif validation_type == ValidationType.ValidationType.Full:
                # Mark that a Full request exists for this hash
                with self.full_req_lock:
                    self.full_requested.add(hash_id)
                # If we already know final full status (e.g., dependency propagation), respond immediately
                with self.status_lock:
                    st = self.validation_status.get(hash_id, {})
                    known_full = st.get('full')
                if known_full is not None:
                    self.logger.debug(f"Full requested for {hash_id.hex()} with known status {known_full}; sending immediately (no re-enqueue)")
                    self.send_response(hash_id, known_full)
                    return
                # If already enqueued or processing for full, do not enqueue again
                with self._queue_lock:
                    if hash_id in self._enqueued['full'] or hash_id in self._processing['full']:
                        self.logger.debug(f"Duplicate full for {hash_id.hex()} ignored (already queued/processing)")
                    else:
                        # Mirror a quick precheck only if quick not already done/queued/processing
                        mirror_quick = False
                        with self.status_lock:
                            quick_done = 'quick' in self.validation_status.get(hash_id, {})
                        if not quick_done:
                            if hash_id not in self._enqueued['quick'] and hash_id not in self._processing['quick']:
                                mirror_quick = True
                        # Enqueue full
                        self._enqueued['full'].add(hash_id)
                        self.full_queue.put((priority, next(self._pq_counter), request_data))
                        # Enqueue mirrored quick with slightly higher priority if needed
                        if mirror_quick:
                            quick_request_data = dict(request_data)
                            quick_request_data['suppress_quick_response'] = True
                            quick_request_data['enqueue_origin'] = 'full_precheck'
                            self._enqueued['quick'].add(hash_id)
                            self.quick_queue.put((priority - 1, next(self._pq_counter), quick_request_data))
                        self.logger.info(
                            "Enqueued full validation for %s (retry=%d, mirrored_quick=%s)",
                            hash_id.hex(),
                            retry_count,
                            mirror_quick,
                        )
                
            elif validation_type == ValidationType.ValidationType.Model:
                # If model already completed, respond immediately with cached result
                with self.status_lock:
                    known_model = self.validation_status.get(hash_id, {}).get('model')
                if known_model is not None:
                    self.logger.debug(f"Model requested for {hash_id.hex()} with known status {known_model}; sending immediately (no re-enqueue)")
                    self.send_response(hash_id, known_model)
                    return
                # If already enqueued or processing, ignore
                with self._queue_lock:
                    if hash_id in self._enqueued['model'] or hash_id in self._processing['model']:
                        self.logger.debug(f"Duplicate model for {hash_id.hex()} ignored (already queued/processing)")
                        return
                    self._enqueued['model'].add(hash_id)
                self.model_queue.put((priority, next(self._pq_counter), request_data))
                self.logger.debug(f"Enqueued model validation for {hash_id.hex()}")
            elif validation_type == ValidationType.ValidationType.Logits:
                # Audit (logits-only) verification. If already completed,
                # respond immediately with the cached result.
                with self.status_lock:
                    known_logits = self.validation_status.get(hash_id, {}).get('logits')
                if known_logits is not None:
                    self.logger.debug(
                        f"Logits requested for {hash_id.hex()} with known status {known_logits}; sending immediately"
                    )
                    self.send_response(hash_id, known_logits)
                    return
                # If already enqueued/processing, ignore duplicate
                with self._queue_lock:
                    if hash_id in self._enqueued['logits'] or hash_id in self._processing['logits']:
                        self.logger.debug(
                            f"Duplicate logits for {hash_id.hex()} ignored (already queued/processing)"
                        )
                        return
                    self._enqueued['logits'].add(hash_id)
                self.logits_queue.put((priority, next(self._pq_counter), request_data))
                self.logger.debug(f"Enqueued logits validation for {hash_id.hex()}")
            elif validation_type == ValidationType.ValidationType.Challenge:
                # If challenge already completed, respond immediately with cached result
                with self.status_lock:
                    known_challenge = self.validation_status.get(hash_id, {}).get('challenge')
                if known_challenge is not None:
                    self.logger.debug(
                        f"Challenge requested for {hash_id.hex()} with known status {known_challenge}; sending immediately"
                    )
                    self.send_response(hash_id, known_challenge)
                    return
                # If already enqueued/processing, ignore duplicate
                with self._queue_lock:
                    if hash_id in self._enqueued['challenge'] or hash_id in self._processing['challenge']:
                        self.logger.debug(
                            f"Duplicate challenge for {hash_id.hex()} ignored (already queued/processing)"
                        )
                        return
                    self._enqueued['challenge'].add(hash_id)
                self.challenge_queue.put((priority, next(self._pq_counter), request_data))
                self.logger.debug(f"Enqueued challenge validation for {hash_id.hex()}")
            else:
                self.logger.warning(f"Unknown validation type: {validation_type}")
                
        except Exception as e:
            self.logger.error(f"Error enqueuing request: {e}")
            fallback_hash = hash_id if hash_id is not None else b'\x00' * 32
            self.send_error_response(fallback_hash, kind='quick')
    
    def reenqueue_full_validation(
        self,
        hash_id: bytes,
        raw_message: bytes,
        retry_count: int,
        execution_retry_count: int = 0,
    ):
        """Re-enqueue a full validation request with updated retry count"""
        try:
            # Update retry count
            with self.retry_lock:
                self.retry_counts[hash_id] = retry_count
            
            # Priority for retries (higher priority than new requests)
            priority = int(time.time() * 1000)
            
            # Create request data for retry
            request = ValidationRequest.ValidationRequest.GetRootAs(raw_message, 0)
            request_data = {
                'hash_id': hash_id,
                'validation_type': ValidationType.ValidationType.Full,
                'request': request,
                'raw_message': raw_message,
                'timestamp': time.time(),
                'retry_count': retry_count,
                'execution_retry_count': execution_retry_count,
            }
            
            # Add to full validation queue (mark as enqueued)
            with self._queue_lock:
                self._enqueued['full'].add(hash_id)
            self.full_queue.put((priority, next(self._pq_counter), request_data))
            self.logger.info(
                f"Re-enqueued full validation for {hash_id.hex()} "
                f"(attempt {retry_count + 1}, execution_retry={execution_retry_count})"
            )
            
        except Exception as e:
            self.logger.error(f"Error re-enqueuing full validation: {e}")
    
    def process_quick_validations(self):
        """Process quick validation queue"""
        # Create dedicated ProofVerifier for this worker thread
        thread_verifier = ProofVerifier()
        mca_set_enabled(False)  # Disable MCA noise for quick validations
        
        while self.running:
            try:
                # Get from queue with timeout
                _, _, request_data = self.quick_queue.get(timeout=0.1)
                # Transition: enqueued -> processing
                with self._queue_lock:
                    hid = request_data['hash_id']
                    self._enqueued['quick'].discard(hid)
                    self._processing['quick'].add(hid)
                
                # Skip if quick phase already processed (could be from Quick_Smell)
                # if self.is_phase_done(request_data['hash_id'], 'quick'):
                #     continue
                
                # Perform quick validation with thread-specific verifier
                self.validate_quick(request_data, thread_verifier)
                # Done processing
                with self._queue_lock:
                    self._processing['quick'].discard(hid)
                
            except queue.Empty:
                continue
            except Exception as e:
                self.logger.error(f"Error in quick validation worker: {e}")
    
    def process_quick_smell_validations(self):
        """Process quick smell validation queue"""
        # Create dedicated ProofVerifier for this worker thread
        thread_verifier = ProofVerifier()
        mca_set_enabled(False)  # Disable MCA noise for quick smell validations
        
        while self.running:
            try:
                # Get from queue with timeout
                _, _, request_data = self.quick_smell_queue.get(timeout=0.1)
                # Transition: enqueued -> processing
                with self._queue_lock:
                    hid = request_data['hash_id']
                    self._enqueued['smell'].discard(hid)
                    self._processing['smell'].add(hid)
                
                # Skip if this phase already processed
                # if self.is_phase_done(request_data['hash_id'], 'smell'):
                #     continue
                
                # Perform quick smell validation with thread-specific verifier
                self.validate_quick_smell(request_data, thread_verifier)
                # Done processing
                with self._queue_lock:
                    self._processing['smell'].discard(hid)
                
            except queue.Empty:
                continue
            except Exception as e:
                self.logger.error(f"Error in quick smell validation worker: {e}")
    
    def process_logits_validations(self):
        """Process logits (audit) validation queue"""
        # Create dedicated ProofVerifier for this worker thread
        thread_verifier = ProofVerifier()
        mca_set_enabled(False)  # Disable MCA noise for logits validations

        while self.running:
            try:
                # Get from queue with timeout
                _, _, request_data = self.logits_queue.get(timeout=0.1)
                # Transition: enqueued -> processing
                with self._queue_lock:
                    hid = request_data['hash_id']
                    self._enqueued['logits'].discard(hid)
                    self._processing['logits'].add(hid)

                # Perform logits validation with thread-specific verifier
                self.validate_logits(request_data, thread_verifier)
                # Done processing
                with self._queue_lock:
                    self._processing['logits'].discard(hid)

            except queue.Empty:
                continue
            except Exception as e:
                self.logger.error(f"Error in logits validation worker: {e}")

    def validate_logits(self, request_data: dict, verifier: 'ProofVerifier'):
        """Perform audit (logits-only) verification: sequence + logits
        replay against the claimed model. No block sanity, no mining
        parameter envelope (see ProofVerifier.logits_verify)."""
        try:
            request = request_data['request']
            request_type = request.RequestType()

            if request_type == ValidationUnion.ValidationUnion.BlockValidation:
                result = verifier.logits_verify(request_data['raw_message'])
                self.set_phase_result(request_data['hash_id'], 'logits', result)
                self.send_response(request_data['hash_id'], result)
            else:
                self.logger.warning(f"Logits validation not supported for request type {request_type}")
                self.send_response(
                    request_data['hash_id'],
                    ResponseValue.ResponseValue.Logits_Fail,
                )
        except Exception as e:
            self.logger.error(f"Error in logits validation: {e}")
            self.send_response(
                request_data['hash_id'],
                ResponseValue.ResponseValue.Logits_Fail,
            )

    def process_full_validations(self):
        """Process full validation queue"""
        # Create dedicated ProofVerifier for this worker thread
        thread_verifier = ProofVerifier()
        
        # Install MCA defaults for this worker thread. Individual full
        # validations enable MCA in a scoped context.
        mca_set_params(k_lin=1.5, k_attn=8.0, target_dtype=torch.float16)  # default, gets updated per proof
        mca_set_enabled(False)

        while self.running:
            try:
                # Dynamic processing based on queue size
                if self.full_queue.qsize() > 10:
                    timeout = 0.01  # Process faster when queue is large
                else:
                    timeout = 0.1
                
                _, _, request_data = self.full_queue.get(timeout=timeout)
                # Transition: enqueued -> processing
                with self._queue_lock:
                    hid = request_data['hash_id']
                    self._enqueued['full'].discard(hid)
                    self._processing['full'].add(hid)
                # Optional remote delegation (compute once, serve many)
                if REMOTE_VERIFY_ENABLED and REMOTE_VERIFY_BASE_URL and remote_delegate is not None:
                    try:
                        # Attempt remote full verification using the raw ValidationRequest bytes
                        response_enum = remote_delegate.verify_full_remote(
                            request_data['raw_message'],
                            base_url=REMOTE_VERIFY_BASE_URL,
                            api_key=REMOTE_VERIFY_API_KEY,
                            timeout=REMOTE_VERIFY_TIMEOUT,
                        )

                        # Parse prev_hash from request for dependency tracking
                        request = request_data['request']
                        if request.RequestType() == ValidationUnion.ValidationUnion.BlockValidation:
                            block = BlockValidation.BlockValidation()
                            block.Init(request.Request().Bytes, request.Request().Pos)
                            prev_block_hash_arr = block.PrevBlockHashAsNumpy()
                            prev_hash = prev_block_hash_arr.tobytes() if prev_block_hash_arr is not None else None
                        else:
                            prev_hash = None

                        # Record status for 'full'
                        with self.status_lock:
                            st = self.validation_status.setdefault(request_data['hash_id'], {})
                            if 'created_at' not in st:
                                st['created_at'] = time.time()
                            st['full'] = response_enum
                            st['timestamp'] = time.time()
                            st['prev_hash'] = prev_hash

                        # Clear event to prevent leaks
                        self._clear_event(request_data['hash_id'])

                        # Send response
                        self.send_response(request_data['hash_id'], response_enum)

                        # Propagate on RED
                        if response_enum == ResponseValue.ResponseValue.Full_Red:
                            self.propagate_validation_failure(request_data['hash_id'])

                        # Go to next item (skip local compute)
                        with self._queue_lock:
                            self._processing['full'].discard(hid)
                        continue
                    except Exception as e:
                        # Fallback to local validation on any remote error
                        self.logger.warning(
                            f"Remote full verification failed for {request_data['hash_id'].hex()}: {e}. Falling back to local."
                        )

                # Wait for quick validation to complete first (from either Quick or Quick_Smell)
                if not self.wait_for_quick_validation(request_data['hash_id']):
                    self.logger.warning(f"Quick validation timeout for {request_data['hash_id'].hex()}")
                    continue
                
                # Check if quick validation failed (single lock acquisition)
                should_skip = False
                with self.status_lock:
                    st = self.validation_status.get(request_data['hash_id'], {})
                    quick_result = st.get('quick')
                    smell_result = st.get('smell')
                    
                    # Check direct quick result first
                    if quick_result == ResponseValue.ResponseValue.Quick_Fail:
                        should_skip = True
                    # If no direct quick result, check if smell result indicates quick failure
                    elif quick_result is None and smell_result == ResponseValue.ResponseValue.Quick_Fail_Smell_Fail:
                        should_skip = True
                    # Check if already completed as failed
                    elif st.get('full') == ResponseValue.ResponseValue.Full_Red:
                        should_skip = True

                # If quick failed but a Full request exists, answer Full_Red immediately
                if should_skip:
                    should_answer_full_red = False
                    with self.full_req_lock:
                        if request_data['hash_id'] in self.full_requested:
                            should_answer_full_red = True
                    if should_answer_full_red:
                        response_enum = ResponseValue.ResponseValue.Full_Red
                        with self.status_lock:
                            st = self.validation_status.setdefault(request_data['hash_id'], {})
                            st['full'] = response_enum
                            st['timestamp'] = time.time()
                            # prev_hash might already be set from earlier phases
                        # Transition: processing done
                        with self._queue_lock:
                            self._processing['full'].discard(hid)
                        self._clear_event(request_data['hash_id'])
                        self.logger.info(
                            f"Quick failed; responding Full_Red immediately for {request_data['hash_id'].hex()}"
                        )
                        self.send_response(request_data['hash_id'], response_enum)
                        # Also propagate failure to dependents, since Full is RED
                        self.propagate_validation_failure(request_data['hash_id'])
                    continue
                
                # Perform full validation with thread-specific verifier
                self.validate_full(request_data, thread_verifier)
                
            except queue.Empty:
                continue
            except Exception as e:
                self.logger.error(f"Error in full validation worker: {e}")
            finally:
                # Ensure processing mark is cleared if an item was being handled
                try:
                    if 'hid' in locals():
                        with self._queue_lock:
                            self._processing['full'].discard(hid)
                except Exception:
                    pass
    
    def process_model_validations(self):
        """Process model validation queue"""
        while self.running:
            try:
                _, _, request_data = self.model_queue.get(timeout=0.1)
                # Transition: enqueued -> processing
                with self._queue_lock:
                    hid = request_data['hash_id']
                    self._enqueued['model'].discard(hid)
                    self._processing['model'].add(hid)
                self.validate_model(request_data)
                with self._queue_lock:
                    self._processing['model'].discard(hid)
            except queue.Empty:
                continue
            except Exception as e:
                self.logger.error(f"Error in model validation worker: {e}")

    def process_challenge_validations(self):
        """Process challenge validation queue"""
        while self.running:
            try:
                _, _, request_data = self.challenge_queue.get(timeout=0.1)
                with self._queue_lock:
                    hid = request_data['hash_id']
                    self._enqueued['challenge'].discard(hid)
                    self._processing['challenge'].add(hid)
                self.validate_challenge(request_data)
                with self._queue_lock:
                    self._processing['challenge'].discard(hid)
            except queue.Empty:
                continue
            except Exception as e:
                self.logger.error(f"Error in challenge validation worker: {e}")
    
    def validate_quick(self, request_data: dict, verifier: 'ProofVerifier'):
        """Perform quick validation (may be redundant if Quick_Smell already ran)"""
        """Perform quick validation"""
        try:
            request = request_data['request']
            request_type = request.RequestType()
            
            if request_type == ValidationUnion.ValidationUnion.BlockValidation:
                block = BlockValidation.BlockValidation()
                block.Init(request.Request().Bytes, request.Request().Pos)
                
                # Quick validation logic with thread-specific verifier.
                # Slice 11: forward target_override_hex if the broker
                # routed this as a share-mode verification — the
                # worker substitutes it into the final PoW threshold
                # check inside _verify_block_sanity. Absent override
                # the call is byte-identical to the pre-slice-11
                # path.
                result = verifier.quick_verify(
                    request_data['raw_message'],
                    target_override_hex=request_data.get('target_override_hex'),
                )

                # Get prev_hash safely
                prev_hash_array = block.PrevBlockHashAsNumpy()
                prev_hash = prev_hash_array.tobytes() if prev_hash_array is not None else None
                
                # Store result for 'quick' phase
                self.set_phase_result(request_data['hash_id'], 'quick', result, prev_hash)
                
                # ONLY Quick sets the "quick done" event
                self._signal_validation_complete(request_data['hash_id'])
                
                # Track dependencies (separate lock)
                if prev_hash is not None:
                    with self.dependency_lock:
                        self.block_dependencies[prev_hash].add(request_data['hash_id'])
                
                # Send response (no locks held) unless this is an internal quick for Full
                if not request_data.get('suppress_quick_response', False):
                    self.send_response(request_data['hash_id'], result)
                else:
                    self.logger.debug(
                        "Suppressed standalone quick response for %s (%s)",
                        request_data['hash_id'].hex(),
                        request_data.get('enqueue_origin', 'internal'),
                    )
                
                # Propagate failure if needed (no locks held)
                if result == ResponseValue.ResponseValue.Quick_Fail:
                    self.propagate_validation_failure(request_data['hash_id'])
                    
            else:
                self.logger.warning(f"Quick validation not supported for request type {request_type}")
                
        except Exception as e:
            self.logger.error(f"Error in quick validation: {e}")
            if request_data.get('suppress_quick_response', False):
                # Internal quick: set failure status without emitting a Quick response
                try:
                    with self.status_lock:
                        st = self.validation_status.setdefault(request_data['hash_id'], {})
                        st['quick'] = ResponseValue.ResponseValue.Quick_Fail
                        st['smell'] = ResponseValue.ResponseValue.Quick_Fail_Smell_Fail
                        st['timestamp'] = time.time()
                    # Signal quick completion
                    self._signal_validation_complete(request_data['hash_id'])
                except Exception:
                    pass
            else:
                self.send_error_response(request_data['hash_id'], kind='quick')
    
    def validate_quick_smell(self, request_data: dict, verifier: 'ProofVerifier'):
        """Perform quick smell validation (superset: does both quick + smell)"""
        try:
            request = request_data['request']
            request_type = request.RequestType()
            
            if request_type == ValidationUnion.ValidationUnion.BlockValidation:
                block = BlockValidation.BlockValidation()
                block.Init(request.Request().Bytes, request.Request().Pos)
                
                # Quick smell validation logic - returns ResponseValue enum.
                # Slice 11: forward override to relax only the final
                # PoW threshold check. See validate_quick comment.
                result = verifier.quick_verify_smell_test(
                    request_data['raw_message'],
                    target_override_hex=request_data.get('target_override_hex'),
                )

                # Get prev_hash safely
                prev_hash_array = block.PrevBlockHashAsNumpy()
                prev_hash = prev_hash_array.tobytes() if prev_hash_array is not None else None
                
                # Store result for 'smell' phase
                self.set_phase_result(request_data['hash_id'], 'smell', result, prev_hash)
                
                # IMPORTANT: Since smell test is a superset, also set the quick result
                # Extract the quick result from the smell result
                if result in (ResponseValue.ResponseValue.Quick_OK_Smell_OK, ResponseValue.ResponseValue.Quick_OK_Smell_Fail):
                    # Quick part passed
                    quick_result = ResponseValue.ResponseValue.Quick_OK
                    self.set_phase_result(request_data['hash_id'], 'quick', quick_result, prev_hash)
                    # Signal Quick completion to unblock Full validation
                    self._signal_validation_complete(request_data['hash_id'])
                elif result == ResponseValue.ResponseValue.Quick_Fail_Smell_Fail:
                    # Quick part failed
                    quick_result = ResponseValue.ResponseValue.Quick_Fail
                    self.set_phase_result(request_data['hash_id'], 'quick', quick_result, prev_hash)
                    # Still signal completion (Full will see the failure and skip)
                    self._signal_validation_complete(request_data['hash_id'])
                
                # Track dependencies (separate lock)
                if prev_hash is not None:
                    with self.dependency_lock:
                        self.block_dependencies[prev_hash].add(request_data['hash_id'])
                
                # Send response (no locks held)
                self.send_response(request_data['hash_id'], result)
                
                # IMPORTANT: Only propagate failure for Quick_Fail_Smell_Fail
                # Quick_OK_Smell_Fail should NOT trigger propagation
                if result == ResponseValue.ResponseValue.Quick_Fail_Smell_Fail:
                    self.propagate_validation_failure(request_data['hash_id'])
                    
            else:
                self.logger.warning(f"Quick smell validation not supported for request type {request_type}")
                
        except Exception as e:
            self.logger.error(f"Error in quick smell validation: {e}")
            self.send_error_response(request_data['hash_id'], kind='quick')
    
    def validate_full(self, request_data: dict, verifier: 'ProofVerifier'):
        """Perform full validation with re-enqueuing logic"""
        try:
            request = request_data['request']
            request_type = request.RequestType()
            
            if request_type == ValidationUnion.ValidationUnion.BlockValidation:
                block = BlockValidation.BlockValidation()
                block.Init(request.Request().Bytes, request.Request().Pos)
                
                hash_id = request_data['hash_id']
                retry_count = request_data.get('retry_count', 0)
                
                # Full validation logic with thread-specific verifier
                result = verifier.full_verify(request_data['raw_message'])
                
                self.logger.info(f"Full validation for {hash_id.hex()} (attempt {retry_count + 1}): {result}")
                
                # Map result to response enum
                if result == 'GREEN':
                    response_enum = ResponseValue.ResponseValue.Full_Green
                elif result == 'AMBER':
                    response_enum = ResponseValue.ResponseValue.Full_Amber
                elif result == 'RED':
                    response_enum = ResponseValue.ResponseValue.Full_Red
                else:
                    raise RuntimeError(f"Unexpected full verifier status: {result!r}")
                
                # Check if we should re-enqueue based on result and retry count
                should_reenqueue = False
                
                if result == 'RED' and retry_count < 1:
                    # Re-enqueue RED results once (max 1 retry)
                    should_reenqueue = True
                    self.logger.info(f"RED result for {hash_id.hex()}, re-enqueuing (attempt {retry_count + 1}/1)")
                    
                elif result == 'AMBER' and retry_count < 2:
                    # Re-enqueue AMBER results twice (max 2 retries)
                    should_reenqueue = True
                    self.logger.info(f"AMBER result for {hash_id.hex()}, re-enqueuing (attempt {retry_count + 1}/2)")
                
                if should_reenqueue:
                    # Re-enqueue for another full validation attempt
                    self.reenqueue_full_validation(hash_id, request_data['raw_message'], retry_count + 1)
                    # Don't send response yet, wait for retry
                    return
                
                # Final result - no more retries
                self.logger.info(f"Final validation result for {hash_id.hex()}: {result} (after {retry_count + 1} attempts)")
                
                # Get prev_hash safely
                prev_hash_array = block.PrevBlockHashAsNumpy()
                prev_hash = prev_hash_array.tobytes() if prev_hash_array is not None else None
                
                # Update status for 'full' phase
                with self.status_lock:
                    st = self.validation_status.setdefault(hash_id, {})
                    if 'created_at' not in st:
                        st['created_at'] = time.time()
                    st['full'] = response_enum
                    st['timestamp'] = time.time()
                    st['prev_hash'] = prev_hash
                    st['final_attempt'] = retry_count + 1
                
                # Clear event to prevent memory leaks
                self._clear_event(hash_id)
                
                # Send response (no locks held)
                self.send_response(hash_id, response_enum)
                
                # Propagate failure if RED (no locks held)
                if result == 'RED':
                    self.propagate_validation_failure(hash_id)
                    
            else:
                self.logger.warning(f"Full validation not supported for request type {request_type}")
                
        except Exception as e:
            self.logger.error(f"Error in full validation: {e}")
            hash_id = request_data.get('hash_id', b'\x00' * 32)
            retry_count = request_data.get('retry_count', 0)
            execution_retry_count = request_data.get('execution_retry_count', 0)
            if execution_retry_count < self.full_execution_retries:
                self.logger.warning(
                    f"Full validation execution error for {hash_id.hex()}, re-enqueuing "
                    f"(execution retry {execution_retry_count + 1}/{self.full_execution_retries})"
                )
                self.reenqueue_full_validation(
                    hash_id,
                    request_data['raw_message'],
                    retry_count,
                    execution_retry_count + 1,
                )
            else:
                self.logger.error(
                    f"Full validation execution error for {hash_id.hex()} exhausted "
                    f"{self.full_execution_retries} retries; not sending a Full_Red response"
                )
                self._clear_event(hash_id)

    @staticmethod
    def _split_model_identifier(model_identifier: str) -> Tuple[str, str]:
        model_identifier = (model_identifier or "").strip()
        if not model_identifier:
            return "", ""
        if "@" not in model_identifier:
            return model_identifier, ""
        model_name, model_commit = model_identifier.rsplit("@", 1)
        return model_name.strip(), model_commit.strip()

    def _extract_challenge_context(self, request_data: dict) -> dict:
        """
        Parse challenge BlockValidation payload and extract:
        - challenged_block_hash (canonical hex)
        - model_identifier from pow blob
        - model_name/model_commit split
        """
        request = request_data['request']
        request_type = request.RequestType()
        if request_type != ValidationUnion.ValidationUnion.BlockValidation:
            raise ValueError(f"Challenge expects BlockValidation request, got type={request_type}")

        block = BlockValidation.BlockValidation()
        block.Init(request.Request().Bytes, request.Request().Pos)

        challenged_block_hash = ""
        try:
            arr = block.HashAsNumpy()
            if arr is not None:
                challenged_block_hash = arr.tobytes()[::-1].hex()
        except Exception:
            challenged_block_hash = ""

        model_identifier = ""
        proof = block.PowBlob()
        if proof is not None and proof.ModelIdentifier():
            raw = proof.ModelIdentifier()
            if isinstance(raw, (bytes, bytearray)):
                model_identifier = raw.decode("utf-8", errors="replace")
            else:
                model_identifier = str(raw)

        model_name, model_commit = self._split_model_identifier(model_identifier)
        return {
            "challenged_block_hash": challenged_block_hash,
            "model_identifier": model_identifier,
            "model_name": model_name,
            "model_commit": model_commit,
        }

    def validate_challenge(self, request_data: dict):
        """
        Perform challenge validation with operator review gate.

        Behavior mirrors model validation:
        - run model audit (by model_identifier in proof payload),
        - enqueue operator review with report,
        - any audit/parsing error still becomes pending operator review.
        """
        hash_id = request_data['hash_id']
        try:
            with self.review_lock:
                resolved = self.resolved_reviews.get(hash_id)
                if resolved is not None:
                    response_enum = resolved['response_enum']
                    self.set_phase_result(hash_id, 'challenge', response_enum, None)
                    self.send_response(hash_id, response_enum)
                    return
                if hash_id in self.pending_reviews:
                    # Challenge has no explicit "pending" enum in protocol.
                    # Keep silent and wait for operator decision.
                    return

            # Optional remote delegation for challenge verification.
            if REMOTE_VERIFY_ENABLED and REMOTE_VERIFY_BASE_URL and remote_delegate is not None:
                try:
                    response_enum = remote_delegate.verify_challenge_remote(
                        request_data['raw_message'],
                        base_url=REMOTE_VERIFY_BASE_URL,
                        api_key=REMOTE_VERIFY_API_KEY,
                        timeout=REMOTE_VERIFY_TIMEOUT,
                    )
                    if response_enum == ResponseValue.ResponseValue.Model_Pending_Review:
                        # Challenge protocol has no explicit pending enum; keep pending locally.
                        self._store_pending_review(
                            hash_id,
                            {"report": "remote_pending_review"},
                            request_data,
                            model_name="(remote)",
                            claimed_difficulty=0,
                            review_type="challenge",
                            approve_enum=ResponseValue.ResponseValue.Challenge_OK,
                            reject_enum=ResponseValue.ResponseValue.Challenge_Fail,
                        )
                        return

                    self.set_phase_result(hash_id, 'challenge', response_enum, None)
                    self.send_response(hash_id, response_enum)
                    return
                except Exception as e:
                    self.logger.warning(
                        f"Remote challenge verification failed for {hash_id.hex()}: {e}. Falling back to local."
                    )

            ctx = self._extract_challenge_context(request_data)
            model_identifier = ctx.get("model_identifier", "")
            model_name = ctx.get("model_name", "")
            model_commit = ctx.get("model_commit", "")

            # For challenge flow difficulty claim is irrelevant; we only need the audit report.
            status, audit_report = self.model_validator.validate(
                request_data['raw_message'],
                claimed_difficulty=0,
                model_name=model_name,
                model_commit=model_commit,
            )

            if status != "pending_operator_review":
                audit_report = {
                    "audit_completed": False,
                    "requires_operator_decision": True,
                    "failure_reason": "unexpected_validator_status",
                    "failure_stage": "validator_return",
                    "error": f"Unexpected validator status: {status}",
                    "model_identifier": model_identifier,
                    "model_name": model_name,
                    "model_commit": model_commit,
                    "original_report": audit_report if isinstance(audit_report, dict) else {},
                    "audit_timestamp": int(time.time()),
                }

            if not isinstance(audit_report, dict):
                audit_report = {"report": str(audit_report)}
            audit_report.setdefault("challenge_context", {})
            audit_report["challenge_context"].update({
                "challenged_block_hash": ctx.get("challenged_block_hash", ""),
                "model_identifier": model_identifier,
                "model_name": model_name,
                "model_commit": model_commit,
            })

            self._store_pending_review(
                hash_id,
                audit_report,
                request_data,
                model_name=model_name or model_identifier,
                claimed_difficulty=0,
                review_type="challenge",
                approve_enum=ResponseValue.ResponseValue.Challenge_OK,
                reject_enum=ResponseValue.ResponseValue.Challenge_Fail,
            )
            self.logger.info(
                "Challenge %s queued for operator review (model=%s)",
                hash_id.hex(),
                model_identifier or "<empty>",
            )
            self._post_audit_report_to_gateway(
                self._model_hash_hex(hash_id),
                model_name or model_identifier,
                0,
                audit_report,
                review_type="challenge",
                hash_id_hex=hash_id.hex(),
            )
            return

        except Exception as e:
            self.logger.error(f"Error in challenge validation: {e}", exc_info=True)
            failure_report = {
                "audit_completed": False,
                "requires_operator_decision": True,
                "failure_reason": "challenge_audit_exception",
                "failure_stage": "challenge_validate",
                "error": str(e),
                "traceback": traceback.format_exc(),
                "audit_timestamp": int(time.time()),
            }
            self._store_pending_review(
                hash_id,
                failure_report,
                request_data,
                model_name="",
                claimed_difficulty=0,
                review_type="challenge",
                approve_enum=ResponseValue.ResponseValue.Challenge_OK,
                reject_enum=ResponseValue.ResponseValue.Challenge_Fail,
            )
            self._post_audit_report_to_gateway(
                self._model_hash_hex(hash_id),
                "",
                0,
                failure_report,
                review_type="challenge",
                hash_id_hex=hash_id.hex(),
            )
            return
    
    def validate_model(self, request_data: dict):
        """Perform model validation with operator review gate.

        Flow:
        1. Check if this hash_id has a resolved review → send terminal response.
        2. Check if this hash_id has a pending review → send Model_Pending_Review.
        3. Try remote delegation (if configured).
        4. Run local ModelAuditor + difficulty validation.
        5. Store pending review, send Model_Pending_Review.
        """
        hash_id = request_data['hash_id']
        try:
            # Check if operator already decided on this hash_id (re-poll from node)
            with self.review_lock:
                resolved = self.resolved_reviews.get(hash_id)
                if resolved is not None:
                    response_enum = resolved['response_enum']
                    self.set_phase_result(hash_id, 'model', response_enum, None)
                    self.send_response(hash_id, response_enum)
                    return

                # Already pending — send Model_Pending_Review again (node re-poll)
                if hash_id in self.pending_reviews:
                    self.send_response(hash_id, ResponseValue.ResponseValue.Model_Pending_Review)
                    return

            # Optional remote delegation (compute once, serve many)
            if REMOTE_VERIFY_ENABLED and REMOTE_VERIFY_BASE_URL and remote_delegate is not None:
                try:
                    response_enum = remote_delegate.verify_model_remote(
                        request_data['raw_message'],
                        base_url=REMOTE_VERIFY_BASE_URL,
                        api_key=REMOTE_VERIFY_API_KEY,
                        timeout=REMOTE_VERIFY_TIMEOUT,
                    )
                    # Remote may return Model_Pending_Review (enum 13) if it also
                    # uses operator review — propagate as-is.
                    if response_enum == ResponseValue.ResponseValue.Model_Pending_Review:
                        self._store_pending_review(hash_id, {}, request_data,
                                                   model_name="(remote)", claimed_difficulty=0)
                    else:
                        self.set_phase_result(hash_id, 'model', response_enum, None)
                    self.send_response(hash_id, response_enum)
                    return
                except Exception as e:
                    self.logger.warning(
                        f"Remote model verification failed for {hash_id.hex()}: {e}. Falling back to local."
                    )

            request = request_data['request']
            request_type = request.RequestType()

            if request_type == ValidationUnion.ValidationUnion.ModelValidation:
                model = ModelValidation.ModelValidation()
                model.Init(request.Request().Bytes, request.Request().Pos)

                claimed_difficulty = model.Difficulty()
                model_name = model.ModelName().decode('utf-8') if model.ModelName() else ""
                model_commit = model.ModelCommit().decode('utf-8') if model.ModelCommit() else ""

                status, audit_report = self.model_validator.validate(
                    request_data['raw_message'],
                    claimed_difficulty=claimed_difficulty,
                    model_name=model_name,
                    model_commit=model_commit,
                )

                if status != "pending_operator_review":
                    self.logger.warning(
                        "Unexpected model validation status for %s (%s): %s. "
                        "Forcing pending operator review.",
                        model_name,
                        hash_id.hex(),
                        status,
                    )
                    audit_report = {
                        "audit_completed": False,
                        "requires_operator_decision": True,
                        "failure_reason": "unexpected_validator_status",
                        "failure_stage": "validator_return",
                        "error": f"Unexpected validator status: {status}",
                        "model_name": model_name,
                        "model_commit": model_commit,
                        "claimed_difficulty": claimed_difficulty,
                        "original_report": audit_report if isinstance(audit_report, dict) else {},
                        "audit_timestamp": int(time.time()),
                    }

                self._store_pending_review(
                    hash_id, audit_report, request_data,
                    model_name=model_name, claimed_difficulty=claimed_difficulty,
                )
                self.send_response(hash_id, ResponseValue.ResponseValue.Model_Pending_Review)
                model_hash_hex = self._model_hash_hex(hash_id)
                self.logger.info(
                    f"Model {model_name} ({model_hash_hex}) queued for operator review"
                )
                # Fire-and-forget: POST audit report to gateway for the
                # superadmin UI (Path R). Only if GATEWAY_CALLBACK_URL is set.
                self._post_audit_report_to_gateway(
                    model_hash_hex, model_name, claimed_difficulty, audit_report,
                    review_type="model", hash_id_hex=hash_id.hex(),
                )
                return
            else:
                self.logger.warning(f"Model validation requested but wrong request type: {request_type}")

        except Exception as e:
            self.logger.error(f"Error in model validation: {e}")
            self.send_error_response(hash_id, kind='model')

    def _store_pending_review(
        self,
        hash_id: bytes,
        audit_report: dict,
        request_data: dict,
        model_name: str = "",
        claimed_difficulty: int = 0,
        review_type: str = "model",
        approve_enum: int = ResponseValue.ResponseValue.Model_OK,
        reject_enum: int = ResponseValue.ResponseValue.Model_Fail,
    ):
        """Store a pending operator review for a model validation request."""
        with self.review_lock:
            self.pending_reviews[hash_id] = {
                "report": audit_report,
                "model_name": model_name,
                "claimed_difficulty": claimed_difficulty,
                "submitted_at": time.time(),
                "hash_id_hex": hash_id.hex(),
                "model_hash": self._model_hash_hex(hash_id),
                "review_type": review_type,
                "approve_enum": approve_enum,
                "reject_enum": reject_enum,
            }
            self._persist_review_state_locked()

    def resolve_review(self, hash_id: bytes, approved: bool, notes: str = "") -> bool:
        """Operator approves or rejects a pending review.

        Sends the terminal ZMQ response (Model/Challenge OK/Fail) to the node
        and moves the review from pending to resolved.

        Returns True if the review was found and resolved, False otherwise.
        """
        with self.review_lock:
            review = self.pending_reviews.pop(hash_id, None)
            if review is None:
                return False

            approve_enum = review.get("approve_enum", ResponseValue.ResponseValue.Model_OK)
            reject_enum = review.get("reject_enum", ResponseValue.ResponseValue.Model_Fail)
            response_enum = approve_enum if approved else reject_enum
            self.resolved_reviews[hash_id] = {
                "response_enum": response_enum,
                "approved": approved,
                "notes": notes,
                "resolved_at": time.time(),
                "model_name": review.get("model_name", ""),
                "review_type": review.get("review_type", "model"),
            }
            self._persist_review_state_locked()

        phase = "challenge" if self.resolved_reviews.get(hash_id, {}).get("review_type") == "challenge" else "model"
        self.set_phase_result(hash_id, phase, response_enum, None)
        self.send_response(hash_id, response_enum)
        self.logger.info(
            f"Review resolved: {hash_id.hex()} → {'approved' if approved else 'rejected'}"
        )
        return True

    def get_pending_reviews(self) -> list:
        """Return summary-only pending reviews for the operator UI list."""
        with self.review_lock:
            return [
                {
                    "review_type": info.get("review_type", "model"),
                    "model_hash": info.get("model_hash", ""),
                    "hash_id": info["hash_id_hex"],
                    "model_name": info.get("model_name", ""),
                    "claimed_difficulty": info.get("claimed_difficulty", 0),
                    "status": "pending_operator_review",
                    "submitted_at": info.get("submitted_at", 0),
                }
                for info in self.pending_reviews.values()
            ]

    def get_review(self, identifier_hex: str) -> Optional[dict]:
        """Get a single review by model_hash (preferred) or legacy hash_id."""
        candidates = self._decode_review_identifier(identifier_hex)
        if not candidates:
            return None
        with self.review_lock:
            for target in candidates:
                pending = self.pending_reviews.get(target)
                if pending:
                    return {
                        "review_type": pending.get("review_type", "model"),
                        "model_hash": pending.get("model_hash", self._model_hash_hex(target)),
                        "hash_id": pending.get("hash_id_hex", target.hex()),
                        "model_name": pending.get("model_name", ""),
                        "claimed_difficulty": pending.get("claimed_difficulty", 0),
                        "status": "pending_operator_review",
                        "audit_report": pending.get("report", {}),
                        "submitted_at": pending.get("submitted_at", 0),
                    }
                resolved = self.resolved_reviews.get(target)
                if resolved:
                    return {
                        "review_type": resolved.get("review_type", "model"),
                        "model_hash": self._model_hash_hex(target),
                        "hash_id": target.hex(),
                        "model_name": resolved.get("model_name", ""),
                        "status": "approved" if resolved.get("approved") else "rejected",
                        "notes": resolved.get("notes", ""),
                        "resolved_at": resolved.get("resolved_at", 0),
                    }
        return None

    def get_review_stats(self) -> dict:
        """Return counts for the operator dashboard."""
        with self.review_lock:
            pending = len(self.pending_reviews)
            approved = sum(1 for r in self.resolved_reviews.values() if r.get("approved"))
            rejected = sum(1 for r in self.resolved_reviews.values() if not r.get("approved"))
        return {"pending": pending, "approved": approved, "rejected": rejected}

    def _post_audit_report_to_gateway(
        self,
        model_hash_hex: str,
        model_name: str,
        claimed_difficulty: int,
        audit_report: dict,
        review_type: str = "model",
        hash_id_hex: str = "",
    ):
        """Fire-and-forget POST of audit report to the gateway (Path R).

        Only fires for remote/orchestrator mode. Runs in a separate thread
        to avoid blocking the model worker.
        """
        gateway_url = os.getenv("GATEWAY_CALLBACK_URL", "")
        if not gateway_url or not REMOTE_VERIFY_ENABLED:
            return

        def _do_post():
            import urllib.request
            internal_key = os.getenv("INTERNAL_API_KEY", "")
            url = gateway_url.rstrip("/") + "/v1/internal/model-audit-report"
            review_key = model_hash_hex
            idempotency_key = f"audit:{review_type}:{review_key}:{hash_id_hex or review_key}"
            payload = json.dumps(_json_safe({
                "review_key": review_key,
                "model_hash": model_hash_hex,
                "hash_id": hash_id_hex or model_hash_hex,  # backward compatibility
                "model_name": model_name,
                "claimed_difficulty": claimed_difficulty,
                "review_type": review_type,
                "submitted_at": time.time(),
                "audit_report": audit_report,
            }), default=str, allow_nan=False).encode("utf-8")
            req = urllib.request.Request(
                url,
                data=payload,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {internal_key}",
                    "X-Idempotency-Key": idempotency_key,
                },
                method="POST",
            )
            max_attempts = max(1, int(os.getenv("GATEWAY_CALLBACK_MAX_ATTEMPTS", "5")))
            timeout_seconds = max(1.0, float(os.getenv("GATEWAY_CALLBACK_TIMEOUT_SECONDS", "10")))
            backoff_seconds = max(0.5, float(os.getenv("GATEWAY_CALLBACK_BACKOFF_SECONDS", "2")))
            for attempt in range(1, max_attempts + 1):
                try:
                    with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:
                        self.logger.info(
                            "Posted audit report to gateway: status=%s review_key=%s attempt=%d",
                            getattr(resp, "status", 200),
                            review_key[:16],
                            attempt,
                        )
                        return
                except Exception as e:
                    if attempt >= max_attempts:
                        self.logger.warning(
                            "Failed to post audit report to gateway after %d attempts: %s",
                            attempt,
                            e,
                        )
                        return
                    sleep_for = min(60.0, backoff_seconds * (2 ** (attempt - 1)))
                    self.logger.warning(
                        "Gateway callback failed (attempt %d/%d): %s; retry in %.1fs",
                        attempt,
                        max_attempts,
                        e,
                        sleep_for,
                    )
                    time.sleep(sleep_for)

        threading.Thread(target=_do_post, daemon=True, name="gateway-callback").start()

    def propagate_validation_failure(self, failed_hash: bytes):
        """Propagate validation failure to dependent blocks - NON-RECURSIVE"""
        try:
            # Use iterative approach instead of recursion to avoid stack overflow and complex locking
            to_fail = [failed_hash]
            processed = set()
            
            while to_fail and self.running:
                current_hash = to_fail.pop(0)
                if current_hash in processed:
                    continue
                processed.add(current_hash)
                
                # Get dependent hashes (minimal lock time)
                dependent_hashes = set()
                with self.dependency_lock:
                    dependent_hashes = self.block_dependencies.get(current_hash, set()).copy()
                
                for dep_hash in dependent_hashes:
                    # Update status (minimal lock time) - mark full phase as failed
                    with self.status_lock:
                        st = self.validation_status.setdefault(dep_hash, {})
                        st['full'] = ResponseValue.ResponseValue.Full_Red
                    
                    # Send response only if a Full request exists for this hash
                    should_send = False
                    with self.full_req_lock:
                        if dep_hash in self.full_requested:
                            should_send = True
                    if should_send:
                        self.send_response(dep_hash, ResponseValue.ResponseValue.Full_Red)
                    
                    # Add to failure propagation queue
                    to_fail.append(dep_hash)
                    
        except Exception as e:
            self.logger.error(f"Error in propagating validation failure: {e}")
    
    def send_response(self, hash_id: bytes, response_enum: int):
        """Queue validation response for sending by the broker"""
        try:
            builder = flatbuffers.Builder(256)

            # Create hash identifier vector
            ValidationResponse.StartHashIdentifierVector(builder, len(hash_id))
            for i in reversed(range(len(hash_id))):
                builder.PrependUint8(hash_id[i])
            hash_vector = builder.EndVector()

            # Create response
            ValidationResponse.Start(builder)
            ValidationResponse.AddHashIdentifier(builder, hash_vector)
            ValidationResponse.AddEnumResponse(builder, response_enum)
            response = ValidationResponse.End(builder)

            builder.Finish(response)

            payload = builder.Output()
            self.logger.debug(
                f"Queueing response: hash={hash_id.hex()} enum={int(response_enum)}"
            )
            ok = self.sender.submit(payload)
            if not ok:
                self.logger.warning("Outbound queue full, dropping response")

        except Exception as e:
            self.logger.error(f"Error preparing response: {e}")
    
    def send_error_response(self, hash_id: bytes, kind: str = 'quick'):
        """Do not turn local execution errors into validator votes."""
        try:
            self.logger.error(
                f"Execution error during {kind} validation for {hash_id.hex()}; "
                "not sending a failure response"
            )
            if kind in {'quick', 'full'}:
                self._clear_event(hash_id)
        except Exception as e:
            self.logger.error(f"Error sending error response: {e}")
    
    def is_already_processed(self, hash_id: bytes) -> bool:
        """Check if validation is completely finished (has full result)"""
        with self.status_lock:
            st = self.validation_status.get(hash_id, {})
            return 'full' in st
    
    def _signal_validation_complete(self, hash_id: bytes):
        """Signal that validation is complete for the given hash"""
        with self.events_lock:
            if hash_id in self.validation_events:
                self.validation_events[hash_id].set()
    
    def wait_for_quick_validation(self, hash_id: bytes, timeout: float = 5.0) -> bool:
        """Wait for quick validation to complete (either from Quick or Quick_Smell)"""
        # Check if quick phase already completed (from either Quick or Quick_Smell)
        with self.status_lock:
            if 'quick' in self.validation_status.get(hash_id, {}):
                return True
        
        # Create or get event for this hash
        with self.events_lock:
            if hash_id not in self.validation_events:
                self.validation_events[hash_id] = threading.Event()
            event = self.validation_events[hash_id]
        
        # Wait for event (no polling, no lock contention!)
        if event.wait(timeout):
            return True
        
        # Timeout - check one more time in case of race condition
        with self.status_lock:
            return 'quick' in self.validation_status.get(hash_id, {})


def setup_logging():
    """Setup logging configuration"""
    logging.basicConfig(
        level=logging.DEBUG,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler('async_validator.log')
        ]
    )


# ---------------------------------------------------------------------------
# Embedded HTTP server for operator review endpoints (Phase 3)
# ---------------------------------------------------------------------------

from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

# Module-level reference set by __main__ before starting the server thread
_validator_instance: Optional['AsyncValidator'] = None

def _json_safe(value):
    """Recursively normalize payload to strict JSON-compatible values.

    Preserve non-finite numeric tokens as strings:
    NaN -> "NaN", +Inf -> "Infinity", -Inf -> "-Infinity".
    """
    if isinstance(value, float):
        if math.isnan(value):
            return "NaN"
        if math.isinf(value):
            return "Infinity" if value > 0 else "-Infinity"
        return value
    if isinstance(value, np.floating):
        f = float(value)
        if math.isnan(f):
            return "NaN"
        if math.isinf(f):
            return "Infinity" if f > 0 else "-Infinity"
        return f
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.bool_):
        return bool(value)
    if isinstance(value, np.ndarray):
        return [_json_safe(v) for v in value.tolist()]
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(v) for v in value]
    return value


class OperatorHTTPHandler(BaseHTTPRequestHandler):
    """
    Lightweight HTTP handler for operator review endpoints.

    Runs in main.py's process — direct access to AsyncValidator's
    pending_reviews and resolved_reviews dicts. No cross-process issue.

    Endpoints:
      GET  /v1/operator/reviews          — list pending reviews
      GET  /v1/operator/reviews/stats     — counts
      GET  /v1/operator/reviews/{hash_id} — single review detail
      POST /v1/operator/reviews/{hash_id}/approve
      POST /v1/operator/reviews/{hash_id}/reject
      GET  /health                        — health check
    """

    def log_message(self, format, *args):
        logger = logging.getLogger("operator-http")
        logger.info(format % args)

    def _check_auth(self) -> bool:
        """Verify OPERATOR_API_KEY if configured. Returns True if authorized."""
        api_key = os.getenv("OPERATOR_API_KEY", "")
        if not api_key:
            return True  # No key configured — auth disabled (local-only miner)
        auth_header = self.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            self._send_json({"error": "Unauthorized: missing Bearer token"}, 401)
            return False
        if auth_header[7:] != api_key:
            self._send_json({"error": "Unauthorized: invalid API key"}, 401)
            return False
        return True

    def _send_json(self, data: dict, status: int = 200):
        body = json.dumps(_json_safe(data), default=str, allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        # Only allow CORS from configured origin, not wildcard
        allowed_origin = os.getenv("OPERATOR_CORS_ORIGIN", "")
        if allowed_origin:
            self.send_header("Access-Control-Allow-Origin", allowed_origin)
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self) -> dict:
        content_length = int(self.headers.get("Content-Length", 0))
        if content_length == 0:
            return {}
        raw = self.rfile.read(content_length)
        return json.loads(raw)

    def _parse_path(self):
        parsed = urlparse(self.path)
        return parsed.path.rstrip("/"), parse_qs(parsed.query)

    def do_OPTIONS(self):
        self.send_response(204)
        allowed_origin = os.getenv("OPERATOR_CORS_ORIGIN", "")
        if allowed_origin:
            self.send_header("Access-Control-Allow-Origin", allowed_origin)
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.end_headers()

    def do_GET(self):
        path, query = self._parse_path()

        # Health check is unauthenticated
        if path == "/health":
            return self._send_json({"status": "ok"})

        if not self._check_auth():
            return

        v = _validator_instance
        if v is None:
            return self._send_json({"error": "Validator not initialized"}, 503)

        if path == "/v1/operator/reviews/stats":
            return self._send_json(v.get_review_stats())

        if path == "/v1/operator/reviews":
            status_filter = query.get("status", ["pending_operator_review"])[0]
            reviews = v.get_pending_reviews()
            if status_filter != "pending_operator_review":
                # Filter resolved reviews from resolved_reviews dict
                with v.review_lock:
                    for key, info in v.resolved_reviews.items():
                        review_status = "approved" if info.get("approved") else "rejected"
                        if review_status == status_filter:
                            reviews.append({
                                "review_type": info.get("review_type", "model"),
                                "model_hash": v._model_hash_hex(key),
                                "hash_id": key.hex(),
                                "model_name": info.get("model_name", ""),
                                "status": review_status,
                                "notes": info.get("notes", ""),
                                "resolved_at": info.get("resolved_at", 0),
                            })
            return self._send_json({"reviews": reviews, "total": len(reviews)})

        # GET /v1/operator/reviews/{model_hash}
        prefix = "/v1/operator/reviews/"
        if path.startswith(prefix) and "/" not in path[len(prefix):]:
            identifier_hex = path[len(prefix):]
            review = v.get_review(identifier_hex)
            if review is None:
                return self._send_json({"error": "Review not found"}, 404)
            return self._send_json(review)

        return self._send_json({"error": "Not found"}, 404)

    def do_POST(self):
        if not self._check_auth():
            return

        path, _ = self._parse_path()
        v = _validator_instance
        if v is None:
            return self._send_json({"error": "Validator not initialized"}, 503)

        # POST /v1/operator/reviews/{model_hash}/approve
        # POST /v1/operator/reviews/{model_hash}/reject
        prefix = "/v1/operator/reviews/"
        if not path.startswith(prefix):
            return self._send_json({"error": "Not found"}, 404)

        remainder = path[len(prefix):]
        parts = remainder.split("/")
        if len(parts) != 2 or parts[1] not in ("approve", "reject"):
            return self._send_json({"error": "Not found"}, 404)

        identifier_hex = parts[0]
        action = parts[1]

        try:
            body = self._read_json_body()
        except Exception:
            body = {}

        notes = body.get("notes", "")
        approved = action == "approve"

        targets = v._decode_review_identifier(identifier_hex)
        if not targets:
            return self._send_json({"error": "Invalid review identifier hex"}, 400)

        resolved_target = None
        for target in targets:
            if v.resolve_review(target, approved=approved, notes=notes):
                resolved_target = target
                break
        if resolved_target is None:
            return self._send_json({"error": "Review not found or already resolved"}, 404)

        model_hash_hex = v._model_hash_hex(resolved_target)
        resolved_review = v.resolved_reviews.get(resolved_target, {})

        # If gateway callback configured, notify gateway of the decision
        gateway_url = os.getenv("GATEWAY_CALLBACK_URL", "")
        if gateway_url:
            _notify_gateway_decision(
                gateway_url,
                model_hash=model_hash_hex,
                approved=approved,
                notes=notes,
                review_type=resolved_review.get("review_type", "model"),
                hash_id_hex=resolved_target.hex(),
            )

        return self._send_json({
            "status": "approved" if approved else "rejected",
            "review_type": resolved_review.get("review_type", "model"),
            "model_hash": model_hash_hex,
            "hash_id": resolved_target.hex(),  # backward compatibility
        })


def _notify_gateway_decision(
    gateway_url: str,
    model_hash: str,
    approved: bool,
    notes: str,
    review_type: str = "model",
    hash_id_hex: str = "",
):
    """Fire-and-forget notification to gateway that a review was decided."""
    import urllib.request

    internal_key = os.getenv("INTERNAL_API_KEY", "")
    url = gateway_url.rstrip("/") + "/v1/internal/model-audit-decision"
    idempotency_key = f"decision:{review_type}:{model_hash}:{'approve' if approved else 'reject'}"
    logger_http = logging.getLogger("operator-http")
    payload = json.dumps(_json_safe({
        "review_key": model_hash,
        "model_hash": model_hash,
        "hash_id": hash_id_hex or model_hash,
        "review_type": review_type,
        "decision": "approved" if approved else "rejected",
        "notes": notes or "",
        "resolved_at": time.time(),
    }), default=str, allow_nan=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {internal_key}",
            "X-Idempotency-Key": idempotency_key,
        },
        method="POST",
    )
    max_attempts = max(1, int(os.getenv("GATEWAY_CALLBACK_MAX_ATTEMPTS", "5")))
    timeout_seconds = max(1.0, float(os.getenv("GATEWAY_CALLBACK_TIMEOUT_SECONDS", "10")))
    backoff_seconds = max(0.5, float(os.getenv("GATEWAY_CALLBACK_BACKOFF_SECONDS", "2")))
    try:
        for attempt in range(1, max_attempts + 1):
            try:
                with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:
                    logger_http.info(
                        "Posted operator decision to gateway: status=%s model_hash=%s attempt=%d",
                        getattr(resp, "status", 200),
                        model_hash[:16],
                        attempt,
                    )
                    return
            except Exception as e:
                if attempt >= max_attempts:
                    logger_http.warning(
                        "Failed to notify gateway of decision after %d attempts: %s",
                        attempt,
                        e,
                    )
                    return
                sleep_for = min(60.0, backoff_seconds * (2 ** (attempt - 1)))
                logger_http.warning(
                    "Decision callback failed (attempt %d/%d): %s; retry in %.1fs",
                    attempt,
                    max_attempts,
                    e,
                    sleep_for,
                )
                time.sleep(sleep_for)
    except Exception as e:
        logger_http.warning(f"Failed to build gateway decision notification: {e}")


def _is_loopback_addr(bind_addr: str) -> bool:
    if bind_addr == "localhost":
        return True
    try:
        return ipaddress.ip_address(bind_addr).is_loopback
    except ValueError:
        # Unparseable (hostname, empty string = all interfaces): assume exposed.
        return False


def _start_operator_http_server(validator: 'AsyncValidator'):
    """Start the embedded HTTP server for operator review endpoints."""
    global _validator_instance
    _validator_instance = validator

    port = int(os.getenv("OPERATOR_HTTP_PORT", "9090"))
    bind_addr = os.getenv("OPERATOR_HTTP_BIND", "127.0.0.1")
    # Without OPERATOR_API_KEY, operator auth is disabled entirely
    # (see OperatorHTTPHandler._check_auth). That is only acceptable on
    # loopback; refuse to expose an unauthenticated operator API.
    if not os.getenv("OPERATOR_API_KEY", "") and not _is_loopback_addr(bind_addr):
        logging.getLogger("operator-http").error(
            "OPERATOR_API_KEY must be set when OPERATOR_HTTP_BIND=%s is not a "
            "loopback address: without a key the operator endpoints are "
            "unauthenticated. Set OPERATOR_API_KEY, or set "
            "OPERATOR_HTTP_BIND=127.0.0.1 to keep the API local-only.",
            bind_addr,
        )
        raise SystemExit(1)
    server = HTTPServer((bind_addr, port), OperatorHTTPHandler)
    server_thread = threading.Thread(
        target=server.serve_forever,
        daemon=True,
        name="operator-http",
    )
    server_thread.start()
    logging.getLogger("operator-http").info(
        f"Operator review HTTP server listening on port {port}"
    )
    return server, server_thread


if __name__ == "__main__":
    setup_logging()
    logger = logging.getLogger(__name__)

    logger.info("Main is starting")
    validator = AsyncValidator()
    logger.info("Validator was created")

    # Start embedded HTTP server for operator review endpoints
    operator_server, operator_thread = _start_operator_http_server(validator)

    try:
        logger.info("Validator is starting")
        validator.start()
    except KeyboardInterrupt:
        logger.info("Shutting down...")
        operator_server.shutdown()
        validator.shutdown()
