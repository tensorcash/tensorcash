// SPDX-License-Identifier: Apache-2.0
package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"log"
	"os"

	"kyc-prover/internal/circuit"
)

func main() {
	// Command line flags
	outputDir := flag.String("output", "vectors_hd_v1", "Output directory for alternative KYC-HD v1 test vectors")
	flag.Parse()

	log.Println("═══════════════════════════════════════════════════════════")
	log.Println("  TensorCash KYC-HD v1 Alternative - Golden Vector Generator")
	log.Println("═══════════════════════════════════════════════════════════")
	log.Println("")
	log.Println("  Alternative implementation features:")
	log.Println("  • Batch hashing instead of byte-by-byte")
	log.Println("  • Commitment-based key derivation")
	log.Println("  • Optimized Merkle proof verification")
	log.Println("  • Cleaner modular architecture")
	log.Println("")

	// Create output directory
	if err := os.MkdirAll(*outputDir, 0755); err != nil {
		log.Fatalf("Failed to create output directory: %v", err)
	}

	// 1. Run setup to generate keys
	log.Println("► Running trusted setup for alternative KYC-HD v1...")
	log.Println("  NOTE: This uses a different circuit architecture than the original")
	setupResult, err := circuit.SetupHDV1()
	if err != nil {
		log.Fatalf("Setup failed: %v", err)
	}

	// Save keys
	pkPath := *outputDir + "/proving_key_v1.bin"
	vkPath := *outputDir + "/verification_key_v1.bin"

	if err := os.WriteFile(pkPath, setupResult.ProvingKey, 0644); err != nil {
		log.Fatalf("Failed to write proving key: %v", err)
	}
	if err := os.WriteFile(vkPath, setupResult.VerificationKey, 0644); err != nil {
		log.Fatalf("Failed to write verification key: %v", err)
	}

	log.Printf("✓ Keys generated (PK: %d bytes, VK: %d bytes)", len(setupResult.ProvingKey), len(setupResult.VerificationKey))
	log.Println("")

	// 2. Create prover
	prover, err := circuit.NewProverHDV1(setupResult.ProvingKey, setupResult.VerificationKey)
	if err != nil {
		log.Fatalf("Failed to create prover: %v", err)
	}

	// 3. Generate test vectors
	vectors := []GoldenVectorHDV1{}

	// Vector 1: Valid proof
	log.Println("► Generating valid proof vector...")
	log.Println("  This will take 30-120 seconds due to secp256k1 EC operations...")
	valid, err := generateVectorHDV1(prover, "valid", "test_seed_hd_v1_valid")
	if err != nil {
		log.Fatalf("Failed to generate valid vector: %v", err)
	}
	vectors = append(vectors, valid)
	log.Println("  ✓ Valid proof generated")

	// Vector 2: Wrong master key
	log.Println("► Generating invalid master key vector...")
	invalidMaster, err := generateInvalidVectorHDV1(prover, "invalid_master_key", "test_seed_hd_v1_invalid_master", circuit.InvalidMasterKeyHDV1)
	if err != nil {
		log.Fatalf("Failed to generate invalid master key vector: %v", err)
	}
	vectors = append(vectors, invalidMaster)

	// Vector 3: Wrong child derivation
	log.Println("► Generating invalid derivation vector...")
	invalidDerivation, err := generateInvalidVectorHDV1(prover, "invalid_derivation", "test_seed_hd_v1_invalid_derivation", circuit.InvalidDerivationHDV1)
	if err != nil {
		log.Fatalf("Failed to generate invalid derivation vector: %v", err)
	}
	vectors = append(vectors, invalidDerivation)

	// Vector 4: Wrong output key binding
	log.Println("► Generating invalid output key vector...")
	invalidOutputKey, err := generateInvalidVectorHDV1(prover, "invalid_output_key", "test_seed_hd_v1_invalid_output_key", circuit.InvalidOutputKeyHDV1)
	if err != nil {
		log.Fatalf("Failed to generate invalid output key vector: %v", err)
	}
	vectors = append(vectors, invalidOutputKey)

	// Vector 5: Wrong Merkle proof
	log.Println("► Generating invalid merkle proof vector...")
	invalidMerkle, err := generateInvalidVectorHDV1(prover, "invalid_merkle", "test_seed_hd_v1_invalid_merkle", circuit.InvalidMerkleProofHDV1)
	if err != nil {
		log.Fatalf("Failed to generate invalid merkle vector: %v", err)
	}
	vectors = append(vectors, invalidMerkle)

	// 4. Add VK hex to all vectors
	vkHex := setupResult.VKHex
	vkGnarkHex := setupResult.VKGnarkHex
	for i := range vectors {
		vectors[i].VKHex = vkHex
		vectors[i].VKGnarkHex = vkGnarkHex
	}

	// 5. Save vectors
	log.Println("")
	log.Println("► Saving golden vectors...")

	vectorsJSON, err := json.MarshalIndent(vectors, "", "  ")
	if err != nil {
		log.Fatalf("Failed to marshal vectors: %v", err)
	}

	vectorsPath := *outputDir + "/golden_vectors_hd_v1.json"
	if err := os.WriteFile(vectorsPath, vectorsJSON, 0644); err != nil {
		log.Fatalf("Failed to write vectors: %v", err)
	}

	log.Printf("✓ Saved %d vectors to %s", len(vectors), vectorsPath)
	log.Println("")

	// 6. Summary
	log.Println("═══════════════════════════════════════════════════════════")
	log.Println("  Alternative KYC-HD v1 Golden Vectors Generated")
	log.Println("═══════════════════════════════════════════════════════════")
	log.Println("")
	log.Println("Files created:")
	log.Printf("  • %s (proving key)", pkPath)
	log.Printf("  • %s (verification key)", vkPath)
	log.Printf("  • %s (golden vectors)", vectorsPath)
	log.Println("")
	log.Println("Vectors:")
	for _, v := range vectors {
		status := "✓ valid"
		if v.ShouldFail {
			status = "✗ invalid"
		}
		log.Printf("  • %s (%s)", v.Name, status)
	}
	log.Println("")
	log.Println("Implementation differences from original:")
	log.Println("  • Batch element hashing vs byte-by-byte")
	log.Println("  • Commitment-based derivation with intermediate step")
	log.Println("  • Packed path and compliance data")
	log.Println("  • Cleaner circuit without debug taps")
	log.Println("")
	log.Println("NOTE: Public inputs format remains IDENTICAL for consensus compatibility")
	log.Println("")
}

