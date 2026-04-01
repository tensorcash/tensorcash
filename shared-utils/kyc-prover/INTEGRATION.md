# KYC Prover Integration Guide

How the KYC proving service integrates with TensorCash core, wallets, and functional
tests. The prover generates Groth16 zero-knowledge proofs that a spender is a member of an
asset's compliance set, without revealing the spender's identity. Those proofs are carried
in the transaction witness and verified by consensus.

## Table of Contents

1. [Quick Start](#quick-start)
2. [Core Node Integration](#core-node-integration)
3. [Wallet Integration](#wallet-integration)
4. [Test Integration](#test-integration)
5. [Production Deployment](#production-deployment)

---

## Quick Start

### 1. Build and Setup

```bash
cd shared-utils/kyc-prover
chmod +x scripts/*.sh
./scripts/setup.sh
```

This generates:
- `kyc-prover` — server binary
- `proving_key.bin` — ~140 MB proving key
- `verification_key.bin` — ~2 KB verification key

### 2. Start Service

```bash
# Direct execution
./kyc-prover -port 8080

# Or with Docker
docker-compose up -d

# Check health
curl http://localhost:8080/health
```

### 3. Test Proof Generation

```bash
./scripts/test_proof.sh
```

---

## Core Node Integration

The core node needs proofs at the moment a KYC-gated asset is spent. There are two ways to
obtain them: call the prover over HTTP, or link the prover directly as a native library.

### Option 1: External Service

The core node calls the HTTP API of a separate `kyc-prover` process.

**Pros**:
- Circuit can be updated independently of the core node
- Prover can be scaled separately
- No core node build changes

**Cons**:
- Network latency
- Extra service to manage

**Illustrative client code** (sketch — not part of the shipped node):

```cpp
// Build a /prove request, post it, parse proof + public inputs.
std::pair<std::vector<uint8_t>, std::vector<uint8_t>>
GenerateKYCProof(const uint256& asset_id, const KYCWitness& witness)
{
    UniValue request(UniValue::VOBJ);
    request.pushKV("chain_separator", GetChainSeparator().GetHex());
    request.pushKV("asset_id", asset_id.GetHex());
    request.pushKV("compliance_root", GetComplianceRoot(asset_id).GetHex());
    request.pushKV("tfr_anchor", "0x" + std::string(64, '0')); // Or actual anchor

    UniValue witnessObj(UniValue::VOBJ);
    witnessObj.pushKV("secret", witness.secret.GetHex());
    witnessObj.pushKV("pubkey_hash", witness.pubkey_hash.GetHex());
    witnessObj.pushKV("country", witness.country);
    witnessObj.pushKV("age", witness.age);
    // ... add merkle proof, index, leaf hash
    request.pushKV("witness", witnessObj);

    std::string prover_url = gArgs.GetArg("-kycproverurl", "http://localhost:8080");
    HTTPClient client(prover_url + "/prove");
    UniValue response = client.Post(request);

    if (!response["success"].get_bool()) {
        throw std::runtime_error("Proof generation failed: " + response["error"].get_str());
    }

    auto proof  = ParseHex(response["proof_hex"].get_str().substr(2));         // Strip 0x
    auto inputs = ParseHex(response["public_inputs_hex"].get_str().substr(2));
    return {proof, inputs};
}
```

### Option 2: Embedded Library

The prover is compiled into a C-ABI shared library and linked directly into the node, so
proofs are generated in-process with no HTTP round-trip.

**Pros**:
- No external service
- Lower latency

**Cons**:
- More involved build (the library carries a Go runtime)

The bridge lives at `shared-utils/kyc-prover/cgo/`. `bridge.go` exports circuit-specific
proving entry points via CGO; the C-callable surface is declared in `zkprover.h`
(`libzkprover.h` is the cgo-generated counterpart). `make` in that directory produces
`libzkprover.so` (Linux), `libzkprover.dylib` (macOS), or `zkprover.dll` (Windows) built
with `-buildmode=c-shared`.

Exported functions include:

```c
// zkprover.h — circuit-agnostic C interface
typedef struct {
    unsigned char* proof_data;     // Raw proof bytes (gnark serialization)
    int            proof_len;
    unsigned char* public_inputs;  // Raw public input bytes (N x 32 bytes)
    int            public_inputs_len;
    char*          error_msg;      // NULL on success
} Groth16ProofResult;

// Plain-address circuit and HD (derived-key) circuits.
Groth16ProofResult Groth16_ProveKYC(const char* pkPath, const char* vkPath, const char* requestJSON);
Groth16ProofResult Groth16_ProveKYCHD(const char* pkPath, const char* vkPath, const char* requestJSON);
Groth16ProofResult Groth16_ProveKYCHDV1(const char* pkPath, const char* vkPath, const char* requestJSON);
char*              Groth16_Verify(const char* vkPath, const unsigned char* proofData, int proofLen,
                                  const unsigned char* publicInputs, int publicInputsLen); // NULL on success
void               Groth16_FreeResult(Groth16ProofResult* result);
```

The request JSON matches the HTTP `/prove` body (`chain_separator`, `asset_id`,
`compliance_root`, `tfr_anchor`, and a `witness` object). Link with `-L. -lzkprover` and
`#include "zkprover.h"`.

---

## Wallet Integration

Spending a KYC-gated asset requires two things from the wallet: the witness material that
proves compliance-set membership, and a way to embed the resulting proof into a transaction.

### The real RPC surface

The wallet RPCs that exist today center on the compliance Merkle tree (the asset issuer's
allow-list) and on generating the witness data the prover consumes:

- `generatecomplianceroot` — build/derive a compliance Merkle root from a member set.
- `getassetcomplianceroot` — read the current compliance root bound to an asset.
- `listassetcomplianceroots` — enumerate compliance roots known for assets.
- `updatecomplianceroot` — publish a new compliance root for an asset.
- `generatehdwitnessdata` — derive a child key from a master public key and assemble the
  full witness (child pubkey coordinates, derivation commitment, packed path/salt vectors,
  Merkle leaf hash + proof, and a `witness_data` object) needed to produce an HD proof.

`generatecomplianceroot` is defined in `services/core-node/bcore/src/wallet/rpc/compliance_root.cpp`;
`getassetcomplianceroot`, `listassetcomplianceroots`, and `updatecomplianceroot` in
`services/core-node/bcore/src/wallet/rpc/assets.cpp`; and `generatehdwitnessdata` in
`services/core-node/bcore/src/wallet/rpc/hd_witness.cpp`.

A typical flow is: obtain the witness with `generatehdwitnessdata`, hand it to the prover
(HTTP or embedded) to get a Groth16 proof, and place the proof and its public inputs into
the spending transaction's witness stack (after the signature). Consensus then verifies the
proof against the asset's compliance root.

### Illustrative spend/import RPCs

The two RPC sketches below — `createkycspend` and `importkycwitness` — are **illustrative
only**; they are not implemented in the node. They show one possible shape for a wallet that
stores witness material and assembles a KYC spend end-to-end on the user's behalf. Treat
them as a design sketch layered on top of the real RPCs above, not as a callable API.

```cpp
// ILLUSTRATIVE — not a shipped RPC.
// One possible witness record a wallet might persist per asset.
struct KYCWitness {
    uint256 secret;            // Private
    uint256 pubkey_hash;       // Hash(secret)
    uint16_t country;          // ISO 3166-1
    uint16_t age;              // Years
    std::vector<uint256> merkle_proof;  // Sibling hashes
    uint32_t merkle_index;     // Leaf position
    uint256 merkle_leaf_hash;  // Hash(pubkey || country || age)
};
```

```cpp
// ILLUSTRATIVE — not a shipped RPC.
// "createkycspend": fetch the stored witness, generate a proof, build and broadcast a
// KYC-compliant spend with the proof embedded in the first input's witness stack
// (proof goes AFTER the signature).
static RPCHelpMan createkycspend()
{
    return RPCHelpMan{"createkycspend",
        "Create a KYC-compliant asset spend transaction",
        {
            {"asset_id", RPCArg::Type::STR_HEX, RPCArg::Optional::NO, "Asset ID"},
            {"inputs",  RPCArg::Type::ARR, RPCArg::Optional::NO, "Transaction inputs"},
            {"outputs", RPCArg::Type::OBJ, RPCArg::Optional::NO, "Outputs {address: amount}"},
        },
        RPCResult{RPCResult::Type::STR_HEX, "txid", "Transaction ID"},
        RPCExamples{
            HelpExampleCli("createkycspend", "\"0x1234...\" '[{\"txid\":\"...\",\"vout\":0}]' '{\"addr1\":1.0}'")
        },
        [&](const RPCHelpMan& self, const JSONRPCRequest& request) -> UniValue
        {
            // Parse asset_id; load the stored KYC witness for it; generate the proof;
            // build inputs/outputs; push {proof, public_inputs} onto vin[0].scriptWitness
            // after the signature; sign; commit and broadcast.
            return /* txid */ NullUniValue;
        }
    };
}
```

```cpp
// ILLUSTRATIVE — not a shipped RPC.
// "importkycwitness": store witness data (e.g. obtained from a KYC provider) in the wallet.
static RPCHelpMan importkycwitness()
{
    return RPCHelpMan{"importkycwitness",
        "Import KYC witness data (from KYC provider)",
        {
            {"asset_id", RPCArg::Type::STR_HEX, RPCArg::Optional::NO, "Asset ID"},
            {"witness",  RPCArg::Type::OBJ, RPCArg::Optional::NO, "Witness data"},
        },
        RPCResult{RPCResult::Type::BOOL, "success", "Import successful"},
        RPCExamples{
            HelpExampleCli("importkycwitness", "\"0x1234...\" '{\"secret\":\"0x...\", ...}'")
        },
        [&](const RPCHelpMan& self, const JSONRPCRequest& request) -> UniValue
        {
            // Parse the witness fields; persist them in the wallet keyed by asset_id.
            return true;
        }
    };
}
```

---

## Test Integration

### Functional Tests

The functional-test framework manages the `kyc-prover` service for a test run:

```python
#!/usr/bin/env python3
from test_framework.test_framework import BitcoinTestFramework
from test_framework.kyc_prover import KYCProverService, WitnessData

class MyKYCTest(BitcoinTestFramework):
    def set_test_params(self):
        self.num_nodes = 1
        self.kyc_prover = KYCProverService(port=8080)

    def set_test_params_extra(self):
        # Start prover before test
        self.kyc_prover.start()

    def cleanup(self):
        # Stop prover after test
        super().cleanup()
        self.kyc_prover.stop()

    def run_test(self):
        witness = WitnessData(
            secret="0x1111...",
            pubkey_hash="0x2222...",
            country=840,
            age=25,
            merkle_proof=["0x00..."] * 8,
            merkle_index=42,
            merkle_leaf_hash="0x8888...",
        )

        proof, inputs = self.kyc_prover.prove(
            chain_separator="0x7bc914",
            asset_id="0x1234...",
            compliance_root="0xfedc...",
            tfr_anchor="0x0000...",
            witness=witness,
        )
        # Use proof in a transaction's witness stack.
```

### Running Tests

```bash
# The harness starts/stops the prover automatically.
./test/functional/feature_asset_zk_validation_real.py
```

---

## Production Deployment

### Security Checklist

- **Trusted Setup**: use a proper MPC ceremony for the proving/verification keys.
- **Key Storage**: keep the proving key in an HSM where possible.
- **TLS/HTTPS**: encrypt all prover API calls.
- **Authentication**: require API keys (or equivalent) for `/prove`.
- **Rate Limiting**: protect against DoS.
- **Logging**: audit proof requests, never witness data.
- **Witness Privacy**: never log secrets; keep witnesses encrypted in transit.

### Deployment Options

The prover handles witness data, so where it runs determines who can see that data.

#### Option A: Centralized Prover (KYC Provider Hosted)

```
┌─────────┐       HTTPS      ┌──────────────┐
│  Wallet │ ─────────────────→ │ KYC Provider │
└─────────┘  POST /prove      │   Prover     │
                               └──────────────┘
                                      │
                                      ↓
                                 HSM with PK
```

The provider controls the circuit and can update it without wallet changes, but sees the
witness data.

#### Option B: User-Hosted Prover

```
┌─────────┐       localhost    ┌──────────────┐
│  Wallet │ ─────────────────→ │  Local Prover│
└─────────┘  POST /prove       └──────────────┘
                                      │
                                      ↓
                                 Local PK file
```

The witness never leaves the user's machine; the user must run the prover.

#### Option C: Hybrid

1. The user downloads witness material from the KYC provider over HTTPS.
2. The user stores the witness in the local wallet (encrypted).
3. The user runs a local prover, or calls the provider's prover.
4. The proof is embedded in the transaction and broadcast to the network.

### Docker Compose

```yaml
version: '3.8'

services:
  kyc-prover:
    image: tensorcash/kyc-prover:latest
    restart: always
    ports:
      - "127.0.0.1:8080:8080"  # Only localhost
    volumes:
      - /secure/path/proving_key.bin:/app/proving_key.bin:ro
      - /secure/path/verification_key.bin:/app/verification_key.bin:ro
    environment:
      - LOG_LEVEL=warn  # Minimal logging
    deploy:
      resources:
        limits:
          cpus: '4'
          memory: 8G
    healthcheck:
      test: ["CMD", "wget", "--spider", "-q", "http://localhost:8080/health"]
      interval: 30s
      timeout: 5s
      retries: 3

  # Reverse proxy with auth/TLS
  nginx:
    image: nginx:alpine
    ports:
      - "443:443"
    volumes:
      - ./nginx.conf:/etc/nginx/nginx.conf:ro
      - /path/to/ssl:/etc/ssl:ro
    depends_on:
      - kyc-prover
```

### Monitoring

```bash
# Prometheus metrics endpoint
GET /metrics

# Example metrics:
kyc_prover_requests_total{status="success"} 1234
kyc_prover_requests_total{status="error"} 5
kyc_prover_proof_generation_duration_seconds_bucket{le="5"} 890
```

---

## Troubleshooting

### "Connection refused" during tests

```bash
# Check if prover is running
curl http://localhost:8080/health

# Start manually if needed
cd shared-utils/kyc-prover
./kyc-prover -port 8080
```

### "Proof generation failed: constraint not satisfied"

Check the witness data:
- Age meets the circuit's minimum
- Country matches the circuit's required value
- The Merkle proof has the expected number of siblings
- The Merkle leaf hash matches the circuit's leaf-hash definition

### "Module not found" in Go

```bash
cd shared-utils/kyc-prover
go mod download
go mod tidy
```

### Slow proof generation

Proofs take a few seconds each. To raise throughput, batch requests across a pool of prover
workers.

---

## References

- [README.md](./README.md) — full documentation
- [Circuit Spec](./internal/circuit/circuit.go) — compliance logic
- [Python Client](./pkg/client/python/kyc_prover.py) — test integration
- [ZK Implementation Codex](../../ZK_IMPLEMENTATION_CODEX.md) — consensus rules
</content>
</invoke>
