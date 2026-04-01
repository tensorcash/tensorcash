#!/usr/bin/env python3

import os
import json
import logging
import secrets
import hashlib
import time
from typing import Optional, Dict, Any, List
from datetime import datetime, timedelta
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Depends, Header, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from pydantic import BaseModel
import httpx
from collections import defaultdict
import ssl

# Configure logging based on environment
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Security configuration
RPC_USER = os.getenv("RPC_USER")
RPC_PASS = os.getenv("RPC_PASS")
RPC_HOST = os.getenv("RPC_HOST", "localhost")
RPC_PORT = os.getenv("RPC_PORT", "18443")
API_PORT = int(os.getenv("API_PORT", "8050"))
MINER_HOST = os.getenv("MINER_HOST", "miner-proxy")
COOKIE_FILE = os.getenv("COOKIE_FILE", "/data/caishtest/.cookie")


# Enhanced security settings (all optional)
API_KEYS = os.getenv("API_KEY", "").split(",") if os.getenv("API_KEY") else []
RATE_LIMIT_PER_MINUTE = int(os.getenv("RATE_LIMIT", "60"))
ALLOWED_ORIGINS = os.getenv("ALLOWED_ORIGINS", "").split(",") if os.getenv("ALLOWED_ORIGINS") else []
ENABLE_TLS = os.getenv("ENABLE_TLS", "false").lower() == "true"
TLS_CERT = os.getenv("TLS_CERT", "/certs/cert.pem")
TLS_KEY = os.getenv("TLS_KEY", "/certs/key.pem")

# Test mode flag
TEST_MODE = os.getenv("TEST_MODE", "false").lower() == "true"
REQUIRE_AUTH = os.getenv("REQUIRE_AUTH", "true").lower() == "true"

# If auth is required but no keys provided, generate one
if REQUIRE_AUTH and not API_KEYS:
    default_key = secrets.token_urlsafe(32)
    API_KEYS = [default_key]
    logger.warning(f"Auth required but no API_KEYS provided. Generated temporary key: {default_key}")

# Validate RPC settings only if not in test mode
# Smart RPC authentication validation - checks cookie file first, then fallback credentials
if not TEST_MODE:
    rpc_user = os.getenv("RPC_USER")
    rpc_pass = os.getenv("RPC_PASS")

    # Wait for cookie file to appear (bitcoin-qt/bitcoind takes time to start)
    import time
    max_wait = 60  # seconds
    wait_interval = 2
    waited = 0
    while not os.path.exists(COOKIE_FILE) and waited < max_wait:
        if rpc_user and rpc_pass:
            logger.info("Cookie file not yet available, but RPC credentials provided - continuing")
            break
        logger.info(f"Waiting for cookie file {COOKIE_FILE}... ({waited}/{max_wait}s)")
        time.sleep(wait_interval)
        waited += wait_interval

    cookie_exists = os.path.exists(COOKIE_FILE)

    if cookie_exists:
        try:
            with open(COOKIE_FILE, 'r') as f:
                cookie_content = f.read().strip()
            if ':' not in cookie_content or not cookie_content:
                logger.warning(f"Invalid cookie file format at {COOKIE_FILE}")
                if not (rpc_user and rpc_pass):
                    raise ValueError("Invalid cookie file and no RPC credentials provided")
            else:
                logger.info(f"✓ RPC cookie file found at {COOKIE_FILE}")
        except (PermissionError, IOError) as e:
            logger.warning(f"Cannot read cookie file {COOKIE_FILE}: {e}")
            if not (rpc_user and rpc_pass):
                raise ValueError(f"Cannot read cookie file and no RPC credentials provided: {e}")
    elif rpc_user and rpc_pass:
        logger.warning("Cookie file not found, using RPC_USER/RPC_PASS (consider migrating to cookie auth)")
    else:
        logger.error(f"RPC authentication not configured:")
        logger.error(f"  - Cookie file not found: {COOKIE_FILE}")
        logger.error(f"  - RPC_USER/RPC_PASS not set")
        logger.error(f"  - Enable TEST_MODE for development")
        raise ValueError("RPC authentication not configured - no cookie file or credentials")

