// SPDX-License-Identifier: Apache-2.0
package main

/*
#include <stdlib.h>
#include <string.h>

// Generic result structure for any Groth16 proof
typedef struct {
    unsigned char* proof_data;      // Raw proof bytes (gnark serialization)
    int proof_len;
    unsigned char* public_inputs;   // Raw public input bytes (32 bytes per field element)
    int public_inputs_len;
    char* error_msg;                // Error message (NULL if success)
} Groth16ProofResult;

// Result structure for MiMC hash computation
typedef struct {
    unsigned char* hash_data;       // 32-byte hash result
    int hash_len;
    char* error_msg;                // Error message (NULL if success)
} MiMCHashResult;
*/
import "C"
import (
	"encoding/hex"
	"encoding/json"
	"fmt"
	"math/big"
	"os"
	"unsafe"

	bls12381fr "github.com/consensys/gnark-crypto/ecc/bls12-381/fr"
	bls12381mimc "github.com/consensys/gnark-crypto/ecc/bls12-381/fr/mimc"
	"kyc-prover/internal/circuit"
)

//export Groth16_ProveKYC
func Groth16_ProveKYC(
	pkPath *C.char,
	vkPath *C.char,
	requestJSON *C.char,
) C.Groth16ProofResult {
	var result C.Groth16ProofResult

	// Read keys
	pkData, err := os.ReadFile(C.GoString(pkPath))
	if err != nil {
		result.error_msg = C.CString(fmt.Sprintf("Failed to read PK: %v", err))
		return result
	}

	vkData, err := os.ReadFile(C.GoString(vkPath))
	if err != nil {
		result.error_msg = C.CString(fmt.Sprintf("Failed to read VK: %v", err))
		return result
	}

	// Initialize prover
	prover, err := circuit.NewProver(pkData, vkData)
	if err != nil {
		result.error_msg = C.CString(fmt.Sprintf("Failed to initialize prover: %v", err))
		return result
	}

	// Parse request (circuit-specific JSON format)
	var req circuit.ProveRequest
	requestStr := C.GoString(requestJSON)
	if err := req.FromJSON([]byte(requestStr)); err != nil {
		result.error_msg = C.CString(fmt.Sprintf("Failed to parse request JSON: %v", err))
		return result
	}

	// Generate proof
	resp, err := prover.Prove(&req)
	if err != nil {
		result.error_msg = C.CString(fmt.Sprintf("Proof generation failed: %v", err))
		return result
	}

	// Parse hex strings to raw bytes
	// Use ProofCustomHex (192-byte BLST format) for on-chain consensus, not ProofHex (244-byte gnark format)
	proofBytes, err := hexToBytes(resp.ProofCustomHex)
	if err != nil {
		result.error_msg = C.CString(fmt.Sprintf("Failed to decode proof hex: %v", err))
		return result
	}

	publicInputsBytes, err := hexToBytes(resp.PublicInputsHex)
	if err != nil {
		result.error_msg = C.CString(fmt.Sprintf("Failed to decode public inputs hex: %v", err))
		return result
	}

	// Allocate C memory
	proofCopy := C.malloc(C.size_t(len(proofBytes)))
	C.memcpy(proofCopy, unsafe.Pointer(&proofBytes[0]), C.size_t(len(proofBytes)))

	publicInputsCopy := C.malloc(C.size_t(len(publicInputsBytes)))
	C.memcpy(publicInputsCopy, unsafe.Pointer(&publicInputsBytes[0]), C.size_t(len(publicInputsBytes)))

	result.proof_data = (*C.uchar)(proofCopy)
	result.proof_len = C.int(len(proofBytes))
	result.public_inputs = (*C.uchar)(publicInputsCopy)
	result.public_inputs_len = C.int(len(publicInputsBytes))
	result.error_msg = nil

	return result
}

