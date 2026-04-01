module kyc-prover

go 1.21

// HDv1 must stay on vanilla Groth16 artifacts for the on-chain C++ verifier.
// Upstream gnark v0.9.1 enables commitment-backed rangechecks for emulated
// arithmetic, which adds an implicit public witness variable and changes the
// proof/VK wire format. This fork disables that path (one function in
// std/rangecheck/rangecheck.go) so proofs stay 192 bytes and gamma_abc
// count matches the logical public input count.
replace github.com/consensys/gnark => github.com/tensorcash/gnark v0.9.1-plain-rangecheck

require (
	github.com/consensys/gnark v0.9.1
	github.com/consensys/gnark-crypto v0.12.2-0.20231013160410-1f65e75b6dfb
)

require (
	github.com/bits-and-blooms/bitset v1.8.0 // indirect
	github.com/blang/semver/v4 v4.0.0 // indirect
	github.com/consensys/bavard v0.1.13 // indirect
	github.com/davecgh/go-spew v1.1.1 // indirect
	github.com/fxamacker/cbor/v2 v2.5.0 // indirect
	github.com/google/pprof v0.0.0-20230817174616-7a8ec2ada47b // indirect
	github.com/mattn/go-colorable v0.1.13 // indirect
	github.com/mattn/go-isatty v0.0.19 // indirect
	github.com/mmcloughlin/addchain v0.4.0 // indirect
	github.com/pmezard/go-difflib v1.0.0 // indirect
	github.com/rs/zerolog v1.30.0 // indirect
	github.com/stretchr/testify v1.8.4 // indirect
	github.com/x448/float16 v0.8.4 // indirect
	golang.org/x/crypto v0.12.0 // indirect
	golang.org/x/exp v0.0.0-20230817173708-d852ddb80c63 // indirect
	golang.org/x/sync v0.3.0 // indirect
	golang.org/x/sys v0.11.0 // indirect
	gopkg.in/yaml.v3 v3.0.1 // indirect
	rsc.io/tmplfunc v0.0.3 // indirect
)
