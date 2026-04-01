// SPDX-License-Identifier: Apache-2.0
package circuit

import (
	"encoding/hex"
	"encoding/json"
	"math/big"
)

// ProveRequest represents a request to generate a ZK proof
type ProveRequest struct {
	// Public inputs
	ChainSeparator string `json:"chain_separator"` // Hex string
	AssetID        string `json:"asset_id"`        // Hex string
	ComplianceRoot string `json:"compliance_root"` // Hex string
	TfrAnchor      string `json:"tfr_anchor"`      // Hex string

	// Private witness
	Witness WitnessData `json:"witness"`
}

// WitnessData contains the private inputs for proof generation
type WitnessData struct {
	Secret     string   `json:"secret"`      // Hex string
	PubkeyHash string   `json:"pubkey_hash"` // Hex string
	Country    int      `json:"country"`     // ISO 3166-1 numeric
	Age        int      `json:"age"`         // Years

	MerkleProof    []string `json:"merkle_proof"`     // Array of hex strings
	MerkleIndex    int      `json:"merkle_index"`     // Leaf position
	MerkleLeafHash string   `json:"merkle_leaf_hash"` // Hex string
}

// ProveResponse contains the generated proof and public inputs
type ProveResponse struct {
	ProofHex        string `json:"proof_hex"`                // Gnark format for API verification (~244 bytes)
	ProofCustomHex  string `json:"proof_custom_hex,omitempty"` // C++ format for on-chain (192 bytes)
	PublicInputsHex string `json:"public_inputs_hex"`         // 128 bytes hex (4 x 32)
	Success         bool   `json:"success"`
	Error           string `json:"error,omitempty"`
}

// VerifyRequest represents a request to verify a proof
type VerifyRequest struct {
	ProofHex        string `json:"proof_hex"`
	PublicInputsHex string `json:"public_inputs_hex"`
	VKHex           string `json:"vk_hex"`
}

// VerifyResponse contains verification result
type VerifyResponse struct {
	Valid   bool   `json:"valid"`
	Error   string `json:"error,omitempty"`
}

// SetupResult contains the proving and verification keys
type SetupResult struct {
	ProvingKey      []byte `json:"-"`              // Binary proving key (large, ~140MB)
	VerificationKey []byte `json:"-"`              // Binary verification key (gnark format, ~872 bytes)
	VKHex           string `json:"vk_hex"`         // Hex-encoded VK in custom C++ format (578 bytes)
	VKGnarkHex      string `json:"vk_gnark_hex"`   // Hex-encoded VK in gnark format (872 bytes) for Go verification
}

// HexToBigInt converts hex string to big.Int
func HexToBigInt(hexStr string) (*big.Int, error) {
	// Remove 0x prefix if present
	if len(hexStr) >= 2 && hexStr[:2] == "0x" {
		hexStr = hexStr[2:]
	}

	bytes, err := hex.DecodeString(hexStr)
	if err != nil {
		return nil, err
	}

	return new(big.Int).SetBytes(bytes), nil
}

// BigIntToHex converts big.Int to hex string (0x-prefixed, 32 bytes)
func BigIntToHex(val *big.Int) string {
	bytes := val.Bytes()

	// Pad to 32 bytes
	if len(bytes) < 32 {
		padded := make([]byte, 32)
		copy(padded[32-len(bytes):], bytes)
		bytes = padded
	}

	return "0x" + hex.EncodeToString(bytes)
}

// ToJSON serializes to JSON
func (r *ProveRequest) ToJSON() ([]byte, error) {
	return json.Marshal(r)
}

// FromJSON deserializes from JSON
func (r *ProveRequest) FromJSON(data []byte) error {
	return json.Unmarshal(data, r)
}
