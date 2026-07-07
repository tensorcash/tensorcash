"""
WebSocket client that connects to Compute Broker as a worker.
Uses the 'websockets' library for proper header handling during WS upgrade.

W2 Confidential Mode:
- Advertises supported modes (plaintext, confidential) in HELLO
- Self-registers public key with auth-service on startup
- Fetches CEK for confidential jobs
- Decrypts incoming payloads and encrypts responses

W3 Tool Capability:
- Registers tools with broker on startup (TOOL_REGISTER)
- Handles TOOL_REGISTER_ACK

W4 Mining Sidecar:
- Advertises mining capability in HELLO
- Handles MINE_REQUEST - injects into VDF context
- Sends MINE_RESULT when proof is ready
"""
import asyncio
import json
import uuid
import base64
import time
import os
import mimetypes
from typing import Optional, Dict, Any, List, TYPE_CHECKING
from urllib.parse import urlparse
import aiohttp
import websockets
import websockets.exceptions
import logging
from components import constants

if TYPE_CHECKING:
    from components.context import LockFreeContext
    from components.zmq_listener import ZMQListener
    from components.proof_collector import ProofCollector

logger = logging.getLogger(__name__)

# Import confidential crypto service (optional - graceful degradation)
try:
    from components.confidential_crypto import get_crypto_service, ConfidentialCryptoService
    CONFIDENTIAL_AVAILABLE = True
except Exception as e:
    CONFIDENTIAL_AVAILABLE = False
    ConfidentialCryptoService = None  # type: ignore
    get_crypto_service = lambda: None  # type: ignore
    logger.warning(f"Confidential crypto not available: {e}")


