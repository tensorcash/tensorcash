"""
Main application entry point for the mining proxy service
Exposes:
- HTTP proxy (aiohttp)
- MCP over HTTP (Streamable HTTP) at /mcp on its own port (optional)
"""

import asyncio
import contextlib
import logging
import signal
import sys
import json
from aiohttp import web

from components.context import LockFreeContext
from components import constants
from components.vdf_service import VDFService
from components.zmq_listener import ZMQListener
# Choose the request manager based on PRIORITY_MODE
try:
    if constants.PRIORITY_MODE:
        from components.proxy_with_priority import PriorityRequestManager as RequestManager
    else:
        from components.proxy import RequestManager
except Exception:  # Fallback to base manager on any import issue
    from components.proxy import RequestManager
from components.proof_cache import ProofCache
from components.proof_collector import ProofCollector

# -------- MCP over HTTP imports --------
try:
    from mcp.server.fastmcp import FastMCP, Context
    import uvicorn
    MCP_AVAILABLE = True
except ImportError:
    MCP_AVAILABLE = False
# ---------------------------------------

# ---------- Logging ----------
logging.basicConfig(
    level=getattr(logging, constants.LOG_LEVEL),
    format=constants.LOG_FORMAT
)
logger = logging.getLogger(__name__)
# ----------------------------


# ---------- MCP server (tools) ----------
def build_mcp_server(app: "MiningProxyApp") -> "FastMCP":
    """
    Create a FastMCP server that exposes your tools over HTTP.
    Tools:
      - proxy_status        -> status JSON
      - chat_completion     -> forwards to your local /v1/chat/completions with PoW injection
    """
    mcp = FastMCP("mining-proxy")

    @mcp.tool()
    async def proxy_status() -> str:
        """Get mining proxy status as JSON."""
        status = {
            "context": app.context.get_status(),
            "vdf": app.vdf_service.get_status(),
            "zmq": app.zmq_listener.get_status(),
            "proxy": app.request_manager.get_status(),
        }
        return json.dumps(status, indent=2)

    @mcp.tool()
    async def chat_completion(
        messages: list[dict],
        model: str = "Qwen/Qwen3-8B",
        max_tokens: int = 256,
        temperature: float = 0.7,
        ctx: Context | None = None,
    ) -> str:
        """
        Generate chat completion with PoW injection by proxying to your local OpenAI-compatible endpoint.
        """
        data = {
            "messages": messages,
            "model": model,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        # Inject PoW details
        modified_data = app.request_manager._inject_pow_data(data)

        async with app.request_manager.session.post(
            f"{app.request_manager.target_url}/v1/chat/completions",
            json=modified_data,
            headers=app.request_manager.auth_headers
        ) as resp:
            # Bubble up upstream HTTP errors with context
            if resp.status >= 400:
                body = await resp.text()
                raise RuntimeError(f"Upstream /v1/chat/completions error {resp.status}: {body}")

            result = await resp.json()
            if "choices" in result and result["choices"]:
                return result["choices"][0].get("message", {}).get("content", json.dumps(result))
            return json.dumps(result, indent=2)

    return mcp

# ---------- Uvicorn server hosting MCP Streamable HTTP ----------
class MCPHttpServer:
    """
    Runs the FastMCP Streamable HTTP ASGI app directly (no Mount/mux).
    The FastMCP app in your SDK already exposes POST /mcp.
    """
    def __init__(self, app: "MiningProxyApp", host: str = "0.0.0.0", port: int = 8090):
        self._app = app
        self._host = host
        self._port = port
        self._uvicorn_server = None
        self._task = None

    async def start(self):
        if not MCP_AVAILABLE:
            logger.warning("MCP not available (pip install 'mcp[cli]' uvicorn)")
            return

        m = build_mcp_server(self._app)

        # IMPORTANT: Use the app exactly as returned; it already registers /mcp.
        asgi = m.streamable_http_app()

        # (Optional) path logger to verify requests hitting /mcp
        class _LogPath:
            def __init__(self, app): self.app = app
            async def __call__(self, scope, receive, send):
                if scope.get("type") == "http":
                    print(f"[mcp-streamable] path={scope.get('path')!r}", file=sys.stderr, flush=True)
                return await self.app(scope, receive, send)

        config = uvicorn.Config(
            _LogPath(asgi),
            host=self._host,
            port=self._port,
            log_level="info",
            loop="asyncio",
            proxy_headers=True,  # harmless if not behind a proxy
        )
        self._uvicorn_server = uvicorn.Server(config)
        self._task = asyncio.create_task(self._uvicorn_server.serve())
        logger.info("MCP Streamable HTTP server running on %s:%d (path: /mcp)", self._host, self._port)

    async def stop(self):
        if self._uvicorn_server and not self._uvicorn_server.should_exit:
            self._uvicorn_server.should_exit = True
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=3.0)
            except asyncio.TimeoutError:
                pass
