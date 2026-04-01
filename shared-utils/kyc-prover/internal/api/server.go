// SPDX-License-Identifier: Apache-2.0
package api

import (
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net/http"
	"time"

	"kyc-prover/internal/circuit"
)

// Server handles HTTP requests for proof generation
type Server struct {
	prover *circuit.Prover
	port   string
}

// NewServer creates a new API server
func NewServer(prover *circuit.Prover, port string) *Server {
	return &Server{
		prover: prover,
		port:   port,
	}
}

// Start runs the HTTP server
func (s *Server) Start() error {
	mux := http.NewServeMux()

	// Register handlers
	mux.HandleFunc("/prove", s.handleProve)
	mux.HandleFunc("/verify", s.handleVerify)
	mux.HandleFunc("/health", s.handleHealth)

	// Add middleware
	handler := loggingMiddleware(corsMiddleware(mux))

	addr := fmt.Sprintf(":%s", s.port)
	log.Printf("KYC Prover service starting on %s", addr)

	return http.ListenAndServe(addr, handler)
}

// handleProve generates a ZK proof
func (s *Server) handleProve(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		return
	}

	// Read request body
	body, err := io.ReadAll(r.Body)
	if err != nil {
		respondError(w, "Failed to read request body", http.StatusBadRequest)
		return
	}
	defer r.Body.Close()

	// Parse request
	var req circuit.ProveRequest
	if err := json.Unmarshal(body, &req); err != nil {
		respondError(w, fmt.Sprintf("Invalid JSON: %v", err), http.StatusBadRequest)
		return
	}

	// Generate proof
	start := time.Now()
	resp, err := s.prover.Prove(&req)
	if err != nil {
		respondError(w, fmt.Sprintf("Proof generation failed: %v", err), http.StatusInternalServerError)
		return
	}

	log.Printf("Proof generated in %v", time.Since(start))

	// Send response
	respondJSON(w, resp, http.StatusOK)
}

// handleVerify verifies a proof locally (for testing)
func (s *Server) handleVerify(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		return
	}

	body, err := io.ReadAll(r.Body)
	if err != nil {
		respondError(w, "Failed to read request body", http.StatusBadRequest)
		return
	}
	defer r.Body.Close()

	var req circuit.VerifyRequest
	if err := json.Unmarshal(body, &req); err != nil {
		respondError(w, fmt.Sprintf("Invalid JSON: %v", err), http.StatusBadRequest)
		return
	}

	// Parse hex inputs
	proofData, err := hexDecode(req.ProofHex)
	if err != nil {
		respondError(w, fmt.Sprintf("Invalid proof_hex: %v", err), http.StatusBadRequest)
		return
	}

	publicInputs, err := hexDecode(req.PublicInputsHex)
	if err != nil {
		respondError(w, fmt.Sprintf("Invalid public_inputs_hex: %v", err), http.StatusBadRequest)
		return
	}

	// Parse VK if provided, otherwise use server's VK
	var vkData []byte
	if req.VKHex != "" {
		vkData, err = hexDecode(req.VKHex)
		if err != nil {
			respondError(w, fmt.Sprintf("Invalid vk_hex: %v", err), http.StatusBadRequest)
			return
		}
	}

	// Verify proof
	var verifyErr error
	if vkData != nil {
		// Use provided VK
		vk, err := circuit.LoadVerificationKey(vkData)
		if err != nil {
			respondError(w, fmt.Sprintf("Failed to load VK: %v", err), http.StatusBadRequest)
			return
		}
		verifyErr = circuit.VerifyWithVK(proofData, publicInputs, vk)
	} else {
		// Use server's VK
		verifyErr = s.prover.Verify(proofData, publicInputs)
	}

	resp := circuit.VerifyResponse{
		Valid: verifyErr == nil,
	}

	if verifyErr != nil {
		resp.Error = verifyErr.Error()
	}

	respondJSON(w, resp, http.StatusOK)
}

// handleHealth returns service status
func (s *Server) handleHealth(w http.ResponseWriter, r *http.Request) {
	respondJSON(w, map[string]string{"status": "ok"}, http.StatusOK)
}

// respondJSON sends JSON response
func respondJSON(w http.ResponseWriter, data interface{}, statusCode int) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(statusCode)
	json.NewEncoder(w).Encode(data)
}

// respondError sends error response
func respondError(w http.ResponseWriter, message string, statusCode int) {
	respondJSON(w, map[string]string{"error": message}, statusCode)
}

// hexDecode decodes hex string (with or without 0x prefix)
func hexDecode(hexStr string) ([]byte, error) {
	if len(hexStr) >= 2 && hexStr[:2] == "0x" {
		hexStr = hexStr[2:]
	}

	// Use circuit package's hex decoding
	// Simple implementation for now
	var result []byte
	for i := 0; i < len(hexStr); i += 2 {
		var b byte
		_, err := fmt.Sscanf(hexStr[i:i+2], "%02x", &b)
		if err != nil {
			return nil, err
		}
		result = append(result, b)
	}
	return result, nil
}

// loggingMiddleware logs all requests
func loggingMiddleware(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		start := time.Now()
		next.ServeHTTP(w, r)
		log.Printf("%s %s %v", r.Method, r.URL.Path, time.Since(start))
	})
}

// corsMiddleware adds CORS headers
func corsMiddleware(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Access-Control-Allow-Origin", "*")
		w.Header().Set("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
		w.Header().Set("Access-Control-Allow-Headers", "Content-Type")

		if r.Method == "OPTIONS" {
			w.WriteHeader(http.StatusOK)
			return
		}

		next.ServeHTTP(w, r)
	})
}
