"""
Configuration constants for the mining proxy service
"""
import os
import json
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any
from utils.uint256_arithmetics import set_compact, get_compact, adjust_nbits_by_multiplier

logger = logging.getLogger(__name__)

# Service Configuration
HTTP_HOST = os.getenv("HTTP_HOST", "0.0.0.0")
HTTP_PORT = int(os.getenv("HTTP_PORT", "8080"))
# Disable HTTP server entirely (broker mode - outbound only)
DISABLE_HTTP_SERVER = os.getenv("DISABLE_HTTP_SERVER", "false").lower() in ("true", "1", "yes")
# Note: TARGET_URL should be a base origin (no path). Paths like /v1/... are appended in code.
TARGET_URL = os.getenv("TARGET_URL", "http://localhost:8000")
API_KEY = os.getenv("API_KEY", "dev-secret")

# Optional model→backend routing for multi-backend workers (one proxy
# fronting several vLLM instances on the same GPU). Comma list of
# "<model_name>[@<commit>]=<base_url>" entries, e.g.
#   MODEL_ROUTES=Qwen/Qwen3-8B@9c92...=http://127.0.0.1:8001,Qwen/Qwen3.5-27B-FP8@97f5...=http://127.0.0.1:8000
# Models without a route fall back to TARGET_URL. The optional commit is
# informational (audit identity); PoW pinning still uses MODEL_NAME/MODEL_COMMIT.
MODEL_ROUTES = {}
MODEL_ROUTE_COMMITS = {}
for _entry in os.getenv("MODEL_ROUTES", "").split(","):
    _entry = _entry.strip()
    if not _entry or "=" not in _entry:
        continue
    _model, _route_url = _entry.split("=", 1)
    _model = _model.strip()
    _route_commit = ""
    if "@" in _model:
        _model, _route_commit = _model.split("@", 1)
    _model = _model.strip()
    if not _model:
        continue
    MODEL_ROUTES[_model] = _route_url.strip()
    if _route_commit.strip():
        MODEL_ROUTE_COMMITS[_model] = _route_commit.strip()

# Standalone mode - skip remote model sync, use local model config
STANDALONE_MODE = os.getenv("STANDALONE_MODE", "false").lower() in ("true", "1", "yes")
# LOCAL_MODEL_NAME takes precedence, falls back to MODEL_NAME for simple-worker compat
LOCAL_MODEL_NAME = (os.getenv("LOCAL_MODEL_NAME") or os.getenv("MODEL_NAME") or "").strip()
MODEL_HASH = (os.getenv("MODEL_HASH") or os.getenv("MODEL_COMMIT") or "").strip()

# ZMQ Configuration
ZMQ_PULL_PORT = int(os.getenv("ZMQ_PULL_PORT", "6000"))
ZMQ_RECV_TIMEOUT_MS = int(os.getenv("ZMQ_RECV_TIMEOUT_MS", "6000000"))
TEST_MODE = bool(os.getenv("TEST_MODE", True))  
ZMQ_RETRY_ATTEMPTS   = int(os.getenv("ZMQ_RETRY_ATTEMPTS",   "10"))
ZMQ_RETRY_BACKOFF    = float(os.getenv("ZMQ_RETRY_BACKOFF",    "1.0"))

# VDF Configuration
VDF_DISCRIMINANT_SIZE = int(os.getenv("VDF_DISCRIMINANT_SIZE", "1024"))
VDF_CHECKPOINT_SIZE = int(os.getenv("VDF_CHECKPOINT_SIZE", "32768"))
VDF_UPDATE_INTERVAL = float(os.getenv("VDF_UPDATE_INTERVAL", "0.1"))

# Request Manager Configuration
MIN_ACTIVE_REQUESTS = int(os.getenv("MIN_ACTIVE_REQUESTS", "32"))
DUMMY_REQUEST_TIMEOUT = int(os.getenv("DUMMY_REQUEST_TIMEOUT", "300"))
MONITOR_INTERVAL = float(os.getenv("MONITOR_INTERVAL", "1.0"))
DUMMY_RETRY_ATTEMPTS = int(os.getenv("DUMMY_RETRY_ATTEMPTS", "10"))
DUMMY_RETRY_BACKOFF  = float(os.getenv("DUMMY_RETRY_BACKOFF",  "1.0"))
# Stop generating dummies if no block received from core-node for this long
MINING_STALE_THRESHOLD_SECONDS = int(os.getenv("MINING_STALE_THRESHOLD_SECONDS", "60"))

