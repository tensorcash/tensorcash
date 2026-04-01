// SPDX-License-Identifier: Apache-2.0
#include "proof_processor.h"
#include "pow_zmq_writer.h"
#include "pfunpack/libproofpack.h"
#include <openssl/sha.h>
#include <cstdlib>
#include <iostream>
#include <chrono>

// Helper to get environment variable with default
static bool get_env_bool(const char* name, bool default_val) {
    const char* value = std::getenv(name);
    if (!value) return default_val;
    std::string str(value);
    return str == "1" || str == "true" || str == "True";
}

ProofProcessor::ProofProcessor() 
    : proxy_audit_enabled_(get_env_bool("POW_PROXY_ENABLE", false)) {
    // Create the submitter which owns the ZMQ context and queue
    submitter_ = std::make_unique<MiningResponseSubmitter>();
}

ProofProcessor::ProofProcessor(bool proxy_audit_enabled) 
    : proxy_audit_enabled_(proxy_audit_enabled) {
    // Create the submitter which owns the ZMQ context and queue
    submitter_ = std::make_unique<MiningResponseSubmitter>();
}

ProofProcessor::~ProofProcessor() = default;

size_t ProofProcessor::get_queue_size() const {
    if (submitter_) {
        auto stats = submitter_->get_stats();
        return stats.queue_size;
    }
    return 0;
}

uint32_t ProofProcessor::extract_nonce(py::array_t<uint8_t> digest) {
    auto buf = digest.request();
    if (buf.size < 4) {
        throw std::runtime_error("Digest too small to extract nonce");
    }
    
    // Little-endian uint32 from first 4 bytes
    const uint8_t* data = static_cast<const uint8_t*>(buf.ptr);
    return data[0] | (data[1] << 8) | (data[2] << 16) | (data[3] << 24);
}

uint32_t ProofProcessor::compute_adjusted_bits(const std::vector<uint8_t>& target) {
    // Use the proper get_compact implementation from pow_utils.cpp
    if (target.size() != 32) {
        return 0;
    }
    
    // Convert bytes to integer (big endian) - find first non-zero byte
    int size = 0;
    uint64_t mantissa = 0;
    
    // Find the byte size (skip leading zeros)
    for (int i = 0; i < 32; i++) {
        if (target[i] != 0) {
            size = 32 - i;
            break;
        }
    }
    
    if (size == 0) {
        return 0;
    }
    
    // Extract mantissa based on size
    if (size <= 3) {
        // Shift target to fit into 3-byte mantissa
        int start_idx = 32 - size;
        for (int i = 0; i < size; i++) {
            mantissa = (mantissa << 8) | target[start_idx + i];
        }
        mantissa <<= (8 * (3 - size));
    } else {
        // Take the first 3 bytes of the significant part
        int start_idx = 32 - size;
        mantissa = (static_cast<uint64_t>(target[start_idx]) << 16) |
                   (static_cast<uint64_t>(target[start_idx + 1]) << 8) |
                   static_cast<uint64_t>(target[start_idx + 2]);
    }
    
    // If the sign bit (0x00800000) is set, shift mantissa down and bump exponent
    if (mantissa & 0x00800000) {
        mantissa >>= 8;
        size += 1;
    }
    
    // Compose compact: 1-byte exponent, 3-byte mantissa
    uint32_t compact = (size << 24) | (mantissa & 0x007fffff);
    
    return compact;
}

template<typename T>
std::vector<T> ProofProcessor::array_to_vector(py::array_t<T> arr) {
    auto buf = arr.request();
    T* ptr = static_cast<T*>(buf.ptr);
    return std::vector<T>(ptr, ptr + buf.size);
}

