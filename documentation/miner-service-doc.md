# Miner Service Documentation

## Overview

The Miner Service is the most sophisticated component of the blockchain GenAI system, serving dual purposes: participating in blockchain mining through AI inference and providing external AI services via an OpenAI-compatible API. It orchestrates prompts, blockchain data, verifiable delay functions (VDF), and model configurations to generate valid mining proofs.

## Architecture Overview

### Component Hierarchy

```
Miner Service
├── Miner Proxy (Gateway/Orchestrator)
│   ├── API Gateway (OpenAI Compatible)
│   ├── Job Scheduler
│   ├── Prompt Manager
│   └── Model Synchronizer
├── VDF Service (Time Proof Generator)
└── Inference Backend(s)
    ├── Small: llama.cpp (Single Instance)
    ├── Medium: vLLM (Single Host, Multi-GPU)
    └── Large: vLLM Cluster (Kubernetes)
```

## Miner Proxy Component

### Overview

The Miner Proxy acts as the intelligent gateway, coordinating between external API requests, blockchain mining requirements, and the inference backend infrastructure.

### API Gateway

#### OpenAI-Compatible Endpoints

>  services/miner-api/src/components/proxy.py 

```python
    async def proxy_request(self, request: web.Request) -> web.Response:
        """Proxy request with PoW injection"""
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
            
            # Check if model client is ready
            if not self.model_client or not self.model_client._initialized:
                logger.error("[RequestManager] Model client not ready for request")
                return web.Response(
                    text='{"error": "Service initializing, please try again"}',
                    status=503,
                    content_type='application/json'
                )
            
            modified_data = self._inject_pow_data(data)
            
            # Forward request
            headers = {k: v for k, v in request.headers.items() 
                      if k.lower() not in ['host', 'content-length']}
            
            async with self.session.post(
                self.target_url,
                json=modified_data,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=300)  # 5 min timeout for streaming
            ) as response:
                body = await response.read()
                
                duration = time.time() - self.active_requests[request_id]
                logger.info(f"[RequestManager] Request {request_id} completed in {duration:.2f}s")
                
                return web.Response(
                    body=body,
                    status=response.status,
                    headers=response.headers
                )
                
        except asyncio.TimeoutError:
            logger.error(f"[RequestManager] Request {request_id} timed out")
            return web.Response(
                text='{"error": "Request timeout"}',
                status=504,
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

```

#### Streaming Implementation

Standard from vLLM / Llama.cpp backend

### Job Scheduler

#### Mining Job Management & Dynamic Job Creation

>  services/miner-api/src/components/proxy.py 

```python
    async def _monitor_loop(self):
        """Monitor and maintain minimum active requests"""
        logger.info(f"[RequestManager] Monitor loop started, maintaining {self.min_active} active requests")
        
        try:
            while True:
                if self.context.vdf_initialised and self.context.miner_initialised:
                    # Clean up stale requests (older than 5 minutes)
                    current_time = time.time()
                    stale_ids = [
                        rid for rid, start_time in self.active_requests.items()
                        if current_time - start_time > 300
                    ]
                    for rid in stale_ids:
                        logger.warning(f"[RequestManager] Cleaning up stale request: {rid}")
                        del self.active_requests[rid]
                    
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
```


### Prompt Management


#### Synthetic Prompt Generation

> services/miner-api/src/components/default_prompt_generator.py

```python
import random
import string

class IntelligentPromptGenerator:
    def __init__(self):
        self.templates = [
            "Write a story about a {trait} {occupation} who discovers {event} in {setting}.",
            "Describe a {adjective} device designed to {function}, found in a {time_period} marketplace.",
            "Design a storyline for a {business_type} that emphasizes {aesthetic_style} in its branding.",
            "Imagine a {tone} conversation between a {role_1} and a {role_2} in {location}.",
        ]

        self.word_bank = {
            "trait": ["curious", "lazy", "obsessive", "naive", "vindictive", "idealistic"],
            "occupation": ["chef", "astronaut", "jeweler", "street magician", "crypto miner"],
            "event": ["a hidden portal", "a time loop", "a haunted mirror", "a talking fox"],
            "setting": ["an underwater city", "a deserted space station", "a post-apocalyptic museum"],

            "adjective": ["shimmering", "rusty", "modular", "organic"],
            "function": ["store dreams", "translate animal speech", "measure nostalgia", "synthesize joy"],
            "time_period": ["neo-Victorian", "retrofuturist", "solar punk", "post-human"],

            "business_type": ["AI startup", "sustainable fashion label", "coffee shop in space"],
            "aesthetic_style": ["brutalist", "vaporwave", "minimalist", "biomorphic"],

            "tone": ["whimsical", "existential", "dark", "absurd"],
            "role_1": ["robot therapist", "17th-century mathematician", "child AI trainer"],
            "role_2": ["time-traveling snail", "retired hacker", "cosmic librarian"],
            "location": ["a glitching metaverse café", "a lunar subway tunnel", "a memory vault"],
        }

    def generate_prompt(self, template: str = None) -> str:
        # Pick a random template if none provided
        tmpl = template or random.choice(self.templates)


```

