// SPDX-License-Identifier: Apache-2.0
#pragma once

#include <memory>
#include <optional>
#include <unordered_map>
#include <vector>
#include <string>
#include <any>
#include <cstdint>
#include <pybind11/pybind11.h>
#include <pybind11/numpy.h>

namespace py = pybind11;

// Forward declarations
class MiningResponseSubmitter;

class ProofProcessor {
private:
    // Reuse existing MiningResponseSubmitter (owns ZMQ context/queue)
    std::unique_ptr<MiningResponseSubmitter> submitter_;

    // Configuration
    bool proxy_audit_enabled_;

    // Temporary storage for is_solution flag
    bool is_solution_;

    // Model metadata (set once during init, used for all proofs)
    std::string model_identifier_;
    std::string compute_precision_;
    std::string model_config_diff_;

    // Proof schema version: 2 = legacy, >= 3 enables the v3 carrier rules
    // (TIP-0003: admission nonce merged into extra_flags). Wired
    // into the serialized Proof.version by pow_zmq_writer/pfunpack via the
    // proof dict — never hardcoded there.
    int proof_version_ = 2;

public:
    ProofProcessor();
    explicit ProofProcessor(bool proxy_audit_enabled);
    ~ProofProcessor();
    
    // Main entry point from Python (synchronous assembly, async send)
    py::dict process_proof(
        int64_t seq_id,
        int step_num,
        py::dict cache_data,
        py::dict window_data,  // Separate window data
        py::array_t<uint8_t> digest,  // Typed array for efficiency
        bool is_solution,
        py::dict pow_hasher_data,
        py::dict seq_params,
        std::optional<std::string> completion_id,
        bool audit_emit = false,  // route to the audit channel (completion-audit cache), never mining
        bool is_share = false     // explicit sub-block share classification
    );

    // Get queue status
    size_t get_queue_size() const;

    // Setters for model metadata (called once during init)
    void set_model_identifier(const std::string& identifier) { model_identifier_ = identifier; }
    void set_compute_precision(const std::string& precision) { compute_precision_ = precision; }
    void set_model_config_diff(const std::string& config_diff) { model_config_diff_ = config_diff; }
    void set_proof_version(int version) { proof_version_ = version; }
    int get_proof_version() const { return proof_version_; }

private:
    // Helper functions
    std::unordered_map<std::string, std::any> assemble_proof_dict(
        int64_t seq_id,
        int step_num,
        py::dict cache_data,
        py::dict window_data,
        py::array_t<uint8_t> digest,
        py::dict pow_hasher_data,
        py::dict seq_params,
        std::optional<std::string> completion_id
    );
    
    uint32_t extract_nonce(py::array_t<uint8_t> digest);
    uint32_t compute_adjusted_bits(const std::vector<uint8_t>& target);
    
    // Efficient array conversion
    template<typename T>
    std::vector<T> array_to_vector(py::array_t<T> arr);
    
    // Extract arrays from window data
    void extract_window_arrays(
        py::dict window_data,
        std::unordered_map<std::string, std::any>& proof
    );
    
    // Extract archive/prompt from cache
    void extract_cache_data(
        py::dict cache_data,
        int window_size,
        std::unordered_map<std::string, std::any>& proof
    );
};
