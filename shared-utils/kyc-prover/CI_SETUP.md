# CI for the KYC Prover

Automated testing for the KYC prover runs from a GitHub Actions workflow at
`.github/workflows/kyc-prover-test.yml`. This document describes what the
workflow validates, how the jobs relate, how to reproduce the checks locally,
and how to diagnose failures.

## What the workflow validates

The workflow exercises the prover toolchain end to end at the service layer:
building the Go binaries, generating real cryptographic golden vectors,
validating their structure, and proving over the HTTP service.

1. **Build** — Go 1.21 compilation of the two binaries:
   - `gentest` — the golden-vector generator (`cmd/gentest`)
   - `kyc-prover` — the HTTP proving server (`cmd/server`)

2. **Vector generation** — deterministic golden vectors: one valid case and
   four invalid cases. Each vector carries a BLS12-381 Groth16 proof (valid
   cases only) and the verifying-key hex.

3. **Proof generation** — real cryptographic proving: a valid witness produces a
   valid proof; invalid witnesses fail to produce a proof.

4. **Vector validation** — JSON integrity, presence of all required fields,
   non-empty VK hex, the rule that invalid vectors carry no proof, and that
   file sizes are within expected bounds.

5. **Service integration** — start the `kyc-prover` HTTP server, hit its health
   endpoint, and generate a proof through the Python client.

### Scope boundary

The workflow validates the prover infrastructure (the off-chain proving path) at
the service layer. It does not stand up a bcore node, so it does not exercise
on-chain transaction creation, mempool acceptance, or consensus validation of a
proof carried in a transaction. Those paths are covered by the bcore consensus
test suite (see `FUNCTIONAL_TEST_INTEGRATION.md`).

The consensus side is implemented: Groth16 verification runs in
`src/crypto/groth16.cpp` (`groth16::VerifyGroth16WithPolicy`) and is called from
`src/consensus/tx_verify.cpp` (the proof-verify path around line 1105), which
returns reject codes such as `zk-proof-bad`, `zk-epoch-stale`, and
`kyc-proof-not-hdv1`. The remaining gap for a fully automated regtest
end-to-end is ICU/TLV asset creation — the on-chain bootstrap of a
ZK-gated asset — not the proof-verification logic itself.

## Trigger conditions

- **Push** to `main`, when KYC files change.
- **Pull request** to `main`, when KYC files change.
- **Manual** dispatch from the GitHub Actions UI.

Monitored paths:

```yaml
paths:
  - 'shared-utils/kyc-prover/**'
  - 'services/core-node/bcore/test/functional/feature_asset_zk_validation_real.py'
  - 'services/core-node/bcore/test/test_framework/kyc_prover.py'
  - '.github/workflows/kyc-prover-test.yml'
```

## Jobs

### `build-and-test`

Builds the binaries and produces the golden vectors.

1. Check out the repository (no submodules required).
2. Set up Go 1.21.
3. Download dependencies.
4. Build `gentest`.
5. Build the `kyc-prover` server.
6. Generate golden vectors.
7. Validate vectors (VK hex present, valid JSON).
8. Test proof generation.
9. Upload artifacts.

Artifacts:
- `golden-vectors/` — 30-day retention.
- `kyc-prover`, `gentest` binaries — 7-day retention.

### `test-vectors`

Depends on `build-and-test`. Downloads the `golden-vectors` artifact and checks:
- JSON structure is well-formed.
- The valid vector carries a proof and non-empty public inputs.
- The invalid vectors carry no proof.
- File sizes match the serialization formats (proving key ~100 MB; the gnark
  verifying key is exactly 872 bytes; the custom C++ verifying-key hex is 578
  bytes; public inputs are 128 bytes).

### `python-client-test`

Depends on `build-and-test`. Validates the service path without a bcore node:

1. Download vectors and binaries.
2. Set up Python 3.10.
3. Start the `kyc-prover` service.
4. Wait for the health endpoint to come up.
5. Generate a proof through the Python client.
6. Stop the service.

## Local reproduction

The same checks run locally:

```bash
# 1. Build
cd shared-utils/kyc-prover
go mod download
go build -o gentest ./cmd/gentest
go build -o kyc-prover ./cmd/server

# 2. Generate vectors
./gentest -output vectors

# 3. Verify vector structure
jq empty vectors/golden_vectors.json
jq '.[] | {name, should_fail}' vectors/golden_vectors.json

# 4. Test proof generation
./scripts/test_proof.sh

# 5. Test the Python client
cd pkg/client/python
python3 kyc_prover.py
```

## Diagnosing failures

### Build failures (`build-and-test`)

- **Go compilation errors** — check `internal/circuit/*.go`, verify imports, and
  check the gnark version in `go.mod`.
- **Vector generation fails** — circuit constraint errors, invalid witness
  generation, or a generation timeout.
- **Missing VK hex** — confirm `setupResult.VKHex` is populated in
  `cmd/gentest/main.go`.

Reproduce:

```bash
cd shared-utils/kyc-prover
go build -v ./cmd/gentest
go build -v ./cmd/server
./gentest -output vectors
```

### Vector-validation failures (`test-vectors`)

- **Invalid JSON** — check the `gentest` output format and JSON marshaling.
- **Invalid vectors carry a proof** — this signals a circuit bug: an invalid
  witness must never produce a valid proof. Inspect `generateInvalidVector()`
  in `cmd/gentest/main.go`; generation should fail fatally in that case.
- **Missing fields** — verify every field of the `GoldenVector` struct is
  populated.

Inspect:

```bash
cd shared-utils/kyc-prover/vectors
jq empty golden_vectors.json
jq '.[] | {name, should_fail, has_proof: (.proof_hex != "")}' golden_vectors.json
```

### Service-path failures (`python-client-test`)

- **Service won't start** — binary permissions, a port already in use, or
  missing keys.
- **Client can't reach the service** — confirm the health endpoint comes up
  before the client runs.
- **Proof generation fails** — invalid golden vectors or a service timeout.

Reproduce:

```bash
cd shared-utils/kyc-prover
./kyc-prover -port 8080 -pk vectors/proving_key.bin -vk vectors/verification_key.bin &
curl http://localhost:8080/health
cd pkg/client/python
python3 kyc_prover.py
```

## Artifacts and logs

Generated artifacts (`golden-vectors`, `kyc-prover-binaries`) are attached to
each workflow run and can be downloaded from the run's Artifacts section in the
GitHub Actions UI. Per-step logs are available by expanding each job step.