type GoldenVectorHDV1 struct {
	Name            string                        `json:"name"`
	Witness         *circuit.ValidWitnessDataHDV1 `json:"witness"`
	ProofHex        string                        `json:"proof_hex"`
	ProofCustomHex  string                        `json:"proof_custom_hex"`
	PublicInputsHex string                        `json:"public_inputs_hex"`
	VKHex           string                        `json:"vk_hex"`
	VKGnarkHex      string                        `json:"vk_gnark_hex"`
	ShouldFail      bool                          `json:"should_fail"`
	ExpectedError   string                        `json:"expected_error,omitempty"`
}

func generateVectorHDV1(prover *circuit.ProverHDV1, name, seed string) (GoldenVectorHDV1, error) {
	// Generate valid witness
	witness, err := circuit.GenerateValidWitnessHDV1(seed)
	if err != nil {
		return GoldenVectorHDV1{}, fmt.Errorf("failed to generate witness: %w", err)
	}

	// Generate proof
	req := witness.ToProveRequestHDV1()
	resp, err := prover.ProveHDV1(req)
	if err != nil {
		return GoldenVectorHDV1{}, fmt.Errorf("proof generation failed: %w", err)
	}

	// Get raw proof for custom serialization
	proof, _, err := prover.ProveRawHDV1(req)
	if err != nil {
		return GoldenVectorHDV1{}, fmt.Errorf("raw proof generation failed: %w", err)
	}

	// Serialize in custom C++ format
	proofCustomBytes, err := circuit.SerializeProofCustom(proof)
	if err != nil {
		return GoldenVectorHDV1{}, fmt.Errorf("custom proof serialization failed: %w", err)
	}

	return GoldenVectorHDV1{
		Name:            name,
		Witness:         witness,
		ProofHex:        resp.ProofHex,
		ProofCustomHex:  "0x" + fmt.Sprintf("%x", proofCustomBytes),
		PublicInputsHex: resp.PublicInputsHex,
		VKHex:           "",
		ShouldFail:      false,
	}, nil
}

func generateInvalidVectorHDV1(prover *circuit.ProverHDV1, name, seed string, invalidType circuit.InvalidWitnessTypeHDV1) (GoldenVectorHDV1, error) {
	// Generate invalid witness
	witness, err := circuit.GenerateInvalidWitnessHDV1(seed, invalidType)
	if err != nil {
		return GoldenVectorHDV1{}, fmt.Errorf("failed to generate witness: %w", err)
	}

	// Try to generate proof (MUST fail)
	req := witness.ToProveRequestHDV1()
	resp, err := prover.ProveHDV1(req)

	if err == nil && resp != nil && resp.Success {
		log.Fatalf("FATAL: Invalid witness '%s' produced a valid proof! Circuit bug detected.", name)
	}

	expectedError := ""
	if err != nil {
		expectedError = err.Error()
	}

	return GoldenVectorHDV1{
		Name:            name,
		Witness:         witness,
		ProofHex:        "",
		ProofCustomHex:  "",
		PublicInputsHex: "",
		VKHex:           "",
		ShouldFail:      true,
		ExpectedError:   expectedError,
	}, nil
}