//export Groth16_ProveKYCHD
func Groth16_ProveKYCHD(
	pkPath *C.char,
	vkPath *C.char,
	requestJSON *C.char,
) C.Groth16ProofResult {
	var result C.Groth16ProofResult

	// Read keys
	pkData, err := os.ReadFile(C.GoString(pkPath))
	if err != nil {
		result.error_msg = C.CString(fmt.Sprintf("Failed to read PK: %v", err))
		return result
	}

	vkData, err := os.ReadFile(C.GoString(vkPath))
	if err != nil {
		result.error_msg = C.CString(fmt.Sprintf("Failed to read VK: %v", err))
		return result
	}

	// Initialize HD prover
	prover, err := circuit.NewProverHD(pkData, vkData)
	if err != nil {
		result.error_msg = C.CString(fmt.Sprintf("Failed to initialize HD prover: %v", err))
		return result
	}

	// Parse request (HD circuit-specific JSON format)
	var req circuit.ProveRequestHD
	requestStr := C.GoString(requestJSON)
	if err := req.FromJSON([]byte(requestStr)); err != nil {
		result.error_msg = C.CString(fmt.Sprintf("Failed to parse HD request JSON: %v", err))
		return result
	}

	// Generate proof
	resp, err := prover.ProveHD(&req)
	if err != nil {
		result.error_msg = C.CString(fmt.Sprintf("HD proof generation failed: %v", err))
		return result
	}

	// Parse hex strings to raw bytes
	proofBytes, err := hexToBytes(resp.ProofHex)
	if err != nil {
		result.error_msg = C.CString(fmt.Sprintf("Failed to decode proof hex: %v", err))
		return result
	}

	publicInputsBytes, err := hexToBytes(resp.PublicInputsHex)
	if err != nil {
		result.error_msg = C.CString(fmt.Sprintf("Failed to decode public inputs hex: %v", err))
		return result
	}

	// Allocate C memory
	proofCopy := C.malloc(C.size_t(len(proofBytes)))
	C.memcpy(proofCopy, unsafe.Pointer(&proofBytes[0]), C.size_t(len(proofBytes)))

	publicInputsCopy := C.malloc(C.size_t(len(publicInputsBytes)))
	C.memcpy(publicInputsCopy, unsafe.Pointer(&publicInputsBytes[0]), C.size_t(len(publicInputsBytes)))

	result.proof_data = (*C.uchar)(proofCopy)
	result.proof_len = C.int(len(proofBytes))
	result.public_inputs = (*C.uchar)(publicInputsCopy)
	result.public_inputs_len = C.int(len(publicInputsBytes))
	result.error_msg = nil

	return result
}

//export Groth16_ProveKYCHDV1
func Groth16_ProveKYCHDV1(
	pkPath *C.char,
	vkPath *C.char,
	requestJSON *C.char,
) C.Groth16ProofResult {
	var result C.Groth16ProofResult

	// Catch panics and convert to error
	defer func() {
		if r := recover(); r != nil {
			result.error_msg = C.CString(fmt.Sprintf("PANIC in HD V1 prover: %v", r))
		}
	}()

	// Read keys
	pkData, err := os.ReadFile(C.GoString(pkPath))
	if err != nil {
		result.error_msg = C.CString(fmt.Sprintf("Failed to read PK: %v", err))
		return result
	}

	vkData, err := os.ReadFile(C.GoString(vkPath))
	if err != nil {
		result.error_msg = C.CString(fmt.Sprintf("Failed to read VK: %v", err))
		return result
	}

	// Initialize HD V1 prover
	prover, err := circuit.NewProverHDV1(pkData, vkData)
	if err != nil {
		result.error_msg = C.CString(fmt.Sprintf("Failed to initialize HD V1 prover: %v", err))
		return result
	}

	// Parse request (HD V1 circuit-specific JSON format)
	var req circuit.ProveRequestHDV1
	requestStr := C.GoString(requestJSON)
	requestBytes := []byte(requestStr)
	if err := json.Unmarshal(requestBytes, &req); err != nil {
		result.error_msg = C.CString(fmt.Sprintf("Failed to parse HD V1 request JSON: %v", err))
		return result
	}

	// Generate proof
	resp, err := prover.ProveHDV1(&req)
	if err != nil {
		result.error_msg = C.CString(fmt.Sprintf("HD V1 proof generation failed: %v", err))
		return result
	}

	// Check response success status
	if !resp.Success {
		result.error_msg = C.CString(fmt.Sprintf("HD V1 proof generation failed: %s", resp.Error))
		return result
	}

	// Parse hex strings to raw bytes
	proofBytes, err := hexToBytes(resp.ProofHex)
	if err != nil {
		result.error_msg = C.CString(fmt.Sprintf("Failed to decode proof hex: %v", err))
		return result
	}

	publicInputsBytes, err := hexToBytes(resp.PublicInputsHex)
	if err != nil {
		result.error_msg = C.CString(fmt.Sprintf("Failed to decode public inputs hex: %v", err))
		return result
	}

	// Allocate C memory
	proofCopy := C.malloc(C.size_t(len(proofBytes)))
	C.memcpy(proofCopy, unsafe.Pointer(&proofBytes[0]), C.size_t(len(proofBytes)))

	publicInputsCopy := C.malloc(C.size_t(len(publicInputsBytes)))
	C.memcpy(publicInputsCopy, unsafe.Pointer(&publicInputsBytes[0]), C.size_t(len(publicInputsBytes)))

	result.proof_data = (*C.uchar)(proofCopy)
	result.proof_len = C.int(len(proofBytes))
	result.public_inputs = (*C.uchar)(publicInputsCopy)
	result.public_inputs_len = C.int(len(publicInputsBytes))
	result.error_msg = nil

	return result
}