class BrokerWorkerClient:
    """WebSocket client that connects to Compute Broker as a worker"""

    def __init__(
        self,
        context: Optional["LockFreeContext"] = None,
        zmq_listener: Optional["ZMQListener"] = None,
        proof_collector: Optional["ProofCollector"] = None,
        request_manager: Optional["RequestManager"] = None,
    ):
        self.broker_url = constants.BROKER_WS_URL
        self.worker_id = str(uuid.uuid4())
        # Separate JWT and shared secret tokens - prefer JWT for production
        # Strip whitespace/newlines that may be present from copy-paste
        self.jwt_token = (constants.PROVIDER_JWT_TOKEN or "").strip()
        self.worker_token = (constants.X_WORKER_TOKEN or "").strip()
        self.miner_proxy_url = self._resolve_local_inference_base_url()
        self.ws: Optional[websockets.WebSocketClientProtocol] = None
        self.http_session: Optional[aiohttp.ClientSession] = None
        self.running = False
        self.active_jobs: set = set()
        self.heartbeat_interval = 15  # will be updated from ACK
        self.heartbeat_task: Optional[asyncio.Task] = None
        self.connection_attempts = 0
        self.last_connect_attempt_at: Optional[float] = None
        self.last_connected_at: Optional[float] = None
        self.last_disconnected_at: Optional[float] = None
        self.last_message_at: Optional[float] = None
        self.last_ack_at: Optional[float] = None
        self.last_heartbeat_sent_at: Optional[float] = None
        self.last_reconnect_error: Optional[str] = None
        self.heartbeat_send_timeout = float(
            os.getenv("BROKER_HEARTBEAT_SEND_TIMEOUT_SECONDS", "10")
        )

        # W2: Confidential mode crypto service
        self.crypto_service: Optional[ConfidentialCryptoService] = None
        if CONFIDENTIAL_AVAILABLE and constants.CONFIDENTIAL_MODE_ENABLED:
            self.crypto_service = get_crypto_service()
            if self.crypto_service:
                logger.info("Confidential mode enabled")

        # W4: Mining sidecar
        self.context = context
        self.zmq_listener = zmq_listener
        self.proof_collector = proof_collector
        self.mining_enabled = constants.MINING_ENABLED and context is not None
        # Slice 9: handle on the local RequestManager so MODEL_REGISTRY_SYNC
        # from the broker can be forwarded into its ModelClient. Optional
        # so standalone tests don't have to wire a RequestManager.
        self.request_manager = request_manager
        # Track job_id -> request_id mapping (broker job_id -> BlockHeader request_id)
        self.mining_job_mapping: Dict[str, int] = {}
        # Track request_id -> job_id for reverse lookup when solution arrives
        self.mining_request_mapping: Dict[int, str] = {}
        # Phase 3: typed in-flight cache so MINE_RESULT can carry the full
        # correlation set (work_unit_id, wallet_id, network, template_id)
        # when proof_collector hands a solution back. Keyed by req_id.
        self._mining_in_flight: Dict[int, "MineRequest"] = {}  # noqa: F821
        # Insertion timestamps for the three maps above. Entries are
        # popped on the solution/share path but NOT when the broker's
        # lease simply times out (the worker is never told), so without
        # a TTL purge they grow one entry per MINE_REQUEST forever —
        # observed in production as active_mining_jobs climbing past
        # 150 within hours. Purged from the heartbeat loop.
        self._mining_job_seen_at: Dict[int, float] = {}
        # Event loop reference for thread-safe callback
        self._loop: Optional[asyncio.AbstractEventLoop] = None

        # W3: Tool registration tracking
        self.registered_tools: set = set()
        # W2: Track key registration status (set true once auth-service confirms)
        self.public_key_registered = False
        # Confidential run context cache for tool_result continuation.
        # Keyed by broker run_id from payload.encryption.run_id.
        self.confidential_runs: Dict[str, Dict[str, Any]] = {}
        self.confidential_run_ttl_sec = 30 * 60

        # Local tool routing + registration catalog.
        self.tool_configs: Dict[str, Dict[str, Any]] = {}
        self.worker_tools: List[Dict[str, Any]] = []
        self._set_worker_tools_from_configs(constants.WORKER_TOOLS)

    def _resolve_local_inference_base_url(self) -> str:
        """
        Resolve where worker should forward local inference requests.

        - Normal mode: local miner-proxy HTTP server.
        - Broker-only mode (HTTP disabled): direct upstream TARGET_URL.
        """
        if constants.DISABLE_HTTP_SERVER:
            candidate = (constants.TARGET_URL or "").strip()
            source = "TARGET_URL (broker-only mode)"
        else:
            candidate = f"http://localhost:{constants.HTTP_PORT}"
            source = "local miner-proxy HTTP"

        parsed = urlparse(candidate)
        if parsed.scheme and parsed.netloc:
            base_url = f"{parsed.scheme}://{parsed.netloc}"
        else:
            base_url = candidate.rstrip("/")

        logger.info("Worker local inference base URL resolved: %s (%s)", base_url, source)
        return base_url

    def _normalize_worker_tool_config(self, tool_config: Any) -> Optional[Dict[str, Any]]:
        """Normalize tool config payloads from env/discovery into broker registration shape."""
        if not isinstance(tool_config, dict):
            return None

        tool_id = tool_config.get("tool_id")
        if not isinstance(tool_id, str):
            return None
        tool_id = tool_id.strip()
        if not tool_id:
            return None

        normalized = dict(tool_config)
        normalized["tool_id"] = tool_id

        encryption = normalized.get("encryption")
        if not isinstance(encryption, str) or not encryption.strip():
            normalized["encryption"] = "aes-256-gcm"

        executor = normalized.get("executor")
        if not isinstance(executor, str) or not executor.strip():
            normalized["executor"] = "worker"

        schema_ref = normalized.get("schema_ref")
        if isinstance(schema_ref, dict):
            try:
                normalized["schema_ref"] = json.dumps(schema_ref, separators=(",", ":"))
            except Exception:
                normalized["schema_ref"] = ""
        elif not isinstance(schema_ref, str):
            normalized["schema_ref"] = ""

        return normalized

    def _set_worker_tools_from_configs(self, tool_configs: List[Any]) -> None:
        """Replace runtime tool catalog from config list."""
        normalized_tools: List[Dict[str, Any]] = []
        tool_map: Dict[str, Dict[str, Any]] = {}
        for raw in tool_configs:
            normalized = self._normalize_worker_tool_config(raw)
            if not normalized:
                continue
            tool_id = normalized["tool_id"]
            # Last writer wins for duplicate tool_ids.
            tool_map[tool_id] = normalized

        for tool_id, tool in tool_map.items():
            normalized_tools.append(tool)

        self.worker_tools = normalized_tools
        self.tool_configs = {tool["tool_id"]: tool for tool in normalized_tools}

    def _extract_discovered_tools(self, payload: Any) -> List[Dict[str, Any]]:
        """Parse tool list response into worker tool registration entries."""
        tools_raw: Any = None
        if isinstance(payload, list):
            tools_raw = payload
        elif isinstance(payload, dict):
            if isinstance(payload.get("tools"), list):
                tools_raw = payload.get("tools")
            elif isinstance(payload.get("result"), dict) and isinstance(payload["result"].get("tools"), list):
                tools_raw = payload["result"]["tools"]
            elif isinstance(payload.get("result"), list):
                tools_raw = payload.get("result")

        if not isinstance(tools_raw, list):
            return []

        discovered: List[Dict[str, Any]] = []
        for item in tools_raw:
            if not isinstance(item, dict):
                continue

            tool_name = item.get("name") or item.get("tool_id")
            if not isinstance(tool_name, str) or not tool_name.strip():
                continue
            tool_name = tool_name.strip()

            description = item.get("description")
            if not isinstance(description, str) or not description.strip():
                description = f"MCP tool {tool_name}"

            parameters = (
                item.get("inputSchema")
                or item.get("input_schema")
                or item.get("parameters")
                or item.get("schema")
            )
            if not isinstance(parameters, dict):
                parameters = {"type": "object", "additionalProperties": True}

            function_schema = {
                "type": "function",
                "function": {
                    "name": tool_name,
                    "description": description,
                    "parameters": parameters,
                },
            }

            discovered.append(
                {
                    "tool_id": tool_name,
                    "encryption": "aes-256-gcm",
                    "schema_ref": json.dumps(function_schema, separators=(",", ":")),
                    "executor": "mcp_proxy",
                }
            )

        return discovered

    async def _discover_mcp_tools(self) -> List[Dict[str, Any]]:
        """
        Discover MCP tools on startup.

        Supported response shapes:
        - {"tools": [...]}
        - {"result": {"tools": [...]}} (JSON-RPC style)
        - [...]
        """
        endpoint = (constants.MCP_TOOL_ENDPOINT or "").strip()
        if not endpoint or not self.http_session:
            return []

        parsed = urlparse(endpoint)
        if not parsed.scheme or not parsed.netloc:
            logger.warning("Skipping MCP discovery; endpoint is not a valid URL: %r", endpoint)
            return []

        base = f"{parsed.scheme}://{parsed.netloc}"
        path = parsed.path.rstrip("/")
        candidates: List[str] = []
        explicit = (constants.MCP_TOOL_DISCOVERY_URL or "").strip()
        if explicit:
            candidates.append(explicit)
        if path:
            candidates.append(f"{base}{path}/list")
        candidates.extend([
            f"{base}/tools",
            f"{base}/tools/list",
            f"{base}/tool/list",
        ])

        # De-duplicate while preserving order.
        seen = set()
        deduped_candidates: List[str] = []
        for url in candidates:
            if url not in seen:
                seen.add(url)
                deduped_candidates.append(url)

        for url in deduped_candidates:
            try:
                async with self.http_session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    if resp.status >= 400:
                        continue
                    data = await resp.json(content_type=None)
                    discovered = self._extract_discovered_tools(data)
                    if discovered:
                        logger.info(
                            "Discovered %d MCP tools from %s: %s",
                            len(discovered),
                            url,
                            [tool["tool_id"] for tool in discovered],
                        )
                        return discovered
            except Exception:
                continue

        # JSON-RPC fallback: call tools/list on the configured endpoint itself.
        json_rpc_payload = {
            "jsonrpc": "2.0",
            "id": "tensorminer-tools-list",
            "method": "tools/list",
            "params": {},
        }
        try:
            async with self.http_session.post(
                endpoint,
                json=json_rpc_payload,
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                if resp.status < 400:
                    data = await resp.json(content_type=None)
                    discovered = self._extract_discovered_tools(data)
                    if discovered:
                        logger.info(
                            "Discovered %d MCP tools via JSON-RPC on %s: %s",
                            len(discovered),
                            endpoint,
                            [tool["tool_id"] for tool in discovered],
                        )
                        return discovered
        except Exception:
            pass

        logger.warning("MCP discovery found no tools for endpoint: %s", endpoint)
        return []

    async def _refresh_worker_tools_from_discovery(self) -> None:
        """
        Build runtime tool catalog before WS connect.
        Static WORKER_TOOLS_JSON is base; discovered MCP tools replace static mcp_proxy entries.
        """
        static_tools = [
            tool for tool in constants.WORKER_TOOLS if isinstance(tool, dict)
        ]
        discovered_mcp_tools = await self._discover_mcp_tools()

        if discovered_mcp_tools:
            non_mcp_static = []
            for tool in static_tools:
                executor = tool.get("executor")
                if isinstance(executor, str) and executor.strip().lower() == "mcp_proxy":
                    continue
                non_mcp_static.append(tool)
            merged_tools = [*non_mcp_static, *discovered_mcp_tools]
            self._set_worker_tools_from_configs(merged_tools)
            logger.info(
                "Worker tool catalog refreshed: %d static + %d discovered MCP",
                len(non_mcp_static),
                len(discovered_mcp_tools),
            )
            return

        self._set_worker_tools_from_configs(static_tools)
        if self.worker_tools:
            logger.info(
                "Worker tool catalog initialized with static tools: %s",
                [tool["tool_id"] for tool in self.worker_tools],
            )
        else:
            logger.info("Worker tool catalog is empty")

    async def start(self):
        """Connect to broker and start worker loop"""
        self.running = True
        if self.http_session is None or self.http_session.closed:
            self.http_session = aiohttp.ClientSession()
        await self._refresh_worker_tools_from_discovery()
        reconnect_delay = 1  # Start with 1 second, exponential backoff
        max_reconnect_delay = 60  # Cap at 60 seconds

        # Store event loop for thread-safe callbacks from proof_collector
        self._loop = asyncio.get_event_loop()

        # W4: Register solution callback with proof_collector for mining sidecar
        if self.mining_enabled and self.proof_collector:
            self.proof_collector.set_solution_callback(self._on_solution_received)
            logger.info("Registered solution callback for mining sidecar")
            # Shares are emitted by the worker sampler directly via
            # _send_mine_share_typed; they do not arrive through
            # ProofCollector. The earlier slice-10 share_callback path
            # was based on classifying proofs by their embedded target,
            # which is always the model-adjusted block target — that
            # never worked. Removed.

        # W2: Register public key early if AGENT_ID is already configured.
        # If AGENT_ID is not configured, we auto-resolve it from broker ACK (API-key introspection).
        if self.crypto_service:
            await self._ensure_public_key_registered()

        try:
            while self.running:
                backoff = False
                try:
                    self.connection_attempts += 1
                    self.last_connect_attempt_at = time.time()
                    await self._connect_and_run()
                    # If _connect_and_run returns normally, the connection was closed
                    # This happens when broker closes connection gracefully
                    logger.warning("WebSocket connection closed normally, reconnecting...")
                    reconnect_delay = 1  # Reset delay - this was a clean disconnect
                except asyncio.CancelledError:
                    logger.warning("Broker worker client start task cancelled")
                    raise
                except websockets.exceptions.ConnectionClosed as e:
                    # Connection closed by broker (graceful or error)
                    logger.warning(f"WebSocket connection closed: code={e.code}, reason={e.reason}")
                    self.last_reconnect_error = f"ConnectionClosed:{e.code}:{e.reason}"
                    reconnect_delay = 1  # Fast reconnect on clean close
                except websockets.exceptions.InvalidStatusCode as e:
                    logger.error(f"WebSocket connection rejected: HTTP {e.status_code}")
                    self.last_reconnect_error = f"InvalidStatusCode:{e.status_code}"
                    backoff = True
                except (websockets.exceptions.WebSocketException, ConnectionError, OSError) as e:
                    logger.error(f"WebSocket connection error: {e}")
                    self.last_reconnect_error = f"{type(e).__name__}:{e}"
                    backoff = True
                except Exception as e:
                    logger.exception(f"Unexpected worker error: {type(e).__name__}: {e}")
                    self.last_reconnect_error = f"{type(e).__name__}:{e}"
                    backoff = True
                finally:
                    self.last_disconnected_at = time.time()
                    self._cleanup_connection()

                if self.running:
                    delay = reconnect_delay if backoff else 1
                    logger.info(f"Reconnecting in {delay} seconds...")
                    await asyncio.sleep(delay)
                    if backoff:
                        reconnect_delay = min(reconnect_delay * 2, max_reconnect_delay)
                    else:
                        reconnect_delay = 1
        finally:
            self._cleanup_connection()

    def _cleanup_connection(self):
        """Clean up connection state before reconnecting"""
        # Cancel heartbeat task
        if self.heartbeat_task and not self.heartbeat_task.done():
            self.heartbeat_task.cancel()
            self.heartbeat_task = None

        # Clear websocket reference
        self.ws = None

        # Clear active jobs - they'll be lost on reconnect anyway
        if self.active_jobs:
            logger.warning(f"Clearing {len(self.active_jobs)} active jobs due to connection loss")
            self.active_jobs.clear()

    async def stop(self):
        """Stop the worker client gracefully"""
        logger.info("Stopping broker worker client...")
        self.running = False
        ws = self.ws

        # W3: Unregister tools before disconnecting
        if self.ws and self.registered_tools:
            try:
                await self._unregister_tools()
            except Exception as e:
                logger.warning(f"Failed to unregister tools on shutdown: {e}")

        # W4: Clear solution callback and mining mappings
        if self.proof_collector:
            self.proof_collector.set_solution_callback(None)
        self.mining_job_mapping.clear()
        self.mining_request_mapping.clear()

        self._cleanup_connection()

        if ws:
            try:
                await ws.close()
            except Exception:
                pass

        if self.http_session:
            await self.http_session.close()
            self.http_session = None

        logger.info("Broker worker client stopped")

    async def _connect_and_run(self):
        """Establish WebSocket connection and handle messages"""
        # Build authentication headers
        headers = {}

        if self.jwt_token:
            if not self.jwt_token.startswith("eyJ"):
                logger.warning("PROVIDER_JWT_TOKEN doesn't look like a JWT (should start with 'eyJ')")
            headers["Authorization"] = f"Bearer {self.jwt_token}"
            logger.info(f"Using JWT authentication (token length: {len(self.jwt_token)}, prefix: {self.jwt_token[:20]}...)")
        elif self.worker_token:
            # Fallback to shared secret (dev mode only - disabled in prod)
            headers["X-Worker-Token"] = self.worker_token
            logger.info(f"Using shared secret authentication (token length: {len(self.worker_token)})")
        else:
            logger.warning("No authentication token configured - connection will fail")

        logger.info(f"Connecting to broker at {self.broker_url}")
        logger.info(f"Sending headers: {list(headers.keys())}")

        # Use websockets library with additional_headers for proper header handling
        async with websockets.connect(
            self.broker_url,
            additional_headers=headers,
            ping_interval=20,  # Send ping every 20s
            ping_timeout=30,   # Wait 30s for pong; app heartbeat handles stale sockets
            close_timeout=5,
        ) as ws:
            self.ws = ws
            self.last_connected_at = time.time()
            self.last_message_at = self.last_connected_at
            logger.info(f"Connected to broker as worker {self.worker_id}")

            # Send HELLO registration
            await self._send_hello()

            # Message loop - exits when connection closes
            try:
                async for message in ws:
                    try:
                        self.last_message_at = time.time()
                        msg = json.loads(message)
                        await self._handle_message(msg)
                    except json.JSONDecodeError as e:
                        logger.error(f"Failed to parse message: {e}")
            except websockets.exceptions.ConnectionClosed as e:
                logger.warning(f"Connection closed during message loop: code={e.code}, reason={e.reason}")
                raise  # Re-raise to trigger reconnection

            # If we exit the loop without exception, connection was closed normally
            logger.info("Message loop exited - connection closed by broker")

    async def _send_hello(self):
        """Register with broker"""
        # W2: Only advertise confidential mode if actually enabled and available
        # Otherwise broker may route confidential jobs to workers that can't handle them.
        # NOTE: `inference_modes` is the top-level capabilities.modes used by the
        # broker for plaintext/confidential model routing. Mining modes live under
        # capabilities.mining.supported_modes and must NOT clobber this variable.
        inference_modes = ["plaintext"]
        if self.crypto_service and constants.CONFIDENTIAL_MODE_ENABLED:
            inference_modes.append("confidential")

        # Only advertise pow_injection when mining is actually wired up;
        # otherwise the broker may route mining work that we will refuse.
        features = ["streaming"]
        if self.mining_enabled:
            features.append("pow_injection")
        features.append("responses")
        # Native confidential Responses streaming: this worker encrypts each
        # native response.* event into `encrypted_response_event` frames rather
        # than falling back to chat-completions framing. The broker gates the
        # `api: "responses"` worker contract on this flag so confidential jobs
        # are only sent the native Responses contract to workers that can keep
        # the stream encrypted end-to-end (older workers get downgraded to the
        # proven chat-completions confidential framing instead).
        if self.crypto_service and constants.CONFIDENTIAL_MODE_ENABLED:
            features.append("confidential_responses")

        # Phase 3: v2 mining capability shape. The Phase 4 MiningScheduler
        # reads schema_version + networks + supported_modes + max_parallel
        # to decide whether and how to dispatch mining work; without
        # these fields the scheduler MUST treat the worker as
        # mining_capable=False even if `enabled` is true. See
        # COMPUTE_BROKER_IMPROV.md §"Effective Runtime Config".
        mining_capability = None
        if self.mining_enabled:
            # mining_modes follows the worker's actual capabilities:
            # - dummy_only: existing miner-proxy dummy loop generates
            #   synthetic low-priority mining requests (proxy.py).
            # - request_attached: PoW context is injected into real user
            #   inference (proxy.py:_inject_pow_data).
            # `disabled` is a broker-side state, not a worker capability.
            mining_modes = ["dummy_only", "request_attached"]
            mining_capability = {
                "enabled": True,
                "schema_version": 2,
                "networks": list(constants.MINING_NETWORKS),
                "supported_modes": mining_modes,
                # Slice 9: the worker accepts a MODEL_REGISTRY_SYNC
                # frame from the broker and feeds it into
                # ModelClient.update_from_payload — same code path as
                # the local /api/v1/models fetch, just sourced from
                # the broker over WS. The broker's dispatch gate
                # REQUIRES this flag True before sending share work.
                "supports_broker_registry": True,
                # supports_solution_return=True: _forward_solution sends
                # MINE_RESULT to the broker rather than direct to Core Node.
                "supports_solution_return": True,
                "max_parallel": int(constants.MINING_MAX_PARALLEL),
            }

        # Single introspection drives BOTH the model list and the advertised
        # max_context_window. Source of truth is the backend itself:
        #   - vllm: /v1/models per-model max_model_len
        #   - llama.cpp: /props default_generation_settings.n_ctx
        # Env MAX_CONTEXT_WINDOW is the fallback ONLY when the operator
        # explicitly pinned it (MAX_CONTEXT_WINDOW_EXPLICIT). Without that
        # pin, the historic 128000 default would let the broker route
        # 100k-token requests to a worker capped at 16k → vllm 400 mid-
        # stream. We retry with backoff to ride out cold-start; if every
        # attempt still fails AND the operator didn't pin the env, we
        # REFUSE to HELLO rather than register with a lie.
        models_info: Dict[str, Optional[int]] = {}
        backoff = 1.0
        for attempt in range(5):
            models_info = await self._get_models_with_context()
            if models_info and any(
                isinstance(v, int) and v > 0 for v in models_info.values()
            ):
                break
            if constants.MAX_CONTEXT_WINDOW_EXPLICIT:
                # Operator-pinned env is trustworthy → don't waste cycles retrying.
                break
            logger.warning(
                "HELLO blocked: no usable max_context from /v1/models or /props "
                "(attempt %d/5) and MAX_CONTEXT_WINDOW env is unset. "
                "Sleeping %.1fs before retry.",
                attempt + 1, backoff,
            )
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)

        if models_info:
            # Dual-backend workers serve the chain-pinned MINING model
            # (LOCAL_MODEL_NAME, re-exported by start-proxy.sh) on its own
            # backend for MINING ONLY. It must not be advertised as an
            # inference model, nor let its (smaller) context cap the
            # worker's single max_context_window — otherwise the real
            # inference model (e.g. the 27B at 32768) registers with the
            # mining model's tiny window. Broker mining dispatch keys on
            # the `mining` capability, not the models list, so dropping it
            # here is safe. Only applies when other models exist (never
            # strips the sole model of a single-model worker).
            mining_model = (constants.LOCAL_MODEL_NAME or "").strip()
            if (self.mining_enabled and mining_model
                    and len(models_info) > 1 and mining_model in models_info):
                inference_info = {
                    m: c for m, c in models_info.items() if m != mining_model
                }
            else:
                inference_info = models_info
            discovered_models = list(inference_info.keys())
            ctx_values = [v for v in inference_info.values() if isinstance(v, int) and v > 0]
        else:
            discovered_models = await self._get_available_models()
            ctx_values = []

        if ctx_values:
            effective_context = min(ctx_values)
            ctx_source = "backend-introspect"
        elif constants.MAX_CONTEXT_WINDOW_EXPLICIT:
            effective_context = constants.MAX_CONTEXT_WINDOW
            ctx_source = "env-pinned"
        else:
            # Last-resort gate: rather than advertise 128000 silently, refuse.
            # Raises out of _send_hello — the caller's retry/backoff loop will
            # re-attempt the connection, by which time vllm/llama may answer.
            raise RuntimeError(
                "Refusing HELLO: backend introspection failed and "
                "MAX_CONTEXT_WINDOW env is not pinned. Set "
                "MAX_CONTEXT_WINDOW=<n> in the worker env (e.g. to match "
                "vllm --max-model-len or llama-cpp -c) before reconnecting."
            )

        # Build capabilities with W2 modes support
        capabilities = {
            "compute_type": constants.COMPUTE_TYPE,
            "gpu_model": constants.GPU_MODEL,
            "memory_gb": constants.GPU_MEMORY_GB,
            "region": constants.WORKER_REGION,
            # Context/output limits for broker scheduling (required by broker)
            "max_context_window": effective_context,
            "max_context_tokens": effective_context,  # alias for compat
            "max_output_tokens": constants.MAX_OUTPUT_TOKENS,
            "features": features,
            "quantization": ["fp16", "int8"],
            # W2: Advertise supported inference modes for plaintext/confidential routing.
            # Mining modes live under capabilities.mining.supported_modes; the two
            # namespaces must stay separate.
            "modes": inference_modes,
            # Phase 3 v2 mining capability (None when mining_enabled=False;
            # the broker must NOT dispatch mining to a worker missing this).
            "mining": mining_capability,
        }

        # Remove None values from capabilities
        capabilities = {k: v for k, v in capabilities.items() if v is not None}

        hello_msg = {
            "type": "HELLO",
            "worker_id": self.worker_id,
            "models": discovered_models,
            "capacity": constants.WORKER_CAPACITY,
            "capabilities": capabilities
        }
        await self.ws.send(json.dumps(hello_msg))
        logger.info(
            "Sent HELLO: %d models, capacity=%s, max_context_window=%s (source=%s), modes=%s",
            len(discovered_models),
            constants.WORKER_CAPACITY,
            effective_context,
            ctx_source,
            capabilities["modes"],
        )

    async def _get_models_with_context(self) -> Dict[str, Optional[int]]:
        """Introspect upstream for {model_id: max_context_tokens}.

        Tries two endpoints in order so this works across backends:
          1. ``/v1/models`` per-model ``max_model_len`` — vllm's ModelCard
             carries it (entrypoints/openai/models/serving.py); the local
             proxy passes /v1/models through verbatim (components/proxy.py
             _handle_models_request).
          2. ``/props`` ``default_generation_settings.n_ctx`` — llama.cpp
             server exposes the loaded context window here. Plain OpenAI
             /v1/models on llama-cpp doesn't carry context info, so /props
             is the only structured source for Hermes / GGUF backends.

        Returns an empty dict on any failure — caller falls back to
        _get_available_models() for IDs and env constant for context.
        Runs even in STANDALONE_MODE: the standalone flag governs chain
        integration, not vllm reachability — the inference backend is
        normally up by the time HELLO is sent.
        """
        out: Dict[str, Optional[int]] = {}

        # Pass 1: /v1/models (vllm route — has max_model_len per model).
        try:
            async with self.http_session.get(
                f"{self.miner_proxy_url}/v1/models",
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    for m in data.get("data", []):
                        mid = m.get("id")
                        if not isinstance(mid, str) or not mid:
                            continue
                        raw = m.get("max_model_len")
                        out[mid] = int(raw) if isinstance(raw, (int, float)) and raw > 0 else None
                else:
                    logger.debug(
                        "/v1/models returned status=%s; trying /props",
                        resp.status,
                    )
        except Exception as e:
            logger.debug(f"/v1/models introspection failed: {e}; trying /props")

        # Pass 2: /props (llama.cpp route — has default_generation_settings.n_ctx).
        # Only consult when we have model ids but no context numbers, OR when we
        # have no models at all. llama-server returns one n_ctx for the whole
        # process (single-model backend), so we apply it to every known model.
        needs_context = (not out) or all(v is None for v in out.values())
        if needs_context:
            try:
                async with self.http_session.get(
                    f"{self.miner_proxy_url}/props",
                    timeout=aiohttp.ClientTimeout(total=5),
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        gen = data.get("default_generation_settings") or {}
                        n_ctx = gen.get("n_ctx")
                        if isinstance(n_ctx, (int, float)) and n_ctx > 0:
                            n_ctx_int = int(n_ctx)
                            if out:
                                # Backfill nulls from /v1/models with /props value.
                                for k, v in out.items():
                                    if v is None:
                                        out[k] = n_ctx_int
                            else:
                                # /v1/models didn't even give us ids — fall back to the
                                # configured local model name. STANDALONE_MODE is the
                                # common case here for llama-cpp single-model setups.
                                fallback_id = (
                                    constants.LOCAL_MODEL_NAME
                                    if constants.STANDALONE_MODE and constants.LOCAL_MODEL_NAME
                                    else "model"
                                )
                                out[fallback_id] = n_ctx_int
            except Exception as e:
                logger.debug(f"/props introspection failed: {e}")

        return out

    async def _get_available_models(self) -> List[str]:
        """Get list of available models from upstream with validation"""
        # In standalone mode, use the configured LOCAL_MODEL_NAME
        if constants.STANDALONE_MODE and constants.LOCAL_MODEL_NAME:
            logger.info(f"Standalone mode: using configured model '{constants.LOCAL_MODEL_NAME}'")
            return [constants.LOCAL_MODEL_NAME]

        try:
            # First try the standard OpenAI models endpoint
            async with self.http_session.get(
                f"{self.miner_proxy_url}/v1/models",
                timeout=aiohttp.ClientTimeout(total=5)
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    models = [m["id"] for m in data.get("data", [])]
                    if models:
                        logger.info(f"Discovered {len(models)} models from /v1/models: {models}")
                        return models
                    else:
                        logger.warning("No models returned from /v1/models endpoint")
        except Exception as e:
            logger.warning(f"Failed to get models from /v1/models endpoint: {e}")

        # Try status endpoint for additional model info
        try:
            async with self.http_session.get(
                f"{self.miner_proxy_url}/status",
                timeout=aiohttp.ClientTimeout(total=5)
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    # Check if proxy status includes model information
                    proxy_info = data.get("proxy", {})
                    if "model" in proxy_info or "models" in proxy_info:
                        logger.info(f"Found model info in status endpoint: {proxy_info}")
        except Exception as e:
            logger.debug(f"Status endpoint check failed: {e}")

        # Fallback to configured models with warning
        fallback_models = list(constants.FALLBACK_MODEL_CONFIGS.keys())
        logger.warning(f"Using fallback model configuration: {fallback_models}")
        logger.warning("This may not reflect the actual models available in vLLM")
        return fallback_models

    async def _handle_message(self, msg: Dict[str, Any]):
        """Route incoming messages by type"""
        msg_type = msg.get("type")
        logger.debug(f"Received message type: {msg_type}")

        if msg_type == "ACK":
            self.last_ack_at = time.time()
            # Broker ACK may include authoritative agent_id resolved from worker API key.
            # This removes the need for users to manually set AGENT_ID.
            ack_agent_id = (msg.get("agent_id") or "").strip()
            if self.crypto_service and ack_agent_id:
                current_agent_id = self.crypto_service.get_agent_id()
                if current_agent_id != ack_agent_id:
                    if current_agent_id:
                        logger.warning(
                            "Overriding configured AGENT_ID '%s' with broker-authoritative '%s'",
                            current_agent_id,
                            ack_agent_id,
                        )
                    else:
                        logger.info("Resolved AGENT_ID from broker ACK: %s", ack_agent_id)
                    self.crypto_service.set_agent_id(ack_agent_id)
                    # Agent binding changed/initialized - ensure key is (re)registered.
                    self.public_key_registered = False

            # Update heartbeat interval and start periodic heartbeats
            self.heartbeat_interval = int(msg.get("heartbeat_interval_sec", 15))
            logger.info(f"Received ACK, starting heartbeat with interval {self.heartbeat_interval}s")
            if self.heartbeat_task:
                self.heartbeat_task.cancel()
            self.heartbeat_task = asyncio.create_task(self._heartbeat_loop(self.ws))

            # Ensure key registration once agent_id is known.
            if self.crypto_service and not self.public_key_registered:
                await self._ensure_public_key_registered()

            # W3: Register tools after ACK
            await self._register_tools()
        elif msg_type == "CHALLENGE":
            await self._handle_challenge(msg)
        elif msg_type == "START":
            # Handle job in background to not block message loop
            asyncio.create_task(self._handle_job_start(msg))
        elif msg_type == "PROOF_REQUEST":
            asyncio.create_task(self._handle_proof_request(msg))
        # W3: Tool registration acknowledgment
        elif msg_type == "TOOL_REGISTER_ACK":
            tool_id = msg.get("tool_id")
            status = msg.get("status", "")
            if status == "registered":
                self.registered_tools.add(tool_id)
                logger.info(f"Tool registered successfully: {tool_id}")
            else:
                error = msg.get("error", "unknown")
                logger.warning(f"Tool registration failed for {tool_id}: status={status}, error={error}")
        elif msg_type == "TOOL_UNREGISTER_ACK":
            tool_id = msg.get("tool_id")
            self.registered_tools.discard(tool_id)
            logger.info(f"Tool unregistered: {tool_id}")
        elif msg_type == "TOOL_INVOKE":
            asyncio.create_task(self._handle_tool_invoke(msg))
        # W4: Mining sidecar - receive mining job from broker
        elif msg_type == "MINE_REQUEST":
            asyncio.create_task(self._handle_mine_request(msg))
        # Slice 9: broker-pushed chain model registry. Same JSON shape
        # as MODEL_API_URL/api/v1/models?extended=true, just sourced
        # from the broker over WS. We forward to ModelClient through
        # the same code path as the local fetch.
        elif msg_type == "MODEL_REGISTRY_SYNC":
            asyncio.create_task(self._handle_model_registry_sync(msg))
        else:
            logger.warning(f"Unknown message type: {msg_type}")

    async def _ensure_public_key_registered(self) -> None:
        """Register worker public key when confidential mode is enabled and agent_id is known."""
        if not self.crypto_service or not self.http_session:
            return

        agent_id = self.crypto_service.get_agent_id()
        if not agent_id:
            logger.info("Waiting for broker ACK to provide AGENT_ID before key registration")
            return

        logger.info(f"Registering public key with auth-service for agent {agent_id}...")
        registered = await self.crypto_service.register_public_key(self.http_session)
        self.public_key_registered = bool(registered)
        if registered:
            logger.info("Public key registration successful")
        else:
            logger.warning("Public key registration failed - confidential jobs may fail")

    def _extract_confidential_context(self, payload: Dict[str, Any]) -> tuple[Optional[str], Optional[int]]:
        """
        Extract room context for confidential CEK lookup.

        Priority:
        1) payload.encryption.{room_id, epoch} (canonical gateway contract)
        2) payload.thread_id / payload.room_id (e2e fallback compatibility)
        """
        encryption_meta = payload.get("encryption") if isinstance(payload.get("encryption"), dict) else {}

        room_id = (
            encryption_meta.get("room_id")
            or payload.get("thread_id")
            or payload.get("room_id")
        )
        room_id = room_id.strip() if isinstance(room_id, str) else None

        epoch_raw = encryption_meta.get("epoch")
        if epoch_raw is None:
            epoch_raw = payload.get("epoch")
        try:
            epoch = int(epoch_raw) if epoch_raw is not None else None
        except (TypeError, ValueError):
            epoch = None

        return room_id, epoch

    def _normalize_decrypted_payload(self, decrypted_payload: Any) -> Optional[Dict[str, Any]]:
        """
        Normalize decrypted confidential payload to an OpenAI request object.

        Client encrypts MessagePlaintext envelope:
          {"v": 1, "text": "{\"messages\":[...], ...}"}
        Worker needs the inner JSON request for local inference APIs.
        """
        if not isinstance(decrypted_payload, dict):
            return None

        # Already in request shape.
        if isinstance(decrypted_payload.get("messages"), list) or "input" in decrypted_payload:
            return decrypted_payload

        # MessagePlaintext envelope shape from client confidential flow.
        text_payload = decrypted_payload.get("text")
        if isinstance(text_payload, str):
            try:
                inner = json.loads(text_payload)
                if isinstance(inner, dict):
                    return inner
            except Exception:
                logger.warning("Failed to parse decrypted payload.text as JSON")

        # Defensive: nested wrapper variants.
        plaintext = decrypted_payload.get("plaintext")
        if isinstance(plaintext, dict):
            if isinstance(plaintext.get("messages"), list) or "input" in plaintext:
                return plaintext
            inner_text = plaintext.get("text")
            if isinstance(inner_text, str):
                try:
                    inner = json.loads(inner_text)
                    if isinstance(inner, dict):
                        return inner
                except Exception:
                    logger.warning("Failed to parse decrypted payload.plaintext.text as JSON")

        return None

    def _prune_confidential_runs(self) -> None:
        """Drop expired confidential run state entries."""
        now = time.time()
        stale = [
            run_id
            for run_id, state in self.confidential_runs.items()
            if now - float(state.get("updated_at", 0)) > self.confidential_run_ttl_sec
        ]
        for run_id in stale:
            self.confidential_runs.pop(run_id, None)

    def _remember_confidential_run_payload(self, run_id: Optional[str], payload: Dict[str, Any]) -> None:
        """Persist latest normalized payload for confidential run continuation."""
        if not run_id:
            return

        try:
            payload_snapshot = json.loads(json.dumps(payload))
        except Exception:
            logger.warning("Failed to snapshot confidential payload for run %s", run_id)
            return

        self._prune_confidential_runs()
        existing = self.confidential_runs.get(run_id) or {}
        tool_calls = existing.get("tool_calls", {})
        if not isinstance(tool_calls, dict):
            tool_calls = {}

        pending_local = existing.get("pending_local_tool_results")
        if not isinstance(pending_local, list):
            pending_local = []

        self.confidential_runs[run_id] = {
            "payload": payload_snapshot,
            "tool_calls": tool_calls,
            "pending_local_tool_results": pending_local,
            "updated_at": time.time(),
        }

    def _remember_confidential_tool_call(
        self,
        run_id: Optional[str],
        tool_call_id: str,
        tool_name: str,
        args: Dict[str, Any],
    ) -> None:
        """Store latest tool_call metadata so follow-up tool_result can be replayed."""
        if not run_id:
            return

        state = self.confidential_runs.get(run_id)
        if not state:
            return

        tool_calls = state.get("tool_calls")
        if not isinstance(tool_calls, dict):
            tool_calls = {}
            state["tool_calls"] = tool_calls

        tool_calls[tool_call_id] = {
            "name": tool_name,
            "args": args if isinstance(args, dict) else {},
        }
        state["updated_at"] = time.time()

    def _discard_confidential_run(self, run_id: Optional[str]) -> None:
        """Remove confidential run state after final completion or unrecoverable error."""
        if run_id:
            self.confidential_runs.pop(run_id, None)

    def _buffer_local_tool_results_for_continuation(
        self,
        run_id: Optional[str],
        tool_results: List[Dict[str, Any]],
    ) -> None:
        """
        Persist locally executed tool results so they can be merged with a later
        remote tool_result continuation for mixed local+remote tool turns.
        """
        if not run_id or not tool_results:
            return

        state = self.confidential_runs.get(run_id)
        if not state:
            return

        existing = state.get("pending_local_tool_results")
        if not isinstance(existing, list):
            existing = []

        merged: List[Dict[str, Any]] = []
        seen_tool_call_ids: set[str] = set()

        for item in existing + tool_results:
            if not isinstance(item, dict):
                continue
            tool_call_id = item.get("tool_call_id")
            if not isinstance(tool_call_id, str) or not tool_call_id:
                continue
            if tool_call_id in seen_tool_call_ids:
                continue
            seen_tool_call_ids.add(tool_call_id)
            merged.append(
                {
                    "tool_call_id": tool_call_id,
                    "tool_id": item.get("tool_id"),
                    "result": item.get("result"),
                }
            )

        state["pending_local_tool_results"] = merged
        state["updated_at"] = time.time()

    def _consume_buffered_local_tool_results(self, run_id: Optional[str]) -> List[Dict[str, Any]]:
        """Pop buffered local tool results for a run."""
        if not run_id:
            return []

        state = self.confidential_runs.get(run_id)
        if not state:
            return []

        buffered = state.get("pending_local_tool_results")
        if not isinstance(buffered, list):
            state["pending_local_tool_results"] = []
            return []

        state["pending_local_tool_results"] = []
        state["updated_at"] = time.time()
        return buffered

    def _build_continuation_payload_from_tool_result(
        self,
        run_id: Optional[str],
        tool_call_id: Optional[str],
        decrypted_tool_result: Any,
    ) -> Optional[Dict[str, Any]]:
        """
        Build a follow-up messages payload by appending assistant tool_call + tool result
        to cached run history.
        """
        if not run_id:
            return None

        state = self.confidential_runs.get(run_id)
        if not state:
            logger.warning("No confidential run state available for continuation: run_id=%s", run_id)
            return None

        payload = state.get("payload")
        if not isinstance(payload, dict):
            return None

        # Native Responses runs are input-shaped (no `messages`); the
        # messages-based continuation below cannot represent them, so they are
        # routed to the Responses-native builder once the tool results are
        # assembled (see the shape branch after dedup).
        is_responses_shape = not isinstance(payload.get("messages"), list) and "input" in payload
        if not is_responses_shape and not isinstance(payload.get("messages"), list):
            return None

        tool_calls = state.get("tool_calls", {})
        tool_meta = tool_calls.get(tool_call_id, {}) if isinstance(tool_calls, dict) else {}
        tool_name = tool_meta.get("name") if isinstance(tool_meta, dict) else None
        if not isinstance(tool_name, str) or not tool_name:
            tool_name = "unknown_tool"
        tool_args = tool_meta.get("args") if isinstance(tool_meta, dict) else {}
        if not isinstance(tool_args, dict):
            tool_args = {}

        if isinstance(decrypted_tool_result, dict):
            result_payload = decrypted_tool_result
        else:
            result_payload = {"success": True, "result": decrypted_tool_result}

        resolved_tool_call_id = (
            tool_call_id if isinstance(tool_call_id, str) and tool_call_id else f"tool_call_{uuid.uuid4().hex[:8]}"
        )

        buffered_local_tool_results = self._consume_buffered_local_tool_results(run_id)
        combined_tool_results = list(buffered_local_tool_results)
        combined_tool_results.append(
            {
                "tool_call_id": resolved_tool_call_id,
                "tool_id": tool_name,
                "result": result_payload,
            }
        )

        # Preserve order while avoiding duplicate tool_call_ids.
        deduped_results: List[Dict[str, Any]] = []
        seen_tool_call_ids: set[str] = set()
        for item in combined_tool_results:
            if not isinstance(item, dict):
                continue
            item_tool_call_id = item.get("tool_call_id")
            if not isinstance(item_tool_call_id, str) or not item_tool_call_id:
                continue
            if item_tool_call_id in seen_tool_call_ids:
                continue
            seen_tool_call_ids.add(item_tool_call_id)
            deduped_results.append(item)

        if len(deduped_results) > 1:
            logger.warning(
                "Guard: merged mixed local+remote tool continuation payload "
                "(run_id=%s tool_results=%s)",
                run_id,
                len(deduped_results),
            )

        # Native Responses run: build an input-shaped continuation (function_call
        # + function_call_output items) instead of the messages-shaped one.
        if is_responses_shape:
            responses_continuation = self._build_responses_continuation_payload_from_tool_results(
                run_id, deduped_results
            )
            if responses_continuation:
                return responses_continuation
            if buffered_local_tool_results:
                self._buffer_local_tool_results_for_continuation(run_id, buffered_local_tool_results)
            return None

        continuation_payload = self._build_continuation_payload_from_tool_results(
            run_id,
            deduped_results,
            drop_tools=False,
        )
        if continuation_payload:
            return continuation_payload

        # If merge-path continuation fails, restore buffered local results so they
        # can still be recovered by a later continuation attempt.
        if buffered_local_tool_results:
            self._buffer_local_tool_results_for_continuation(run_id, buffered_local_tool_results)

        # Fallback to legacy single-tool behavior for safety if merge path fails.
        assistant_tool_call = {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": resolved_tool_call_id,
                    "type": "function",
                    "function": {
                        "name": tool_name,
                        "arguments": json.dumps(tool_args, ensure_ascii=False),
                    },
                }
            ],
        }

        if isinstance(result_payload.get("result"), str):
            tool_content = result_payload.get("result")
        else:
            try:
                tool_content = json.dumps(result_payload, ensure_ascii=False)
            except Exception:
                tool_content = str(result_payload)

        tool_result_message = {
            "role": "tool",
            "tool_call_id": assistant_tool_call["tool_calls"][0]["id"],
            "name": tool_name,
            "content": tool_content,
        }

        updated_payload = json.loads(json.dumps(payload))
        updated_messages = updated_payload.get("messages")
        if not isinstance(updated_messages, list):
            return None

        updated_messages.append(assistant_tool_call)
        updated_messages.append(tool_result_message)
        updated_payload["stream"] = True

        self._remember_confidential_run_payload(run_id, updated_payload)
        return updated_payload

    def _build_continuation_payload_from_tool_results(
        self,
        run_id: Optional[str],
        tool_results: List[Dict[str, Any]],
        drop_tools: bool = True,
    ) -> Optional[Dict[str, Any]]:
        """
        Build follow-up payload by appending assistant tool_calls + tool outputs for
        all locally executed tools in a single assistant turn.
        """
        if not run_id or not tool_results:
            return None

        state = self.confidential_runs.get(run_id)
        if not state:
            logger.warning("No confidential run state available for local continuation: run_id=%s", run_id)
            return None

        payload = state.get("payload")
        if not isinstance(payload, dict):
            return None

        messages = payload.get("messages")
        if not isinstance(messages, list):
            return None

        tool_calls_meta = state.get("tool_calls", {})
        if not isinstance(tool_calls_meta, dict):
            tool_calls_meta = {}

        assistant_tool_calls: List[Dict[str, Any]] = []
        tool_messages: List[Dict[str, Any]] = []

        for item in tool_results:
            tool_call_id = item.get("tool_call_id")
            if not isinstance(tool_call_id, str) or not tool_call_id:
                tool_call_id = f"tool_call_{uuid.uuid4().hex[:8]}"

            tool_meta = tool_calls_meta.get(tool_call_id, {})
            tool_name = tool_meta.get("name") if isinstance(tool_meta, dict) else None
            if not isinstance(tool_name, str) or not tool_name:
                tool_name = item.get("tool_id") if isinstance(item.get("tool_id"), str) else "unknown_tool"

            tool_args = tool_meta.get("args") if isinstance(tool_meta, dict) else {}
            if not isinstance(tool_args, dict):
                tool_args = {}

            result_payload = item.get("result")
            if not isinstance(result_payload, dict):
                result_payload = {"success": True, "result": result_payload}

            assistant_tool_calls.append(
                {
                    "id": tool_call_id,
                    "type": "function",
                    "function": {
                        "name": tool_name,
                        "arguments": json.dumps(tool_args, ensure_ascii=False),
                    },
                }
            )

            if isinstance(result_payload.get("result"), str):
                tool_content = result_payload["result"]
            else:
                try:
                    tool_content = json.dumps(result_payload, ensure_ascii=False)
                except Exception:
                    tool_content = str(result_payload)

            tool_messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_call_id,
                    "name": tool_name,
                    "content": tool_content,
                }
            )

        if not assistant_tool_calls:
            return None

        updated_payload = json.loads(json.dumps(payload))
        updated_messages = updated_payload.get("messages")
        if not isinstance(updated_messages, list):
            return None

        updated_messages.append(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": assistant_tool_calls,
            }
        )
        updated_messages.extend(tool_messages)
        updated_payload["stream"] = True
        if drop_tools:
            # Local-only tool results are already resolved; force final assistant response
            # instead of re-entering tool planning.
            updated_payload.pop("tools", None)
            updated_payload.pop("tool_choice", None)

        self._remember_confidential_run_payload(run_id, updated_payload)
        return updated_payload

    def _build_responses_continuation_payload_from_tool_results(
        self,
        run_id: Optional[str],
        tool_results: List[Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        """Build an input-shaped (OpenAI Responses) continuation payload.

        The follow-up turn re-sends the prior ``input`` plus, for each resolved
        tool call, the assistant ``function_call`` item the model emitted and a
        matching ``function_call_output`` item carrying the tool result. This is
        the Responses-API analogue of the messages-shaped continuation and is
        what makes confidential tool calling work end-to-end on the native
        Responses path (remote client-executed AND worker-local tools).
        """
        if not run_id or not tool_results:
            return None

        state = self.confidential_runs.get(run_id)
        if not state:
            logger.warning("No confidential run state for Responses continuation: run_id=%s", run_id)
            return None

        payload = state.get("payload")
        if not isinstance(payload, dict):
            return None

        original_input = payload.get("input")
        if isinstance(original_input, str):
            input_items: List[Any] = [
                {"type": "message", "role": "user", "content": original_input}
            ]
        elif isinstance(original_input, list):
            input_items = list(original_input)
        else:
            logger.warning("Responses continuation: unsupported input shape for run_id=%s", run_id)
            return None

        tool_calls_meta = state.get("tool_calls", {})
        if not isinstance(tool_calls_meta, dict):
            tool_calls_meta = {}

        function_call_items: List[Dict[str, Any]] = []
        output_items: List[Dict[str, Any]] = []

        for item in tool_results:
            tool_call_id = item.get("tool_call_id")
            if not isinstance(tool_call_id, str) or not tool_call_id:
                tool_call_id = f"tool_call_{uuid.uuid4().hex[:8]}"

            meta = tool_calls_meta.get(tool_call_id, {})
            tool_name = meta.get("name") if isinstance(meta, dict) else None
            if not isinstance(tool_name, str) or not tool_name:
                tool_name = item.get("tool_id") if isinstance(item.get("tool_id"), str) else "unknown_tool"

            tool_args = meta.get("args") if isinstance(meta, dict) else {}
            if not isinstance(tool_args, dict):
                tool_args = {}

            result_payload = item.get("result")
            if not isinstance(result_payload, dict):
                result_payload = {"success": True, "result": result_payload}

            if isinstance(result_payload.get("result"), str):
                output_text = result_payload["result"]
            else:
                try:
                    output_text = json.dumps(result_payload, ensure_ascii=False)
                except Exception:
                    output_text = str(result_payload)

            function_call_items.append(
                {
                    "type": "function_call",
                    "call_id": tool_call_id,
                    "name": tool_name,
                    "arguments": json.dumps(tool_args, ensure_ascii=False),
                }
            )
            output_items.append(
                {
                    "type": "function_call_output",
                    "call_id": tool_call_id,
                    "output": output_text,
                }
            )

        if not function_call_items:
            return None

        updated_payload = json.loads(json.dumps(payload))
        updated_payload["input"] = input_items + function_call_items + output_items
        updated_payload["stream"] = True

        self._remember_confidential_run_payload(run_id, updated_payload)
        return updated_payload

    @staticmethod
    def _strip_agent_prefix(tool_id: str) -> str:
        """Strip agent_id prefix: 'agent_abc__file_search' -> 'file_search'."""
        if "__" in tool_id:
            return tool_id.split("__", 1)[1]
        return tool_id

    def _is_local_worker_tool(self, tool_id: str) -> bool:
        """Return True when tool is configured for local worker execution."""
        base_id = self._strip_agent_prefix(tool_id)
        tool = self.tool_configs.get(base_id)
        if not isinstance(tool, dict):
            return False
        executor = tool.get("executor")
        if isinstance(executor, str) and executor.strip().lower() in ("file_search", "mcp_proxy", "worker"):
            return True
        return False

    async def _execute_local_worker_tool(
        self,
        tool_id: str,
        args: Dict[str, Any],
        room_id: Optional[str],
        run_id: Optional[str],
    ) -> Dict[str, Any]:
        """
        Execute a registered worker tool locally and return tool-result payload
        in the format expected by continuation builder.
        """
        base_id = self._strip_agent_prefix(tool_id)
        if base_id == "file_search":
            return self._execute_local_file_search(args)
        return await self._execute_local_mcp_proxy(base_id, args, room_id, run_id)

    @staticmethod
    def _extract_tool_invoke_args(payload: Any) -> Optional[Dict[str, Any]]:
        """
        Normalize TOOL_INVOKE payloads into plain args dict.

        Accepts:
        - {"args": {...}}
        - {...} (already args)
        - {"text": "{\"args\": {...}"} (message envelope fallback)
        """
        if isinstance(payload, dict):
            args = payload.get("args")
            if isinstance(args, dict):
                return args

            text_payload = payload.get("text")
            if isinstance(text_payload, str):
                try:
                    inner = json.loads(text_payload)
                    if isinstance(inner, dict):
                        inner_args = inner.get("args")
                        if isinstance(inner_args, dict):
                            return inner_args
                        return inner
                except Exception:
                    pass

            return payload

        return None

    async def _handle_tool_invoke(self, msg: Dict[str, Any]) -> None:
        """
        Execute worker-local tool directly (outside START/job flow).

        Request frame:
        {
          "type": "TOOL_INVOKE",
          "invoke_id": "...",
          "tool_id": "agent__tool",
          "mode": "confidential" | "plaintext",
          "room_id": "...", "epoch": 1, "payload_b64": "..."   # confidential
          "args": {...},                                        # plaintext
          "run_id": "..."                                       # optional context
        }
        """
        invoke_id = msg.get("invoke_id")
        tool_id = msg.get("tool_id")
        mode_raw = msg.get("mode")
        mode = str(mode_raw).strip().lower() if isinstance(mode_raw, str) else "plaintext"
        if mode == "open":
            mode = "plaintext"
        run_id = msg.get("run_id") if isinstance(msg.get("run_id"), str) else None
        room_id = msg.get("room_id") if isinstance(msg.get("room_id"), str) else None

        async def _send_result(payload: Dict[str, Any]) -> None:
            if not self.ws:
                return
            try:
                await self.ws.send(json.dumps(payload))
            except Exception:
                logger.warning("Failed to send TOOL_INVOKE_RESULT for invoke_id=%s", invoke_id)

        if not isinstance(invoke_id, str) or not invoke_id:
            await _send_result(
                {
                    "type": "TOOL_INVOKE_RESULT",
                    "invoke_id": invoke_id,
                    "tool_id": tool_id,
                    "status": "error",
                    "error": {"code": "INVALID_INVOKE", "message": "invoke_id is required"},
                }
            )
            return

        if not isinstance(tool_id, str) or not tool_id:
            await _send_result(
                {
                    "type": "TOOL_INVOKE_RESULT",
                    "invoke_id": invoke_id,
                    "run_id": run_id,
                    "tool_id": tool_id,
                    "status": "error",
                    "error": {"code": "INVALID_TOOL", "message": "tool_id is required"},
                }
            )
            return

        if mode not in ("confidential", "plaintext"):
            await _send_result(
                {
                    "type": "TOOL_INVOKE_RESULT",
                    "invoke_id": invoke_id,
                    "run_id": run_id,
                    "tool_id": tool_id,
                    "status": "error",
                    "error": {"code": "INVALID_MODE", "message": f"unsupported mode: {mode}"},
                }
            )
            return

        if not self._is_local_worker_tool(tool_id):
            await _send_result(
                {
                    "type": "TOOL_INVOKE_RESULT",
                    "invoke_id": invoke_id,
                    "run_id": run_id,
                    "tool_id": tool_id,
                    "status": "error",
                    "error": {"code": "TOOL_NOT_LOCAL", "message": "tool is not configured for local worker execution"},
                }
            )
            return

        cek = None
        args: Dict[str, Any] = {}

        try:
            if mode == "confidential":
                if not self.crypto_service:
                    raise ValueError("worker is not configured for confidential mode")
                if not room_id:
                    raise ValueError("room_id is required for confidential invoke")
                if not self.http_session:
                    raise ValueError("HTTP session is not initialized")

                epoch_raw = msg.get("epoch")
                try:
                    epoch = int(epoch_raw)
                except (TypeError, ValueError):
                    raise ValueError("valid epoch is required for confidential invoke")

                encrypted_payload = msg.get("payload_b64")
                if not isinstance(encrypted_payload, str) or not encrypted_payload:
                    raise ValueError("payload_b64 is required for confidential invoke")

                cek = await self.crypto_service.fetch_cek(self.http_session, room_id, epoch)
                if not cek:
                    raise RuntimeError("failed to fetch room encryption key")

                decrypted_payload = self.crypto_service.decrypt_payload(encrypted_payload, cek)
                parsed_args = self._extract_tool_invoke_args(decrypted_payload)
                if not isinstance(parsed_args, dict):
                    raise ValueError("decrypted payload must contain tool args object")
                args = parsed_args
            else:
                raw_args = msg.get("args")
                if raw_args is None:
                    args = {}
                elif isinstance(raw_args, dict):
                    args = raw_args
                else:
                    raise ValueError("plaintext invoke requires args object")

            result = await self._execute_local_worker_tool(tool_id, args, room_id, run_id)
            success = bool(result.get("success")) if isinstance(result, dict) else False

            response: Dict[str, Any] = {
                "type": "TOOL_INVOKE_RESULT",
                "invoke_id": invoke_id,
                "run_id": run_id,
                "tool_id": tool_id,
                "status": "success" if success else "error",
            }

            if mode == "confidential":
                if not cek or not self.crypto_service:
                    raise RuntimeError("encryption context unavailable for confidential result")
                encrypted_result = self.crypto_service.encrypt_response(result, cek)
                if not encrypted_result:
                    raise RuntimeError("failed to encrypt tool result")
                response["result_b64"] = encrypted_result
            else:
                response["result"] = result

            if not success:
                response["error"] = result.get("error") if isinstance(result, dict) else {
                    "code": "TOOL_EXECUTION_FAILED",
                    "message": "tool execution failed",
                }

            await _send_result(response)
        except Exception as exc:
            await _send_result(
                {
                    "type": "TOOL_INVOKE_RESULT",
                    "invoke_id": invoke_id,
                    "run_id": run_id,
                    "tool_id": tool_id,
                    "status": "error",
                    "error": {"code": "TOOL_INVOKE_FAILED", "message": str(exc)},
                }
            )

    def _execute_local_file_search(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """
        Minimal local folder-backed file search for confidential worker tools.
        """
        root = (constants.RAG_CONTEXT_PATH or "").strip()
        if not root:
            return {
                "success": False,
                "error": {"code": "RAG_CONTEXT_MISSING", "message": "RAG_CONTEXT_PATH is not configured"},
            }
        if not os.path.isdir(root):
            return {
                "success": False,
                "error": {"code": "RAG_CONTEXT_INVALID", "message": f"RAG context folder not found: {root}"},
            }

        query = args.get("query")
        if not isinstance(query, str) or not query.strip():
            return {
                "success": False,
                "error": {"code": "INVALID_ARGS", "message": "file_search requires non-empty 'query'"},
            }
        normalized_query = query.strip().lower()

        k_raw = args.get("k", 5)
        try:
            k = int(k_raw)
        except (TypeError, ValueError):
            k = 5
        k = max(1, min(k, 20))

        max_file_size = 2 * 1024 * 1024
        max_files = 2000
        scanned = 0
        matches: List[Dict[str, Any]] = []

        for current_root, _, files in os.walk(root):
            for filename in files:
                if scanned >= max_files:
                    break
                path = os.path.join(current_root, filename)
                scanned += 1
                try:
                    if os.path.getsize(path) > max_file_size:
                        continue
                except OSError:
                    continue

                mime, _ = mimetypes.guess_type(path)
                if mime and not (
                    mime.startswith("text/")
                    or mime in ("application/json", "application/xml", "application/yaml", "application/x-yaml")
                ):
                    continue

                try:
                    with open(path, "r", encoding="utf-8", errors="ignore") as handle:
                        text = handle.read()
                except Exception:
                    continue

                haystack = text.lower()
                first_index = haystack.find(normalized_query)
                if first_index < 0:
                    continue

                count = haystack.count(normalized_query)
                snippet_start = max(0, first_index - 120)
                snippet_end = min(len(text), first_index + 240)
                snippet = text[snippet_start:snippet_end].replace("\n", " ").strip()
                rel_path = os.path.relpath(path, root)
                matches.append(
                    {
                        "path": rel_path,
                        "score": count,
                        "snippet": snippet,
                    }
                )

            if scanned >= max_files:
                break

        matches.sort(key=lambda item: item.get("score", 0), reverse=True)
        top_matches = matches[:k]

        return {
            "success": True,
            "result": {
                "query": query,
                "results": top_matches,
                "total_matches": len(matches),
                "scanned_files": scanned,
            },
        }

    async def _execute_local_mcp_proxy(
        self,
        tool_id: str,
        args: Dict[str, Any],
        room_id: Optional[str],
        run_id: Optional[str],
    ) -> Dict[str, Any]:
        endpoint = (constants.MCP_TOOL_ENDPOINT or "").strip()
        if not endpoint:
            return {
                "success": False,
                "error": {"code": "MCP_ENDPOINT_MISSING", "message": "MCP_TOOL_ENDPOINT is not configured"},
            }
        if not self.http_session:
            return {
                "success": False,
                "error": {"code": "SESSION_UNAVAILABLE", "message": "HTTP session not initialized"},
            }

        request_body = {
            "tool_id": tool_id,
            "args": args,
            "context": {
                "room_id": room_id,
                "run_id": run_id,
            },
        }

        try:
            async with self.http_session.post(
                endpoint,
                json=request_body,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                content_type = (resp.headers.get("content-type") or "").lower()
                if "json" in content_type:
                    data = await resp.json()
                else:
                    data = {"text": await resp.text()}

                if resp.status >= 400:
                    return {
                        "success": False,
                        "error": {
                            "code": f"MCP_HTTP_{resp.status}",
                            "message": str(data)[:400],
                        },
                    }
                return {
                    "success": True,
                    "result": data,
                }
        except Exception as exc:
            return {
                "success": False,
                "error": {"code": "MCP_REQUEST_FAILED", "message": str(exc)},
            }

    def _extract_text_from_content(self, content: Any) -> Optional[str]:
        """Best-effort extraction of text content from Responses-style content blocks."""
        if isinstance(content, str):
            return content

        if isinstance(content, dict):
            # Common nested shapes:
            # {"text":"..."} / {"text":{"value":"..."}}
            # {"content":"..."} / {"value":"..."}
            for key in ("text", "content", "value", "output_text", "delta"):
                value = content.get(key)
                if isinstance(value, str) and value:
                    return value
                nested = self._extract_text_from_content(value)
                if nested:
                    return nested
            return None

        if isinstance(content, list):
            parts: List[str] = []
            for block in content:
                if isinstance(block, str):
                    parts.append(block)
                    continue
                extracted = self._extract_text_from_content(block)
                if extracted:
                    parts.append(extracted)
            merged = "\n".join([p for p in parts if p])
            return merged or None

        return None

    def _extract_text_from_chat_result(self, result: Any) -> Optional[str]:
        """Best-effort extraction of assistant text from chat-completions style JSON."""
        if not isinstance(result, dict):
            return None

        choices = result.get("choices")
        if not isinstance(choices, list) or not choices:
            return None

        first = choices[0] if isinstance(choices[0], dict) else {}
        delta_content = first.get("delta", {}).get("content")
        message_content = first.get("message", {}).get("content")
        text_content = first.get("text")

        for candidate in (delta_content, message_content, text_content):
            if isinstance(candidate, str) and candidate:
                return candidate
            extracted = self._extract_text_from_content(candidate)
            if extracted:
                return extracted

        return None

    def _build_chat_payload_from_responses_input(self, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        Convert Responses-style `input` payload into Chat Completions payload for local backends
        (e.g. llama-server) that may not implement `/v1/responses`.
        """
        input_value = payload.get("input")
        messages: List[Dict[str, Any]] = []

        if isinstance(input_value, str):
            messages.append({"role": "user", "content": input_value})
        elif isinstance(input_value, list):
            for item in input_value:
                if isinstance(item, str):
                    messages.append({"role": "user", "content": item})
                    continue
                if not isinstance(item, dict):
                    continue

                role = item.get("role")
                content = self._extract_text_from_content(item.get("content"))
                if isinstance(role, str) and content:
                    messages.append({"role": role, "content": content})
                    continue

                # Fallback for blocks like {"type":"input_text","text":"..."}.
                text = item.get("text")
                if isinstance(text, str) and text:
                    messages.append({"role": "user", "content": text})
        elif isinstance(input_value, dict):
            role = input_value.get("role", "user")
            content = self._extract_text_from_content(input_value.get("content")) or input_value.get("text")
            if isinstance(role, str) and isinstance(content, str) and content:
                messages.append({"role": role, "content": content})

        if not messages:
            return None

        chat_payload: Dict[str, Any] = {
            "model": payload.get("model"),
            "messages": messages,
        }

        passthrough_keys = [
            "temperature",
            "top_p",
            "frequency_penalty",
            "presence_penalty",
            "stream",
            "stop",
            "tools",
            "tool_choice",
            "n",
            "user",
            "response_format",
        ]
        for key in passthrough_keys:
            if key in payload:
                if key == "tools":
                    chat_payload[key] = [
                        self._normalize_responses_tool_for_chat(tool)
                        for tool in payload[key]
                    ] if isinstance(payload[key], list) else payload[key]
                elif key == "tool_choice":
                    chat_payload[key] = self._normalize_responses_tool_choice_for_chat(payload[key])
                else:
                    chat_payload[key] = payload[key]

        if "max_tokens" in payload:
            chat_payload["max_tokens"] = payload["max_tokens"]
        elif "max_output_tokens" in payload:
            chat_payload["max_tokens"] = payload["max_output_tokens"]
        elif "max_completion_tokens" in payload:
            chat_payload["max_tokens"] = payload["max_completion_tokens"]

        return chat_payload

    def _normalize_responses_tool_for_chat(self, tool: Any) -> Any:
        if not isinstance(tool, dict):
            return tool
        if tool.get("type") != "function" or isinstance(tool.get("function"), dict):
            return tool
        name = tool.get("name")
        if not isinstance(name, str) or not name:
            return tool

        function: Dict[str, Any] = {"name": name}
        description = tool.get("description")
        if isinstance(description, str):
            function["description"] = description
        if "parameters" in tool:
            function["parameters"] = tool.get("parameters")
        if "strict" in tool:
            function["strict"] = tool.get("strict")
        return {"type": "function", "function": function}

    def _normalize_responses_tool_choice_for_chat(self, tool_choice: Any) -> Any:
        if not isinstance(tool_choice, dict):
            return tool_choice
        if tool_choice.get("type") != "function" or isinstance(tool_choice.get("function"), dict):
            return tool_choice
        name = tool_choice.get("name")
        if not isinstance(name, str) or not name:
            return tool_choice
        return {"type": "function", "function": {"name": name}}

    def _normalize_responses_payload_for_local_backend(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        Keep the broker's `/v1/responses` contract, but adapt function-tool
        definitions for local OpenAI-compatible backends that validate tools
        with the Chat Completions schema even on their Responses endpoint.

        OpenAI Responses accepts function tools as:
          {"type": "function", "name": "...", "parameters": {...}}

        Some vLLM builds reject that on `/v1/responses` and require:
          {"type": "function", "function": {"name": "...", "parameters": {...}}}

        This is a local-backend compatibility shim only. The request still
        goes to `/v1/responses`, streams as Responses SSE, and keeps `input`.
        """
        if not isinstance(payload, dict):
            return payload

        updated: Optional[Dict[str, Any]] = None

        tools = payload.get("tools")
        if isinstance(tools, list):
            normalized_tools = [self._normalize_responses_tool_for_chat(tool) for tool in tools]
            if normalized_tools != tools:
                updated = dict(payload)
                updated["tools"] = normalized_tools

        if "tool_choice" in payload:
            normalized_choice = self._normalize_responses_tool_choice_for_chat(payload["tool_choice"])
            if normalized_choice != payload["tool_choice"]:
                if updated is None:
                    updated = dict(payload)
                updated["tool_choice"] = normalized_choice

        return updated if updated is not None else payload

    def _select_local_inference_api(
        self, api_type: str, payload: Dict[str, Any]
    ):
        payload_has_messages = isinstance(payload.get("messages"), list)
        payload_has_responses_input = "input" in payload
        chat_payload_from_input = None
        if payload_has_responses_input:
            chat_payload_from_input = self._build_chat_payload_from_responses_input(payload)

        if api_type == "responses" and payload_has_responses_input:
            # The broker explicitly requested the Responses API. Preserve
            # that contract so native Responses SSE reaches the caller.
            # The chat conversion below is only a compatibility fallback
            # for older broker messages/local backends that do not request
            # /v1/responses.
            return "responses", self._normalize_responses_payload_for_local_backend(payload)
        if payload_has_messages:
            # Worker payload is chat-format; force chat/completions regardless of broker api hint.
            return "chat_completions", payload
        if chat_payload_from_input is not None:
            # Prefer chat/completions for local inference to avoid /v1/responses compatibility drift.
            logger.info("Coerced responses-style input into chat payload for local inference")
            return "chat_completions", chat_payload_from_input
        if payload_has_responses_input:
            return "responses", payload

        raise Exception(
            "Invalid payload shape for local inference API: missing both 'messages' and 'input'"
        )

    def _payload_uses_tools(self, payload: Dict[str, Any]) -> bool:
        """Whether payload contains tool-calling fields that some local servers may reject."""
        tools = payload.get("tools")
        if isinstance(tools, list) and len(tools) > 0:
            return True
        return payload.get("tool_choice") is not None

    def _is_jinja_tools_error(self, error_body: str) -> bool:
        """Detect llama.cpp server error when tools are sent without --jinja."""
        if not isinstance(error_body, str):
            return False
        body_lc = error_body.lower()
        return "tools param requires --jinja flag" in body_lc

    async def _handle_challenge(self, ch: Dict[str, Any]):
        """Respond to broker CHALLENGE if configured."""
        if not constants.CHALLENGE_SECRET:
            logger.warning("Received CHALLENGE but no CHALLENGE_SECRET configured")
            return

        import hmac
        import hashlib

        nonce = ch.get("nonce", "")
        ts = str(ch.get("timestamp", ""))
        msg = f"{nonce}{self.worker_id}{ts}".encode()
        mac = hmac.new(constants.CHALLENGE_SECRET.encode(), msg, hashlib.sha256).digest()
        proof = base64.b64encode(mac).decode()

        await self.ws.send(json.dumps({
            "type": "CHALLENGE_RESP",
            "nonce": nonce,
            "proof": proof
        }))
        logger.info("Sent CHALLENGE_RESP")

    async def _handle_job_start(self, msg: Dict[str, Any]):
        """Process job request from broker"""
        job_id = msg["job_id"]
        api_type = msg.get("api", "chat_completions")  # Default to chat_completions for backwards compat
        # CRITICAL: Never use job_id as fallback - completion_id must come from vLLM
        self.active_jobs.add(job_id)  # Track by job_id until we get completion_id

        # Immediately send ACK to broker before processing
        # This tells broker "I received the job" - separate from "I'm processing"
        # Allows broker to use short ACK timeout (10s) + long processing timeout (5min)
        try:
            await self.ws.send(json.dumps({
                "type": "ACK",
                "job_id": job_id
            }))
            logger.info(f"Sent ACK for job {job_id}")
        except Exception as e:
            logger.error(f"Failed to send ACK for job {job_id}: {e}")
            self.active_jobs.discard(job_id)
            return  # Can't process if we can't ACK

        # Extract the payload from START frame
        payload = msg["payload"]
        requested_completion_id = payload.get("completion_id") or job_id
        confidential_run_id: Optional[str] = None
        confidential_continuation: Optional[str] = None
        confidential_tool_call_id: Optional[str] = None
        room_id: Optional[str] = None
        epoch: int = 0

        # W2: Check if this is a confidential mode job
        # Broker sends payload.confidential=true and payload.encrypted_payload
        is_confidential = payload.get("confidential", False)
        cek = None

        if is_confidential:
            # Extract room_id/epoch from canonical metadata, with e2e fallbacks.
            room_id, epoch = self._extract_confidential_context(payload)

            logger.info(f"Starting confidential job {job_id} for room {room_id}, epoch {epoch}")

            if not self.crypto_service:
                logger.error("Confidential job received but crypto service not available")
                await self._send_job_error(job_id, "Worker not configured for confidential mode")
                return

            if not room_id:
                logger.error("Confidential job missing room_id in encryption metadata")
                await self._send_job_error(job_id, "Missing room_id for confidential mode")
                return

            encryption_meta = payload.get("encryption") if isinstance(payload.get("encryption"), dict) else {}
            run_id_value = encryption_meta.get("run_id")
            confidential_run_id = run_id_value if isinstance(run_id_value, str) and run_id_value else None
            continuation_value = encryption_meta.get("continuation")
            confidential_continuation = (
                continuation_value if isinstance(continuation_value, str) and continuation_value else None
            )
            tool_call_value = encryption_meta.get("tool_call_id")
            confidential_tool_call_id = (
                tool_call_value if isinstance(tool_call_value, str) and tool_call_value else None
            )

            # Fetch CEK for this room (with retry for key-package propagation races).
            cek = None
            retries = max(1, int(constants.CONFIDENTIAL_CEK_FETCH_RETRIES))
            retry_delay = max(0.0, float(constants.CONFIDENTIAL_CEK_FETCH_RETRY_DELAY_SEC))
            for attempt in range(1, retries + 1):
                cek = await self.crypto_service.fetch_cek(self.http_session, room_id, epoch)
                if cek:
                    break
                if attempt < retries:
                    logger.info(
                        "No CEK package yet for room %s (epoch=%s), retrying in %.1fs (%s/%s)",
                        room_id,
                        epoch,
                        retry_delay,
                        attempt,
                        retries,
                    )
                    await asyncio.sleep(retry_delay)

            if not cek:
                logger.error(f"Failed to fetch CEK for room {room_id}")
                await self._send_job_error(job_id, "Failed to fetch room encryption key")
                return

            # Decrypt the encrypted_payload to get the actual request
            encrypted_payload = payload.get("encrypted_payload")
            if not encrypted_payload:
                logger.error("Confidential job missing encrypted_payload")
                await self._send_job_error(job_id, "Missing encrypted_payload")
                return

            decrypted_payload = self.crypto_service.decrypt_payload(encrypted_payload, cek)
            if not decrypted_payload:
                logger.error("Failed to decrypt job payload")
                await self._send_job_error(job_id, "Failed to decrypt payload")
                return

            normalized_payload = self._normalize_decrypted_payload(decrypted_payload)
            if (
                not normalized_payload
                and confidential_continuation == "tool_result"
                and confidential_run_id
            ):
                normalized_payload = self._build_continuation_payload_from_tool_result(
                    confidential_run_id,
                    confidential_tool_call_id,
                    decrypted_payload,
                )
                if normalized_payload:
                    logger.info(
                        "Built confidential continuation payload from tool_result: run_id=%s tool_call_id=%s",
                        confidential_run_id,
                        confidential_tool_call_id,
                    )

            if not normalized_payload:
                logger.error(
                    "Decrypted payload has unsupported shape (keys=%s)",
                    list(decrypted_payload.keys()) if isinstance(decrypted_payload, dict) else type(decrypted_payload),
                )
                await self._send_job_error(job_id, "Decrypted payload format is invalid")
                self._discard_confidential_run(confidential_run_id)
                return

            # Use normalized decrypted payload for local inference API.
            payload = normalized_payload
            self._remember_confidential_run_payload(confidential_run_id, payload)
            logger.debug("Decrypted confidential payload successfully")
        else:
            logger.info(f"Starting job {job_id} api={api_type} (completion_id will be extracted from response)")

        try:

            # Forward to local vLLM via miner-proxy
            # Use the same API_KEY that miner-proxy uses for vLLM auth
            request_headers = {"Content-Type": "application/json"}
            if constants.API_KEY:
                request_headers["Authorization"] = f"Bearer {constants.API_KEY}"

            payload_has_messages = isinstance(payload.get("messages"), list)
            payload_has_responses_input = "input" in payload
            effective_api_type, payload = self._select_local_inference_api(api_type, payload)

            logger.info(
                "Selected local inference API: hint=%s, selected=%s, has_messages=%s, has_input=%s",
                api_type,
                effective_api_type,
                payload_has_messages,
                payload_has_responses_input,
            )

            if effective_api_type == "responses":
                endpoint = f"{self.miner_proxy_url}/v1/responses"
            else:
                endpoint = f"{self.miner_proxy_url}/v1/chat/completions"

            request_payload = payload

            while True:
                async with self.http_session.post(
                    endpoint,
                    json=request_payload,
                    headers=request_headers
                ) as resp:
                    payload = request_payload
                    if resp.status >= 400:
                        error_body = await resp.text()
                        error_preview = (error_body or "")[:600]

                        if (
                            is_confidential
                            and effective_api_type == "chat_completions"
                            and self._payload_uses_tools(payload)
                            and self._is_jinja_tools_error(error_body)
                        ):
                            raise Exception(
                                "Local inference rejected tools payload: "
                                "llama-server requires --jinja for tool calling"
                            )

                        raise Exception(
                            f"Local inference request failed: HTTP {resp.status} "
                            f"(content-type={(resp.headers.get('content-type') or '(none)')}): {error_preview}"
                        )

                    if effective_api_type == "responses":
                        # Handle OpenAI Responses API format. In confidential
                        # mode the handler encrypts every native response.* event
                        # into encrypted_response_event frames (and bridges
                        # function-call events into encrypted_tool_call), so the
                        # stream never leaves this worker in plaintext.
                        await self._handle_responses_api(
                            resp,
                            job_id,
                            payload,
                            is_confidential=is_confidential,
                            cek=cek,
                            confidential_run_id=confidential_run_id,
                            room_id=room_id,
                            requested_completion_id=requested_completion_id,
                            endpoint=endpoint,
                            request_headers=request_headers,
                        )
                    elif payload.get("stream") and not is_confidential:
                        # Handle streaming response (plaintext mode only)
                        # Confidential mode uses buffered response for encryption
                        current_cid: Optional[str] = None
                        sent_tool_call_indices: set = set()
                        async for line in resp.content:
                            text = line.decode("utf-8", errors="ignore").strip()
                            if not text:
                                continue
                            if text == "data: [DONE]":
                                break
                            if text.startswith("data: "):
                                try:
                                    evt = json.loads(text[6:])
                                    # Capture completion_id from upstream chunk - REQUIRED
                                    if not current_cid and evt.get("id"):
                                        current_cid = evt.get("id")
                                        logger.info(f"Captured completion_id from vLLM: {current_cid}")
                                        # Update active jobs tracking
                                        self.active_jobs.discard(job_id)
                                        self.active_jobs.add(current_cid)

                                    # Only send chunks if we have a valid completion_id
                                    if current_cid:
                                        choices = evt.get("choices", [])
                                        if choices:
                                            delta_obj = choices[0].get("delta", {}) or {}
                                            has_content = isinstance(delta_obj.get("content"), str) and delta_obj.get("content")
                                            tool_calls_raw = delta_obj.get("tool_calls") if isinstance(delta_obj.get("tool_calls"), list) else None
                                            # Sanitize tool_call deltas to OpenAI streaming convention:
                                            # FIRST chunk per `index` carries id/type/function.{name,arguments};
                                            # SUBSEQUENT chunks carry only `function.arguments`.
                                            # vLLM 0.19 emits id/type/name=null in every chunk, which makes
                                            # LangChain (LibreChat) overwrite accumulator.function.name with
                                            # null on later chunks → tool dispatch fails silently → no agent
                                            # loop → bubble dies after the tool_call stream completes.
                                            sanitized_tool_calls = []
                                            if tool_calls_raw:
                                                for tc in tool_calls_raw:
                                                    if not isinstance(tc, dict):
                                                        continue
                                                    idx = tc.get("index", 0)
                                                    func = tc.get("function") or {}
                                                    args = func.get("arguments")
                                                    if idx not in sent_tool_call_indices:
                                                        sanitized_tool_calls.append(tc)
                                                        sent_tool_call_indices.add(idx)
                                                    elif isinstance(args, str) and args:
                                                        sanitized_tool_calls.append({
                                                            "index": idx,
                                                            "function": {"arguments": args},
                                                        })
                                                if sanitized_tool_calls:
                                                    delta_obj = {**delta_obj, "tool_calls": sanitized_tool_calls}
                                                else:
                                                    delta_obj = {k: v for k, v in delta_obj.items() if k != "tool_calls"}
                                            has_tool_calls = bool(sanitized_tool_calls)
                                            if has_content or has_tool_calls:
                                                await self.ws.send(json.dumps({
                                                    "type": "CHUNK",
                                                    "job_id": job_id,
                                                    "completion_id": current_cid,
                                                    "delta": delta_obj,
                                                }))
                                    else:
                                        logger.warning(f"No completion_id received yet for job {job_id}")
                                except json.JSONDecodeError:
                                    logger.error(f"Failed to parse SSE chunk: {text}")
                                    continue

                        # Send END message only if we have a valid completion_id
                        if current_cid:
                            await self.ws.send(json.dumps({
                                "type": "END",
                                "job_id": job_id,
                                "completion_id": current_cid,
                                "usage": {}  # TODO: extract usage from last chunk if available
                            }))
                        else:
                            # CRITICAL ERROR: No completion_id received from vLLM
                            raise Exception(f"No completion_id received from vLLM for streaming job {job_id}")
                    else:
                        # Handle non-streaming response (or confidential streaming with encrypted chunks)
                        if payload.get("stream") and is_confidential:
                            # Confidential streaming: emit encrypted CHUNK messages
                            # Each chunk is individually encrypted for secure streaming
                            current_cid = None
                            usage = {}
                            total_encrypted_bytes = 0
                            raw_line_count = 0
                            data_event_count = 0
                            parsed_event_count = 0
                            emitted_chunk_count = 0
                            emitted_tool_call_count = 0
                            detected_tool_call_count = 0
                            locally_executed_tool_results: List[Dict[str, Any]] = []
                            non_data_samples: List[str] = []
                            raw_fallback_buffer: List[str] = []
                            tool_calls_accumulator: Dict[int, Dict[str, Any]] = {}
                            content_type = (resp.headers.get("content-type") or "").lower()
    
                            logger.info(
                                "Confidential stream response metadata: status=%s content_type=%s endpoint=%s",
                                resp.status,
                                content_type or "(none)",
                                endpoint,
                            )
    
                            async for line in resp.content:
                                text = line.decode("utf-8", errors="ignore").strip()
                                if not text:
                                    continue
                                raw_line_count += 1
                                if len(raw_fallback_buffer) < 100:
                                    raw_fallback_buffer.append(text)
                                if text == "data: [DONE]":
                                    break
                                if text.startswith("data: "):
                                    data_event_count += 1
                                    try:
                                        evt = json.loads(text[6:])
                                        parsed_event_count += 1
                                        if not current_cid and evt.get("id"):
                                            current_cid = evt.get("id")
                                            logger.info(f"Captured completion_id from vLLM: {current_cid}")
                                            self.active_jobs.discard(job_id)
                                            self.active_jobs.add(current_cid)
    
                                        choices = evt.get("choices", [])
                                        if choices and not current_cid:
                                            current_cid = requested_completion_id
    
                                        if choices and current_cid:
                                            choice0 = choices[0] if isinstance(choices[0], dict) else {}
                                            delta_payload = (
                                                choice0.get("delta", {})
                                                if isinstance(choice0.get("delta", {}), dict)
                                                else {}
                                            )
    
                                            tool_calls_delta = delta_payload.get("tool_calls", [])
                                            if isinstance(tool_calls_delta, list):
                                                for tc in tool_calls_delta:
                                                    if not isinstance(tc, dict):
                                                        continue
                                                    raw_index = tc.get("index", 0)
                                                    try:
                                                        index = int(raw_index)
                                                    except (TypeError, ValueError):
                                                        index = 0
                                                    entry = tool_calls_accumulator.setdefault(
                                                        index,
                                                        {
                                                            "id": None,
                                                            "function": {"name": "", "arguments": ""},
                                                        },
                                                    )
                                                    if isinstance(tc.get("id"), str) and tc.get("id"):
                                                        entry["id"] = tc["id"]
                                                    function_data = tc.get("function", {})
                                                    if isinstance(function_data, dict):
                                                        if isinstance(function_data.get("name"), str):
                                                            entry["function"]["name"] += function_data["name"]
                                                        if isinstance(function_data.get("arguments"), str):
                                                            entry["function"]["arguments"] += function_data["arguments"]
    
                                            if (
                                                choice0.get("finish_reason") == "tool_calls"
                                                and tool_calls_accumulator
                                            ):
                                                for idx in sorted(tool_calls_accumulator.keys()):
                                                    tool_entry = tool_calls_accumulator[idx]
                                                    function_data = tool_entry.get("function", {})
                                                    tool_name = (
                                                        function_data.get("name")
                                                        if isinstance(function_data, dict)
                                                        else None
                                                    )
                                                    if not isinstance(tool_name, str) or not tool_name:
                                                        tool_name = "unknown_tool"
                                                    tool_call_id = (
                                                        tool_entry.get("id")
                                                        if isinstance(tool_entry.get("id"), str)
                                                        and tool_entry.get("id")
                                                        else f"tool_call_{idx}"
                                                    )
                                                    raw_arguments = (
                                                        function_data.get("arguments")
                                                        if isinstance(function_data, dict)
                                                        else "{}"
                                                    )
                                                    parsed_arguments: Dict[str, Any]
                                                    if isinstance(raw_arguments, str) and raw_arguments.strip():
                                                        try:
                                                            parsed = json.loads(raw_arguments)
                                                            if isinstance(parsed, dict):
                                                                parsed_arguments = parsed
                                                            else:
                                                                parsed_arguments = {"_value": parsed}
                                                        except Exception:
                                                            parsed_arguments = {"_raw": raw_arguments}
                                                    else:
                                                        parsed_arguments = {}
    
                                                    self._remember_confidential_tool_call(
                                                        confidential_run_id,
                                                        tool_call_id,
                                                        tool_name,
                                                        parsed_arguments,
                                                    )
                                                    detected_tool_call_count += 1
                                                    if self._is_local_worker_tool(tool_name):
                                                        local_result = await self._execute_local_worker_tool(
                                                            tool_name,
                                                            parsed_arguments,
                                                            room_id,
                                                            confidential_run_id,
                                                        )
                                                        locally_executed_tool_results.append(
                                                            {
                                                                "tool_call_id": tool_call_id,
                                                                "tool_id": tool_name,
                                                                "result": local_result,
                                                            }
                                                        )
                                                        logger.info(
                                                            "Executed local worker tool: tool=%s call_id=%s success=%s",
                                                            tool_name,
                                                            tool_call_id,
                                                            bool(local_result.get("success")),
                                                        )
                                                    else:
                                                        encrypted_tool_call = self.crypto_service.encrypt_response(
                                                            {
                                                                "tool_id": tool_name,
                                                                "args": parsed_arguments,
                                                            },
                                                            cek,
                                                        )
                                                        if encrypted_tool_call:
                                                            encrypted_tool_frame = {
                                                                "type": "encrypted_tool_call",
                                                                "payload_b64": encrypted_tool_call,
                                                                "tool_call_id": tool_call_id,
                                                                "tool_id": tool_name,
                                                            }
                                                            await self.ws.send(
                                                                json.dumps(
                                                                    {
                                                                        "type": "CHUNK",
                                                                        "job_id": job_id,
                                                                        "completion_id": current_cid,
                                                                        "data": encrypted_tool_frame,
                                                                        "delta": encrypted_tool_frame,
                                                                    }
                                                                )
                                                            )
                                                            emitted_tool_call_count += 1
                                                tool_calls_accumulator = {}
    
                                            # Prefer streaming deltas, but tolerate providers that emit full
                                            # message content in streaming mode.
                                            delta = delta_payload.get("content")
                                            if not delta:
                                                delta = choice0.get("message", {}).get("content") or choice0.get("text")
                                            if not delta:
                                                # Reasoning-parser fallback: with --reasoning-parser qwen3,
                                                # vLLM routes pre-</think> tokens into delta.reasoning_content
                                                # and leaves delta.content=null. If the model never closes
                                                # </think> (long thinking, no answer) we'd otherwise emit zero
                                                # chunks and the FE would show an empty bubble. Surface
                                                # reasoning_content as content so the user sees the thinking.
                                                delta = (
                                                    delta_payload.get("reasoning_content")
                                                    or choice0.get("message", {}).get("reasoning_content")
                                                )
                                            if not isinstance(delta, str):
                                                delta = self._extract_text_from_content(delta)
                                            if delta:
                                                # Client tool loop expects decrypted payload shape {"text": "..."}.
                                                encrypted_chunk = self.crypto_service.encrypt_response(
                                                    {"text": delta}, cek
                                                )
                                                if encrypted_chunk:
                                                    # Use structured confidential chunk format expected by
                                                    # compute-broker -> gateway -> client relay path.
                                                    # Include both `data` and `delta` for parity with the
                                                    # confidential echo worker used in e2e.
                                                    encrypted_frame = {
                                                        "type": "encrypted_chunk",
                                                        "payload_b64": encrypted_chunk,
                                                    }
                                                    await self.ws.send(json.dumps({
                                                        "type": "CHUNK",
                                                        "job_id": job_id,
                                                        "completion_id": current_cid,
                                                        "data": encrypted_frame,
                                                        "delta": encrypted_frame,
                                                    }))
                                                    total_encrypted_bytes += len(encrypted_chunk) * 3 // 4
                                                    emitted_chunk_count += 1
    
                                        if evt.get("usage"):
                                            usage = evt.get("usage")
                                    except json.JSONDecodeError:
                                        logger.warning(
                                            "Confidential stream JSON parse failure (line_preview=%s)",
                                            text[:240],
                                        )
                                        continue
                                else:
                                    if len(non_data_samples) < 3:
                                        non_data_samples.append(text[:240])
    
                            if not current_cid:
                                # Some local backends omit stream chunk ids.
                                current_cid = requested_completion_id
                                logger.warning(
                                    "No completion_id received from local stream; "
                                    "falling back to requested completion_id=%s",
                                    current_cid,
                                )
    
                            if emitted_chunk_count == 0 and raw_fallback_buffer:
                                # Some local backends ignore stream=true and return JSON body instead of SSE.
                                # Attempt one-shot fallback to avoid silent empty completions.
                                fallback_payload = "\n".join(
                                    line for line in raw_fallback_buffer if not line.startswith("data: ")
                                ).strip()
                                if fallback_payload:
                                    try:
                                        fallback_json = json.loads(fallback_payload)
                                        fallback_text = self._extract_text_from_chat_result(fallback_json)
                                        if fallback_text:
                                            encrypted_chunk = self.crypto_service.encrypt_response(
                                                {"text": fallback_text}, cek
                                            )
                                            if encrypted_chunk:
                                                encrypted_frame = {
                                                    "type": "encrypted_chunk",
                                                    "payload_b64": encrypted_chunk,
                                                }
                                                await self.ws.send(json.dumps({
                                                    "type": "CHUNK",
                                                    "job_id": job_id,
                                                    "completion_id": current_cid,
                                                    "data": encrypted_frame,
                                                    "delta": encrypted_frame,
                                                }))
                                                total_encrypted_bytes += len(encrypted_chunk) * 3 // 4
                                                emitted_chunk_count += 1
                                                logger.warning(
                                                    "Confidential stream fallback used: local backend returned JSON body with no SSE chunks"
                                                )
                                        usage = fallback_json.get("usage", usage) if isinstance(fallback_json, dict) else usage
                                    except Exception:
                                        logger.warning(
                                            "Confidential stream fallback failed to parse non-SSE body (preview=%s)",
                                            fallback_payload[:240],
                                        )
    
                            logger.info(
                                "Confidential stream parse summary: raw_lines=%s data_events=%s parsed_events=%s "
                                "emitted_chunks=%s emitted_tool_calls=%s non_data_samples=%s",
                                raw_line_count,
                                data_event_count,
                                parsed_event_count,
                                emitted_chunk_count,
                                emitted_tool_call_count,
                                non_data_samples,
                            )
                            if self._payload_uses_tools(payload) and detected_tool_call_count == 0:
                                logger.warning(
                                    "Confidential request included tools but model produced no tool call deltas "
                                    "(job_id=%s run_id=%s model=%s)",
                                    job_id,
                                    confidential_run_id,
                                    payload.get("model"),
                                )
                            if (
                                self._payload_uses_tools(payload)
                                and detected_tool_call_count > 0
                                and emitted_tool_call_count == 0
                                and locally_executed_tool_results
                            ):
                                logger.info(
                                    "Confidential tool calls were handled locally by worker tools "
                                    "(job_id=%s run_id=%s local_results=%s)",
                                    job_id,
                                    confidential_run_id,
                                    len(locally_executed_tool_results),
                                )

                            if locally_executed_tool_results and emitted_tool_call_count > 0:
                                self._buffer_local_tool_results_for_continuation(
                                    confidential_run_id,
                                    locally_executed_tool_results,
                                )
                                logger.warning(
                                    "Guard: mixed local+remote tool calls in one turn; buffered %s local result(s) "
                                    "for next remote continuation (job_id=%s run_id=%s remote_tool_calls=%s)",
                                    len(locally_executed_tool_results),
                                    job_id,
                                    confidential_run_id,
                                    emitted_tool_call_count,
                                )

                            if locally_executed_tool_results and emitted_tool_call_count == 0:
                                continuation_payload = self._build_continuation_payload_from_tool_results(
                                    confidential_run_id,
                                    locally_executed_tool_results,
                                )
                                if not continuation_payload:
                                    raise Exception("Failed to build confidential continuation payload from local tool results")
    
                                logger.info(
                                    "Continuing confidential run with %s local tool result(s)",
                                    len(locally_executed_tool_results),
                                )
    
                                continuation_usage: Dict[str, Any] = {}
                                continuation_emitted_chunks = 0
                                continuation_current_cid = current_cid
                                continuation_errors: List[str] = []
                                continuation_raw_line_count = 0
                                continuation_data_event_count = 0
                                continuation_parsed_event_count = 0
                                continuation_non_data_samples: List[str] = []
                                continuation_raw_fallback_buffer: List[str] = []

                                for continuation_attempt in range(1, 3):
                                    try:
                                        async with self.http_session.post(
                                            endpoint,
                                            json=continuation_payload,
                                            headers=request_headers,
                                        ) as continuation_resp:
                                            continuation_content_type = (
                                                continuation_resp.headers.get("content-type") or ""
                                            ).lower()
                                            if continuation_resp.status >= 400:
                                                body_preview = (await continuation_resp.text())[:500]
                                                raise Exception(
                                                    f"Continuation request failed: HTTP {continuation_resp.status} "
                                                    f"(content-type={continuation_content_type or '(none)'}): {body_preview}"
                                                )

                                            async for line in continuation_resp.content:
                                                text = line.decode("utf-8", errors="ignore").strip()
                                                if not text:
                                                    continue
                                                continuation_raw_line_count += 1
                                                if len(continuation_raw_fallback_buffer) < 100:
                                                    continuation_raw_fallback_buffer.append(text)
                                                if text == "data: [DONE]":
                                                    break
                                                if not text.startswith("data: "):
                                                    if len(continuation_non_data_samples) < 3:
                                                        continuation_non_data_samples.append(text[:240])
                                                    continue
                                                continuation_data_event_count += 1
                                                try:
                                                    evt = json.loads(text[6:])
                                                    continuation_parsed_event_count += 1
                                                except json.JSONDecodeError:
                                                    continue

                                                if not continuation_current_cid and evt.get("id"):
                                                    continuation_current_cid = evt.get("id")
                                                    self.active_jobs.discard(job_id)
                                                    self.active_jobs.add(continuation_current_cid)

                                                choices = evt.get("choices", [])
                                                if choices and not continuation_current_cid:
                                                    continuation_current_cid = requested_completion_id

                                                if choices and continuation_current_cid:
                                                    choice0 = choices[0] if isinstance(choices[0], dict) else {}
                                                    delta_payload = (
                                                        choice0.get("delta", {})
                                                        if isinstance(choice0.get("delta", {}), dict)
                                                        else {}
                                                    )
                                                    delta = delta_payload.get("content")
                                                    if not delta:
                                                        delta = (
                                                            choice0.get("message", {}).get("content")
                                                            or choice0.get("text")
                                                        )
                                                    if not delta:
                                                        # Reasoning-parser fallback (see other site above).
                                                        delta = (
                                                            delta_payload.get("reasoning_content")
                                                            or choice0.get("message", {}).get("reasoning_content")
                                                        )
                                                    if not isinstance(delta, str):
                                                        delta = self._extract_text_from_content(delta)
                                                    if delta:
                                                        encrypted_chunk = self.crypto_service.encrypt_response(
                                                            {"text": delta},
                                                            cek,
                                                        )
                                                        if encrypted_chunk:
                                                            encrypted_frame = {
                                                                "type": "encrypted_chunk",
                                                                "payload_b64": encrypted_chunk,
                                                            }
                                                            await self.ws.send(
                                                                json.dumps(
                                                                    {
                                                                        "type": "CHUNK",
                                                                        "job_id": job_id,
                                                                        "completion_id": continuation_current_cid,
                                                                        "data": encrypted_frame,
                                                                        "delta": encrypted_frame,
                                                                    }
                                                                )
                                                            )
                                                            total_encrypted_bytes += len(encrypted_chunk) * 3 // 4
                                                            continuation_emitted_chunks += 1

                                                if evt.get("usage"):
                                                    continuation_usage = evt.get("usage")

                                            if continuation_emitted_chunks == 0:
                                                # Some backends may ignore stream=true and return raw JSON body.
                                                # We cannot rely on continuation_resp.text() here because the stream
                                                # has already been consumed in the loop above.
                                                fallback_payload = "\n".join(
                                                    line
                                                    for line in continuation_raw_fallback_buffer
                                                    if not line.startswith("data: ")
                                                ).strip()
                                                try:
                                                    if fallback_payload:
                                                        continuation_json = json.loads(fallback_payload)
                                                        continuation_text = self._extract_text_from_chat_result(continuation_json)
                                                        if continuation_text and continuation_current_cid:
                                                            encrypted_chunk = self.crypto_service.encrypt_response(
                                                                {"text": continuation_text},
                                                                cek,
                                                            )
                                                            if encrypted_chunk:
                                                                encrypted_frame = {
                                                                    "type": "encrypted_chunk",
                                                                    "payload_b64": encrypted_chunk,
                                                                }
                                                                await self.ws.send(
                                                                    json.dumps(
                                                                        {
                                                                            "type": "CHUNK",
                                                                            "job_id": job_id,
                                                                            "completion_id": continuation_current_cid,
                                                                            "data": encrypted_frame,
                                                                            "delta": encrypted_frame,
                                                                        }
                                                                    )
                                                                )
                                                                total_encrypted_bytes += len(encrypted_chunk) * 3 // 4
                                                                continuation_emitted_chunks += 1
                                                        if isinstance(continuation_json, dict) and continuation_json.get("usage"):
                                                            continuation_usage = continuation_json.get("usage")
                                                except Exception as fallback_error:
                                                    logger.warning(
                                                        "Continuation JSON fallback parse failed: %s (preview=%s)",
                                                        fallback_error,
                                                        fallback_payload[:240] if fallback_payload else "",
                                                    )

                                            logger.info(
                                                "Confidential continuation parse summary: raw_lines=%s data_events=%s "
                                                "parsed_events=%s emitted_chunks=%s non_data_samples=%s",
                                                continuation_raw_line_count,
                                                continuation_data_event_count,
                                                continuation_parsed_event_count,
                                                continuation_emitted_chunks,
                                                continuation_non_data_samples,
                                            )
                                            if continuation_emitted_chunks == 0:
                                                logger.warning(
                                                    "Confidential continuation emitted no response chunks "
                                                    "(job_id=%s run_id=%s model=%s)",
                                                    job_id,
                                                    confidential_run_id,
                                                    continuation_payload.get("model"),
                                                )
                                        break
                                    except (aiohttp.ClientConnectionError, aiohttp.ClientOSError, asyncio.TimeoutError, OSError) as cont_err:
                                        continuation_errors.append(str(cont_err))
                                        if continuation_attempt < 2:
                                            logger.warning(
                                                "Continuation transport failed (attempt %s/2), retrying: %s",
                                                continuation_attempt,
                                                cont_err,
                                            )
                                            await asyncio.sleep(0.2)
                                            continue
                                        raise Exception(
                                            f"Continuation request transport failed: {cont_err}"
                                        ) from cont_err

                                if continuation_errors:
                                    logger.info("Continuation transport recovered after retry: errors=%s", continuation_errors)

                                if continuation_current_cid:
                                    current_cid = continuation_current_cid
                                else:
                                    current_cid = requested_completion_id
                                usage = continuation_usage or usage
    
                            # Send END for confidential streaming (chunks already emitted)
                            await self.ws.send(json.dumps({
                                "type": "END",
                                "job_id": job_id,
                                "completion_id": current_cid,
                                "encrypted_output_size": total_encrypted_bytes,
                                "usage": usage
                            }))
                            logger.debug(f"Sent END for confidential streaming: {total_encrypted_bytes} bytes")
    
                            # Keep run context alive only when model requested tool execution.
                            if confidential_run_id and emitted_tool_call_count == 0:
                                self._discard_confidential_run(confidential_run_id)

                        else:
                            # Standard non-streaming response (plaintext or confidential)
                            result = await resp.json()

                            current_cid = result.get("id") or requested_completion_id
    
                            logger.info(f"Captured completion_id from vLLM: {current_cid}")
                            # Update active jobs tracking
                            self.active_jobs.discard(job_id)
                            self.active_jobs.add(current_cid)
    
                            # W2: Encrypt response for confidential mode
                            if is_confidential and cek:
                                encrypted_output = self.crypto_service.encrypt_response(result, cek)
                                if not encrypted_output:
                                    raise Exception("Failed to encrypt response")
    
                                # Calculate actual byte size (base64 decodes to ~3/4 of string length)
                                # This is used by broker for token estimation
                                encrypted_output_bytes = len(encrypted_output) * 3 // 4
    
                                # Send encrypted response with field name broker expects
                                await self.ws.send(json.dumps({
                                    "type": "END",
                                    "job_id": job_id,
                                    "completion_id": current_cid,
                                    "encrypted_output": encrypted_output,
                                    "encrypted_output_size": encrypted_output_bytes,
                                    "usage": result.get("usage", {})
                                }))
                                logger.debug("Sent encrypted response for confidential job")
                                self._discard_confidential_run(confidential_run_id)
                            else:
                                # Plaintext mode - send content as chunks
                                choices = result.get("choices", [])
                                if choices:
                                    content = choices[0].get("message", {}).get("content", "")
                                    if content:
                                        # Send as single chunk
                                        await self.ws.send(json.dumps({
                                            "type": "CHUNK",
                                            "job_id": job_id,
                                            "completion_id": current_cid,
                                            "delta": content
                                        }))
    
                                # Send END with usage
                                await self.ws.send(json.dumps({
                                    "type": "END",
                                    "job_id": job_id,
                                    "completion_id": current_cid,
                                    "usage": result.get("usage", {})
                                }))

                break

            logger.info(f"Completed job {job_id}")

        except Exception as e:
            logger.error(f"Error processing job {job_id}: {e}")
            if is_confidential:
                self._discard_confidential_run(confidential_run_id)
            # Send error - no completion_id if vLLM failed
            try:
                await self.ws.send(json.dumps({
                    "type": "ERROR",
                    "job_id": job_id,
                    "error": str(e)
                }))
            except Exception:
                pass  # WS might be closed
        finally:
            # Clean up tracking - could be either job_id or completion_id
            self.active_jobs.discard(job_id)
            # Note: completion_id will be cleaned up separately if it was added

    async def _handle_responses_api(
        self,
        resp,
        job_id: str,
        payload: Dict[str, Any],
        *,
        is_confidential: bool = False,
        cek: Optional[bytes] = None,
        confidential_run_id: Optional[str] = None,
        room_id: Optional[str] = None,
        requested_completion_id: Optional[str] = None,
        endpoint: Optional[str] = None,
        request_headers: Optional[Dict[str, str]] = None,
    ):
        """Handle OpenAI Responses API format (streaming and non-streaming).

        In confidential mode each native ``response.*`` event is encrypted with
        the room CEK and forwarded as an ``encrypted_response_event`` frame, so
        the broker and gateway stay blind relays and the browser replays a
        native Responses stream after decryption. Function-call events are
        assembled and bridged into the existing ``encrypted_tool_call`` frame so
        the confidential tool loop is reused unchanged.
        """
        is_streaming = payload.get("stream", False)
        current_cid: Optional[str] = None
        response_obj: Optional[Dict[str, Any]] = None
        usage: Dict[str, Any] = {}
        content_type = (resp.headers.get("content-type") or "").lower()

        # Hard guard: a confidential job must never reach a plaintext-forwarding
        # path. If we somehow got here without crypto/cek, fail the job rather
        # than stream user content in the clear.
        if is_confidential and not (self.crypto_service and cek):
            raise Exception("confidential responses job missing encryption context")

        if resp.status >= 400:
            body_preview = (await resp.text())[:500]
            raise Exception(
                f"Responses API returned HTTP {resp.status} (content-type={content_type or '(none)'}): "
                f"{body_preview}"
            )

        if is_streaming and "text/event-stream" not in content_type:
            body_preview = (await resp.text())[:500]
            raise Exception(
                f"Responses API streaming expected text/event-stream but got "
                f"{content_type or '(none)'}: {body_preview}"
            )

        if not is_streaming and "json" not in content_type:
            body_preview = (await resp.text())[:500]
            raise Exception(
                f"Responses API expected JSON but got {content_type or '(none)'}: {body_preview}"
            )

        if is_streaming and is_confidential:
            # Confidential native Responses streaming: encrypt each event.
            await self._handle_confidential_responses_stream(
                resp,
                job_id,
                cek=cek,
                room_id=room_id,
                confidential_run_id=confidential_run_id,
                requested_completion_id=requested_completion_id,
                endpoint=endpoint,
                request_headers=request_headers,
            )
            return

        if not is_streaming and is_confidential:
            # Confidential non-streaming Responses: encrypt the full result as a
            # single response.completed event frame, then close the job. The
            # browser's response_event path finalizes from it.
            result = await resp.json()
            current_cid = result.get("id") or requested_completion_id or job_id
            self.active_jobs.discard(job_id)
            self.active_jobs.add(current_cid)
            completed_event = {"type": "response.completed", "response": result}
            encrypted_event = self.crypto_service.encrypt_response(completed_event, cek)
            if not encrypted_event:
                raise Exception("Failed to encrypt responses result")
            frame = {
                "type": "encrypted_response_event",
                "event_type": "response.completed",
                "payload_b64": encrypted_event,
            }
            await self.ws.send(json.dumps({
                "type": "CHUNK",
                "job_id": job_id,
                "completion_id": current_cid,
                "data": frame,
                "delta": frame,
            }))
            await self.ws.send(json.dumps({
                "type": "END",
                "job_id": job_id,
                "completion_id": current_cid,
                "encrypted_output_size": len(encrypted_event) * 3 // 4,
                "usage": result.get("usage", {}),
            }))
            self._discard_confidential_run(confidential_run_id)
            return

        if is_streaming:
            # Handle streaming Responses API
            async for line in resp.content:
                text = line.decode("utf-8", errors="ignore").strip()
                if not text:
                    continue
                if text == "data: [DONE]":
                    break
                if text.startswith("data: "):
                    try:
                        chunk = json.loads(text[6:])

                        # Capture response ID from any chunk that has it
                        if not current_cid:
                            # Try various fields where ID might appear
                            chunk_id = chunk.get("response_id") or chunk.get("id")
                            if chunk_id:
                                current_cid = chunk_id
                                logger.info(f"Captured response_id from responses API: {current_cid}")
                                self.active_jobs.discard(job_id)
                                self.active_jobs.add(current_cid)
                            # Also check nested response object
                            if not current_cid and isinstance(chunk.get("response"), dict):
                                current_cid = chunk["response"].get("id")
                                if current_cid:
                                    logger.info(f"Captured response_id from nested response: {current_cid}")
                                    self.active_jobs.discard(job_id)
                                    self.active_jobs.add(current_cid)

                        # Forward the chunk to broker with the native format
                        # The broker expects CHUNK messages with the full chunk data
                        await self.ws.send(json.dumps({
                            "type": "CHUNK",
                            "job_id": job_id,
                            "completion_id": current_cid or job_id,
                            "data": chunk,  # Full chunk for responses API
                            "delta": chunk.get("delta", ""),  # Also include delta for backwards compat
                        }))

                        # Capture response.completed event for final response
                        if chunk.get("type") == "response.completed":
                            response_obj = chunk.get("response")
                            if response_obj and isinstance(response_obj, dict):
                                usage = response_obj.get("usage", {})
                                if not current_cid and response_obj.get("id"):
                                    current_cid = response_obj["id"]
                                    self.active_jobs.discard(job_id)
                                    self.active_jobs.add(current_cid)

                        # Also capture usage from standalone usage events
                        if chunk.get("usage"):
                            usage = chunk["usage"]

                    except json.JSONDecodeError as e:
                        logger.error(f"Failed to parse responses SSE chunk: {text[:100]}... error={e}")
                        continue

            # Send END message with the response object
            await self.ws.send(json.dumps({
                "type": "END",
                "job_id": job_id,
                "completion_id": current_cid or job_id,
                "response": response_obj,
                "usage": usage,
            }))
        else:
            # Handle non-streaming Responses API
            result = await resp.json()

            # Extract response ID
            current_cid = result.get("id")
            if current_cid:
                logger.info(f"Captured response_id from responses API: {current_cid}")
                self.active_jobs.discard(job_id)
                self.active_jobs.add(current_cid)
            else:
                logger.warning(f"No response_id in responses API result for job {job_id}")

            # Extract usage
            usage = result.get("usage", {})

            # Send END message with the full response
            await self.ws.send(json.dumps({
                "type": "END",
                "job_id": job_id,
                "completion_id": current_cid or job_id,
                "response": result,
                "usage": usage,
            }))

    async def _handle_confidential_responses_stream(
        self,
        resp,
        job_id: str,
        *,
        cek: bytes,
        room_id: Optional[str],
        confidential_run_id: Optional[str],
        requested_completion_id: Optional[str],
        endpoint: Optional[str] = None,
        request_headers: Optional[Dict[str, str]] = None,
    ) -> None:
        """Stream a native Responses SSE response with per-event encryption.

        Every native ``response.*`` event is encrypted whole and forwarded as an
        ``encrypted_response_event`` frame (only ``event_type`` stays clear, for
        client-side routing — same privacy posture as the existing ``delta`` /
        ``tool_call`` frame categories). Function calls are assembled across
        deltas and handled by tool kind:

          - REMOTE (client-executed) tools → bridged to the existing
            ``encrypted_tool_call`` frame; the client returns an encrypted
            tool_result that re-enters as a fresh confidential continuation turn.
          - WORKER-LOCAL tools (file_search/mcp_proxy) → executed in-worker and
            fed back through an INLINE Responses continuation (re-POST to the
            local ``/v1/responses`` endpoint) with no client round-trip.

        This delivers full confidential tool calling on the native Responses
        path with no fall back to chat-completions framing.
        """
        current_cid: Optional[str] = None
        usage: Dict[str, Any] = {}
        total_encrypted_bytes = 0
        emitted_event_count = 0
        remote_tool_call_count = 0

        async def _emit_encrypted_event(event: Dict[str, Any]) -> None:
            nonlocal total_encrypted_bytes, emitted_event_count
            encrypted = self.crypto_service.encrypt_response(event, cek)
            if not encrypted:
                raise Exception("Failed to encrypt responses event")
            frame = {
                "type": "encrypted_response_event",
                "event_type": event.get("type"),
                "payload_b64": encrypted,
            }
            await self.ws.send(json.dumps({
                "type": "CHUNK",
                "job_id": job_id,
                "completion_id": current_cid or requested_completion_id or job_id,
                "data": frame,
                "delta": frame,
            }))
            total_encrypted_bytes += len(encrypted) * 3 // 4
            emitted_event_count += 1

        async def _process_stream(stream_resp) -> List[Dict[str, Any]]:
            """Process one native Responses SSE stream; return worker-local tool
            results collected this turn (for inline continuation)."""
            nonlocal current_cid, usage, remote_tool_call_count
            fc_items: Dict[str, Dict[str, Any]] = {}
            local_results: List[Dict[str, Any]] = []

            async for line in stream_resp.content:
                text = line.decode("utf-8", errors="ignore").strip()
                if not text:
                    continue
                if text == "data: [DONE]":
                    break
                if not text.startswith("data: "):
                    continue
                try:
                    event = json.loads(text[6:])
                except json.JSONDecodeError:
                    logger.warning("Confidential responses SSE parse failure (preview=%s)", text[:200])
                    continue

                if not isinstance(event, dict):
                    continue

                # Capture response id for completion tracking.
                if not current_cid:
                    chunk_id = event.get("response_id") or event.get("id")
                    if not chunk_id and isinstance(event.get("response"), dict):
                        chunk_id = event["response"].get("id")
                    if chunk_id:
                        current_cid = chunk_id
                        self.active_jobs.discard(job_id)
                        self.active_jobs.add(current_cid)

                etype = event.get("type")

                # Assemble function-call output items across their streamed events.
                # ONLY genuine function-call items are diverted; every other event
                # — text deltas, *message* output items, lifecycle, usage — is
                # forwarded as an encrypted_response_event for native fidelity.
                item = event.get("item") if isinstance(event.get("item"), dict) else {}
                item_id = item.get("id") or event.get("item_id")
                handled_as_function_call = False

                if etype == "response.output_item.added" and item.get("type") == "function_call" and item_id:
                    fc_items[item_id] = {
                        "call_id": item.get("call_id") or item_id,
                        "name": item.get("name") or "",
                        "arguments": item.get("arguments") or "",
                    }
                    handled_as_function_call = True
                elif etype == "response.function_call_arguments.delta":
                    # Always function-call-scoped — no message-item equivalent.
                    if item_id:
                        entry = fc_items.setdefault(
                            item_id, {"call_id": item_id, "name": "", "arguments": ""}
                        )
                        delta = event.get("delta")
                        if isinstance(delta, str):
                            entry["arguments"] += delta
                    handled_as_function_call = True
                elif etype == "response.function_call_arguments.done":
                    if item_id:
                        entry = fc_items.setdefault(
                            item_id, {"call_id": item_id, "name": "", "arguments": ""}
                        )
                        if isinstance(event.get("arguments"), str):
                            entry["arguments"] = event["arguments"]
                    handled_as_function_call = True
                elif etype == "response.output_item.done" and item.get("type") == "function_call":
                    entry = fc_items.get(item_id) if item_id else None
                    call_id = (
                        item.get("call_id")
                        or (entry or {}).get("call_id")
                        or item_id
                        or f"tool_call_{remote_tool_call_count + len(local_results)}"
                    )
                    name = item.get("name") or (entry or {}).get("name") or "unknown_tool"
                    raw_args = item.get("arguments")
                    if not isinstance(raw_args, str) or not raw_args:
                        raw_args = (entry or {}).get("arguments") or "{}"
                    outcome = await self._handle_responses_function_call(
                        job_id=job_id,
                        completion_id=current_cid,
                        cek=cek,
                        room_id=room_id,
                        confidential_run_id=confidential_run_id,
                        tool_call_id=call_id,
                        tool_name=name,
                        raw_arguments=raw_args,
                    )
                    if outcome is not None:
                        # Worker-local tool executed in-worker.
                        local_results.append(outcome)
                    else:
                        # Remote tool: client will execute and continue.
                        remote_tool_call_count += 1
                    handled_as_function_call = True

                if handled_as_function_call:
                    continue

                # Capture terminal/usage before encrypting (server-side accounting).
                if etype == "response.completed" and isinstance(event.get("response"), dict):
                    usage = event["response"].get("usage", usage) or usage
                if event.get("usage"):
                    usage = event["usage"]

                await _emit_encrypted_event(event)

            return local_results

        # Initial model turn.
        local_results = await _process_stream(resp)

        # Inline continuation for worker-local tool results — no client round-trip.
        # Only when there are NO pending remote tool calls (those are continued by
        # the client via an encrypted tool_result). Mixed local+remote turns buffer
        # the local results so they merge into the client-driven continuation.
        MAX_LOCAL_CONTINUATIONS = 3
        attempts = 0
        while local_results and remote_tool_call_count == 0 and attempts < MAX_LOCAL_CONTINUATIONS:
            attempts += 1
            if not (endpoint and request_headers and self.http_session):
                logger.error(
                    "Worker-local Responses tool results pending but no local endpoint to "
                    "continue inline (run_id=%s); results would be lost",
                    confidential_run_id,
                )
                raise Exception("cannot continue worker-local Responses tool results inline")
            continuation_payload = self._build_responses_continuation_payload_from_tool_results(
                confidential_run_id, local_results
            )
            if not continuation_payload:
                raise Exception("failed to build Responses continuation from worker-local tool results")
            continuation_payload["stream"] = True
            logger.info(
                "Continuing confidential Responses run inline with %s worker-local tool result(s) "
                "(run_id=%s attempt=%s)",
                len(local_results),
                confidential_run_id,
                attempts,
            )
            async with self.http_session.post(
                endpoint, json=continuation_payload, headers=request_headers
            ) as cont_resp:
                if cont_resp.status >= 400:
                    body = (await cont_resp.text())[:300]
                    raise Exception(
                        f"Worker-local Responses continuation failed: HTTP {cont_resp.status}: {body}"
                    )
                local_results = await _process_stream(cont_resp)

        if local_results and remote_tool_call_count > 0:
            # Mixed turn: defer local results to the client-driven continuation.
            self._buffer_local_tool_results_for_continuation(confidential_run_id, local_results)

        if not current_cid:
            current_cid = requested_completion_id or job_id

        await self.ws.send(json.dumps({
            "type": "END",
            "job_id": job_id,
            "completion_id": current_cid,
            "encrypted_output_size": total_encrypted_bytes,
            "usage": usage,
        }))
        logger.info(
            "Confidential responses stream complete: events=%s remote_tool_calls=%s bytes=%s",
            emitted_event_count,
            remote_tool_call_count,
            total_encrypted_bytes,
        )

        # Keep run context alive only while a client-driven tool continuation is
        # pending; otherwise (no tools, or all tools resolved inline) the run is done.
        if confidential_run_id and remote_tool_call_count == 0:
            self._discard_confidential_run(confidential_run_id)

    @staticmethod
    def _parse_responses_tool_arguments(raw_arguments: Any) -> Dict[str, Any]:
        if isinstance(raw_arguments, str) and raw_arguments.strip():
            try:
                parsed = json.loads(raw_arguments)
                return parsed if isinstance(parsed, dict) else {"_value": parsed}
            except Exception:
                return {"_raw": raw_arguments}
        return {}

    async def _handle_responses_function_call(
        self,
        *,
        job_id: str,
        completion_id: Optional[str],
        cek: bytes,
        room_id: Optional[str],
        confidential_run_id: Optional[str],
        tool_call_id: str,
        tool_name: str,
        raw_arguments: str,
    ) -> Optional[Dict[str, Any]]:
        """Process an assembled Responses function call.

        Worker-local tools are executed in-worker; their result is returned for
        inline continuation. Remote (client-executed) tools are encrypted and
        bridged to the existing ``encrypted_tool_call`` loop; returns ``None``
        for those so the caller knows a client round-trip is pending.
        """
        if not isinstance(tool_name, str) or not tool_name:
            tool_name = "unknown_tool"

        parsed_arguments = self._parse_responses_tool_arguments(raw_arguments)

        self._remember_confidential_tool_call(
            confidential_run_id,
            tool_call_id,
            tool_name,
            parsed_arguments,
        )

        # Worker-local tool (file_search/mcp_proxy): execute in-worker and return
        # the result for inline continuation — the client cannot run these.
        if self._is_local_worker_tool(tool_name):
            local_result = await self._execute_local_worker_tool(
                tool_name,
                parsed_arguments,
                room_id,
                confidential_run_id,
            )
            return {
                "tool_call_id": tool_call_id,
                "tool_id": tool_name,
                "result": local_result,
            }

        # Remote tool: encrypt + bridge to the existing confidential tool loop.
        encrypted_tool_call = self.crypto_service.encrypt_response(
            {"tool_id": tool_name, "args": parsed_arguments},
            cek,
        )
        if not encrypted_tool_call:
            raise Exception("Failed to encrypt responses tool call")

        encrypted_frame = {
            "type": "encrypted_tool_call",
            "payload_b64": encrypted_tool_call,
            "tool_call_id": tool_call_id,
            "tool_id": tool_name,
        }
        await self.ws.send(json.dumps({
            "type": "CHUNK",
            "job_id": job_id,
            "completion_id": completion_id or job_id,
            "data": encrypted_frame,
            "delta": encrypted_frame,
        }))
        return None

    async def _send_job_error(self, job_id: str, error: str):
        """Send error response for a job"""
        try:
            await self.ws.send(json.dumps({
                "type": "ERROR",
                "job_id": job_id,
                "error": error
            }))
        except Exception:
            pass  # WS might be closed
        finally:
            self.active_jobs.discard(job_id)

    async def _handle_proof_request(self, msg: Dict[str, Any]):
        """Fetch local miner proof for completion_id and return to broker."""
        cid = msg.get("completion_id")
        if not cid:
            logger.warning("Received PROOF_REQUEST without completion_id")
            return

        logger.info(f"Handling proof request for completion_id: {cid}")

        try:
            async with self.http_session.get(
                f"{self.miner_proxy_url}/v1/proof/{cid}",
                timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status == 200:
                    blob = await resp.read()
                    if len(blob) == 0:
                        raise Exception("Empty proof blob received")

                    await self.ws.send(json.dumps({
                        "type": "PROOF_RESULT",
                        "completion_id": cid,
                        "proof_b64": base64.b64encode(blob).decode()
                    }))
                    logger.info(f"Sent proof for completion_id: {cid} ({len(blob)} bytes)")
                elif resp.status == 404:
                    # Proof not yet available - broker may retry
                    await self.ws.send(json.dumps({
                        "type": "PROOF_RESULT",
                        "completion_id": cid,
                        "error": "proof_not_ready"
                    }))
                    logger.warning(f"Proof not ready for {cid}")
                else:
                    error_msg = await resp.text()
                    await self.ws.send(json.dumps({
                        "type": "PROOF_RESULT",
                        "completion_id": cid,
                        "error": f"http_{resp.status}:{error_msg}"
                    }))
                    logger.error(f"Proof request failed for {cid}: HTTP {resp.status} - {error_msg}")
        except asyncio.TimeoutError:
            await self.ws.send(json.dumps({
                "type": "PROOF_RESULT",
                "completion_id": cid,
                "error": "proof_timeout"
            }))
            logger.error(f"Timeout fetching proof for {cid}")
        except Exception as e:
            try:
                await self.ws.send(json.dumps({
                    "type": "PROOF_RESULT",
                    "completion_id": cid,
                    "error": f"internal_error:{str(e)}"
                }))
            except Exception:
                pass
            logger.error(f"Error fetching proof for {cid}: {e}")

    async def _heartbeat_loop(self, ws=None):
        """Send periodic metrics to broker"""
        consecutive_failures = 0
        max_failures = 3
        ws = ws or self.ws
        exit_reason = "loop_condition_false"

        try:
            while ws and self.ws is ws and self.running:
                try:
                    # Check if websocket is still open
                    try:
                        from websockets.protocol import State
                        if ws.state != State.OPEN:
                            exit_reason = "websocket_not_open"
                            logger.warning("Heartbeat detected closed connection")
                            break
                    except (AttributeError, ImportError):
                        pass

                    heartbeat = {
                        "type": "HEARTBEAT",
                        "busy": len(self.active_jobs),
                        "input_tokens_per_sec": await self._get_input_tps(),
                        "output_tokens_per_sec": await self._get_output_tps(),
                        "error_rate": 0.0,
                        "queue_depth": 0
                    }
                    await asyncio.wait_for(
                        ws.send(json.dumps(heartbeat)),
                        timeout=self.heartbeat_send_timeout,
                    )
                    self.last_heartbeat_sent_at = time.time()
                    logger.debug(f"Sent heartbeat: busy={heartbeat['busy']}")
                    consecutive_failures = 0  # Reset on success

                    try:
                        self._purge_stale_mining_jobs()
                    except Exception as purge_exc:  # noqa: BLE001
                        logger.warning(f"Stale mining-job purge failed: {purge_exc}")

                except asyncio.CancelledError:
                    exit_reason = "cancelled"
                    raise
                except websockets.exceptions.ConnectionClosed as e:
                    exit_reason = f"connection_closed:{e.code}"
                    logger.warning(f"Heartbeat failed - connection closed: code={e.code}")
                    break
                except Exception as e:
                    consecutive_failures += 1
                    exit_reason = f"{type(e).__name__}:{e}"
                    logger.error(f"Error sending heartbeat ({consecutive_failures}/{max_failures}): {e}")
                    if consecutive_failures >= max_failures:
                        logger.error("Too many heartbeat failures, forcing broker reconnect")
                        break

                await asyncio.sleep(self.heartbeat_interval)

        finally:
            logger.info("Heartbeat loop exited: %s", exit_reason)
            if exit_reason != "cancelled" and self.running and self.ws is ws and ws is not None:
                try:
                    await ws.close(code=1011, reason=f"heartbeat_failed:{exit_reason}"[:120])
                except Exception as exc:  # noqa: BLE001
                    logger.debug(f"Failed to close websocket after heartbeat exit: {exc}")

    _MINING_JOB_TTL_SEC = 900.0

    def _purge_stale_mining_jobs(self) -> int:
        """Drop mining-job tracking entries older than the TTL.

        The solution/share path pops entries itself; this catches the
        common case where the broker's 60s lease times out and the
        worker is never told. 15 min is far beyond any legitimate
        result latency — a late solution for a purged job would be
        tombstone-dropped by the broker anyway.
        """
        now = time.time()
        # Lazily stamp anything tracked before this code ran (or via a
        # path that skipped the stamp) so it ages out next cycle.
        for rid in self.mining_request_mapping:
            self._mining_job_seen_at.setdefault(rid, now)
        stale = [
            rid for rid, ts in self._mining_job_seen_at.items()
            if now - ts > self._MINING_JOB_TTL_SEC
        ]
        for rid in stale:
            job_id = self.mining_request_mapping.pop(rid, None)
            if job_id is not None:
                self.mining_job_mapping.pop(job_id, None)
            self._mining_in_flight.pop(rid, None)
            self._mining_job_seen_at.pop(rid, None)
        if stale:
            logger.info(
                "Purged %d stale mining-job entries (tracked=%d)",
                len(stale), len(self.mining_request_mapping),
            )
        return len(stale)

    async def _get_input_tps(self) -> float:
        """Get input tokens per second metric from miner proxy status"""
        try:
            async with self.http_session.get(
                f"{self.miner_proxy_url}/status",
                timeout=aiohttp.ClientTimeout(total=2)
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    proxy_status = data.get("proxy", {})
                    # Look for input token rate metrics
                    return float(proxy_status.get("input_tokens_per_sec", 0.0))
        except Exception as e:
            logger.debug(f"Failed to get input TPS: {e}")
        return 0.0

    async def _get_output_tps(self) -> float:
        """Get output tokens per second metric from miner proxy status"""
        try:
            async with self.http_session.get(
                f"{self.miner_proxy_url}/status",
                timeout=aiohttp.ClientTimeout(total=2)
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    proxy_status = data.get("proxy", {})
                    # Look for output token rate metrics
                    return float(proxy_status.get("output_tokens_per_sec", 0.0))
        except Exception as e:
            logger.debug(f"Failed to get output TPS: {e}")
        return 0.0

    def get_status(self) -> Dict[str, Any]:
        """Get current worker status"""
        now = time.time()

        def age(ts: Optional[float]) -> Optional[float]:
            if ts is None:
                return None
            return round(max(0.0, now - ts), 1)

        # websockets library uses .state instead of .closed
        connected = False
        if self.ws is not None:
            try:
                # websockets >= 10.0 uses .state
                from websockets.protocol import State
                connected = self.ws.state == State.OPEN
            except (AttributeError, ImportError):
                # Fallback: check if ws exists and hasn't been closed
                connected = self.ws is not None
        return {
            "worker_id": self.worker_id,
            "connected": connected,
            "broker_url": self.broker_url,
            "active_jobs": len(self.active_jobs),
            "active_mining_jobs": len(self.mining_job_mapping),
            "registered_tools": list(self.registered_tools),
            "mining_enabled": self.mining_enabled,
            "running": self.running,
            "heartbeat_task_running": (
                self.heartbeat_task is not None and not self.heartbeat_task.done()
            ),
            "heartbeat_interval_seconds": self.heartbeat_interval,
            "connection_attempts": self.connection_attempts,
            "last_reconnect_error": self.last_reconnect_error,
            "last_connect_attempt_age_seconds": age(self.last_connect_attempt_at),
            "last_connected_age_seconds": age(self.last_connected_at),
            "last_disconnected_age_seconds": age(self.last_disconnected_at),
            "last_message_age_seconds": age(self.last_message_at),
            "last_ack_age_seconds": age(self.last_ack_at),
            "last_heartbeat_sent_age_seconds": age(self.last_heartbeat_sent_at),
        }

    # =========================================================================
    # W3: Tool Capability Registration
    # =========================================================================

    async def _register_tools(self):
        """Register configured tools with broker after ACK"""
        if not self.worker_tools:
            logger.debug("No tools configured for registration")
            return

        for tool_config in self.worker_tools:
            tool_id = tool_config.get("tool_id")
            if not tool_id:
                logger.warning(f"Tool config missing tool_id: {tool_config}")
                continue

            try:
                await self.ws.send(json.dumps({
                    "type": "TOOL_REGISTER",
                    "tool_id": tool_id,
                    "encryption": tool_config.get("encryption", "none"),
                    "schema_ref": tool_config.get("schema_ref", ""),
                    "executor": tool_config.get("executor", "worker")
                }))
                logger.info(f"Sent TOOL_REGISTER for {tool_id}")
            except Exception as e:
                logger.error(f"Failed to register tool {tool_id}: {e}")

    async def _unregister_tools(self):
        """Unregister all registered tools on shutdown"""
        for tool_id in list(self.registered_tools):
            try:
                await self.ws.send(json.dumps({
                    "type": "TOOL_UNREGISTER",
                    "tool_id": tool_id
                }))
                logger.info(f"Sent TOOL_UNREGISTER for {tool_id}")
            except Exception as e:
                logger.error(f"Failed to unregister tool {tool_id}: {e}")

    # =========================================================================
    # W4: Mining Sidecar
    # Replaces ZMQ as the source of mining jobs (MINE_REQUEST) and
    # forwards solutions to broker (MINE_RESULT) instead of to core-node via ZMQ.
    # =========================================================================

    async def _handle_model_registry_sync(self, msg: Dict[str, Any]) -> None:
        """Slice 9: broker-pushed chain model registry.

        The payload's ``models`` field carries the same JSON shape the
        worker's :class:`ModelClient` would have fetched from
        ``{MODEL_API_URL}/api/v1/models?extended=true``. We forward
        verbatim to :meth:`ModelClient.update_from_payload`, which
        applies the same ``status == 2`` filter and indexing as the
        local fetch path. The broker is the trust boundary here — we
        deliberately do NOT re-validate the list contents on the
        worker side.

        No-op (with a warning) when:

          - ``request_manager`` was not injected (older callers); or
          - The proxy's ``ModelClient`` is None (broker inference-only
            mode, where the worker doesn't care about the registry).
        """
        if self.request_manager is None:
            logger.warning(
                "[worker] MODEL_REGISTRY_SYNC received but request_manager "
                "is None; dropping. Wire RequestManager into "
                "BrokerWorkerClient if you intend to consume broker-pushed "
                "registry sync."
            )
            return
        model_client = getattr(self.request_manager, "model_client", None)
        if model_client is None:
            logger.debug(
                "[worker] MODEL_REGISTRY_SYNC received but the local "
                "ModelClient is disabled (broker inference-only mode); "
                "ignoring."
            )
            return
        models = msg.get("models") or []
        if not isinstance(models, list):
            logger.warning(
                "[worker] MODEL_REGISTRY_SYNC carried non-list models "
                "field (got %s); ignoring",
                type(models).__name__,
            )
            return
        try:
            await model_client.update_from_payload(models, source="broker-push")
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "[worker] MODEL_REGISTRY_SYNC update_from_payload raised: %s",
                exc,
            )

    async def _handle_mine_request(self, msg: Dict[str, Any]):
        """Phase 3: typed MINE_REQUEST handler.

        Validates the typed schema, mints a BlockHeader FlatBuffer from
        the broker-supplied template, and feeds it into
        ``zmq_listener._process_mining_job`` — the same downstream entry
        point the legacy ZMQ source used. The conversion is the
        BrokerMiningContextAdapter from COMPUTE_BROKER_IMPROV.md, inlined
        here so we don't introduce a new class for what's effectively
        a per-request transform.
        """
        from components.mining_protocol import (  # local import to avoid early-import cost
            MineRequest, MineResult, MiningProtocolError,
        )

        # Step 1 — protocol validation. We need at least job_id to send a
        # structured rejection back; everything else flows through schema.
        job_id = msg.get("job_id") if isinstance(msg, dict) else None
        if not job_id:
            logger.warning("Received MINE_REQUEST without job_id")
            return

        if not self.mining_enabled:
            logger.warning(
                "Refusing MINE_REQUEST %s: mining disabled on this worker "
                "(MINING_ENABLED=%s, context_attached=%s)",
                job_id, constants.MINING_ENABLED, self.context is not None,
            )
            await self._send_mine_result_raw(job_id, error="mining_disabled")
            return

        if not self.zmq_listener:
            logger.error(f"MINE_REQUEST {job_id} but no zmq_listener available")
            await self._send_mine_result_raw(job_id, error="mining_not_available")
            return

        try:
            request = MineRequest.from_dict(msg)
        except MiningProtocolError as exc:
            logger.warning("MINE_REQUEST %s schema validation failed: %s", job_id, exc)
            await self._send_mine_result_raw(job_id, error=f"invalid_payload: {exc}")
            return

        logger.info(
            "Received MINE_REQUEST: job_id=%s req_id=%d network=%s mode=%s template=%s",
            request.job_id, request.work_unit_id, request.network,
            request.mode, request.template.template_id,
        )

        try:
            header_fb = self._build_block_header_fb(request)
        except Exception as exc:  # noqa: BLE001
            logger.error("MINE_REQUEST %s: BlockHeader construction failed: %s", job_id, exc)
            await self._send_mine_result_typed(
                MineResult.from_request(request, worker_id=self.worker_id, agent_id=self._agent_id_for_result()),
                error=f"invalid_payload: {exc}",
            )
            return

        request_id = request.template.request_id
        self.mining_job_mapping[request.job_id] = request_id
        self.mining_request_mapping[request_id] = request.job_id
        self._mining_in_flight[request_id] = request
        self._mining_job_seen_at[request_id] = time.time()
        # The broker supplies base_share_target (unadjusted, base
        # domain). It rides into the context VERBATIM; the proxy
        # derives adjusted_share_target per request at PoW-injection
        # time (the adjustment needs the selected model's difficulty)
        # and the sampler dual-threshold gates on it. It is NOT the
        # mining target — the proof stays bound to the chain
        # (model-adjusted block) difficulty.
        logger.info("Tracking mining job: job_id=%s ↔ request_id=%d", request.job_id, request_id)

        try:
            self.zmq_listener._process_mining_job(
                header_fb, base_share_target=request.template.base_share_target,
            )
            logger.info("Mining job %s injected into context", request.job_id)
        except Exception as exc:  # noqa: BLE001
            logger.error("MINE_REQUEST %s: _process_mining_job failed: %s", request.job_id, exc)
            # Roll back tracking so a retry can re-attempt cleanly.
            self.mining_job_mapping.pop(request.job_id, None)
            self.mining_request_mapping.pop(request_id, None)
            self._mining_in_flight.pop(request_id, None)
            await self._send_mine_result_typed(
                MineResult.from_request(request, worker_id=self.worker_id, agent_id=self._agent_id_for_result()),
                error=f"processing_error: {exc}",
            )

    def _build_block_header_fb(self, request: "MineRequest") -> bytes:  # noqa: F821
        """Convert typed MineRequest.template into the BlockHeader FlatBuffer.

        ZMQListener._process_mining_job consumes a serialised
        ``proof::BlockHeader`` (req_id, version, prev hash, merkle root,
        timestamp, bits). The 76-byte header_prefix carries the same
        fields in fixed-offset positions matching bcore's encoding
        (mining.cpp:EncodeHeaderPrefix); we reverse them here.
        """
        import flatbuffers
        from proof import BlockHeader

        prefix_bytes = bytes.fromhex(request.template.header_prefix)
        if len(prefix_bytes) != 76:
            raise ValueError(
                f"header_prefix decoded to {len(prefix_bytes)} bytes; expected 76"
            )

        n_version = int.from_bytes(prefix_bytes[0:4], "little")
        prev_block_le = prefix_bytes[4:36]
        merkle_root_le = prefix_bytes[36:68]
        n_time = int.from_bytes(prefix_bytes[68:72], "little")
        n_bits = int.from_bytes(prefix_bytes[72:76], "little")

        builder = flatbuffers.Builder(1024)
        prev_hash = builder.CreateByteVector(prev_block_le)
        merkle_root = builder.CreateByteVector(merkle_root_le)

        BlockHeader.BlockHeaderStart(builder)
        BlockHeader.BlockHeaderAddVersion(builder, n_version)
        BlockHeader.BlockHeaderAddPrevBlockHash(builder, prev_hash)
        BlockHeader.BlockHeaderAddMerkleRoot(builder, merkle_root)
        BlockHeader.BlockHeaderAddTimestamp(builder, n_time)
        BlockHeader.BlockHeaderAddBits(builder, n_bits)
        BlockHeader.BlockHeaderAddReqId(builder, request.template.request_id)
        header = BlockHeader.BlockHeaderEnd(builder)
        builder.Finish(header)
        return bytes(builder.Output())

    def _agent_id_for_result(self) -> Optional[str]:
        try:
            if self.crypto_service:
                aid = self.crypto_service.get_agent_id()
                return aid or None
        except Exception:  # noqa: BLE001
            pass
        return None

    def _on_solution_received(self, req_id: int, mining_buf: bytes):
        """Callback from proof_collector when a proof is received.

        Slice 11.4: ProofCollector now sees BOTH block-tier solutions
        AND sub-block share-tier proofs (the sampler emits whenever
        ``digest <= share_target`` OR ``digest <= block_target``).
        Classify here and route:

          - ``Proof.is_solution == True``  → MineResult path (block hit).
            The broker also credits the matching share inside its
            MineResult handler (``credit_block_hit_share``).
          - ``Proof.is_solution == False`` AND we're in broker share-
            mode (request had ``template.base_share_target``) →
            sub-block share emission; goes to MineShare path.
          - Otherwise (e.g. legacy proxy-audit proof) → drop on the
            broker-mode worker; the audit path is independent of
            slice 11.

        Runs in the proof_collector thread; schedules the async work
        on the main event loop.
        """
        job_id = self.mining_request_mapping.get(req_id)
        if not job_id:
            logger.debug(f"Solution received for unknown req_id={req_id}, not from broker")
            return

        if not (self._loop and self.running):
            return

        result_b64 = base64.b64encode(mining_buf).decode()

        # Classify on the FlatBuffer's ``is_solution`` boolean.
        # Decode is cheap (a single FlatBuffer field read) and the
        # underlying mining_buf is bound to this callback frame, so
        # there's no race with mutation.
        is_block_solution = True  # safe default: legacy behaviour
        try:
            from components.proof_collector import _extract_proof_hash_hex  # noqa: F401
            # Lazy import the FB parser the broker uses; same vendored
            # _vendored_fb path the worker uses for its own decode.
            from components.proof_collector import _extract_is_solution  # type: ignore
            parsed = _extract_is_solution(mining_buf)
            if parsed is not None:
                is_block_solution = bool(parsed)
        except Exception as exc:  # noqa: BLE001
            # Module missing the helper OR FB parse failed — fall
            # through to the legacy "treat as block solution" path
            # so we don't silently drop block hits while wiring up.
            logger.debug(
                "Could not classify proof for req_id=%d: %s; defaulting "
                "to block-solution path", req_id, exc,
            )

        if is_block_solution:
            logger.info(
                "Block-tier solution received for broker job_id=%s "
                "(req_id=%d)", job_id, req_id,
            )
            asyncio.run_coroutine_threadsafe(
                self._forward_solution(job_id, req_id, result_b64),
                self._loop,
            )
        else:
            logger.info(
                "Sub-block share-tier proof received for broker "
                "job_id=%s (req_id=%d); routing as MineShare",
                job_id, req_id,
            )
            asyncio.run_coroutine_threadsafe(
                self._forward_share(
                    job_id=job_id, req_id=req_id,
                    mining_buf=mining_buf, share_b64=result_b64,
                ),
                self._loop,
            )


    async def _forward_solution(self, job_id: str, req_id: int, result_b64: str):
        """Forward a worker-side solution to the broker as a typed MINE_RESULT.

        Slice 11 (revised): a block-tier hit is also a share by
        definition (block target ⊂ share target). The broker
        now credits that share INTERNALLY in its MineResult
        handler (mining_scheduler._await_and_route → AFTER chain
        submit, BEFORE lease close — see ShareVerifier.
        credit_block_hit_share). The worker therefore emits ONLY
        MineResult here; emitting MineShare separately introduced
        a race where the share frame arrived after the broker
        closed the lease and got REJECT_LEASE_INACTIVE.

        Sub-block-but-above-share proofs (the bulk of real share
        volume) are NOT yet emitted by the underlying C++ miner;
        when that lands, those proofs arrive at this layer via
        a separate code path (proof_collector classifies; the
        sampler emits MineShare via ``_forward_share``).
        """
        from components.mining_protocol import MineResult

        request = self._mining_in_flight.get(req_id)
        if request is None:
            logger.warning(
                "Solution arrived for req_id=%d (job_id=%s) but no in-flight MineRequest cached; "
                "broker may have evicted the lease", req_id, job_id,
            )
            await self._send_mine_result_raw(job_id, error="no_in_flight_context")
            self.mining_job_mapping.pop(job_id, None)
            self.mining_request_mapping.pop(req_id, None)
            return

        result = MineResult.from_request(
            request,
            worker_id=self.worker_id,
            agent_id=self._agent_id_for_result(),
        )
        result.solution_b64 = result_b64
        try:
            await self._send_mine_result_typed(result)
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to forward solution for job_id=%s: %s", job_id, exc)
            return
        finally:
            self.mining_job_mapping.pop(job_id, None)
            self.mining_request_mapping.pop(req_id, None)
            self._mining_in_flight.pop(req_id, None)
        logger.info("Forwarded solution for job_id=%s to broker", job_id)

    async def _send_mine_result_typed(
        self,
        result: "MineResult",  # noqa: F821
        *,
        error: Optional[str] = None,
    ) -> None:
        """Emit a typed MINE_RESULT, optionally overriding the error field."""
        if error is not None:
            result.error = error
            result.solution_b64 = None
        if not self.ws:
            logger.warning(
                "Cannot send MINE_RESULT for job_id=%s: no WebSocket connection",
                result.job_id,
            )
            return
        try:
            await self.ws.send(json.dumps(result.to_dict()))
            if result.error:
                logger.warning("Sent MINE_RESULT for %s with error: %s", result.job_id, result.error)
            else:
                logger.info("Sent MINE_RESULT for %s", result.job_id)
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to send MINE_RESULT for %s: %s", result.job_id, exc)

    async def _forward_share(
        self, job_id: str, req_id: int, mining_buf: bytes, share_b64: str,
    ) -> None:
        """Slice 11: forward a share to the broker.

        Mirrors :meth:`_forward_solution` but:

          - The in-flight MineRequest is NOT popped — the same lease
            can produce many shares before (or alongside) a block-
            level solution. The MINE_RESULT path pops; shares keep
            the lease alive.
          - Builds a MINE_SHARE frame (different ``type``, different
            field set) instead of MINE_RESULT.

        Share threshold is the worker-derived
        ``adjusted_share_target = floor(base_share_target * N / D)``
        where N is the chain ``ModelDifficultyNormalizer``
        (env ``MODEL_DIFFICULTY_NORMALIZER``) and D is
        ``model.difficulty`` from the worker's ``ModelClient``
        snapshot. Broker re-derives the same value from its
        pinned registry and uses it as the X-Target-Override
        when verifying the share via verify-service. Worker and
        broker MUST agree byte-exactly; if they diverge the
        broker will reject the share as ``above_share_target``.

        Fail-soft on any prerequisite missing
        (base_share_target unset, normalizer=0, model not in
        registry): log a warning and skip share emission. The
        MineResult path is independent and unaffected.

        ``mining_buf`` is the raw FlatBuffer bytes (used to extract
        nonce + achieved_hash for the wire payload); ``share_b64`` is
        the same bytes b64-encoded for the ``proof_b64`` field.
        """
        from components import constants as _constants
        from components.mining_protocol import (
            MineShare,
            MiningProtocolError,
            derive_adjusted_share_target,
        )
        from components.proof_collector import (
            _extract_proof_hash_hex,
            _extract_proof_nonce,
        )

        request = self._mining_in_flight.get(req_id)
        if request is None:
            logger.warning(
                "Share arrived for req_id=%d (job_id=%s) but no in-flight MineRequest cached; "
                "broker may have evicted the lease — dropping share",
                req_id, job_id,
            )
            return

        base_share_target = request.template.base_share_target
        if not base_share_target:
            # Broker didn't supply a share-mode threshold for this
            # job. Block-only mining; nothing to emit.
            logger.debug(
                "Skipping MINE_SHARE for req_id=%d: template.base_share_target is empty "
                "(broker is not in share-mode for this lease)",
                req_id,
            )
            return

        normalizer = int(getattr(_constants, "MODEL_DIFFICULTY_NORMALIZER", 0) or 0)
        if normalizer <= 0:
            logger.warning(
                "Skipping MINE_SHARE for req_id=%d: MODEL_DIFFICULTY_NORMALIZER unset "
                "or zero. Set it to the chain ModelDifficultyNormalizer (e.g. 1000000 "
                "for TensorMain) to enable share emission.",
                req_id,
            )
            return

        # Look up the model's difficulty in the worker's pinned
        # registry snapshot via ModelClient. We use the broker-
        # supplied (name, commit) tuple — the broker authored the
        # MineRequest and is the binding source for which model
        # this lease was dispatched against.
        model_client = None
        if self.request_manager is not None:
            model_client = getattr(self.request_manager, "model_client", None)
        if model_client is None:
            logger.warning(
                "Skipping MINE_SHARE for req_id=%d: no ModelClient available "
                "(request_manager not wired); cannot resolve model.difficulty",
                req_id,
            )
            return
        record = model_client.get_model_by_name_and_commit(
            request.model.name, request.model.commit,
        )
        if not record:
            logger.warning(
                "Skipping MINE_SHARE for req_id=%d: model %s@%s not in worker's "
                "registry snapshot; refusing to guess difficulty",
                req_id, request.model.name, request.model.commit,
            )
            return
        difficulty = record.get("difficulty")
        try:
            difficulty = int(difficulty) if difficulty is not None else 0
        except (TypeError, ValueError):
            difficulty = 0
        if difficulty <= 0:
            logger.warning(
                "Skipping MINE_SHARE for req_id=%d: model %s@%s has invalid "
                "difficulty=%r in registry",
                req_id, request.model.name, request.model.commit, difficulty,
            )
            return

        try:
            adjusted_share_target = derive_adjusted_share_target(
                base_share_target_hex=base_share_target,
                normalizer=normalizer,
                difficulty=difficulty,
            )
        except MiningProtocolError as exc:
            logger.warning(
                "Skipping MINE_SHARE for req_id=%d: adjusted target derivation "
                "failed: %s",
                req_id, exc,
            )
            return

        achieved_hash = _extract_proof_hash_hex(mining_buf) or ""
        nonce = _extract_proof_nonce(mining_buf) or 0
        share = MineShare.from_request(
            request,
            nonce=int(nonce),
            achieved_hash=achieved_hash,
            share_target=adjusted_share_target,
            worker_id=self.worker_id,
            agent_id=self._agent_id_for_result(),
            proof_b64=share_b64,
        )
        try:
            await self._send_mine_share_typed(share)
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to forward share for job_id=%s: %s", job_id, exc)

    async def _send_mine_share_typed(self, share: "MineShare") -> None:  # noqa: F821
        """Emit a typed MINE_SHARE frame to the broker."""
        if not self.ws:
            logger.warning(
                "Cannot send MINE_SHARE for job_id=%s: no WebSocket connection",
                share.job_id,
            )
            return
        try:
            await self.ws.send(json.dumps(share.to_dict()))
            logger.info(
                "Sent MINE_SHARE for %s req_id=%d nonce=%d",
                share.job_id, share.request_id, share.nonce,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "Failed to send MINE_SHARE for %s: %s", share.job_id, exc,
            )

    async def _send_mine_result_raw(
        self,
        job_id: str,
        *,
        error: str,
    ) -> None:
        """Emit a structurally minimal MINE_RESULT when the typed
        correlation set isn't available (mining_disabled, request never
        validated, lease evicted before solution returned).

        Carries enough for the broker to release the lease — it cannot
        carry work_unit_id / wallet_id / network because those came from
        the request that we couldn't or didn't accept.
        """
        if not self.ws:
            logger.warning(f"Cannot send MINE_RESULT for {job_id}: no WebSocket connection")
            return
        try:
            await self.ws.send(json.dumps({
                "type": "MINE_RESULT",
                "job_id": job_id,
                "error": error,
            }))
            logger.warning("Sent MINE_RESULT for %s with error: %s", job_id, error)
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to send MINE_RESULT for %s: %s", job_id, exc)

    # Compatibility shim: keep _send_mine_result on the class signature so
    # any internal reference (or future tests) that calls it still works.
    # Phase 3: forwards into the typed/raw helpers based on what the
    # caller supplied. Callers that already migrated to the typed schema
    # should use _send_mine_result_typed / _send_mine_result_raw directly.
    async def _send_mine_result(
        self,
        job_id: str,
        result_b64: Optional[str] = None,
        error: Optional[str] = None,
    ) -> None:
        if error is not None and result_b64 is None:
            await self._send_mine_result_raw(job_id, error=error)
            return
        # No typed context available here, but solutions should always
        # flow through _forward_solution which calls _send_mine_result_typed
        # directly. If we somehow land here with a raw result_b64 it's a
        # bug in a caller; emit a degraded MINE_RESULT and log loudly.
        logger.error(
            "_send_mine_result called with raw result_b64 for job_id=%s; "
            "this path no longer carries typed correlation. Routing as raw.",
            job_id,
        )
        if not self.ws:
            return
        try:
            payload = {"type": "MINE_RESULT", "job_id": job_id}
            if result_b64:
                payload["solution_b64"] = result_b64
            if error:
                payload["error"] = error
            await self.ws.send(json.dumps(payload))
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to send legacy MINE_RESULT for %s: %s", job_id, exc)