# Qwen3 emits a deterministic <think> prelude by default on the /v1/responses
# (ChatML) dummy-mining path. Those low-entropy thinking tokens push the proof's
# average CDF-upper over the consensus entropy gate (>=0.925 -> quick_verify_failed),
# rejecting ~1/3 of otherwise-valid block solutions. The verifier replays the
# proof's stored prompt_tokens (it never re-renders a chat template), so appending
# Qwen3's `/no_think` soft-switch to the dummy prompt is purely a miner-side
# rendering choice and is fully consensus-safe. Measured: CDF-upper 0.928 (thinking)
# -> 0.838 (/no_think), clearing the gate. Default on for mining; toggle without a
# rebuild via env.
MINING_DISABLE_THINKING = os.getenv("MINING_DISABLE_THINKING", "true").lower() in ("true", "1", "yes")
MINING_NO_THINK_DIRECTIVE = os.getenv("MINING_NO_THINK_DIRECTIVE", "/no_think")

# Upper bound on the chiavdf StreamingProver's iteration budget per
# challenge. The VDF MUST keep advancing for the FULL block interval —
# a prover that stops early stalls mining on long blocks — so this must
# stay comfortably above the worst-case interval (default = historical
# 3e9, ≈10h at ~80k ticks/s). It exists as a knob for test/regtest
# profiles only, NOT as a production memory mitigation: the checkpoint
# RSS growth between block resets is a chiavdf retention issue
# (prover_slow.h offers every iteration to every K/L store) and needs a
# native fix, not a shorter budget.
VDF_MAX_ITERS = int(os.getenv("VDF_MAX_ITERS", "3000000000"))

# Model API Configuration
MODEL_API_URL = os.getenv("MODEL_API_URL", "http://localhost:8080")
MODEL_API_KEY = os.getenv("MODEL_API_KEY", "")
MODEL_REQUIRE_AUTH = os.getenv("MODEL_REQUIRE_AUTH", "false").lower() in ("true", "1", "yes")
MODEL_RETRY_ATTEMPTS = int(os.getenv("MODEL_RETRY_ATTEMPTS", "3"))
MODEL_RETRY_BACKOFF = float(os.getenv("MODEL_RETRY_BACKOFF", "1.0"))
MODEL_POLL_INTERVAL = float(os.getenv("MODEL_POLL_INTERVAL", "600"))

# Specific Generation Mode
MCP_MODE = os.getenv("MCP_MODE", "False") == "True"
LLAMA_CPP = os.getenv("LLAMA_CPP", "False") == "True"
# Optional backend control API used for real runtime model switching.
BACKEND_MODEL_SWITCH_ENABLED = os.getenv("BACKEND_MODEL_SWITCH_ENABLED", "false").lower() in ("true", "1", "yes")
BACKEND_CONTROL_URL = os.getenv("BACKEND_CONTROL_URL", "").strip()
BACKEND_CONTROL_API_KEY = os.getenv("BACKEND_CONTROL_API_KEY", API_KEY).strip()
BACKEND_SWITCH_TIMEOUT_SEC = int(os.getenv("BACKEND_SWITCH_TIMEOUT_SEC", "90"))
MODEL_SWITCH_FORCE_SUPPRESS_SEC = float(os.getenv("MODEL_SWITCH_FORCE_SUPPRESS_SEC", "3"))
# vLLM >=0.16 renamed extra_sampling_params -> vllm_xargs.
# llama.cpp still uses extra_sampling_params unless explicitly overridden.
_USE_VLLM_XARGS_RAW = os.getenv("USE_VLLM_XARGS")
if _USE_VLLM_XARGS_RAW is None:
    USE_VLLM_XARGS = not LLAMA_CPP
else:
    USE_VLLM_XARGS = _USE_VLLM_XARGS_RAW.lower() in ("true", "1", "yes")
GENESIS_GENERATOR = os.getenv("GENESIS_GENERATOR", "False") == "True"
PRIORITY_MODE = os.getenv("PRIORITY_MODE", "true").lower() in ("true", "1", "yes")

# Mining Configuration
DEFAULT_BLOCK_HASH = "0" * 64
DEFAULT_DIFFICULTY = 1_000_000
_BASE_NBITS_RAW = os.getenv("BASE_NBITS", "536990216")
try:
    BASE_NBITS = int(_BASE_NBITS_RAW, 0)   # auto-detect hex (0x…) or decimal
except ValueError:
    raise RuntimeError(f"Invalid BASE_NBITS: {_BASE_NBITS_RAW!r}")
