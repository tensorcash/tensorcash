# KYC Prover - Quick Start

**Generate real BLS12-381 Groth16 proofs for TensorCash KYC testing.**

## 1. Setup (One Time)

```bash
cd shared-utils/kyc-prover
./scripts/setup.sh
```

**Output**:
- `kyc-prover` - Server binary
- `proving_key.bin` - ~140 MB
- `verification_key.bin` - ~2 KB

**Time**: ~1 minute (downloads ~2GB dependencies)

## 2. Generate Golden Vectors (One Time)

```bash
./scripts/generate_vectors.sh
```

**Output**: `vectors/golden_vectors.json` with valid test proofs

**Time**: ~30 seconds

## 3. Start Service

```bash
./kyc-prover -port 8080
```

Or with Docker:
```bash
docker-compose up -d
```

## 4. Test

```bash
# Health check
curl http://localhost:8080/health

# Generate test proof
./scripts/test_proof.sh
```

## Usage in Tests

```python
from test_framework.kyc_prover import KYCProverService, WitnessData

# Start service
prover = KYCProverService(port=8080)
prover.start()

# Generate proof
witness = WitnessData(
    secret="0x1111...",
    pubkey_hash="0x2222...",
    country=840,
    age=25,
    merkle_proof=["0x00..."] * 8,
    merkle_index=42,
    merkle_leaf_hash="0x8888..."
)

proof_bytes, inputs_bytes = prover.prove(
    chain_separator="0x7bc914",
    asset_id="0x1234...",
    compliance_root="0xfedc...",
    tfr_anchor="0x0000...",
    witness=witness
)

# Use in transaction
tx.vin[0].scriptWitness.stack.append(proof_bytes)
tx.vin[0].scriptWitness.stack.append(inputs_bytes)
```

## Run Functional Tests

```bash
cd services/core-node/bcore
./test/functional/feature_asset_zk_validation_real.py
```

Service starts/stops automatically.

## What This Generates

| Output | Size | Description |
|--------|------|-------------|
| `proof_hex` | 192 bytes | Groth16 proof (A, B, C points) |
| `public_inputs_hex` | 128 bytes | 4 field elements (32 bytes each) |

**Public Inputs** (in order):
1. `chain_separator` - Anti-replay
2. `asset_id` - Asset binding
3. `compliance_root` - Merkle root of whitelist
4. `tfr_anchor` - Transfer reporting commitment

## API Endpoints

### POST /prove
Generate ZK proof

**Request**:
```json
{
  "chain_separator": "0x...",
  "asset_id": "0x...",
  "compliance_root": "0x...",
  "tfr_anchor": "0x...",
  "witness": { ... }
}
```

**Response**:
```json
{
  "proof_hex": "0x...",
  "public_inputs_hex": "0x...",
  "success": true
}
```

### POST /verify
Verify proof locally (testing only)

### GET /health
Health check

## Circuit Constraints

1. **Knowledge**: `MiMC(secret) == pubkey_hash`
2. **Compliance**: `country == 840 && age >= 18`
3. **Whitelist**: Merkle proof → `compliance_root`
4. **Binding**: `asset_id != 0 && chain_separator != 0`

## Troubleshooting

**Go not found?**
```bash
wget https://go.dev/dl/go1.21.6.linux-amd64.tar.gz
sudo tar -C /usr/local -xzf go1.21.6.linux-amd64.tar.gz
export PATH=$PATH:/usr/local/go/bin
```

**Module errors?**
```bash
go mod download && go mod tidy
```

**Service won't start?**
```bash
# Check if keys exist
ls -lh proving_key.bin verification_key.bin

# Regenerate if needed
./kyc-prover -setup
```

## Files

```
kyc-prover/
├── QUICKSTART.md          # This file
├── README.md              # Full documentation
├── INTEGRATION.md         # Wallet/RPC integration guide
├── scripts/setup.sh       # One-command setup
├── scripts/test_proof.sh  # Test script
├── cmd/server/main.go     # Server entry point
├── internal/circuit/      # Circuit definition
├── pkg/client/python/     # Python client for tests
└── docker-compose.yml     # Docker setup
```

## More

- Setup: `./scripts/setup.sh`
- Start: `./kyc-prover -port 8080`
- Test: `./scripts/test_proof.sh`
- Full reference and production deployment: [README.md](./README.md)
- Wallet/RPC integration: [INTEGRATION.md](./INTEGRATION.md)

---

**Real BLS12-381 proofs ready for testing.**
