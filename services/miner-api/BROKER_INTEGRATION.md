# Miner-Proxy Broker Integration

## Overview

This document describes the WebSocket worker client integration that allows the miner-proxy to connect to a Compute Broker as a worker node. This enables miners behind NAT to provide compute services through a public broker.

## Architecture

```
End Users → Compute Broker → WebSocket → [Worker Client] → Miner Proxy → vLLM Backend
         (public endpoint)            (behind NAT)
```

## Key Components

### 1. Worker Client (`worker_client.py`)
- WebSocket client that connects TO the broker (outbound from NAT)
- Handles job requests and forwards them to local miner-proxy
- Streams responses back through WebSocket
- Manages heartbeats and proof requests

### 2. Message Protocol
The worker implements the following WebSocket message types:

#### Outbound (Worker → Broker)
- `HELLO`: Initial registration with capabilities
- `CHALLENGE_RESP`: Response to authentication challenge
- `HEARTBEAT`: Periodic metrics update
- `CHUNK`: Streaming response data
- `END`: Job completion with usage stats
- `ERROR`: Job error notification
- `PROOF_RESULT`: Proof data response

#### Inbound (Broker → Worker)
- `ACK`: Registration acknowledgment
- `CHALLENGE`: Authentication challenge
- `START`: New job request
- `PROOF_REQUEST`: Request for completion proof

## Configuration

### Environment Variables

```bash
# Enable broker worker mode
WORKER_MODE=broker  # Options: standalone | broker

# Broker connection
BROKER_WS_URL=wss://broker.example.com/v1/ws
PROVIDER_JWT_TOKEN=eyJ...  # Provider JWT with compute:provide scope
X_WORKER_TOKEN=...         # Alternative: shared secret for dev mode
CHALLENGE_SECRET=...       # If broker requires challenge authentication

# Worker capabilities
WORKER_CAPACITY=4          # Number of concurrent jobs
COMPUTE_TYPE=nvidia-8.6    # GPU compute capability
GPU_MODEL=A100-80GB        # GPU model identifier
GPU_MEMORY_GB=80          # GPU memory in GB
WORKER_REGION=us-west-2   # Geographic region
MAX_CONTEXT_WINDOW=128000 # Maximum token context

# Local backend (still required)
TARGET_URL=http://localhost:8000  # Local vLLM endpoint
```

## Deployment Modes

### 1. Standalone Mode (Default)
```bash
WORKER_MODE=standalone
# Miner-proxy operates independently
# Direct HTTP access to /v1/chat/completions
```

### 2. Broker Mode
```bash
WORKER_MODE=broker
BROKER_WS_URL=wss://broker.example.com/v1/ws
PROVIDER_JWT_TOKEN=<your-jwt-token>
# Miner-proxy connects to broker via WebSocket
# Receives jobs from broker, no direct HTTP access needed
```

## Completion ID Flow

The system preserves completion IDs end-to-end:

1. vLLM generates a `completion_id` in its response
2. Worker captures this ID from upstream responses
3. Worker includes the ID in all `CHUNK` and `END` messages
4. Broker passes the same ID to end users
5. The ID is used for proof collection and auditing

## Proof Handling

The broker can request proofs for completed jobs:

1. Broker sends `PROOF_REQUEST` with `completion_id`
2. Worker fetches proof from local endpoint: `GET /v1/proof/{completion_id}`
3. Worker returns proof as base64 in `PROOF_RESULT`
4. Broker verifies proof through Verification Service

## Monitoring

### Status Endpoint
The `/status` endpoint now includes worker information when in broker mode:

```json
{
  "worker": {
    "worker_id": "uuid",
    "connected": true,
    "broker_url": "wss://broker.example.com/v1/ws",
    "active_jobs": 2,
    "running": true
  }
}
```

### Metrics
- WebSocket connection status
- Jobs received/completed/failed
- Reconnection attempts
- Active job count
- Heartbeat metrics (TPS, error rate)

## Testing

### Local Testing
```bash
# Start local broker (if available)
cd <compute-broker>
python -m compute_broker

# Start miner-proxy in broker mode
cd services/miner-api
WORKER_MODE=broker \
BROKER_WS_URL=ws://localhost:8003/v1/ws \
X_WORKER_TOKEN=dev-token \
python src/main.py

# Send test request to broker
curl -X POST http://localhost:8003/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"messages": [{"role": "user", "content": "Hello"}], "model": "Qwen/Qwen3-8B"}'
```

### Production Deployment
1. Obtain provider JWT token from marketplace
2. Configure environment variables
3. Deploy miner-proxy behind NAT
4. Verify WebSocket connection in logs
5. Monitor through broker admin endpoints

## Security Considerations

1. **JWT Authentication**: Use proper JWT tokens in production
2. **WebSocket TLS**: Always use `wss://` for production brokers
3. **Challenge Response**: Implement CHALLENGE_SECRET if required
4. **Local HTTP**: Miner-proxy can use HTTP locally (not exposed)

## Migration Path

1. **Stage 1**: Deploy with `WORKER_MODE=standalone` (no change)
2. **Stage 2**: Test broker connection with small traffic percentage
3. **Stage 3**: Switch to `WORKER_MODE=broker` for full broker integration
4. **Stage 4**: Disable direct HTTP access to miner-proxy

## Troubleshooting

### Connection Issues
- Check `BROKER_WS_URL` is accessible
- Verify JWT token or shared secret
- Check firewall allows outbound WebSocket
- Review logs for connection errors

### Job Processing Issues
- Verify local vLLM backend is running
- Check `TARGET_URL` configuration
- Monitor `/status` endpoint
- Review worker client logs

### Proof Issues
- Ensure proof cache is enabled
- Check proof collector is running
- Verify completion IDs match
- Monitor proof TTL expiration