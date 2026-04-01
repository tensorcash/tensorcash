// SPDX-License-Identifier: Apache-2.0
package circuit

import (
	"bytes"
	"encoding/hex"
	"fmt"
	"math/big"
	"strings"

	"github.com/consensys/gnark-crypto/ecc"
	"github.com/consensys/gnark/backend/groth16"
	"github.com/consensys/gnark/backend/witness"
	"github.com/consensys/gnark/constraint"
	"github.com/consensys/gnark/frontend"
	"github.com/consensys/gnark/frontend/cs/r1cs"
	"github.com/consensys/gnark/std/math/emulated"
)

// ProverHDV1 handles proof generation for the alternative KYC-HD v1 circuit
type ProverHDV1 struct {
	provingKey      groth16.ProvingKey
	verificationKey groth16.VerifyingKey
	ccs             constraint.ConstraintSystem
}

// NewProverHDV1 creates a new prover instance
func NewProverHDV1(pkBytes, vkBytes []byte) (*ProverHDV1, error) {
	// Deserialize proving key
	pk := groth16.NewProvingKey(ecc.BLS12_381)
	if _, err := pk.ReadFrom(bytes.NewReader(pkBytes)); err != nil {
		return nil, fmt.Errorf("failed to read proving key: %w", err)
	}

	// Deserialize verification key
	vk := groth16.NewVerifyingKey(ecc.BLS12_381)
	if _, err := vk.ReadFrom(bytes.NewReader(vkBytes)); err != nil {
		return nil, fmt.Errorf("failed to read verification key: %w", err)
	}

	// Compile circuit (needed for proving)
	var circuit TensorCashKYCCircuitHDV1
	ccs, err := frontend.Compile(ecc.BLS12_381.ScalarField(), r1cs.NewBuilder, &circuit)
	if err != nil {
		return nil, fmt.Errorf("circuit compilation failed: %w", err)
	}

	return &ProverHDV1{
		provingKey:      pk,
		verificationKey: vk,
		ccs:             ccs,
	}, nil
}

// ProveHDV1 generates a proof for the given request
func (p *ProverHDV1) ProveHDV1(req *ProveRequestHDV1) (*ProveResponseHDV1, error) {
	// Build witness
	witnessValues, err := buildWitnessHDV1(req)
	if err != nil {
		return &ProveResponseHDV1{
			Success: false,
			Error:   fmt.Sprintf("witness build failed: %v", err),
		}, nil
	}

	// Create witness
	witness, err := frontend.NewWitness(witnessValues, ecc.BLS12_381.ScalarField())
	if err != nil {
		return &ProveResponseHDV1{
			Success: false,
			Error:   fmt.Sprintf("witness creation failed: %v", err),
		}, nil
	}

	// Generate proof
	proof, err := groth16.Prove(p.ccs, p.provingKey, witness)
	if err != nil {
		return &ProveResponseHDV1{
			Success: false,
			Error:   fmt.Sprintf("proof generation failed: %v", err),
		}, nil
	}

	// Extract public witness
	publicWitness, err := witness.Public()
	if err != nil {
		return &ProveResponseHDV1{
			Success: false,
			Error:   fmt.Sprintf("public witness extraction failed: %v", err),
		}, nil
	}

	// Verify proof
	err = groth16.Verify(proof, p.verificationKey, publicWitness)
	if err != nil {
		return &ProveResponseHDV1{
			Success: false,
			Error:   fmt.Sprintf("proof verification failed: %v", err),
		}, nil
	}

	// Serialize proof using custom BLST-compatible format (192 bytes)
	proofBytes, err := SerializeProofCustom(proof)
	if err != nil {
		return &ProveResponseHDV1{
			Success: false,
			Error:   fmt.Sprintf("proof serialization failed: %v", err),
		}, nil
	}

	// Serialize public inputs manually (6 field elements, 32 bytes each = 192 bytes)
	// Parse back the public inputs to serialize them properly
	chainSeparator, _ := parseBigInt(req.ChainSeparator)
	assetID, _ := parseBigInt(req.AssetID)
	complianceRoot, _ := parseBigInt(req.ComplianceRoot)
	tfrAnchor, _ := parseBigInt(req.TfrAnchor)
	outputKeyHigh, _ := parseBigInt(req.OutputKeyHigh)
	outputKeyLow, _ := parseBigInt(req.OutputKeyLow)

	publicInputs := make([]byte, 192)
	copy(publicInputs[0:32], padTo32BytesV1(chainSeparator.Bytes()))
	copy(publicInputs[32:64], padTo32BytesV1(assetID.Bytes()))
	copy(publicInputs[64:96], padTo32BytesV1(complianceRoot.Bytes()))
	copy(publicInputs[96:128], padTo32BytesV1(tfrAnchor.Bytes()))
	copy(publicInputs[128:160], padTo32BytesV1(outputKeyHigh.Bytes()))
	copy(publicInputs[160:192], padTo32BytesV1(outputKeyLow.Bytes()))

	return &ProveResponseHDV1{
		Success:         true,
		ProofHex:        "0x" + hex.EncodeToString(proofBytes),
		PublicInputsHex: "0x" + hex.EncodeToString(publicInputs),
	}, nil
}

