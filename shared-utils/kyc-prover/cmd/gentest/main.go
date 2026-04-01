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
	outputDir := flag.String("output", "vectors", "Output directory for test vectors")
	flag.Parse()

	log.Println("═══════════════════════════════════════════════════════════")
	log.Println("  TensorCash KYC - Golden Vector Generator")
	log.Println("═══════════════════════════════════════════════════════════")
	log.Println("")

	// Create output directory
	if err := os.MkdirAll(*outputDir, 0755); err != nil {
		log.Fatalf("Failed to create output directory: %v", err)
	}

	// 1. Run setup to generate keys
	log.Println("► Running trusted setup...")
	setupResult, err := circuit.Setup()
	if err != nil {
		log.Fatalf("Setup failed: %v", err)
	}

	// Save keys
	pkPath := *outputDir + "/proving_key.bin"
	vkPath := *outputDir + "/verification_key.bin"

	if err := os.WriteFile(pkPath, setupResult.ProvingKey, 0644); err != nil {
		log.Fatalf("Failed to write proving key: %v", err)
	}
	if err := os.WriteFile(vkPath, setupResult.VerificationKey, 0644); err != nil {
		log.Fatalf("Failed to write verification key: %v", err)
	}

	log.Printf("✓ Keys generated (PK: %d bytes, VK: %d bytes)", len(setupResult.ProvingKey), len(setupResult.VerificationKey))
	log.Println("")

	// 2. Create prover
	prover, err := circuit.NewProver(setupResult.ProvingKey, setupResult.VerificationKey)
	if err != nil {
		log.Fatalf("Failed to create prover: %v", err)
	}

	// 3. Generate test vectors
	vectors := []GoldenVector{}

	// Vector 1: Valid proof
	log.Println("► Generating valid proof vector...")
	valid, err := generateVector(prover, "valid", "test_seed_valid", circuit.InvalidSecret)
	if err != nil {
		log.Fatalf("Failed to generate valid vector: %v", err)
	}
	vectors = append(vectors, valid)

	// Vector 2: Wrong pubkey_hash (invalid secret)
	log.Println("► Generating invalid secret vector...")
	invalidSecret, err := generateInvalidVector(prover, "invalid_secret", "test_seed_invalid_secret", circuit.InvalidSecret)
	if err != nil {
		log.Fatalf("Failed to generate invalid secret vector: %v", err)
	}
	vectors = append(vectors, invalidSecret)

	// Vector 3: Wrong age
	log.Println("► Generating invalid age vector...")
	invalidAge, err := generateInvalidVector(prover, "invalid_age", "test_seed_invalid_age", circuit.InvalidAge)
	if err != nil {
		log.Fatalf("Failed to generate invalid age vector: %v", err)
	}
	vectors = append(vectors, invalidAge)

	// Vector 4: Wrong country
	log.Println("► Generating invalid country vector...")
	invalidCountry, err := generateInvalidVector(prover, "invalid_country", "test_seed_invalid_country", circuit.InvalidCountry)
	if err != nil {
		log.Fatalf("Failed to generate invalid country vector: %v", err)
	}
	vectors = append(vectors, invalidCountry)

	// Vector 5: Wrong Merkle proof
	log.Println("► Generating invalid merkle proof vector...")
	invalidMerkle, err := generateInvalidVector(prover, "invalid_merkle", "test_seed_invalid_merkle", circuit.InvalidMerkleProof)
	if err != nil {
		log.Fatalf("Failed to generate invalid merkle vector: %v", err)
	}
	vectors = append(vectors, invalidMerkle)

	// 4. Add VK hex to all vectors (both formats)
	vkHex := setupResult.VKHex           // Custom C++ format (578 bytes)
	vkGnarkHex := setupResult.VKGnarkHex // Gnark format for Go tests (872 bytes)
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

	vectorsPath := *outputDir + "/golden_vectors.json"
	if err := os.WriteFile(vectorsPath, vectorsJSON, 0644); err != nil {
		log.Fatalf("Failed to write vectors: %v", err)
	}

	log.Printf("✓ Saved %d vectors to %s", len(vectors), vectorsPath)
	log.Println("")

	// 5. Summary
	log.Println("═══════════════════════════════════════════════════════════")
	log.Println("  Golden Vectors Generated")
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
}

