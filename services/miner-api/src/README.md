# Mining Proxy Service

A high-performance HTTP proxy that injects Proof-of-Work (PoW) data into LLM inference requests, including VDF (Verifiable Delay Function) proofs and mining context. The proxy maintains minimum active GPU utilization and supports an optional priority mode that prefers external requests over dummy ones. It also supports OpenAI-compatible embeddings (pass-through) and the Responses API (with PoW injection and streaming).

## Architecture Overview

The service implements a lock-free, multi-threaded architecture with the following core components:

### Core Components

1. **LockFreeContext** (`context.py`)
   - Thread-safe shared state management using immutable snapshots
   - Atomic reference swapping leveraging Python's GIL
   - Tracks mining parameters, VDF state, and initialization status

2. **VDFService** (`vdf_service.py`)
   - Manages VDF proof generation using ChiaVDF library
   - Runs continuous proof computation in background thread
   - Automatically restarts VDF computation on block changes
   - Configurable discriminant size and checkpoint intervals

3. **ZMQListener** (`zmq_listener.py`)
   - Receives mining job updates via ZeroMQ PULL socket
   - Parses Flatbuffers-encoded block headers
   - Triggers VDF restarts on new blocks
   - Implements retry logic with exponential backoff

4. **RequestManager** (`components/proxy.py`)
    - HTTP proxy with PoW injection middleware
    - Maintains minimum active GPU requests for efficiency
    - Handles both real and dummy requests
    - Supports streaming responses for chat completions and responses API
    - Integrates with ModelClient for blockchain model registry

5. **PriorityRequestManager** (`components/proxy_with_priority.py` + `components/request_priority_manager.py`)
   - Optional enhanced proxy that prioritizes external requests over dummy ones
   - Aborts dummy requests 1-for-1 when at capacity (batch-aware)
   - Maintains minimum and maximum concurrency
   - Drop-in replacement for `RequestManager` (see “Priority Mode” below)

6. **ModelClient** (`components/model_synch.py`)
   - Synchronizes with blockchain model registry
   - Caches model configurations and difficulty adjustments
   - Provides model lookup by name or hash
   - Implements periodic polling for updates

7. **ProofCache** (`components/proof_cache.py`)
   - In-memory cache for generated proofs with TTL
   - Size-limited storage with LRU eviction
   - Thread-safe access with aliasing support

8. **ProofCollector** (`components/proof_collector.py`)
   - Collects proofs from inference servers via ZMQ
   - Stores proofs in cache for later retrieval
   - Runs on separate port (default: 7002)

9. **PromptGenerators** (`components/default_prompt_generator.py`, `components/genesis.py`)
   - IntelligentPromptGenerator: Creates diverse, contextual prompts
   - SeedPhrasePromptGenerator: Genesis mode for deterministic generation
   - Used for dummy request generation

## Project Structure

```
services/miner-api/
├── src/
│   ├── main.py                          # Application entry point with web server
│   ├── components/
│   │   ├── context.py                   # Lock-free context management
│   │   ├── vdf_service.py               # VDF proof generation service
│   │   ├── zmq_listener.py              # ZMQ mining job listener
│   │   ├── proxy.py                     # HTTP request proxy with PoW injection
│   │   ├── proxy_with_priority.py       # Optional priority-aware proxy
│   │   ├── request_priority_manager.py  # Priority/abortion logic
│   │   ├── model_synch.py               # Blockchain model synchronization
│   │   ├── proof_cache.py               # In-memory proof storage
│   │   ├── proof_collector.py           # Proof collection service
│   │   ├── constants.py                 # Configuration constants
│   │   ├── default_prompt_generator.py  # Intelligent prompt generation
│   │   └── genesis.py                   # Genesis prompt generation
│   ├── config/
│   │   └── constants.py                 # Additional configuration (overwritten in Docker)
│   ├── utils/                           # Created at build time (see below)
│   │   ├── pow_utils.py                 # PoW utility functions
│   │   └── uint256_arithmetics.py       # 256-bit arithmetic operations
│   └── proof/                           # Generated Flatbuffers code (build output)
├── proxy.Dockerfile                     # Multi-stage Docker build
├── proxy_requirements.txt               # Python dependencies
└── README.md                            # This file
```

## Serialization & Shared Utilities

The service uses Flatbuffers for efficient serialization. Schema files and certain utilities are copied during the Docker build process and therefore may not appear in the local src tree until built:

- Flatbuffers schemas (from `shared-utils/fb-schemas/`):
  - `proof.fbs` — proof data structures
  - `blockheader.fbs` — block header serialization
  - `validation.fbs` — validation structures

