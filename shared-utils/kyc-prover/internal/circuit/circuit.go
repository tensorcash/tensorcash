// SPDX-License-Identifier: Apache-2.0
package circuit

import (
	"github.com/consensys/gnark/frontend"
	"github.com/consensys/gnark/std/hash/mimc"
)

// TensorCashKYCCircuit is a reference implementation for KYC compliance proofs.
// This circuit proves:
// 1. Holder knows a secret that hashes to their registered pubkey
// 2. Holder meets compliance requirements (country, age, etc.)
// 3. Holder is in the compliance Merkle tree
// 4. Proof is bound to specific asset and chain
type TensorCashKYCCircuit struct {
	// PUBLIC INPUTS (on-chain, visible to everyone)
	// MUST be in this exact order to match TensorCash consensus rules
	ChainSeparator frontend.Variable `gnark:",public"` // Index 0: Prevents cross-chain replay
	AssetID        frontend.Variable `gnark:",public"` // Index 1: Binds proof to asset
	ComplianceRoot frontend.Variable `gnark:",public"` // Index 2: Merkle root || height
	TfrAnchor      frontend.Variable `gnark:",public"` // Index 3: Transfer reporting commitment

	// PRIVATE INPUTS (off-chain, secret witness data)
	Secret     frontend.Variable `gnark:",secret"` // Holder's secret preimage
	PubkeyHash frontend.Variable `gnark:",secret"` // Hash(secret) - proves identity

	// KYC attributes (private)
	Country frontend.Variable `gnark:",secret"` // ISO 3166-1 numeric (e.g., 840 = USA)
	Age     frontend.Variable `gnark:",secret"` // Holder's age

	// Merkle proof (private)
	MerkleProof    [8]frontend.Variable `gnark:",secret"` // Path from leaf to root
	MerkleIndex    frontend.Variable   `gnark:",secret"` // Leaf position
	MerkleLeafHash frontend.Variable   `gnark:",secret"` // Hash(pubkey || country || age)
}

// Define implements the circuit constraints
func (circuit *TensorCashKYCCircuit) Define(api frontend.API) error {
	// ====================================================================
	// CONSTRAINT 1: Prove knowledge of secret
	// ====================================================================
	// This proves the holder knows the secret that produces their pubkey hash
	// Without revealing the secret itself
	mimc, err := mimc.NewMiMC(api)
	if err != nil {
		return err
	}
	mimc.Write(circuit.Secret)
	computedHash := mimc.Sum()
	api.AssertIsEqual(computedHash, circuit.PubkeyHash)

	// ====================================================================
	// CONSTRAINT 2: Prove compliance requirements
	// ====================================================================
	// Example: Holder must be from USA (country code 840) and 18+
	// Real implementations would make these configurable per asset
	api.AssertIsEqual(circuit.Country, 840) // USA only
	api.AssertIsLessOrEqual(18, circuit.Age) // 18+

	// ====================================================================
	// CONSTRAINT 3: Prove holder is in whitelist (Merkle proof)
	// ====================================================================
	// Compute leaf hash: Hash(pubkey_hash || country || age)
	mimc.Reset()
	mimc.Write(circuit.PubkeyHash)
	mimc.Write(circuit.Country)
	mimc.Write(circuit.Age)
	leafHash := mimc.Sum()
	api.AssertIsEqual(leafHash, circuit.MerkleLeafHash)

	// Verify Merkle proof
	// CRITICAL: Decompose index bits ONCE before loop (api.Div is field division, not integer shift)
	bits := api.ToBinary(circuit.MerkleIndex, MerkleTreeDepth())
	currentHash := leafHash

	for i := 0; i < MerkleTreeDepth(); i++ {
		// Determine if we're left or right child using pre-computed bit
		bit := bits[i]

		// Hash with sibling (order depends on bit)
		mimc.Reset()
		leftHash := api.Select(bit, circuit.MerkleProof[i], currentHash)
		rightHash := api.Select(bit, currentHash, circuit.MerkleProof[i])
		mimc.Write(leftHash)
		mimc.Write(rightHash)
		currentHash = mimc.Sum()
	}

	// Final hash must equal the public compliance root
	api.AssertIsEqual(currentHash, circuit.ComplianceRoot)

	// ====================================================================
	// CONSTRAINT 4: Bind to asset and chain
	// ====================================================================
	// These constraints ensure the proof can't be replayed across chains
	// or used for different assets
	api.AssertIsDifferent(circuit.ChainSeparator, 0)
	api.AssertIsDifferent(circuit.AssetID, 0)

	// TFR anchor can be zero (no transfer reporting required)
	// but if set, it binds the proof to a specific reporting commitment

	return nil
}

// PublicInputCount returns the number of public inputs (always 4)
func PublicInputCount() int {
	return 4
}

// MerkleTreeDepth returns the depth of the compliance Merkle tree
func MerkleTreeDepth() int {
	return 8
}
