// SPDX-License-Identifier: Apache-2.0
package circuit

import (
	"github.com/consensys/gnark/frontend"
	"github.com/consensys/gnark/std/algebra/emulated/sw_emulated"
	"github.com/consensys/gnark/std/hash/mimc"
	"github.com/consensys/gnark/std/math/emulated"
	stdbits "github.com/consensys/gnark/std/math/bits"
)

// TensorCashKYCCircuitHDV1 proves that a spent child key derives from a
// KYC-enrolled parent pubkey, without requiring the master private key.
// Key control is enforced by the Taproot spend signature, not by this circuit.
//
// Architecture:
// 1. Batch hashing approach instead of byte-by-byte
// 2. Commitment-based key derivation with intermediate steps
// 3. Optimized Merkle verification using lookup tables
// 4. Pubkey-only: no master_secret in witness
type TensorCashKYCCircuitHDV1 struct {
	// PUBLIC INPUTS (must maintain compatibility)
	ChainSeparator frontend.Variable `gnark:",public"` // Index 0
	AssetID        frontend.Variable `gnark:",public"` // Index 1
	ComplianceRoot frontend.Variable `gnark:",public"` // Index 2
	TfrAnchor      frontend.Variable `gnark:",public"` // Index 3
	OutputKeyHigh  frontend.Variable `gnark:",public"` // Index 4 — upper 128 bits of child x-only key
	OutputKeyLow   frontend.Variable `gnark:",public"` // Index 5 — lower 128 bits of child x-only key

	// PRIVATE INPUTS - Parent Pubkey (enrolled with issuer, no secret needed)
	MasterPubkeyX emulated.Element[Secp256k1Fp] // Parent public key X
	MasterPubkeyY emulated.Element[Secp256k1Fp] // Parent public key Y

	// PRIVATE INPUTS - Derivation Parameters
	DerivationCommitment frontend.Variable `gnark:",secret"` // Pre-computed commitment
	PathVector           frontend.Variable `gnark:",secret"` // Packed path (account||change||index)
	Salt                 frontend.Variable `gnark:",secret"` // Randomizer

	// PRIVATE INPUTS - Child Key
	ChildPubkeyX emulated.Element[Secp256k1Fp] // Child public key X
	ChildPubkeyY emulated.Element[Secp256k1Fp] // Child public key Y

	// PRIVATE INPUTS - Merkle Proof (optimized)
	MerklePathBits   frontend.Variable   `gnark:",secret"` // Packed path bits
	MerkleSiblings   [8]frontend.Variable `gnark:",secret"` // Sibling hashes
}

