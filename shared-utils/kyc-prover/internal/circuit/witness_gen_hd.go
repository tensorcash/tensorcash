// SPDX-License-Identifier: Apache-2.0
package circuit

import (
	"crypto/sha256"
	"encoding/binary"
	"fmt"
	"hash"
	"math/big"

	bls12381 "github.com/consensys/gnark-crypto/ecc/bls12-381"
	bls12381fr "github.com/consensys/gnark-crypto/ecc/bls12-381/fr"
	bls12381mimc "github.com/consensys/gnark-crypto/ecc/bls12-381/fr/mimc"
	secp256k1 "github.com/consensys/gnark-crypto/ecc/secp256k1"
)

var (
	secp256k1Order = secp256k1.ID.ScalarField()
	secpMontFactor = func() *big.Int {
		v, _ := new(big.Int).SetString("ebf44ecede08dc3bcd46cc6e60927f1fb6f49a530390c6bc81e489ab7e7169b3", 16)
		return v
	}()
)

// ValidWitnessDataHD contains a complete, valid witness for KYC-HD v1
type ValidWitnessDataHD struct {
	// Public inputs
	ChainSeparator string `json:"chain_separator"`
	AssetID        string `json:"asset_id"`
	ComplianceRoot string `json:"compliance_root"`
	TfrAnchor      string `json:"tfr_anchor"`

	// Master key
	MasterSecret  string `json:"master_secret"`
	MasterPubkeyX string `json:"master_pubkey_x"`
	MasterPubkeyY string `json:"master_pubkey_y"`

	// Derivation path
	PathAccount int `json:"path_account"`
	PathChange  int `json:"path_change"`
	PathIndex   int `json:"path_index"`
	Salt        int `json:"salt"`

	// Derived child key
	ChildPubkeyX string `json:"child_pubkey_x"`
	ChildPubkeyY string `json:"child_pubkey_y"`

	// KYC attributes
	Country int `json:"country"`
	Age     int `json:"age"`

	// Merkle proof
	MerkleIndex    int      `json:"merkle_index"`
	MerkleLeafHash string   `json:"merkle_leaf_hash"`
	MerkleProof    []string `json:"merkle_proof"`

	// DEBUG
	DerivDigestDebug     string   `json:"deriv_digest_debug"`
	DerivScalarDebug     string   `json:"deriv_scalar_debug"`
	DerivScalarMontDebug string   `json:"deriv_scalar_mont_debug"`
	MontFixDebug         string   `json:"mont_fix_debug"`
	LeafDigestDebug    string   `json:"leaf_digest_debug"`
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

// pack32BE returns a 32-byte big-endian slice for v
func pack32BE(v *big.Int) []byte {
	out := make([]byte, 32)
	vb := v.Bytes() // big-endian
	copy(out[32-len(vb):], vb)
	return out
}

// writeFE writes a single Fr element (value = v) into MiMC
// The value should be small (0-255 for bytes) and is written as a field element
func writeFE(H hash.Hash, v *big.Int) {
	var e bls12381fr.Element
	e.SetBigInt(v) // numeric value v in Fr
	// Write the field element directly (not marshaled bytes)
	H.Write(e.Marshal())
}

// writeU32BytesAsFEs writes a uint32 as 4 bytes (big-endian), each absorbed as separate field element
func writeU32BytesAsFEs(H hash.Hash, u uint32) {
	var b [4]byte
	binary.BigEndian.PutUint32(b[:], u)
	for i := 0; i < 4; i++ {
		writeFE(H, new(big.Int).SetUint64(uint64(b[i])))
	}
}

// derivationDigestBytesAsElems computes MiMC hash with each byte absorbed as separate field element
// Matches circuit's byte-by-byte Write() calls
func derivationDigestBytesAsElems(Px *big.Int, acct, change, index, salt uint32) []byte {
	H := bls12381mimc.NewMiMC()

	// Tag bytes - each byte as one FE
	for _, b := range []byte("KYC-HD-v1") {
		writeFE(H, new(big.Int).SetUint64(uint64(b)))
	}

	// Px as 32B big-endian; each byte -> one FE
	px := pack32BE(Px)
	for i := 0; i < 32; i++ {
		writeFE(H, new(big.Int).SetUint64(uint64(px[i])))
	}

	// Path bytes (each u32 as 4B BE; each byte -> one FE)
	writeU32BytesAsFEs(H, acct)
	writeU32BytesAsFEs(H, change)
	writeU32BytesAsFEs(H, index)
	writeU32BytesAsFEs(H, salt)

	// Output digest (field element canonical bytes, 32B BE)
	return H.Sum(nil)
}

// parentMiMC computes MiMC(left || right) where each is a canonical 32B Fr element
func parentMiMC(left, right []byte) []byte {
	H := bls12381mimc.NewMiMC()
	H.Write(left)
	H.Write(right)
	return H.Sum(nil)
}

// rootLSB computes Merkle root using LSB-first bit order (matches circuit)
func rootLSB(leaf []byte, index uint32, siblings [][]byte) []byte {
	cur := append([]byte(nil), leaf...)
	for i := 0; i < len(siblings); i++ {
		if ((index >> uint(i)) & 1) == 1 {
			// bit==1: leaf was RIGHT → sibling is LEFT
			cur = parentMiMC(siblings[i], cur)
		} else {
			// bit==0: leaf was LEFT → sibling is RIGHT
			cur = parentMiMC(cur, siblings[i])
		}
	}
	return cur
}

// rootMSB computes Merkle root using MSB-first bit order (for debugging)
func rootMSB(leaf []byte, index uint32, siblings [][]byte) []byte {
	cur := append([]byte(nil), leaf...)
	for i := len(siblings) - 1; i >= 0; i-- {
		bit := (index >> uint(i)) & 1
		if bit == 1 {
			cur = parentMiMC(siblings[len(siblings)-1-i], cur)
		} else {
			cur = parentMiMC(cur, siblings[len(siblings)-1-i])
		}
	}
	return cur
}

// pxOnlyDigestBytesAsElems computes MiMC(Px[32-bytes-as-FEs])
func pxOnlyDigestBytesAsElems(Px *big.Int) []byte {
	H := bls12381mimc.NewMiMC()

	// Px as 32 bytes -> 32 FE (each byte absorbed separately)
	px := pack32BE(Px)
	for i := 0; i < 32; i++ {
		writeFE(H, new(big.Int).SetUint64(uint64(px[i])))
	}

	return H.Sum(nil)
}

// leafDigestBytesAsElems computes leaf hash: MiMC(Px[32-bytes-as-FEs] || Country[4-bytes-as-FEs] || Age[4-bytes-as-FEs])
func leafDigestBytesAsElems(Px *big.Int, country, age uint32) []byte {
	H := bls12381mimc.NewMiMC()

	// Px as 32 bytes -> 32 FE (each byte absorbed separately)
	px := pack32BE(Px)
	for i := 0; i < 32; i++ {
		writeFE(H, new(big.Int).SetUint64(uint64(px[i])))
	}

	// Country as 4 bytes big-endian, each byte -> one FE (matches circuit writeU32BE)
	writeU32BytesAsFEs(H, country)

	// Age as 4 bytes big-endian, each byte -> one FE (matches circuit writeU32BE)
	writeU32BytesAsFEs(H, age)

	return H.Sum(nil)
}

// GenerateValidWitnessHD creates a deterministic valid witness for KYC-HD v1
func GenerateValidWitnessHD(seed string) (*ValidWitnessDataHD, error) {
	// 1. Generate master key pair (for testing - normally issuer provides P only)
	h := sha256.Sum256([]byte(seed + "_master"))
	masterSecret := new(big.Int).SetBytes(h[:])
	masterSecret.Mod(masterSecret, secp256k1Order)

	// Compute P = secret·G (only for test witness generation)
	var masterPubkeyPoint secp256k1.G1Affine
	g1GenJac, _ := secp256k1.Generators()
	var g1Gen secp256k1.G1Affine
	g1Gen.FromJacobian(&g1GenJac)
	masterPubkeyPoint.ScalarMultiplication(&g1Gen, masterSecret)

	masterPubkeyX := masterPubkeyPoint.X.BigInt(new(big.Int))
	masterPubkeyY := masterPubkeyPoint.Y.BigInt(new(big.Int))

	// 2. Set derivation path
	pathAccount := 0 // m/44'/0'/0'
	pathChange := 0  // External addresses
	pathIndex := 0   // First address
	salt := 42       // Deterministic salt for testing

	// 3. Derive child key: h = MiMC(tag || Px_bytes || path_u32s || salt_u32)
	// Use byte-as-element absorption to match circuit exactly

	fmt.Printf("DEBUG [witness_gen_hd]: Master pubkey P = (0x%x, 0x%x)\n", masterPubkeyX, masterPubkeyY)

	// Build preimage for logging
	var preimage []byte
	preimage = append(preimage, []byte("KYC-HD-v1")...)
	pxBytes := pack32BE(masterPubkeyX)
	preimage = append(preimage, pxBytes...)
	var tmp [4]byte
	binary.BigEndian.PutUint32(tmp[:], uint32(pathAccount))
	preimage = append(preimage, tmp[:]...)
	binary.BigEndian.PutUint32(tmp[:], uint32(pathChange))
	preimage = append(preimage, tmp[:]...)
	binary.BigEndian.PutUint32(tmp[:], uint32(pathIndex))
	preimage = append(preimage, tmp[:]...)
	binary.BigEndian.PutUint32(tmp[:], uint32(salt))
	preimage = append(preimage, tmp[:]...)

	fmt.Printf("DEBUG [witness_gen_hd]: DERIVATION preimage bytes (57 total): 0x%x\n", preimage)
	fmt.Printf("DEBUG [witness_gen_hd]: Path: account=%d, change=%d, index=%d, salt=%d\n", pathAccount, pathChange, pathIndex, salt)

	// Compute MiMC with byte-as-element absorption (matches circuit)
	digest := derivationDigestBytesAsElems(masterPubkeyX, uint32(pathAccount), uint32(pathChange), uint32(pathIndex), uint32(salt))
	derivationHash := new(big.Int).SetBytes(digest)

	fmt.Printf("DEBUG [witness_gen_hd]: DERIVATION mimc digest: 0x%x\n", digest)
	fmt.Printf("DEBUG [witness_gen_hd]: DBG r_D: %064x\n", derivationHash)

	// Convert digest to integer and reduce mod secp256k1 order
	derivationScalarSecp := new(big.Int).Mod(derivationHash, secp256k1Order)

	fmt.Printf("DEBUG [witness_gen_hd]: DERIVATION h (mod n): 0x%064x\n", derivationScalarSecp)
	fmt.Printf("DEBUG [witness_gen_hd]: DBG h_D: %064x\n", derivationScalarSecp)

	// Circuit expects gnark's Montgomery-form scalar for hMont
	derivationScalarMont := new(big.Int).Mul(derivationScalarSecp, secpMontFactor)
	derivationScalarMont.Mod(derivationScalarMont, secp256k1Order)
	fmt.Printf("DEBUG [witness_gen_hd]: DBG h_Mont: %064x\n", derivationScalarMont)

	montFix := new(big.Int).ModInverse(secpMontFactor, secp256k1Order)
	if montFix == nil {
		return nil, fmt.Errorf("montgomery factor has no inverse mod n")
	}
	fmt.Printf("DEBUG [witness_gen_hd]: DBG mont_fix: %064x\n", montFix)

	// Compute h·G (secp256k1) - reuse g1Gen from master key computation
	var hG secp256k1.G1Affine
	hG.ScalarMultiplication(&g1Gen, derivationScalarSecp)

	hGx := hG.X.BigInt(new(big.Int))
	hGy := hG.Y.BigInt(new(big.Int))
	fmt.Printf("DEBUG [witness_gen_hd]: h·G (R) = (0x%064x, 0x%064x)\n", hGx, hGy)

	// Compute child pubkey Q = P + h·G
	var childPubkeyPoint secp256k1.G1Affine
	childPubkeyPoint.Add(&masterPubkeyPoint, &hG)

	childPubkeyX := childPubkeyPoint.X.BigInt(new(big.Int))
	childPubkeyY := childPubkeyPoint.Y.BigInt(new(big.Int))

	fmt.Printf("DEBUG [witness_gen_hd]: Qcalc.x = 0x%064x\n", childPubkeyX)
	fmt.Printf("DEBUG [witness_gen_hd]: Qcalc.y = 0x%064x\n", childPubkeyY)

	// 4. Set compliance attributes
	country := 840 // USA
	age := 25      // >= 18

	// 5. Compute merkle_leaf_hash = MiMC(master_pubkey_x_bytes || country || age)
	// Use byte-as-element absorption (32 bytes of P.x + country + age as FEs)

	// DEBUG: Compute Px-only hash first
	pxOnlyBytes := pxOnlyDigestBytesAsElems(masterPubkeyX)
	pxOnlyHash := new(big.Int).SetBytes(pxOnlyBytes)
	fmt.Printf("DEBUG [witness_gen_hd]: Px-only hash  = 0x%064x\n", pxOnlyHash)

	leafBytes := leafDigestBytesAsElems(masterPubkeyX, uint32(country), uint32(age))
	merkleLeafHash := new(big.Int).SetBytes(leafBytes)

	fmt.Printf("DEBUG [witness_gen_hd]: LEAF(preimage) = Px=%064x country=%d age=%d\n", masterPubkeyX, country, age)
	fmt.Printf("DEBUG [witness_gen_hd]: LEAF(digest)   = 0x%064x\n", merkleLeafHash)
	fmt.Printf("DEBUG [witness_gen_hd]: DBG leaf_D: %064x\n", merkleLeafHash)

	// 6. Generate Merkle proof
	merkleIndex := 42
	merkleProof, complianceRoot, err := generateMerkleProof(merkleLeafHash, merkleIndex)
	if err != nil {
		return nil, fmt.Errorf("failed to generate merkle proof: %w", err)
	}

	// Convert siblings to byte slices for root computation
	siblingsBytes := make([][]byte, len(merkleProof))
	for i, sib := range merkleProof {
		siblingsBytes[i] = pack32BE(sib)
	}

	// Compute per-level nodes for debugging (LSB-first)
	nodesBytes := make([][]byte, len(merkleProof))
	cur := leafBytes
	for i := 0; i < len(siblingsBytes); i++ {
		if ((uint32(merkleIndex) >> uint(i)) & 1) == 1 {
			// bit==1: leaf was RIGHT → sibling is LEFT
			cur = parentMiMC(siblingsBytes[i], cur)
		} else {
			// bit==0: leaf was LEFT → sibling is RIGHT
			cur = parentMiMC(cur, siblingsBytes[i])
		}
		nodesBytes[i] = cur
		fmt.Printf("DEBUG [witness_gen_hd]: NodeDebug[%d] = 0x%064x\n", i, new(big.Int).SetBytes(nodesBytes[i]))
	}

	// Compute roots using both LSB and MSB to debug bit order
	rootLSBBytes := rootLSB(leafBytes, uint32(merkleIndex), siblingsBytes)
	rootMSBBytes := rootMSB(leafBytes, uint32(merkleIndex), siblingsBytes)
	rootLSBInt := new(big.Int).SetBytes(rootLSBBytes)
	rootMSBInt := new(big.Int).SetBytes(rootMSBBytes)

	fmt.Printf("DEBUG [witness_gen_hd]: DBG root(LSB): %064x\n", rootLSBInt)
	fmt.Printf("DEBUG [witness_gen_hd]: DBG root(MSB): %064x\n", rootMSBInt)
	fmt.Printf("DEBUG [witness_gen_hd]: DBG root(pub): %064x\n", complianceRoot)

	// 7. Generate public inputs
	chainSeparator := big.NewInt(0x7bc914)
	assetHash := sha256.Sum256([]byte(seed + "_asset_hd"))
	assetID := new(big.Int).SetBytes(assetHash[:])
	bls12381Modulus := bls12381.ID.ScalarField()
	assetID.Mod(assetID, bls12381Modulus)
	tfrAnchor := big.NewInt(0)

	// Convert per-level node bytes to hex strings
	nodeDebugHex := make([]string, len(nodesBytes))
	for i, nodeBytes := range nodesBytes {
		nodeDebugHex[i] = BigIntToHex(new(big.Int).SetBytes(nodeBytes))
	}

	// Compute master mul debug taps: p·G using same library as circuit
	// This verifies the master secret is correctly encoded
	var masterMulDebug secp256k1.G1Affine
	masterMulDebug.ScalarMultiplication(&g1Gen, masterSecret)
	masterMulDebugX := masterMulDebug.X.BigInt(new(big.Int))
	masterMulDebugY := masterMulDebug.Y.BigInt(new(big.Int))

	fmt.Printf("DEBUG [witness_gen_hd]: p·G debug = (0x%064x, 0x%064x)\n", masterMulDebugX, masterMulDebugY)
	fmt.Printf("DEBUG [witness_gen_hd]: Master P  = (0x%064x, 0x%064x)\n", masterPubkeyX, masterPubkeyY)

	// Compute child derivation debug taps: R = h·G and Q - P
	var Rpt secp256k1.G1Affine
	Rpt.ScalarMultiplication(&g1Gen, derivationScalarSecp)
	rDebugX := Rpt.X.BigInt(new(big.Int))
	rDebugY := Rpt.Y.BigInt(new(big.Int))

	var negP secp256k1.G1Affine
	negP.Neg(&masterPubkeyPoint)
	var QmP secp256k1.G1Affine
	QmP.Add(&childPubkeyPoint, &negP)
	qmPDebugX := QmP.X.BigInt(new(big.Int))
	qmPDebugY := QmP.Y.BigInt(new(big.Int))

	fmt.Printf("DEBUG [witness_gen_hd]: R (h·G)   = (0x%064x, 0x%064x)\n", rDebugX, rDebugY)
	fmt.Printf("DEBUG [witness_gen_hd]: Q - P     = (0x%064x, 0x%064x)\n", qmPDebugX, qmPDebugY)

	return &ValidWitnessDataHD{
		ChainSeparator: BigIntToHex(chainSeparator),
		AssetID:        BigIntToHex(assetID),
		ComplianceRoot: BigIntToHex(complianceRoot),
		TfrAnchor:      BigIntToHex(tfrAnchor),
		MasterSecret:   BigIntToHex(masterSecret),
		MasterPubkeyX:  BigIntToHex(masterPubkeyX),
		MasterPubkeyY:  BigIntToHex(masterPubkeyY),
		PathAccount:    pathAccount,
		PathChange:     pathChange,
		PathIndex:      pathIndex,
		Salt:           salt,
		ChildPubkeyX:   BigIntToHex(childPubkeyX),
		ChildPubkeyY:   BigIntToHex(childPubkeyY),
		Country:        country,
		Age:            age,
		MerkleIndex:        merkleIndex,
		MerkleLeafHash:     BigIntToHex(merkleLeafHash),
		MerkleProof:        bigIntsToHex(merkleProof),
		DerivDigestDebug:   BigIntToHex(derivationHash),
		DerivScalarDebug:     BigIntToHex(derivationScalarSecp),
		DerivScalarMontDebug: BigIntToHex(derivationScalarMont),
		MontFixDebug:         BigIntToHex(montFix),
		LeafDigestDebug:    BigIntToHex(merkleLeafHash),
		PxOnlyHashDebug:    BigIntToHex(pxOnlyHash),
		RootFromProofDebug: BigIntToHex(rootLSBInt),
		NodeDebug:          nodeDebugHex,
		MasterMulDebugX:    BigIntToHex(masterMulDebugX),
		MasterMulDebugY:    BigIntToHex(masterMulDebugY),
		RDebugX:            BigIntToHex(rDebugX),
		RDebugY:            BigIntToHex(rDebugY),
		QmPDebugX:          BigIntToHex(qmPDebugX),
		QmPDebugY:          BigIntToHex(qmPDebugY),
		RDebugBLS:          BigIntToHex(derivationHash),
		HDebugSecp:         BigIntToHex(derivationScalarSecp),
	}, nil
}

// ToProveRequestHD converts ValidWitnessDataHD to ProveRequestHD
func (w *ValidWitnessDataHD) ToProveRequestHD() *ProveRequestHD {
	return &ProveRequestHD{
		ChainSeparator: w.ChainSeparator,
		AssetID:        w.AssetID,
		ComplianceRoot: w.ComplianceRoot,
		TfrAnchor:      w.TfrAnchor,
		Witness: WitnessDataHD{
			MasterSecret:   w.MasterSecret,
			MasterPubkeyX:  w.MasterPubkeyX,
			MasterPubkeyY:  w.MasterPubkeyY,
			PathAccount:    w.PathAccount,
			PathChange:     w.PathChange,
			PathIndex:      w.PathIndex,
			Salt:           w.Salt,
			ChildPubkeyX:   w.ChildPubkeyX,
			ChildPubkeyY:   w.ChildPubkeyY,
			Country:        w.Country,
			Age:            w.Age,
			MerkleProof:        w.MerkleProof,
			MerkleIndex:        w.MerkleIndex,
			MerkleLeafHash:     w.MerkleLeafHash,
			DerivDigestDebug:   w.DerivDigestDebug,
		DerivScalarDebug:     w.DerivScalarDebug,
		DerivScalarMontDebug: w.DerivScalarMontDebug,
		MontFixDebug:         w.MontFixDebug,
			LeafDigestDebug:    w.LeafDigestDebug,
			RootFromProofDebug: w.RootFromProofDebug,
			NodeDebug:          w.NodeDebug,
			MasterMulDebugX:    w.MasterMulDebugX,
			MasterMulDebugY:    w.MasterMulDebugY,
			RDebugX:            w.RDebugX,
			RDebugY:            w.RDebugY,
			QmPDebugX:          w.QmPDebugX,
			QmPDebugY:          w.QmPDebugY,
			RDebugBLS:          w.RDebugBLS,
			HDebugSecp:         w.HDebugSecp,
		},
	}
}

// InvalidWitnessTypeHD defines types of invalid witnesses for testing
type InvalidWitnessTypeHD int

const (
	InvalidMasterKeyHD InvalidWitnessTypeHD = iota // Wrong master key relationship
	InvalidDerivationHD                             // Wrong child key derivation
	InvalidAgeHD                                    // Age < 18
	InvalidCountryHD                                // Country != 840
	InvalidMerkleProofHD                            // Bad Merkle proof
)

// GenerateInvalidWitnessHD creates an invalid witness for testing
func GenerateInvalidWitnessHD(seed string, invalidType InvalidWitnessTypeHD) (*ValidWitnessDataHD, error) {
	// Start with valid witness
	valid, err := GenerateValidWitnessHD(seed)
	if err != nil {
		return nil, err
	}

	// Corrupt based on type
	switch invalidType {
	case InvalidMasterKeyHD:
		// Change master pubkey so master_pubkey != master_secret · G
		h := sha256.Sum256([]byte("corrupt_master"))
		corruptX := new(big.Int).SetBytes(h[:])
		valid.MasterPubkeyX = BigIntToHex(corruptX)

	case InvalidDerivationHD:
		// Change child pubkey so Q != P + h·G
		h := sha256.Sum256([]byte("corrupt_child"))
		corruptX := new(big.Int).SetBytes(h[:])
		valid.ChildPubkeyX = BigIntToHex(corruptX)

	case InvalidAgeHD:
		valid.Age = 17

	case InvalidCountryHD:
		valid.Country = 124 // Canada

	case InvalidMerkleProofHD:
		h := sha256.Sum256([]byte("corrupt_merkle"))
		corruptSibling := new(big.Int).SetBytes(h[:])
		valid.MerkleProof[0] = BigIntToHex(corruptSibling)
	}

	return valid, nil
}
