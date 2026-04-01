import asyncio
import logging
import os
from typing import Dict, Any, Optional, List
from datetime import datetime
import subprocess
import httpx
from httpx import HTTPError

# Configure module logger
logger = logging.getLogger(__name__)
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(os.getenv("LOG_FORMAT", "%(asctime)s - %(name)s - %(levelname)s - %(message)s")))
    logger.addHandler(handler)
logger.setLevel(getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO))


class ModelClient:
    """
    Client to fetch model information from the Bitcoin Core Model API.
    Builds in-memory dictionaries of models keyed by model_hash and by model_name.

    In STANDALONE_MODE, the client skips remote sync and uses locally configured model.
    """
    def __init__(self):
        # Standalone mode - skip remote model sync (for desktop app / single worker)
        self.standalone_mode = os.getenv("STANDALONE_MODE", "false").lower() in ("true", "1", "yes")

        # API settings
        self.base_url = os.getenv("MODEL_API_URL", "http://localhost:8050")
        self.api_key = os.getenv("MODEL_API_KEY", "")
        self.require_auth = os.getenv("MODEL_REQUIRE_AUTH", "false").lower() in ("true", "1", "yes")
        
        # Retry settings
        self.retry_attempts = int(os.getenv("MODEL_RETRY_ATTEMPTS", "3"))
        self.retry_backoff = float(os.getenv("MODEL_RETRY_BACKOFF", "1.0"))
        
        # Poll interval
        self.poll_interval = float(os.getenv("MODEL_POLL_INTERVAL", "300"))  # 5 minutes default
        
        # Build headers
        self.headers: Dict[str, str] = {}
        if self.require_auth and self.api_key:
            self.headers["Authorization"] = f"Bearer {self.api_key}"
        
        # Model storage.
        # models_by_name is multi-valued: same model name can have several
        # commits live on chain simultaneously. Auto-select must refuse to
        # pick under ambiguity so the proof's model_identifier always matches
        # the locally-served weights.
        self.models_by_hash: Dict[str, Any] = {}
        self.models_by_name: Dict[str, List[Any]] = {}
        self.last_update_time: Optional[float] = None
        self.last_update_timestamp: Optional[str] = None
        
        # State tracking
        self._initialized = False
        self._update_task: Optional[asyncio.Task] = None
        # Defer creating Event until inside an async loop to avoid 3.8 loop errors in sync tests
        self._startup_event: Optional[asyncio.Event] = None
        
        # Log configuration (avoid leaking secrets)
        logger.info(
            f"[ModelClient] Initialized with config: "
            f"base_url={self.base_url}, "
            f"auth_required={self.require_auth}, "
            f"poll_interval={self.poll_interval}s"
        )

    async def start(self):
        """Start the model client and periodic updates"""
        if self._update_task is not None:
            logger.warning("[ModelClient] Already started, ignoring duplicate start call")
            return

        # In standalone mode, skip remote sync entirely
        if self.standalone_mode:
            logger.info("[ModelClient] STANDALONE_MODE enabled - skipping remote model sync")
            self._initialized = True
            # Register local model if specified
            local_model = os.getenv("LOCAL_MODEL_NAME", "")
            if local_model:
                self.models_by_name.setdefault(local_model, []).append({
                    "model_name": local_model,
                    "model_hash": "local",
                    "local": True
                })
                logger.info(f"[ModelClient] Registered local model: {local_model}")
            return

        logger.info("[ModelClient] Starting periodic model updates...")
        # Ensure startup event exists under a running loop
        if self._startup_event is None:
            self._startup_event = asyncio.Event()
        self._update_task = asyncio.create_task(self._periodic_update_loop())

        # Wait for first successful update or timeout
        try:
            await asyncio.wait_for(self._startup_event.wait(), timeout=30.0)
            logger.info("[ModelClient] Successfully completed initial model fetch")
        except asyncio.TimeoutError:
            logger.error("[ModelClient] Initial model fetch timed out after 30s")
            raise RuntimeError("Failed to initialize model client - timeout")

    async def stop(self):
        """Stop periodic updates gracefully"""
        if self._update_task:
            logger.info("[ModelClient] Stopping periodic updates...")
            self._update_task.cancel()
            try:
                await self._update_task
            except asyncio.CancelledError:
                pass
            self._update_task = None
            logger.info("[ModelClient] Stopped")

    async def fetch_models(self, extended: bool = True) -> List[Dict[str, Any]]:
        """
        Fetch the list of models. Retries on transient errors.
        """
        url = f"{self.base_url}/api/v1/models"
        params = {"extended": str(extended).lower()}
        
        for attempt in range(1, self.retry_attempts + 1):
            try:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    logger.debug(f"[ModelClient] Fetching models (attempt {attempt}/{self.retry_attempts})")
                    response = await client.get(url, params=params, headers=self.headers)
                    response.raise_for_status()
                    models = response.json()
                    logger.debug(f"[ModelClient] Successfully fetched {len(models)} models")
                    return models
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 403:
                    logger.error(f"[ModelClient] Authentication failed: {exc}")
                    raise
                logger.warning(f"[ModelClient] HTTP error on attempt {attempt}/{self.retry_attempts}: "
                             f"status={exc.response.status_code}, msg={exc}")
            except (httpx.TimeoutException, HTTPError) as exc:
                logger.warning(f"[ModelClient] Network error on attempt {attempt}/{self.retry_attempts}: {exc}")
            except Exception as exc:
                logger.error(f"[ModelClient] Unexpected error on attempt {attempt}/{self.retry_attempts}: {exc}")
            
            if attempt < self.retry_attempts:
                wait_time = self.retry_backoff * attempt
                logger.debug(f"[ModelClient] Waiting {wait_time}s before retry...")
                await asyncio.sleep(wait_time)
        
        logger.error("[ModelClient] Max retry attempts reached. Unable to fetch model list.")
        return []

    async def update_models(self):
        """
        Fetch models from MODEL_API_URL and apply them.

        Standalone / local-miner mode path: the worker has direct
        Core Node access and polls the REST endpoint itself.

        Broker-mining mode uses :meth:`update_from_payload` instead —
        the broker fetches once and pushes the same JSON over the WS.
        """
        try:
            model_list = await self.fetch_models(extended=True)
            await self.update_from_payload(model_list, source="local-fetch")
        except Exception as e:
            logger.exception(f"[ModelClient] Error updating models: {e}")
            if not self._initialized:
                logger.error("[ModelClient] Failed to complete initial model sync")

    async def update_from_payload(
        self, model_list, *, source: str = "broker-push",
    ):
        """Apply an externally-supplied model list (same shape as
        ``GET /api/v1/models?extended=true``). Used by:

          - :meth:`update_models` after a local HTTP fetch
            (``source="local-fetch"``).
          - :class:`worker_client.WorkerClient` on receipt of a
            ``MODEL_REGISTRY_SYNC`` from the broker
            (``source="broker-push"``).

        Same filter (``status == 2``), same indexing, same startup
        signalling — the only difference vs the old in-line code is
        the data source. Broker-pushed payloads go through the
        identical code path so a future schema tightening lands in
        one place.
        """
        if not model_list:
            if not self._initialized:
                logger.error(
                    "[ModelClient] No models in payload (source=%s) on initial update",
                    source,
                )
            else:
                logger.warning(
                    "[ModelClient] No models in payload (source=%s), keeping existing data",
                    source,
                )
            return

        if not self._initialized:
            logger.info(
                "[ModelClient] Initial model load starting (source=%s, dictionaries currently empty)",
                source,
            )
        else:
            logger.debug(
                "[ModelClient] Updating models (source=%s, current count: %d)",
                source, len(self.models_by_hash),
            )

        old_count = len(self.models_by_hash)
        self.models_by_hash.clear()
        self.models_by_name.clear()

        skipped = 0
        for model in model_list:
            status = model.get("status")
            if status is not None and status != 2:
                skipped += 1
                continue
            self.models_by_hash[model["model_hash"]] = model
            self.models_by_name.setdefault(model["model_name"], []).append(model)
        if skipped:
            logger.info(
                "[ModelClient] Skipped %d model(s) with non-active status", skipped,
            )

        self.last_update_time = asyncio.get_event_loop().time()
        self.last_update_timestamp = datetime.now().isoformat()

        logger.info(
            "[ModelClient] Model update complete (source=%s): %d models loaded (previous: %d)",
            source, len(self.models_by_hash), old_count,
        )

        if self.models_by_name:
            sample_size = min(5, len(self.models_by_name))
            sample_models = list(self.models_by_name.keys())[:sample_size]
            logger.info(
                "[ModelClient] Sample models available: %s%s",
                sample_models,
                "..." if len(self.models_by_name) > sample_size else "",
            )

        if not self._initialized:
            self._initialized = True
            if self._startup_event is not None:
                self._startup_event.set()
            logger.info(
                "[ModelClient] Initial model sync completed successfully (source=%s)",
                source,
            )

    async def _periodic_update_loop(self):
        """
        Periodically fetch and update model dictionaries with robust error handling.
        """
        logger.info(f"[ModelClient] Starting periodic update loop (interval: {self.poll_interval}s)")
        
        try:
            # Initial update
            await self.update_models()
            
            # Periodic updates
            while True:
                await asyncio.sleep(self.poll_interval)
                logger.debug("[ModelClient] Running scheduled model update...")
                await self.update_models()
                
        except asyncio.CancelledError:
            logger.info("[ModelClient] Periodic update loop cancelled")
            raise
        except Exception as e:
            logger.exception(f"[ModelClient] Fatal error in periodic update loop: {e}")
            raise

    def get_model_by_hash(self, model_hash: str) -> Optional[Dict[str, Any]]:
        """Get a model by its hash"""
        model = self.models_by_hash.get(model_hash)
        if not model and not self._initialized:
            logger.warning(f"[ModelClient] Model lookup attempted before initialization (hash: {model_hash[:8]}...)")
        return model
    
    def get_model_by_name(self, model_name: str) -> Optional[Dict[str, Any]]:
        """Get a model by name. Raises if multiple commits share this name —
        callers must use get_model_by_name_and_commit to disambiguate."""
        records = self.models_by_name.get(model_name) or []
        if not records:
            if not self._initialized:
                logger.warning(f"[ModelClient] Model lookup attempted before initialization (name: {model_name})")
            return None
        if len(records) > 1:
            commits = sorted(
                ((r.get("model_commit") or "")[:12]) for r in records if r.get("model_commit")
            )
            raise ValueError(
                f"model name '{model_name}' is ambiguous on chain "
                f"({len(records)} commits: {commits}); pin MODEL_COMMIT or call get_model_by_name_and_commit"
            )
        return records[0]

    def get_models_by_name(self, model_name: str) -> List[Dict[str, Any]]:
        """Get all chain records for a model name. Empty list if unknown."""
        return list(self.models_by_name.get(model_name) or [])

    def get_model_by_name_and_commit(self, model_name: str, model_commit: str) -> Optional[Dict[str, Any]]:
        """Get a model by exact (name, commit) pair."""
        model_name = (model_name or "").strip()
        model_commit = (model_commit or "").strip()
        if not model_name or not model_commit:
            return None

        for record in self.models_by_name.get(model_name) or []:
            if (record.get("model_commit") or "").strip() == model_commit:
                return record

        # Fallback scan across hash-indexed models in case name was not indexed
        # (e.g. local sentinel registered before chain sync).
        for model in self.models_by_hash.values():
            if (model.get("model_name") or "").strip() != model_name:
                continue
            if (model.get("model_commit") or "").strip() == model_commit:
                return model
        return None

    def get_status(self) -> Dict[str, Any]:
        """Get detailed status of the model client"""
        return {
            "initialized": self._initialized,
            "models_loaded": len(self.models_by_hash),
            "last_update_timestamp": self.last_update_timestamp,
            "update_task_running": self._update_task is not None and not self._update_task.done(),
            "base_url": self.base_url,
            "poll_interval": self.poll_interval
        }
