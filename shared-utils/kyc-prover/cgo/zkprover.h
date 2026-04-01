// SPDX-License-Identifier: Apache-2.0
// zkprover.h - Circuit-agnostic C header for CGO bridge to Go ZK prover
//
// This header provides a C-compatible interface to Groth16 proof generation.
// Supports multiple circuits via circuit-specific functions.

#ifndef ZKPROVER_H
#define ZKPROVER_H

#ifdef __cplusplus
extern "C" {
#endif

#include <stddef.h>

// Generic result structure for any Groth16 proof
typedef struct {
    unsigned char* proof_data;      // Raw proof bytes (gnark serialization)
    int proof_len;                  // Length of proof_data
    unsigned char* public_inputs;   // Raw public input bytes (N x 32 bytes)
    int public_inputs_len;          // Length of public_inputs
    char* error_msg;                // Error message (NULL if success)
} Groth16ProofResult;

// ============================================================================
// KYC v1 Circuit - Plain Address
// ============================================================================

// Generate a proof for the KYC v1 circuit (plain address, no HD derivation)
//
// Request JSON format:
// {
//   "chain_separator": "0x7bc914",
//   "asset_id": "0xabc123...",
//   "compliance_root": "0xdef456...",
//   "tfr_anchor": "0x0",
//   "witness": {
//     "secret": "0x...",
//     "pubkey_hash": "0x...",
//     "country": 840,
//     "age": 25,
//     "merkle_proof": ["0x...", "0x...", ...],  // 8 elements
//     "merkle_index": 42,
//     "merkle_leaf_hash": "0x..."
//   }
// }
//
// Returns Groth16ProofResult. Check result.error_msg for errors.
// Caller MUST call Groth16_FreeResult when done.
Groth16ProofResult Groth16_ProveKYC(
    const char* pkPath,
    const char* vkPath,
    const char* requestJSON
);

// ============================================================================
// KYC-HD v1 Circuit - Pubkey-only HD Derivation (CURRENT)
// ============================================================================

// Generate a proof for the KYC-HD v1 circuit (pubkey-only, output-key binding).
// No master_secret needed — key control is proven by the Taproot spend signature.
//
// Request JSON format:
// {
//   "chain_separator": "0x7bc914",
//   "asset_id": "0xabc123...",
//   "compliance_root": "0xdef456...",
//   "tfr_anchor": "0x0",
//   "output_key_high": "0x...",
//   "output_key_low": "0x...",
//   "witness": {
//     "master_pubkey_x": "0x...",
//     "master_pubkey_y": "0x...",
//     "derivation_commitment": "0x...",
//     "path_vector": "0x...",
//     "salt": "0x...",
//     "child_pubkey_x": "0x...",
//     "child_pubkey_y": "0x...",
//     "merkle_path_bits": "0x...",
//     "merkle_siblings": ["0x...", ...]  // 8 elements
//   }
// }
//
// Returns Groth16ProofResult. Check result.error_msg for errors.
// Caller MUST call Groth16_FreeResult when done.
Groth16ProofResult Groth16_ProveKYCHDV1(
    const char* pkPath,
    const char* vkPath,
    const char* requestJSON
);

// Legacy HD prover — DEPRECATED, use Groth16_ProveKYCHDV1 instead.
Groth16ProofResult Groth16_ProveKYCHD(
    const char* pkPath,
    const char* vkPath,
    const char* requestJSON
);

// ============================================================================
// Verification (Circuit-Agnostic)
// ============================================================================

// Verify a Groth16 proof (works for any circuit)
//
// Returns NULL on success, or error message string on failure.
// If non-NULL, caller should free the returned string.
char* Groth16_Verify(
    const char* vkPath,
    const unsigned char* proofData,
    int proofLen,
    const unsigned char* publicInputs,
    int publicInputsLen
);

// ============================================================================
// Memory Management
// ============================================================================

// Free memory allocated by Groth16_Prove* functions
// Must be called for every Groth16ProofResult to avoid memory leaks
void Groth16_FreeResult(Groth16ProofResult* result);

#ifdef __cplusplus
}
#endif

#endif // ZKPROVER_H
