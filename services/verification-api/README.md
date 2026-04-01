# Verification API Service

This service validates block proofs and model claims for the TensorCash network. It exposes an asynchronous ZeroMQ interface that accepts FlatBuffers `ValidationRequest` messages and emits `ValidationResponse` messages. Internally, it schedules work across multiple queues (Quick, Quick_Smell, Full, Model), coordinates phases per hash, and handles dependency propagation and backpressure-safe egress.

## Overview

- Entry: ZMQ `PULL` socket receives FlatBuffers `ValidationRequest` bytes.
- Processing: Worker threads per queue run validators:
  - Quick: fast proof sanity checks
  - Quick_Smell: superset that performs Quick and a “smell” test, returning combined enums
  - Full: deterministic, slower proof verification; includes retry policy
  - Model: validates model metadata/claims
- Coordination: Per-hash phase state (quick, smell, full), event signaling to gate Full on Quick, and prev-hash dependency tracking for failure propagation.
- Exit: A dedicated `ZmqSendBroker` owns a ZMQ `PUSH` socket and safely sends `ValidationResponse` bytes, optionally dropping on backpressure.

Code entrypoint: `services/verification-api/src/main.py` (class `AsyncValidator`).

## Message Schema

The service uses FlatBuffers generated bindings from `shared-utils/fb-schemas` under the Python package `utils.proof` (tests alias this path). Key types:

- `ValidationRequest` (fields):
  - `hash_id: [ubyte]` (32 bytes)
  - `validation_type: ValidationType`
  - `request: ValidationUnion` → one of:
    - `BlockValidation` (fields include `prev_block_hash`, `pow_blob`, etc.)
    - `ModelValidation` (model metadata)
- `ValidationType` values used by this service:
  - `Quick`, `Quick_Smell`, `Full`, `Model`
- `ValidationResponse` (fields):
  - `hash_identifier: [ubyte]` (echoes `hash_id`)
  - `enum_response: ResponseValue`
- `ResponseValue` (selected values):
  - Quick path: `Quick_OK`, `Quick_Fail`, `Quick_OK_Smell_OK`, `Quick_OK_Smell_Fail`, `Quick_Fail_Smell_Fail`
  - Full path: `Full_Green`, `Full_Amber`, `Full_Red`
  - Model path: `Model_OK`, `Model_Fail`

Generated Python modules for these schemas live at `shared-utils/fb-schemas/proof/*.py` and are imported in code as `utils.proof.<Type>`.

## Architecture and Components

- `AsyncValidator` (src/main.py):
  - Binds ZMQ `PULL` on `tcp://*:<pull_port>` to receive requests.
  - Creates a `ZmqSendBroker` that connects to `tcp://<push_host>:<push_port>` to send responses.
  - Queues: `quick_queue`, `quick_smell_queue`, `full_queue`, `model_queue` (all `PriorityQueue`). Items carry a timestamp-based priority and a tie-breaker counter.
  - State tracking:
    - `validation_status[hash]` stores phase results: `quick`, `smell`, `full`, plus `created_at`, `timestamp`, and `prev_hash`.
    - `full_requested` set marks hashes that explicitly requested Full; used to decide if propagated failures should emit a response.
    - `block_dependencies[prev_hash] -> {hashes}` enables failure propagation along the chain.
    - `validation_events[hash]` is used to gate Full until Quick finishes (or is learned via Quick_Smell).
  - Workers (daemon threads):
    - `receive_requests()`: parses `ValidationRequest` and enqueues by `ValidationType`.
    - `process_quick_validations()`: drains `quick_queue` and calls `validate_quick`.
    - `process_quick_smell_validations()`: drains `quick_smell_queue` and calls `validate_quick_smell`.
    - `process_full_validations()`: waits for Quick to complete (`wait_for_quick_validation`), then calls `validate_full` (with retry policy). If Quick failed and Full was requested, it short-circuits to `Full_Red`.
    - `process_model_validations()`: drains `model_queue` and calls `validate_model`.
    - `_cleanup_loop()`: a daemon thread (started in `start()`) that periodically evicts validation-status entries older than the configured TTL, along with their associated events.

- `ProofVerifier` (src/proof_verifier.py):
  - Implements core proof checks:
    - `quick_verify(buf) -> ResponseValue`
    - `quick_verify_smell_test(buf) -> ResponseValue` (combined Quick/Smell enum)
    - `full_verify(buf) -> str` returning `'GREEN' | 'AMBER' | 'RED'`
  - Uses PyTorch, statistics, and optional MCA noise injection (`mca_set_enabled`, `mca_set_params`) during Full validation.

- `ModelVerifier` (src/model_verifier.py):
  - Exposes `validate(raw_message, claimed_difficulty, model_name, model_commit) -> (status, report)`, returning a status string and an audit report dict.
  - `AsyncValidator.validate_model()` parses `ModelValidation` fields from the request and calls `ModelVerifier.validate(...)` with the same signature.