// ProveRawHDV1 generates a proof and returns the raw proof object
func (p *ProverHDV1) ProveRawHDV1(req *ProveRequestHDV1) (groth16.Proof, witness.Witness, error) {
	// Build witness
	witnessValues, err := buildWitnessHDV1(req)
	if err != nil {
		return nil, nil, fmt.Errorf("witness build failed: %w", err)
	}

	// Create witness
	w, err := frontend.NewWitness(witnessValues, ecc.BLS12_381.ScalarField())
	if err != nil {
		return nil, nil, fmt.Errorf("witness creation failed: %w", err)
	}

	// Generate proof
	proof, err := groth16.Prove(p.ccs, p.provingKey, w)
	if err != nil {
		return nil, nil, fmt.Errorf("proof generation failed: %w", err)
	}

	return proof, w, nil
}

// buildWitnessHDV1 builds the witness values for the alternative circuit
func buildWitnessHDV1(req *ProveRequestHDV1) (*TensorCashKYCCircuitHDV1, error) {
	var circuit TensorCashKYCCircuitHDV1

	// Parse public inputs
	chainSeparator, err := parseBigInt(req.ChainSeparator)
	if err != nil {
		return nil, fmt.Errorf("invalid chain separator: %w", err)
	}
	circuit.ChainSeparator = chainSeparator

	assetID, err := parseBigInt(req.AssetID)
	if err != nil {
		return nil, fmt.Errorf("invalid asset ID: %w", err)
	}
	circuit.AssetID = assetID

	complianceRoot, err := parseBigInt(req.ComplianceRoot)
	if err != nil {
		return nil, fmt.Errorf("invalid compliance root: %w", err)
	}
	circuit.ComplianceRoot = complianceRoot

	tfrAnchor, err := parseBigInt(req.TfrAnchor)
	if err != nil {
		return nil, fmt.Errorf("invalid tfr anchor: %w", err)
	}
	circuit.TfrAnchor = tfrAnchor

	outputKeyHigh, err := parseBigInt(req.OutputKeyHigh)
	if err != nil {
		return nil, fmt.Errorf("invalid output key high: %w", err)
	}
	circuit.OutputKeyHigh = outputKeyHigh

	outputKeyLow, err := parseBigInt(req.OutputKeyLow)
	if err != nil {
		return nil, fmt.Errorf("invalid output key low: %w", err)
	}
	circuit.OutputKeyLow = outputKeyLow

	// Parse parent pubkey
	masterPubkeyX, err := parseBigInt(req.Witness.MasterPubkeyX)
	if err != nil {
		return nil, fmt.Errorf("invalid master pubkey X: %w", err)
	}
	circuit.MasterPubkeyX = emulated.ValueOf[Secp256k1Fp](masterPubkeyX)

	masterPubkeyY, err := parseBigInt(req.Witness.MasterPubkeyY)
	if err != nil {
		return nil, fmt.Errorf("invalid master pubkey Y: %w", err)
	}
	circuit.MasterPubkeyY = emulated.ValueOf[Secp256k1Fp](masterPubkeyY)

	// Parse derivation parameters
	derivCommitment, err := parseBigInt(req.Witness.DerivationCommitment)
	if err != nil {
		return nil, fmt.Errorf("invalid derivation commitment: %w", err)
	}
	circuit.DerivationCommitment = derivCommitment

	pathVector, err := parseBigInt(req.Witness.PathVector)
	if err != nil {
		return nil, fmt.Errorf("invalid path vector: %w", err)
	}
	circuit.PathVector = pathVector

	salt, err := parseBigInt(req.Witness.Salt)
	if err != nil {
		return nil, fmt.Errorf("invalid salt: %w", err)
	}
	circuit.Salt = salt

	// Parse child key
	childPubkeyX, err := parseBigInt(req.Witness.ChildPubkeyX)
	if err != nil {
		return nil, fmt.Errorf("invalid child pubkey X: %w", err)
	}
	circuit.ChildPubkeyX = emulated.ValueOf[Secp256k1Fp](childPubkeyX)

	childPubkeyY, err := parseBigInt(req.Witness.ChildPubkeyY)
	if err != nil {
		return nil, fmt.Errorf("invalid child pubkey Y: %w", err)
	}
	circuit.ChildPubkeyY = emulated.ValueOf[Secp256k1Fp](childPubkeyY)

	// Parse Merkle proof
	merklePathBits, err := parseBigInt(req.Witness.MerklePathBits)
	if err != nil {
		return nil, fmt.Errorf("invalid merkle path bits: %w", err)
	}
	circuit.MerklePathBits = merklePathBits

	if len(req.Witness.MerkleSiblings) != 8 {
		return nil, fmt.Errorf("merkle proof must have exactly 8 siblings, got %d", len(req.Witness.MerkleSiblings))
	}

	for i, siblingHex := range req.Witness.MerkleSiblings {
		sibling, err := parseBigInt(siblingHex)
		if err != nil {
			return nil, fmt.Errorf("invalid merkle sibling %d: %w", i, err)
		}
		circuit.MerkleSiblings[i] = sibling
	}

	return &circuit, nil
}

// parseBigInt parses a hex string (with or without 0x prefix) to big.Int
func parseBigInt(hexStr string) (*big.Int, error) {
	hexStr = strings.TrimPrefix(hexStr, "0x")
	hexStr = strings.TrimPrefix(hexStr, "0X")

	val := new(big.Int)
	if _, ok := val.SetString(hexStr, 16); !ok {
		return nil, fmt.Errorf("invalid hex string: %s", hexStr)
	}

	return val, nil
}

// padTo32BytesV1 pads byte slice to 32 bytes (left-padded with zeros)
func padTo32BytesV1(b []byte) []byte {
	if len(b) >= 32 {
		return b[len(b)-32:]
	}
	padded := make([]byte, 32)
	copy(padded[32-len(b):], b)
	return padded
}