# TensorCash KYC Prover

**Reference implementation of a KYC proving service for TensorCash privacy-preserving compliance.**

This service generates BLS12-381 Groth16 zero-knowledge proofs that enable compliant asset transfers without revealing holder identity or KYC attributes on-chain.

## What This Is

- **Reference Circuit**: A minimal compliant KYC proof implementation.
- **HTTP API**: REST endpoints for proof generation and local verification.
- **Test Integration**: Used by TensorCash functional tests.
- **Blueprint for KYC Providers**: An example to adapt for real-world deployments.

The reference circuit uses an in-process trusted setup (`groth16.Setup()`), is single-threaded, and covers proof generation only — not identity verification. A production deployment replaces the setup with an MPC ceremony, hardens key storage, and customizes the compliance rules (see [Blueprint for KYC Providers](#blueprint-for-kyc-providers)).

## Quick Start

### 1. Setup (One-Time)

```bash
cd shared-utils/kyc-prover
chmod +x scripts/setup.sh
./scripts/setup.sh
```

This will:
- Download gnark dependencies (~2GB)
- Build the server binary
- Generate proving key (~140 MB) and verification key (~2 KB)

**Output**:
```
proving_key.bin
verification_key.bin
kyc-prover
```

### 2. Start Service

```bash
./kyc-prover -port 8080
```

Or with Docker:
```bash
docker-compose up -d
```

### 3. Test

```bash
curl http://localhost:8080/health
# {"status":"ok"}

chmod +x scripts/test_proof.sh
./scripts/test_proof.sh
```

## API Reference

### POST /prove

Generate a ZK proof.

**Request**:
```json
{
  "chain_separator": "0x00...7bc914",
  "asset_id": "0x1234...abcdef",
  "compliance_root": "0xfedc...543210",
  "tfr_anchor": "0x0000...000000",
  "witness": {
    "secret": "0x1111...1111",
    "pubkey_hash": "0x2222...2222",
    "country": 840,
    "age": 25,
    "merkle_proof": ["0x00...00", ...],
    "merkle_index": 42,
    "merkle_leaf_hash": "0x8888...8888"
  }
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

**Proof Format**:
- `proof_hex`: 192 bytes (48 + 96 + 48) - Groth16 proof (A, B, C)
- `public_inputs_hex`: 128 bytes (4 × 32) - Public inputs in order

### POST /verify

Verify a proof locally (for testing). When a verification key is supplied in `vk_hex`, it is honored; otherwise the service's own verification key is used.

**Request**:
```json
{
  "proof_hex": "0x...",
  "public_inputs_hex": "0x...",
  "vk_hex": "0x..."
}
```

**Response**:
```json
{
  "valid": true
}
```

### GET /health

Health check endpoint.

**Response**:
```json
{
  "status": "ok"
}
```

## Circuit Specification

### Public Inputs (On-Chain)

| Index | Name | Description |
|-------|------|-------------|
| 0 | `chain_separator` | Prevents cross-chain replay attacks |
| 1 | `asset_id` | Binds proof to specific asset |
| 2 | `compliance_root` | Merkle root of approved holder list |
| 3 | `tfr_anchor` | Transfer reporting commitment (optional) |

### Private Inputs (Off-Chain)

| Field | Description |
|-------|-------------|
| `secret` | Holder's secret preimage |
| `pubkey_hash` | Hash of secret (proves identity) |
| `country` | ISO 3166-1 numeric country code |
| `age` | Holder's age in years |
| `merkle_proof` | 8-level Merkle proof (holder in whitelist) |
| `merkle_index` | Leaf position in tree |
| `merkle_leaf_hash` | Hash(pubkey_hash ‖ country ‖ age) |

### Constraints

The reference circuit (`internal/circuit/circuit.go`) enforces:

1. **Knowledge of Secret**: `MiMC(secret) == pubkey_hash`
2. **Compliance Rules**: `country == 840 && age >= 18` (example rule set)
3. **Leaf Binding**: `MiMC(pubkey_hash ‖ country ‖ age) == merkle_leaf_hash`
4. **Whitelist Membership**: the 8-level Merkle proof verifies the leaf against `compliance_root`

All in-circuit hashing uses MiMC over the BLS12-381 scalar field.

## Usage in Tests

### Python Client

```python
from kyc_prover import KYCProverClient, WitnessData

client = KYCProverClient("http://localhost:8080")

# Create witness
witness = WitnessData(
    secret="0x1111...",
    pubkey_hash="0x2222...",
    country=840,
    age=25,
    merkle_proof=["0x00...", ...],
    merkle_index=42,
    merkle_leaf_hash="0x8888..."
)

# Generate proof
proof_hex, inputs_hex = client.prove(
    chain_separator="0x7bc914",
    asset_id="0x1234...",
    compliance_root="0xfedc...",
    tfr_anchor="0x0000...",
    witness=witness
)

# Use in transaction
tx.vin[0].scriptWitness.stack.append(bytes.fromhex(proof_hex[2:]))
tx.vin[0].scriptWitness.stack.append(bytes.fromhex(inputs_hex[2:]))
```

### In Functional Tests

The functional-test framework exposes a `KYCProverService` wrapper (`test_framework/kyc_prover.py`) that starts the prover process and proxies its `/prove` endpoint. The sketch below is illustrative of the shape of a test that drives the service:

```python
from test_framework.kyc_prover import KYCProverService

# Start the prover service, generate a proof, attach it to a transaction
prover = KYCProverService()
prover.start()

proof, inputs = prover.prove(
    chain_separator=chain_separator,
    asset_id=asset_id,
    compliance_root=compliance_root,
    tfr_anchor=tfr_anchor,
    witness=witness,
)

# Build a transaction carrying the proof on its input witness stack, then
# submit it with sendrawtransaction and assert the expected accept/reject.
```

## Integration with TensorCash Core

A wallet obtains a proof by calling the prover service's `/prove` endpoint, then places the `proof_hex` and `public_inputs_hex` byte strings onto the spending input's witness stack. The node validates the proof in consensus during transaction acceptance.

On the node side, the ZK proof is verified by `groth16::VerifyGroth16WithPolicy` (`crypto/groth16.cpp`), called from `consensus/tx_verify.cpp`. Invalid or stale proofs are rejected with consensus reject codes `zk-proof-bad`, `zk-epoch-stale`, and `kyc-proof-not-hdv1`.

The compliance Merkle root that a proof commits to is produced and managed via the wallet RPCs `generatecomplianceroot`, `getassetcomplianceroot`, `listassetcomplianceroots`, and `updatecomplianceroot`. HD witness material is produced with `generatehdwitnessdata`.

### Illustrative RPC Wiring

> The `createkycspend` RPC below is **illustrative only** — it sketches how a KYC-aware spend RPC could marshal a witness, call the prover, and attach the proof to a transaction. It is **not** a real RPC in the node. The shipped compliance surface is the `*complianceroot` / `generatehdwitnessdata` RPCs listed above.

```cpp
// Illustrative — not a shipped RPC
static RPCHelpMan createkycspend()
{
    return RPCHelpMan{"createkycspend",
        "Create a KYC-compliant asset spend transaction",
        {
            {"asset_id", RPCArg::Type::STR_HEX, RPCArg::Optional::NO, "Asset ID"},
            {"amount", RPCArg::Type::AMOUNT, RPCArg::Optional::NO, "Amount to send"},
            {"address", RPCArg::Type::STR, RPCArg::Optional::NO, "Destination address"},
        },
        RPCResult{RPCResult::Type::STR_HEX, "txid", "Transaction ID"},
        [&](const RPCHelpMan& self, const JSONRPCRequest& request) -> UniValue
        {
            // Get witness from wallet
            auto witness = pwallet->GetKYCWitness(asset_id);

            // Generate proof via prover service
            auto [proof, inputs] = GenerateKYCProof(asset_id, witness);

            // Build transaction
            CMutableTransaction tx;
            tx.vin[0].scriptWitness.stack.push_back(proof);
            tx.vin[0].scriptWitness.stack.push_back(inputs);

            return tx.GetHash().GetHex();
        }
    };
}
```

## Blueprint for KYC Providers

A production KYC provider adapts this reference along these axes:

- **Replace Trusted Setup**: use an MPC ceremony rather than the in-process `groth16.Setup()`.
- **Secure Key Storage**: keep the proving key in an HSM or encrypted storage.
- **Customize Circuit**: change the compliance rules in `internal/circuit/circuit.go`.
- **Add Authentication**: protect `/prove` (API keys, OAuth).
- **Rate Limiting**: proof generation is expensive; throttle requests.
- **Monitoring**: add metrics, logging, and alerting.
- **Scalability**: add proof batching and worker pools.
- **Witness Privacy**: encrypt witness data in transit and at rest.

### Custom Circuit Example

```go
// Custom circuit with provider-specific rules
type MyKYCCircuit struct {
    TensorCashKYCCircuit

    // Add custom fields
    AccreditationLevel frontend.Variable `gnark:",secret"`
    JurisdictionFlags  frontend.Variable `gnark:",secret"`
}

func (c *MyKYCCircuit) Define(api frontend.API) error {
    // Call base circuit
    if err := c.TensorCashKYCCircuit.Define(api); err != nil {
        return err
    }

    // Add custom constraints
    api.AssertIsLessOrEqual(2, c.AccreditationLevel) // Accredited investor
    api.AssertIsEqual(c.JurisdictionFlags & 0x01, 1) // US jurisdiction

    return nil
}
```

### Security Properties

**Trusted Setup**: the reference uses `groth16.Setup()`, which produces toxic waste. For production, use gnark's [Phase 2 Ceremony](https://docs.gnark.consensys.net/HowTo/write/ceremony), or a SNARK-friendly universal setup (PLONK, Marlin).

**Witness Privacy**: serve `/prove` over TLS, never log witness data, and use ephemeral witness storage (delete after proof generation).

**Proof Binding**: Groth16 proofs are non-malleable. Bind each proof tightly to its transaction by ensuring the public inputs match the transaction exactly, and require the compliance root to be recent (the node enforces a root-age bound; stale proofs reject with `zk-epoch-stale`).

## Performance

### Benchmarks (Apple M1, 8 cores)

| Operation | Time | Notes |
|-----------|------|-------|
| Setup | ~15s | One-time |
| Prove | ~3-5s | Per transaction |
| Verify | ~50ms | Node validation |

### Optimization

- **Parallel Proving**: run multiple prover workers.
- **Caching**: cache compiled circuits.
- **Batching**: batch multiple proof requests (gnark supports this).

## Files

```
kyc-prover/
├── cmd/server/main.go              # HTTP server entry point
├── internal/
│   ├── circuit/
│   │   ├── circuit.go              # Circuit definition
│   │   ├── setup.go                # Trusted setup
│   │   ├── prove.go                # Proof generation
│   │   └── types.go                # API types
│   └── api/
│       └── server.go               # HTTP handlers
├── pkg/client/
│   └── python/
│       └── kyc_prover.py           # Python client
├── scripts/
│   ├── setup.sh                    # Initial setup
│   ├── generate_vectors.sh         # Golden test-vector generation
│   └── test_proof.sh               # Test script
├── Dockerfile                      # Container build
├── docker-compose.yml              # Service orchestration
└── README.md                       # This file
```

## Test Vectors

Deterministic golden test vectors are produced by:

```bash
./scripts/generate_vectors.sh
```

This writes `vectors/golden_vectors.json` with valid test proofs, used by the cross-language and functional tests.

## Troubleshooting

### "Go not found"
```bash
wget https://go.dev/dl/go1.21.6.linux-amd64.tar.gz
sudo tar -C /usr/local -xzf go1.21.6.linux-amd64.tar.gz
export PATH=$PATH:/usr/local/go/bin
```

### "Module not found"
```bash
cd shared-utils/kyc-prover
go mod download
go mod tidy
```

### "Proof generation failed: constraint not satisfied"
- Check witness values match circuit constraints
- Ensure Merkle proof is valid (8 siblings)
- Verify age >= 18 and country == 840

### "Connection refused"
```bash
# Check service is running
curl http://localhost:8080/health

# Or start it
./kyc-prover -port 8080
```

## References

- [gnark Documentation](https://docs.gnark.consensys.net/)
- [Groth16 Paper](https://eprint.iacr.org/2016/260.pdf)
- [BLS12-381 Spec](https://github.com/supranational/blst)
- [TensorCash ZK Implementation](../../ZK_IMPLEMENTATION_CODEX.md)

## License

MIT License - See TensorCash main repository for details.
