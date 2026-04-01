// SPDX-License-Identifier: Apache-2.0
package circuit

import (
	"bytes"
	"fmt"
	"math/big"

	"github.com/consensys/gnark-crypto/ecc"
	"github.com/consensys/gnark/backend/groth16"
	"github.com/consensys/gnark/backend/witness"
	"github.com/consensys/gnark/constraint"
	"github.com/consensys/gnark/frontend"
	"github.com/consensys/gnark/frontend/cs/r1cs"
	"github.com/consensys/gnark/std/math/emulated"
)

// ProverHD handles proof generation and verification for KYC-HD v1
type ProverHD struct {
	pk  groth16.ProvingKey
	vk  groth16.VerifyingKey
	ccs constraint.ConstraintSystem
}

// NewProverHD creates a new prover instance for KYC-HD v1
func NewProverHD(provingKeyData, verificationKeyData []byte) (*ProverHD, error) {
	pk, err := LoadProvingKey(provingKeyData)
	if err != nil {
		return nil, err
	}

	vk, err := LoadVerificationKey(verificationKeyData)
	if err != nil {
		return nil, err
	}

	// Compile circuit (needed for proving)
	var circuit TensorCashKYCCircuitHD
	ccs, err := frontend.Compile(ecc.BLS12_381.ScalarField(), r1cs.NewBuilder, &circuit)
	if err != nil {
		return nil, fmt.Errorf("circuit compilation failed: %w", err)
	}

	return &ProverHD{
		pk:  pk,
		vk:  vk,
		ccs: ccs,
	}, nil
}