DEFAULT_VERSION = 3

# Logging configuration
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
LOG_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"

# BATCHING REQUEST SIZE
BATCH_SIZE = 32

# Proof cache and collector
PROOF_CACHE_ENABLED = os.getenv("PROOF_CACHE_ENABLED", "true").lower() in ("1", "true", "yes")
PROOF_CACHE_TTL_SECONDS = int(os.getenv("PROOF_CACHE_TTL_SECONDS", "900"))
PROOF_CACHE_MAX_SIZE_MB = int(os.getenv("PROOF_CACHE_MAX_SIZE_MB", "500"))
PROOF_COLLECTOR_PORT = int(os.getenv("PROOF_COLLECTOR_PORT", "7002"))

# Slice 11 — chain ``ModelDifficultyNormalizer`` consensus constant.
# Used by the worker sampler to compute the adjusted share target:
#   adjusted_share_target = floor(base_share_target * N / model.difficulty)
# MUST equal bcore's consensus param for the active chain
# (mainnet TensorMain = 1_000_000, see
# bcore/src/kernel/chainparams.cpp:299). Worker and broker share
# this value — a mismatch silently retargets every share by a
# constant factor.
#
# 0 = "not configured" → worker mints MineResult without the
# matching MineShare on each block hit (slice-11 share emission
# disabled; block-only behaviour preserved as a rollout safety
# net). Production MUST set this to a positive value matching
# the active chain profile.
MODEL_DIFFICULTY_NORMALIZER = int(os.getenv("MODEL_DIFFICULTY_NORMALIZER", "0"))

# Broker Worker Configuration
WORKER_MODE = os.getenv("WORKER_MODE", "standalone")  # standalone | broker
BROKER_WS_URL = os.getenv("BROKER_WS_URL", "ws://localhost:8003/v1/ws")
PROVIDER_JWT_TOKEN = os.getenv("PROVIDER_JWT_TOKEN", "")
X_WORKER_TOKEN = os.getenv("X_WORKER_TOKEN", "")  # optional shared-secret dev mode if broker allows
WORKER_CAPACITY = int(os.getenv("WORKER_CAPACITY", "4"))
COMPUTE_TYPE = os.getenv("COMPUTE_TYPE", "nvidia-8.6")
GPU_MODEL = os.getenv("GPU_MODEL", "A100-80GB")
GPU_MEMORY_GB = int(os.getenv("GPU_MEMORY_GB", "80"))
WORKER_REGION = os.getenv("WORKER_REGION", "us-west-2")
MAX_CONTEXT_WINDOW = int(os.getenv("MAX_CONTEXT_WINDOW", "128000"))
# True iff the operator explicitly set MAX_CONTEXT_WINDOW in the env. Used by
# worker_client._send_hello to refuse the silent 128k fallback: if backend
# introspection (/v1/models, /props) returns no context AND the operator
# didn't pin the env, advertising 128000 would let the broker route oversized
# requests that vllm/llama would 400 mid-stream. Set MAX_CONTEXT_WINDOW=<n>
# in your worker env to bypass introspection (e.g. on llama-cpp without /props).
MAX_CONTEXT_WINDOW_EXPLICIT = "MAX_CONTEXT_WINDOW" in os.environ
# Maximum output tokens the model can generate per request (reported to broker for scheduling)
MAX_OUTPUT_TOKENS = int(os.getenv("MAX_OUTPUT_TOKENS", "8192"))
CHALLENGE_SECRET = os.getenv("CHALLENGE_SECRET", "")  # optional; if set by broker, worker must respond

