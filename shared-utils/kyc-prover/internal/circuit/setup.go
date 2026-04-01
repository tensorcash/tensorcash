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

// Setup performs the Groth16 trusted setup ceremony
// WARNING: This is for TESTING ONLY. Production must use a proper MPC ceremony.
func Setup() (*SetupResult, error) {
	// Create an empty circuit to compile
	var circuit TensorCashKYCCircuit

	// Compile circuit to R1CS
	ccs, err := frontend.Compile(ecc.BLS12_381.ScalarField(), r1cs.NewBuilder, &circuit)
	if err != nil {
		return nil, fmt.Errorf("circuit compilation failed: %w", err)
	}

	// Run Groth16 setup
	// WARNING: This generates random toxic waste!
	// Production should use gnark-crypto's MPC setup
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

	// Serialize VK in gnark format (for loading/proving)
	var vkBuf bytes.Buffer
	_, err = vk.WriteTo(&vkBuf)
	if err != nil {
		return nil, fmt.Errorf("vk serialization failed: %w", err)
	}

	// Also serialize VK in custom TensorCash format for C++ compatibility (golden vectors only)
	vkCustomBytes, err := SerializeVKCustom(vk)
	if err != nil {
		return nil, fmt.Errorf("vk custom serialization failed: %w", err)
	}

	return &SetupResult{
		ProvingKey:      pkBuf.Bytes(),
		VerificationKey: vkBuf.Bytes(),                           // Gnark format for proving
		VKHex:           "0x" + fmt.Sprintf("%x", vkCustomBytes), // Custom format for C++ golden vectors
		VKGnarkHex:      "0x" + fmt.Sprintf("%x", vkBuf.Bytes()), // Gnark format for Go verification tests
	}, nil
}

// LoadProvingKey deserializes a proving key from bytes
func LoadProvingKey(data []byte) (groth16.ProvingKey, error) {
	pk := groth16.NewProvingKey(ecc.BLS12_381)
	_, err := pk.ReadFrom(bytes.NewReader(data))
	if err != nil {
		return nil, fmt.Errorf("failed to load proving key: %w", err)
	}
	return pk, nil
}

// LoadVerificationKey deserializes a verification key from bytes
func LoadVerificationKey(data []byte) (groth16.VerifyingKey, error) {
	vk := groth16.NewVerifyingKey(ecc.BLS12_381)
	_, err := vk.ReadFrom(bytes.NewReader(data))
	if err != nil {
		return nil, fmt.Errorf("failed to load verification key: %w", err)
	}
	return vk, nil
}