// ProveHD generates a proof for the given request
func (p *ProverHD) ProveHD(req *ProveRequestHD) (*ProveResponseHD, error) {
	// Parse public inputs
	chainSeparator, err := HexToBigInt(req.ChainSeparator)
	if err != nil {
		return nil, fmt.Errorf("invalid chain_separator: %w", err)
	}

	assetID, err := HexToBigInt(req.AssetID)
	if err != nil {
		return nil, fmt.Errorf("invalid asset_id: %w", err)
	}

	complianceRoot, err := HexToBigInt(req.ComplianceRoot)
	if err != nil {
		return nil, fmt.Errorf("invalid compliance_root: %w", err)
	}

	tfrAnchor, err := HexToBigInt(req.TfrAnchor)
	if err != nil {
		return nil, fmt.Errorf("invalid tfr_anchor: %w", err)
	}

	// Parse master key
	masterSecret, err := HexToBigInt(req.Witness.MasterSecret)
	if err != nil {
		return nil, fmt.Errorf("invalid master_secret: %w", err)
	}

	masterPubkeyX, err := HexToBigInt(req.Witness.MasterPubkeyX)
	if err != nil {
		return nil, fmt.Errorf("invalid master_pubkey_x: %w", err)
	}

	masterPubkeyY, err := HexToBigInt(req.Witness.MasterPubkeyY)
	if err != nil {
		return nil, fmt.Errorf("invalid master_pubkey_y: %w", err)
	}

	// Parse child key
	childPubkeyX, err := HexToBigInt(req.Witness.ChildPubkeyX)
	if err != nil {
		return nil, fmt.Errorf("invalid child_pubkey_x: %w", err)
	}

	childPubkeyY, err := HexToBigInt(req.Witness.ChildPubkeyY)
	if err != nil {
		return nil, fmt.Errorf("invalid child_pubkey_y: %w", err)
	}

	// Parse merkle proof
	merkleLeafHash, err := HexToBigInt(req.Witness.MerkleLeafHash)
	if err != nil {
		return nil, fmt.Errorf("invalid merkle_leaf_hash: %w", err)
	}

	if len(req.Witness.MerkleProof) != MerkleTreeDepthHD() {
		return nil, fmt.Errorf("invalid merkle proof length: expected %d, got %d",
			MerkleTreeDepthHD(), len(req.Witness.MerkleProof))
	}

	var merkleProof [8]frontend.Variable
	for i, hexStr := range req.Witness.MerkleProof {
		val, err := HexToBigInt(hexStr)
		if err != nil {
			return nil, fmt.Errorf("invalid merkle proof[%d]: %w", i, err)
		}
		merkleProof[i] = val
	}

	// Parse debug fields
	derivDigestDebug, err := HexToBigInt(req.Witness.DerivDigestDebug)
	if err != nil {
		return nil, fmt.Errorf("invalid deriv_digest_debug: %w", err)
	}
	derivScalarDebug, err := HexToBigInt(req.Witness.DerivScalarDebug)
	if err != nil {
		return nil, fmt.Errorf("invalid deriv_scalar_debug: %w", err)
	}
	derivScalarMontDebug, err := HexToBigInt(req.Witness.DerivScalarMontDebug)
	if err != nil {
		return nil, fmt.Errorf("invalid deriv_scalar_mont_debug: %w", err)
	}
	montFixDebug, err := HexToBigInt(req.Witness.MontFixDebug)
	if err != nil {
		return nil, fmt.Errorf("invalid mont_fix_debug: %w", err)
	}
	leafDigestDebug, err := HexToBigInt(req.Witness.LeafDigestDebug)
	if err != nil {
		return nil, fmt.Errorf("invalid leaf_digest_debug: %w", err)
	}
	pxOnlyHashDebug, err := HexToBigInt(req.Witness.PxOnlyHashDebug)
	if err != nil {
		return nil, fmt.Errorf("invalid px_only_hash_debug: %w", err)
	}
	rootFromProofDebug, err := HexToBigInt(req.Witness.RootFromProofDebug)
	if err != nil {
		return nil, fmt.Errorf("invalid root_from_proof_debug: %w", err)
	}

	// Parse NodeDebug array
	if len(req.Witness.NodeDebug) != MerkleTreeDepthHD() {
		return nil, fmt.Errorf("invalid node_debug length: expected %d, got %d",
			MerkleTreeDepthHD(), len(req.Witness.NodeDebug))
	}

	var nodeDebug [8]frontend.Variable
	for i, hexStr := range req.Witness.NodeDebug {
		val, err := HexToBigInt(hexStr)
		if err != nil {
			return nil, fmt.Errorf("invalid node_debug[%d]: %w", i, err)
		}
		nodeDebug[i] = val
	}

	// Parse MasterMulDebug taps
	masterMulDebugX, err := HexToBigInt(req.Witness.MasterMulDebugX)
	if err != nil {
		return nil, fmt.Errorf("invalid master_mul_debug_x: %w", err)
	}
	masterMulDebugY, err := HexToBigInt(req.Witness.MasterMulDebugY)
	if err != nil {
		return nil, fmt.Errorf("invalid master_mul_debug_y: %w", err)
	}

	// Parse R and Q-P debug taps
	rDebugX, err := HexToBigInt(req.Witness.RDebugX)
	if err != nil {
		return nil, fmt.Errorf("invalid r_debug_x: %w", err)
	}
	rDebugY, err := HexToBigInt(req.Witness.RDebugY)
	if err != nil {
		return nil, fmt.Errorf("invalid r_debug_y: %w", err)
	}
	qmPDebugX, err := HexToBigInt(req.Witness.QmPDebugX)
	if err != nil {
		return nil, fmt.Errorf("invalid qmp_debug_x: %w", err)
	}
	qmPDebugY, err := HexToBigInt(req.Witness.QmPDebugY)
	if err != nil {
		return nil, fmt.Errorf("invalid qmp_debug_y: %w", err)
	}

	// Parse r and h debug pins
	rDebugBLS, err := HexToBigInt(req.Witness.RDebugBLS)
	if err != nil {
		return nil, fmt.Errorf("invalid r_debug_bls: %w", err)
	}
	hDebugSecp, err := HexToBigInt(req.Witness.HDebugSecp)
	if err != nil {
		return nil, fmt.Errorf("invalid h_debug_secp: %w", err)
	}

	// Create witness assignment
	assignment := TensorCashKYCCircuitHD{
		ChainSeparator: chainSeparator,
		AssetID:        assetID,
		ComplianceRoot: complianceRoot,
		TfrAnchor:      tfrAnchor,
		MasterSecret:   emulated.ValueOf[Secp256k1Fr](masterSecret),
		MasterPubkeyX:  emulated.ValueOf[Secp256k1Fp](masterPubkeyX),
		MasterPubkeyY:  emulated.ValueOf[Secp256k1Fp](masterPubkeyY),
		PathAccount:    req.Witness.PathAccount,
		PathChange:     req.Witness.PathChange,
		PathIndex:      req.Witness.PathIndex,
		Salt:           req.Witness.Salt,
		ChildPubkeyX:     emulated.ValueOf[Secp256k1Fp](childPubkeyX),
		ChildPubkeyY:     emulated.ValueOf[Secp256k1Fp](childPubkeyY),
		Country:          req.Witness.Country,
		Age:              req.Witness.Age,
		MerkleProof:        merkleProof,
		MerkleIndex:        req.Witness.MerkleIndex,
		MerkleLeafHash:     merkleLeafHash,
		DerivDigestDebug:   derivDigestDebug,
		DerivScalarDebug:   emulated.ValueOf[Secp256k1Fr](derivScalarDebug),
		HMontDebug:         emulated.ValueOf[Secp256k1Fr](derivScalarMontDebug),
		MontFixDebug:       emulated.ValueOf[Secp256k1Fr](montFixDebug),
		LeafDigestDebug:    leafDigestDebug,
		PxOnlyHashDebug:    pxOnlyHashDebug,
		RootFromProofDebug: rootFromProofDebug,
		NodeDebug:          nodeDebug,
		MasterMulDebugX:    emulated.ValueOf[Secp256k1Fp](masterMulDebugX),
		MasterMulDebugY:    emulated.ValueOf[Secp256k1Fp](masterMulDebugY),
		RDebugX:            emulated.ValueOf[Secp256k1Fp](rDebugX),
		RDebugY:            emulated.ValueOf[Secp256k1Fp](rDebugY),
		QmPDebugX:          emulated.ValueOf[Secp256k1Fp](qmPDebugX),
		QmPDebugY:          emulated.ValueOf[Secp256k1Fp](qmPDebugY),
		RDebugBLS:          rDebugBLS,
		HDebugSecp:         emulated.ValueOf[Secp256k1Fr](hDebugSecp),
	}

	// Create witness
	w, err := frontend.NewWitness(&assignment, ecc.BLS12_381.ScalarField())
	if err != nil {
		return nil, fmt.Errorf("failed to create witness: %w", err)
	}

	// Generate proof
	proof, err := groth16.Prove(p.ccs, p.pk, w)
	if err != nil {
		return nil, fmt.Errorf("proof generation failed: %w", err)
	}

	// Serialize proof
	var proofBuf bytes.Buffer
	_, err = proof.WriteTo(&proofBuf)
	if err != nil {
		return nil, fmt.Errorf("proof serialization failed: %w", err)
	}

	// Serialize public inputs manually (4 field elements, 32 bytes each = 128 bytes)
	// This matches v1 format for consensus compatibility
	publicInputs := make([]byte, 128)
	copy(publicInputs[0:32], padTo32Bytes(chainSeparator.Bytes()))
	copy(publicInputs[32:64], padTo32Bytes(assetID.Bytes()))
	copy(publicInputs[64:96], padTo32Bytes(complianceRoot.Bytes()))
	copy(publicInputs[96:128], padTo32Bytes(tfrAnchor.Bytes()))

	return &ProveResponseHD{
		ProofHex:        "0x" + fmt.Sprintf("%x", proofBuf.Bytes()),
		PublicInputsHex: "0x" + fmt.Sprintf("%x", publicInputs),
		Success:         true,
	}, nil
}