# W2 Confidential Mode Configuration
# Supported modes: "plaintext" (default), "confidential" (encrypted payloads)
WORKER_SUPPORTED_MODES = os.getenv("WORKER_SUPPORTED_MODES", "plaintext,confidential").split(",")
# Auth service URL for key registration and CEK fetch
AUTH_SERVICE_URL = os.getenv("AUTH_SERVICE_URL", "http://localhost:8001")
# Agent ID (extracted from API key, but can be overridden for testing)
AGENT_ID = os.getenv("AGENT_ID", "")
# X25519 key pair paths for confidential mode (PEM or raw base64)
AGENT_PRIVATE_KEY_PATH = os.getenv("AGENT_PRIVATE_KEY_PATH", "")
AGENT_PUBLIC_KEY_PATH = os.getenv("AGENT_PUBLIC_KEY_PATH", "")
# Inline base64 keys (alternative to file paths)
AGENT_PRIVATE_KEY_B64 = os.getenv("AGENT_PRIVATE_KEY_B64", "")
AGENT_PUBLIC_KEY_B64 = os.getenv("AGENT_PUBLIC_KEY_B64", "")
# Enable/disable confidential mode (master switch)
CONFIDENTIAL_MODE_ENABLED = os.getenv("CONFIDENTIAL_MODE_ENABLED", "false").lower() in ("true", "1", "yes")
# Retry window for CEK package availability races (room creation/invite vs first job)
CONFIDENTIAL_CEK_FETCH_RETRIES = int(os.getenv("CONFIDENTIAL_CEK_FETCH_RETRIES", "10"))
CONFIDENTIAL_CEK_FETCH_RETRY_DELAY_SEC = float(
    os.getenv("CONFIDENTIAL_CEK_FETCH_RETRY_DELAY_SEC", "1.0")
)
# TEE attestation service URL (runs on the same VM, self-signed cert)
ATTESTATION_SERVICE_URL = os.getenv("ATTESTATION_SERVICE_URL", "https://localhost:9443")

# W3 Tool Capability Configuration
# Tools that this worker can execute (JSON array of tool definitions)
# Format: [{"tool_id": "...", "schema_ref": "...", "encryption": "none|aes-256-gcm"}]
WORKER_TOOLS_JSON = os.getenv("WORKER_TOOLS_JSON", "[]")
try:
    WORKER_TOOLS = json.loads(WORKER_TOOLS_JSON)
except json.JSONDecodeError:
    WORKER_TOOLS = []

# Optional local tool execution settings (desktop worker UX)
RAG_CONTEXT_PATH = os.getenv("RAG_CONTEXT_PATH", "")
MCP_TOOL_ENDPOINT = os.getenv("MCP_TOOL_ENDPOINT", "")
# Optional explicit MCP discovery endpoint. If empty, worker derives candidates
# from MCP_TOOL_ENDPOINT (e.g. /tools, /tools/list, JSON-RPC tools/list on endpoint).
MCP_TOOL_DISCOVERY_URL = os.getenv("MCP_TOOL_DISCOVERY_URL", "")

# W4 Mining Sidecar Configuration
# Enable mining capability advertisement (replaces ZMQ node source when using broker)
MINING_ENABLED = os.getenv("MINING_ENABLED", "true").lower() in ("true", "1", "yes")


def is_broker_inference_only() -> bool:
    """True when the worker is in broker mode with mining effectively disabled.

    In this mode the miner-proxy must not depend on a colocated Core Node:
    no ModelClient, no model registry lookup, no VDF requirement, no PoW
    injection on ordinary inference requests. Reads module-level constants
    each call so tests can patch ``WORKER_MODE`` / ``MINING_ENABLED``.
    """
    return WORKER_MODE == "broker" and not MINING_ENABLED



# Time to wait for VDF proof before sending MINE_RESULT (seconds)
MINING_JOB_TIMEOUT_SEC = int(os.getenv("MINING_JOB_TIMEOUT_SEC", "30"))
# After a solution is found, suspend further mining for this many seconds.
MINING_SOLUTION_COOLDOWN_SEC = int(os.getenv("MINING_SOLUTION_COOLDOWN_SEC", "0"))
# Minimum VDF iterations before considering a proof "ready"
MINING_MIN_ITERATIONS = int(os.getenv("MINING_MIN_ITERATIONS", "100000"))
# Poll interval for checking VDF progress on mining jobs (seconds)
MINING_POLL_INTERVAL = float(os.getenv("MINING_POLL_INTERVAL", "0.5"))

# Phase 3 v2 capability advertisement (HELLO).
# CSV of bcore-canonical chain names this worker is willing to mine on.
# Empty list means "do not dispatch mining work to this worker" — the
# scheduler must respect that, even if MINING_ENABLED=true.
MINING_NETWORKS = [
    s.strip() for s in os.getenv("MINING_NETWORKS", "").split(",") if s.strip()
]
# Maximum concurrent broker-issued mining jobs the worker accepts.
# Mining state is single-context (LockFreeContext is process-wide), so
# the safe default is 1; raise only when the worker can prove it can
# carry independent mining contexts (Phase 7 hardening territory).
MINING_MAX_PARALLEL = max(1, int(os.getenv("MINING_MAX_PARALLEL", "1")))