void ProofProcessor::extract_window_arrays(
    py::dict window_data,
    std::unordered_map<std::string, std::any>& proof
) {
    // Extract 1D arrays from window_data
    if (window_data.contains("tokens")) {
        py::array_t<int32_t> tokens = window_data["tokens"].cast<py::array_t<int32_t>>();
        auto vec = array_to_vector<int32_t>(tokens);
        std::vector<uint32_t> u32_vec(vec.begin(), vec.end());
        proof["chosen_tokens"] = u32_vec;
    }
    
    if (window_data.contains("probs")) {
        py::array_t<float> probs = window_data["probs"].cast<py::array_t<float>>();
        proof["chosen_probs"] = array_to_vector<float>(probs);
    }
    
    if (window_data.contains("sampling_u")) {
        py::array_t<float> u_values = window_data["sampling_u"].cast<py::array_t<float>>();
        proof["sampling_u"] = array_to_vector<float>(u_values);
    }
    
    if (window_data.contains("softmax_normalizers")) {
        py::array_t<float> norms = window_data["softmax_normalizers"].cast<py::array_t<float>>();
        proof["softmax_normalizers"] = array_to_vector<float>(norms);
    }
    
    // Handle 2D arrays
    if (window_data.contains("topk_logits")) {
        py::array_t<float> logits = window_data["topk_logits"].cast<py::array_t<float>>();
        auto buf = logits.request();
        float* ptr = static_cast<float*>(buf.ptr);
        size_t rows = buf.shape[0];
        size_t cols = (buf.ndim > 1) ? buf.shape[1] : 1;
        
        std::vector<std::vector<float>> matrix;
        matrix.reserve(rows);
        for (size_t i = 0; i < rows; ++i) {
            std::vector<float> row(ptr + i * cols, ptr + (i + 1) * cols);
            matrix.push_back(row);
        }
        proof["topk_logits"] = matrix;
    }
    
    if (window_data.contains("topk_indices")) {
        py::array_t<int32_t> indices = window_data["topk_indices"].cast<py::array_t<int32_t>>();
        auto buf = indices.request();
        int32_t* ptr = static_cast<int32_t*>(buf.ptr);
        size_t rows = buf.shape[0];
        size_t cols = (buf.ndim > 1) ? buf.shape[1] : 1;
        
        std::vector<std::vector<uint32_t>> matrix;
        matrix.reserve(rows);
        for (size_t i = 0; i < rows; ++i) {
            std::vector<uint32_t> row;
            row.reserve(cols);
            for (size_t j = 0; j < cols; ++j) {
                row.push_back(static_cast<uint32_t>(ptr[i * cols + j]));
            }
            matrix.push_back(row);
        }
        proof["topk_indices"] = matrix;
    }
    
    if (window_data.contains("logsumexp_stats")) {
        py::array_t<float> stats = window_data["logsumexp_stats"].cast<py::array_t<float>>();
        auto buf = stats.request();
        float* ptr = static_cast<float*>(buf.ptr);
        size_t rows = buf.shape[0];
        size_t cols = (buf.ndim > 1) ? buf.shape[1] : 1;
        
        std::vector<std::vector<float>> matrix;
        matrix.reserve(rows);
        for (size_t i = 0; i < rows; ++i) {
            std::vector<float> row(ptr + i * cols, ptr + (i + 1) * cols);
            matrix.push_back(row);
        }
        proof["logsumexp_stats"] = matrix;
    }
    
    // Handle attention_mask from window_data
    // Note: attention_mask is for the window tokens, NOT for prompt tokens
    // The pad_mask in the proof schema is for prompt tokens and comes from seq_info
    if (window_data.contains("attention_mask")) {
        py::array_t<bool> mask = window_data["attention_mask"].cast<py::array_t<bool>>();
        auto vec = array_to_vector<bool>(mask);
        // Keep attention_mask for window data processing
        proof["attention_mask"] = vec;
    } else if (window_data.contains("pad_mask")) {
        // Some code might pass pad_mask in window_data, treat as attention_mask
        py::array_t<bool> mask = window_data["pad_mask"].cast<py::array_t<bool>>();
        auto vec = array_to_vector<bool>(mask);
        proof["attention_mask"] = vec;
    }
}