- Utilities (from `shared-utils/`):
  - `chiavdf/` — VDF implementation (built as a wheel in a separate stage)
  - `pow-utils/` — PoW helpers copied to `/app/src/utils/`
  - `config/constants.py` — shared configuration copied over `src/config/constants.py`

Notes for local dev/testing:
- The `utils/` and `proof/` folders are created at build time; local unit tests provide mocks when running without Docker (see Testing below).
- In Docker, `shared-utils/config/constants.py` intentionally overwrites `src/config/constants.py`.

## Configuration

All configuration is managed through environment variables:

### HTTP Server Configuration
- `HTTP_HOST`: Listen address (default: "0.0.0.0")
- `HTTP_PORT`: Listen port (default: 8080)
- `TARGET_URL`: Upstream base URL (no path) (default: "http://localhost:8000"). The proxy normalizes `TARGET_URL` to its origin (scheme+host+port). If a path is present, it is ignored and a warning is logged.
- `API_KEY`: Authentication key for upstream server

### ZMQ Configuration
- `ZMQ_PULL_PORT`: ZMQ pull socket port for mining jobs (default: 6000)
- `ZMQ_RECV_TIMEOUT_MS`: Receive timeout in milliseconds (default: 6000000)
- `TEST_MODE`: Enable test mode (default: true)
- `ZMQ_RETRY_ATTEMPTS`: Retry attempts for ZMQ operations (default: 10)
- `ZMQ_RETRY_BACKOFF`: Backoff multiplier for retries (default: 1.0)

### VDF Configuration
- `VDF_DISCRIMINANT_SIZE`: VDF discriminant size in bits (default: 1024)
- `VDF_CHECKPOINT_SIZE`: VDF checkpoint interval (default: 32768)
- `VDF_UPDATE_INTERVAL`: VDF update check interval in seconds (default: 0.1)

### Request Management
- `MIN_ACTIVE_REQUESTS`: Minimum concurrent requests to maintain (default: 4)
- `DUMMY_REQUEST_TIMEOUT`: Timeout for dummy requests in seconds (default: 30)
- `MONITOR_INTERVAL`: Request monitor interval in seconds (default: 1.0)
- `DUMMY_RETRY_ATTEMPTS`: Retry attempts for failed dummy requests (default: 10)
- `DUMMY_RETRY_BACKOFF`: Backoff multiplier for dummy retries (default: 1.0)
- `BATCH_SIZE`: Batch size for dummy requests (default: 20)

### Model Registry Configuration
- `MODEL_API_URL`: Model registry API URL (default: "http://localhost:8080")
- `MODEL_API_KEY`: API key for model registry
- `MODEL_REQUIRE_AUTH`: Require authentication for model API (default: false)
- `MODEL_RETRY_ATTEMPTS`: Retry attempts for model API (default: 3)
- `MODEL_RETRY_BACKOFF`: Backoff for model API retries (default: 1.0)
- `MODEL_POLL_INTERVAL`: Model registry poll interval in seconds (default: 600)

### Mining Configuration
- `DEFAULT_DIFFICULTY`: Default mining difficulty (default: 1000000)
- `BASE_NBITS`: Base difficulty target in nBits format (default: 536990216)

### Proof Cache Configuration
- `PROOF_CACHE_ENABLED`: Enable proof caching (default: true)
- `PROOF_CACHE_TTL_SECONDS`: Proof cache TTL (default: 900)
- `PROOF_CACHE_MAX_SIZE_MB`: Maximum cache size in MB (default: 500)
- `PROOF_COLLECTOR_PORT`: Proof collector ZMQ port (default: 7002)

### Special Modes
- `MCP_MODE`: Enable MCP (Model Context Protocol) server (default: false)
- `LLAMA_CPP`: Enable llama.cpp compatibility mode (default: false)
- `GENESIS_GENERATOR`: Enable genesis prompt generation (default: false)

### Logging
- `LOG_LEVEL`: Logging level (default: "INFO")

## API Endpoints

### POST /v1/completions
Proxy endpoint for text completions with PoW injection.

**Request:**
```json
{
  "model": "Qwen/Qwen3-8B",
  "prompt": ["Hello, world!"],
  "max_tokens": 256,
  "temperature": 0.7,
  "top_k": 50,
  "top_p": 1.0
}
```