@dataclass
class ModelConfig:
    """Configuration for a specific model"""
    model_hash: str
    model_name: str
    model_commit: str
    difficulty: int
    ipfs_cid: Optional[str] = None
    target_adj: Optional[str] = None
    txid: Optional[str] = None
    block_hash: Optional[str] = None
    block_height: Optional[int] = None
    
    @property
    def target_adjustment(self) -> str:
        """Get target adjustment, defaulting if not set"""
        return self.target_adj or "7fffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"


# Fallback model configurations (used when API is unavailable)
FALLBACK_MODEL_CONFIGS: Dict[str, ModelConfig] = {
    "Qwen/Qwen3-8B": ModelConfig(
        model_hash="0" * 64,
        model_name="Qwen/Qwen3-8B",
        model_commit="9c925d64d72725edaf899c6cb9c377fd0709d9c5",
        difficulty=DEFAULT_DIFFICULTY,
        ipfs_cid="bafybeihosbewxanqruo7br4va7vul3j3ntnudynczfckohya7yo4mqa3oy",
    )
}

# Default model if not found
DEFAULT_MODEL_CONFIG = FALLBACK_MODEL_CONFIGS["Qwen/Qwen3-8B"]


def get_model_config(model_name: str, model_client=None) -> ModelConfig:
    """
    Get model configuration by name.
    First tries the model client if provided, then falls back to static config.

    Auto-select callers (no MODEL_COMMIT pin) pass through here. When a
    name maps to multiple registered commits on chain (e.g. Qwen/Qwen3-0.6B
    with two simultaneous registrations), pick deterministically — the
    record with the highest block_height — so every miner converges on
    the same commit without needing operator intervention.
    """
    if model_client:
        records = model_client.get_models_by_name(model_name)
        if records:
            if len(records) > 1:
                records = sorted(
                    records,
                    key=lambda r: (r.get("block_height") or 0, r.get("model_commit") or ""),
                    reverse=True,
                )
                logger.info(
                    "[get_model_config] %d commits for %s; auto-picking %s (block_height=%s)",
                    len(records),
                    model_name,
                    (records[0].get("model_commit") or "")[:12],
                    records[0].get("block_height"),
                )
            model_data = records[0]
            return ModelConfig(
                model_hash=model_data.get("model_hash", ""),
                model_name=model_data.get("model_name", model_name),
                model_commit=model_data.get("model_commit", ""),
                difficulty=model_data.get("difficulty", DEFAULT_DIFFICULTY),
                ipfs_cid=model_data.get("cid"),
                target_adj=None,
                txid=model_data.get("txid"),
                block_hash=model_data.get("block_hash"),
                block_height=model_data.get("block_height")
            )
        return FALLBACK_MODEL_CONFIGS.get(model_name, DEFAULT_MODEL_CONFIG)
    return FALLBACK_MODEL_CONFIGS.get(model_name, DEFAULT_MODEL_CONFIG)


def get_model_config_by_hash(model_hash: str, model_client=None) -> Optional[ModelConfig]:
    """
    Get model configuration by hash.
    First tries the model client if provided, then checks fallback configs.
    """
    # Try to get from model client if available
    if model_client:
        model_data = model_client.get_model_by_hash(model_hash)
        if model_data:
            return ModelConfig(
                model_hash=model_data.get("model_hash", model_hash),
                model_name=model_data.get("model_name", ""),
                model_commit=model_data.get("model_commit", ""),
                difficulty=model_data.get("difficulty", DEFAULT_DIFFICULTY),
                ipfs_cid=model_data.get("cid"),
                target_adj=None,
                txid=model_data.get("txid"),
                block_hash=model_data.get("block_hash"),
                block_height=model_data.get("block_height")
            )
    
    # Check fallback configs
    for config in FALLBACK_MODEL_CONFIGS.values():
        if config.model_hash == model_hash:
            return config
    
    return None


@dataclass
class Settings:
    """
    Application settings loaded from environment
    """
    # Service settings
    http_host: str = HTTP_HOST
    http_port: int = HTTP_PORT
    target_url: str = TARGET_URL
    
    # Model API settings
    model_api_url: str = MODEL_API_URL
    model_api_key: str = MODEL_API_KEY
    model_require_auth: bool = MODEL_REQUIRE_AUTH
    model_retry_attempts: int = MODEL_RETRY_ATTEMPTS
    model_retry_backoff: float = MODEL_RETRY_BACKOFF
    model_poll_interval: float = MODEL_POLL_INTERVAL
    
    # Other settings
    log_level: str = LOG_LEVEL
    log_format: str = LOG_FORMAT
    
    @classmethod
    def load(cls) -> "Settings":
        """Load settings from environment"""
        return cls()
