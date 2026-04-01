// SPDX-License-Identifier: Apache-2.0
package circuit

import (
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"math/big"

	"github.com/consensys/gnark-crypto/ecc"
	"github.com/consensys/gnark-crypto/ecc/bls12-381/fr"
	"github.com/consensys/gnark-crypto/ecc/bls12-381/fr/mimc"
)

// ValidWitnessData contains a complete, valid witness that satisfies the circuit
type ValidWitnessData struct {
	// Public inputs
	ChainSeparator string `json:"chain_separator"`
	AssetID        string `json:"asset_id"`
	ComplianceRoot string `json:"compliance_root"`
	TfrAnchor      string `json:"tfr_anchor"`

	// Private witness
	Secret         string   `json:"secret"`
	PubkeyHash     string   `json:"pubkey_hash"` // MiMC(secret)
	Country        int      `json:"country"`
	Age            int      `json:"age"`
	MerkleIndex    int      `json:"merkle_index"`
	MerkleLeafHash string   `json:"merkle_leaf_hash"` // MiMC(pubkey_hash || country || age)
	MerkleProof    []string `json:"merkle_proof"`     // Computed siblings
}

// GenerateValidWitness creates a deterministic valid witness from a seed
// The witness will satisfy ALL circuit constraints
func GenerateValidWitness(seed string) (*ValidWitnessData, error) {
	// Use seed to derive all values deterministically
	h := sha256.Sum256([]byte(seed))

	// 1. Generate secret from seed
	secret := new(big.Int).SetBytes(h[:])
	// Reduce to field size
	modulus := ecc.BLS12_381.ScalarField()
	secret.Mod(secret, modulus)

	// 2. Compute pubkey_hash = MiMC(secret)
	pubkeyHash, err := mimcHash(secret)
	if err != nil {
		return nil, fmt.Errorf("failed to compute pubkey hash: %w", err)
	}

	// 3. Set compliance attributes
	country := big.NewInt(840) // USA (required by circuit)
	age := big.NewInt(25)      // >= 18 (required by circuit)

	// 4. Compute merkle_leaf_hash = MiMC(pubkey_hash || country || age)
	merkleLeafHash, err := mimcHashMulti(pubkeyHash, country, age)
	if err != nil {
		return nil, fmt.Errorf("failed to compute leaf hash: %w", err)
	}

	// 5. Generate Merkle proof
	merkleIndex := 42 // Arbitrary leaf position
	merkleProof, complianceRoot, err := generateMerkleProof(merkleLeafHash, merkleIndex)
	if err != nil {
		return nil, fmt.Errorf("failed to generate merkle proof: %w", err)
	}

	// 6. Generate public inputs
	chainSeparator := big.NewInt(0x7bc914) // Arbitrary chain ID
	assetHash := sha256.Sum256([]byte(seed + "_asset"))
	assetID := new(big.Int).SetBytes(assetHash[:])
	assetID.Mod(assetID, modulus)
	tfrAnchor := big.NewInt(0) // No TFR anchor for basic tests

	return &ValidWitnessData{
		ChainSeparator: BigIntToHex(chainSeparator),
		AssetID:        BigIntToHex(assetID),
		ComplianceRoot: BigIntToHex(complianceRoot), // Pure merkle root (no height encoding here)
		TfrAnchor:      BigIntToHex(tfrAnchor),
		Secret:         BigIntToHex(secret),
		PubkeyHash:     BigIntToHex(pubkeyHash),
		Country:        840,
		Age:            25,
		MerkleIndex:    merkleIndex,
		MerkleLeafHash: BigIntToHex(merkleLeafHash),
		MerkleProof:    bigIntsToHex(merkleProof),
	}, nil
}

// mimcHash computes MiMC hash of a single value using gnark-crypto
func mimcHash(value *big.Int) (*big.Int, error) {
	// Convert to field element
	var elem fr.Element
	elem.SetBigInt(value)

	// Create MiMC hasher
	h := mimc.NewMiMC()

	// Hash the value
	h.Write(elem.Marshal())
	digest := h.Sum(nil)

	// Convert back to big.Int
	var resultElem fr.Element
	resultElem.SetBytes(digest)

	return resultElem.BigInt(new(big.Int)), nil
}

// mimcHashMulti computes MiMC hash of multiple values using gnark-crypto
func mimcHashMulti(values ...*big.Int) (*big.Int, error) {
	h := mimc.NewMiMC()

	for _, v := range values {
		var elem fr.Element
		elem.SetBigInt(v)
		h.Write(elem.Marshal())
	}

	digest := h.Sum(nil)

	var resultElem fr.Element
	resultElem.SetBytes(digest)

	return resultElem.BigInt(new(big.Int)), nil
}