// ProveRawHD generates a proof and returns raw groth16.Proof (for custom serialization)
func (p *ProverHD) ProveRawHD(req *ProveRequestHD) (groth16.Proof, witness.Witness, error) {
	// Reuse ProveHD logic but return raw proof
	resp, err := p.ProveHD(req)
	if err != nil {
		return nil, nil, err
	}

	// Deserialize proof back
	proofBytes, err := hexToBytes(resp.ProofHex)
	if err != nil {
		return nil, nil, err
	}

	proof := groth16.NewProof(ecc.BLS12_381)
	_, err = proof.ReadFrom(bytes.NewReader(proofBytes))
	if err != nil {
		return nil, nil, err
	}

	// Create witness for verification
	chainSeparator, _ := HexToBigInt(req.ChainSeparator)
	assetID, _ := HexToBigInt(req.AssetID)
	complianceRoot, _ := HexToBigInt(req.ComplianceRoot)
	tfrAnchor, _ := HexToBigInt(req.TfrAnchor)

	masterSecret, _ := HexToBigInt(req.Witness.MasterSecret)
	masterPubkeyX, _ := HexToBigInt(req.Witness.MasterPubkeyX)
	masterPubkeyY, _ := HexToBigInt(req.Witness.MasterPubkeyY)
	childPubkeyX, _ := HexToBigInt(req.Witness.ChildPubkeyX)
	childPubkeyY, _ := HexToBigInt(req.Witness.ChildPubkeyY)
	merkleLeafHash, _ := HexToBigInt(req.Witness.MerkleLeafHash)

	var merkleProof [8]frontend.Variable
	for i, hexStr := range req.Witness.MerkleProof {
		merkleProof[i], _ = HexToBigInt(hexStr)
	}

	derivDigestDebug, _ := HexToBigInt(req.Witness.DerivDigestDebug)
	derivScalarDebug, _ := HexToBigInt(req.Witness.DerivScalarDebug)
	derivScalarMontDebug, _ := HexToBigInt(req.Witness.DerivScalarMontDebug)
	montFixDebug, _ := HexToBigInt(req.Witness.MontFixDebug)
	leafDigestDebug, _ := HexToBigInt(req.Witness.LeafDigestDebug)
	pxOnlyHashDebug, _ := HexToBigInt(req.Witness.PxOnlyHashDebug)
	rootFromProofDebug, _ := HexToBigInt(req.Witness.RootFromProofDebug)

	var nodeDebug [8]frontend.Variable
	for i, hexStr := range req.Witness.NodeDebug {
		nodeDebug[i], _ = HexToBigInt(hexStr)
	}

	masterMulDebugX, _ := HexToBigInt(req.Witness.MasterMulDebugX)
	masterMulDebugY, _ := HexToBigInt(req.Witness.MasterMulDebugY)
	rDebugX, _ := HexToBigInt(req.Witness.RDebugX)
	rDebugY, _ := HexToBigInt(req.Witness.RDebugY)
	qmPDebugX, _ := HexToBigInt(req.Witness.QmPDebugX)
	qmPDebugY, _ := HexToBigInt(req.Witness.QmPDebugY)
	rDebugBLS, _ := HexToBigInt(req.Witness.RDebugBLS)
	hDebugSecp, _ := HexToBigInt(req.Witness.HDebugSecp)

	assignment := TensorCashKYCCircuitHD{
		ChainSeparator: chainSeparator,
		AssetID:        assetID,
		ComplianceRoot: complianceRoot,
		TfrAnchor:      tfrAnchor,
		MasterSecret:   emulated.ValueOf[Secp256k1Fr](masterSecret),
		MasterPubkeyX:  emulated.ValueOf[Secp256k1Fp](masterPubkeyX),
		MasterPubkeyY:  emulated.ValueOf[Secp256k1Fp](masterPubkeyY),
		PathAccount:    req.Witness.PathAccount,
		PathChange:     req.Witness.PathChange,
		PathIndex:      req.Witness.PathIndex,
		Salt:           req.Witness.Salt,
		ChildPubkeyX:     emulated.ValueOf[Secp256k1Fp](childPubkeyX),
		ChildPubkeyY:     emulated.ValueOf[Secp256k1Fp](childPubkeyY),
		Country:          req.Witness.Country,
		Age:              req.Witness.Age,
		MerkleProof:        merkleProof,
		MerkleIndex:        req.Witness.MerkleIndex,
		MerkleLeafHash:     merkleLeafHash,
		DerivDigestDebug:   derivDigestDebug,
		DerivScalarDebug:   emulated.ValueOf[Secp256k1Fr](derivScalarDebug),
		HMontDebug:         emulated.ValueOf[Secp256k1Fr](derivScalarMontDebug),
		MontFixDebug:       emulated.ValueOf[Secp256k1Fr](montFixDebug),
		LeafDigestDebug:    leafDigestDebug,
		PxOnlyHashDebug:    pxOnlyHashDebug,
		RootFromProofDebug: rootFromProofDebug,
		NodeDebug:          nodeDebug,
		MasterMulDebugX:    emulated.ValueOf[Secp256k1Fp](masterMulDebugX),
		MasterMulDebugY:    emulated.ValueOf[Secp256k1Fp](masterMulDebugY),
		RDebugX:            emulated.ValueOf[Secp256k1Fp](rDebugX),
		RDebugY:            emulated.ValueOf[Secp256k1Fp](rDebugY),
		QmPDebugX:          emulated.ValueOf[Secp256k1Fp](qmPDebugX),
		QmPDebugY:          emulated.ValueOf[Secp256k1Fp](qmPDebugY),
		RDebugBLS:          rDebugBLS,
		HDebugSecp:         emulated.ValueOf[Secp256k1Fr](hDebugSecp),
	}

	w, err := frontend.NewWitness(&assignment, ecc.BLS12_381.ScalarField())
	if err != nil {
		return nil, nil, err
	}

	return proof, w, nil
}