**Injected PoW Data:**
The proxy automatically injects the following into `extra_sampling_params.pow`:
```json
{
  "block_hash": "current_block_hash",
  "vdf": "base64_encoded_vdf_proof",
  "tick": 1000000,
  "target": "adjusted_difficulty_target",
  "header_prefix": "bitcoin_header_prefix",
  "ipfs_cid": "model_ipfs_cid",
  "request_id": 12345,
  "difficulty": 1000000
}
```

### POST /v1/chat/completions
Proxy endpoint for chat completions with PoW injection. Supports streaming responses.

### POST /v1/embeddings
Pass-through to upstream embeddings endpoint (no PoW injection). Request and response bodies are proxied as-is.

### OpenAI Responses API (with PoW)
- `POST /v1/responses`: PoW injection by default; supports streaming when `stream=true`. Forwarded to upstream OpenAI-compatible endpoint.
- `GET /v1/responses/{response_id}`: Retrieve async response state (pass-through).
- `POST /v1/responses/{response_id}/cancel`: Cancel an async response (pass-through).

### GET /v1/models
Proxies model list from upstream server.

### GET /status
Returns comprehensive service status. Example (base RequestManager):
```json
{
  "context": {
    "block_hash": "current_block_hash",
    "request_id": 12345,
    "vdf_tick": 1000000,
    "has_vdf_proof": true,
    "age_seconds": 45.2,
    "miner_initialized": true,
    "vdf_initialized": true
  },
  "vdf": { "running": true, "discriminant_size": 1024, "checkpoint_size": 32768 },
  "zmq": { "running": true, "port": 6000, "timeout_ms": 6000000 },
  "proxy": {
    "active_requests": 2,
    "target_url": "http://localhost:8000/v1/completions",
    "min_active": 4,
    "session_open": true,
    "model_sync": { "initialized": true, "total_models": 2, "sample_models": ["Qwen/Qwen3-8B"] }
  }
}
```

When Priority Mode is enabled (see below), `proxy.status` also includes a `priority` block with live statistics:
```json
"priority": {
  "total_external": 150,
  "total_dummy": 500,
  "total_aborted": 45,
  "current_external": 2,
  "current_dummy": 4,
  "capacity_used": 0.75,
  "can_accept_external": true
}
```

### GET /health
Simple health check endpoint.

### Proof Retrieval Endpoints

#### GET /v1/proof/{completion_id}
Retrieve binary proof data for a completion ID.

#### GET /v1/proof/status/{completion_id}
Check if proof is available for a completion ID.

#### GET /v1/proof/keys
List all cached completion IDs (debug endpoint).

#### GET /v1/proof/stats
Get proof cache statistics.

## Docker Deployment

The service uses a multi-stage Docker build for optimization:

### Building the Image
```bash
docker build -f proxy.Dockerfile -t mining-proxy:latest .
```

### Running with Docker
```bash
docker run -d \
  --name mining-proxy \
  -p 8080:8080 \
  -p 6000:6000 \
  -e TARGET_URL=http://vllm-server:8000 \
  -e ZMQ_PULL_PORT=6000 \
  -e MIN_ACTIVE_REQUESTS=4 \
  -e MODEL_API_URL=http://model-registry:8080 \
  mining-proxy:latest
```

### Docker Compose Example
```yaml
version: '3.8'
services:
  mining-proxy:
    build:
      context: ../../
      dockerfile: services/miner-api/proxy.Dockerfile
    ports:
      - "8080:8080"
      - "6000:6000"
    environment:
      - TARGET_URL=http://vllm:8000
      - MIN_ACTIVE_REQUESTS=4
      - MODEL_API_URL=http://model-registry:8080
      - LOG_LEVEL=INFO
    volumes:
      - /bcore_data:/bcore_data
    depends_on:
      - vllm
      - model-registry
```

## Request Flow

1. **Incoming Request** → RequestManager receives HTTP request
2. **Model Lookup** → ModelClient fetches model configuration from blockchain
3. **Context Read** → LockFreeContext provides current mining snapshot
4. **PoW Injection** → Request modified with mining parameters and VDF proof
5. **Difficulty Adjustment** → Target adjusted based on model difficulty
6. **Upstream Forward** → Modified request sent to inference server
7. **Response Handling** → Stream or buffered response returned to client
8. **Proof Collection** → ProofCollector stores generated proofs in cache

## Dummy Request Management

The proxy maintains minimum GPU utilization through intelligent dummy request generation:

1. **Monitor Loop** checks active request count every `MONITOR_INTERVAL`
2. If below `MIN_ACTIVE_REQUESTS`, generates dummy requests
3. Dummy requests use diverse prompts from prompt generators
4. Failed dummy requests retry with exponential backoff
5. Stale requests (>5 minutes) are automatically cleaned up

