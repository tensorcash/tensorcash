# Verification Service Documentation

## Overview

The Verification Service is a GPU-accelerated component responsible for validating GenAI models and their proof-of-work outputs within the TensorCash consensus mechanism. It re-executes the AI inference embedded in mined blocks and confirms that each proof meets the cryptographic and quality requirements before the node accepts the block.

The service runs alongside a core node. It receives validation requests over a ZeroMQ channel, schedules them across phase-specific worker queues, runs the appropriate verifier (block-proof or model-audit), and pushes a typed verdict back to the node.

## Architecture

### Core Components

1. **Request Receiver**: Binds a ZeroMQ PULL socket and feeds incoming FlatBuffers requests to the queue layer.
2. **Queue Manager**: Routes each request to the correct phase queue with idempotent, deduplicated semantics.
3. **Proof Verifier**: PyTorch-based engine that re-runs inference and checks block sanity, smell tests, and full proof replay.
4. **Model Verifier**: Audits a registered model and produces an operator-review report.
5. **Result Sender**: A single-owner broker thread that serializes responses and pushes them over a ZeroMQ PUSH socket.
6. **State Manager**: Tracks per-hash, phase-aware validation status, block dependencies, retries, and operator-review state.

### Technical Stack

```yaml
Base Image: nvidia/cuda:12.0.0-devel-ubuntu22.04
Framework: PyTorch 2.x (CUDA build)
Inference: vLLM / Transformers
Communication: ZeroMQ with FlatBuffers
VDF: ChiaVDF (built from source)
Acceleration: CUDA 12.0+ compatible GPUs
```

## Communication Protocol

### ZMQ Interface

The validator owns one inbound PULL socket and one outbound PUSH socket. The PULL socket is bound on the configured pull port; outbound responses are handled by a dedicated send broker that owns the PUSH socket, so application threads never touch the network socket directly.

```python
class AsyncValidator:
    def __init__(self, pull_port=None, push_host=None, push_port=None):
        pull_port = pull_port or constants.ZMQ_VERIFY_PULL_PORT
        push_host = push_host or constants.ZMQ_VERIFY_PUSH_HOST
        push_port = push_port or constants.ZMQ_VERIFY_PUSH_PORT

        self.context = zmq.Context()

        # PULL socket for receiving requests
        self.pull_socket = self.context.socket(zmq.PULL)
        self.pull_socket.bind(f"tcp://*:{pull_port}")

        # OUTBOUND: send broker owns the network PUSH socket
        endpoint = f"tcp://{push_host}:{push_port}"
        self.sender = ZmqSendBroker(
            endpoint=endpoint,
            hwm=2000,
            max_queue=10000,
            drop_on_backpressure=True,
            io_threads=1,
        )
```

### Message Schema

