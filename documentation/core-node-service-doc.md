# Core-Node Service Documentation

## Overview

The Core-Node service is the central coordinator of the blockchain GenAI system, managing blockchain consensus, peer communication, and orchestrating interactions between the verification and mining services. Built on Bitcoin-core foundations, it extends traditional blockchain functionality with GenAI model management and cryptographic proof coordination.

## Architecture

### Base Components
- **Bitcoin-Core Foundation**: Leverages battle-tested blockchain infrastructure
- **Custom Extensions**: GenAI model registry and proof validation
- **Communication Layer**: ZMQ-based async messaging system
- **API Gateway**: FastAPI for internal service communication

## External Connections

### P2P Bitcoin Network
- **Port**: Configurable per chain (tensor-test default `29241`, tensor-main default `39241`)
- **Protocol**: Bitcoin P2P protocol
- **Functions**:
  - Peer discovery and connection management
  - Block and transaction propagation
  - Consensus participation
  - Network synchronization

### Tor Integration (Optional)
- **Purpose**: IP anonymization and privacy protection
- **Configuration**:
  ```toml
  # torrc configuration
  proxy=127.0.0.1:9050
  proxy_randomize_credentials=1
  onlynet=onion
  ```
- **Benefits**:
  - Hides node IP from network peers
  - Resistant to network-level attacks
  - Optional for development/testing environments

### RPC Interface
- **Access**: Administrator only (not recommended for external exposure)
- **Authentication**: RPC username/password
- **Common Commands**:
  ```bash
  # Get blockchain info
  bitcoin-cli getblockchaininfo

  # List the registered GenAI models
  bitcoin-cli getmodelslist

  # Inspect a single model record by hash
  bitcoin-cli getmodelinfo <model_hash>
  ```
  Model registration is performed on-chain through the wallet RPCs
  (`createmodeldeposit` / `createmodelcommit`), not through a single
  "register" call.

## Internal APIs

### FastAPI Model Registry (Port 8050)

A FastAPI service (default port `8050`, overridable via `API_PORT`) exposes a
read-only HTTP view over the node's JSON-RPC interface. It exists so that the
miner and verification services can query model-registry and chain state over
HTTP without speaking Bitcoin RPC directly. All write operations to the
registry happen on-chain through model-registration transactions, not through
this API — there are no mutating HTTP endpoints.

#### Authentication and rate limiting

- Requests carry a bearer token: `Authorization: Bearer <api-key>`.
- Keys come from the `API_KEY` environment variable (comma-separated). When
  `REQUIRE_AUTH=true` (the default) and no keys are configured, the service
  generates a temporary key at startup and logs it.
- `GET /health` is unauthenticated; all other endpoints are authenticated and
  rate-limited per client.

#### Endpoints

