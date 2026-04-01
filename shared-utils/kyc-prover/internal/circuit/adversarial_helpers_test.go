// SPDX-License-Identifier: Apache-2.0
package circuit

// Adversarial soundness test battery for the HDv1 KYC circuit.
//
// GOAL: try to make a FALSE statement satisfy the constraint system. Every test
// here drives test.IsSolved, which runs ONLY the R1CS solver against a witness we
// hand-craft. No Groth16 proving, no trusted setup, no toxic waste — so these
// tests isolate exactly the thing we care about: does the *circuit* (and the
// hand-patched gnark fork it compiles against) actually constrain what it claims?
//
// Convention:
//   - assertUnsat  => the statement is false; the solver MUST reject it.
//                     If it ever SOLVES, that is a soundness break (embarrassment).
//   - assertSat    => a known-good baseline; the solver must accept it. If it
//                     fails, the harness/circuit is broken (not a forgery, but a bug).
//
// These are white-box (package circuit) so we can reuse the project's own witness
// builders and compute helpers, eliminating any encoding drift between test and prod.

import (
	"crypto/sha256"
	"math/big"
	"testing"

	secp256k1 "github.com/consensys/gnark-crypto/ecc/secp256k1"
	"github.com/consensys/gnark/frontend"
)

// g1Gen returns the secp256k1 affine generator (mirrors witness_gen_hd_v1.go).
func g1Gen() secp256k1.G1Affine {
	jac, _ := secp256k1.Generators()
	var g secp256k1.G1Affine
	g.FromJacobian(&jac)
	return g
}

// scalarMulG returns s*G as an affine secp256k1 point.
func scalarMulG(s *big.Int) secp256k1.G1Affine {
	g := g1Gen()
	var p secp256k1.G1Affine
	p.ScalarMultiplication(&g, s)
	return p
}

// keypair derives a deterministic secp256k1 keypair from a label.
// Returns (privkey, pubX, pubY). The attacker KNOWS this privkey — the whole
// point of the forgery tests is "a key I can sign for".
func keypair(label string) (*big.Int, *big.Int, *big.Int) {
	h := sha256.Sum256([]byte(label))
	k := new(big.Int).SetBytes(h[:])
	k.Mod(k, secp256k1.ID.ScalarField())
	if k.Sign() == 0 {
		k.SetUint64(1)
	}
	pub := scalarMulG(k)
	return k, pub.X.BigInt(new(big.Int)), pub.Y.BigInt(new(big.Int))
}

// splitX splits a 32-byte-big-endian x coordinate into (high128, low128).
func splitX(x *big.Int) (high, low *big.Int) {
	b := make([]byte, 32)
	raw := x.Bytes()
	copy(b[32-len(raw):], raw)
	return new(big.Int).SetBytes(b[0:16]), new(big.Int).SetBytes(b[16:32])
}

// buildValidParamWitness reconstructs a fully-valid HDv1 witness for arbitrary
// derivation parameters, mirroring GenerateValidWitnessHDV1 but parameterised so
// boundary cases (salt at 2^32, specific paths, etc.) can be exercised cleanly.
//
// It reuses the in-package compute* helpers, so by construction it agrees with
// the circuit's intended semantics. Mutating one field of the result is then a
// surgical "false statement" we feed to the solver.
func buildValidParamWitness(seedLabel string, account, change, index uint32, salt *big.Int) *ValidWitnessDataHDV1 {
	_, masterX, masterY := keypair(seedLabel + "_master")

	pathVector := packPathVector(account, change, index)

	commitmentBytes := computeCommitmentV1(masterX, pathVector, salt)
	commitment := new(big.Int).SetBytes(commitmentBytes)

	derivScalar := computeDerivationScalarV1(commitmentBytes, account, change, index)

	master := secp256k1.G1Affine{}
	master.X.SetBigInt(masterX)
	master.Y.SetBigInt(masterY)
	derivPoint := scalarMulG(derivScalar)
	var child secp256k1.G1Affine
	child.Add(&master, &derivPoint)
	childX := child.X.BigInt(new(big.Int))
	childY := child.Y.BigInt(new(big.Int))

	high, low := splitX(childX)

	leafBytes := computeLeafHashV1(masterX, masterY)

	merkleIndex := uint8(42)
	siblings := make([][]byte, 8)
	siblingsBig := make([]*big.Int, 8)
	for i := 0; i < 8; i++ {
		h := sha256.Sum256([]byte(seedLabel + "_sib_" + string(rune('a'+i))))
		b := new(big.Int).SetBytes(h[:])
		b.Mod(b, blsField) // siblings are BLS Fr elements — must be canonical (< r) like GenerateValidWitnessHDV1
		siblingsBig[i] = b
		siblings[i] = pack32BE(b)
	}
	root := computeMerkleRootV1(leafBytes, merkleIndex, siblings)

	chainSep := big.NewInt(0x7bc915)
	assetHash := sha256.Sum256([]byte(seedLabel + "_asset"))
	assetID := new(big.Int).SetBytes(assetHash[:])

	return &ValidWitnessDataHDV1{
		ChainSeparator:       BigIntToHex(chainSep),
		AssetID:              BigIntToHex(assetID),
		ComplianceRoot:       BigIntToHex(new(big.Int).SetBytes(root)),
		TfrAnchor:            BigIntToHex(big.NewInt(0)),
		OutputKeyHigh:        BigIntToHex(high),
		OutputKeyLow:         BigIntToHex(low),
		MasterPubkeyX:        BigIntToHex(masterX),
		MasterPubkeyY:        BigIntToHex(masterY),
		DerivationCommitment: BigIntToHex(commitment),
		PathVector:           BigIntToHex(pathVector),
		Salt:                 BigIntToHex(salt),
		ChildPubkeyX:         BigIntToHex(childX),
		ChildPubkeyY:         BigIntToHex(childY),
		MerklePathBits:       BigIntToHex(big.NewInt(int64(merkleIndex))),
		MerkleSiblings:       bigIntsToHex(siblingsBig),
	}
}

// assignmentFrom converts a (possibly mutated) witness into a circuit assignment
// using the production converter buildWitnessHDV1 — same path the real prover uses.
func assignmentFrom(t *testing.T, w *ValidWitnessDataHDV1) *TensorCashKYCCircuitHDV1 {
	t.Helper()
	a, err := buildWitnessHDV1(w.ToProveRequestHDV1())
	if err != nil {
		t.Fatalf("buildWitnessHDV1: %v", err)
	}
	return a
}

// clone makes a shallow copy of a witness so a test can mutate one field without
// disturbing other table-driven cases.
func clone(w *ValidWitnessDataHDV1) *ValidWitnessDataHDV1 {
	cp := *w
	cp.MerkleSiblings = append([]string(nil), w.MerkleSiblings...)
	return &cp
}

var blsField = func() *big.Int {
	// BLS12-381 scalar field (the native field the circuit is compiled over).
	// Imported lazily to keep this file's imports minimal.
	v, _ := new(big.Int).SetString("73eda753299d7d483339d80809a1d80553bda402fffe5bfeffffffff00000001", 16)
	return v
}()

var _ = frontend.Variable(nil) // keep frontend imported for sibling test files