Requests and responses are FlatBuffers messages defined in `validation.fbs` (which includes the shared `proof.fbs`). The request carries a 32-byte `hash_id`, a `ValidationType`, and a union payload that is either a `BlockValidation` (a block header plus its proof-of-work blob) or a `ModelValidation` (a registered model's identity and difficulty claim).

```fbs
include "proof.fbs";

namespace proof;

enum ValidationType : uint8 {
  Quick = 0,
  Quick_Smell = 1,
  Full = 2,
  Model = 3,
  Challenge = 4,
  Logits = 5
}

enum ResponseValue : uint8 {
  Quick_OK = 0,
  Quick_Fail = 1,
  Quick_OK_Smell_OK = 2,
  Quick_OK_Smell_Fail = 3,
  Quick_Fail_Smell_OK = 4,
  Quick_Fail_Smell_Fail = 5,
  Full_Green = 6,
  Full_Amber = 7,
  Full_Red = 8,
  Model_OK = 9,
  Model_Fail = 10,
  Challenge_OK = 11,
  Challenge_Fail = 12,
  Model_Pending_Review = 13,
  Logits_OK = 14,
  Logits_Fail = 15
}

table BlockValidation {
  version: uint32;
  hash: [ubyte];             // 32 bytes
  prev_block_hash: [ubyte];  // 32 bytes
  merkle_root: [ubyte];      // 32 bytes
  timestamp: uint32;
  bits: uint32;
  nonce: uint32;
  pow_blob_hash: [ubyte];
  adjusted_bits: uint32;
  pow_blob: Proof;
}

table ModelValidation {
  model_name: string;
  model_commit: string;
  difficulty: int64;
  cid: string;
  extra: string;
  txid: [ubyte];             // 32 bytes
  block_hash: [ubyte];       // 32 bytes
  block_height: int32;
}

union ValidationUnion {
  BlockValidation,
  ModelValidation
}

table ValidationRequest {
  hash_id: [ubyte];          // 32 bytes
  validation_type: ValidationType;
  request: ValidationUnion;
}

table ValidationResponse {
  hash_identifier: [ubyte];
  enum_response: ResponseValue;
}

root_type ValidationRequest;
```

The `ValidationType`/`ResponseValue` enums are append-only: new values are added at the end so older readers stay wire-compatible (they warn on an unknown value rather than mis-parse). The mining side uses the separate `blockheader.fbs` schema (`BlockHeader` request / `MiningResponse`) for the work-distribution path.

### Validation Phases

| Phase | Purpose | Verdicts |
|-------|---------|----------|
| **Quick** | Cheap block-sanity and proof-shape checks. | `Quick_OK`, `Quick_Fail` |
| **Quick_Smell** | Superset of Quick that also runs a logits "smell test"; sets both the quick and smell results. | `Quick_OK_Smell_OK`, `Quick_OK_Smell_Fail`, `Quick_Fail_Smell_OK`, `Quick_Fail_Smell_Fail` |
| **Full** | Full inference replay of the proof. Re-runs on AMBER/RED a bounded number of times before reporting a final verdict. | `Full_Green`, `Full_Amber`, `Full_Red` |
| **Model** | Audits a registered model and queues an operator review. | `Model_OK`, `Model_Fail`, `Model_Pending_Review` |
| **Challenge** | Re-audits a previously-accepted block's model on challenge; gated behind operator review. | `Challenge_OK`, `Challenge_Fail` |
| **Logits** | Audit-only: sequence + logits replay against the claimed model, with no block sanity and no mining parameter envelope. | `Logits_OK`, `Logits_Fail` |

Full validation waits for the Quick phase to complete first. If Quick failed, Full short-circuits to `Full_Red` (when a Full request exists) without re-running inference, and propagates the failure to any dependent blocks.

## Queue Management

### Idempotency and Deduplication

Each phase has its own priority queue plus `enqueued`/`processing` sets guarded by a lock. A request is dropped if its phase already has a recorded result, or if the same `hash_id` is already queued or being processed for that phase. Completed Model, Logits, and Challenge results are answered immediately from cache on a duplicate request rather than re-enqueued.

```python
        # Validation queues (one per phase)
        self.quick_queue = queue.PriorityQueue()
        self.quick_smell_queue = queue.PriorityQueue()
        self.full_queue = queue.PriorityQueue()
        self.model_queue = queue.PriorityQueue()
        self.challenge_queue = queue.PriorityQueue()
        self.logits_queue = queue.PriorityQueue()

        # Per-phase dedup trackers and lock
        self._enqueued = {p: set() for p in
            ('quick', 'smell', 'full', 'model', 'challenge', 'logits')}
        self._processing = {p: set() for p in
            ('quick', 'smell', 'full', 'model', 'challenge', 'logits')}
        self._queue_lock = threading.RLock()

        # Phase-aware status: hash_id -> {'quick': ..., 'smell': ..., 'full': ...}
        self.validation_status = {}
        self.status_lock = threading.RLock()

        # Block dependencies: prev_hash -> dependent hashes
        self.block_dependencies = defaultdict(set)
        self.dependency_lock = threading.RLock()

        # Model auditor
        self.model_validator = ModelVerifier()
```

### Priority Queue Routing

Requests are prioritized by enqueue timestamp (older = higher priority), with a monotonic counter to break ties. A Full request also mirrors a higher-priority internal Quick precheck if Quick has not yet run, so the fast sanity check completes before the expensive inference replay starts.

```python
    def enqueue_request(self, message: bytes, retry_count: int = 0):
        request = ValidationRequest.ValidationRequest.GetRootAs(message, 0)

        hash_id_array = request.HashIdAsNumpy()
        hash_id = hash_id_array.tobytes() if hash_id_array is not None else None
        if hash_id is None:
            self.logger.error("Received request with no hash_id")
            return

        validation_type = request.ValidationType()
        priority = int(time.time() * 1000)

        request_data = {
            'hash_id': hash_id,
            'validation_type': validation_type,
            'request': request,
            'raw_message': message,
            'timestamp': time.time(),
            'retry_count': retry_count,
        }

        if validation_type == ValidationType.ValidationType.Quick:
            with self.status_lock:
                if 'quick' in self.validation_status.get(hash_id, {}):
                    return  # already completed
            with self._queue_lock:
                if (hash_id in self._enqueued['quick']
                        or hash_id in self._processing['quick']):
                    return  # already queued/processing
                self._enqueued['quick'].add(hash_id)
            self.quick_queue.put((priority, next(self._pq_counter), request_data))
        # ... analogous idempotent routing for Quick_Smell / Full / Model /
        #     Logits / Challenge ...
```

## Model Management

### Model Loading Strategy

> services/verification-api/src/proof_verifier.py

The verifier loads each model at its pinned commit, with resource-aware GPU placement. It picks the GPU with the most free memory that can hold the model plus a 20% buffer, falling back to CPU when no GPU fits or none is present. If a Hugging Face load fails and an IPFS CID is available, it retries from IPFS. The declared proof precision is checked against the model's native dtype, and a mismatch (other than an fp16-over-bf16 fallback) is flagged because it can produce verification errors.

```python
    def _load_model(self) -> None:
        """Load the model with resource-aware placement and IPFS fallback."""
        dtype = self.dtype

        dtype_strg = (torch.float16 if self.precision == 'fp16' else
                      torch.bfloat16 if self.precision == 'bf16' else
                      torch.int8 if self.precision == 'int8' else
                      torch.float32)

        # Precision check against the model's native dtype
        dtype_original = inspect_model_dtype(self.model_name, self.commit_hash)
        if dtype_strg != dtype_original:
            if dtype_strg == torch.float16 and dtype_original == torch.bfloat16:
                # fp16-over-bf16 fallback is tolerated but may cause errors
                pass
            else:
                # incompatible precision claim
                pass

        config = AutoConfig.from_pretrained(
            self.model_name,
            revision=self.commit_hash,
            trust_remote_code=True,
        )

        # Choose device map based on free VRAM (20% buffer over model size)
        device_map = None
        if torch.cuda.is_available():
            gpu_mem = self._get_all_gpu_mem()
            model_bytes_est = self._estimate_model_size(None)
            best_gpu = max(
                (idx for idx, free in gpu_mem.items()
                 if free > model_bytes_est * 1.2),
                key=gpu_mem.get,
                default=None,
            )
            if best_gpu is not None:
                device_map = {"": best_gpu}
        if device_map is None:
            device_map = {"": "cpu"}  # GPU insufficient or absent

        try:
            self.model = AutoModelForCausalLM.from_pretrained(
                self.model_name,
                revision=self.commit_hash,
                config=config,
                torch_dtype=dtype,
                device_map=device_map,
                trust_remote_code=True,
            ).eval()
        except Exception as hf_err:
            if self.ipfs_cid is None:
                raise
            warnings.warn(f"HF load failed ({hf_err}); attempting IPFS fallback",
                          RuntimeWarning)
            self.model = self._load_model_from_ipfs(self.ipfs_cid, dtype=dtype)

        self._ensure_tokenizer()
```

## GPU Optimization

### Memory Management

> services/verification-api/src/proof_verifier.py

A cached model can be promoted from CPU to GPU when memory frees up. The verifier clears CUDA caches first, then moves the model onto the best-fitting GPU (with the same 20% buffer), shards across multiple GPUs when no single device fits but the aggregate does, or keeps the model on CPU otherwise.

```python
    def _promote_cached_model_to_gpu(self) -> None:
        """Move cached model to GPU if memory allows."""
        if not (torch.cuda.is_available() and self.model):
            return

        torch.cuda.empty_cache()
        torch.cuda.synchronize()

        gpu_mem = self._get_all_gpu_mem()
        if not gpu_mem:
            return

        model_bytes = self._estimate_model_size(self.model)
        best_gpu = max(
            (idx for idx, free in gpu_mem.items() if free > model_bytes * 1.2),
            key=gpu_mem.get,
            default=None,
        )

        if best_gpu is not None:
            self.model.to(f"cuda:{best_gpu}")
            self.device = f"cuda:{best_gpu}"
            # Drop any cached KV states after moving
            for attr in list(self.__dict__.keys()):
                if attr.startswith('_kv_cache') or attr.startswith('_cached_ctx'):
                    delattr(self, attr)
            return

        total_gpu_free = sum(gpu_mem.values())
        if total_gpu_free > model_bytes * 1.2 and torch.cuda.device_count() > 1:
            self.model.cuda()       # let PyTorch distribute across GPUs
            self.device = "cuda"
        else:
            self.device = "cpu"     # insufficient GPU memory
```

## Configuration

### Environment Variables

> shared-utils/config/constants.py

```bash
# ZMQ Configuration
ZMQ_VERIFY_PULL_PORT        = int(os.getenv("ZMQ_VERIFY_PULL_PORT", "6001"))
ZMQ_VERIFY_RECV_TIMEOUT_MS  = int(os.getenv("ZMQ_VERIFY_RECV_TIMEOUT_MS", "6000000"))
ZMQ_VERIFY_RETRY_ATTEMPTS   = int(os.getenv("ZMQ_VERIFY_RETRY_ATTEMPTS", "10"))
ZMQ_VERIFY_RETRY_BACKOFF    = float(os.getenv("ZMQ_VERIFY_RETRY_BACKOFF", "1.0"))

ZMQ_VERIFY_PUSH_HOST        = os.getenv("ZMQ_VERIFY_PUSH_HOST", "0.0.0.0")
ZMQ_VERIFY_PUSH_PORT        = int(os.getenv("ZMQ_VERIFY_PUSH_PORT", "7001"))

# VDF Configuration
VDF_DISCRIMINANT_SIZE       = int(os.getenv("VDF_DISCRIMINANT_SIZE", "1024"))
VDF_CHECKPOINT_SIZE         = int(os.getenv("VDF_CHECKPOINT_SIZE", "32768"))
VDF_UPDATE_INTERVAL         = float(os.getenv("VDF_UPDATE_INTERVAL", "0.1"))
```

Additional runtime variables read by the validator include `VALIDATION_TTL_SECONDS` (status retention, default 5 days), `VALIDATION_CLEANUP_INTERVAL_SECONDS` (default hourly), `FULL_EXECUTION_RETRIES`, `OPERATOR_REVIEW_STATE_PATH`, and `OPERATOR_API_KEY` / `OPERATOR_CORS_ORIGIN` for the operator HTTP endpoints.

### Docker Configuration

> services/verification-api/cu120.Dockerfile

The image is a multi-stage build: a ChiaVDF/GMP build stage, a vLLM wheel-fetch stage, and a final CUDA runtime stage. FlatBuffers schemas are compiled to Python at build time and copied into the source tree.

```dockerfile
# Stage 1: Build ChiaVDF with assembly-optimized GMP from source
FROM python:3.10-slim AS chiavdf-builder
RUN apt-get update && apt-get install -y --no-install-recommends \
      nasm yasm build-essential cmake git patch pkg-config \
      libtool autoconf automake wget m4 libboost-all-dev libflint-dev && \
    rm -rf /var/lib/apt/lists/*

ENV GMP_VERSION=6.3.0
RUN wget https://gmplib.org/download/gmp/gmp-${GMP_VERSION}.tar.xz && \
    tar xf gmp-${GMP_VERSION}.tar.xz && cd gmp-${GMP_VERSION} && \
    ./configure --enable-assembly --enable-shared --enable-static --with-pic && \
    make -j$(nproc) && make install && ldconfig

# (ChiaVDF compiled to a wheel here)

# Stage 2: Fetch vLLM wheel
FROM python:3.10-slim as vllm-wheel-fetch
RUN pip install --upgrade pip wheel
RUN pip wheel vllm==0.8.5 --no-deps -w /wheels

# Stage 3: Final unified runtime image
FROM nvidia/cuda:12.0.0-devel-ubuntu22.04

# FlatBuffers 2.0.0 compiler + GMP/ChiaVDF copied from the build stage
RUN wget https://github.com/google/flatbuffers/releases/download/v2.0.0/Linux.flatc.binary.clang++-9.zip && \
    unzip Linux.flatc.binary.clang++-9.zip -d /usr/local/bin/ && \
    chmod +x /usr/local/bin/flatc

# PyTorch
RUN apt-get update && apt-get install -y --no-install-recommends ninja-build && \
    pip install --no-cache-dir \
      torch==2.6.0 \
      torchvision==0.21.0 \
      torchaudio==2.6.0

# Verifier requirements and source
COPY services/verification-api/requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt
COPY services/verification-api/src /app/src

# Compile FlatBuffers schemas to Python
COPY shared-utils/fb-schemas/proof.fbs /app/
COPY shared-utils/fb-schemas/validation.fbs /app/
COPY shared-utils/fb-schemas/blockheader.fbs /app/
RUN flatc --python proof.fbs && flatc --python validation.fbs && flatc --python blockheader.fbs

WORKDIR /app/src
CMD ["python", "main.py"]
```

A `generic.Dockerfile` variant parameterizes the CUDA and vLLM versions for tagged release images.

## Deployment

### Compose Service

The verifier runs as a container alongside the core node. Data and model paths are mounted as named volumes (or operator-chosen host paths) and the ZeroMQ and VDF settings are passed as environment variables.

```yaml
  verification-api:
    build:
      context: ../../../
      dockerfile: services/verification-api/generic.Dockerfile
    image: verification-api:latest
    container_name: verification-node
    volumes:
      - verifier-data:/data:rw       # operator review state, logs
      - models:/models:rw            # local model cache
    environment:
      # ZeroMQ
      ZMQ_VERIFY_PULL_PORT: "${ZMQ_VERIFY_PULL_PORT:-6001}"
      ZMQ_VERIFY_RECV_TIMEOUT_MS: "${ZMQ_VERIFY_RECV_TIMEOUT_MS:-6000000}"
      ZMQ_VERIFY_PUSH_HOST: "${ZMQ_VERIFY_PUSH_HOST:-0.0.0.0}"
      ZMQ_VERIFY_PUSH_PORT: "${ZMQ_VERIFY_PUSH_PORT:-7001}"

      # VDF
      VDF_DISCRIMINANT_SIZE: "${VDF_DISCRIMINANT_SIZE:-1024}"
      VDF_CHECKPOINT_SIZE: "${VDF_CHECKPOINT_SIZE:-32768}"
      VDF_UPDATE_INTERVAL: "${VDF_UPDATE_INTERVAL:-0.1}"

      # Logging
      LOG_LEVEL: "${LOG_LEVEL:-INFO}"

volumes:
  verifier-data:
  models:
```

The `/data` and `/models` mount sources are configurable; supply any host directory or named volume that has enough capacity for cached models. GPU access (the NVIDIA container runtime) is required for production inference.

## Monitoring and Operations

### Operator HTTP API

The validator runs a lightweight HTTP server for operator review and health. Auth is via an optional `OPERATOR_API_KEY` bearer token; when unset, auth is disabled for local-only operation. CORS is restricted to `OPERATOR_CORS_ORIGIN` when configured (never wildcard).

```
GET  /health                              — unauthenticated health check -> {"status": "ok"}
GET  /v1/operator/reviews                 — list pending reviews (status filter via ?status=)
GET  /v1/operator/reviews/stats            — review counts
GET  /v1/operator/reviews/{model_hash}     — single review detail
POST /v1/operator/reviews/{model_hash}/approve
POST /v1/operator/reviews/{model_hash}/reject
```

### Health Check

The `/health` endpoint is the only unauthenticated route; it always returns `200` with `{"status": "ok"}` and is evaluated before auth. The authenticated operator routes return `503` if the validator instance has not finished initializing.

```python
    def do_GET(self):
        path, query = self._parse_path()

        # Health check is unauthenticated
        if path == "/health":
            return self._send_json({"status": "ok"})

        if not self._check_auth():
            return

        v = _validator_instance
        if v is None:
            return self._send_json({"error": "Validator not initialized"}, 503)
        # ... operator review routes ...
```

### Status Retention

A background cleanup thread evicts validation-status entries older than `VALIDATION_TTL_SECONDS` (default 5 days), running every `VALIDATION_CLEANUP_INTERVAL_SECONDS` (default hourly), and drops their completion events to bound memory.

## Performance Optimization

- **Phase pipelining**: Quick/Smell run on dedicated workers ahead of Full, and a Full request mirrors a higher-priority internal Quick precheck so the cheap sanity check gates the expensive inference replay. Quick failure short-circuits Full to `Full_Red` without running the model.
- **Adaptive queue draining**: the Full worker shortens its dequeue timeout from 0.1s to 0.01s when its queue exceeds 10 items, so backlogs drain faster.
- **Bounded retries**: Full verdicts re-enqueue a fixed number of times (RED once, AMBER twice) to absorb transient nondeterminism before a final verdict; execution errors re-enqueue up to `FULL_EXECUTION_RETRIES` and never become a vote.
- **GPU placement and promotion**: models load onto the best-fitting GPU with a 20% memory buffer, can be promoted from CPU to GPU as memory frees, and shard across multiple GPUs when no single device fits.
- **Per-thread verifiers**: each worker owns its own `ProofVerifier`, avoiding cross-thread contention on model/KV-cache state.
- **Non-blocking outbound path**: a single broker thread owns the PUSH socket with a bounded queue; under backpressure it drops responses rather than block the verification workers.
- **Idempotent dedup**: duplicate requests for an already-completed or in-flight hash are answered from cache or dropped, so re-sends from the node never trigger redundant inference.

## Security Considerations

### Input Validation

Incoming requests are parsed defensively. A request with no `hash_id` is rejected; unknown `ValidationType` values are logged and ignored (forward-compatible with newer schemas); and the operator identifier on review endpoints must decode to a 32-byte value before it is accepted. Parse failures never produce a verdict — execution errors are logged but never turned into a `Fail`/`Red` vote that could be attributed to the miner.

```python
    def enqueue_request(self, message: bytes, retry_count: int = 0):
        hash_id = None
        try:
            request = ValidationRequest.ValidationRequest.GetRootAs(message, 0)
            hash_id_array = request.HashIdAsNumpy()
            hash_id = hash_id_array.tobytes() if hash_id_array is not None else None
            if hash_id is None:
                self.logger.error("Received request with no hash_id")
                return

            validation_type = request.ValidationType()
            # ... idempotent routing per known type ...
            else:
                self.logger.warning(f"Unknown validation type: {validation_type}")
        except Exception as e:
            # Parse/handling errors are logged, NOT converted into a vote
            self.logger.error(f"Error enqueuing request: {e}")

    @staticmethod
    def _decode_review_identifier(identifier_hex: str) -> list:
        """Decode an operator identifier; reject anything not 32 bytes."""
        try:
            raw = bytes.fromhex(identifier_hex)
        except ValueError:
            return []
        if len(raw) != 32:
            return []
        return [raw[::-1]] if raw[::-1] == raw else [raw[::-1], raw]
```

### Errors Never Become Votes

A local execution error during quick or full validation is logged and the per-hash completion event is cleared, but no failure response is emitted. This prevents a transient GPU/load problem on one validator from casting a `Quick_Fail`/`Full_Red` vote against an otherwise-valid block.

```python
    def send_error_response(self, hash_id: bytes, kind: str = 'quick'):
        """Do not turn local execution errors into validator votes."""
        self.logger.error(
            f"Execution error during {kind} validation for {hash_id.hex()}; "
            "not sending a failure response"
        )
        if kind in {'quick', 'full'}:
            self._clear_event(hash_id)
```

### Operator Review Auth

The operator HTTP API requires a bearer token when `OPERATOR_API_KEY` is set; missing or wrong tokens return `401`. CORS is restricted to a configured origin only. Pending and resolved reviews are persisted atomically (write-temp-then-rename) so review state survives restarts.

## Troubleshooting

### Common Issues

1. **GPU Memory Errors**
   ```bash
   # Monitor GPU memory usage
   watch -n 1 nvidia-smi
   ```
   The verifier already clears CUDA caches before promoting a model; persistent OOM usually means the chosen GPU cannot hold the model plus the 20% buffer, in which case it falls back to CPU.

2. **Model Loading Failures**
   A Hugging Face load failure falls back to IPFS when a CID is available; otherwise it raises. Confirm the model name and pinned commit are correct and that `/models` has sufficient capacity.

3. **Queue Backpressure**
   Under heavy load the outbound broker drops responses when its bounded queue fills (logged as "Outbound queue full, dropping response"); the node re-requests, and completed phases are answered from cache. The Full worker also tightens its dequeue timeout when its queue exceeds 10 items.