//export Groth16_Verify
func Groth16_Verify(
	vkPath *C.char,
	proofData *C.uchar, proofLen C.int,
	publicInputs *C.uchar, publicInputsLen C.int,
) *C.char {
	// Read VK
	vkData, err := os.ReadFile(C.GoString(vkPath))
	if err != nil {
		return C.CString(fmt.Sprintf("Failed to read VK: %v", err))
	}

	vk, err := circuit.LoadVerificationKey(vkData)
	if err != nil {
		return C.CString(fmt.Sprintf("Failed to load VK: %v", err))
	}

	// Convert C buffers to Go slices
	proofBytes := C.GoBytes(unsafe.Pointer(proofData), proofLen)
	publicInputsBytes := C.GoBytes(unsafe.Pointer(publicInputs), publicInputsLen)

	// Verify using circuit package
	err = circuit.VerifyWithVK(proofBytes, publicInputsBytes, vk)
	if err != nil {
		return C.CString(fmt.Sprintf("Verification failed: %v", err))
	}

	return nil // Success
}

//export Groth16_FreeResult
func Groth16_FreeResult(result *C.Groth16ProofResult) {
	if result.proof_data != nil {
		C.free(unsafe.Pointer(result.proof_data))
	}
	if result.public_inputs != nil {
		C.free(unsafe.Pointer(result.public_inputs))
	}
	if result.error_msg != nil {
		C.free(unsafe.Pointer(result.error_msg))
	}
}

func hexToBytes(hexStr string) ([]byte, error) {
	if len(hexStr) >= 2 && hexStr[:2] == "0x" {
		hexStr = hexStr[2:]
	}
	return hex.DecodeString(hexStr)
}

//export Groth16_ComputeMiMCHash
func Groth16_ComputeMiMCHash(
	tag *C.char,
	input1Hex *C.char,
	input2Hex *C.char,
	input3Hex *C.char,
) C.MiMCHashResult {
	var result C.MiMCHashResult

	// Catch panics
	defer func() {
		if r := recover(); r != nil {
			result.error_msg = C.CString(fmt.Sprintf("PANIC in MiMC hash: %v", r))
		}
	}()

	// Initialize MiMC hasher
	H := bls12381mimc.NewMiMC()

	// Write tag as field element
	tagStr := C.GoString(tag)
	var tagElem bls12381fr.Element
	if tagStr != "" {
		// Try to parse as hex or string
		if len(tagStr) >= 2 && tagStr[:2] == "0x" {
			tagBytes, err := hexToBytes(tagStr)
			if err != nil {
				result.error_msg = C.CString(fmt.Sprintf("Failed to decode tag hex: %v", err))
				return result
			}
			tagBig := new(big.Int).SetBytes(tagBytes)
			tagElem.SetBigInt(tagBig)
		} else {
			// ASCII string - convert to hex
			tagBig := new(big.Int).SetBytes([]byte(tagStr))
			tagElem.SetBigInt(tagBig)
		}
		H.Write(tagElem.Marshal())
	}

	// Helper to write input as field element
	writeInput := func(inputHex *C.char) error {
		if inputHex == nil {
			return nil
		}
		hexStr := C.GoString(inputHex)
		if hexStr == "" {
			return nil
		}

		inputBytes, err := hexToBytes(hexStr)
		if err != nil {
			return fmt.Errorf("failed to decode input hex: %v", err)
		}

		inputBig := new(big.Int).SetBytes(inputBytes)
		var elem bls12381fr.Element
		elem.SetBigInt(inputBig)
		H.Write(elem.Marshal())
		return nil
	}

	// Write input1
	if err := writeInput(input1Hex); err != nil {
		result.error_msg = C.CString(fmt.Sprintf("Input1 error: %v", err))
		return result
	}

	// Write input2
	if err := writeInput(input2Hex); err != nil {
		result.error_msg = C.CString(fmt.Sprintf("Input2 error: %v", err))
		return result
	}

	// Write input3
	if err := writeInput(input3Hex); err != nil {
		result.error_msg = C.CString(fmt.Sprintf("Input3 error: %v", err))
		return result
	}

	// Compute hash
	hashBytes := H.Sum(nil)

	// Allocate C memory for hash
	hashCopy := C.malloc(C.size_t(len(hashBytes)))
	C.memcpy(hashCopy, unsafe.Pointer(&hashBytes[0]), C.size_t(len(hashBytes)))

	result.hash_data = (*C.uchar)(hashCopy)
	result.hash_len = C.int(len(hashBytes))
	result.error_msg = nil

	return result
}

//export Groth16_FreeMiMCResult
func Groth16_FreeMiMCResult(result *C.MiMCHashResult) {
	if result.hash_data != nil {
		C.free(unsafe.Pointer(result.hash_data))
	}
	if result.error_msg != nil {
		C.free(unsafe.Pointer(result.error_msg))
	}
}

// Required for CGO to build as shared library
func main() {}