# ----------------------------------------------------------


class MiningProxyApp:
    """Main application that coordinates all services"""

    def __init__(self):
        # Initialize shared context
        self.context = LockFreeContext(constants.DEFAULT_BLOCK_HASH, constants.BASE_NBITS)

        # Initialize services
        self.vdf_service = VDFService(self.context)
        self.zmq_listener = ZMQListener(self.context, test_mode=constants.TEST_MODE)
        self.request_manager = RequestManager(self.context)
        # Proof cache + collector
        self.proof_cache: ProofCache | None = None
        self.proof_collector: ProofCollector | None = None

        # Wire up VDF service reference to ZMQ listener
        self.zmq_listener.set_vdf_service(self.vdf_service)

        # Web app and MCP server
        self.app: web.Application | None = None
        self.mcp_http_server: MCPHttpServer | None = None
        
        # Broker worker client (if enabled)
        self.worker_client = None
        self.worker_client_task: asyncio.Task | None = None

    def _start_worker_client_task(self) -> None:
        if self.worker_client is None:
            return
        if self.worker_client_task and not self.worker_client_task.done():
            return
        self.worker_client_task = asyncio.create_task(
            self.worker_client.start(),
            name="broker_worker_client",
        )
        self.worker_client_task.add_done_callback(self._on_worker_client_done)

    def _on_worker_client_done(self, task: asyncio.Task) -> None:
        if self.worker_client is None:
            return
        try:
            exc = task.exception()
        except asyncio.CancelledError:
            exc = None
            if self.worker_client.running:
                logger.warning("[App] Broker worker client task was cancelled")
            else:
                logger.debug("[App] Broker worker client task cancelled during shutdown")
        if exc is not None:
            logger.error(
                "[App] Broker worker client task exited: %s",
                exc,
                exc_info=(type(exc), exc, exc.__traceback__),
            )
        elif self.worker_client.running:
            logger.error("[App] Broker worker client task exited while still marked running")

        if self.worker_client.running:
            logger.warning("[App] Restarting broker worker client task")
            self.worker_client_task = None
            self._start_worker_client_task()

    async def start(self):
        """Start all services"""
        logger.info("Starting mining proxy application...")

        # Determine if we're in broker mode
        is_broker_mode = constants.WORKER_MODE == "broker"

        # Start background services
        self.vdf_service.start()

        # W4: In broker mode, broker replaces ZMQ listener as job source.
        # Don't start zmq_listener to avoid external ZMQ jobs conflicting with broker jobs.
        if not is_broker_mode:
            self.zmq_listener.start()
            logger.info("ZMQ listener started (standalone mode)")
        else:
            logger.info("ZMQ listener NOT started (broker mode - broker replaces ZMQ job source)")

        # Start async services
        await self.request_manager.start()

        # Start proof cache + collector
        # W4: In broker mode, ProofCollector is required for mining result forwarding,
        # even if caching is disabled. Create a dummy cache if needed.
        if constants.PROOF_CACHE_ENABLED or is_broker_mode:
            if constants.PROOF_CACHE_ENABLED:
                self.proof_cache = ProofCache(
                    ttl_seconds=constants.PROOF_CACHE_TTL_SECONDS,
                    max_size_mb=constants.PROOF_CACHE_MAX_SIZE_MB,
                )
            else:
                # Broker mode needs ProofCollector for mining forwarding even without caching
                # Create a minimal cache that won't actually be used for retrieval
                self.proof_cache = ProofCache(ttl_seconds=60, max_size_mb=10)
                logger.info("Created minimal proof cache for broker mining forwarding")

            self.proof_collector = ProofCollector(self.proof_cache, self.context)
            self.proof_collector.start()

        # Create web app
        self.app = await self._create_web_app()

        # Start MCP HTTP server on port 8090 (if enabled)
        if constants.MCP_MODE:
            self.mcp_http_server = MCPHttpServer(self)
            await self.mcp_http_server.start()
        else:
            logger.info("MCP server disabled via constants.MCP_MODE")

        # Start broker worker if configured
        if is_broker_mode:
            from worker_client import BrokerWorkerClient
            # W4: Pass context, zmq_listener, and proof_collector for mining sidecar support
            self.worker_client = BrokerWorkerClient(
                context=self.context,
                zmq_listener=self.zmq_listener,
                proof_collector=self.proof_collector,
                # Slice 9: WS handler routes MODEL_REGISTRY_SYNC into
                # the RequestManager's ModelClient via
                # update_from_payload — same path as the local
                # /api/v1/models fetch.
                request_manager=self.request_manager,
            )
            self._start_worker_client_task()
            logger.info("[App] Started broker worker client")
        else:
            logger.info(f"Broker worker disabled (WORKER_MODE={constants.WORKER_MODE})")

        logger.info("Mining proxy application started successfully")
        return self.app

    async def stop(self):
        """Stop all services gracefully"""
        logger.info("Stopping mining proxy application...")

        # Stop MCP server (if it was started)
        if self.mcp_http_server:
            await self.mcp_http_server.stop()

        # Stop broker worker client (if it was started)
        if self.worker_client:
            await self.worker_client.stop()
        if self.worker_client_task and not self.worker_client_task.done():
            self.worker_client_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.worker_client_task
        self.worker_client_task = None

        # Stop async services
        await self.request_manager.stop()

        # Stop background services
        self.zmq_listener.stop()
        self.vdf_service.stop()

        if self.proof_collector:
            self.proof_collector.stop()

        logger.info("Mining proxy application stopped")

    async def _create_web_app(self) -> web.Application:
        app = web.Application()

        @web.middleware
        async def cors_middleware(request, handler):
            if request.method == "OPTIONS":
                response = web.Response()
            else:
                response = await handler(request)
            response.headers['Access-Control-Allow-Origin'] = '*'
            response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
            response.headers['Access-Control-Allow-Headers'] = '*'
            return response

        app.middlewares.append(cors_middleware)

        # Regular routes
        app.router.add_get('/status', self._handle_status)
        app.router.add_get('/health', self._handle_health)
        app.router.add_get('/v1/mining/active-model', self._handle_get_active_model)
        app.router.add_post('/v1/mining/active-model', self._handle_set_active_model)
        app.router.add_post('/v1/chat/completions', self.request_manager.proxy_request)
        app.router.add_post('/v1/completions', self.request_manager.proxy_request)
        app.router.add_post('/v1/embeddings', self.request_manager.proxy_request)
        # OpenAI Responses API pass-through (async + streaming)
        app.router.add_post('/v1/responses', self.request_manager.proxy_request)
        app.router.add_get('/v1/responses/{response_id}', self.request_manager.proxy_request)
        app.router.add_post('/v1/responses/{response_id}/cancel', self.request_manager.proxy_request)
        app.router.add_get('/v1/models', self.request_manager.proxy_request)

        # Proof retrieval endpoints
        app.router.add_get('/v1/proof/{completion_id}', self._handle_get_proof)
        app.router.add_get('/v1/proof/status/{completion_id}', self._handle_get_proof_status)
        # Debug endpoints
        app.router.add_get('/v1/proof/keys', self._handle_list_proof_keys)
        app.router.add_get('/v1/proof/stats', self._handle_proof_stats)

        app.on_cleanup.append(self._cleanup)
        return app

    async def _handle_status(self, request: web.Request) -> web.Response:
        """Handle status endpoint"""
        status = {
            "context": self.context.get_status(),
            "vdf": self.vdf_service.get_status(),
            "zmq": self.zmq_listener.get_status(),
            "proxy": self.request_manager.get_status(),
        }
        if self.worker_client:
            status["worker"] = self.worker_client.get_status()
        formatted_json = json.dumps(status, indent=2)
        return web.Response(text=formatted_json, content_type='application/json')

    async def _handle_health(self, request: web.Request) -> web.Response:
        """Handle health check endpoint"""
        return web.json_response({"status": "healthy"})

    async def _handle_get_active_model(self, request: web.Request) -> web.Response:
        """Return current runtime model selection used by mining proxy."""
        try:
            return web.json_response(self.request_manager.get_active_model())
        except Exception as e:
            logger.exception("Failed to read active model: %s", e)
            return web.json_response({"error": str(e)}, status=500)

    async def _handle_set_active_model(self, request: web.Request) -> web.Response:
        """Set runtime model selection used by mining proxy.

        Body:
          {"model_name":"...", "model_commit":"..."}
        Special case:
          both empty => enable auto-select mode.
        """
        try:
            payload = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON body"}, status=400)

        model_name = ""
        model_commit = ""
        force_switch = False
        if isinstance(payload, dict):
            model_name = str(payload.get("model_name", "") or "")
            model_commit = str(payload.get("model_commit", "") or "")
            force_switch = bool(payload.get("force_switch", False))
        else:
            return web.json_response({"error": "JSON object expected"}, status=400)

        try:
            state = self.request_manager.set_active_model(model_name, model_commit, force_switch=force_switch)
            return web.json_response({
                "ok": True,
                "active_model": state,
            })
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=400)
        except Exception as e:
            logger.exception("Failed to set active model: %s", e)
            return web.json_response({"error": str(e)}, status=500)

    async def _handle_get_proof(self, request: web.Request) -> web.Response:
        if not self.proof_cache:
            return web.json_response({"error": "proof cache disabled"}, status=404)
        completion_id = request.match_info.get('completion_id')
        # Try exact match first
        item = self.proof_cache.get(completion_id)
        # If not found and completion_id looks like cmpl-...-N, try stripping suffix
        aliased = False
        canonical_id = completion_id
        if not item and completion_id and '-' in completion_id:
            base, suffix = completion_id.rsplit('-', 1)
            if suffix.isdigit():
                maybe = self.proof_cache.get(base)
                if maybe:
                    item = maybe
                    aliased = True
                    canonical_id = base
        if not item:
            return web.json_response({"error": "not found"}, status=404)
        ts, blob, size_bytes, ttl_rem = item
        headers = {
            'Content-Type': 'application/octet-stream',
            'Content-Disposition': f'attachment; filename="proof_{canonical_id}.bin"',
            'X-Proof-Timestamp': str(int(ts)),
            'X-Proof-TTL-Remaining': str(ttl_rem),
            'X-Proof-Canonical-Id': canonical_id,
            'X-Proof-Aliased': '1' if aliased else '0',
        }
        return web.Response(body=blob, headers=headers)

    async def _handle_get_proof_status(self, request: web.Request) -> web.Response:
        if not self.proof_cache:
            return web.json_response({"error": "proof cache disabled"}, status=404)
        completion_id = request.match_info.get('completion_id')
        # Try exact match first
        item = self.proof_cache.get(completion_id)
        aliased = False
        canonical_id = completion_id
        # If not found and completion_id looks like cmpl-...-N, try stripping suffix
        if not item and completion_id and '-' in completion_id:
            base, suffix = completion_id.rsplit('-', 1)
            if suffix.isdigit():
                maybe = self.proof_cache.get(base)
                if maybe:
                    item = maybe
                    aliased = True
                    canonical_id = base
        if not item:
            return web.json_response({
                "completion_id": completion_id,
                "available": False
            })
        ts, blob, size_bytes, ttl_rem = item
        return web.json_response({
            "completion_id": canonical_id,
            "available": True,
            "timestamp": int(ts),
            "size_bytes": size_bytes,
            "ttl_remaining_seconds": ttl_rem,
            "aliased": aliased,
        })

    async def _handle_list_proof_keys(self, request: web.Request) -> web.Response:
        """List current completion_ids present in the cache (for debugging)."""
        if not self.proof_cache:
            return web.json_response({"error": "proof cache disabled"}, status=404)
        # Access internal keys safely
        # This is a debug endpoint; avoid heavy payloads
        keys = []
        try:
            # type: ignore[attr-defined]
            keys = list(self.proof_cache._store.keys())  # noqa: SLF001
        except Exception:
            pass
        return web.json_response({
            "count": len(keys),
            "keys": keys[:500],  # cap to avoid huge responses
        })

    async def _handle_proof_stats(self, request: web.Request) -> web.Response:
        if not self.proof_cache:
            return web.json_response({"error": "proof cache disabled"}, status=404)
        return web.json_response(self.proof_cache.stats())

    async def _cleanup(self, app: web.Application):
        """Cleanup handler for web app shutdown"""
        await self.stop()


