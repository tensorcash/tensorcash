# Functional Test Integration Guide

How to use the KYC prover golden vectors to exercise compliance-proof
validation in the bcore (`services/core-node/bcore`) functional and unit test
suites.

## Overview

The KYC prover generates Groth16 (BLS12-381) compliance proofs from witness
data. bcore enforces those proofs in consensus: a transaction that spends a
KYC-gated asset must carry a valid proof, and the node re-verifies it during
transaction acceptance. The golden vectors in this package are a fixed set of
real proofs (and deliberately invalid witnesses) that let tests drive both the
Python functional suite and the C++ unit suite against the shipped consensus
code path.

Two layers consume the vectors:

- **C++ unit tests** (`src/test/groth16_golden_tests.cpp`) call the consensus
  verifier directly with real proofs and assert the pairing/policy outcome.
- **Python functional tests**
  (`test/functional/feature_asset_zk_validation_real.py`) generate proofs from
  a running kyc-prover service, build transactions, and broadcast them to a
  regtest node to observe acceptance or the consensus reject code.

## Consensus behavior the tests target

Compliance-proof verification lives in `CheckTxInputs`
(`src/consensus/tx_verify.cpp`). The verifier runs for every input that spends a
KYC-gated asset and calls `groth16::VerifyGroth16WithPolicy`
(`src/crypto/groth16.cpp`).

### How a proof reaches consensus

The proof and its public inputs are **not** carried in the spending input's
witness stack. They travel in a `ZK_PROOF_PAYLOAD` TLV (type `0x22`) inside the
`vExt` of a transaction output. Exactly one `ZK_PROOF_PAYLOAD` is required per
KYC asset spent in the transaction, matched by `asset_id`; a missing payload is
`zk-proof-missing` and a duplicate is `zk-proof-duplicate`. The spending input's
witness stack is still validated for layout (it must be a SegWit/Taproot witness
with the signature present), but it does not transport the proof itself.

### Byte formats

| Field | Size | Notes |
|-------|------|-------|
| Groth16 proof | 192 bytes | compressed `A‖B‖C` = 48 + 96 + 48 (`GROTH16_PROOF_SIZE`) |
| Field element (`Fr`) | 32 bytes | big-endian (`GROTH16_FR_SIZE`) |
| Public inputs (legacy) | 128 bytes | 4 × 32 |
| Public inputs (HDv1) | 192 bytes | 6 × 32 |

The parser accepts at most `GROTH16_MAX_PUBLIC_INPUTS` (8) field elements.

### Public-input schema

```
[0] chain/domain separator   (prevents cross-chain replay)
[1] asset_id commitment      (binds the proof to this asset)
[2] compliance root          (Merkle root the proof is checked against)
[3] tfr_commit               (transfer-reporting anchor, or zero if not required)
[4] output_key_high          (HDv1: upper 128 bits of the child x-only key)
[5] output_key_low           (HDv1: lower 128 bits of the child x-only key)
```

Indices `[4]`/`[5]` exist only in HDv1 (output-bound) proofs. Each holds a
128-bit half of the spent Taproot output's x-only key, left-padded into a
32-byte big-endian field element.

### Validation rules and reject codes

Consensus checks the public inputs field-by-field and then runs the pairing
check. The reject codes a test can assert on:

| Reject code | Condition |
|-------------|-----------|
| `kyc-proof-not-hdv1` | fewer than 6 public inputs — legacy/unbound proofs are rejected on the output-bound interface |
| `zk-chain-mismatch` | `[0]` does not equal this network's chain separator |
| `zk-asset-mismatch` | `[1]` does not equal the spent asset id |
| `zk-root-not-set` / `zk-root-mismatch` | issuer has no committed compliance root, or `[2]` matches neither the active root nor a still-fresh historical root |
| `zk-anchor-mismatch` / `tfr-anchor-mismatch` | `[3]` does not match the on-chain TFR anchor commitment |
| `kyc-proof-output-mismatch` / `kyc-proof-output-not-taproot` | the HDv1 output-key binding does not match every spent input's Taproot x-only key |
| `zk-epoch-stale` | the compliance root is older than the asset's `max_root_age` window (`VerifyGroth16WithPolicy` → `RootTooOld`) |
| `zk-proof-bad` | malformed proof or failed pairing (`InvalidProofFormat` / `PairingFailed`) |
| `zk-vk-missing` / `zk-vk-invalid` | the issuer's verifying key is absent from the VK cache or fails structural checks |

