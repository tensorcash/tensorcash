// SPDX-License-Identifier: Apache-2.0
package circuit

// WitnessDataHDV1 contains the private witness data for the KYC-HD v1 circuit.
// Pubkey-only: no master_secret needed. Key control is proven by the Taproot spend signature.
type WitnessDataHDV1 struct {
	// Parent pubkey (enrolled with issuer)
	MasterPubkeyX string `json:"master_pubkey_x"`
	MasterPubkeyY string `json:"master_pubkey_y"`

	// Derivation parameters (packed format)
	DerivationCommitment string `json:"derivation_commitment"`
	PathVector           string `json:"path_vector"` // Packed account||change||index
	Salt                 string `json:"salt"`

	// Child key
	ChildPubkeyX string `json:"child_pubkey_x"`
	ChildPubkeyY string `json:"child_pubkey_y"`

	// Merkle proof (optimized format)
	MerklePathBits string   `json:"merkle_path_bits"` // Packed path as single value
	MerkleSiblings []string `json:"merkle_siblings"`  // Sibling hashes
}

// ProveRequestHDV1 represents a request to generate a proof for the alternative circuit
type ProveRequestHDV1 struct {
	// Public inputs
	ChainSeparator string `json:"chain_separator"`
	AssetID        string `json:"asset_id"`
	ComplianceRoot string `json:"compliance_root"`
	TfrAnchor      string `json:"tfr_anchor"`
	OutputKeyHigh  string `json:"output_key_high"` // Upper 128 bits of child x-only key
	OutputKeyLow   string `json:"output_key_low"`  // Lower 128 bits of child x-only key

	// Private witness
	Witness WitnessDataHDV1 `json:"witness"`
}

// ProveResponseHDV1 represents the response from proof generation
type ProveResponseHDV1 struct {
	Success         bool   `json:"success"`
	ProofHex        string `json:"proof_hex,omitempty"`
	PublicInputsHex string `json:"public_inputs_hex,omitempty"`
	Error           string `json:"error,omitempty"`
}