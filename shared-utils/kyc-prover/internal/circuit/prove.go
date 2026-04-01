// SPDX-License-Identifier: Apache-2.0
package circuit

import (
	"bytes"
	"encoding/hex"
	"fmt"
	"math/big"

	"github.com/consensys/gnark-crypto/ecc"
	"github.com/consensys/gnark/backend/groth16"
	"github.com/consensys/gnark/constraint"
	"github.com/consensys/gnark/frontend"
	"github.com/consensys/gnark/frontend/cs/r1cs"
)

// Prover handles proof generation
type Prover struct {
	pk  groth16.ProvingKey
	vk  groth16.VerifyingKey
	ccs constraint.ConstraintSystem
}

// NewProver creates a prover with loaded keys
func NewProver(pkData, vkData []byte) (*Prover, error) {
	pk, err := LoadProvingKey(pkData)
	if err != nil {
		return nil, err
	}

	vk, err := LoadVerificationKey(vkData)
	if err != nil {
		return nil, err
	}

	// Compile circuit (needed for witness generation)
	var circuit TensorCashKYCCircuit
	ccs, err := frontend.Compile(ecc.BLS12_381.ScalarField(), r1cs.NewBuilder, &circuit)
	if err != nil {
		return nil, fmt.Errorf("circuit compilation failed: %w", err)
	}

	return &Prover{
		pk:  pk,
		vk:  vk,
		ccs: ccs,
	}, nil
}

// Prove generates a ZK proof from a request
func (p *Prover) Prove(req *ProveRequest) (*ProveResponse, error) {
	// Parse public inputs
	chainSep, err := HexToBigInt(req.ChainSeparator)
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

	// Parse witness
	secret, err := HexToBigInt(req.Witness.Secret)
	if err != nil {
		return nil, fmt.Errorf("invalid secret: %w", err)
	}

	pubkeyHash, err := HexToBigInt(req.Witness.PubkeyHash)
	if err != nil {
		return nil, fmt.Errorf("invalid pubkey_hash: %w", err)
	}

	merkleLeafHash, err := HexToBigInt(req.Witness.MerkleLeafHash)
	if err != nil {
		return nil, fmt.Errorf("invalid merkle_leaf_hash: %w", err)
	}

	// Parse Merkle proof
	var merkleProof [8]*big.Int
	if len(req.Witness.MerkleProof) != 8 {
		return nil, fmt.Errorf("merkle proof must have 8 elements, got %d", len(req.Witness.MerkleProof))
	}

	for i, hexStr := range req.Witness.MerkleProof {
		val, err := HexToBigInt(hexStr)
		if err != nil {
			return nil, fmt.Errorf("invalid merkle_proof[%d]: %w", i, err)
		}
		merkleProof[i] = val
	}

	// Build circuit assignment
	assignment := TensorCashKYCCircuit{
		ChainSeparator: chainSep,
		AssetID:        assetID,
		ComplianceRoot: complianceRoot,
		TfrAnchor:      tfrAnchor,
		Secret:         secret,
		PubkeyHash:     pubkeyHash,
		Country:        big.NewInt(int64(req.Witness.Country)),
		Age:            big.NewInt(int64(req.Witness.Age)),
		MerkleIndex:    big.NewInt(int64(req.Witness.MerkleIndex)),
		MerkleLeafHash: merkleLeafHash,
	}

	for i := 0; i < 8; i++ {
		assignment.MerkleProof[i] = merkleProof[i]
	}

	// Generate witness
	fullWitness, err := frontend.NewWitness(&assignment, ecc.BLS12_381.ScalarField())
	if err != nil {
		return nil, fmt.Errorf("witness generation failed: %w", err)
	}

	// Generate proof
	proof, err := groth16.Prove(p.ccs, p.pk, fullWitness)
	if err != nil {
		return nil, fmt.Errorf("proof generation failed: %w", err)
	}

	// Serialize proof in gnark format for API verification
	var proofBuf bytes.Buffer
	_, err = proof.WriteTo(&proofBuf)
	if err != nil {
		return nil, fmt.Errorf("proof serialization failed: %w", err)
	}

	// Also serialize in BLST-compatible format for C++ consensus validation (192 bytes)
	// Format: [A:48][B:96][C:48] - matches what C++ BLST verifier expects
	proofBytesCustom, err := SerializeProofCustom(proof)
	if err != nil {
		return nil, fmt.Errorf("custom proof serialization failed: %w", err)
	}

	// Serialize public inputs (4 field elements, 32 bytes each = 128 bytes)
    publicInputs := make([]byte, 128)
    copy(publicInputs[0:32], padTo32Bytes(chainSep.Bytes()))
    copy(publicInputs[32:64], padTo32Bytes(assetID.Bytes()))
    copy(publicInputs[64:96], padTo32Bytes(complianceRoot.Bytes()))
    copy(publicInputs[96:128], padTo32Bytes(tfrAnchor.Bytes()))

	return &ProveResponse{
		ProofHex:        "0x" + hex.EncodeToString(proofBuf.Bytes()), // Gnark format for API verification
		ProofCustomHex:  "0x" + hex.EncodeToString(proofBytesCustom),  // C++ format for on-chain
		PublicInputsHex: "0x" + hex.EncodeToString(publicInputs),
		Success:         true,
	}, nil
}