# Rate limiting storage
rate_limit_storage = defaultdict(list)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan manager for startup and shutdown events."""
    # Startup
    logger.info("Bitcoin Core Model API starting...")
    logger.info(f"Configuration: TEST_MODE={TEST_MODE}, REQUIRE_AUTH={REQUIRE_AUTH}, "
                f"ENABLE_TLS={ENABLE_TLS}, API_PORT={API_PORT}")
    
    if TEST_MODE:
        logger.warning("Running in TEST MODE - using mock responses")
    
    if not REQUIRE_AUTH:
        logger.warning("Authentication is DISABLED - API is open to all requests")
    elif API_KEYS:
        logger.info(f"Authentication enabled with {len(API_KEYS)} API key(s)")
    
    if ENABLE_TLS:
        if not os.path.exists(TLS_CERT) or not os.path.exists(TLS_KEY):
            logger.error(f"TLS enabled but certificate files not found! "
                        f"Expected cert: {TLS_CERT}, key: {TLS_KEY}")
            raise FileNotFoundError("TLS certificate files not found")
        logger.info(f"TLS enabled using {TLS_CERT}")
    else:
        logger.warning("TLS is DISABLED - using plain HTTP")
    
    yield
    
    # Shutdown
    logger.info("Bitcoin Core Model API shutting down...")


# FastAPI app with security middleware
app = FastAPI(
    title="Bitcoin Core Model API",
    version="1.0.0",
    docs_url="/docs" if TEST_MODE else None,  # Enable docs in test mode
    redoc_url="/redoc" if TEST_MODE else None,
    lifespan=lifespan
)

# # Add trusted host middleware to prevent host header injection
# app.add_middleware(
#     TrustedHostMiddleware,
#     allowed_hosts=[
#         "localhost",
#         "127.0.0.1",
#         RPC_HOST,                   # e.g. "core-node"
#         MINER_HOST,
#         f"{RPC_HOST}:{API_PORT}",   # include port in Host header
#     ])

# # Add CORS middleware if origins specified
# if ALLOWED_ORIGINS:
#     app.add_middleware(
#         CORSMiddleware,
#         allow_origins=ALLOWED_ORIGINS,
#         allow_credentials=True,
#         allow_methods=["GET"],
#         allow_headers=["Authorization"],
#     )

# Security - auto_error=False to handle missing credentials ourselves (consistent across FastAPI versions)
security = HTTPBearer(auto_error=False)

# Response models
class ModelInfo(BaseModel):
    model_hash: str
    model_name: str
    model_commit: str
    difficulty: int
    status: Optional[int] = None
    cid: Optional[str] = None
    extra: Optional[str] = None
    txid: Optional[str] = None  # legacy fixtures
    block_hash: Optional[str] = None  # legacy fixtures
    block_height: Optional[int] = None  # legacy fixtures
    deposit_txid: Optional[str] = None
    deposit_vout: Optional[int] = None
    deposit_amount: Optional[int] = None
    owner_key_hash: Optional[str] = None
    deposit_block_hash: Optional[str] = None
    deposit_block_height: Optional[int] = None
    commit_txid: Optional[str] = None
    commit_block_hash: Optional[str] = None
    commit_block_height: Optional[int] = None
    verification_code: Optional[int] = None
    verification_details: Optional[str] = None


def get_test_data():
    """Load test data from file or return default test data."""
    test_data_file = os.getenv("TEST_DATA_FILE", "tests/test_data.json")
    

    # Try to load from file if it exists
    if os.path.exists(test_data_file):
        try:
            with open(test_data_file, 'r') as f:
                loaded_data = json.load(f)
                logger.info(f"Loaded test data from {test_data_file}")
                return loaded_data
        except Exception as e:
            logger.warning(f"Failed to load test data from {test_data_file}: {e}")
    

def hash_api_key(key: str) -> str:
    """Hash API key for secure comparison."""
    return hashlib.sha256(key.encode()).hexdigest()


def check_rate_limit(client_id: str) -> bool:
    """Check if client has exceeded rate limit."""
    now = time.time()
    minute_ago = now - 60
    
    # Clean old entries
    rate_limit_storage[client_id] = [
        timestamp for timestamp in rate_limit_storage[client_id]
        if timestamp > minute_ago
    ]
    
    # Check limit
    if len(rate_limit_storage[client_id]) >= RATE_LIMIT_PER_MINUTE:
        return False
    
    # Add current request
    rate_limit_storage[client_id].append(now)
    return True


async def verify_api_key(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = None
) -> Optional[str]:
    """Verify API key and apply rate limiting if auth is required."""
    # If auth is not required, allow access
    if not REQUIRE_AUTH:
        return "anonymous"
    
    # If auth is required, check for credentials
    if not credentials:
        credentials = await security(request)

    # Handle missing credentials (auto_error=False means we get None instead of 401)
    if not credentials:
        raise HTTPException(status_code=403, detail="Missing API key")

    provided_key = credentials.credentials
    
    # Check if key is valid
    if provided_key not in API_KEYS:
        # Log failed attempt without exposing the key
        logger.warning(f"Invalid API key attempt from {request.client.host}")
        raise HTTPException(status_code=403, detail="Invalid API key")
    
    # Check rate limit
    client_id = hash_api_key(provided_key)
    if not check_rate_limit(client_id):
        raise HTTPException(
            status_code=429,
            detail="Rate limit exceeded. Please try again later."
        )
    
    return client_id


# Create dependency that conditionally requires auth
async def get_client_id(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security) if REQUIRE_AUTH else None
) -> str:
    """Get client ID with conditional authentication."""
    return await verify_api_key(request, credentials)

async def call_rpc(method: str, params: List[Any] = None) -> Any:
    """Make RPC call to Bitcoin Core using cookie authentication or return test data."""
    # If in test mode, return test data
    if TEST_MODE:
        test_data = get_test_data()
        
        if method == "getmodelslist":
            simple = params[0] if params else True
            return test_data["getmodelslist_simple"] if simple else test_data["getmodelslist_extended"]
        elif method == "getmodelinfo":
            model_hash = params[0] if params else ""
            return test_data["getmodelinfo"].get(model_hash, f"Model {model_hash} not found in test data")
        elif method == "getblockchaininfo":
            return test_data["getblockchaininfo"]
        else:
            return {"error": f"Method {method} not implemented in test mode"}
    
    # Validate method is allowed
    allowed_methods = ["getmodelslist", "getmodelinfo", "getblockchaininfo"]
    if method not in allowed_methods:
        raise HTTPException(status_code=403, detail="Method not allowed")
    
    url = f"http://{RPC_HOST}:{RPC_PORT}"
    
    payload = {
        "jsonrpc": "2.0",
        "id": secrets.randbits(32),
        "method": method,
        "params": params or []
    }
    
    # Read cookie file for authentication
    cookie_file = COOKIE_FILE 
    try:
        with open(cookie_file, 'r') as f:
            cookie_content = f.read().strip()
            username, password = cookie_content.split(':', 1)
    except FileNotFoundError:
        logger.error(f"Cookie file not found at {cookie_file}")
        raise HTTPException(status_code=503, detail="Authentication file not available")
    except ValueError:
        logger.error(f"Invalid cookie file format at {cookie_file}")
        raise HTTPException(status_code=503, detail="Authentication configuration error")
    
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            response = await client.post(
                url,
                json=payload,
                auth=(username, password)  # Use cookie-based auth
            )
            response.raise_for_status()
            
            result = response.json()
            if "error" in result and result["error"]:
                logger.error(f"RPC error: {result['error']}")
                raise HTTPException(
                    status_code=400,
                    detail="Request failed"
                )
            
            return result.get("result")
            
        except httpx.TimeoutException:
            raise HTTPException(
                status_code=504,
                detail="Request timeout"
            )
        except httpx.HTTPError:
            raise HTTPException(
                status_code=503,
                detail="Service temporarily unavailable"
            )
        except Exception as e:
            logger.error(f"Unexpected error: {type(e).__name__}")
            raise HTTPException(
                status_code=500,
                detail="Internal server error"
            )


@app.get("/")
async def root():
    """Root endpoint - provides API information."""
    return {
        "service": "Bitcoin Core Model API",
        "version": "1.0.0",
        "test_mode": TEST_MODE,
        "auth_required": REQUIRE_AUTH,
        "endpoints": [
            "/health",
            "/api/v1/models",
            "/api/v1/models/{model_hash}"
        ]
    }


@app.get("/health")
async def health_check():
    """Health check endpoint - no auth required."""
    try:
        result = await call_rpc("getblockchaininfo")
        return {
            "status": "healthy",
            "test_mode": TEST_MODE,
            "auth_required": REQUIRE_AUTH,
            "blockchain_info": result if TEST_MODE else {"blocks": result.get("blocks", 0)}
        }
    except Exception as e:
        logger.error(f"Health check failed: {e}")
        return {
            "status": "unhealthy",
            "test_mode": TEST_MODE,
            "auth_required": REQUIRE_AUTH,
            "error": str(e)
        }


@app.get("/api/v1/models", response_model=List[ModelInfo])
async def get_models_list(
    extended: bool = False,
    client_id: str = Depends(get_client_id)
):
    """Get list of models with rate limiting."""
    logger.debug(f"Client {client_id} requested models list (extended={extended})")
    result = await call_rpc("getmodelslist", [not extended])
    return result


@app.get("/api/v1/blockchaininfo")
async def get_blockchain_info(
    client_id: str = Depends(get_client_id),
):
    """Pass-through to the node's `getblockchaininfo` RPC.

    Used by the verification orchestrator to compute distance-to-verdict for
    PendingVerification model records and trigger anticipatory smell-stat
    pre-bake before a model flips to Registered.
    """
    logger.debug(f"Client {client_id} requested blockchaininfo")
    return await call_rpc("getblockchaininfo", [])


@app.get("/api/v1/models/{model_hash}")
async def get_model_info(
    model_hash: str,
    client_id: str = Depends(get_client_id)
):
    """Get model information with input validation."""
    # Validate model_hash format (assuming it's a hex string)
    if not all(c in '0123456789abcdef' for c in model_hash.lower()):
        raise HTTPException(status_code=400, detail="Invalid model hash format")

    if len(model_hash) != 64:  # Assuming 32-byte hash
        raise HTTPException(status_code=400, detail="Invalid model hash length")

    logger.debug(f"Client {client_id} requested model info for {model_hash}")
    result = await call_rpc("getmodelinfo", [model_hash])

    # Parse the response
    if isinstance(result, str) and result.startswith("ModelRecord("):
        fields_str = result[12:-1]
        fields = {}

        for field in fields_str.split(", "):
            key, value = field.split("=", 1)
            # Map field names from RPC response to API response
            field_map = {
                "name": "model_name",
                "commit": "model_commit"
            }
            mapped_key = field_map.get(key, key)

            if key in ["difficulty", "block_height"]:
                fields[mapped_key] = int(value) if value != "0" else 0
            else:
                fields[mapped_key] = value

        # Add model_hash to response
        fields["model_hash"] = model_hash

        return fields

    return result


# Miner proxy configuration
MINER_PROXY_PORT = int(os.getenv("MINER_PROXY_PORT", "8080"))


@app.get("/api/v1/miner/metrics")
async def get_miner_metrics(client_id: str = Depends(get_client_id)):
    """
    Get mining throughput metrics from the miner proxy.
    Returns token throughput (tokens/sec), completion rate, and totals.
    """
    miner_url = f"http://{MINER_HOST}:{MINER_PROXY_PORT}/status"

    async with httpx.AsyncClient(timeout=5.0) as client:
        try:
            response = await client.get(miner_url)
            response.raise_for_status()
            status = response.json()

            # Extract throughput and totals from proxy status
            proxy_status = status.get("proxy", {})
            throughput = proxy_status.get("throughput", {})
            totals = proxy_status.get("totals", {})

            return {
                "throughput": {
                    "prompt_tokens_per_sec": throughput.get("prompt_tokens_per_sec", 0.0),
                    "completion_tokens_per_sec": throughput.get("completion_tokens_per_sec", 0.0),
                    "total_tokens_per_sec": throughput.get("total_tokens_per_sec", 0.0),
                    "completions_per_min": throughput.get("completions_per_min", 0.0),
                },
                "totals": {
                    "prompt_tokens": totals.get("prompt_tokens", 0),
                    "completion_tokens": totals.get("completion_tokens", 0),
                    "completions": totals.get("completions", 0),
                },
                "active_requests": proxy_status.get("active_requests", 0),
                "miner_healthy": True,
            }
        except httpx.TimeoutException:
            logger.warning("Miner proxy timeout")
            raise HTTPException(status_code=504, detail="Miner proxy timeout")
        except httpx.HTTPError as e:
            logger.warning(f"Miner proxy unreachable: {e}")
            raise HTTPException(status_code=503, detail="Miner proxy unavailable")
        except Exception as e:
            logger.error(f"Error fetching miner metrics: {e}")
            raise HTTPException(status_code=500, detail="Internal error")


if __name__ == "__main__":
    import uvicorn
    if ENABLE_TLS:
        # Run with TLS
        uvicorn.run(
            app,
            host="0.0.0.0",
            port=API_PORT,
            ssl_keyfile=TLS_KEY,
            ssl_certfile=TLS_CERT,
            ssl_version=ssl.PROTOCOL_TLS_SERVER,
            ssl_ciphers="TLSv1.2:!aNULL:!eNULL:!EXPORT:!DES:!MD5:!PSK:!RC4",
            log_level=LOG_LEVEL.lower()
        )
    else:
        # Run without TLS
        uvicorn.run(
            app, 
            host="0.0.0.0", 
            port=API_PORT,
            log_level=LOG_LEVEL.lower()
        )
