// SPDX-License-Identifier: Apache-2.0
#pragma once

#include <vector>
#include <unordered_map>
#include <any>
#include <string>
#include <cstdint>

namespace proofpack {

// Main serialization function (deterministic)
std::vector<uint8_t> pack_proof(
    const std::unordered_map<std::string, std::any>& proof_dict
);

// Helper utilities
std::vector<uint8_t> hex_to_bytes(const std::string& hex);
std::string bytes_to_hex(const std::vector<uint8_t>& bytes);
std::string bytes_to_hex(const uint8_t* data, size_t size);

// Type extraction helpers for std::any
template<typename T>
std::vector<T> extract_vector(const std::any& value, const std::vector<T>& default_val = {});

template<typename T>
std::vector<std::vector<T>> extract_matrix(const std::any& value, 
                                           const std::vector<std::vector<T>>& default_val = {});

std::string extract_string(const std::any& value, const std::string& default_val = "");

template<typename T>
T extract_numeric(const std::any& value, T default_val = 0);

bool extract_bool(const std::any& value, bool default_val = false);

// Field name normalization
void normalize_field_names(std::unordered_map<std::string, std::any>& dict);

// ProofData structure for internal use
struct ProofData {
    // Version and metadata
    uint32_t version = 2;
    int64_t tick = 0;
    int64_t timestamp = 0;
    bool is_solution = false;
    
    // String fields
    std::string model_identifier;
    std::string compute_precision;
    std::string ipfs_cid;
    std::string model_config_diff;
    
    // Sampling parameters
    float temperature = 1.0f;
    float top_p = 1.0f;
    uint32_t top_k = 50;
    float repetition_penalty = 1.0f;
    
    // Binary fields (hex strings or byte vectors)
    std::vector<uint8_t> target;
    std::vector<uint8_t> vdf;
    std::vector<uint8_t> hash;
    std::vector<uint8_t> block_hash;
    std::vector<uint8_t> header_prefix;
    
    // 1D arrays
    std::vector<uint32_t> chosen_tokens;
    std::vector<float> chosen_probs;
    std::vector<float> sampling_u;
    std::vector<float> softmax_normalizers;
    std::vector<uint32_t> prompt_tokens;
    std::vector<bool> pad_mask;  // Schema uses pad_mask
    
    // 2D arrays
    std::vector<std::vector<float>> topk_logits;
    std::vector<std::vector<uint32_t>> topk_indices;
    std::vector<std::vector<float>> logsumexp_stats;
};

// Convert dict to ProofData
ProofData dict_to_proof_data(const std::unordered_map<std::string, std::any>& dict);

// Pack from ProofData structure
std::vector<uint8_t> pack_proof(const ProofData& data);

// Convenience function
std::vector<uint8_t> pack_proof_from_dict(const std::unordered_map<std::string, std::any>& proof_dict);

} // namespace proofpack