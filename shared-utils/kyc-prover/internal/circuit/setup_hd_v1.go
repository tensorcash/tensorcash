// SPDX-License-Identifier: Apache-2.0
package circuit

import (
	"bytes"
	"fmt"

	"github.com/consensys/gnark-crypto/ecc"
	"github.com/consensys/gnark/backend/groth16"
	"github.com/consensys/gnark/frontend"
	"github.com/consensys/gnark/frontend/cs/r1cs"
)

// SetupHDV1 performs the Groth16 trusted setup ceremony for the alternative KYC-HD v1
// WARNING: This is for TESTING ONLY. Production must use a proper MPC ceremony.
func SetupHDV1() (*SetupResult, error) {
	// Create an empty circuit to compile
	var circuit TensorCashKYCCircuitHDV1

	// Compile circuit to R1CS
	ccs, err := frontend.Compile(ecc.BLS12_381.ScalarField(), r1cs.NewBuilder, &circuit)
	if err != nil {
		return nil, fmt.Errorf("circuit compilation failed: %w", err)
	}

	// Print circuit stats
	fmt.Printf("  Circuit V1 stats: %d constraints, %d public inputs, %d secret inputs\n",
		ccs.GetNbConstraints(),
		ccs.GetNbPublicVariables()-1, // -1 for ONE wire
		ccs.GetNbSecretVariables())

	// Run Groth16 setup
	pk, vk, err := groth16.Setup(ccs)
	if err != nil {
		return nil, fmt.Errorf("groth16 setup failed: %w", err)
	}

	// Serialize keys
	var pkBuf bytes.Buffer
	_, err = pk.WriteTo(&pkBuf)
	if err != nil {
		return nil, fmt.Errorf("pk serialization failed: %w", err)
	}

	var vkBuf bytes.Buffer
	_, err = vk.WriteTo(&vkBuf)
	if err != nil {
		return nil, fmt.Errorf("vk serialization failed: %w", err)
	}

	// Convert VK to hex for JSON serialization
	vkHex := fmt.Sprintf("0x%x", vkBuf.Bytes())

	// For C++ compatibility, also serialize in custom format
	vkCustomBytes, err := SerializeVKCustom(vk)
	if err != nil {
		return nil, fmt.Errorf("custom vk serialization failed: %w", err)
	}
	vkCustomHex := fmt.Sprintf("0x%x", vkCustomBytes)

	return &SetupResult{
		ProvingKey:      pkBuf.Bytes(),
		VerificationKey: vkBuf.Bytes(),
		VKHex:           vkCustomHex, // Custom format for C++
		VKGnarkHex:      vkHex,        // Gnark native format
	}, nil
}