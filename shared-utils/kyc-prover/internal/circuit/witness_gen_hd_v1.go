// SPDX-License-Identifier: Apache-2.0
package circuit

import (
	"crypto/sha256"
	"fmt"
	"math/big"

	bls12381 "github.com/consensys/gnark-crypto/ecc/bls12-381"
	bls12381fr "github.com/consensys/gnark-crypto/ecc/bls12-381/fr"
	bls12381mimc "github.com/consensys/gnark-crypto/ecc/bls12-381/fr/mimc"
	secp256k1 "github.com/consensys/gnark-crypto/ecc/secp256k1"
)

// ValidWitnessDataHDV1 contains witness data for the KYC-HD v1 circuit.
// Pubkey-only: no master_secret in the witness.
type ValidWitnessDataHDV1 struct {
	// Public inputs
	ChainSeparator string `json:"chain_separator"`
	AssetID        string `json:"asset_id"`
	ComplianceRoot string `json:"compliance_root"`
	TfrAnchor      string `json:"tfr_anchor"`
	OutputKeyHigh  string `json:"output_key_high"` // Upper 128 bits of child x-only key
	OutputKeyLow   string `json:"output_key_low"`  // Lower 128 bits of child x-only key

	// Parent pubkey (enrolled with issuer)
	MasterPubkeyX string `json:"master_pubkey_x"`
	MasterPubkeyY string `json:"master_pubkey_y"`

	// Derivation parameters (new packed format)
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

// packPathVector packs account, change, index into a single 96-bit value
func packPathVector(account, change, index uint32) *big.Int {
	packed := new(big.Int)
	packed.SetUint64(uint64(account))
	packed.Lsh(packed, 32)
	packed.Or(packed, new(big.Int).SetUint64(uint64(change)))
	packed.Lsh(packed, 32)
	packed.Or(packed, new(big.Int).SetUint64(uint64(index)))
	return packed
}

// computeCommitmentV1 computes H("KYCHDV1" || P.X || PathVector || Salt) using batch approach
func computeCommitmentV1(Px *big.Int, pathVector, salt *big.Int) []byte {
	H := bls12381mimc.NewMiMC()

	// Write tag as single field element
	tag := new(big.Int).SetUint64(0x4B594348445631) // "KYCHDV1" in hex
	var tagElem bls12381fr.Element
	tagElem.SetBigInt(tag)
	H.Write(tagElem.Marshal())

	// Write P.X as single field element
	var pxElem bls12381fr.Element
	pxElem.SetBigInt(Px)
	H.Write(pxElem.Marshal())

	// Write path vector as single field element
	var pathElem bls12381fr.Element
	pathElem.SetBigInt(pathVector)
	H.Write(pathElem.Marshal())

	// Write salt as single field element
	var saltElem bls12381fr.Element
	saltElem.SetBigInt(salt)
	H.Write(saltElem.Marshal())

	return H.Sum(nil)
}

// computeDerivationScalarV1 computes the derivation scalar from commitment
func computeDerivationScalarV1(commitment []byte, account, change, index uint32) *big.Int {
	H := bls12381mimc.NewMiMC()

	// Write commitment
	H.Write(commitment)

	// Write individual components for additional binding
	var elem bls12381fr.Element
	elem.SetUint64(uint64(account))
	H.Write(elem.Marshal())

	elem.SetUint64(uint64(change))
	H.Write(elem.Marshal())

	elem.SetUint64(uint64(index))
	H.Write(elem.Marshal())

	digest := H.Sum(nil)
	derivHash := new(big.Int).SetBytes(digest)

	// Reduce modulo secp256k1 order
	return new(big.Int).Mod(derivHash, secp256k1.ID.ScalarField())
}

// computeLeafHashV1 computes leaf hash: MiMC(P.x, P.y)
// Binding both coordinates prevents P/-P ambiguity in the pubkey-only design.
func computeLeafHashV1(Px, Py *big.Int) []byte {
	H := bls12381mimc.NewMiMC()

	var pxElem bls12381fr.Element
	pxElem.SetBigInt(Px)
	H.Write(pxElem.Marshal())

	var pyElem bls12381fr.Element
	pyElem.SetBigInt(Py)
	H.Write(pyElem.Marshal())

	return H.Sum(nil)
}

// computeMerkleRootV1 computes root with optimized path traversal
func computeMerkleRootV1(leaf []byte, pathBits uint8, siblings [][]byte) []byte {
	current := append([]byte(nil), leaf...)

	for i := 0; i < len(siblings); i++ {
		bit := (pathBits >> uint(i)) & 1

		H := bls12381mimc.NewMiMC()
		if bit == 1 {
			// Current is right child
			H.Write(siblings[i])
			H.Write(current)
		} else {
			// Current is left child
			H.Write(current)
			H.Write(siblings[i])
		}
		current = H.Sum(nil)
	}

	return current
}

// GenerateValidWitnessHDV1 creates a valid witness for the alternative circuit
func GenerateValidWitnessHDV1(seed string) (*ValidWitnessDataHDV1, error) {
	// 1. Generate master key pair
	h := sha256.Sum256([]byte(seed + "_master_v1"))
	masterSecret := new(big.Int).SetBytes(h[:])
	masterSecret.Mod(masterSecret, secp256k1.ID.ScalarField())

	// Compute master public key
	var masterPubkeyPoint secp256k1.G1Affine
	g1GenJac, _ := secp256k1.Generators()
	var g1Gen secp256k1.G1Affine
	g1Gen.FromJacobian(&g1GenJac)
	masterPubkeyPoint.ScalarMultiplication(&g1Gen, masterSecret)

	masterPubkeyX := masterPubkeyPoint.X.BigInt(new(big.Int))
	masterPubkeyY := masterPubkeyPoint.Y.BigInt(new(big.Int))

	fmt.Printf("DEBUG [witness_gen_hd_v1]: Master pubkey P = (0x%x, 0x%x)\n", masterPubkeyX, masterPubkeyY)

	// 2. Set derivation parameters
	account := uint32(0)
	change := uint32(0)
	index := uint32(0)
	salt := new(big.Int).SetUint64(12345) // Different salt for v1

	// Pack path into vector
	pathVector := packPathVector(account, change, index)
	fmt.Printf("DEBUG [witness_gen_hd_v1]: PathVector = 0x%x\n", pathVector)

	// 3. Compute derivation commitment (BE format for circuit)
	commitmentBytes := computeCommitmentV1(masterPubkeyX, pathVector, salt)
	commitment := new(big.Int).SetBytes(commitmentBytes)
	fmt.Printf("DEBUG [witness_gen_hd_v1]: Commitment = 0x%064x\n", commitment)

	// 4. Compute derivation scalar (BE format for circuit)
	derivScalar := computeDerivationScalarV1(commitmentBytes, account, change, index)
	fmt.Printf("DEBUG [witness_gen_hd_v1]: DerivScalar = 0x%064x\n", derivScalar)

	// 5. Compute child public key: Q = P + derivScalar·G (BE format)
	var derivPoint secp256k1.G1Affine
	derivPoint.ScalarMultiplication(&g1Gen, derivScalar)

	var childPubkeyPoint secp256k1.G1Affine
	childPubkeyPoint.Add(&masterPubkeyPoint, &derivPoint)

	childPubkeyX := childPubkeyPoint.X.BigInt(new(big.Int))
	childPubkeyY := childPubkeyPoint.Y.BigInt(new(big.Int))

	fmt.Printf("DEBUG [witness_gen_hd_v1]: Child pubkey Q = (0x%x, 0x%x)\n", childPubkeyX, childPubkeyY)

	// 6. Split child x-only key into high/low 128-bit halves
	childXBytes := make([]byte, 32)
	childXRaw := childPubkeyX.Bytes()
	copy(childXBytes[32-len(childXRaw):], childXRaw)
	outputKeyHigh := new(big.Int).SetBytes(childXBytes[0:16])  // upper 128 bits
	outputKeyLow := new(big.Int).SetBytes(childXBytes[16:32])  // lower 128 bits
	fmt.Printf("DEBUG [witness_gen_hd_v1]: OutputKeyHigh = 0x%032x\n", outputKeyHigh)
	fmt.Printf("DEBUG [witness_gen_hd_v1]: OutputKeyLow  = 0x%032x\n", outputKeyLow)

	// 7. Compute leaf hash — MiMC(P.x, P.y) to bind full parent pubkey
	leafBytes := computeLeafHashV1(masterPubkeyX, masterPubkeyY)
	leafHash := new(big.Int).SetBytes(leafBytes)
	fmt.Printf("DEBUG [witness_gen_hd_v1]: LeafHash = 0x%064x\n", leafHash)

	// 8. Generate Merkle proof
	merkleIndex := uint8(42) // Use same index for comparison
	merklePathBits := merkleIndex

	// Generate deterministic siblings
	siblings := make([][]byte, 8)
	siblingsBigInt := make([]*big.Int, 8)
	for i := 0; i < 8; i++ {
		h := sha256.Sum256([]byte(fmt.Sprintf("%s_sibling_v1_%d", seed, i)))
		siblings[i] = h[:]
		siblingsBigInt[i] = new(big.Int).SetBytes(h[:])
		siblingsBigInt[i].Mod(siblingsBigInt[i], bls12381.ID.ScalarField())
		siblings[i] = pack32BE(siblingsBigInt[i])
	}

	// Compute root
	root := computeMerkleRootV1(leafBytes, merklePathBits, siblings)
	complianceRoot := new(big.Int).SetBytes(root)
	fmt.Printf("DEBUG [witness_gen_hd_v1]: ComplianceRoot = 0x%064x\n", complianceRoot)

	// 9. Generate public inputs
	chainSeparator := big.NewInt(0x7bc915) // Slightly different for v1
	assetHash := sha256.Sum256([]byte(seed + "_asset_hd_v1"))
	assetID := new(big.Int).SetBytes(assetHash[:])
	bls12381Modulus := bls12381.ID.ScalarField()
	assetID.Mod(assetID, bls12381Modulus)
	tfrAnchor := big.NewInt(0)

	return &ValidWitnessDataHDV1{
		ChainSeparator:       BigIntToHex(chainSeparator),
		AssetID:              BigIntToHex(assetID),
		ComplianceRoot:       BigIntToHex(complianceRoot),
		TfrAnchor:            BigIntToHex(tfrAnchor),
		OutputKeyHigh:        BigIntToHex(outputKeyHigh),
		OutputKeyLow:         BigIntToHex(outputKeyLow),
		MasterPubkeyX:        BigIntToHex(masterPubkeyX),
		MasterPubkeyY:        BigIntToHex(masterPubkeyY),
		DerivationCommitment: BigIntToHex(commitment),
		PathVector:           BigIntToHex(pathVector),
		Salt:                 BigIntToHex(salt),
		ChildPubkeyX:         BigIntToHex(childPubkeyX),
		ChildPubkeyY:         BigIntToHex(childPubkeyY),
		MerklePathBits:       BigIntToHex(new(big.Int).SetUint64(uint64(merklePathBits))),
		MerkleSiblings:       bigIntsToHex(siblingsBigInt),
	}, nil
}

// ToProveRequestHDV1 converts witness data to prove request
func (w *ValidWitnessDataHDV1) ToProveRequestHDV1() *ProveRequestHDV1 {
	return &ProveRequestHDV1{
		ChainSeparator: w.ChainSeparator,
		AssetID:        w.AssetID,
		ComplianceRoot: w.ComplianceRoot,
		TfrAnchor:      w.TfrAnchor,
		OutputKeyHigh:  w.OutputKeyHigh,
		OutputKeyLow:   w.OutputKeyLow,
		Witness: WitnessDataHDV1{
			MasterPubkeyX:        w.MasterPubkeyX,
			MasterPubkeyY:        w.MasterPubkeyY,
			DerivationCommitment: w.DerivationCommitment,
			PathVector:           w.PathVector,
			Salt:                 w.Salt,
			ChildPubkeyX:         w.ChildPubkeyX,
			ChildPubkeyY:         w.ChildPubkeyY,
			MerklePathBits:       w.MerklePathBits,
			MerkleSiblings:       w.MerkleSiblings,
		},
	}
}

// InvalidWitnessTypeHDV1 defines types of invalid witnesses for testing
type InvalidWitnessTypeHDV1 int

const (
	InvalidMasterKeyHDV1 InvalidWitnessTypeHDV1 = iota
	InvalidDerivationHDV1
	InvalidOutputKeyHDV1
	InvalidMerkleProofHDV1
)

// GenerateInvalidWitnessHDV1 creates an invalid witness for testing
func GenerateInvalidWitnessHDV1(seed string, invalidType InvalidWitnessTypeHDV1) (*ValidWitnessDataHDV1, error) {
	// Start with valid witness
	valid, err := GenerateValidWitnessHDV1(seed)
	if err != nil {
		return nil, err
	}

	// Corrupt based on type
	switch invalidType {
	case InvalidMasterKeyHDV1:
		// Wrong master key relationship
		h := sha256.Sum256([]byte("corrupt_master_v1"))
		corruptX := new(big.Int).SetBytes(h[:])
		valid.MasterPubkeyX = BigIntToHex(corruptX)

	case InvalidDerivationHDV1:
		// Wrong child key
		h := sha256.Sum256([]byte("corrupt_child_v1"))
		corruptX := new(big.Int).SetBytes(h[:])
		valid.ChildPubkeyX = BigIntToHex(corruptX)

	case InvalidOutputKeyHDV1:
		// Corrupt OutputKeyHigh so it doesn't match ChildPubkeyX
		h := sha256.Sum256([]byte("corrupt_output_key_v1"))
		corruptHigh := new(big.Int).SetBytes(h[:16]) // 128-bit value
		valid.OutputKeyHigh = BigIntToHex(corruptHigh)

	case InvalidMerkleProofHDV1:
		// Corrupt first sibling
		h := sha256.Sum256([]byte("corrupt_merkle_v1"))
		corruptSibling := new(big.Int).SetBytes(h[:])
		valid.MerkleSiblings[0] = BigIntToHex(corruptSibling)
	}

	return valid, nil
}