All of these surface as `sendrawtransaction` errors with RPC error code `-26`.

### Root-age rule

`max_root_age` is a per-asset policy value. When the proof's compliance root is
the active root, freshness is satisfied implicitly. When it is a historical root
(the issuer rotated since), consensus accepts it only while
`current_height − root_activation_height ≤ max_root_age`, scanning the root
history ring buffer and verifying the matched root under its own verifying key.
Once a historical root ages past the window, spends under it fail with
`zk-epoch-stale`.

### Output-key binding (HDv1)

For HDv1 proofs, consensus extracts the x-only key from every spent prevout
(`OP_1 OP_PUSHBYTES_32 <32-byte key>`), splits it into a high/low 128-bit pair,
and byte-compares against public inputs `[4]`/`[5]`. This binds the proof to the
exact Taproot output being spent, preventing proof transfer or reuse across
addresses. A multi-input same-asset spend must have all inputs under the same
x-only key, since one proof covers one key.

## Golden vectors

### Location

```
shared-utils/kyc-prover/vectors/golden_vectors.json
```

The file is a JSON array of vector objects. Each object has the fields:

```json
{
  "name": "valid",
  "witness": {
    "secret": "0x...",
    "pubkey_hash": "0x...",
    "country": 840,
    "age": 25,
    "merkle_proof": ["0x...", "..."],
    "merkle_index": 42,
    "merkle_leaf_hash": "0x...",
    "chain_separator": "0x7bc914",
    "asset_id": "0x...",
    "compliance_root": "0x...",
    "tfr_anchor": "0x0"
  },
  "proof_hex": "0x...",          // 192 bytes
  "public_inputs_hex": "0x...",  // 128 bytes (4 × 32)
  "vk_hex": "0x...",             // verifying key
  "should_fail": false
}
```

### Vectors provided

| Name | Property |
|------|----------|
| `valid` | all constraints satisfied; proof verifies |
| `invalid_secret` | `MiMC(secret) != pubkey_hash` |
| `invalid_age` | age below the minimum |
| `invalid_country` | country code not in the allowed set |
| `invalid_merkle` | corrupted Merkle sibling |

Invalid vectors carry no proof — the prover cannot satisfy the circuit — so they
test that no proof can be generated for a non-compliant witness.

## C++ unit tests against real proofs

`src/test/groth16_golden_tests.cpp` loads the golden vectors and exercises the
consensus verifier directly, complementing the mock suite
(`src/test/groth16_tests.cpp`) which uses structurally-valid but
cryptographically-meaningless proofs to cover error paths.

Loading helper: `src/test/util/golden_vector_loader.h`.

```cpp
#include <test/util/golden_vector_loader.h>

if (!golden_vectors::GoldenVectorsAvailable()) {
    BOOST_TEST_MESSAGE("Skipping: golden vectors not found");
    return;
}

auto golden_opt = golden_vectors::LoadGoldenVector("valid");
BOOST_REQUIRE(golden_opt.has_value());
const auto& golden = golden_opt.value();

auto result = groth16::VerifyGroth16WithPolicy(
    std::span<const unsigned char>(golden.proof_bytes),
    std::span<const unsigned char>(golden.public_inputs_bytes),
    std::span<const unsigned char>(golden.vk_bytes),
    ctx);

BOOST_CHECK(result == groth16::VerifyError::OK);
```

The suite covers: valid-proof verification, corrupted-proof rejection (flipped
bytes), wrong public inputs (pairing failure), cross-VK rejection (proof under a
different verifying key), `max_root_age` enforcement, TFR-anchor enforcement,
and confirmation that invalid witnesses produce no proof. Each case skips
gracefully when the vectors are absent.

Build and run:

```bash
cd services/core-node/bcore
cmake -B build -DBUILD_TESTS=ON
cmake --build build -j"$(nproc)"

# all golden-vector tests
./build/src/test_bitcoin --run_test=groth16_golden_tests

# a single case
./build/src/test_bitcoin --run_test=groth16_golden_tests/groth16_golden_valid_proof
```

## Python functional test

`test/functional/feature_asset_zk_validation_real.py` runs the full path:
generate a real proof, build a funding transaction that registers the issuer's
verifying key, build a spend transaction carrying the proof, broadcast to a
regtest node, and assert acceptance or the expected reject code.