- `ZmqSendBroker` (src/zmq_send_broker.py):
  - Single-owner thread that creates a ZMQ `PUSH` socket, applies send HWM and `LINGER=0`, and transmits payloads from a bounded queue.
  - `drop_on_backpressure=True` by default; when broker queue or ZMQ send buffer is full, items are dropped (see tests for backpressure behavior).

## Phase Semantics and Dependency Rules

- Quick and Quick_Smell:
  - `validate_quick` sets `status[hash]['quick']` and sends `Quick_OK` or `Quick_Fail`.
  - `validate_quick_smell` sets `status[hash]['smell']` and also sets `quick` based on the combined smell result:
    - `Quick_OK_Smell_OK` or `Quick_OK_Smell_Fail` → sets `quick=Quick_OK`
    - `Quick_Fail_Smell_Fail` → sets `quick=Quick_Fail`
  - Both record `prev_hash` (from `BlockValidation.prev_block_hash`) and add `hash` to `block_dependencies[prev_hash]`.

- Full:
  - A `Full` request enqueues a Quick mirror (to ensure pre-checks) and marks `hash ∈ full_requested`.
  - `process_full_validations` waits until `quick` is known; if `quick` failed (either from Quick or derived from Quick_Smell), and Full was requested, respond immediately with `Full_Red` and propagate failure to dependents.
  - Retry policy in `validate_full`:
    - `'RED'` → re-enqueue once (max 1 retry)
    - `'AMBER'` → re-enqueue twice (max 2 retries)
    - Only the final result sends a `Full_*` response and updates `status[hash]['full']`.

- Failure propagation:
  - When a block fails Quick (or `Quick_Fail_Smell_Fail`), `propagate_validation_failure` marks descendants’ `full` as `Full_Red`. Responses are sent only for those descendants that are in `full_requested`.

## Configuration

- Ports and host (defaults from `shared-utils/config/constants.py`):
  - `ZMQ_VERIFY_PULL_PORT` (default `6001`)
  - `ZMQ_VERIFY_PUSH_HOST` (default `0.0.0.0`)
  - `ZMQ_VERIFY_PUSH_PORT` (default `7001`)
- Validation retention (drives `_cleanup_loop`):
  - `VALIDATION_TTL_SECONDS` (default ~5 days)
  - `VALIDATION_CLEANUP_INTERVAL_SECONDS` (default 3600 seconds)
- Broker tuning (constructor args in `ZmqSendBroker`): `hwm`, `max_queue`, `drop_on_backpressure`, `io_threads`.

## Run Locally

Prerequisites:
- Python 3.10+
- `pyzmq`, `flatbuffers`, `numpy` (see `services/verification-api/requirements.txt`)
- For Full verification: PyTorch and CUDA-enabled environment as used by `proof_verifier.py`.
- PYTHONPATH must include `services/verification-api/src` and `shared-utils/fb-schemas` so imports like `utils.proof` resolve.

Steps:
- Set optional env vars for ports/host, e.g.:
  - `export ZMQ_VERIFY_PULL_PORT=6001`
  - `export ZMQ_VERIFY_PUSH_HOST=127.0.0.1`
  - `export ZMQ_VERIFY_PUSH_PORT=7001`
- Start the service:
  - `python services/verification-api/src/main.py`

Example client (Quick request):

```python
import zmq, flatbuffers, os
from utils.proof import BlockValidation, ValidationRequest, ValidationUnion, ValidationType, ValidationResponse

ctx = zmq.Context()
push = ctx.socket(zmq.PUSH)
push.connect(f"tcp://127.0.0.1:{os.environ.get('ZMQ_VERIFY_PULL_PORT', '6001')}")

def _vec_u8(bld, data):
    ValidationRequest.StartHashIdVector(bld, len(data))
    for b in reversed(data): bld.PrependUint8(b)
    return bld.EndVector()

bld = flatbuffers.Builder(256)
prev = _vec_u8(bld, b"\x00"*32)
blk_hash = _vec_u8(bld, os.urandom(32))
merkle = _vec_u8(bld, os.urandom(32))
powh = _vec_u8(bld, os.urandom(32))
BlockValidation.Start(bld)
BlockValidation.AddVersion(bld, 1)
BlockValidation.AddHash(bld, blk_hash)
BlockValidation.AddPrevBlockHash(bld, prev)
BlockValidation.AddMerkleRoot(bld, merkle)
BlockValidation.AddTimestamp(bld, 0)
BlockValidation.AddBits(bld, 0)
BlockValidation.AddNonce(bld, 0)
BlockValidation.AddPowBlobHash(bld, powh)
BlockValidation.AddAdjustedBits(bld, 0)
blk = BlockValidation.End(bld)

h = os.urandom(32)
hid = _vec_u8(bld, h)
ValidationRequest.Start(bld)
ValidationRequest.AddHashId(bld, hid)
ValidationRequest.AddValidationType(bld, ValidationType.ValidationType.Quick)
ValidationRequest.AddRequestType(bld, ValidationUnion.ValidationUnion.BlockValidation)
ValidationRequest.AddRequest(bld, blk)
req = ValidationRequest.End(bld)
bld.Finish(req)
push.send(bld.Output())

sink = ctx.socket(zmq.PULL)
sink.bind(f"tcp://*:{os.environ.get('ZMQ_VERIFY_PUSH_PORT', '7001')}")
payload = sink.recv()
r = ValidationResponse.ValidationResponse.GetRootAs(payload, 0)
print(r.EnumResponse())
```