type GoldenVector struct {
	Name            string                   `json:"name"`
	Witness         *circuit.ValidWitnessData `json:"witness"`
	ProofHex        string                   `json:"proof_hex"`         // Gnark format for Go API tests (~244 bytes)
	ProofCustomHex  string                   `json:"proof_custom_hex"`  // Custom C++ format (192 bytes)
	PublicInputsHex string                   `json:"public_inputs_hex"`
	VKHex           string                   `json:"vk_hex"`          // Custom C++ format (578 bytes)
	VKGnarkHex      string                   `json:"vk_gnark_hex"`    // Gnark format for Go tests (872 bytes)
	ShouldFail      bool                     `json:"should_fail"`
	ExpectedError   string                   `json:"expected_error,omitempty"`
}

func generateVector(prover *circuit.Prover, name, seed string, _ circuit.InvalidWitnessType) (GoldenVector, error) {
	// Generate valid witness
	witness, err := circuit.GenerateValidWitness(seed)
	if err != nil {
		return GoldenVector{}, fmt.Errorf("failed to generate witness: %w", err)
	}

	// Generate proof (both formats)
	req := witness.ToProveRequest()

	// Get gnark format for Go API tests
	resp, err := prover.Prove(req)
	if err != nil {
		return GoldenVector{}, fmt.Errorf("proof generation failed: %w", err)
	}

	// Get raw proof for custom C++ format
	proof, _, err := prover.ProveRaw(req)
	if err != nil {
		return GoldenVector{}, fmt.Errorf("raw proof generation failed: %w", err)
	}

	// Serialize in custom C++ format (192 bytes)
	proofCustomBytes, err := circuit.SerializeProofCustom(proof)
	if err != nil {
		return GoldenVector{}, fmt.Errorf("custom proof serialization failed: %w", err)
	}

	return GoldenVector{
		Name:            name,
		Witness:         witness,
		ProofHex:        resp.ProofHex,                          // Gnark format
		ProofCustomHex:  "0x" + fmt.Sprintf("%x", proofCustomBytes), // Custom C++ format
		PublicInputsHex: resp.PublicInputsHex,
		VKHex:           "", // Will be set separately
		ShouldFail:      false,
	}, nil
}

func generateInvalidVector(prover *circuit.Prover, name, seed string, invalidType circuit.InvalidWitnessType) (GoldenVector, error) {
	// Generate invalid witness
	witness, err := circuit.GenerateInvalidWitness(seed, invalidType)
	if err != nil {
		return GoldenVector{}, fmt.Errorf("failed to generate witness: %w", err)
	}

	// Try to generate proof (MUST fail for invalid witness)
	req := witness.ToProveRequest()
	resp, err := prover.Prove(req)

	// CRITICAL: If proof generation succeeds with an invalid witness, that's a bug
	if err == nil && resp != nil && resp.Success {
		log.Fatalf("FATAL: Invalid witness '%s' produced a valid proof! This indicates a circuit bug.", name)
	}

	expectedError := ""
	if err != nil {
		expectedError = err.Error()
	}

	// For invalid witnesses, we expect proof generation to fail
	// Save the witness for documentation but no proof data
	return GoldenVector{
		Name:           name,
		Witness:        witness,
		ProofHex:       "",           // No proof for invalid witness
		PublicInputsHex: "",           // No inputs
		VKHex:          "",            // Will be set later
		ShouldFail:     true,
		ExpectedError:  expectedError,
	}, nil
}
