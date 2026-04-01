// SPDX-License-Identifier: Apache-2.0
package circuit

import (
	"encoding/json"
)

// ProveRequestHD represents a request to generate a ZK proof for KYC-HD v1
type ProveRequestHD struct {
	// Public inputs (same as v1)
	ChainSeparator string `json:"chain_separator"`
	AssetID        string `json:"asset_id"`
	ComplianceRoot string `json:"compliance_root"`
	TfrAnchor      string `json:"tfr_anchor"`

	// Private witness (extended for HD)
	Witness WitnessDataHD `json:"witness"`
}

// WitnessDataHD contains the private inputs for KYC-HD v1 proof generation
type WitnessDataHD struct {
	// Master key
	MasterSecret  string `json:"master_secret"`   // Hex string - secp256k1 scalar
	MasterPubkeyX string `json:"master_pubkey_x"` // Hex string - X coordinate
	MasterPubkeyY string `json:"master_pubkey_y"` // Hex string - Y coordinate

	// Derivation path
	PathAccount int `json:"path_account"` // Account index
	PathChange  int `json:"path_change"`  // 0 = external, 1 = internal
	PathIndex   int `json:"path_index"`   // Address index
	Salt        int `json:"salt"`         // Optional randomizer

	// Derived child key
	ChildPubkeyX string `json:"child_pubkey_x"` // Hex string - X coordinate
	ChildPubkeyY string `json:"child_pubkey_y"` // Hex string - Y coordinate

	// KYC attributes
	Country int `json:"country"`
	Age     int `json:"age"`

	// Merkle proof (commits to master pubkey, not child)
	MerkleProof    []string `json:"merkle_proof"`
	MerkleIndex    int      `json:"merkle_index"`
	MerkleLeafHash string   `json:"merkle_leaf_hash"` // Hash(master_pubkey_x || country || age)

	// DEBUG
	DerivDigestDebug     string   `json:"deriv_digest_debug"`
	DerivScalarDebug     string   `json:"deriv_scalar_debug"`
	DerivScalarMontDebug string   `json:"deriv_scalar_mont_debug"`
	MontFixDebug         string   `json:"mont_fix_debug"`
	LeafDigestDebug      string   `json:"leaf_digest_debug"`
	PxOnlyHashDebug    string   `json:"px_only_hash_debug"`
	RootFromProofDebug string   `json:"root_from_proof_debug"`
	NodeDebug          []string `json:"node_debug"`
	MasterMulDebugX    string   `json:"master_mul_debug_x"`
	MasterMulDebugY    string   `json:"master_mul_debug_y"`
	RDebugX            string   `json:"r_debug_x"`
	RDebugY            string   `json:"r_debug_y"`
	QmPDebugX          string   `json:"qmp_debug_x"`
	QmPDebugY          string   `json:"qmp_debug_y"`
	RDebugBLS          string   `json:"r_debug_bls"`
	HDebugSecp         string   `json:"h_debug_secp"`
}

// ProveResponseHD contains the generated proof and public inputs (same format as v1)
type ProveResponseHD struct {
	ProofHex        string `json:"proof_hex"`
	PublicInputsHex string `json:"public_inputs_hex"`
	Success         bool   `json:"success"`
	Error           string `json:"error,omitempty"`
}

// ToJSON serializes to JSON
func (r *ProveRequestHD) ToJSON() ([]byte, error) {
	return json.Marshal(r)
}

// FromJSON deserializes from JSON
func (r *ProveRequestHD) FromJSON(data []byte) error {
	return json.Unmarshal(data, r)
}