**GET /**
- Service banner: name, version, `test_mode`/`auth_required` flags, and the
  list of available endpoints.

**GET /health**
- Unauthenticated liveness/readiness probe. Calls the node's
  `getblockchaininfo` RPC and reports `healthy`/`unhealthy` plus current block
  height.

**GET /api/v1/models**
- Returns the list of registered models, backed by the node's `getmodelslist`
  RPC. Pass `extended=true` for the full record set.
- Each entry follows the `ModelInfo` schema:
  ```json
  {
    "model_hash": "…",
    "model_name": "llama-7b-v2",
    "model_commit": "…",
    "difficulty": 0,
    "status": 0,
    "cid": null,
    "block_height": null,
    "deposit_txid": null,
    "owner_key_hash": null
  }
  ```
  (`model_hash`, `model_name`, `model_commit`, and `difficulty` are always
  present; the remaining fields are optional registry/deposit metadata.)

**GET /api/v1/models/{model_hash}**
- Returns the record for a single model, backed by the node's `getmodelinfo`
  RPC. `model_hash` must be a 64-character (32-byte) hex string; otherwise the
  service responds `400 Bad Request`.

**GET /api/v1/blockchaininfo**
- Pass-through to the node's `getblockchaininfo` RPC, used by the verification
  orchestrator to compute distance-to-verdict for pending model records.

**GET /api/v1/miner/metrics**
- Proxies the miner service's `/status` endpoint and returns token throughput
  (prompt/completion tokens per second), completion rate, cumulative totals,
  and active-request count.

## Communication Protocols

### ZMQ Architecture

The Core-Node uses ZeroMQ for high-performance asynchronous messaging with other services.

#### Port Allocation

The Core-Node talks to the verification service over a ZMQ PUSH/PULL pair. The
verification service binds a PULL socket to receive jobs Core pushes to it, and
pushes its results back to a PUSH socket Core listens on. The Core-side
defaults are configured via `VALIDATOR_PUSH_PORT` / `VALIDATOR_PULL_PORT`, and
the verification side via `ZMQ_VERIFY_PULL_PORT` / `ZMQ_VERIFY_PUSH_PORT`; all
of these are overridable through environment variables.

```yaml
# Verification Service Communication (default ports)
# Core PUSHes jobs to the verification service's PULL socket on 6001
# The verification service PUSHes results back on 7001
ZMQ_VERIFY_PULL_PORT: 6001  # Core -> Verification (requests)
ZMQ_VERIFY_PUSH_PORT: 7001  # Verification -> Core (responses)
```

#### Message Format

ZMQ payloads are serialized with FlatBuffers (both the miner and verification
services link the `flatbuffers` runtime and parse the wire frames as FlatBuffer
roots). The verification side exchanges `ValidationRequest` / `ValidationResponse`
messages (with the `BlockValidation`, `ModelValidation`, and union-typed
`ValidationUnion` / `ResponseValue` members), while the miner side parses a
`BlockHeader` frame. The exact field layouts live in the generated FlatBuffer
modules under each service's `proof` package — treat those generated types as
the source of truth for the wire format.

### Message Flow Patterns

#### Verification Flow
1. Core-Node detects a block requiring GenAI verification
2. Builds a `ValidationRequest` with the block/model data
3. Pushes the request to the verification service via ZMQ
4. Verification service processes asynchronously
5. Returns a `ValidationResponse` on its PUSH socket
6. Core-Node updates block validation status

#### Mining Flow
1. Core-Node creates a block template
2. Sends the block header to the miner service
3. Miner service works on proof generation
4. Returns the completed proof when a solution is found
5. Core-Node validates and broadcasts the new block

## Configuration

### Environment Variables
```bash
# Blockchain Configuration
CHAIN_NAME=tensor-test|tensor-main    # selects the chain (default tensor-test)
RPC_HOST=127.0.0.1                    # node RPC host the API server talks to
RPC_PORT=8332                         # node RPC port
COOKIE_FILE=/data/.cookie             # preferred RPC auth (cookie-based)
RPC_USER=<rpc-user>                   # optional fallback if no cookie file
RPC_PASS=<rpc-password>               # optional fallback if no cookie file
```

RPC authentication prefers the node's cookie file; `RPC_USER` / `RPC_PASS`
are only used as a fallback when the cookie file is unavailable.

## State Management

### Blockchain State
- Maintains full blockchain history
- Tracks GenAI proof validations
- Manages UTXO set with model-specific rules

### Model Registry State
- Persistent storage of approved models
- Maintains difficulty adjustment history

### Connection State
- Active peer connections
- Service health status (verification/miner)
- Message queue depths

## Security Considerations

### Network Security
1. **Firewall Rules**: Restrict RPC access to localhost
2. **API Authentication**: Use API keys for internal services
3. **TLS Encryption**: Enable for FastAPI endpoints
4. **Rate Limiting**: Implement on all external interfaces

### Cryptographic Security
1. **Model Hashing**: SHA-256 for model identification
2. **Proof Validation**: Multi-signature requirements
3. **Time-lock Mechanisms**: VDF integration for temporal security

### Operational Security
1. **Key Management**: Secure storage of private keys
2. **Audit Logging**: All administrative actions logged
3. **Access Control**: Role-based permissions
4. **Backup Strategy**: Regular state backups

## Monitoring and Metrics

### Key Metrics
- Block height and sync status
- Peer connection count
- Verification queue depth
- Mining job success rate
- Model registry size
- API request latency

### Health Checks
The unauthenticated `GET /health` endpoint calls the node's
`getblockchaininfo` RPC and returns a small status document, for example:

```json
{
  "status": "healthy",
  "test_mode": false,
  "auth_required": true,
  "blockchain_info": { "blocks": 0 }
}
```

On failure it returns `"status": "unhealthy"` with an `error` field instead.

## Troubleshooting

### Common Issues

1. **Sync Issues**
   - Check network connectivity
   - Verify peer connections
   - Review blockchain corruption

2. **ZMQ Connection Failures**
   - Validate port availability
   - Check firewall rules
   - Review socket configuration

3. **Model Registry Errors**
   - Verify storage permissions
   - Check model hash validity
   - Review difficulty parameters

### Debug Commands
```bash
# Check node status
docker exec core-node bitcoin-cli getblockchaininfo

# View ZMQ connections
docker exec core-node netstat -an | grep -E '6001|7001'

# Check model registry (bearer auth required)
curl -H "Authorization: Bearer <api-key>" http://localhost:8050/api/v1/models

# View logs
docker logs -f core-node
```

## Best Practices

1. **Deployment**
   - Use StatefulSet in Kubernetes for data persistence
   - Mount blockchain data on fast SSD storage
   - Implement proper backup procedures

2. **Configuration**
   - Start with testnet for development
   - Gradually increase verification requirements
   - Monitor resource usage patterns

3. **Security**
   - Regular security audits
   - Keep Bitcoin-core updated
   - Implement defense-in-depth

4. **Performance**
   - Tune ZMQ buffer sizes
   - Optimize database parameters
   - Use connection pooling