```python
from test_framework.kyc_prover import load_golden_vector

def setup_network(self):
    super().setup_network()
    self.ensure_golden_vectors()   # auto-generates vectors/golden_vectors.json if missing
    self.kyc_prover.start()        # starts the kyc-prover service

def test_valid_proof_acceptance(self):
    witness, golden = self.create_valid_witness("valid")

    proof_bytes, inputs_bytes = self.kyc_prover.prove(
        chain_separator=witness["chain_separator"],
        asset_id=witness["asset_id"],
        compliance_root=witness["compliance_root"],
        tfr_anchor=witness["tfr_anchor"],
        witness=witness,
    )

    asset_id, icu_txid, _, asset_txid, asset_vout = self.create_kyc_asset_with_vk(vk_hex)
    tx_hex = self.create_kyc_spend_tx(proof_bytes, inputs_bytes, asset_id, asset_txid, asset_vout)

    txid = self.nodes[0].sendrawtransaction(tx_hex)
    assert txid in self.nodes[0].getrawmempool()
```

`load_golden_vector(name)` returns a single vector dict
(`{name, witness, proof_hex, public_inputs_hex, vk_hex, should_fail}`) for the
named entry.

Run it:

```bash
cd services/core-node/bcore
./test/functional/feature_asset_zk_validation_real.py
# or via the runner
./test/functional/test_runner.py feature_asset_zk_validation_real
```

Representative assertions, mapping directly to the consensus reject codes above:

```python
# valid proof accepted into the mempool
txid = node.sendrawtransaction(tx_hex)
assert txid in node.getrawmempool()

# corrupted proof or wrong asset → pairing/field check fails
assert_raises_rpc_error(-26, "zk-proof-bad", node.sendrawtransaction, tx_hex)

# compliance root older than max_root_age
assert_raises_rpc_error(-26, "zk-epoch-stale", node.sendrawtransaction, tx_hex)

# legacy 4-input proof on the output-bound interface
assert_raises_rpc_error(-26, "kyc-proof-not-hdv1", node.sendrawtransaction, tx_hex)
```

## Generating proofs without a node

To validate the prover and circuit in isolation — independent of consensus —
generate a proof and verify it against the service's local verify endpoint:

```python
def test_proof_generation_only(self):
    witness, golden = self.create_valid_witness("valid")
    proof_bytes, inputs_bytes = self.kyc_prover.prove(...)

    result = requests.post("http://localhost:8080/verify", json={
        "proof_hex": proof_bytes.hex(),
        "public_inputs_hex": inputs_bytes.hex(),
        "vk_hex": golden["vk_hex"],
    })
    assert result.json()["valid"] is True
```

## Adding a test case

1. **Add a witness type** in
   `shared-utils/kyc-prover/internal/circuit/witness_gen.go`:

   ```go
   const (
       InvalidSecret InvalidWitnessType = iota
       InvalidAge
       InvalidCountry
       InvalidMerkleProof
       InvalidTfrAnchor  // new
   )
   ```

2. **Produce the invalid witness**:

   ```go
   case InvalidTfrAnchor:
       valid.TfrAnchor = "0xdeadbeef"
   ```

3. **Emit it from gentest** in `cmd/gentest/main.go`:

   ```go
   invalidTfr, err := generateInvalidVector(prover, "invalid_tfr", "seed", circuit.InvalidTfrAnchor)
   vectors = append(vectors, invalidTfr)
   ```

4. **Add the test** (the prover should refuse to generate a proof):

   ```python
   def test_invalid_tfr_anchor(self):
       witness, golden = self.create_valid_witness("invalid_tfr")
       with self.assertRaises(Exception):
           self.kyc_prover.prove(...)
   ```

5. **Regenerate the vectors**:

   ```bash
   cd shared-utils/kyc-prover
   ./scripts/generate_vectors.sh
   ```

## CI coverage

CI builds the prover and exercises the vector pipeline end to end without a
bcore node:

- Go unit tests (witness generation, hex encoding)
- build `gentest` + kyc-prover and generate the golden vectors
- proof generation, proof verification, invalid-witness rejection, cross-VK
  rejection, and prover benchmarks
- vector-structure validation: JSON shape, verifying-key presence, valid
  vectors have proofs, invalid vectors have none
- Python client integration

Transaction broadcast, mempool acceptance, and block mining with proofs require
a bcore node and run in the functional suite (above), not in the prover CI job.
