"""
Enhanced HTTP proxy with request priority management

This module extends the existing proxy.py to integrate priority-based request handling,
allowing external requests to preempt dummy requests when GPU capacity is limited.
"""

import asyncio
import json
import uuid
import time
import logging
from typing import Dict, Optional, List
import struct

import aiohttp
from aiohttp import web

from components.proxy import RequestManager
from components.request_priority_manager import RequestPriorityManager, RequestType
from components.context import LockFreeContext
from components import constants

logger = logging.getLogger(__name__)


class PriorityRequestManager(RequestManager):
    """
    Enhanced RequestManager with priority-based request handling.
    
    Extends the base RequestManager to support:
    - Aborting dummy requests when external requests arrive
    - Batch-aware dummy request generation
    - Priority-based capacity management
    """
    
    def __init__(self, context: LockFreeContext):
        """Initialize with priority management"""
        super().__init__(context)
        
        # Initialize priority manager
        self.priority_manager = RequestPriorityManager(
            min_concurrent=self.min_active,
            # Headroom above the warm-dummy floor, but never 0: with
            # MIN_ACTIVE_REQUESTS=0 (no warm dummies) min_active*2 collapses to
            # zero capacity, which 503s every external request and makes
            # get_statistics() divide by zero. Capacity must hold at least one
            # batch regardless of the dummy minimum.
            max_concurrent=max(self.min_active * 2, constants.BATCH_SIZE),
            batch_size=constants.BATCH_SIZE
        )
        
        # Track batch generation tasks
        self._batch_tasks: Dict[str, asyncio.Task] = {}
        self._last_tip_block_hash: Optional[str] = None
        # req_ids the broker cancelled via MINE_CANCEL (superseded).
        # Gates dummy GENERATION: cancelling in-flight dummies is not
        # enough — the local context still holds the dead parent until
        # the next MINE_REQUEST refreshes it, so an ungated monitor
        # loop would immediately re-mint dummies against the same
        # cancelled req_id. Keyed req_id -> cancel time, bounded in
        # cancel_dummy_requests_for_req_id.
        self._broker_cancelled_req_ids: Dict[int, float] = {}
        self._dummy_poll_interval_seconds = 0.5
        # Single in-flight dummy batch generator (see monitor loop).
        self._dummy_batch_task: Optional[asyncio.Task] = None
        
        logger.info("[PriorityRequestManager] Initialized with priority management")

    def _use_background_dummy_responses(self) -> bool:
        # Genesis grind must drive via /v1/completions: the PoW payload is
        # injected as vllm_xargs, which vLLM threads to the sampler on the
        # completions path. The background Responses API path does not forward
        # vllm_xargs, so generation runs with NO PoW and emits no proofs.
        if constants.GENESIS_GENERATOR:
            return False
        # llama.cpp currently exposes POST /v1/responses but not the async
        # retrieve/cancel lifecycle used by the warm dummy pool.
        return not constants.LLAMA_CPP
    
    async def start(self):
        """Start request manager with priority support"""
        await super().start()
        
        # Start cleanup task for priority manager
        asyncio.create_task(self._priority_cleanup_loop())
        
        logger.info("[PriorityRequestManager] Started with priority management enabled")
    
    def _dummy_backend_url(self) -> str:
        """Backend base URL for dummy mining requests.

        Dummies always target the chain-pinned mining model, which on a
        dual-backend worker (MODEL_ROUTES) lives on the mining vLLM (e.g.
        :8001) — NOT the default TARGET_URL (the inference model, e.g. the
        27B on :8000), which would 404 on the mining model. In
        single-backend mode this resolves to _base_url unchanged.
        """
        return self._backend_base_url(self._active_model_name)

    async def _dummy_backend_healthy(self) -> bool:
        """Cached /health check for the dummy (mining) backend.

        Gates dummy generation so a loading/crashed/down mining vLLM
        can't trigger a retry storm that saturates the proxy. Cached for
        ~3s so a full dummy batch costs at most one health probe.
        """
        now = time.time()
        cached = getattr(self, "_dummy_health_cache", None)
        if cached is not None and (now - cached[0]) < 3.0:
            return cached[1]
        healthy = False
        try:
            async with self.session.get(
                f"{self._dummy_backend_url()}/health",
                timeout=aiohttp.ClientTimeout(total=2),
            ) as resp:
                healthy = resp.status == 200
        except Exception:
            healthy = False
        self._dummy_health_cache = (now, healthy)
        return healthy

    async def _handle_completion_request(self, request: web.Request) -> web.Response:
        """
        Override to add priority management for external requests.
        """
        # Register as external request with priority manager
        request_id, should_proceed = await self.priority_manager.register_external_request()
        
        if not should_proceed:
            # We're at capacity and couldn't make room
            return web.Response(
                text='{"error": "Service at capacity, please retry"}',
                status=503,
                content_type='application/json'
            )
        
        # Track in both systems for compatibility
        self.active_requests[request_id] = time.time()
        
        logger.info(f"[PriorityRequestManager] External request {request_id} started")
        
        try:
            # Process the request as normal
            response = await super()._handle_completion_request(request)
            return response
            
        finally:
            # Clean up from both tracking systems
            self.active_requests.pop(request_id, None)
            await self.priority_manager.unregister_request(request_id)
            
            logger.info(f"[PriorityRequestManager] External request {request_id} completed")

    async def _handle_responses_request(self, request: web.Request) -> web.Response:
        """Treat POST /v1/responses as an external request for priority purposes."""
        request_id, should_proceed = await self.priority_manager.register_external_request()

        if not should_proceed:
            return web.Response(
                text='{"error": "Service at capacity, please retry"}',
                status=503,
                content_type='application/json'
            )

        self.active_requests[request_id] = time.time()
        logger.info(f"[PriorityRequestManager] External responses request {request_id} started")

        try:
            response = await super()._handle_responses_request(request)
            return response
        finally:
            self.active_requests.pop(request_id, None)
            await self.priority_manager.unregister_request(request_id)
            logger.info(f"[PriorityRequestManager] External responses request {request_id} completed")
    
    async def _generate_dummy_batch(self):
        """
        Generate a batch of dummy requests with individual abortion support.
        
        Instead of sending one request with batch_size prompts, we send
        individual requests that can be aborted independently.
        """
        batch_id = f"batch-{uuid.uuid4().hex[:8]}"
        batch_tasks = []
        
        logger.info(f"[PriorityRequestManager] Generating dummy batch {batch_id} "
                   f"with {constants.BATCH_SIZE} requests")
        
        for i in range(constants.BATCH_SIZE):
            # Check if we should still generate this dummy
            if not await self.priority_manager.should_generate_dummy():
                logger.debug(f"Stopping batch generation at position {i}: "
                           "minimum concurrency reached")
                break
            
            # Create individual dummy request task
            task = asyncio.create_task(
                self._generate_single_dummy(batch_position=i)
            )
            batch_tasks.append(task)
            
            # Small delay between requests to avoid overwhelming the system
            await asyncio.sleep(0.1)
        
        # Track batch task
        if batch_tasks:
            batch_task = asyncio.gather(*batch_tasks, return_exceptions=True)
            self._batch_tasks[batch_id] = batch_task
            
            try:
                results = await batch_task
                successful = sum(1 for r in results if not isinstance(r, Exception))
                logger.info(f"[PriorityRequestManager] Batch {batch_id} completed: "
                          f"{successful}/{len(batch_tasks)} successful")
            finally:
                self._batch_tasks.pop(batch_id, None)
    
    async def _generate_single_dummy(self, batch_position: Optional[int] = None):
        """
        Generate a single dummy request with priority tracking.
        
        Args:
            batch_position: Position in batch for priority-based abortion
        """
        snapshot = self.context.read()
        if snapshot.request_id in self._broker_cancelled_req_ids:
            # Broker cancelled this req_id (MINE_CANCEL, dead parent) and
            # the context hasn't been re-anchored by a new MINE_REQUEST
            # yet — minting more work against it is guaranteed waste.
            logger.debug(
                "[PriorityRequestManager] Skipping dummy generation: "
                "req_id=%d cancelled by broker", snapshot.request_id,
            )
            return
        dummy_id = (
            f"resp_dummy_{snapshot.block_hash[:8]}_"
            f"{snapshot.request_id}_{uuid.uuid4().hex[:8]}"
        )

        # Register with priority manager
        dummy_id = await self.priority_manager.register_dummy_request(
            request_id=dummy_id,
            batch_position=batch_position
        )
        
        # Track in legacy system for compatibility
        self.active_requests[dummy_id] = time.time()
        self._register_dummy_task(dummy_id)
        
        try:
            if self._is_mining_paused():
                logger.info(
                    "[PriorityRequestManager] Skipping dummy generation: %s",
                    self._mining_cooldown_error(),
                )
                return

            # Robustness gate: skip dummy generation while the mining
            # backend is unhealthy (loading at boot, crashed, or down).
            # Otherwise failing dummies retry-storm the proxy event loop,
            # which starves real inference and the worker's HELLO
            # /v1/models introspection — dropping the inference model from
            # broker registration. The check is cached (~3s) so it adds
            # negligible overhead.
            if not await self._dummy_backend_healthy():
                logger.debug(
                    "[PriorityRequestManager] Skipping dummy: mining backend not healthy"
                )
                return

            try:
                model_name = self._select_dummy_model_name()
            except Exception as e:
                logger.error("[PriorityRequestManager] %s", e)
                return
            
            # Generate single prompt
            prompt = self.prompt_generator.generate_prompt()
            if constants.GENESIS_GENERATOR:
                prompt = self.genesis_generator.generate(1)[0]

            # Close Qwen3's default <think> prelude on the ChatML dummy path so the
            # generation clears the consensus entropy gate (avg CDF-upper < 0.925).
            # Verifier-safe: it replays stored prompt_tokens, never re-renders.
            if constants.MINING_DISABLE_THINKING and not constants.GENESIS_GENERATOR:
                prompt = f"{prompt} {constants.MINING_NO_THINK_DIRECTIVE}"

            if self._use_background_dummy_responses():
                dummy_data = {
                    "model": model_name,
                    "input": prompt,
                    "max_output_tokens": 256,
                    "background": True,
                    "store": True,
                    "request_id": dummy_id,
                    "temperature": 1.0,
                    "top_k": 50,
                    "top_p": 1.0,
                }
            else:
                dummy_data = {
                    "model": model_name,
                    "prompt": prompt,
                    "max_tokens": 256,
                    "temperature": 1.0,
                    "top_k": 50,
                    "top_p": 1.0,
                    "request_id": dummy_id,
                    "ignore_eos": True,
                }
            
            modified_data = self._inject_pow_data(dummy_data)
            
            # Create the request task
            if self._use_background_dummy_responses():
                request_task = asyncio.create_task(
                    self._execute_dummy_response(dummy_id, modified_data)
                )
            else:
                request_task = asyncio.create_task(
                    self._execute_dummy_completion(dummy_id, modified_data)
                )
            
            # Attach task to priority manager for cancellation support
            await self.priority_manager.attach_task(dummy_id, request_task)

            # The dummy may have been preempted before its task was attached.
            if await self.priority_manager.get_request_info(dummy_id) is None:
                request_task.cancel()
                await asyncio.gather(request_task, return_exceptions=True)
                return
            
            # Wait for completion
            await request_task
            
        except asyncio.CancelledError:
            logger.info(f"[PriorityRequestManager] Dummy {dummy_id} was cancelled")
            raise
            
        except Exception as e:
            logger.error(f"[PriorityRequestManager] Dummy {dummy_id} failed: {e}")
            
        finally:
            # Clean up from both tracking systems
            self._unregister_dummy_task(dummy_id)
            self.active_requests.pop(dummy_id, None)
            await self.priority_manager.unregister_request(dummy_id)
    
    async def _execute_dummy_response(self, dummy_id: str, data: dict):
        """Execute a cancellable dummy mining request via the Responses API."""
        for attempt in range(1, constants.DUMMY_RETRY_ATTEMPTS + 1):
            try:
                async with self.session.post(
                    f"{self._dummy_backend_url()}/v1/responses",
                    json=data,
                    headers=self.auth_headers,
                    timeout=aiohttp.ClientTimeout(total=constants.DUMMY_REQUEST_TIMEOUT)
                ) as resp:
                    body = await resp.read()
                if resp.status >= 400:
                    raise RuntimeError(
                        f"upstream create failed: status={resp.status}, body={body[:200]!r}"
                    )

                logger.debug(
                    f"[PriorityRequestManager] Dummy {dummy_id} "
                    f"attempt {attempt} accepted by /v1/responses"
                )
                await self._wait_for_dummy_response(dummy_id)
                return
                
            except asyncio.CancelledError:
                await self._cancel_upstream_response(dummy_id, reason="task_cancelled")
                logger.info(
                    f"[PriorityRequestManager] Dummy {dummy_id} "
                    f"cancelled during attempt {attempt}"
                )
                raise
                
            except Exception as e:
                if attempt == constants.DUMMY_RETRY_ATTEMPTS:
                    logger.error(f"[PriorityRequestManager] Dummy {dummy_id} "
                               f"failed after {attempt} attempts: {e}")
                else:
                    delay = constants.DUMMY_RETRY_BACKOFF * (2 ** (attempt - 1))
                    logger.warning(f"[PriorityRequestManager] Dummy {dummy_id} "
                                 f"attempt {attempt} failed, retrying in {delay}s: {e}")
                    
                    # Use asyncio.sleep with cancellation check
                    try:
                        await asyncio.sleep(delay)
                    except asyncio.CancelledError:
                        await self._cancel_upstream_response(
                            dummy_id,
                            reason="task_cancelled_during_backoff",
                        )
                        logger.info(f"[PriorityRequestManager] Dummy {dummy_id} "
                                  f"cancelled during backoff")
                        raise

    async def _execute_dummy_completion(self, dummy_id: str, data: dict):
        """Fallback path for backends without async Responses lifecycle support."""
        for attempt in range(1, constants.DUMMY_RETRY_ATTEMPTS + 1):
            try:
                async with self.session.post(
                    f"{self._dummy_backend_url()}/v1/completions",
                    json=data,
                    headers=self.auth_headers,
                    timeout=aiohttp.ClientTimeout(total=constants.DUMMY_REQUEST_TIMEOUT)
                ) as resp:
                    body = await resp.read()
                if resp.status >= 400:
                    raise RuntimeError(
                        f"upstream completion failed: status={resp.status}, body={body[:200]!r}"
                    )
                self._record_token_usage(body)
                logger.debug(
                    f"[PriorityRequestManager] Dummy {dummy_id} "
                    f"attempt {attempt} completed via /v1/completions"
                )
                return
            except asyncio.CancelledError:
                logger.info(
                    f"[PriorityRequestManager] Dummy {dummy_id} "
                    f"cancelled during completion attempt {attempt}"
                )
                raise
            except Exception as e:
                if attempt == constants.DUMMY_RETRY_ATTEMPTS:
                    logger.error(
                        f"[PriorityRequestManager] Dummy {dummy_id} "
                        f"failed after {attempt} completion attempts: {e}"
                    )
                else:
                    delay = constants.DUMMY_RETRY_BACKOFF * (2 ** (attempt - 1))
                    logger.warning(
                        f"[PriorityRequestManager] Dummy {dummy_id} "
                        f"completion attempt {attempt} failed, retrying in {delay}s: {e}"
                    )
                    try:
                        await asyncio.sleep(delay)
                    except asyncio.CancelledError:
                        logger.info(
                            f"[PriorityRequestManager] Dummy {dummy_id} "
                            f"cancelled during completion backoff"
                        )
                        raise

    async def _wait_for_dummy_response(self, response_id: str) -> None:
        """Poll a background response until it reaches a terminal state."""
        terminal_statuses = {"completed", "cancelled", "incomplete", "failed"}

        while True:
            try:
                async with self.session.get(
                    f"{self._dummy_backend_url()}/v1/responses/{response_id}",
                    headers=self.auth_headers,
                    timeout=aiohttp.ClientTimeout(total=constants.DUMMY_REQUEST_TIMEOUT)
                ) as resp:
                    body = await resp.read()
            except asyncio.CancelledError:
                await self._cancel_upstream_response(
                    response_id,
                    reason="task_cancelled_while_polling",
                )
                raise

            if resp.status == 404:
                await asyncio.sleep(self._dummy_poll_interval_seconds)
                continue

            if resp.status >= 400:
                raise RuntimeError(
                    f"upstream poll failed for {response_id}: "
                    f"status={resp.status}, body={body[:200]!r}"
                )

            try:
                payload = json.loads(body)
            except json.JSONDecodeError as exc:
                raise RuntimeError(
                    f"invalid JSON while polling {response_id}: {body[:200]!r}"
                ) from exc

            status = payload.get("status")
            if status in terminal_statuses:
                if status == "completed":
                    self._record_token_usage(body)
                logger.debug(
                    "[PriorityRequestManager] Dummy %s reached terminal status=%s",
                    response_id,
                    status,
                )
                return

            await asyncio.sleep(self._dummy_poll_interval_seconds)

    async def _cancel_upstream_response(self, response_id: str, reason: str) -> None:
        """Best-effort cancel of an in-flight background response."""
        try:
            async with self.session.post(
                f"{self._dummy_backend_url()}/v1/responses/{response_id}/cancel",
                headers=self.auth_headers,
                timeout=aiohttp.ClientTimeout(total=constants.DUMMY_REQUEST_TIMEOUT)
            ) as resp:
                body = await resp.read()

            if resp.status >= 400 and resp.status not in (404, 409):
                logger.warning(
                    "[PriorityRequestManager] Cancel %s for %s returned status=%s body=%r",
                    reason,
                    response_id,
                    resp.status,
                    body[:200],
                )
                return

            logger.info(
                "[PriorityRequestManager] Cancelled upstream dummy %s (%s)",
                response_id,
                reason,
            )
        except Exception as exc:
            logger.warning(
                "[PriorityRequestManager] Failed to cancel upstream dummy %s (%s): %s",
                response_id,
                reason,
                exc,
            )

    async def _cancel_all_dummy_requests(self, reason: str) -> None:
        """Cancel all tracked dummy requests and wait for cleanup."""
        if not self._dummy_tasks:
            return

        logger.info(
            "[PriorityRequestManager] Cancelling %d dummy request(s): %s",
            len(self._dummy_tasks),
            reason,
        )
        tasks = list(self._dummy_tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    
    @staticmethod
    def _req_id_of_dummy(dummy_id: str) -> Optional[int]:
        """Extract the mining req_id a dummy request was minted under.

        Dummy IDs are ``resp_dummy_{block_hash[:8]}_{request_id}_{uuid8}``
        (see _generate_single_dummy); the hash and uuid segments are hex
        and can never contain an underscore, so the split is stable.
        Returns None for any non-conforming id.
        """
        parts = dummy_id.split("_")
        if len(parts) != 5 or parts[0] != "resp" or parts[1] != "dummy":
            return None
        try:
            return int(parts[3])
        except ValueError:
            return None

    def _current_req_id_cancelled(self) -> bool:
        """True while the context still points at a broker-cancelled
        req_id — i.e. between a MINE_CANCEL(superseded) and the next
        MINE_REQUEST re-anchoring the context. Dummy generation must
        pause in that window instead of re-minting dead-parent work."""
        try:
            return self.context.read().request_id in self._broker_cancelled_req_ids
        except Exception:  # noqa: BLE001
            return False

    async def cancel_dummy_requests_for_req_id(
        self, request_id: int, reason: str,
    ) -> int:
        """Targeted cancel for one broker req_id (MINE_CANCEL path).

        Cancels ONLY the dummy tasks whose ID embeds ``request_id`` —
        same-parent work under a different (still-live) req_id keeps
        running. Task cancellation triggers each dummy's own cleanup
        path, which best-effort-cancels the upstream /v1/responses
        generation; completions-path dummies only stop locally (the
        backend finishes the generation, but its proofs are dropped by
        the worker-client tombstone). Also gates future generation for
        this req_id until the context moves on. Returns the number of
        tasks cancelled.
        """
        self._broker_cancelled_req_ids[request_id] = time.time()
        while len(self._broker_cancelled_req_ids) > 64:
            self._broker_cancelled_req_ids.pop(
                next(iter(self._broker_cancelled_req_ids))
            )

        targets = [
            (dummy_id, task)
            for dummy_id, task in list(self._dummy_tasks.items())
            if self._req_id_of_dummy(dummy_id) == request_id
        ]
        if not targets:
            logger.info(
                "[PriorityRequestManager] MINE_CANCEL req_id=%d: no in-flight "
                "dummy tasks matched (%d active); generation gated (%s)",
                request_id, len(self._dummy_tasks), reason,
            )
            return 0

        logger.info(
            "[PriorityRequestManager] Cancelling %d dummy request(s) for "
            "req_id=%d: %s",
            len(targets), request_id, reason,
        )
        for _, task in targets:
            task.cancel()
        await asyncio.gather(
            *(task for _, task in targets), return_exceptions=True,
        )
        return len(targets)

    async def _monitor_loop(self):
        """
        Enhanced monitor loop with priority-aware dummy generation.
        """
        logger.info(f"[PriorityRequestManager] Monitor loop started with priority management")

        try:
            while True:
                # Apply deferred model switches in priority mode as well.
                self._apply_pending_model_switch()

                if self.context.vdf_initialised and self.context.miner_initialised:
                    snapshot = self.context.read()

                    if self._last_tip_block_hash is None:
                        self._last_tip_block_hash = snapshot.block_hash
                    elif snapshot.block_hash != self._last_tip_block_hash:
                        old_tip = self._last_tip_block_hash
                        self._last_tip_block_hash = snapshot.block_hash
                        await self._cancel_all_dummy_requests(
                            reason=(
                                f"tip_changed:{old_tip[:16]}..."
                                f"->{snapshot.block_hash[:16]}..."
                            )
                        )

                    if self._is_mining_paused():
                        await self._cancel_all_dummy_requests(
                            reason=self._mining_cooldown_error(),
                        )
                        await asyncio.sleep(self.monitor_interval)
                        continue

                    # Clean up stale requests in both systems
                    current_time = time.time()
                    stale_ids = [
                        rid for rid, start_time in self.active_requests.items()
                        if current_time - start_time > 300
                    ]
                    for rid in stale_ids:
                        self.active_requests.pop(rid, None)

                    # Also clean up in priority manager
                    await self.priority_manager.cleanup_stale_requests()

                    # Check if mining context is stale (no block from core-node recently)
                    context_status = self.context.get_status()
                    context_age = context_status.get("age_seconds", 0)
                    is_stale = context_age > constants.MINING_STALE_THRESHOLD_SECONDS

                    if is_stale:
                        logger.debug(f"[PriorityRequestManager] Mining context stale "
                                   f"(age={context_age:.1f}s > {constants.MINING_STALE_THRESHOLD_SECONDS}s), "
                                   f"skipping dummy generation")
                    elif self._current_req_id_cancelled():
                        logger.debug(
                            "[PriorityRequestManager] Dummy generation gated: "
                            "context req_id cancelled by broker; awaiting "
                            "next MINE_REQUEST"
                        )
                    elif await self.priority_manager.should_generate_dummy():
                        # Use batch generation for efficiency. One generator
                        # at a time: the monitor ticks every second, so an
                        # unguarded create_task stacks overlapping batch
                        # coroutines whenever a batch outlives the tick —
                        # needless event-loop/request pressure (the refill
                        # depth itself is governed by MIN_ACTIVE_REQUESTS).
                        if self._dummy_batch_task is None or self._dummy_batch_task.done():
                            self._dummy_batch_task = asyncio.create_task(
                                self._generate_dummy_batch()
                            )

                    # Log current status
                    counts = await self.priority_manager.get_active_count()
                    logger.debug(f"[PriorityRequestManager] Active requests: "
                               f"total={counts['total']}, "
                               f"external={counts['external']}, "
                               f"dummy={counts['dummy']}, "
                               f"context_age={context_age:.1f}s")

                await asyncio.sleep(self.monitor_interval)
                
        except asyncio.CancelledError:
            logger.info("[PriorityRequestManager] Monitor loop cancelled")
            raise
        except Exception as e:
            logger.exception(f"[PriorityRequestManager] Monitor loop error: {e}")
    
    async def _priority_cleanup_loop(self):
        """Periodic cleanup for priority manager"""
        while True:
            try:
                await asyncio.sleep(30)  # Run every 30 seconds
                cleaned = await self.priority_manager.cleanup_stale_requests()
                if cleaned > 0:
                    logger.info(f"[PriorityRequestManager] Cleaned {cleaned} stale requests")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[PriorityRequestManager] Cleanup error: {e}")
    
    def get_status(self) -> dict:
        """Get enhanced status including priority information"""
        base_status = super().get_status()
        
        # Add priority manager statistics
        priority_stats = self.priority_manager.get_statistics()
        
        base_status["priority"] = {
            "total_external": priority_stats["total_external"],
            "total_dummy": priority_stats["total_dummy"],
            "total_aborted": priority_stats["total_aborted"],
            "current_external": priority_stats["current_external"],
            "current_dummy": priority_stats["current_dummy"],
            "capacity_used": priority_stats["capacity_used"],
            "can_accept_external": priority_stats["can_accept_external"]
        }
        
        return base_status


# Export the enhanced manager
__all__ = ['PriorityRequestManager']