void ProofProcessor::extract_cache_data(
    py::dict cache_data,
    int window_size,
    std::unordered_map<std::string, std::any>& proof
) {
    // Extract prompt_tokens from archive_list
    if (cache_data.contains("archive_list")) {
        py::list archive = cache_data["archive_list"].cast<py::list>();
        std::vector<uint32_t> prompt_tokens;
        
        // If archive > window_size, take all except last window_size elements as prompt
        int archive_len = archive.size();
        if (archive_len > window_size) {
            int prompt_len = archive_len - window_size;
            prompt_tokens.reserve(prompt_len);
            for (int i = 0; i < prompt_len; ++i) {
                prompt_tokens.push_back(archive[i].cast<uint32_t>());
            }
        }
        proof["prompt_tokens"] = prompt_tokens;
    }
    
    // Extract pad_mask_list for prompt tokens (not window tokens)
    if (cache_data.contains("pad_mask_list")) {
        py::list pad_mask_list = cache_data["pad_mask_list"].cast<py::list>();
        int mask_len = pad_mask_list.size();
        
        // Extract prompt pad_mask (for tokens before the window)
        std::vector<bool> prompt_pad_mask;
        if (mask_len > window_size) {
            int prompt_len = mask_len - window_size;
            prompt_pad_mask.reserve(prompt_len);
            for (int i = 0; i < prompt_len; ++i) {
                prompt_pad_mask.push_back(pad_mask_list[i].cast<bool>());
            }
        }
        // Store the prompt's pad_mask (this is what goes in the proof schema)
        proof["pad_mask"] = prompt_pad_mask;
    } else {
        // No pad_mask_list means empty prompt pad_mask
        proof["pad_mask"] = std::vector<bool>();
    }
}

std::unordered_map<std::string, std::any> ProofProcessor::assemble_proof_dict(
    int64_t seq_id,
    int step_num,
    py::dict cache_data,
    py::dict window_data,
    py::array_t<uint8_t> digest,
    py::dict pow_hasher_data,
    py::dict seq_params,
    std::optional<std::string> completion_id
) {
    std::unordered_map<std::string, std::any> proof;
    
    // Basic metadata
    proof["version"] = uint32_t(2);
    proof["is_solution"] = is_solution_;
    
    // Extract POW hasher data
    if (pow_hasher_data.contains("tick")) {
        proof["tick"] = pow_hasher_data["tick"].cast<int64_t>();
    }
    
    if (pow_hasher_data.contains("target")) {
        py::bytes target_bytes = pow_hasher_data["target"].cast<py::bytes>();
        std::string target_str = target_bytes;
        std::vector<uint8_t> target_vec(target_str.begin(), target_str.end());
        proof["target"] = target_vec;
    }
    
    if (pow_hasher_data.contains("vdf")) {
        py::bytes vdf_bytes = pow_hasher_data["vdf"].cast<py::bytes>();
        std::string vdf_str = vdf_bytes;
        std::vector<uint8_t> vdf_vec(vdf_str.begin(), vdf_str.end());
        proof["vdf"] = vdf_vec;
    }
    
    if (pow_hasher_data.contains("block_hash")) {
        py::bytes hash_bytes = pow_hasher_data["block_hash"].cast<py::bytes>();
        std::string hash_str = hash_bytes;
        std::vector<uint8_t> hash_vec(hash_str.begin(), hash_str.end());
        proof["block_hash"] = hash_vec;
    }
    
    if (pow_hasher_data.contains("header_prefix")) {
        py::bytes prefix_bytes = pow_hasher_data["header_prefix"].cast<py::bytes>();
        std::string prefix_str = prefix_bytes;
        std::vector<uint8_t> prefix_vec(prefix_str.begin(), prefix_str.end());
        proof["header_prefix"] = prefix_vec;
    }
    
    if (pow_hasher_data.contains("ipfs_cid")) {
        proof["ipfs_cid"] = pow_hasher_data["ipfs_cid"].cast<std::string>();
    }
    
    if (pow_hasher_data.contains("window_size")) {
        proof["window_size"] = pow_hasher_data["window_size"].cast<uint32_t>();
    }
    
    // Extract sequence parameters
    if (seq_params.contains("temperature")) {
        proof["temperature"] = seq_params["temperature"].cast<float>();
    }
    if (seq_params.contains("top_p")) {
        proof["top_p"] = seq_params["top_p"].cast<float>();
    }
    if (seq_params.contains("top_k")) {
        proof["top_k"] = seq_params["top_k"].cast<uint32_t>();
    }
    if (seq_params.contains("repetition_penalty")) {
        proof["repetition_penalty"] = seq_params["repetition_penalty"].cast<float>();
    }

    // Use model metadata from ProofProcessor fields (set during init, not per-proof)
    proof["model_identifier"] = model_identifier_.empty() ? "unknown" : model_identifier_;
    proof["compute_precision"] = compute_precision_.empty() ? "fp16" : compute_precision_;
    proof["model_config_diff"] = model_config_diff_.empty() ? "" : model_config_diff_;
    
    // Add completion_id if present (for MiningResponse, not in Proof flatbuffer)
    if (completion_id.has_value()) {
        proof["completion_id"] = completion_id.value();
    }
    
    // Add digest/hash
    auto digest_buf = digest.request();
    uint8_t* digest_ptr = static_cast<uint8_t*>(digest_buf.ptr);
    std::vector<uint8_t> digest_vec(digest_ptr, digest_ptr + digest_buf.size);
    proof["hash"] = digest_vec;
    
    // Add timestamp (in seconds to match Python's int(time.time()))
    auto now = std::chrono::system_clock::now();
    auto timestamp = std::chrono::duration_cast<std::chrono::seconds>(
        now.time_since_epoch()
    ).count();
    proof["timestamp"] = static_cast<int64_t>(timestamp);
    
    // Extract window data arrays
    extract_window_arrays(window_data, proof);
    
    // Extract cache data (prompt tokens)
    int window_size = 256;  // Default
    if (pow_hasher_data.contains("window_size")) {
        window_size = pow_hasher_data["window_size"].cast<int>();
    }
    extract_cache_data(cache_data, window_size, proof);
    
    return proof;
}