// VerifyHD verifies a proof
func (p *ProverHD) VerifyHD(proofHex, publicInputsHex string) (bool, error) {
	// Deserialize proof
	proofBytes, err := hexToBytes(proofHex)
	if err != nil {
		return false, fmt.Errorf("invalid proof hex: %w", err)
	}

	proof := groth16.NewProof(ecc.BLS12_381)
	_, err = proof.ReadFrom(bytes.NewReader(proofBytes))
	if err != nil {
		return false, fmt.Errorf("proof deserialization failed: %w", err)
	}

	// Deserialize public inputs
	publicWitness, err := deserializePublicInputsHD(publicInputsHex)
	if err != nil {
		return false, fmt.Errorf("public inputs deserialization failed: %w", err)
	}

	// Verify proof
	err = groth16.Verify(proof, p.vk, publicWitness)
	return err == nil, err
}

// deserializePublicInputsHD deserializes hex to public witness
func deserializePublicInputsHD(hexStr string) (witness.Witness, error) {
	inputBytes, err := hexToBytes(hexStr)
	if err != nil {
		return nil, err
	}

	if len(inputBytes) != 128 { // 4 x 32 bytes
		return nil, fmt.Errorf("expected 128 bytes, got %d", len(inputBytes))
	}

	// Extract 4 public inputs
	publicInputs := make([]big.Int, 4)
	for i := 0; i < 4; i++ {
		publicInputs[i].SetBytes(inputBytes[i*32 : (i+1)*32])
	}

	// Create circuit with public inputs
	assignment := TensorCashKYCCircuitHD{
		ChainSeparator: &publicInputs[0],
		AssetID:        &publicInputs[1],
		ComplianceRoot: &publicInputs[2],
		TfrAnchor:      &publicInputs[3],
	}

	return frontend.NewWitness(&assignment, ecc.BLS12_381.ScalarField(), frontend.PublicOnly())
}

// hexToBytes converts hex string to bytes
func hexToBytes(hexStr string) ([]byte, error) {
	if len(hexStr) >= 2 && hexStr[:2] == "0x" {
		hexStr = hexStr[2:]
	}

	if len(hexStr)%2 != 0 {
		return nil, fmt.Errorf("hex string has odd length")
	}

	bytes := make([]byte, len(hexStr)/2)
	for i := 0; i < len(bytes); i++ {
		_, err := fmt.Sscanf(hexStr[i*2:i*2+2], "%02x", &bytes[i])
		if err != nil {
			return nil, err
		}
	}

	return bytes, nil
}
