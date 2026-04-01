// SPDX-License-Identifier: Apache-2.0
package main

import (
	"flag"
	"fmt"
	"log"
	"os"

	"kyc-prover/internal/api"
	"kyc-prover/internal/circuit"
)

const (
	defaultPort   = "8080"
	defaultPKPath = "proving_key.bin"
	defaultVKPath = "verification_key.bin"
)

func main() {
	// Command line flags
	port := flag.String("port", defaultPort, "HTTP server port")
	pkPath := flag.String("pk", defaultPKPath, "Path to proving key")
	vkPath := flag.String("vk", defaultVKPath, "Path to verification key")
	setupOnly := flag.Bool("setup", false, "Run setup only (generate keys)")
	flag.Parse()

	// If setup flag is set, generate keys and exit
	if *setupOnly {
		if err := runSetup(*pkPath, *vkPath); err != nil {
			log.Fatalf("Setup failed: %v", err)
		}
		return
	}

	// Load keys
	log.Println("Loading proving key...")
	pkData, err := os.ReadFile(*pkPath)
	if err != nil {
		log.Fatalf("Failed to load proving key from %s: %v", *pkPath, err)
	}

	log.Println("Loading verification key...")
	vkData, err := os.ReadFile(*vkPath)
	if err != nil {
		log.Fatalf("Failed to load verification key from %s: %v", *vkPath, err)
	}

	// Create prover
	log.Println("Initializing prover...")
	prover, err := circuit.NewProver(pkData, vkData)
	if err != nil {
		log.Fatalf("Failed to create prover: %v", err)
	}

	// Start server
	server := api.NewServer(prover, *port)
	if err := server.Start(); err != nil {
		log.Fatalf("Server failed: %v", err)
	}
}

// runSetup generates proving and verification keys
func runSetup(pkPath, vkPath string) error {
	log.Println("═══════════════════════════════════════════════════════════")
	log.Println("  TensorCash KYC Prover - Trusted Setup")
	log.Println("═══════════════════════════════════════════════════════════")
	log.Println("")
	log.Println("⚠️  WARNING: This is a TRUSTED SETUP")
	log.Println("⚠️  DO NOT use for production without a proper MPC ceremony")
	log.Println("")

	log.Println("Compiling circuit...")
	result, err := circuit.Setup()
	if err != nil {
		return fmt.Errorf("setup failed: %w", err)
	}

	log.Printf("Circuit compiled successfully")
	log.Printf("Proving key size: %d bytes (%.2f MB)", len(result.ProvingKey), float64(len(result.ProvingKey))/1024/1024)
	log.Printf("Verification key size: %d bytes", len(result.VerificationKey))

	// Write proving key
	log.Printf("Writing proving key to %s...", pkPath)
	if err := os.WriteFile(pkPath, result.ProvingKey, 0644); err != nil {
		return fmt.Errorf("failed to write proving key: %w", err)
	}

	// Write verification key
	log.Printf("Writing verification key to %s...", vkPath)
	if err := os.WriteFile(vkPath, result.VerificationKey, 0644); err != nil {
		return fmt.Errorf("failed to write verification key: %w", err)
	}

	log.Println("")
	log.Println("✓ Setup complete!")
	log.Println("")
	log.Println("Verification Key (for on-chain deployment):")
	log.Println(result.VKHex)
	log.Println("")
	log.Println("Next steps:")
	log.Println("  1. Start server: ./server -pk proving_key.bin -vk verification_key.bin")
	log.Println("  2. Test endpoint: curl http://localhost:8080/health")
	log.Println("")

	return nil
}