## Tests

Location: `services/verification-api/tests`

- Unit (`tests/unit`):
  - Exercise queue routing, phase updates, event signaling, failure propagation, and retry logic in isolation.
  - Use fakes for ZMQ, a stub `ZmqSendBroker`, and monkeypatched `ProofVerifier`/`ModelVerifier` for deterministic outcomes.
- E2E (`tests/e2e`):
  - Spawn `AsyncValidator` with real ZMQ sockets on ephemeral ports and send/receive real FlatBuffers.
  - Cover quick, quick_smell cases, full after quick, short-circuit to `Full_Red` on quick failure, retry sequences, propagation, graceful shutdown, and broker backpressure.
- Helpers (`tests/helpers`):
  - FlatBuffers builders for BlockValidation and ModelValidation requests.
- Test harness (`tests/conftest.py`):
  - Sets up PYTHONPATH and aliases `utils.proof` to the local generated schema modules.
  - Provides lightweight stubs for `proof_verifier`, `zmq_send_broker`, and `torch` to keep tests fast.

Run tests:
- Quick runner:
  - `services/verification-api/tests/run_tests.sh unit` (unit tests with coverage)
  - `services/verification-api/tests/run_tests.sh e2e` (e2e tests)
  - `services/verification-api/tests/run_tests.sh all` (both)
  - Optional gate: `COV_FAIL_UNDER=70 services/verification-api/tests/run_tests.sh unit`
  - If your system pip is broken/unavailable: `SKIP_DEPS=1 services/verification-api/tests/run_tests.sh unit`
  - Recommended: use a clean venv with Python 3.10+ and install the minimal test deps listed below.

- Manual:
  - `pip install -r services/verification-api/requirements.txt`
  - `pip install pytest pytest-asyncio pytest-cov pytest-timeout pytest-mock pyzmq flatbuffers numpy`
  - `cd services/verification-api/tests && TEST_MODE=true python -m pytest -q unit --cov=../src --cov-branch`

## Docker

- `services/verification-api/generic.Dockerfile`: multi-stage build including Chiavdf and the `pfunpack` extension; final image uses an `nvidia/cuda` runtime base (CUDA 12.6 by default).
- `services/verification-api/cu120.Dockerfile`: CUDA 12.0 dev base, installs PyTorch and builds FlatBuffers Python files into the image.

Related compose files exist under `deployments/docker-compose/*` for multi-service integration.

## Operational Notes

- Logging: `src/main.py` configures standard logging and writes to `async_validator.log`.
- Backpressure: `ZmqSendBroker` drops payloads when its internal queue or ZMQ send buffer is full if `drop_on_backpressure=True`. Tests include a simplified backpressure scenario.
- GPU/CPU: Quick and Quick_Smell do not require GPU noise injection; Full enables MCA noise with parameters (`mca_set_params`) and expects PyTorch available.

## Implementation Notes

- State retention: `AsyncValidator.start()` launches `_cleanup_loop` as a daemon thread. It wakes every `VALIDATION_CLEANUP_INTERVAL_SECONDS` and evicts any `validation_status` entry whose `created_at`/`timestamp` is older than `VALIDATION_TTL_SECONDS`, also removing the matching `validation_events`.
- Model validation API: `ModelVerifier.validate(raw_message, claimed_difficulty, model_name, model_commit)` returns `(status, report)`, and `AsyncValidator.validate_model()` calls it with that exact signature.
- Schema import path: `utils.proof` exposes the generated FlatBuffers modules. Production images arrange this during build; in local dev/testing, ensure `PYTHONPATH` includes `shared-utils/fb-schemas` or use the test harness aliasing.

## File Map (selected)

- `src/main.py` — AsyncValidator (queues, workers, state, ZMQ I/O)
- `src/proof_verifier.py` — full/quick proof verification logic (heavy deps)
- `src/model_verifier.py` — model claim validation (`ModelVerifier.validate`)
- `src/zmq_send_broker.py` — single-threaded PUSH sender with backpressure policy
- `shared-utils/fb-schemas/*.fbs` — FlatBuffers schemas; generated Python modules used as `utils.proof.*`
- `services/verification-api/tests/*` — unit and e2e tests, builders, stubs
