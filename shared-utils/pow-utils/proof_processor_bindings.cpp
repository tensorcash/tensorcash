// SPDX-License-Identifier: Apache-2.0
#include <pybind11/pybind11.h>
#include <pybind11/numpy.h>
#include <pybind11/stl.h>
#include "proof_processor.h"

namespace py = pybind11;

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
             "Set model config diff (called once during init)");
}
