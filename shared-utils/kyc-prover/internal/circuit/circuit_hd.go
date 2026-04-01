// SPDX-License-Identifier: Apache-2.0
package circuit

import (
	"github.com/consensys/gnark/frontend"
	"github.com/consensys/gnark/std/algebra/emulated/sw_emulated"
	"github.com/consensys/gnark/std/hash/mimc"
	"github.com/consensys/gnark/std/math/emulated"
	stdbits "github.com/consensys/gnark/std/math/bits"
)

// Secp256k1Fp is the base field of secp256k1
type Secp256k1Fp = emulated.Secp256k1Fp

// Secp256k1Fr is the scalar field of secp256k1
type Secp256k1Fr = emulated.Secp256k1Fr

// TensorCashKYCCircuitHD is the KYC-HD v1 circuit with master key derivation.
// This circuit proves:
// 1. Holder knows a master key pair (master_secret, master_pubkey)
// 2. Holder can derive a child key Q = P + h·G where h = MiMC(P || path || salt)
// 3. Master pubkey commitment is in the compliance Merkle tree
// 4. Proof is bound to specific asset and chain
//
// CRITICAL: Public inputs remain IDENTICAL to v1 for consensus compatibility
type TensorCashKYCCircuitHD struct {
	// PUBLIC INPUTS (on-chain, visible to everyone)
	// MUST be in this exact order to match TensorCash consensus rules
	ChainSeparator frontend.Variable `gnark:",public"` // Index 0: Prevents cross-chain replay
	AssetID        frontend.Variable `gnark:",public"` // Index 1: Binds proof to asset
	ComplianceRoot frontend.Variable `gnark:",public"` // Index 2: Merkle root || height
	TfrAnchor      frontend.Variable `gnark:",public"` // Index 3: Transfer reporting commitment

	// PRIVATE INPUTS (off-chain, secret witness data)

	// Master key (enrolled with issuer)
	MasterSecret  emulated.Element[Secp256k1Fr] // Master secret key (secp256k1 scalar)
	MasterPubkeyX emulated.Element[Secp256k1Fp] // Master public key X coordinate
	MasterPubkeyY emulated.Element[Secp256k1Fp] // Master public key Y coordinate

	// Derivation path (BIP32-style but simplified)
	PathAccount frontend.Variable `gnark:",secret"` // Account index
	PathChange  frontend.Variable `gnark:",secret"` // Change index (0 = external, 1 = internal)
	PathIndex   frontend.Variable `gnark:",secret"` // Address index
	Salt        frontend.Variable `gnark:",secret"` // Optional randomizer for privacy

	// Derived child key (used on-chain, but never revealed as public input)
	ChildPubkeyX emulated.Element[Secp256k1Fp] // Child public key X coordinate
	ChildPubkeyY emulated.Element[Secp256k1Fp] // Child public key Y coordinate

	// KYC attributes (private)
	Country frontend.Variable `gnark:",secret"` // ISO 3166-1 numeric
	Age     frontend.Variable `gnark:",secret"` // Holder's age

	// Merkle proof (private) - commits to MASTER pubkey, not child
	MerkleProof    [8]frontend.Variable `gnark:",secret"` // Path from leaf to root
	MerkleIndex    frontend.Variable   `gnark:",secret"` // Leaf position
	MerkleLeafHash frontend.Variable   `gnark:",secret"` // Hash(master_pubkey_x || country || age)

	// DEBUG taps to pinpoint mismatch
	DerivDigestDebug   frontend.Variable             `gnark:",secret"` // r_D (BLS12-381 Fr)
	DerivScalarDebug   emulated.Element[Secp256k1Fr] `gnark:",secret"` // h_D (secp256k1 Fr)
	LeafDigestDebug    frontend.Variable             `gnark:",secret"` // leaf_D (BLS12-381 Fr)
	PxOnlyHashDebug    frontend.Variable             `gnark:",secret"` // MiMC(Px_bytes) only
	RootFromProofDebug frontend.Variable             `gnark:",secret"` // root computed from proof
	NodeDebug          [8]frontend.Variable          `gnark:",secret"` // per-level Merkle nodes
	MasterMulDebugX    emulated.Element[Secp256k1Fp] `gnark:",secret"` // p·G.x from host
	MasterMulDebugY    emulated.Element[Secp256k1Fp] `gnark:",secret"` // p·G.y from host
	RDebugX            emulated.Element[Secp256k1Fp] `gnark:",secret"` // h·G.x from host
	RDebugY            emulated.Element[Secp256k1Fp] `gnark:",secret"` // h·G.y from host
	QmPDebugX          emulated.Element[Secp256k1Fp] `gnark:",secret"` // (Q-P).x from host
	QmPDebugY          emulated.Element[Secp256k1Fp] `gnark:",secret"` // (Q-P).y from host
	RDebugBLS          frontend.Variable             `gnark:",secret"` // MiMC digest r in BLS12-381 Fr
	HDebugSecp         emulated.Element[Secp256k1Fr] `gnark:",secret"` // h mod n in secp256k1 Fr
	HMontDebug         emulated.Element[Secp256k1Fr] `gnark:",secret"` // host copy of gnark FromBits output (Montgomery form)
	MontFixDebug       emulated.Element[Secp256k1Fr] `gnark:",secret"` // Montgomery correction factor (R^-1)
}