py::dict ProofProcessor::process_proof(
    int64_t seq_id,
    int step_num,
    py::dict cache_data,
    py::dict window_data,
    py::array_t<uint8_t> digest,
    bool is_solution,
    py::dict pow_hasher_data,
    py::dict seq_params,
    std::optional<std::string> completion_id,
    bool audit_emit,
    bool is_share
) {
    // Store is_solution for use in assemble_proof_dict. Audit proofs are
    // never mining solutions — force the wire bit False so nothing
    // downstream can misread them as a block hit.
    is_solution_ = audit_emit ? false : is_solution;

    // Assemble proof dict synchronously
    auto proof_dict = assemble_proof_dict(
        seq_id, step_num, cache_data, window_data, digest,
        pow_hasher_data, seq_params, completion_id
    );

    // Stamp the explicit audit purpose marker into model_config_diff,
    // which serialize_response writes verbatim into Proof.extra_flags.
    // The ProofCollector branches on proof_purpose=audit BEFORE its
    // mining filters; completion_id rides MiningResponse.CompletionId
    // separately, so retrieval is unaffected. Merge into existing JSON
    // when present, else emit a fresh object.
    if (audit_emit) {
        std::string existing;
        if (proof_dict.count("model_config_diff")) {
            try {
                existing = std::any_cast<std::string>(proof_dict.at("model_config_diff"));
            } catch (...) { existing = ""; }
        }
        std::string trimmed = existing;
        // strip surrounding whitespace
        size_t b = trimmed.find_first_not_of(" \t\r\n");
        size_t e = trimmed.find_last_not_of(" \t\r\n");
        trimmed = (b == std::string::npos) ? "" : trimmed.substr(b, e - b + 1);
        if (trimmed.size() >= 2 && trimmed.front() == '{' && trimmed.back() == '}') {
            // Insert the key into the existing JSON object.
            std::string inner = trimmed.substr(1, trimmed.size() - 2);
            size_t inner_b = inner.find_first_not_of(" \t\r\n");
            if (inner_b == std::string::npos) {
                proof_dict["model_config_diff"] = std::string("{\"proof_purpose\":\"audit\"}");
            } else {
                proof_dict["model_config_diff"] =
                    std::string("{\"proof_purpose\":\"audit\",") + inner + "}";
            }
        } else {
            proof_dict["model_config_diff"] = std::string("{\"proof_purpose\":\"audit\"}");
        }
    }
    
    // Normalize field names (attention_mask → pad_mask for schema)
    // Already handled in extract_window_arrays - we set both fields
    
    // Serialize proof to compute hash and get bytes
    std::vector<uint8_t> proof_bytes = proofpack::pack_proof_from_dict(proof_dict);
    
    // Compute SHA256 hash of serialized proof
    std::vector<uint8_t> pow_blob_hash(SHA256_DIGEST_LENGTH);
    SHA256(proof_bytes.data(), proof_bytes.size(), pow_blob_hash.data());
    
    // Extract metadata
    uint32_t nonce = extract_nonce(digest);
    
    // Get target and compute adjusted bits
    uint32_t adjusted_bits = 0;
    if (pow_hasher_data.contains("target")) {
        py::bytes target_bytes = pow_hasher_data["target"].cast<py::bytes>();
        std::string target_str = target_bytes;
        std::vector<uint8_t> target_vec(target_str.begin(), target_str.end());
        adjusted_bits = compute_adjusted_bits(target_vec);
    }
    
    // Get request_id and difficulty
    int64_t req_id = 0;
    uint32_t difficulty = 0;
    if (pow_hasher_data.contains("request_id")) {
        req_id = pow_hasher_data["request_id"].cast<int64_t>();
    }
    if (pow_hasher_data.contains("difficulty")) {
        difficulty = pow_hasher_data["difficulty"].cast<uint32_t>();
    }
    
    // Submit to writer (async, returns immediately).
    //
    // Slice 11.4 — three branches:
    //   1. Block-tier solution (is_solution=true): submit_solution,
    //      which in broker mode forwards to miner-proxy and in
    //      local_miner mode forwards to Core Node.
    //   2. Sub-block share (is_solution=false, is_share=true) in
    //      BROKER mode: submit_share — same egress as block solutions;
    //      the worker classifies on Proof.is_solution (which the
    //      serializer at pow_zmq_writer.cpp:~537 reads from proof_dict,
    //      so the bit makes it onto the wire as false for shares).
    //   3. Legacy audit (POW_PROXY_ENABLE=true, non-broker):
    //      submit_proof_for_audit on the proxy channel.
    //
    // The explicit is_share guard matters: a non-solution proof is not
    // automatically an accounting share. It must have passed the same
    // header-hash <= adjusted_share_target predicate that the broker
    // verify-service enforces, otherwise the worker over-emits and the
    // broker correctly rejects it as above_share_target.
    bool submitted = false;
    if (audit_emit) {
        // Audit proof: completion-audit artifact, never mining. Routed
        // to the audit channel (broker mode → the single ProofCollector
        // socket; local_miner → the proxy channel). The proof_purpose
        // marker keeps it off the MINE_RESULT/MINE_SHARE path downstream.
        submitted = submitter_->submit_proof_for_audit(
            req_id,
            proof_dict
        );
    } else if (is_solution) {
        submitted = submitter_->submit_solution(
            req_id,
            nonce,
            adjusted_bits,
            pow_blob_hash,
            difficulty,
            proof_dict
        );
    } else if (is_share && submitter_->is_broker_mode()) {
        submitted = submitter_->submit_share(
            req_id,
            nonce,
            adjusted_bits,
            pow_blob_hash,
            difficulty,
            proof_dict
        );
    } else if (proxy_audit_enabled_) {
        submitted = submitter_->submit_proof_for_audit(
            req_id,
            proof_dict
        );
    }
    
    // Get current queue size
    size_t queue_size = get_queue_size();
    
    // Return metadata including proof_bytes
    py::dict result;
    result["nonce"] = nonce;
    result["adjusted_bits"] = adjusted_bits;
    result["pow_blob_hash"] = py::bytes(
        reinterpret_cast<const char*>(pow_blob_hash.data()),
        pow_blob_hash.size()
    );
    result["proof_bytes"] = py::bytes(
        reinterpret_cast<const char*>(proof_bytes.data()),
        proof_bytes.size()
    );
    result["queued"] = submitted;
    result["queue_size"] = queue_size;
    result["is_share"] = is_share;
    
    return result;
}