#### Utilization Management

Mining proofs are produced as a side effect of inference: every completion the
backend serves is injected with PoW data and may yield a valid share. To keep the
GPU continuously busy (and therefore continuously mining) when external API traffic
is low, the proxy maintains a floor of in-flight requests. `MIN_ACTIVE_REQUESTS`
(default `32`) sets that floor and the monitor loop tops it up with synthetic
("dummy") completions generated from the prompt templates below.

Dummy generation is gated: it is skipped while mining is paused (post-solution
cooldown) and while the mining context is stale (no recent block from the Core
Node, `MINING_STALE_THRESHOLD_SECONDS`), so the worker never grinds against an
outdated or empty block template.

> services/miner-api/src/components/proxy.py

```python
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
                model_name = constants.DEFAULT_MODEL_CONFIG.model_name
            elif self.model_client and self.model_client.models_by_name:
                model_name = self._select_dummy_model_name()
            else:
                logger.error("[RequestManager] No models available for dummy request")
                return

            prompts = [self.prompt_generator.generate_prompt() for _ in range(constants.BATCH_SIZE)]
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
                    break
                except Exception as e:
                    if attempt == constants.DUMMY_RETRY_ATTEMPTS:
                        logger.error(f"[RequestManager] Dummy {dummy_id} failed after {attempt} attempts: {e}")
                    else:
                        delay = constants.DUMMY_RETRY_BACKOFF * (2 ** (attempt - 1))
                        await asyncio.sleep(delay)
        finally:
            self._unregister_dummy_task(dummy_id)
            self.active_requests.pop(dummy_id, None)
```

### Model Synchronization

> services/miner-api/src/components/model_synch.py

```python
class ModelClient:
    """
    Client to fetch model information from the Bitcoin Core Model API.
    Builds in-memory dictionaries of models keyed by model_hash and by model_name.
    """
    def __init__(self):
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
```

## VDF Service Component

### Verifiable Delay Function Implementation

> services/miner-api/src/components/vdf_service.py

```python
"""
VDF (Verifiable Delay Function) service for proof generation
"""
class VDFService:
    """Manages the VDF prover with automatic reset on block changes"""
    
    def __init__(self, context: LockFreeContext):
        self.context = context
        self.discriminant_size = constants.VDF_DISCRIMINANT_SIZE
        self.checkpoint_size = constants.VDF_CHECKPOINT_SIZE
        self.update_interval = constants.VDF_UPDATE_INTERVAL
        
        self.prover = None
        self.running = False
        self.thread = None
        self._current_block_hash = None
        self._reset_event = threading.Event()  # Event to trigger immediate reset
        self.next_log_threshold = 0 
```

> shared-utils/chiavdf/src/python_bindings/fastvdf.cpp

```cpp
"""
VDF (Verifiable Delay Function) service for proof generation
"""
    py::class_<ThreadedStreamingProver>(m, "StreamingProver")
      .def(py::init([](py::bytes challenge_hash,
                       int       discr_bits,
                       uint64_t  checkpoint_n,
                       uint64_t  max_iters,
                       uint64_t  proof_interval_ms)
           {
               // Convert Python bytes -> std::vector<uint8_t>
               std::string s = challenge_hash;  // must be exactly 32 bytes
               if (s.size() != 32)
                   throw std::runtime_error("challenge_hash must be exactly 32 bytes");
               std::vector<uint8_t> v(s.begin(), s.end());
               return new ThreadedStreamingProver(
                   std::move(v),
                   discr_bits,
                   checkpoint_n,
                   max_iters,
                   proof_interval_ms
               );
           }),
           py::arg("challenge_hash"),
           py::arg("discriminant_size_bits") = 1024,
           py::arg("checkpoint_n")           = 10'000,
           py::arg("max_iters")              = 100'000'000,
           py::arg("proof_interval_ms")      = 1'000,
           "Create a streaming prover.  \n\n"
           "  • challenge_hash: 32-byte SHA-256 digest  \n"
           "  • discriminant_size_bits: size of the class group  \n"
           "  • checkpoint_n: number of squarings per proof chunk  \n"
           "  • max_iters: maximum total squarings supported  \n"
           "  • proof_interval_ms: (ignored—proofs fire immediately each chunk)")
        .def("start", &ThreadedStreamingProver::start,
             "Start the prover threads. Must be called before using the prover.")
        .def("get_last_available_proof",
            [](ThreadedStreamingProver &self) {
                auto pr = self.get_last_available_proof();
                const auto &blob = pr.first;
                uint64_t iters   = pr.second;
                // return (bytes, int)
                return std::make_pair(
                    py::bytes(reinterpret_cast<const char*>(blob.data()), blob.size()),
                    iters
                );
            },
            "Get the last proof as (blob: bytes, iterations: int); empty blob if none yet.")
        .def("get_current_iterations", &ThreadedStreamingProver::get_current_iterations,
             "Get the current number of iterations completed")
        .def("set_verbose", &ThreadedStreamingProver::set_verbose,
             "Enable/disable verbose logging")
        .def("stop", &ThreadedStreamingProver::stop,
             "Stop the prover threads")
        .def("reset", [](ThreadedStreamingProver &self, py::bytes new_challenge_hash) {
            std::string s = new_challenge_hash;
            if (s.size() != 32)
                throw std::runtime_error("challenge_hash must be exactly 32 bytes");
            std::vector<uint8_t> v(s.begin(), s.end());
            self.reset(std::move(v));
        }, py::arg("new_challenge_hash"),
        "Reset the prover with a new challenge hash");  // <- This semicolon ends the whole statement

```


