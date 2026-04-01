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
	outputDir := flag.String("output", "vectors_hd", "Output directory for KYC-HD v1 test vectors")
	flag.Parse()

	log.Println("═══════════════════════════════════════════════════════════")
	log.Println("  TensorCash KYC-HD v1 - Golden Vector Generator")
	log.Println("═══════════════════════════════════════════════════════════")
	log.Println("")

	// Create output directory
	if err := os.MkdirAll(*outputDir, 0755); err != nil {
		log.Fatalf("Failed to create output directory: %v", err)
	}

	// 1. Run setup to generate keys
	log.Println("► Running trusted setup for KYC-HD v1...")
	log.Println("  NOTE: This includes secp256k1 EC operations and will be slower than v1")
	setupResult, err := circuit.SetupHD()
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
	prover, err := circuit.NewProverHD(setupResult.ProvingKey, setupResult.VerificationKey)
	if err != nil {
		log.Fatalf("Failed to create prover: %v", err)
	}

	// 3. Generate test vectors
	vectors := []GoldenVectorHD{}

	// Vector 1: Valid proof
	log.Println("► Generating valid proof vector...")
	log.Println("  This will take 30-120 seconds due to secp256k1 EC operations...")
	valid, err := generateVectorHD(prover, "valid", "test_seed_hd_valid")
	if err != nil {
		log.Fatalf("Failed to generate valid vector: %v", err)
	}
	vectors = append(vectors, valid)
	log.Println("  ✓ Valid proof generated")

	// Vector 2: Wrong master key
	log.Println("► Generating invalid master key vector...")
	invalidMaster, err := generateInvalidVectorHD(prover, "invalid_master_key", "test_seed_hd_invalid_master", circuit.InvalidMasterKeyHD)
	if err != nil {
		log.Fatalf("Failed to generate invalid master key vector: %v", err)
	}
	vectors = append(vectors, invalidMaster)

	// Vector 3: Wrong child derivation
	log.Println("► Generating invalid derivation vector...")
	invalidDerivation, err := generateInvalidVectorHD(prover, "invalid_derivation", "test_seed_hd_invalid_derivation", circuit.InvalidDerivationHD)
	if err != nil {
		log.Fatalf("Failed to generate invalid derivation vector: %v", err)
	}
	vectors = append(vectors, invalidDerivation)

	// Vector 4: Wrong age
	log.Println("► Generating invalid age vector...")
	invalidAge, err := generateInvalidVectorHD(prover, "invalid_age", "test_seed_hd_invalid_age", circuit.InvalidAgeHD)
	if err != nil {
		log.Fatalf("Failed to generate invalid age vector: %v", err)
	}
	vectors = append(vectors, invalidAge)

	// Vector 5: Wrong country
	log.Println("► Generating invalid country vector...")
	invalidCountry, err := generateInvalidVectorHD(prover, "invalid_country", "test_seed_hd_invalid_country", circuit.InvalidCountryHD)
	if err != nil {
		log.Fatalf("Failed to generate invalid country vector: %v", err)
	}
	vectors = append(vectors, invalidCountry)

	// Vector 6: Wrong Merkle proof
	log.Println("► Generating invalid merkle proof vector...")
	invalidMerkle, err := generateInvalidVectorHD(prover, "invalid_merkle", "test_seed_hd_invalid_merkle", circuit.InvalidMerkleProofHD)
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

	vectorsPath := *outputDir + "/golden_vectors_hd.json"
	if err := os.WriteFile(vectorsPath, vectorsJSON, 0644); err != nil {
		log.Fatalf("Failed to write vectors: %v", err)
	}

	log.Printf("✓ Saved %d vectors to %s", len(vectors), vectorsPath)
	log.Println("")

	// 6. Summary
	log.Println("═══════════════════════════════════════════════════════════")
	log.Println("  KYC-HD v1 Golden Vectors Generated")
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
	log.Println("NOTE: Public inputs format is IDENTICAL to v1 (consensus compatible)")
	log.Println("")
}

type GoldenVectorHD struct {
	Name            string                        `json:"name"`
	Witness         *circuit.ValidWitnessDataHD   `json:"witness"`
	ProofHex        string                        `json:"proof_hex"`
	ProofCustomHex  string                        `json:"proof_custom_hex"`
	PublicInputsHex string                        `json:"public_inputs_hex"`
	VKHex           string                        `json:"vk_hex"`
	VKGnarkHex      string                        `json:"vk_gnark_hex"`
	ShouldFail      bool                          `json:"should_fail"`
	ExpectedError   string                        `json:"expected_error,omitempty"`
}

func generateVectorHD(prover *circuit.ProverHD, name, seed string) (GoldenVectorHD, error) {
	// Generate valid witness
	witness, err := circuit.GenerateValidWitnessHD(seed)
	if err != nil {
		return GoldenVectorHD{}, fmt.Errorf("failed to generate witness: %w", err)
	}

	// Generate proof
	req := witness.ToProveRequestHD()
	resp, err := prover.ProveHD(req)
	if err != nil {
		return GoldenVectorHD{}, fmt.Errorf("proof generation failed: %w", err)
	}

	// Get raw proof for custom serialization
	proof, _, err := prover.ProveRawHD(req)
	if err != nil {
		return GoldenVectorHD{}, fmt.Errorf("raw proof generation failed: %w", err)
	}

	// Serialize in custom C++ format
	proofCustomBytes, err := circuit.SerializeProofCustom(proof)
	if err != nil {
		return GoldenVectorHD{}, fmt.Errorf("custom proof serialization failed: %w", err)
	}

	return GoldenVectorHD{
		Name:            name,
		Witness:         witness,
		ProofHex:        resp.ProofHex,
		ProofCustomHex:  "0x" + fmt.Sprintf("%x", proofCustomBytes),
		PublicInputsHex: resp.PublicInputsHex,
		VKHex:           "",
		ShouldFail:      false,
	}, nil
}

func generateInvalidVectorHD(prover *circuit.ProverHD, name, seed string, invalidType circuit.InvalidWitnessTypeHD) (GoldenVectorHD, error) {
	// Generate invalid witness
	witness, err := circuit.GenerateInvalidWitnessHD(seed, invalidType)
	if err != nil {
		return GoldenVectorHD{}, fmt.Errorf("failed to generate witness: %w", err)
	}

	// Try to generate proof (MUST fail)
	req := witness.ToProveRequestHD()
	resp, err := prover.ProveHD(req)

	if err == nil && resp != nil && resp.Success {
		log.Fatalf("FATAL: Invalid witness '%s' produced a valid proof! Circuit bug detected.", name)
	}

	expectedError := ""
	if err != nil {
		expectedError = err.Error()
	}

	return GoldenVectorHD{
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