def handle_signals():
    """Set up signal handlers for graceful shutdown"""
    def signal_handler(sig, frame):
        logger.info(f"Received signal {sig}, shutting down...")
        sys.exit(0)
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)


async def create_app():
    """Factory function to create the application"""
    app_instance = MiningProxyApp()
    return await app_instance.start()


async def run_broker_only():
    """Run in broker-only mode without HTTP server (outbound WSS only)"""
    app_instance = MiningProxyApp()
    await app_instance.start()
    logger.info("Broker-only mode active. Press Ctrl+C to stop.")
    try:
        # Keep running until interrupted
        while True:
            await asyncio.sleep(1)
    except asyncio.CancelledError:
        pass
    finally:
        await app_instance.stop()


def main():
    """Main entry point"""
    logger.info("=" * 60)
    logger.info("Mining Proxy Starting")

    if constants.DISABLE_HTTP_SERVER:
        logger.info("HTTP: DISABLED (broker-only mode)")
    else:
        logger.info(f"HTTP: {constants.HTTP_HOST}:{constants.HTTP_PORT}")

    if constants.MCP_MODE:
        logger.info(f"MCP:  127.0.0.1:8090 (path: /mcp)")
    else:
        logger.info("MCP:  DISABLED")

    logger.info("Priority mode: %s", "ENABLED" if constants.PRIORITY_MODE else "DISABLED")

    logger.info(f"Target: {constants.TARGET_URL}")
    logger.info(f"ZMQ Port: {constants.ZMQ_PULL_PORT}")
    logger.info(f"Min Active Requests: {constants.MIN_ACTIVE_REQUESTS}")
    logger.info("=" * 60)

    handle_signals()

    if constants.DISABLE_HTTP_SERVER:
        # Broker-only mode: just run the worker client without HTTP server
        asyncio.run(run_broker_only())
    else:
        web.run_app(
            create_app(),
            host=constants.HTTP_HOST,
            port=constants.HTTP_PORT,
            print=lambda *args: None
        )


if __name__ == "__main__":
    main()