// packFpToBytes32 converts an emulated secp256k1 Fp element to 32 bytes big-endian
func packFpToBytes32(api frontend.API, fp *emulated.Field[Secp256k1Fp], a *emulated.Element[Secp256k1Fp]) []frontend.Variable {
	// 256 bits, LSB-first for the whole number
	all := fp.ToBits(a)

	// Build 32 bytes: for little-endian bytes, byte i = bits[8*i : 8*i+8]
	// Then reverse to big-endian byte order.
	bytesBE := make([]frontend.Variable, 32)
	for i := 0; i < 32; i++ {
		byteLE := all[i*8 : i*8+8]                                 // LSB..MSB within the byte
		b := stdbits.FromBinary(api, byteLE, stdbits.WithNbDigits(8)) // ∑ bit[k]*2^k
		bytesBE[31-i] = b                                          // flip to big-endian byte order
	}
	return bytesBE
}

// writeU32BE writes a uint32 as 4 bytes big-endian to MiMC
func writeU32BE(api frontend.API, hasher mimc.MiMC, v frontend.Variable) {
	bits := api.ToBinary(v, 32) // LSB-first for the whole u32
	// Build 4 bytes: byte i = bits[8*i : 8*i+8] (LSB-first within byte)
	// Then reverse to big-endian byte order
	bytes := make([]frontend.Variable, 4)
	for i := 0; i < 4; i++ {
		byteLE := bits[i*8 : i*8+8]                                    // LSB..MSB within the byte
		bytes[3-i] = stdbits.FromBinary(api, byteLE, stdbits.WithNbDigits(8)) // flip to big-endian byte order
	}
	for _, b := range bytes {
		hasher.Write(b)
	}
}

