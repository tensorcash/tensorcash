"""
Request Priority Manager for Mining Proxy

Manages priority between external and dummy requests to ensure efficient GPU utilization
while giving priority to real external requests.
"""

import asyncio
import time
import logging
from typing import Dict, List, Optional, Set, Tuple
from dataclasses import dataclass, field
from enum import Enum
import uuid

logger = logging.getLogger(__name__)


class RequestType(Enum):
    """Request type enumeration"""
    EXTERNAL = "external"
    DUMMY = "dummy"


@dataclass
class RequestInfo:
    """Information about an active request"""
    request_id: str
    request_type: RequestType
    start_time: float
    task: Optional[asyncio.Task] = None
    can_abort: bool = True
    batch_position: Optional[int] = None  # Position in batch for dummy requests
    
    @property
    def age_seconds(self) -> float:
        """Get request age in seconds"""
        return time.time() - self.start_time
    
    @property
    def is_stale(self) -> bool:
        """Check if request is stale (>5 minutes)"""
        return self.age_seconds > 300


class RequestPriorityManager:
    """
    Manages request priorities and enables aborting dummy requests for incoming external ones.
    
    Key features:
    - Tracks all active requests (external and dummy)
    - Aborts dummy requests when external requests arrive and capacity is full
    - Maintains minimum GPU concurrency
    - Handles batch-aware abortion (aborts individual requests in batch, not entire batch)
    """
    
    def __init__(self, 
                 min_concurrent: int = 4,
                 max_concurrent: int = 8,
                 batch_size: int = 20):
        """
        Initialize the priority manager.
        
        Args:
            min_concurrent: Minimum concurrent requests to maintain
            max_concurrent: Maximum concurrent requests allowed
            batch_size: Size of dummy request batches
        """
        self.min_concurrent = min_concurrent
        self.max_concurrent = max_concurrent
        self.batch_size = batch_size
        
        # Track active requests
        self._active_requests: Dict[str, RequestInfo] = {}
        self._lock = asyncio.Lock()
        
        # Statistics
        self.stats = {
            "total_external": 0,
            "total_dummy": 0,
            "total_aborted": 0,
            "current_external": 0,
            "current_dummy": 0
        }
        
        logger.info(f"RequestPriorityManager initialized: min={min_concurrent}, "
                   f"max={max_concurrent}, batch={batch_size}")
    
    async def register_external_request(self, request_id: str = None) -> Tuple[str, bool]:
        """
        Register an external request and potentially abort a dummy if needed.
        
        Returns:
            Tuple of (request_id, should_proceed)
            should_proceed is False if we're at max capacity and can't abort any dummies
        """
        if not request_id:
            request_id = f"ext-{uuid.uuid4().hex[:8]}"
        
        async with self._lock:
            # External traffic always takes precedence over dummy mining.
            # If a dummy exists, replace it immediately instead of letting
            # total concurrency grow beyond the warm pool.
            aborted = await self._abort_dummy_for_external()
            if not aborted and len(self._active_requests) >= self.max_concurrent:
                logger.warning(f"Cannot register external request {request_id}: "
                             f"at max capacity with no abortable dummies")
                return request_id, False
            
            # Register the external request
            self._active_requests[request_id] = RequestInfo(
                request_id=request_id,
                request_type=RequestType.EXTERNAL,
                start_time=time.time(),
                can_abort=False  # External requests cannot be aborted
            )
            
            self.stats["total_external"] += 1
            self.stats["current_external"] += 1
            
            logger.info(f"Registered external request {request_id}. "
                       f"Active: {len(self._active_requests)} "
                       f"(ext={self.stats['current_external']}, "
                       f"dummy={self.stats['current_dummy']})")
            
            return request_id, True
    
    async def register_dummy_request(self, 
                                    request_id: str = None,
                                    batch_position: Optional[int] = None) -> str:
        """
        Register a dummy request.
        
        Args:
            request_id: Optional request ID
            batch_position: Position in batch (for batch-aware abortion)
        
        Returns:
            The request ID
        """
        if not request_id:
            request_id = f"dummy-{uuid.uuid4().hex[:8]}"
        
        async with self._lock:
            self._active_requests[request_id] = RequestInfo(
                request_id=request_id,
                request_type=RequestType.DUMMY,
                start_time=time.time(),
                can_abort=True,
                batch_position=batch_position
            )
            
            self.stats["total_dummy"] += 1
            self.stats["current_dummy"] += 1
            
            logger.debug(f"Registered dummy request {request_id} "
                        f"(batch_pos={batch_position})")
            
            return request_id
    
    async def unregister_request(self, request_id: str):
        """Unregister a completed request"""
        async with self._lock:
            if request_id in self._active_requests:
                req_info = self._active_requests[request_id]
                
                # Update statistics
                if req_info.request_type == RequestType.EXTERNAL:
                    self.stats["current_external"] -= 1
                else:
                    self.stats["current_dummy"] -= 1
                
                del self._active_requests[request_id]
                
                logger.debug(f"Unregistered {req_info.request_type.value} "
                           f"request {request_id}")
    
    async def _abort_dummy_for_external(self) -> bool:
        """
        Abort a dummy request to make room for an external request.
        
        Selection strategy:
        1. Prefer aborting newer dummy requests (LIFO)
        2. Prefer aborting requests at the end of batches
        3. External traffic replaces dummy traffic immediately
        
        Returns:
            True if a dummy was aborted, False otherwise
        """
        # Find abortable dummy requests
        dummy_requests = [
            (rid, info) for rid, info in self._active_requests.items()
            if info.request_type == RequestType.DUMMY and info.can_abort
        ]
        
        if not dummy_requests:
            return False
        
        # Sort by priority for abortion (newer first, higher batch position first)
        dummy_requests.sort(
            key=lambda x: (
                -x[1].start_time,  # Newer first (negative for reverse sort)
                -(x[1].batch_position or 0)  # Higher batch position first
            )
        )
        
        # Abort the first candidate
        request_id, req_info = dummy_requests[0]
        
        # If the request has an associated task, cancel it
        if req_info.task and not req_info.task.done():
            req_info.task.cancel()
            logger.info(f"Cancelled dummy request task {request_id}")
        
        # Remove from active requests
        del self._active_requests[request_id]
        self.stats["current_dummy"] -= 1
        self.stats["total_aborted"] += 1
        
        logger.info(f"Aborted dummy request {request_id} (age={req_info.age_seconds:.1f}s, "
                   f"batch_pos={req_info.batch_position}) to make room for external request")
        
        return True
    
    async def attach_task(self, request_id: str, task: asyncio.Task):
        """Attach an asyncio task to a request for cancellation support"""
        async with self._lock:
            if request_id in self._active_requests:
                self._active_requests[request_id].task = task
    
    async def get_active_count(self) -> Dict[str, int]:
        """Get count of active requests by type"""
        async with self._lock:
            return {
                "total": len(self._active_requests),
                "external": self.stats["current_external"],
                "dummy": self.stats["current_dummy"]
            }
    
    async def should_generate_dummy(self) -> bool:
        """
        Determine if a new dummy request should be generated.
        
        Returns:
            True if we're below minimum concurrency and should generate a dummy
        """
        async with self._lock:
            current_total = len(self._active_requests)
            return current_total < self.min_concurrent
    
    async def cleanup_stale_requests(self, max_age_seconds: float = 300):
        """Remove stale requests from tracking"""
        async with self._lock:
            stale_ids = [
                rid for rid, info in self._active_requests.items()
                if info.age_seconds > max_age_seconds
            ]
            
            for rid in stale_ids:
                req_info = self._active_requests[rid]
                
                # Cancel associated task if exists
                if req_info.task and not req_info.task.done():
                    req_info.task.cancel()
                
                # Update stats
                if req_info.request_type == RequestType.EXTERNAL:
                    self.stats["current_external"] -= 1
                else:
                    self.stats["current_dummy"] -= 1
                
                del self._active_requests[rid]
                logger.warning(f"Cleaned up stale {req_info.request_type.value} "
                             f"request {rid} (age={req_info.age_seconds:.1f}s)")
            
            return len(stale_ids)
    
    def get_statistics(self) -> Dict:
        """Get current statistics"""
        return {
            **self.stats,
            "active_requests": len(self._active_requests),
            "capacity_used": len(self._active_requests) / self.max_concurrent,
            "can_accept_external": len(self._active_requests) < self.max_concurrent
        }
    
    async def get_request_info(self, request_id: str) -> Optional[RequestInfo]:
        """Get information about a specific request"""
        async with self._lock:
            return self._active_requests.get(request_id)
    
    async def list_active_requests(self) -> List[Dict]:
        """List all active requests with their details"""
        async with self._lock:
            return [
                {
                    "request_id": info.request_id,
                    "type": info.request_type.value,
                    "age_seconds": info.age_seconds,
                    "can_abort": info.can_abort,
                    "batch_position": info.batch_position,
                    "is_stale": info.is_stale
                }
                for info in self._active_requests.values()
            ]
