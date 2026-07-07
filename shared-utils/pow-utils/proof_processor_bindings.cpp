// SPDX-License-Identifier: Apache-2.0
#include <pybind11/pybind11.h>
#include <pybind11/numpy.h>
#include <pybind11/stl.h>
#include "proof_processor.h"
#include "pow_v3.h"

#include <array>
#include <cstring>
#include <optional>
#include <stdexcept>
#include <vector>

namespace py = pybind11;

namespace {

std::array<uint8_t, 32> bytes32_from_py(py::bytes b, const char* what) {
    std::string raw = b;
    if (raw.size() != 32) {
        throw std::invalid_argument(std::string(what) +
                                    " must be exactly 32 bytes");
    }
    std::array<uint8_t, 32> out{};
    std::memcpy(out.data(), raw.data(), 32);
    return out;
}

}  // namespace

PYBIND11_MODULE(proof_processor, m) {
    m.doc() = "High-performance proof processing with C++";
    
    py::class_<ProofProcessor>(m, "ProofProcessor")
        .def(py::init<>(),
             "Initialize ProofProcessor with environment-based configuration")
        .def(py::init<bool>(),
             py::arg("proxy_audit_enabled") = false,
             "Initialize ProofProcessor with optional proxy audit setting")
        
        .def("process_proof",
             &ProofProcessor::process_proof,
             py::arg("seq_id"),
             py::arg("step_num"),
             py::arg("cache_data"),
             py::arg("window_data"),
             py::arg("digest"),
             py::arg("is_solution"),
             py::arg("pow_hasher_data"),
             py::arg("seq_params"),
             py::arg("completion_id") = py::none(),
             py::arg("audit_emit") = false,
             py::arg("is_share") = false,
             "Process proof with GIL release, returns immediately with metadata")
        
        .def("get_queue_size",
             &ProofProcessor::get_queue_size,
             "Get current queue size")

        .def("set_model_identifier",
             &ProofProcessor::set_model_identifier,
             py::arg("identifier"),
             "Set model identifier (called once during init)")

        .def("set_compute_precision",
             &ProofProcessor::set_compute_precision,
             py::arg("precision"),
             "Set compute precision (called once during init)")

        .def("set_model_config_diff",
             &ProofProcessor::set_model_config_diff,
             py::arg("config_diff"),
             "Set model config diff (called once during init)")

        .def("set_proof_version",
             &ProofProcessor::set_proof_version,
             py::arg("version"),
             "Set proof schema version (2 = legacy, >= 3 enables the v3 "
             "extra_flags admission-nonce carrier, TIP-0003)")

        .def("get_proof_version",
             &ProofProcessor::get_proof_version,
             "Get proof schema version");

    // ---- pow_v3 module-level helpers (TIP-0003) --------------- //
    // The vLLM sampler schedules the admission grind per row/window but the
    // attempt loop runs here in C++/libargon2 with the GIL RELEASED — no
    // Python nonce loop exists.

    m.def("admission_grind",
          [](py::bytes msg_w, const std::string& model_identifier,
             py::bytes target_le, uint64_t max_tries,
             py::bytes prompt_commitment) -> py::object {
              std::string mw = msg_w;
              std::vector<uint8_t> msg_w_vec(mw.begin(), mw.end());
              std::array<uint8_t, 32> target =
                  bytes32_from_py(target_le, "target_le");
              std::array<uint8_t, 32> commitment =
                  bytes32_from_py(prompt_commitment, "prompt_commitment");
              std::optional<std::array<uint8_t, 32>> found;
              {
                  py::gil_scoped_release release;
                  found = pow_v3::admission_grind(msg_w_vec, model_identifier,
                                                  target, max_tries,
                                                  commitment);
              }
              if (!found) return py::none();
              return py::bytes(reinterpret_cast<const char*>(found->data()),
                               found->size());
          },
          py::arg("msg_w"), py::arg("model_identifier"), py::arg("target_le"),
          py::arg("max_tries"), py::arg("prompt_commitment"),
          "Grind Argon2id admission nonces (32 random start bytes, "
          "little-endian counter increment) until uint256_le(digest) < "
          "target_le or max_tries; returns the 32 nonce bytes or None. "
          "prompt_commitment (32 bytes, from prompt_commitment()) binds the "
          "FULL model-visible prefix into admission (TIP-0003). "
          "Releases the GIL for the whole loop (§6, §9).");

    m.def("prompt_commitment",
          [](const std::vector<int64_t>& prompt_tokens,
             const std::vector<uint8_t>& pad_mask) -> py::bytes {
              auto d = pow_v3::prompt_commitment(prompt_tokens, pad_mask);
              return py::bytes(reinterpret_cast<const char*>(d.data()),
                               d.size());
          },
          py::arg("prompt_tokens"), py::arg("pad_mask"),
          "SHA256(TC_V3_PROMPT_CTX | u32le(n_tokens) | prompt_tokens_i64le "
          "| u32le(n_mask) | pad_mask_u8) — the full-prefix commitment bound "
          "into admission (TIP-0003). pad_mask must have one "
          "entry per prompt token. Native mirror of pow_v3.prompt_commitment.");

    m.def("merge_extra_flags_v3",
          &pow_v3::merge_extra_flags_v3,
          py::arg("extra_flags"), py::arg("admission_nonce_hex"),
          "Merge {\"v3\":{\"admission_nonce\":...}} into an extra_flags/"
          "model_config_diff string, preserving existing members "
          "(TIP-0003). Native mirror of "
          "pow_v3.merge_extra_flags_v3.");

    m.def("admission_expected_tries",
          &pow_v3::admission_expected_tries,
          py::arg("difficulty"),
          py::arg("normalizer") = pow_v3::MODEL_DIFFICULTY_NORMALIZER,
          py::arg("decode_us_at_normalizer") = pow_v3::DECODE_US_AT_NORMALIZER,
          py::arg("elig_alpha_num") = pow_v3::ELIG_ALPHA_NUM,
          py::arg("elig_alpha_den") = pow_v3::ELIG_ALPHA_DEN,
          py::arg("argon_ref_us") = pow_v3::ARGON_REF_US,
          "Integer-exact expected Argon2 tries from registered difficulty "
          "(INVERSE compute scalar, TIP-0003).");

    m.def("admission_target",
          [](int64_t difficulty, uint64_t normalizer,
             uint64_t decode_us_at_normalizer, uint64_t elig_alpha_num,
             uint64_t elig_alpha_den, uint64_t argon_ref_us) {
              auto target = pow_v3::admission_target_le(
                  difficulty, normalizer, decode_us_at_normalizer,
                  elig_alpha_num, elig_alpha_den, argon_ref_us);
              return py::bytes(reinterpret_cast<const char*>(target.data()),
                               target.size());
          },
          py::arg("difficulty"),
          py::arg("normalizer") = pow_v3::MODEL_DIFFICULTY_NORMALIZER,
          py::arg("decode_us_at_normalizer") = pow_v3::DECODE_US_AT_NORMALIZER,
          py::arg("elig_alpha_num") = pow_v3::ELIG_ALPHA_NUM,
          py::arg("elig_alpha_den") = pow_v3::ELIG_ALPHA_DEN,
          py::arg("argon_ref_us") = pow_v3::ARGON_REF_US,
          "(2^256 - 1) // expected_tries as 32 LITTLE-ENDIAN bytes — the "
          "target_le input of admission_grind (TIP-0003).");
}