## Thread Safety

The service implements a lock-free design for high performance:

- **Immutable Snapshots**: All shared state uses immutable data structures
- **Atomic Updates**: Python's GIL ensures reference assignment atomicity
- **No Explicit Locks**: Eliminates lock contention and deadlock risks
- **Thread Isolation**: Each component runs in its own thread with message passing

## MCP (Model Context Protocol) Support

When `MCP_MODE=true`, the service exposes an additional MCP server on port 8090:

- **Tools Available**:
  - `proxy_status`: Get full proxy status as JSON
  - `chat_completion`: Generate completions with PoW injection
- **Endpoint**: POST /mcp (Streamable HTTP)
- **Use Case**: Integration with MCP-compatible tools and IDEs

## Performance Considerations

- **Batch Processing**: Dummy requests use configurable batch sizes
- **Connection Pooling**: Persistent HTTP sessions for upstream requests
- **Async I/O**: Non-blocking request handling with aiohttp
- **Memory Management**: Proof cache with size limits and TTL
- **CPU Optimization**: ChiaVDF compiled with assembly optimizations

## Error Handling

- **Retry Logic**: Configurable retries for ZMQ, model API, and dummy requests
- **Graceful Degradation**: Falls back to default models if registry unavailable
- **Request Validation**: Input sanitization and bounds checking
- **Circuit Breaking**: Stale request cleanup prevents resource leaks

### Proxy Error Mapping
- Upstream network/client errors map to `502 Bad Gateway`.
- Upstream timeouts map to `504 Gateway Timeout`.
- Non-streaming responses are buffered and returned with upstream status and headers.
- Streaming responses (SSE) are proxied with proper termination (`write_eof`) and upstream resources are released to avoid connection pool leaks.

## Monitoring & Debugging

- Comprehensive logging with configurable levels
- Request tracking with unique IDs
- Performance metrics in status endpoint
- Debug endpoints for proof cache inspection

## Security Considerations

- Input validation on all request parameters
- Secure handling of API keys and authentication
- No storage of sensitive data in logs
- Containerized deployment for isolation

## Priority Mode

The priority system allows external requests to preempt dummy requests while maintaining minimum GPU concurrency. It is implemented by `PriorityRequestManager` and can replace `RequestManager` with a small change:

- In `src/main.py`, replace:
  - `from components.proxy import RequestManager`
  - with `from components.proxy_with_priority import PriorityRequestManager as RequestManager`

This swap enables priority handling without other code changes. See `PRIORITY_SYSTEM.md` for architecture, tuning, and monitoring details.

Tip: You may guard this with an env flag (e.g., `PRIORITY_MODE=true`) and select the manager at runtime.

Notes:
- In priority mode, POST `/v1/responses` is treated as an external request for capacity management and may preempt dummy requests when at capacity. Retrieval/cancel endpoints are pass-through and not prioritized.

## Testing

The repository includes unit tests and E2E tests, with scripts to run them in isolation (mocks) or fully (Docker):

- Unit tests (isolated, with mocks):
  - `services/miner-api/tests/run_isolated_tests.py`
  - Minimal local environment; creates mock `utils`, `chiavdf`, etc.

- Unit and integration tests (local environment):
  - `services/miner-api/tests/run_tests.sh`
  - `services/miner-api/tests/setup_test_env.py` to copy `shared-utils` and optionally build Flatbuffers

- E2E tests (Docker Compose):
  - `services/miner-api/tests/docker-compose.test.yml`
  - Brings up a mock vLLM server, the proxy image, and runs `e2e_tests.py`

Common commands:
```
cd services/miner-api/tests
python3 run_isolated_tests.py                # Fast local pass (mocks)
docker-compose -f docker-compose.test.yml up --build  # Full E2E
```

### Coverage & Quality Gates

- Unit tests enforce a minimum coverage gate. The test runner fails if coverage drops below the threshold.
- Run unit tests locally with coverage gate:
  - `cd services/miner-api/tests && ./run_tests.sh unit` (uses `--cov-fail-under`, default 25%)
- Adjust the gate with `COV_FAIL_UNDER` env var, e.g. `COV_FAIL_UNDER=50 ./run_tests.sh unit`.
- Scope covered:
  - `src/components/` (core services) and `src/main.py`
  - Report: `tests/test_results/coverage/index.html` and terminal summary (missing lines shown)

Plan to raise the threshold progressively as we add tests across modules like `constants.py`, `proof_cache.py`, `default_prompt_generator.py`, `model_synch.py`, and the proxy components.