## Inference Backend Implementations

The proxy does not embed an inference engine. Each backend is an external,
OpenAI-compatible HTTP server that the proxy forwards to over `aiohttp`, after
injecting PoW data into the request body. The backend kind is selected by
configuration; only the request shaping and upstream routing differ.

### Small Scale: llama.cpp Backend

A single `llama.cpp` server (`/v1/completions`) for low-resource / desktop
operation. Set `LLAMA_CPP=True`. In this mode the proxy:

- generates one prompt per request rather than a batch (`llama.cpp` completion
  endpoints expect a single `prompt`, not the batched list vLLM accepts);
- carries the PoW payload under `extra_sampling_params` rather than `vllm_xargs`
  (controlled by `USE_VLLM_XARGS`, which defaults to `not LLAMA_CPP`);
- adds `model_identifier` and `compute_precision` (`bf16`) into the PoW payload so
  the verifier can pin the exact model/precision used.

```python
# constants.py
LLAMA_CPP = os.getenv("LLAMA_CPP", "False") == "True"
# vLLM >=0.16 renamed extra_sampling_params -> vllm_xargs;
# llama.cpp still uses extra_sampling_params unless explicitly overridden.
USE_VLLM_XARGS = not LLAMA_CPP

# proxy._inject_pow_data(...)
if constants.LLAMA_CPP:
    pow_payload['pow']['model_identifier'] = f"{model_config.model_name}@{model_config.model_commit}"
    pow_payload['pow']['compute_precision'] = "bf16"

if constants.USE_VLLM_XARGS:
    data['vllm_xargs'] = {**(data.get('vllm_xargs') or {}), **pow_payload}
else:
    data['extra_sampling_params'] = {**(data.get('extra_sampling_params') or {}), **pow_payload}
```

### Medium Scale: vLLM Single Host

One or more vLLM instances on a single multi-GPU host. The proxy forwards batched
prompts (`BATCH_SIZE`, default `32`) to `TARGET_URL` and carries the PoW payload
under `vllm_xargs` (vLLM ≥ 0.16). To front several vLLM instances from one proxy —
for example a distinct upstream per model — set `MODEL_ROUTES` to a per-model base
URL map; requests for an unrouted model fall back to `TARGET_URL`.

```python
# constants.py
TARGET_URL = os.getenv("TARGET_URL", "http://localhost:8000")  # base origin, no path
# MODEL_ROUTES=Model/Name@commit=http://127.0.0.1:8001,Other/Model@commit=http://127.0.0.1:8000
MODEL_ROUTES = {}  # parsed from the MODEL_ROUTES env var

# proxy: resolve the upstream for a given model
def _backend_base_url(self, model_name) -> str:
    """Resolve the upstream base URL for a model (MODEL_ROUTES multi-backend
    routing); falls back to the default TARGET_URL backend for unrouted models
    and model-less requests."""
    if model_name and self._model_base_urls:
        routed = self._model_base_urls.get(model_name)
        if routed:
            return routed
    return self._base_url
```

## Troubleshooting

### Common Issues

1. **High Latency**
   - Check GPU utilization
   - Verify batch sizes
   - Monitor network latency
   - Review model loading times

2. **Memory Issues**
   ```bash
   # Monitor GPU memory
   nvidia-smi dmon -i 0
   
   # Clear cache
   echo 3 > /proc/sys/vm/drop_caches
   ```

3. **Mining Efficiency**
   - Adjust synthetic prompt generation
   - Tune difficulty parameters
   - Optimize model selection

4. **Backend Failures**
   - Check the `/health` and `/status` endpoints
   - Confirm the upstream backend (`TARGET_URL` / `MODEL_ROUTES`) is reachable
   - Verify model availability

### Status Endpoints

The proxy exposes two introspection routes alongside the OpenAI-compatible API. A
lightweight liveness check at `GET /health`, and an aggregate status snapshot at
`GET /status` that composes the status of each subsystem (mining context, VDF
service, the block-template ZMQ listener, the request manager/proxy, and — when
present — the worker client).

> services/miner-api/src/main.py

```python
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
```