// ProveRaw generates a proof and returns the raw gnark proof object (for golden vector generation)
func (p *Prover) ProveRaw(req *ProveRequest) (groth16.Proof, []byte, error) {
	// Parse inputs (same as Prove)
	chainSep, err := HexToBigInt(req.ChainSeparator)
	if err != nil {
		return nil, nil, fmt.Errorf("invalid chain_separator: %w", err)
	}
	assetID, err := HexToBigInt(req.AssetID)
	if err != nil {
		return nil, nil, fmt.Errorf("invalid asset_id: %w", err)
	}
	complianceRoot, err := HexToBigInt(req.ComplianceRoot)
	if err != nil {
		return nil, nil, fmt.Errorf("invalid compliance_root: %w", err)
	}
	tfrAnchor, err := HexToBigInt(req.TfrAnchor)
	if err != nil {
		return nil, nil, fmt.Errorf("invalid tfr_anchor: %w", err)
	}

	secret, err := HexToBigInt(req.Witness.Secret)
	if err != nil {
		return nil, nil, fmt.Errorf("invalid witness.secret: %w", err)
	}
	pubkeyHash, err := HexToBigInt(req.Witness.PubkeyHash)
	if err != nil {
		return nil, nil, fmt.Errorf("invalid witness.pubkey_hash: %w", err)
	}

	merkleProof := make([]*big.Int, 8)
	for i := 0; i < 8; i++ {
		if i < len(req.Witness.MerkleProof) {
			mp, err := HexToBigInt(req.Witness.MerkleProof[i])
			if err != nil {
				return nil, nil, fmt.Errorf("invalid witness.merkle_proof[%d]: %w", i, err)
			}
			merkleProof[i] = mp
		} else {
			merkleProof[i] = big.NewInt(0)
		}
	}

	merkleLeafHash, err := HexToBigInt(req.Witness.MerkleLeafHash)
	if err != nil {
		return nil, nil, fmt.Errorf("invalid witness.merkle_leaf_hash: %w", err)
	}

	assignment := TensorCashKYCCircuit{
		ChainSeparator: chainSep,
		AssetID:        assetID,
		ComplianceRoot: complianceRoot,
		TfrAnchor:      tfrAnchor,
		Secret:         secret,
		PubkeyHash:     pubkeyHash,
		Country:        big.NewInt(int64(req.Witness.Country)),
		Age:            big.NewInt(int64(req.Witness.Age)),
		MerkleIndex:    big.NewInt(int64(req.Witness.MerkleIndex)),
		MerkleLeafHash: merkleLeafHash,
	}

	for i := 0; i < 8; i++ {
		assignment.MerkleProof[i] = merkleProof[i]
	}

	fullWitness, err := frontend.NewWitness(&assignment, ecc.BLS12_381.ScalarField())
	if err != nil {
		return nil, nil, fmt.Errorf("witness generation failed: %w", err)
	}

	proof, err := groth16.Prove(p.ccs, p.pk, fullWitness)
	if err != nil {
		return nil, nil, fmt.Errorf("proof generation failed: %w", err)
	}

	// Build public inputs
	publicInputs := make([]byte, 128)
	copy(publicInputs[0:32], padTo32Bytes(chainSep.Bytes()))
	copy(publicInputs[32:64], padTo32Bytes(assetID.Bytes()))
	copy(publicInputs[64:96], padTo32Bytes(complianceRoot.Bytes()))
	copy(publicInputs[96:128], padTo32Bytes(tfrAnchor.Bytes()))

	return proof, publicInputs, nil
}

// Verify checks a proof locally using the prover's VK (for testing)
func (p *Prover) Verify(proofData []byte, publicInputs []byte) error {
	return VerifyWithVK(proofData, publicInputs, p.vk)
}

// VerifyWithVK verifies a proof with a provided verification key
func VerifyWithVK(proofData []byte, publicInputs []byte, vk groth16.VerifyingKey) error {
	// Deserialize proof
	proof := groth16.NewProof(ecc.BLS12_381)
	_, err := proof.ReadFrom(bytes.NewReader(proofData))
	if err != nil {
		return fmt.Errorf("proof deserialization failed: %w", err)
	}

	// Parse public inputs (4 x 32 bytes)
	if len(publicInputs) != 128 {
		return fmt.Errorf("public inputs must be 128 bytes, got %d", len(publicInputs))
	}

	chainSep := new(big.Int).SetBytes(publicInputs[0:32])
	assetID := new(big.Int).SetBytes(publicInputs[32:64])
	complianceRoot := new(big.Int).SetBytes(publicInputs[64:96])
	tfrAnchor := new(big.Int).SetBytes(publicInputs[96:128])

	// Build public witness
	publicAssignment := TensorCashKYCCircuit{
		ChainSeparator: chainSep,
		AssetID:        assetID,
		ComplianceRoot: complianceRoot,
		TfrAnchor:      tfrAnchor,
	}

	publicWitness, err := frontend.NewWitness(&publicAssignment, ecc.BLS12_381.ScalarField(), frontend.PublicOnly())
	if err != nil {
		return fmt.Errorf("public witness creation failed: %w", err)
	}

	// Verify proof
	err = groth16.Verify(proof, vk, publicWitness)
	if err != nil {
		return fmt.Errorf("verification failed: %w", err)
	}

	return nil
}

// padTo32Bytes pads byte slice to 32 bytes (left-padded with zeros)
func padTo32Bytes(b []byte) []byte {
	if len(b) >= 32 {
		return b[len(b)-32:]
	}
	padded := make([]byte, 32)
	copy(padded[32-len(b):], b)
	return padded
}
