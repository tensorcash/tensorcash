// SPDX-License-Identifier: Apache-2.0
#include "libproofpack.h"
#include "proof_generated.h"
#include <openssl/sha.h>
#include <algorithm>
#include <cstring>
#include <stdexcept>
#include <chrono>
#include <typeinfo>
#include <typeindex>

namespace proofpack {

// Template specializations must come before any use
template<>
std::vector<std::vector<float>> extract_matrix(const std::any& value, 
                                               const std::vector<std::vector<float>>& default_val) {
    try {
        if (value.type() == typeid(std::vector<std::vector<float>>)) {
            return std::any_cast<std::vector<std::vector<float>>>(value);
        }
    } catch (...) {}
    return default_val;
}

template<>
std::vector<std::vector<uint32_t>> extract_matrix(const std::any& value,
                                                  const std::vector<std::vector<uint32_t>>& default_val) {
    try {
        if (value.type() == typeid(std::vector<std::vector<uint32_t>>)) {
            return std::any_cast<std::vector<std::vector<uint32_t>>>(value);
        } else if (value.type() == typeid(std::vector<std::vector<int>>)) {
            auto matrix = std::any_cast<std::vector<std::vector<int>>>(value);
            std::vector<std::vector<uint32_t>> result;
            result.reserve(matrix.size());
            for (const auto& row : matrix) {
                result.emplace_back(row.begin(), row.end());
            }
            return result;
        }
    } catch (...) {}
    return default_val;
}

// Anonymous namespace for internal helpers
namespace {

// Helper to create FlatBuffer vector from std::vector<uint32_t>
flatbuffers::Offset<flatbuffers::Vector<uint32_t>> 
create_uint32_vector(flatbuffers::FlatBufferBuilder& builder, 
                    const std::vector<uint32_t>& data) {
    if (data.empty()) {
        std::vector<uint32_t> empty;
        return builder.CreateVector(empty);
    }
    return builder.CreateVector(data);
}

// Helper to create FlatBuffer vector from std::vector<float>
flatbuffers::Offset<flatbuffers::Vector<float>> 
create_float_vector(flatbuffers::FlatBufferBuilder& builder, 
                   const std::vector<float>& data) {
    if (data.empty()) {
        std::vector<float> empty;
        return builder.CreateVector(empty);
    }
    return builder.CreateVector(data);
}

// Helper to create FlatBuffer vector from std::vector<uint8_t>
flatbuffers::Offset<flatbuffers::Vector<uint8_t>> 
create_byte_vector(flatbuffers::FlatBufferBuilder& builder, 
                  const std::vector<uint8_t>& data) {
    if (data.empty()) {
        std::vector<uint8_t> empty;
        return builder.CreateVector(empty);
    }
    return builder.CreateVector(data);
}

// Helper to create FlatBuffer vector from std::vector<bool>
flatbuffers::Offset<flatbuffers::Vector<uint8_t>> 
create_bool_vector(flatbuffers::FlatBufferBuilder& builder, 
                  const std::vector<bool>& data) {
    std::vector<uint8_t> bytes;
    bytes.reserve(data.size());
    for (bool b : data) {
        bytes.push_back(b ? 1 : 0);
    }
    return builder.CreateVector(bytes);
}

// Note: Removed create_float_array and create_uint_array helper functions
// They were causing nested construction issues in FlatBuffers
// Now using inline creation pattern from working proof_writer

} // anonymous namespace

std::vector<uint8_t> hex_to_bytes(const std::string& hex) {
    std::vector<uint8_t> bytes;
    size_t start = 0;
    
    // Skip 0x prefix if present
    if (hex.size() >= 2 && hex[0] == '0' && (hex[1] == 'x' || hex[1] == 'X')) {
        start = 2;
    }
    
    // Process pairs of hex characters
    for (size_t i = start; i < hex.length(); i += 2) {
        std::string byteString = hex.substr(i, 2);
        uint8_t byte = static_cast<uint8_t>(std::strtol(byteString.c_str(), nullptr, 16));
        bytes.push_back(byte);
    }
    
    return bytes;
}

std::string bytes_to_hex(const std::vector<uint8_t>& bytes) {
    return bytes_to_hex(bytes.data(), bytes.size());
}

std::string bytes_to_hex(const uint8_t* data, size_t size) {
    static const char hex_chars[] = "0123456789abcdef";
    std::string hex;
    hex.reserve(size * 2);
    
    for (size_t i = 0; i < size; ++i) {
        hex.push_back(hex_chars[(data[i] >> 4) & 0x0F]);
        hex.push_back(hex_chars[data[i] & 0x0F]);
    }
    
    return hex;
}

void normalize_field_names(std::unordered_map<std::string, std::any>& dict) {
    // Map attention_mask to pad_mask (schema uses pad_mask)
    if (dict.count("attention_mask") && !dict.count("pad_mask")) {
        dict["pad_mask"] = dict["attention_mask"];
        dict.erase("attention_mask");
    }
    
    // Map model_config_diff to extra_flags if needed
    if (dict.count("model_config_diff") && !dict.count("extra_flags")) {
        dict["extra_flags"] = dict["model_config_diff"];
    }
}

// Template implementations
template<typename T>
std::vector<T> extract_vector(const std::any& value, const std::vector<T>& default_val) {
    try {
        if (value.type() == typeid(std::vector<T>)) {
            return std::any_cast<std::vector<T>>(value);
        }
    } catch (...) {}
    return default_val;
}

std::string extract_string(const std::any& value, const std::string& default_val) {
    try {
        if (value.type() == typeid(std::string)) {
            return std::any_cast<std::string>(value);
        }
    } catch (...) {}
    return default_val;
}

template<typename T>
T extract_numeric(const std::any& value, T default_val) {
    try {
        if (value.type() == typeid(T)) {
            return std::any_cast<T>(value);
        }
        // Try common conversions
        if (value.type() == typeid(int)) {
            return static_cast<T>(std::any_cast<int>(value));
        }
        if (value.type() == typeid(int64_t)) {
            return static_cast<T>(std::any_cast<int64_t>(value));
        }
        if (value.type() == typeid(uint32_t)) {
            return static_cast<T>(std::any_cast<uint32_t>(value));
        }
        if (value.type() == typeid(float)) {
            return static_cast<T>(std::any_cast<float>(value));
        }
        if (value.type() == typeid(double)) {
            return static_cast<T>(std::any_cast<double>(value));
        }
    } catch (...) {}
    return default_val;
}

bool extract_bool(const std::any& value, bool default_val) {
    try {
        if (value.type() == typeid(bool)) {
            return std::any_cast<bool>(value);
        }
        if (value.type() == typeid(int)) {
            return std::any_cast<int>(value) != 0;
        }
    } catch (...) {}
    return default_val;
}

// Explicit instantiations
template std::vector<uint32_t> extract_vector(const std::any&, const std::vector<uint32_t>&);
template std::vector<float> extract_vector(const std::any&, const std::vector<float>&);
template std::vector<bool> extract_vector(const std::any&, const std::vector<bool>&);
template std::vector<uint8_t> extract_vector(const std::any&, const std::vector<uint8_t>&);

template int32_t extract_numeric(const std::any&, int32_t);
template uint32_t extract_numeric(const std::any&, uint32_t);
template int64_t extract_numeric(const std::any&, int64_t);
template float extract_numeric(const std::any&, float);

ProofData dict_to_proof_data(const std::unordered_map<std::string, std::any>& dict) {
    ProofData data;
    
    // Normalize field names first
    auto normalized = dict;
    normalize_field_names(normalized);
    
    // Extract scalar fields
    data.version = extract_numeric<uint32_t>(normalized.count("version") ? normalized.at("version") : std::any(2u), 2);
    data.tick = extract_numeric<int64_t>(normalized.count("tick") ? normalized.at("tick") : std::any(0ll), 0);
    data.timestamp = extract_numeric<int64_t>(normalized.count("timestamp") ? normalized.at("timestamp") : std::any(0ll), 0);
    data.is_solution = extract_bool(normalized.count("is_solution") ? normalized.at("is_solution") : std::any(false), false);
    
    // String fields
    data.model_identifier = extract_string(normalized.count("model_identifier") ? normalized.at("model_identifier") : std::any(std::string("")), "");
    data.compute_precision = extract_string(normalized.count("compute_precision") ? normalized.at("compute_precision") : std::any(std::string("")), "");
    data.ipfs_cid = extract_string(normalized.count("ipfs_cid") ? normalized.at("ipfs_cid") : std::any(std::string("")), "");
    data.model_config_diff = extract_string(normalized.count("model_config_diff") ? normalized.at("model_config_diff") : std::any(std::string("")), "");
    
    // Sampling parameters
    data.temperature = extract_numeric<float>(normalized.count("temperature") ? normalized.at("temperature") : std::any(1.0f), 1.0f);
    data.top_p = extract_numeric<float>(normalized.count("top_p") ? normalized.at("top_p") : std::any(1.0f), 1.0f);
    data.top_k = extract_numeric<uint32_t>(normalized.count("top_k") ? normalized.at("top_k") : std::any(50u), 50);
    data.repetition_penalty = extract_numeric<float>(normalized.count("repetition_penalty") ? normalized.at("repetition_penalty") : std::any(1.0f), 1.0f);
    
    // Binary fields (handle both hex strings and byte vectors)
    auto extract_bytes = [](const std::any& value) -> std::vector<uint8_t> {
        if (value.type() == typeid(std::string)) {
            return hex_to_bytes(std::any_cast<std::string>(value));
        } else if (value.type() == typeid(std::vector<uint8_t>)) {
            return std::any_cast<std::vector<uint8_t>>(value);
        }
        return {};
    };
    
    if (normalized.count("target")) data.target = extract_bytes(normalized.at("target"));
    if (normalized.count("vdf")) data.vdf = extract_bytes(normalized.at("vdf"));
    if (normalized.count("hash")) data.hash = extract_bytes(normalized.at("hash"));
    if (normalized.count("block_hash")) data.block_hash = extract_bytes(normalized.at("block_hash"));
    if (normalized.count("header_prefix")) data.header_prefix = extract_bytes(normalized.at("header_prefix"));
    
    // 1D arrays
    if (normalized.count("chosen_tokens")) data.chosen_tokens = extract_vector<uint32_t>(normalized.at("chosen_tokens"));
    if (normalized.count("chosen_probs")) data.chosen_probs = extract_vector<float>(normalized.at("chosen_probs"));
    if (normalized.count("sampling_u")) data.sampling_u = extract_vector<float>(normalized.at("sampling_u"));
    if (normalized.count("softmax_normalizers")) data.softmax_normalizers = extract_vector<float>(normalized.at("softmax_normalizers"));
    if (normalized.count("prompt_tokens")) data.prompt_tokens = extract_vector<uint32_t>(normalized.at("prompt_tokens"));
    if (normalized.count("pad_mask")) data.pad_mask = extract_vector<bool>(normalized.at("pad_mask"));
    
    // 2D arrays
    if (normalized.count("topk_logits")) data.topk_logits = extract_matrix<float>(normalized.at("topk_logits"));
    if (normalized.count("topk_indices")) data.topk_indices = extract_matrix<uint32_t>(normalized.at("topk_indices"));
    if (normalized.count("logsumexp_stats")) data.logsumexp_stats = extract_matrix<float>(normalized.at("logsumexp_stats"));
    
    return data;
}

std::vector<uint8_t> pack_proof(const ProofData& data) {
    flatbuffers::FlatBufferBuilder builder(4096);
    
    // Create string offsets
    auto model_id_offset = builder.CreateString(data.model_identifier);
    auto compute_prec_offset = builder.CreateString(data.compute_precision);
    auto ipfs_offset = builder.CreateString(data.ipfs_cid);
    auto extra_flags_offset = builder.CreateString(data.model_config_diff);
    
    // Create byte vectors
    auto target_vec = create_byte_vector(builder, data.target);
    auto vdf_vec = create_byte_vector(builder, data.vdf);
    auto hash_vec = create_byte_vector(builder, data.hash);
    auto block_hash_vec = create_byte_vector(builder, data.block_hash);
    auto header_prefix_vec = create_byte_vector(builder, data.header_prefix);
    
    // Create 1D arrays
    auto chosen_tokens_vec = create_uint32_vector(builder, data.chosen_tokens);
    auto chosen_probs_vec = create_float_vector(builder, data.chosen_probs);
    auto sampling_u_vec = create_float_vector(builder, data.sampling_u);
    auto softmax_norm_vec = create_float_vector(builder, data.softmax_normalizers);
    auto prompt_tokens_vec = create_uint32_vector(builder, data.prompt_tokens);
    auto pad_mask_vec = create_bool_vector(builder, data.pad_mask);
    
    // Create 2D arrays using the same pattern as the working proof_writer
    // Create topk_logits 2D array
    std::vector<flatbuffers::Offset<proof::FloatArray>> logits_offsets;
    logits_offsets.reserve(data.topk_logits.size());
    for (const auto& row : data.topk_logits) {
        logits_offsets.push_back(
            proof::CreateFloatArray(builder, builder.CreateVector(row))
        );
    }
    auto topk_logits_vec = builder.CreateVector(logits_offsets);
    
    // Create topk_indices 2D array
    std::vector<flatbuffers::Offset<proof::UIntArray>> indices_offsets;
    indices_offsets.reserve(data.topk_indices.size());
    for (const auto& row : data.topk_indices) {
        indices_offsets.push_back(
            proof::CreateUIntArray(builder, builder.CreateVector(row))
        );
    }
    auto topk_indices_vec = builder.CreateVector(indices_offsets);
    
    // Create logsumexp 2D array
    std::vector<flatbuffers::Offset<proof::FloatArray>> lse_offsets;
    lse_offsets.reserve(data.logsumexp_stats.size());
    for (const auto& row : data.logsumexp_stats) {
        lse_offsets.push_back(
            proof::CreateFloatArray(builder, builder.CreateVector(row))
        );
    }
    auto logsumexp_vec = builder.CreateVector(lse_offsets);
    
    // Build the Proof
    proof::ProofBuilder pb(builder);
    pb.add_version(data.version);
    pb.add_tick(data.tick);
    pb.add_timestamp(data.timestamp);
    pb.add_is_solution(data.is_solution);
    pb.add_model_identifier(model_id_offset);
    pb.add_compute_precision(compute_prec_offset);
    pb.add_ipfs_cid(ipfs_offset);
    pb.add_extra_flags(extra_flags_offset);
    pb.add_temperature(data.temperature);
    pb.add_top_p(data.top_p);
    pb.add_top_k(data.top_k);
    pb.add_repetition_penalty(data.repetition_penalty);
    pb.add_target(target_vec);
    pb.add_vdf(vdf_vec);
    pb.add_hash(hash_vec);
    pb.add_block_hash(block_hash_vec);
    pb.add_header_prefix(header_prefix_vec);
    pb.add_chosen_tokens(chosen_tokens_vec);
    pb.add_chosen_probs(chosen_probs_vec);
    pb.add_sampling_u(sampling_u_vec);
    pb.add_softmax_normalizers(softmax_norm_vec);
    pb.add_prompt_tokens(prompt_tokens_vec);
    pb.add_pad_mask(pad_mask_vec);
    pb.add_topk_logits(topk_logits_vec);
    pb.add_topk_indices(topk_indices_vec);
    pb.add_logsumexp_stats(logsumexp_vec);
    
    auto proof_offset = pb.Finish();
    builder.Finish(proof_offset);
    
    // Get the buffer
    const uint8_t* buf = builder.GetBufferPointer();
    size_t size = builder.GetSize();
    
    return std::vector<uint8_t>(buf, buf + size);
}

std::vector<uint8_t> pack_proof_from_dict(const std::unordered_map<std::string, std::any>& proof_dict) {
    ProofData data = dict_to_proof_data(proof_dict);
    return pack_proof(data);
}

} // namespace proofpack