// generateMerkleProof creates a valid Merkle proof that hashes to a root
// Returns (siblings, root)
func generateMerkleProof(leafHash *big.Int, index int) ([]*big.Int, *big.Int, error) {
	const depth = 8
	siblings := make([]*big.Int, depth)

	// Generate deterministic siblings based on leaf hash
	currentHash := leafHash

	for i := 0; i < depth; i++ {
		// Determine if we're left (0) or right (1) child at this level
		bit := (index >> i) & 1

		// Generate deterministic sibling
		siblingBytes := sha256.Sum256(append(currentHash.Bytes(), byte(i)))
		sibling := new(big.Int).SetBytes(siblingBytes[:])
		sibling.Mod(sibling, ecc.BLS12_381.ScalarField())
		siblings[i] = sibling

		// Hash current node with sibling to get parent
		var parent *big.Int
		var err error
		if bit == 0 {
			// We're left child: parent = Hash(current || sibling)
			parent, err = mimcHashMulti(currentHash, sibling)
		} else {
			// We're right child: parent = Hash(sibling || current)
			parent, err = mimcHashMulti(sibling, currentHash)
		}
		if err != nil {
			return nil, nil, fmt.Errorf("failed to hash at level %d: %w", i, err)
		}

		currentHash = parent
	}

	// currentHash is now the root
	return siblings, currentHash, nil
}

// bigIntsToHex converts slice of big.Int to hex strings
func bigIntsToHex(vals []*big.Int) []string {
	result := make([]string, len(vals))
	for i, v := range vals {
		result[i] = BigIntToHex(v)
	}
	return result
}

// ToProveRequest converts ValidWitnessData to ProveRequest
func (w *ValidWitnessData) ToProveRequest() *ProveRequest {
	return &ProveRequest{
		ChainSeparator: w.ChainSeparator,
		AssetID:        w.AssetID,
		ComplianceRoot: w.ComplianceRoot,
		TfrAnchor:      w.TfrAnchor,
		Witness: WitnessData{
			Secret:         w.Secret,
			PubkeyHash:     w.PubkeyHash,
			Country:        w.Country,
			Age:            w.Age,
			MerkleProof:    w.MerkleProof,
			MerkleIndex:    w.MerkleIndex,
			MerkleLeafHash: w.MerkleLeafHash,
		},
	}
}

// GenerateInvalidWitness creates witnesses that fail specific constraints
type InvalidWitnessType int

const (
	InvalidSecret InvalidWitnessType = iota // Wrong pubkey_hash
	InvalidAge                              // Age < 18
	InvalidCountry                          // Country != 840
	InvalidMerkleProof                      // Bad Merkle proof
)

// GenerateInvalidWitness creates an invalid witness for testing failure paths
func GenerateInvalidWitness(seed string, invalidType InvalidWitnessType) (*ValidWitnessData, error) {
	// Start with valid witness
	valid, err := GenerateValidWitness(seed)
	if err != nil {
		return nil, err
	}

	// Corrupt based on type
	switch invalidType {
	case InvalidSecret:
		// Change pubkey_hash so MiMC(secret) != pubkey_hash
		h := sha256.Sum256([]byte("corrupt"))
		corruptHash := new(big.Int).SetBytes(h[:])
		valid.PubkeyHash = BigIntToHex(corruptHash)

	case InvalidAge:
		// Set age < 18
		valid.Age = 17

	case InvalidCountry:
		// Set country != 840
		valid.Country = 124 // Canada

	case InvalidMerkleProof:
		// Corrupt first sibling
		h := sha256.Sum256([]byte("corrupt"))
		corruptSibling := new(big.Int).SetBytes(h[:])
		valid.MerkleProof[0] = BigIntToHex(corruptSibling)
	}

	return valid, nil
}

// SerializeWitness converts witness to JSON
func SerializeWitness(w *ValidWitnessData) string {
	// Manual JSON to avoid import cycles
	return fmt.Sprintf(`{
  "chain_separator": "%s",
  "asset_id": "%s",
  "compliance_root": "%s",
  "tfr_anchor": "%s",
  "secret": "%s",
  "pubkey_hash": "%s",
  "country": %d,
  "age": %d,
  "merkle_index": %d,
  "merkle_leaf_hash": "%s",
  "merkle_proof": ["%s"]
}`, w.ChainSeparator, w.AssetID, w.ComplianceRoot, w.TfrAnchor,
		w.Secret, w.PubkeyHash, w.Country, w.Age, w.MerkleIndex,
		w.MerkleLeafHash, hex.EncodeToString([]byte(fmt.Sprintf("%v", w.MerkleProof))))
}
