"""
HTTP proxy with PoW injection for requests - Updated for robust model sync
"""
import asyncio
import uuid
import time
import logging
import threading
import os
from typing import Dict, Optional
import struct
import json
from urllib import request as urlrequest

import aiohttp
from aiohttp import web
from urllib.parse import urlparse

from components.context import LockFreeContext
from components import constants
from components.model_synch import ModelClient
from utils.uint256_arithmetics import set_compact, get_compact, adjust_nbits_by_multiplier
from components.default_prompt_generator import IntelligentPromptGenerator
from config.constants import TOPK_MIN, TOPK_MAX, TOPP_MIN, TOPP_MAX, TEMP_MIN, TEMP_MAX, DEFAULT_TOP_K, DEFAULT_TOP_P, DEFAULT_TEMP

if constants.GENESIS_GENERATOR:
    from components import genesis 
    from components.genesis import SeedPhrasePromptGenerator

logger = logging.getLogger(__name__)

class RequestManager:
    """HTTP proxy that injects PoW data into requests"""

    def __init__(self, context: LockFreeContext):
        self.context = context
        self.target_url = constants.TARGET_URL
        # Normalize base URL (without path) for upstream requests
        parsed = urlparse(self.target_url)
        if parsed.scheme and parsed.netloc:
            self._base_url = f"{parsed.scheme}://{parsed.netloc}"
            if parsed.path and parsed.path not in ("", "/"):
                logger.warning(
                    "[RequestManager] TARGET_URL contains a path (%s); using base %s",
                    parsed.path, self._base_url
                )
        else:
            logger.error("[RequestManager] TARGET_URL seems invalid: %r", self.target_url)
            self._base_url = self.target_url.rstrip('/')
        # Multi-backend routing (MODEL_ROUTES): per-model upstream base
        # URLs for proxies fronting more than one vLLM instance. Models
        # without a route use self._base_url.
        self._model_base_urls = {}
        for _route_model, _route_url in getattr(constants, "MODEL_ROUTES", {}).items():
            _parsed = urlparse(_route_url)
            if _parsed.scheme and _parsed.netloc:
                self._model_base_urls[_route_model] = f"{_parsed.scheme}://{_parsed.netloc}"
            else:
                logger.error(
                    "[RequestManager] MODEL_ROUTES url for %r seems invalid: %r",
                    _route_model, _route_url,
                )
                self._model_base_urls[_route_model] = _route_url.rstrip('/')
        if self._model_base_urls:
            logger.info("[RequestManager] Model routes: %s", self._model_base_urls)
        self.min_active = constants.MIN_ACTIVE_REQUESTS
        self.monitor_interval = constants.MONITOR_INTERVAL

        self.session = None
        self.active_requests: Dict[str, float] = {}
        self._dummy_tasks: Dict[str, asyncio.Task] = {}
        self._monitor_task = None

        # Broker mode has two distinct states:
        #   - WORKER_MODE=broker + MINING_ENABLED=false: inference-only sidecar.
        #     Forward requests untouched; no VDF/PoW/registry dependency.
        #   - WORKER_MODE=broker + MINING_ENABLED=true: broker-mining sidecar.
        #     There is still no colocated Core Node, but the broker supplies
        #     mining templates over MINE_REQUEST and the chain model registry via
        #     MODEL_REGISTRY_SYNC. This path MUST keep PoW injection live.
        #
        # STANDALONE_MODE means "no local Core Node registry fetch"; it is not a
        # mining-disable flag. In broker-mining mode we create a ModelClient but
        # skip its local startup fetch, then populate it from broker-pushed
        # registry snapshots before accepting mining work.
        _broker_mode = getattr(constants, "WORKER_MODE", "standalone") == "broker"
        _mining_enabled = bool(getattr(constants, "MINING_ENABLED", True))
        _standalone = bool(getattr(constants, "STANDALONE_MODE", False))
        self._broker_mining_mode = _broker_mode and _mining_enabled
        self._broker_registry_only = self._broker_mining_mode and _standalone
        self._broker_inference_only = _broker_mode and not _mining_enabled
        if self._broker_inference_only:
            logger.info(
                "[RequestManager] broker inference-only mode: ModelClient and "
                "PoW injection disabled (WORKER_MODE=%s, MINING_ENABLED=%s, "
                "STANDALONE_MODE=%s)",
                getattr(constants, "WORKER_MODE", "?"),
                getattr(constants, "MINING_ENABLED", "?"),
                _standalone,
            )
            self.model_client = None
        else:
            # Model client will be initialized asynchronously, or populated by
            # broker MODEL_REGISTRY_SYNC when _broker_registry_only is true.
            self.model_client = ModelClient()
        self.prompt_generator = IntelligentPromptGenerator()
        if constants.GENESIS_GENERATOR:
            self.genesis_generator = SeedPhrasePromptGenerator(genesis.SEED_PHRASE)

        self.auth_headers = {}
        key = constants.API_KEY
        self.auth_headers["Authorization"] = f"Bearer {key}"

        # Runtime-active model (can be switched without restart).
        # Empty values mean "auto-select" mode.
        self._model_selection_lock = threading.RLock()
        self._active_model_name = constants.LOCAL_MODEL_NAME
        self._active_model_commit = constants.MODEL_HASH
        self._active_model_source = "env"
        self._runtime_model_state_path = os.getenv(
            "MINER_RUNTIME_MODEL_STATE_PATH",
            "/data/miner_runtime_model_state.json",
        ).strip() or "/data/miner_runtime_model_state.json"
        self._switch_state_lock = threading.RLock()
        self._pending_model_switch = None
        self._switch_in_progress = False
        self._load_runtime_model_state()
        self.context.set_expected_model_identifier(
            self._active_model_name,
            self._active_model_commit,
        )

        # Token throughput tracking for mining stats
        self._throughput_window_seconds = 60.0  # Rolling window for throughput calculation
        self._token_events: list[tuple[float, int, int]] = []  # (timestamp, prompt_tokens, completion_tokens)
        self._total_prompt_tokens = 0
        self._total_completion_tokens = 0
        self._total_completions = 0

    def _load_runtime_model_state(self) -> None:
        """Restore runtime model selection from disk if a state file exists."""
        path = self._runtime_model_state_path
        try:
            if not os.path.exists(path):
                return
            with open(path, "r", encoding="utf-8") as f:
                state = json.load(f)
            if not isinstance(state, dict):
                logger.warning(
                    "[RequestManager] Ignoring invalid runtime model state at %s: not an object",
                    path,
                )
                return

            name = (state.get("model_name") or "").strip()
            commit = (state.get("model_commit") or "").strip()
            if bool(name) != bool(commit):
                logger.warning(
                    "[RequestManager] Ignoring invalid runtime model state at %s: partial pin",
                    path,
                )
                return

            with self._model_selection_lock:
                self._active_model_name = name
                self._active_model_commit = commit
                self._active_model_source = "persisted"

            if name and commit:
                logger.info(
                    "[RequestManager] Restored runtime model from %s: %s@%s",
                    path, name, commit,
                )
            else:
                logger.info("[RequestManager] Restored runtime auto-select mode from %s", path)
        except Exception as e:
            logger.warning(
                "[RequestManager] Failed to read runtime model state from %s: %s",
                path, e,
            )

    def _save_runtime_model_state(self) -> None:
        """Persist runtime model selection to disk."""
        path = self._runtime_model_state_path
        with self._model_selection_lock:
            name = (self._active_model_name or "").strip()
            commit = (self._active_model_commit or "").strip()
            source = (self._active_model_source or "env").strip() or "env"
        payload = {
            "model_name": name,
            "model_commit": commit,
            "source": source,
            "updated_at": int(time.time()),
        }
        try:
            parent = os.path.dirname(path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            tmp = f"{path}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=True)
            os.replace(tmp, path)
        except Exception as e:
            logger.warning(
                "[RequestManager] Failed to persist runtime model state to %s: %s",
                path, e,
            )

    def get_active_model(self) -> dict:
        """Return current runtime model selection."""
        with self._model_selection_lock:
            name = (self._active_model_name or "").strip()
            commit = (self._active_model_commit or "").strip()
            source = (self._active_model_source or "env").strip() or "env"
        pending = None
        switching = False
        with self._switch_state_lock:
            switching = bool(self._switch_in_progress)
            if self._pending_model_switch:
                pending = {
                    "model_name": self._pending_model_switch["model_name"],
                    "model_commit": self._pending_model_switch["model_commit"],
                    "mode": self._pending_model_switch["mode"],
                    "start_request_id": self._pending_model_switch["start_request_id"],
                }
        return {
            "model_name": name,
            "model_commit": commit,
            "pinned": bool(name and commit),
            "auto_select": not bool(name and commit),
            "source": source,
            "switch_in_progress": switching,
            "pending_switch": pending,
        }

    def set_active_model(self, model_name: str, model_commit: str, force_switch: bool = False) -> dict:
        """Update runtime model selection.

        Rules:
        - both empty => auto-select mode
        - one empty, one set => invalid
        - both set => must exist in model registry with matching commit
        """
        model_name = (model_name or "").strip()
        model_commit = (model_commit or "").strip()
        force_switch = bool(force_switch)

        if bool(model_name) != bool(model_commit):
            raise ValueError("model_name and model_commit must be provided together, or both empty")

        if not model_name and not model_commit:
            with self._model_selection_lock:
                self._active_model_name = ""
                self._active_model_commit = ""
                self._active_model_source = "runtime"
            with self._switch_state_lock:
                self._pending_model_switch = None
            self.context.set_expected_model_identifier("", "")
            self._save_runtime_model_state()
            logger.warning("[RequestManager] Runtime model pin cleared; auto-select mode enabled")
            return self.get_active_model()

        if not self.model_client:
            raise RuntimeError("Model client not initialized")

        model_info = self.model_client.get_model_by_name_and_commit(model_name, model_commit)
        if not model_info:
            by_name = self.model_client.get_model_by_name(model_name)
            if by_name:
                actual_commit = (by_name.get("model_commit") or "").strip()
                raise ValueError(
                    f"registry commit mismatch for '{model_name}': expected '{model_commit}', got '{actual_commit}'"
                )
            raise ValueError(f"model '{model_name}@{model_commit}' not found in registry")

        # Force switch: immediate backend restart and immediate runtime pin update.
        if force_switch:
            with self._switch_state_lock:
                self._switch_in_progress = True
                self._pending_model_switch = None
            try:
                # Guard window: suppress any lingering in-flight proofs from old backend.
                suppress_sec = max(0.0, float(constants.MODEL_SWITCH_FORCE_SUPPRESS_SEC))
                if suppress_sec > 0:
                    self.context.activate_solution_cooldown(
                        suppress_sec,
                        reason="model_switch_force",
                    )
                self._switch_backend_model_if_enabled(model_name, model_commit)
                with self._model_selection_lock:
                    self._active_model_name = model_name
                    self._active_model_commit = model_commit
                    self._active_model_source = "runtime"
                self.context.set_expected_model_identifier(model_name, model_commit)
                self._save_runtime_model_state()
                logger.info("[RequestManager] Runtime model force-switched to %s@%s", model_name, model_commit)
            finally:
                with self._switch_state_lock:
                    self._switch_in_progress = False
            return self.get_active_model()

        # Graceful switch: finish current mining request first (request_id boundary),
        # then switch backend and commit runtime pin.
        start_snapshot = self.context.read()
        start_request_id = int(start_snapshot.request_id)
        with self._switch_state_lock:
            self._pending_model_switch = {
                "model_name": model_name,
                "model_commit": model_commit,
                "mode": "graceful",
                "start_request_id": start_request_id,
                "start_block_hash": start_snapshot.block_hash,
                "start_header_prefix": start_snapshot.header_prefix,
                "start_target": start_snapshot.target,
                "created_at": time.time(),
            }
        logger.info(
            "[RequestManager] Scheduled graceful model switch to %s@%s at task boundary "
            "(start_request_id=%s, start_block=%s...)",
            model_name, model_commit, start_request_id, (start_snapshot.block_hash or "")[:16]
        )
        state = self.get_active_model()
        state["switch_scheduled"] = True
        return state

    def _apply_pending_model_switch(self) -> None:
        with self._switch_state_lock:
            pending = dict(self._pending_model_switch) if self._pending_model_switch else None
            if not pending or self._switch_in_progress:
                return
            self._switch_in_progress = True

        try:
            mode = pending.get("mode", "graceful")
            start_request_id = int(pending.get("start_request_id", 0))
            current_snapshot = self.context.read()
            current_request_id = int(current_snapshot.request_id)

            # Graceful switch must happen only when the mining task itself changes.
            # In practice request_id/header_prefix can rotate while still mining the
            # same block, so we use block_hash as the authoritative task boundary.
            start_block_hash = str(pending.get("start_block_hash") or "").strip()
            current_block_hash = str(current_snapshot.block_hash or "").strip()
            if start_block_hash:
                task_changed = bool(current_block_hash and current_block_hash != start_block_hash)
            else:
                # Backward-compatible fallback for legacy pending payloads.
                task_changed = current_request_id > start_request_id

            if mode == "graceful" and not task_changed:
                # Still mining the same task: defer switch until task boundary.
                return

            model_name = pending.get("model_name", "")
            model_commit = pending.get("model_commit", "")
            if not model_name or not model_commit:
                logger.warning("[RequestManager] Dropping invalid pending model switch payload: %s", pending)
                return

            self._switch_backend_model_if_enabled(model_name, model_commit)
            with self._model_selection_lock:
                self._active_model_name = model_name
                self._active_model_commit = model_commit
                self._active_model_source = "runtime"
            self.context.set_expected_model_identifier(model_name, model_commit)
            self._save_runtime_model_state()
            logger.info(
                "[RequestManager] Applied pending %s model switch to %s@%s (request_id %s -> %s)",
                mode, model_name, model_commit, start_request_id, current_request_id
            )
            with self._switch_state_lock:
                self._pending_model_switch = None
        except Exception as e:
            logger.exception("[RequestManager] Pending model switch failed: %s", e)
        finally:
            with self._switch_state_lock:
                self._switch_in_progress = False

    def _switch_backend_model_if_enabled(self, model_name: str, model_commit: str) -> None:
        if not constants.BACKEND_MODEL_SWITCH_ENABLED:
            return
        control_url = (constants.BACKEND_CONTROL_URL or "").strip().rstrip("/")
        if not control_url:
            raise ValueError("backend model switch is enabled but BACKEND_CONTROL_URL is empty")

        url = f"{control_url}/admin/switch-model"
        payload = json.dumps({
            "model_name": model_name,
            "model_commit": model_commit,
        }).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
        }
        if constants.BACKEND_CONTROL_API_KEY:
            headers["Authorization"] = f"Bearer {constants.BACKEND_CONTROL_API_KEY}"

        req = urlrequest.Request(url, data=payload, headers=headers, method="POST")
        timeout_sec = max(1, int(constants.BACKEND_SWITCH_TIMEOUT_SEC))
        deadline = time.time() + timeout_sec
        last_err = None

        while time.time() < deadline:
            try:
                # Keep per-attempt timeout short to allow retries during backend warmup.
                attempt_timeout = min(5, timeout_sec)
                with urlrequest.urlopen(req, timeout=attempt_timeout) as resp:
                    body = resp.read().decode("utf-8", errors="replace")
                    try:
                        parsed = json.loads(body)
                    except Exception:
                        parsed = {"ok": False, "error": f"invalid control response: {body[:200]}"}
                    if not parsed.get("ok", False):
                        raise ValueError(parsed.get("error", "backend rejected model switch"))
                    return
            except Exception as e:
                last_err = e
                # Retry window handles startup race (connection refused / backend not ready).
                time.sleep(1.0)

        raise ValueError(f"backend model switch failed after {timeout_sec}s: {last_err}")

    def _current_model_selection(self) -> tuple[str, str, bool]:
        """Get (model_name, model_commit, pinned) from runtime state."""
        with self._model_selection_lock:
            name = (self._active_model_name or "").strip()
            commit = (self._active_model_commit or "").strip()
        return name, commit, bool(name and commit)

    def _select_dummy_model_name(self) -> str:
        """Pick model name for dummy generation based on runtime selection."""
        if constants.GENESIS_GENERATOR:
            # Genesis grind: force the genesis model; do not depend on the
            # (testnet) Core Node registry.
            return constants.DEFAULT_MODEL_CONFIG.model_name
        if not self.model_client or not self.model_client.models_by_name:
            raise RuntimeError("No models available for dummy request")

        configured_name, configured_commit, model_pinned = self._current_model_selection()
        if not model_pinned:
            selected = next(iter(self.model_client.models_by_name.keys()))
            logger.warning(
                "[RequestManager] Runtime model is auto-select; using %r for dummy generation",
                selected,
            )
            return selected

        model_info = self.model_client.get_model_by_name_and_commit(configured_name, configured_commit)
        if not model_info:
            raise RuntimeError(
                f"Configured runtime model '{configured_name}@{configured_commit}' is not available in registry"
            )
        return configured_name

    def _mining_cooldown_error(self) -> str:
        remaining = self.context.get_solution_cooldown_remaining()
        return f"mining cooldown active for another {remaining:.1f}s"

    def _is_mining_paused(self) -> bool:
        return self.context.is_mining_paused()

    async def _cancel_dummy_tasks(self):
        if not self._dummy_tasks:
            return

        logger.info(
            "[RequestManager] Cancelling %d dummy request(s) during mining cooldown",
            len(self._dummy_tasks),
        )
        tasks = list(self._dummy_tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def _register_dummy_task(self, dummy_id: str) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._dummy_tasks[dummy_id] = task

    def _unregister_dummy_task(self, dummy_id: str) -> None:
        self._dummy_tasks.pop(dummy_id, None)

    async def start(self):
        """Start request manager and monitoring"""
        if self._broker_inference_only:
            logger.info(
                "[RequestManager] broker inference-only mode: skipping ModelClient "
                "startup, model registry sync, and backend model sync"
            )
        elif self._broker_registry_only:
            logger.info(
                "[RequestManager] broker mining mode: skipping local ModelClient "
                "startup; waiting for broker-pushed MODEL_REGISTRY_SYNC"
            )
        else:
            # Initialize model client first
            logger.info("[RequestManager] Initializing model client...")
            try:
                await self.model_client.start()
                model_status = self.model_client.get_status()
                logger.info(f"[RequestManager] Model client initialized: {model_status}")
            except Exception as e:
                logger.error(f"[RequestManager] Failed to initialize model client: {e}")
                # raise RuntimeError("Cannot start request manager without model client")

            # Cold-start sync: backend process may be started with env MODEL_* while
            # runtime model pin was restored from persisted state. Ensure backend
            # actually runs the same model used for proof model_identifier.
            configured_name, configured_commit, model_pinned = self._current_model_selection()
            if model_pinned:
                logger.info(
                    "[RequestManager] Startup model sync requested for runtime pin %s@%s",
                    configured_name, configured_commit,
                )
                try:
                    self._switch_backend_model_if_enabled(configured_name, configured_commit)
                    logger.info(
                        "[RequestManager] Startup model sync applied: backend aligned to %s@%s",
                        configured_name, configured_commit,
                    )
                except Exception as e:
                    logger.error(
                        "[RequestManager] Startup model sync failed for %s@%s: %s",
                        configured_name, configured_commit, e,
                    )
                    # Fail fast to avoid mining proofs where model_identifier and real
                    # backend model diverge.
                    raise RuntimeError(
                        f"startup model sync failed for {configured_name}@{configured_commit}: {e}"
                    )

        # Start HTTP session
        self.session = aiohttp.ClientSession()
        
        # Start monitoring
        self._monitor_task = asyncio.create_task(self._monitor_loop())
        
        logger.info(f"[RequestManager] Started successfully, target: {self.target_url}")
    
    async def stop(self):
        """Stop request manager gracefully"""
        logger.info("[RequestManager] Stopping...")
        
        # Cancel monitor task
        if self._monitor_task:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass
        
        # Close session
        if self.session:
            await self.session.close()
        
        # Note: Don't stop the model client as it's a global singleton
        
        logger.info("[RequestManager] Stopped")
    
    @staticmethod
    def update_header_prefix_bits(prefix_hex: str, new_bits: int) -> str:
        """
        Given a 76-byte header prefix as a hex string, replace the 4-byte 'bits'
        field (bytes 72–75) with new_bits (little-endian), and return the updated
        prefix as a hex string.

        :param prefix_hex: hex string of length 76*2 = 152 characters
        :param new_bits:  unsigned 32-bit integer to pack into the bits field
        :return:          new 76-byte prefix, hex-encoded
        """
        header = bytearray.fromhex(prefix_hex)
        if len(header) != 76:
            raise ValueError(f"Expected 76 bytes (152 hex chars), got {len(header)} bytes")

        # bytes  0– 3: version
        #       4–35: prev block hash
        #      36–67: merkle root
        #      68–71: timestamp
        #      72–75: bits  ← we replace this
        header[72:76] = struct.pack('<I', new_bits)

        return header.hex()

    def _validate_and_rebound_sampling_params(self, data: dict) -> None:
        """
        Ensure top_p, top_k, and temperature are present and within allowed bounds.
        If missing, set to defaults. If out of bounds, clamp and log a warning.
        """

        # Helper for clamping and logging
        def clamp_param(name, value, minv, maxv, default):
            if value is None:
                logger.info(f"[RequestManager] {name} not provided, using default: {default}")
                return default
            if not (minv <= value <= maxv):
                logger.warning(f"[RequestManager] {name}={value} out of bounds, clamping to [{minv}, {maxv}]")
                return max(minv, min(value, maxv))
            return value

        # Validate and set top_k
        top_k = data.get("top_k")
        data["top_k"] = clamp_param("top_k", top_k, TOPK_MIN, TOPK_MAX, DEFAULT_TOP_K)

        # Validate and set top_p
        top_p = data.get("top_p")
        data["top_p"] = clamp_param("top_p", top_p, TOPP_MIN, TOPP_MAX, DEFAULT_TOP_P)

        # Validate and set temperature
        temperature = data.get("temperature")
        data["temperature"] = clamp_param("temperature", temperature, TEMP_MIN, TEMP_MAX, DEFAULT_TEMP)

        # v3 mining (TIP-0003): the sampler profile is
        # consensus-FIXED. The verifier REJECTS any v3 proof not sampled with
        # EXACTLY temperature=1.0, top_k=50, top_p=1.0, repetition_penalty=1.0
        # (quick_verifier VerifyV3SamplerProfile / proof_verifier). Clamping is
        # not enough — repetition_penalty is not clamped above, and a merely
        # in-bounds top_k/top_p still fails the exact-equality check. This is
        # the PoW-mining ingress (not user inference: non-mining requests are
        # routed to the audit path before here), so force the fixed profile
        # rather than emit work the verifier will discard.
        if int(os.getenv("POW_PROOF_VERSION", "2")) >= 3:
            fixed_profile = {"temperature": 1.0, "top_k": 50, "top_p": 1.0,
                             "repetition_penalty": 1.0}
            for name, value in fixed_profile.items():
                if data.get(name) != value:
                    logger.info(f"[RequestManager] v3 fixed profile: forcing "
                                f"{name}={value} (was {data.get(name)})")
                data[name] = value
        return data

    def _backend_base_url(self, model_name) -> str:
        """Resolve the upstream base URL for a model (MODEL_ROUTES
        multi-backend routing); falls back to the default TARGET_URL
        backend for unrouted models and model-less requests."""
        if model_name and self._model_base_urls:
            routed = self._model_base_urls.get(model_name)
            if routed:
                return routed
        return self._base_url

    def _is_mining_model(self, model_name) -> bool:
        """True when the requested model is the one PoW-mining is pinned
        to. With no pin configured every model is treated as the mining
        model (legacy single-model behavior)."""
        configured_name, _configured_commit, model_pinned = self._current_model_selection()
        if not model_pinned:
            return True
        return model_name == configured_name

    def _inject_pow_data(self, data: dict) -> dict:
        """Inject PoW data into request"""
        if self._broker_inference_only:
            # Phase 0: ordinary inference must not require local Core Node
            # registry, VDF state, or ModelClient. Forward the request body
            # untouched; sampling defaults are left to the upstream backend.
            return data
        try:
            model_name = data.get('model', 'gpt2')
            configured_name, configured_commit, model_pinned = self._current_model_selection()

            # Non-mining models (e.g. the confidential inference model on a
            # dual-backend worker) get the AUDIT injection instead of the
            # old hard pin-mismatch failure: per-request audit_emit proofs
            # for the completion-audit cache, never mining. Audit is
            # fail-open and exempt from the mining pause/switch guards.
            if model_pinned and model_name != configured_name:
                return self._inject_audit_pow_data(data, model_name)

            with self._switch_state_lock:
                if self._switch_in_progress:
                    raise RuntimeError("Model switch in progress; request injection is temporarily paused")

            if self._is_mining_paused():
                raise RuntimeError(self._mining_cooldown_error())

            # Genesis grind: force the genesis model from config (DEFAULT_MODEL_CONFIG
            # = Qwen/Qwen3-8B@9c925d64..., CID bafybei..., difficulty=NORMALIZER so the
            # model multiplier is 1.0). Do NOT consult the (testnet) Core Node registry.
            if constants.GENESIS_GENERATOR:
                model_config = constants.DEFAULT_MODEL_CONFIG
            elif not self.model_client:
                raise RuntimeError("Model client not initialized")
            elif model_pinned:
                selected_model = self.model_client.get_model_by_name_and_commit(configured_name, configured_commit)
                if not selected_model:
                    raise RuntimeError(
                        f"Pinned model '{configured_name}@{configured_commit}' is not available in blockchain registry"
                    )
                model_config = constants.ModelConfig(
                    model_hash=selected_model.get("model_hash", ""),
                    model_name=selected_model.get("model_name", configured_name),
                    model_commit=selected_model.get("model_commit", configured_commit),
                    difficulty=selected_model.get("difficulty", constants.DEFAULT_DIFFICULTY),
                    ipfs_cid=selected_model.get("cid"),
                    target_adj=None,
                    txid=selected_model.get("txid"),
                    block_hash=selected_model.get("block_hash"),
                    block_height=selected_model.get("block_height")
                )
            else:
                # Get model config using name-based helper in auto-select / legacy mode
                try:
                    model_config = constants.get_model_config(model_name, self.model_client)
                except Exception as e:
                    logger.error(f"[RequestManager] Failed to get model config for '{model_name}': {e}")
                    # Log available models for debugging
                    available_models = list(self.model_client.models_by_name.keys())[:10]
                    logger.error(f"[RequestManager] Available models (first 10): {available_models}")
                    raise RuntimeError("Model not found in blockchain registry")
            
            if model_name != model_config.model_name:
                logger.error(f"[RequestManager] Model name mismatch: {model_name} != {model_config.model_name} - Defaulting to {model_config.model_name}")
                data['model'] = model_config.model_name

            # Get current mining context
            snapshot = self.context.read()

            # Guard: reject requests before VDF is ready (startup, block
            # change, cooldown recovery).  Sending an empty VDF crashes the
            # vLLM PoW sampler (torch.frombuffer on 0 bytes).
            if not snapshot.vdf_proof:
                raise RuntimeError(
                    "VDF proof not yet available — waiting for first "
                    "checkpoint after block change"
                )

            # compute adjusted difficulty for mining
            difficulty_dictionary = adjust_nbits_by_multiplier(snapshot.target,model_config.difficulty,constants.DEFAULT_DIFFICULTY)
            adj_difficulty = difficulty_dictionary["target_bytes"].hex()  
            # adj_difficulty = "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"
            adjnBits = difficulty_dictionary["nbits"]

            adjprefix = self.update_header_prefix_bits(snapshot.header_prefix, adjnBits)

            # Build PoW payload
            pow_payload = {
                'pow': {
                    'block_hash': snapshot.block_hash,
                    'vdf': snapshot.vdf_proof or "",
                    'tick': snapshot.vdf_tick,
                    'target': adj_difficulty,
                    'header_prefix': adjprefix,
                    'ipfs_cid': model_config.ipfs_cid or "QmDefault",
                    'request_id': snapshot.request_id,
                    'difficulty': model_config.difficulty,
                }
            }
            # Slice 11 dual-threshold emission: pass the model-adjusted
            # share target so the sampler also emits sub-block share
            # proofs (digest <= share_target, is_solution=False). Older
            # vllm images ignore the extra key (pow_snapshot.get -> ""
            # keeps pow_share_target zeroed). Fail-open: a derivation
            # problem degrades to block-only mining for this request,
            # never blocks it.
            if snapshot.base_share_target and constants.MODEL_DIFFICULTY_NORMALIZER > 0:
                try:
                    from components.mining_protocol import derive_adjusted_share_target
                    pow_payload['pow']['share_target'] = derive_adjusted_share_target(
                        base_share_target_hex=snapshot.base_share_target,
                        normalizer=constants.MODEL_DIFFICULTY_NORMALIZER,
                        difficulty=model_config.difficulty,
                    )
                except Exception as share_exc:  # noqa: BLE001
                    logger.warning(
                        f"[RequestManager] share-target derivation failed "
                        f"(req_id={snapshot.request_id}): {share_exc}; "
                        f"emitting block-tier only for this request"
                    )
            if constants.LLAMA_CPP:
                pow_payload['pow']['model_identifier'] = f"{model_config.model_name}@{model_config.model_commit}"
                pow_payload['pow']['compute_precision'] = f"bf16"
            # vllm_xargs for vLLM ≥0.16, extra_sampling_params for older vLLM / llama.cpp
            # Merge with any existing payload to avoid overwriting caller-provided args
            if constants.USE_VLLM_XARGS:
                data['vllm_xargs'] = {**(data.get('vllm_xargs') or {}), **pow_payload}
            else:
                data['extra_sampling_params'] = {**(data.get('extra_sampling_params') or {}), **pow_payload}
            
            data = self._validate_and_rebound_sampling_params(data)
            logger.debug(f"[RequestManager] Injected PoW data for model {model_name} "
                        f"(hash: {model_config.model_hash[:8]}..., "
                        f"difficulty: {adj_difficulty}, "
                        f"tick={snapshot.vdf_tick})")
            # import json
            # logger.info(json.dumps(data, indent=4))
            return data
            
        except Exception as e:
            logger.exception(f"[RequestManager] Error injecting PoW data: {e}")
            raise

    def _inject_audit_pow_data(self, data: dict, model_name: str) -> dict:
        """Audit-mode injection for models that are NOT the chain-pinned
        mining model (e.g. the confidential inference model on a
        dual-backend worker).

        The payload reuses the live mining context verbatim — block hash,
        VDF, tick, header prefix and the REAL block target (no fake-easy
        thresholds; emission is driven by ``audit_emit``, which the
        sampler honors per request and routes to the audit channel only).
        Registry-derived economic fields get neutral placeholders:
        difficulty = the normalizer (model multiplier 1.0) and the default
        CID. The proof's model identity comes from the serving vLLM
        instance's own ProofWriter metadata, not from the chain registry.

        Fail-open by contract: an audit proof must never cost an
        inference. No mining context yet, VDF not ready, or any other
        problem → forward the request body untouched. User sampling
        params are NOT clamped — the mining entropy envelope does not
        apply to audit proofs (verification uses the audit parameter
        check instead).
        """
        try:
            snapshot = self.context.read() if self.context else None
            if not snapshot or not snapshot.vdf_proof:
                logger.debug(
                    "[RequestManager] Audit injection skipped for '%s' "
                    "(mining context/VDF not ready); forwarding un-injected",
                    model_name,
                )
                return data

            difficulty = (
                constants.MODEL_DIFFICULTY_NORMALIZER
                if constants.MODEL_DIFFICULTY_NORMALIZER > 0
                else constants.DEFAULT_DIFFICULTY
            )
            pow_payload = {
                'pow': {
                    'block_hash': snapshot.block_hash,
                    'vdf': snapshot.vdf_proof or "",
                    'tick': snapshot.vdf_tick,
                    'target': snapshot.target,
                    'header_prefix': snapshot.header_prefix,
                    'ipfs_cid': "QmDefault",
                    'request_id': snapshot.request_id,
                    'difficulty': difficulty,
                    # Per-request audit flag — the sampler emits a proof
                    # for every window of this sequence and submits it
                    # with proof_purpose=audit; the collector caches it
                    # by completion_id and never feeds mining.
                    'audit_emit': True,
                }
            }
            if constants.USE_VLLM_XARGS:
                data['vllm_xargs'] = {**(data.get('vllm_xargs') or {}), **pow_payload}
            else:
                data['extra_sampling_params'] = {**(data.get('extra_sampling_params') or {}), **pow_payload}
            logger.debug(
                f"[RequestManager] Injected audit PoW data for model {model_name} "
                f"(tick={snapshot.vdf_tick}, req_id={snapshot.request_id})"
            )
            return data
        except Exception as e:
            logger.warning(
                f"[RequestManager] Audit injection failed for '{model_name}' "
                f"(forwarding un-injected): {e}"
            )
            return data

    async def proxy_request(self, request: web.Request) -> web.Response:
        """Route requests to appropriate handlers"""
        path = request.path
        method = request.method
        
        if method == "GET" and path == "/v1/models":
            return await self._handle_models_request(request)
        elif method == "GET" and path == "/props":
            # llama.cpp server exposes context window via /props (vllm doesn't
            # serve this route). worker_client uses it as a fallback when
            # /v1/models on llama-cpp returns no max_model_len. Plain
            # passthrough — no PoW injection, no body transform.
            return await self._handle_passthrough_request(request)
        elif method == "POST" and path in ["/v1/chat/completions", "/v1/completions"]:
            return await self._handle_completion_request(request)
        elif method == "POST" and path == "/v1/embeddings":
            # Pass-through (no PoW injection)
            return await self._handle_passthrough_request(request)
        elif method == "POST" and path == "/v1/responses":
            # OpenAI Responses API (with PoW injection by default)
            return await self._handle_responses_request(request)
        elif method == "GET" and path.startswith("/v1/responses/"):
            # Retrieve async response by id
            return await self._handle_passthrough_request(request)
        elif method == "POST" and path.startswith("/v1/responses/") and path.endswith("/cancel"):
            # Cancel async response
            return await self._handle_passthrough_request(request)
        else:
            return web.Response(
                text='{"error": "Endpoint not supported"}',
                status=404,
                content_type='application/json'
            )

    async def _handle_models_request(self, request: web.Request) -> web.Response:
        """Handle /v1/models GET request.

        Multi-backend (MODEL_ROUTES): fan out to every routed backend plus
        the default and merge the model lists, so worker_client's HELLO
        introspection advertises everything served behind this proxy."""
        backend_urls = []
        for url in [self._base_url, *self._model_base_urls.values()]:
            if url not in backend_urls:
                backend_urls.append(url)

        if len(backend_urls) == 1:
            async with self.session.get(
                f"{backend_urls[0]}/v1/models", headers=self.auth_headers
            ) as response:
                body = await response.read()
                return web.Response(body=body, status=response.status, headers=response.headers)

        merged, seen = [], set()
        reachable = 0
        for url in backend_urls:
            try:
                # vLLM's /v1/models requires the API key (401 without it).
                # Send auth so the fan-out actually succeeds — otherwise
                # every backend reads as "unreachable", the merge 502s, and
                # the worker's HELLO falls back to the env-pinned (mining)
                # model, dropping the inference model from broker
                # registration.
                async with self.session.get(
                    f"{url}/v1/models", headers=self.auth_headers
                ) as response:
                    if response.status != 200:
                        logger.warning(
                            "[RequestManager] /v1/models from %s returned %s",
                            url, response.status,
                        )
                        continue
                    payload = await response.json()
                reachable += 1
                for entry in payload.get("data", []) or []:
                    model_id = entry.get("id")
                    if model_id and model_id not in seen:
                        seen.add(model_id)
                        merged.append(entry)
            except Exception as e:
                logger.warning("[RequestManager] /v1/models fan-out to %s failed: %s", url, e)
        if reachable == 0:
            # All backends down/unready — let the worker's introspection
            # retry rather than advertising an empty model list.
            return web.Response(
                text='{"error": "no upstream backend reachable"}',
                status=502,
                content_type='application/json',
            )
        return web.json_response({"object": "list", "data": merged})

    async def _handle_completion_request(self, request: web.Request) -> web.Response:
        """Handle completion requests with PoW injection"""
        request_id = str(uuid.uuid4())
        self.active_requests[request_id] = time.time()
        
        logger.info(f"[RequestManager] Request {request_id} started - Total active: {len(self.active_requests)}")
        
        try:
            # Validate request has JSON body
            if request.content_type != 'application/json':
                return web.Response(
                    text='{"error": "Content-Type must be application/json"}',
                    status=400,
                    content_type='application/json'
                )
            
            # Read and inject data
            try:
                data = await request.json()
            except Exception as e:
                return web.Response(
                    text=f'{{"error": "Invalid JSON: {str(e)}"}}',
                    status=400,
                    content_type='application/json'
                )
            
            # Mining readiness gates apply ONLY to the mining model —
            # non-mining (audit-only) models must never be 503'd by
            # mining cooldown or a cold registry.
            if not self._broker_inference_only and self._is_mining_model(data.get('model')):
                # Check if model client is ready
                if not self.model_client or not self.model_client._initialized:
                    logger.error("[RequestManager] Model client not ready for request")
                    return web.Response(
                        text='{"error": "Service initializing, please try again"}',
                        status=503,
                        content_type='application/json'
                    )
                if self._is_mining_paused():
                    return web.Response(
                        text=f'{{"error": "{self._mining_cooldown_error()}"}}',
                        status=503,
                        content_type='application/json'
                    )

            # Forward request
            headers = {k: v for k, v in request.headers.items()
                      if k.lower() not in ['host', 'content-length']}

            is_streaming = data.get('stream', False)
            modified_data = self._inject_pow_data(data)

            # For streaming requests, enable usage inclusion in the final event
            if is_streaming:
                if "stream_options" not in modified_data:
                    modified_data["stream_options"] = {}
                modified_data["stream_options"]["include_usage"] = True

            response = await self.session.post(
                f"{self._backend_base_url(data.get('model'))}{request.path}",
                json=modified_data,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=300)
            )
            if is_streaming:
                    response_headers = {
                        k: v for k, v in response.headers.items()
                        if k.lower() not in ('content-length', 'transfer-encoding')
                    }
                    response_headers['Content-Type'] = 'text/event-stream'
                    response_headers['Cache-Control'] = 'no-cache'

                    stream_response = web.StreamResponse(
                        status=response.status,
                        headers=response_headers
                    )
                    await stream_response.prepare(request)
                    # Accumulate stream data to extract token usage from final event
                    accumulated_data = b""
                    try:
                        async for chunk in response.content.iter_chunked(8192):
                            await stream_response.write(chunk)
                            accumulated_data += chunk
                    finally:
                        try:
                            await stream_response.write_eof()
                        except Exception:
                            pass
                        # Try to extract token usage from SSE stream
                        self._record_stream_token_usage(accumulated_data)
                    return stream_response
            else:
                    body = await response.read()
                    # Track token usage for throughput metrics
                    self._record_token_usage(body)
                    return web.Response(body=body, status=response.status, headers=response.headers)

        except asyncio.TimeoutError:
            logger.error(f"[RequestManager] Request {request_id} timed out")
            return web.Response(
                text='{"error": "Request timeout"}',
                status=504,
                content_type='application/json'
            )
        except aiohttp.ClientError as e:
            logger.error(f"[RequestManager] Upstream client error: {e}")
            return web.Response(
                text='{"error": "Bad Gateway"}',
                status=502,
                content_type='application/json'
            )
            
        except Exception as e:
            logger.exception(f"[RequestManager] Request {request_id} failed: {e}")
            return web.Response(
                text=f'{{"error": "Proxy error: {str(e)}"}}',
                status=500,
                content_type='application/json'
            )
            
        finally:
            self.active_requests.pop(request_id, None)

    async def _handle_passthrough_request(self, request: web.Request) -> web.Response:
        """Generic pass-through handler (no PoW injection). Supports streaming if requested."""
        try:
            headers = {k: v for k, v in request.headers.items()
                      if k.lower() not in ['host', 'content-length']}

            # GET requests: pass query params and return body
            if request.method == 'GET':
                try:
                    response = await self.session.get(
                        f"{self._base_url}{request.path}",
                        params=request.rel_url.query,
                        headers=headers,
                        timeout=aiohttp.ClientTimeout(total=300)
                    )
                    body = await response.read()
                    try:
                        response.release()
                    except Exception:
                        pass
                    return web.Response(body=body, status=response.status, headers=response.headers)
                except asyncio.TimeoutError:
                    return web.Response(text='{"error": "Request timeout"}', status=504, content_type='application/json')
                except aiohttp.ClientError:
                    return web.Response(text='{"error": "Bad Gateway"}', status=502, content_type='application/json')

            # POST requests
            is_json = request.content_type == 'application/json'
            data = None
            raw = None
            is_streaming = False
            if is_json:
                data = await request.json()
                # Some endpoints (e.g., /v1/responses) stream when stream=true
                if isinstance(data, dict):
                    is_streaming = bool(data.get('stream', False))
            else:
                raw = await request.read()

            response = await self.session.post(
                f"{self._base_url}{request.path}",
                json=data if is_json else None,
                data=raw if not is_json else None,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=300)
            )
            upstream_ct = response.headers.get('Content-Type', '')
            if is_streaming or ('text/event-stream' in upstream_ct.lower()):
                    response_headers = {
                        k: v for k, v in response.headers.items()
                        if k.lower() not in ('content-length', 'transfer-encoding')
                    }
                    response_headers['Content-Type'] = 'text/event-stream'
                    response_headers['Cache-Control'] = 'no-cache'

                    stream_response = web.StreamResponse(
                        status=response.status,
                        headers=response_headers
                    )
                    await stream_response.prepare(request)
                    try:
                        async for chunk in response.content.iter_chunked(8192):
                            await stream_response.write(chunk)
                    finally:
                        try:
                            await stream_response.write_eof()
                        except Exception:
                            pass
                        try:
                            response.release()
                        except Exception:
                            pass
                    return stream_response

            body = await response.read()
            try:
                response.release()
            except Exception:
                pass
            return web.Response(body=body, status=response.status, headers=response.headers)

        except Exception as e:
            logger.exception(f"[RequestManager] Passthrough error: {e}")
            return web.Response(
                text=f'{{"error": "Proxy error: {str(e)}"}}',
                status=500,
                content_type='application/json'
            )

    async def _handle_responses_request(self, request: web.Request) -> web.Response:
        """Handle POST /v1/responses with PoW injection and streaming support."""
        request_id = str(uuid.uuid4())
        self.active_requests[request_id] = time.time()

        logger.info(f"[RequestManager] Responses request {request_id} started - Total active: {len(self.active_requests)}")

        try:
            if request.content_type != 'application/json':
                return web.Response(
                    text='{"error": "Content-Type must be application/json"}',
                    status=400,
                    content_type='application/json'
                )

            try:
                data = await request.json()
            except Exception as e:
                return web.Response(
                    text=f'{{"error": "Invalid JSON: {str(e)}"}}',
                    status=400,
                    content_type='application/json'
                )

            # Mining readiness gates apply ONLY to the mining model (see
            # _handle_completion_request for the rationale).
            if not self._broker_inference_only and self._is_mining_model(data.get('model')):
                if not self.model_client or not self.model_client._initialized:
                    logger.error("[RequestManager] Model client not ready for responses request")
                    return web.Response(
                        text='{"error": "Service initializing, please try again"}',
                        status=503,
                        content_type='application/json'
                    )
                if self._is_mining_paused():
                    return web.Response(
                        text=f'{{"error": "{self._mining_cooldown_error()}"}}',
                        status=503,
                        content_type='application/json'
                    )

            headers = {k: v for k, v in request.headers.items()
                      if k.lower() not in ['host', 'content-length']}

            is_streaming = bool(data.get('stream', False))
            modified_data = self._inject_pow_data(data)

            response = await self.session.post(
                f"{self._backend_base_url(data.get('model'))}{request.path}",
                json=modified_data,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=300)
            )
            if is_streaming:
                    response_headers = {
                        k: v for k, v in response.headers.items()
                        if k.lower() not in ('content-length', 'transfer-encoding')
                    }
                    response_headers['Content-Type'] = 'text/event-stream'
                    response_headers['Cache-Control'] = 'no-cache'

                    stream_response = web.StreamResponse(
                        status=response.status,
                        headers=response_headers
                    )
                    await stream_response.prepare(request)
                    # Accumulate stream data to extract token usage from final event
                    accumulated_data = b""
                    try:
                        async for chunk in response.content.iter_chunked(8192):
                            await stream_response.write(chunk)
                            accumulated_data += chunk
                    finally:
                        try:
                            await stream_response.write_eof()
                        except Exception:
                            pass
                        try:
                            response.release()
                        except Exception:
                            pass
                        # Try to extract token usage from SSE stream
                        self._record_stream_token_usage(accumulated_data)
                    return stream_response

            body = await response.read()
            try:
                response.release()
            except Exception:
                pass
            # Track token usage for throughput metrics
            self._record_token_usage(body)
            return web.Response(body=body, status=response.status, headers=response.headers)

        except asyncio.TimeoutError:
            logger.error(f"[RequestManager] Responses request {request_id} timed out")
            return web.Response(
                text='{"error": "Request timeout"}',
                status=504,
                content_type='application/json'
            )
        except Exception as e:
            logger.exception(f"[RequestManager] Responses request {request_id} failed: {e}")
            return web.Response(
                text=f'{{"error": "Proxy error: {str(e)}"}}',
                status=500,
                content_type='application/json'
            )
        finally:
            self.active_requests.pop(request_id, None)
    
    async def _generate_dummy_request(self):
        """Generate dummy request to maintain minimum active"""
        dummy_id = f"dummy-{uuid.uuid4()}"
        self.active_requests[dummy_id] = time.time()
        self._register_dummy_task(dummy_id)
                
        try:
            if self._is_mining_paused():
                logger.info("[RequestManager] Skipping dummy generation: %s", self._mining_cooldown_error())
                return

            # Prefer configured runtime model; if MODEL_NAME and MODEL_COMMIT are both unset,
            # auto-select from registry as a backward-compatible fallback.
            if constants.GENESIS_GENERATOR:
                # Genesis grind: pin the genesis model from config; the Core Node
                # registry (testnet) is intentionally not consulted.
                model_name = constants.DEFAULT_MODEL_CONFIG.model_name
            elif self.model_client and self.model_client.models_by_name:
                try:
                    model_name = self._select_dummy_model_name()
                except Exception as e:
                    logger.error("[RequestManager] %s", e)
                    return
            else:
                logger.error("[RequestManager] No models available for dummy request")
                return

            prompts = [self.prompt_generator.generate_prompt() for _ in range(constants.BATCH_SIZE)]
            if constants.GENESIS_GENERATOR:
                prompts = self.genesis_generator.generate(constants.BATCH_SIZE)
            if constants.LLAMA_CPP:
                prompts = self.prompt_generator.generate_prompt()
            dummy_data = {
                "model": model_name,
                "prompt": prompts,
                "max_tokens": 256,
                "temperature": 1.0,
                "top_k": 50,
                "top_p": 1.0,            
            }            
            modified_data = self._inject_pow_data(dummy_data)
            
            for attempt in range(1, constants.DUMMY_RETRY_ATTEMPTS + 1):
                try:
                    async with self.session.post(
                        f"{self._backend_base_url(model_name)}/v1/completions",
                        json=modified_data,
                        headers=self.auth_headers,
                        timeout=aiohttp.ClientTimeout(total=constants.DUMMY_REQUEST_TIMEOUT)
                    ) as resp:
                        body = await resp.read()
                        # Track token usage from dummy requests for throughput metrics
                        self._record_token_usage(body)
                    logger.debug(f"[RequestManager] Dummy {dummy_id} attempt {attempt} succeeded")
                    break

                except Exception as e:
                    if attempt == constants.DUMMY_RETRY_ATTEMPTS:
                        logger.error(f"[RequestManager] Dummy {dummy_id} failed after {attempt} attempts: {e}")
                    else:
                        delay = constants.DUMMY_RETRY_BACKOFF * (2 ** (attempt - 1))
                        logger.warning(
                            f"[RequestManager] Dummy {dummy_id} attempt {attempt} failed, "
                            f"retrying in {delay}s: {e}"
                        )
                        await asyncio.sleep(delay)
            
        finally:
            self._unregister_dummy_task(dummy_id)
            self.active_requests.pop(dummy_id, None)
    
    async def _monitor_loop(self):
        """Monitor and maintain minimum active requests"""
        logger.info(f"[RequestManager] Monitor loop started, maintaining {self.min_active} active requests")

        try:
            while True:
                # Apply deferred model switches even when miner is idle.
                self._apply_pending_model_switch()

                if self.context.vdf_initialised and self.context.miner_initialised:
                    if self._is_mining_paused():
                        await self._cancel_dummy_tasks()
                        await asyncio.sleep(self.monitor_interval)
                        continue

                    # Clean up stale requests (older than 5 minutes)
                    current_time = time.time()
                    stale_ids = [
                        rid for rid, start_time in self.active_requests.items()
                        if current_time - start_time > 300
                    ]
                    for rid in stale_ids:
                        logger.warning(f"[RequestManager] Cleaning up stale request: {rid}")
                        del self.active_requests[rid]

                    # Check if mining context is stale (no block from core-node recently)
                    context_status = self.context.get_status()
                    context_age = context_status.get("age_seconds", 0)
                    is_stale = context_age > constants.MINING_STALE_THRESHOLD_SECONDS

                    if is_stale:
                        logger.debug(f"[RequestManager] Mining context stale "
                                   f"(age={context_age:.1f}s > {constants.MINING_STALE_THRESHOLD_SECONDS}s), "
                                   f"skipping dummy generation")
                    else:
                        # Check if we need dummy requests
                        active_count = len(self.active_requests)
                        if active_count < self.min_active:
                            needed = self.min_active - active_count
                            logger.info(f"[RequestManager] Creating {needed} dummy requests (current: {active_count})")

                            tasks = [self._generate_dummy_request() for _ in range(needed)]
                            await asyncio.gather(*tasks, return_exceptions=True)

                await asyncio.sleep(self.monitor_interval)
                
        except asyncio.CancelledError:
            logger.info("[RequestManager] Monitor loop cancelled")
            raise
        except Exception as e:
            logger.exception(f"[RequestManager] Monitor loop error: {e}")
    
    def _record_token_usage(self, response_body: bytes) -> None:
        """Extract and record token usage from vLLM response for throughput tracking"""
        try:
            import json
            data = json.loads(response_body)
            usage = data.get("usage", {})
            prompt_tokens = usage.get("prompt_tokens", 0)
            completion_tokens = usage.get("completion_tokens", 0)

            if prompt_tokens > 0 or completion_tokens > 0:
                now = time.time()
                self._token_events.append((now, prompt_tokens, completion_tokens))
                self._total_prompt_tokens += prompt_tokens
                self._total_completion_tokens += completion_tokens
                self._total_completions += 1

                # Prune old events outside the window
                cutoff = now - self._throughput_window_seconds
                self._token_events = [(t, p, c) for t, p, c in self._token_events if t > cutoff]
        except Exception:
            pass  # Don't fail on parsing errors

    def _record_stream_token_usage(self, stream_data: bytes) -> None:
        """Extract and record token usage from SSE stream data (vLLM streaming response)"""
        try:
            import json
            # Parse SSE events - format: "data: {...}\n\n" or "data: [DONE]\n\n"
            text = stream_data.decode('utf-8', errors='ignore')

            # Look for usage data in the stream - vLLM includes it in final chunks
            # Try to find the last JSON object with "usage" field
            prompt_tokens = 0
            completion_tokens = 0

            for line in text.split('\n'):
                line = line.strip()
                if line.startswith('data:'):
                    data_str = line[5:].strip()
                    if data_str == '[DONE]':
                        continue
                    try:
                        data = json.loads(data_str)
                        # Check for usage in the event (vLLM includes in final event)
                        usage = data.get("usage")
                        if usage:
                            prompt_tokens = usage.get("prompt_tokens", 0)
                            completion_tokens = usage.get("completion_tokens", 0)
                        # Also accumulate from choices if usage not available
                        # vLLM sometimes sends token counts per chunk
                        for choice in data.get("choices", []):
                            if "usage" in choice:
                                prompt_tokens = choice["usage"].get("prompt_tokens", prompt_tokens)
                                completion_tokens = choice["usage"].get("completion_tokens", completion_tokens)
                    except json.JSONDecodeError:
                        continue

            if prompt_tokens > 0 or completion_tokens > 0:
                now = time.time()
                self._token_events.append((now, prompt_tokens, completion_tokens))
                self._total_prompt_tokens += prompt_tokens
                self._total_completion_tokens += completion_tokens
                self._total_completions += 1

                # Prune old events outside the window
                cutoff = now - self._throughput_window_seconds
                self._token_events = [(t, p, c) for t, p, c in self._token_events if t > cutoff]
                logger.debug(f"[RequestManager] Stream tokens recorded: prompt={prompt_tokens}, completion={completion_tokens}")
        except Exception as e:
            logger.debug(f"[RequestManager] Failed to parse stream token usage: {e}")

    def _get_throughput_stats(self) -> dict:
        """Calculate token throughput from rolling window"""
        now = time.time()
        cutoff = now - self._throughput_window_seconds

        # Prune old events
        self._token_events = [(t, p, c) for t, p, c in self._token_events if t > cutoff]

        if not self._token_events:
            return {
                "prompt_tokens_per_sec": 0.0,
                "completion_tokens_per_sec": 0.0,
                "total_tokens_per_sec": 0.0,
                "completions_per_min": 0.0,
                "hashes_per_sec": 0.0,  # 1 hash = 256 completion tokens
                "window_seconds": self._throughput_window_seconds,
            }

        # Calculate time span of events in window
        oldest_time = min(t for t, _, _ in self._token_events)
        time_span = max(now - oldest_time, 1.0)  # Avoid division by zero

        window_prompt = sum(p for _, p, _ in self._token_events)
        window_completion = sum(c for _, _, c in self._token_events)
        completion_per_sec = window_completion / time_span

        return {
            "prompt_tokens_per_sec": round(window_prompt / time_span, 2),
            "completion_tokens_per_sec": round(completion_per_sec, 2),
            "total_tokens_per_sec": round((window_prompt + window_completion) / time_span, 2),
            "completions_per_min": round(len(self._token_events) / time_span * 60, 2),
            "hashes_per_sec": round(completion_per_sec / 256, 4),  # 1 hash = 256 completion tokens
            "window_seconds": self._throughput_window_seconds,
        }

    def get_status(self) -> dict:
        """Get current status of request manager"""
        model_status = {}
        if self.model_client:
            model_status = self.model_client.get_status()
            # Add sample of available models
            available_models = list(self.model_client.models_by_name.keys())
            model_status["sample_models"] = available_models[:10]
            model_status["total_models"] = len(available_models)

        throughput = self._get_throughput_stats()

        return {
            "active_requests": len(self.active_requests),
            "target_url": self.target_url,
            "min_active": self.min_active,
            "session_open": self.session is not None and not self.session.closed,
            "active_model": self.get_active_model(),
            "model_sync": model_status,
            "throughput": throughput,
            "totals": {
                "prompt_tokens": self._total_prompt_tokens,
                "completion_tokens": self._total_completion_tokens,
                "completions": self._total_completions,
                "hashes": self._total_completion_tokens // 256,  # 1 hash = 256 completion tokens
            }
        }