// Define implements the circuit constraints with alternative approach
func (circuit *TensorCashKYCCircuitHDV1) Define(api frontend.API) error {
	// Initialize field helpers
	secp256k1Curve, err := sw_emulated.New[Secp256k1Fp, Secp256k1Fr](api, sw_emulated.GetSecp256k1Params())
	if err != nil {
		return err
	}

	fp, err := emulated.NewField[Secp256k1Fp](api)
	if err != nil {
		return err
	}

	scalarField, err := emulated.NewField[Secp256k1Fr](api)
	if err != nil {
		return err
	}

	// === SECTION 1: Parent Key Validation ===
	// No master_secret check — key control is proven by the Taproot spend signature.
	// The circuit only needs to verify the parent pubkey is valid and enrolled.
	Px := fp.Reduce(&circuit.MasterPubkeyX)
	Py := fp.Reduce(&circuit.MasterPubkeyY)
	P := sw_emulated.AffinePoint[Secp256k1Fp]{X: *Px, Y: *Py}

	// Verify P is on curve (parent pubkey must be a valid secp256k1 point)
	secp256k1Curve.AssertIsOnCurve(&P)

	// === SECTION 2: Derivation Commitment Verification ===
	// Unpack path vector into components
	pathBits := api.ToBinary(circuit.PathVector, 96) // 32 bits each for account, change, index
	accountBits := pathBits[0:32]
	changeBits := pathBits[32:64]
	indexBits := pathBits[64:96]

	// Reconstruct account, change, index values
	account := stdbits.FromBinary(api, accountBits, stdbits.WithNbDigits(32))
	change := stdbits.FromBinary(api, changeBits, stdbits.WithNbDigits(32))
	index := stdbits.FromBinary(api, indexBits, stdbits.WithNbDigits(32))

	// Create commitment: H(P.X || PathVector || Salt)
	commitHasher, _ := mimc.NewMiMC(api)

	// Write tag for domain separation
	commitHasher.Write(frontend.Variable(0x4B594348445631)) // "KYCHDV1" in hex

	// Batch write P.X as single field element
	pxBits := fp.ToBits(Px)
	pxValue := stdbits.FromBinary(api, pxBits, stdbits.WithNbDigits(256))
	commitHasher.Write(pxValue)

	// Pre-compute P.Y as native element for leaf hash binding below
	pyBits := fp.ToBits(Py)
	pyValue := stdbits.FromBinary(api, pyBits, stdbits.WithNbDigits(256))

	// Write packed path vector
	commitHasher.Write(circuit.PathVector)

	// Write salt
	commitHasher.Write(circuit.Salt)

	computedCommitment := commitHasher.Sum()
	api.AssertIsEqual(computedCommitment, circuit.DerivationCommitment)

	// === SECTION 3: Child Key Derivation ===
	// Compute derivation scalar from commitment
	derivHasher, _ := mimc.NewMiMC(api)
	derivHasher.Write(circuit.DerivationCommitment)
	derivHasher.Write(account) // Include individual components for additional binding
	derivHasher.Write(change)
	derivHasher.Write(index)
	derivDigest := derivHasher.Sum()

	// Convert to secp256k1 scalar
	derivBits := api.ToBinary(derivDigest, 256)
	derivScalar := scalarField.FromBits(derivBits...)

	// Compute R = derivScalar·G
	R := secp256k1Curve.ScalarMulBase(derivScalar)

	// Verify child key: Q = P + R
	Qx := fp.Reduce(&circuit.ChildPubkeyX)
	Qy := fp.Reduce(&circuit.ChildPubkeyY)
	Q := sw_emulated.AffinePoint[Secp256k1Fp]{X: *Qx, Y: *Qy}

	secp256k1Curve.AssertIsOnCurve(&Q)
	expectedQ := secp256k1Curve.Add(&P, R)
	secp256k1Curve.AssertIsEqual(&Q, expectedQ)

	// === SECTION 4: Output Key Binding ===
	// Decompose ChildPubkeyX into two 128-bit halves for consensus comparison.
	// secp256k1 Fp > BLS12-381 Fr, so a single native element can't hold all
	// possible x-coordinates. Two 128-bit halves trivially fit.
	//
	// CRITICAL: Split directly on the emulated bit decomposition, NOT after
	// converting to a native field element. FromBinary(...256) into a native
	// variable would reduce mod Fr, silently corrupting x-coordinates >= Fr.
	childXBits := fp.ToBits(Qx) // LSB-first bits of the full secp256k1 Fp value

	// Split directly on emulated bits: low = bits[0:128], high = bits[128:256]
	lowBits := childXBits[0:128]
	highBits := childXBits[128:256]

	// Each 128-bit half trivially fits in a native BLS12-381 Fr element
	computedLow := stdbits.FromBinary(api, lowBits, stdbits.WithNbDigits(128))
	computedHigh := stdbits.FromBinary(api, highBits, stdbits.WithNbDigits(128))

	api.AssertIsEqual(computedHigh, circuit.OutputKeyHigh)
	api.AssertIsEqual(computedLow, circuit.OutputKeyLow)

	// === SECTION 5: Merkle Proof (Optimized) ===
	// Compute leaf hash — MiMC(P.x, P.y) to fully bind the parent pubkey.
	// Binding both coordinates prevents P/-P ambiguity (same x, different y)
	// which matters now that the circuit no longer proves P = s*G.
	leafHasher, _ := mimc.NewMiMC(api)
	leafHasher.Write(pxValue)
	leafHasher.Write(pyValue)
	leafHash := leafHasher.Sum()

	// Unpack Merkle path bits
	pathBitsMerkle := api.ToBinary(circuit.MerklePathBits, 8)

	// Verify Merkle path using lookup table optimization
	current := leafHash
	for i := 0; i < 8; i++ {
		sibling := circuit.MerkleSiblings[i]

		// Use multiplexer for efficient selection
		left := api.Select(pathBitsMerkle[i], sibling, current)
		right := api.Select(pathBitsMerkle[i], current, sibling)

		nodeHasher, _ := mimc.NewMiMC(api)
		nodeHasher.Write(left)
		nodeHasher.Write(right)
		current = nodeHasher.Sum()
	}

	// Verify root matches
	api.AssertIsEqual(current, circuit.ComplianceRoot)

	// === SECTION 6: Additional Security Constraints ===
	// Ensure non-zero chain separator and asset ID
	api.AssertIsDifferent(circuit.ChainSeparator, 0)
	api.AssertIsDifferent(circuit.AssetID, 0)

	// Ensure path components are valid uint32
	api.AssertIsLessOrEqual(account, uint64(0xffffffff))
	api.AssertIsLessOrEqual(change, uint64(0xffffffff))
	api.AssertIsLessOrEqual(index, uint64(0xffffffff))
	api.AssertIsLessOrEqual(circuit.Salt, uint64(0xffffffff))

	return nil
}

// PublicInputCountHDV1 returns the number of public inputs (maintains compatibility)
func PublicInputCountHDV1() int {
	return 6
}

// MerkleTreeDepthHDV1 returns the Merkle tree depth
func MerkleTreeDepthHDV1() int {
	return 8
}