// Define implements the circuit constraints
func (circuit *TensorCashKYCCircuitHD) Define(api frontend.API) error {
	// Initialize secp256k1 helpers
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

	// Reduce coordinates into the base field
	Px := fp.Reduce(&circuit.MasterPubkeyX)
	Py := fp.Reduce(&circuit.MasterPubkeyY)
	Qx := fp.Reduce(&circuit.ChildPubkeyX)
	Qy := fp.Reduce(&circuit.ChildPubkeyY)

	P := sw_emulated.AffinePoint[Secp256k1Fp]{X: *Px, Y: *Py}
	Q := sw_emulated.AffinePoint[Secp256k1Fp]{X: *Qx, Y: *Qy}

	// Basic on-curve enforcement
	secp256k1Curve.AssertIsOnCurve(&P)
	secp256k1Curve.AssertIsOnCurve(&Q)

	// Master secret must match supplied master public key (fixed-base multiplication)
	masterSecret := scalarField.Reduce(&circuit.MasterSecret)
	Pcalc := secp256k1Curve.ScalarMulBase(masterSecret)
	secp256k1Curve.AssertIsEqual(&P, Pcalc)

	// Debug taps: ensure p·G from host matches circuit computation
	masterMulDebugX := fp.Reduce(&circuit.MasterMulDebugX)
	masterMulDebugY := fp.Reduce(&circuit.MasterMulDebugY)
	secp256k1Curve.AssertIsEqual(
		Pcalc,
		&sw_emulated.AffinePoint[Secp256k1Fp]{X: *masterMulDebugX, Y: *masterMulDebugY},
	)

	// Range-check derivation path components (uint32)
	api.AssertIsLessOrEqual(circuit.PathAccount, uint64(0xffffffff))
	api.AssertIsLessOrEqual(circuit.PathChange, uint64(0xffffffff))
	api.AssertIsLessOrEqual(circuit.PathIndex, uint64(0xffffffff))
	api.AssertIsLessOrEqual(circuit.Salt, uint64(0xffffffff))

	// Prepare MiMC inputs
	mHasher, err := mimc.NewMiMC(api)
	if err != nil {
		return err
	}

	for _, b := range []byte("KYC-HD-v1") {
		mHasher.Write(b)
	}

	pxBytes := packFpToBytes32(api, fp, Px)
	for _, b := range pxBytes {
		mHasher.Write(b)
	}

	writeU32BE(api, mHasher, circuit.PathAccount)
	writeU32BE(api, mHasher, circuit.PathChange)
	writeU32BE(api, mHasher, circuit.PathIndex)
	writeU32BE(api, mHasher, circuit.Salt)

	derivDigest := mHasher.Sum()
	api.AssertIsEqual(derivDigest, circuit.DerivDigestDebug)
	api.AssertIsEqual(derivDigest, circuit.RDebugBLS)

	// Convert digest to secp scalar and compare with witness-provided scalar
	digestBits := api.ToBinary(derivDigest, 256)
	witnessScalarCanonical := scalarField.FromBits(digestBits...)
	hWitness := scalarField.Reduce(&circuit.HDebugSecp)
	scalarField.AssertIsEqual(witnessScalarCanonical, hWitness)
	scalarField.AssertIsEqual(hWitness, scalarField.Reduce(&circuit.DerivScalarDebug))

	// Compute R = h·G
	R := secp256k1Curve.ScalarMulBase(hWitness)
	rDebugX := fp.Reduce(&circuit.RDebugX)
	rDebugY := fp.Reduce(&circuit.RDebugY)
	secp256k1Curve.AssertIsEqual(
		R,
		&sw_emulated.AffinePoint[Secp256k1Fp]{X: *rDebugX, Y: *rDebugY},
	)

	// Check Q - P debug taps and enforce Q = P + R
	negP := secp256k1Curve.Neg(&P)
	QminusP := secp256k1Curve.Add(&Q, negP)
	qmPX := fp.Reduce(&circuit.QmPDebugX)
	qmPY := fp.Reduce(&circuit.QmPDebugY)
	secp256k1Curve.AssertIsEqual(
		QminusP,
		&sw_emulated.AffinePoint[Secp256k1Fp]{X: *qmPX, Y: *qmPY},
	)
	secp256k1Curve.AssertIsEqual(QminusP, R)

	expectedChild := secp256k1Curve.Add(&P, R)
	secp256k1Curve.AssertIsEqual(expectedChild, &Q)

	// Px-only hash (debug)
	mHasher.Reset()
	for _, b := range pxBytes {
		mHasher.Write(b)
	}
	pxOnly := mHasher.Sum()
	api.AssertIsEqual(pxOnly, circuit.PxOnlyHashDebug)

	// Leaf hash: Hash(Px || country || age)
	mHasher.Reset()
	for _, b := range pxBytes {
		mHasher.Write(b)
	}
	writeU32BE(api, mHasher, circuit.Country)
	writeU32BE(api, mHasher, circuit.Age)
	leaf := mHasher.Sum()
	api.AssertIsEqual(leaf, circuit.LeafDigestDebug)
	api.AssertIsEqual(leaf, circuit.MerkleLeafHash)

	// Verify Merkle path (LSB-first index convention)
	indexBits := stdbits.ToBinary(api, circuit.MerkleIndex, stdbits.WithNbDigits(MerkleTreeDepthHD()))
	current := leaf
	for i := 0; i < MerkleTreeDepthHD(); i++ {
		sibling := circuit.MerkleProof[i]
		left := api.Select(indexBits[i], sibling, current)
		right := api.Select(indexBits[i], current, sibling)

		mHasher.Reset()
		mHasher.Write(left)
		mHasher.Write(right)
		current = mHasher.Sum()
		api.AssertIsEqual(current, circuit.NodeDebug[i])
	}

	api.AssertIsLessOrEqual(circuit.MerkleIndex, uint64((1<<MerkleTreeDepthHD())-1))
	api.AssertIsEqual(current, circuit.RootFromProofDebug)
	api.AssertIsEqual(current, circuit.ComplianceRoot)

	// Policy checks
	api.AssertIsEqual(circuit.Country, 840)
	api.AssertIsLessOrEqual(18, circuit.Age)
	api.AssertIsDifferent(circuit.ChainSeparator, 0)
	api.AssertIsDifferent(circuit.AssetID, 0)

	return nil
}

// PublicInputCountHD returns the number of public inputs (always 4, same as v1)
func PublicInputCountHD() int {
	return 4
}

// MerkleTreeDepthHD returns the depth of the compliance Merkle tree
func MerkleTreeDepthHD() int {
	return 8
}
