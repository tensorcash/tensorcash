// SPDX-License-Identifier: Apache-2.0
// pow_test.cpp - Test suite for PoW utilities equivalency
#include "pow_utils.h"
#include <iostream>
#include <iomanip>
#include <cassert>
#include <cmath>
#include <flatbuffers/flatbuffers.h>

// Test data structure to share between Python and C++
struct TestVector {
    std::string name;
    std::vector<int64_t> context_tokens;
    int32_t step;
    std::string block_hash;
    std::string vdf;
    std::string target;
    int tick;
    std::string compute_precision;
    std::vector<float> cdf;
    
    // Expected outputs
    std::string expected_msg_hex;
    std::string expected_digest_hex;
    float expected_u_value;
    int64_t expected_token_id;
};

// bytes_to_hex is already defined in pow_utils.cpp and declared in pow_utils.h

// Helper function to compare floats with tolerance
bool float_equal(float a, float b, float epsilon = 1e-7f) {
    return std::fabs(a - b) < epsilon;
}

std::unordered_map<std::string, std::string> make_test_pow_params(const std::string& precision = "fp16") {
    const std::string zero32(64, '0');
    const std::string ones32(64, '1');
    const std::string ff32(64, 'f');
    const std::string header76(152, '2');

    return {
        {"block_hash", zero32},
        {"vdf", ones32},
        {"tick", "7"},
        {"target", ff32},
        {"header_prefix", header76},
        {"request_id", "99"},
        {"difficulty", "1"},
        {"model_identifier", "test/model@local"},
        {"compute_precision", precision},
        {"ipfs_cid", "QmPowTest"},
    };
}

void test_sampler_uses_snapped_domain_for_topk() {
    std::cout << "\nTest 7: Sampler Uses Snapped Domain For Top-K" << std::endl;

    PowSamplingCoordinator coordinator(256, 1);
    coordinator.initialize("/tmp/pow-test-logs", "/tmp/pow-test-proofs");

    const int seq_id = 7;
    coordinator.update_pow_params_for_sequence(seq_id, make_test_pow_params("fp16"));
    coordinator.ensure_sequences({seq_id}, {{seq_id, {}}});

    const std::vector<float> raw_logits = {
        1.0f,
        0.50040f,
        0.50030f,
        0.0f,
    };

    bool found_high_u = false;
    for (int ctx = 0; ctx < 4096; ++ctx) {
        auto result = coordinator.sample_token_complete(
            seq_id,
            raw_logits.data(),
            static_cast<int>(raw_logits.size()),
            1.0f,
            2,
            1.0f,
            {ctx},
            "fp16"
        );

        // These two logits collapse to the same fp16 bucket. Telemetry must
        // reflect that snapped domain, otherwise proof replay diverges.
        assert(float_equal(result.topk_logits[1], result.topk_logits[2], 1e-7f));

        if (result.u_value > 0.7f) {
            found_high_u = true;
            assert(result.token_id == 0);
            break;
        }
    }

    assert(found_high_u);
    std::cout << "  sampler top-k precision consistency: PASS" << std::endl;
}

void test_proof_writer_preserves_recorded_topk_and_pad_mask() {
    std::cout << "\nTest 8: ProofWriter Preserves Recorded Top-K And Pad Mask" << std::endl;

    ProofWriter writer("/tmp/pow-test-proofs");
    writer.set_model_identifier("test/model@local");
    writer.set_compute_precision("fp16");

    std::unordered_map<std::string, std::any> window_data;
    window_data["chosen_tokens"] = std::vector<int32_t>{1};
    window_data["chosen_probs"] = std::vector<float>{1.0f};
    window_data["sampling_u"] = std::vector<float>{0.5f};
    window_data["softmax_normalizers"] = std::vector<float>{1.0f};
    window_data["topk_logits"] = std::vector<std::vector<float>>{
        std::vector<float>(70, 0.0f)
    };
    window_data["topk_indices"] = std::vector<std::vector<int32_t>>{
        std::vector<int32_t>(70, 0)
    };
    window_data["logsumexp_stats"] = std::vector<std::vector<float>>{
        std::vector<float>(6, 0.0f)
    };

    auto& topk_logits = std::any_cast<std::vector<std::vector<float>>&>(window_data["topk_logits"]);
    topk_logits[0][0] = 1.0006f;

    std::unordered_map<std::string, std::any> seq_info;
    seq_info["prompt_tokens"] = std::vector<int32_t>{42};
    seq_info["pad_mask"] = std::vector<bool>{true};

    const std::vector<uint8_t> digest(32, 0);
    auto [bytes, _proof_dict] = writer.write_proof(
        1,
        1,
        window_data,
        digest,
        false,
        make_test_pow_params("fp16"),
        seq_info
    );

    auto* response = flatbuffers::GetRoot<proof::MiningResponse>(bytes.data());
    auto* proof_blob = response->pow_blob();
    const float stored = proof_blob->topk_logits()->Get(0)->values()->Get(0);
    const bool stored_pad_mask = proof_blob->pad_mask()->Get(0) != 0;

    assert(float_equal(stored, topk_logits[0][0], 1e-7f));
    assert(stored_pad_mask);
    std::cout << "  proof writer preserves recorded top-k/pad-mask: PASS" << std::endl;
}

void test_sampler_tracks_post_truncation_logz() {
    std::cout << "\nTest 9: Sampler Tracks Post-Truncation LogZ" << std::endl;

    PowSamplingCoordinator coordinator(256, 1);
    coordinator.initialize("/tmp/pow-test-logs", "/tmp/pow-test-proofs");

    const int seq_id = 8;
    coordinator.update_pow_params_for_sequence(seq_id, make_test_pow_params("fp32"));
    coordinator.ensure_sequences({seq_id}, {{seq_id, {}}});

    const std::vector<float> raw_logits = {
        10.0f,
        9.0f,
        8.0f,
        0.0f,
    };

    auto result = coordinator.sample_token_complete(
        seq_id,
        raw_logits.data(),
        static_cast<int>(raw_logits.size()),
        1.0f,
        3,
        1.0f,
        {0},
        "fp32"
    );

    const float expected_full = std::log(
        std::exp(10.0f) + std::exp(9.0f) + std::exp(8.0f) + std::exp(0.0f)
    );
    const float expected_post = std::log(std::exp(10.0f) + std::exp(9.0f));

    assert(float_equal(result.logsumexp_full, expected_full, 1e-5f));
    assert(float_equal(result.logsumexp_stats[0], expected_full, 1e-5f));
    assert(float_equal(result.softmax_log_z, expected_post, 1e-5f));
    assert(std::fabs(result.softmax_log_z - result.logsumexp_full) > 1e-3f);
    std::cout << "  sampler stores pre/full and post-truncation logZ separately: PASS" << std::endl;
}

// Test 1: Byte conversion functions
void test_byte_conversions() {
    std::cout << "Test 1: Byte Conversions" << std::endl;
    
    // Test hex_to_bytes
    {
        std::string hex = "0123456789abcdef";
        auto bytes = hex_to_bytes(hex);
        assert(bytes.size() == 8);
        assert(bytes[0] == 0x01);
        assert(bytes[7] == 0xef);
        std::cout << "  hex_to_bytes: PASS" << std::endl;
        // Emit parsed value for cross-language comparison
        std::cout << "  hex_to_bytes_bytes=" << bytes_to_hex(bytes) << std::endl;
    }
    
    // Test tok_le_bytes
    {
        std::vector<int64_t> tokens = {0x0123456789ABCDEF,  static_cast<int64_t>(0xFEDCBA9876543210ULL) };
        auto bytes = tok_le_bytes(tokens);
        assert(bytes.size() == 16);
        // Check little-endian encoding
        assert(bytes[0] == 0xEF);  // LSB of first token
        assert(bytes[7] == 0x01);  // MSB of first token
        assert(bytes[8] == 0x10);  // LSB of second token
        assert(bytes[15] == 0xFE); // MSB of second token
        std::cout << "  tok_le_bytes: PASS" << std::endl;
        // Emit parsed value for cross-language comparison (both tokens)
        std::cout << "  tok_le_bytes_bytes=" << bytes_to_hex(bytes) << std::endl;
    }
    
    // Test u32le
    {
        uint32_t value = 0x12345678;
        auto bytes = u32le(value);
        assert(bytes.size() == 4);
        assert(bytes[0] == 0x78);  // LSB
        assert(bytes[1] == 0x56);
        assert(bytes[2] == 0x34);
        assert(bytes[3] == 0x12);  // MSB
        std::cout << "  u32le: PASS" << std::endl;
        // Emit parsed value for cross-language comparison
        std::cout << "  u32le_bytes=" << bytes_to_hex(bytes) << std::endl;
    }
    
    // Test digest_to_u
    {
        std::vector<uint8_t> digest = {0x00, 0x00, 0x00, 0x80}; // 0x80000000 in little-endian
        float u = digest_to_u(digest);
        float expected = 2147483648.0f / 4294967296.0f; // 0.5
        assert(float_equal(u, expected));
        std::cout << "  digest_to_u: PASS" << std::endl;
        // Emit input and computed value
        std::cout << "  digest_to_u_input=" << bytes_to_hex(digest) << std::endl;
        std::cout << std::fixed << std::setprecision(10);
        std::cout << "  digest_to_u_value=" << u << std::endl;
    }
}

// Test 2: SHA-256 functionality
void test_sha256() {
    std::cout << "\nTest 2: SHA-256 Hashing" << std::endl;
    
    // Test vector from NIST
    std::vector<uint8_t> msg = {'a', 'b', 'c'};
    auto results = sha256_many({msg});
    auto hash = results[0];
    
    // Expected: SHA256("abc") = ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad
    std::string expected = "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad";
    std::string actual = bytes_to_hex(hash);
    
    assert(actual == expected);
    std::cout << "  SHA-256: PASS" << std::endl;
    // Emit hash for cross-language comparison
    std::cout << "  sha256_abc=" << actual << std::endl;
}

// Test 3: Message building
void test_message_building() {
    std::cout << "\nTest 3: Message Building" << std::endl;
    
    PowHasher hasher;
    
    // Set up test parameters
    std::unordered_map<std::string, std::string> payload;
    payload["block_hash"] = "0000000000000000000000000000000000000000000000000000000000000001";
    payload["vdf"] = "0000000000000000000000000000000000000000000000000000000000000002";
    payload["tick"] = "100";
    payload["target"] = "00000000ffff0000000000000000000000000000000000000000000000000000";
    
    hasher.update_from_payload(payload);
    
    // Test context tokens
    std::vector<int64_t> context = {1234, 5678};
    auto ctx_bytes = tok_le_bytes(context);
    
    // Build message components
    auto j4 = u32le(42);  // step = 42
    auto T8_32 = u32le(100);  // tick = 100
    std::vector<uint8_t> T8(8, 0);
    std::copy(T8_32.begin(), T8_32.end(), T8.begin());
    
    auto precision_bytes = str_bytes("fp16");
    
    std::cout << "  Message components:" << std::endl;
    std::cout << "    Context bytes: " << bytes_to_hex(ctx_bytes) << std::endl;
    std::cout << "    Step bytes: " << bytes_to_hex(j4) << std::endl;
    std::cout << "    Tick bytes: " << bytes_to_hex(T8) << std::endl;
    std::cout << "    Precision: " << bytes_to_hex(precision_bytes) << std::endl;
}

// Test 4: Token sampling
void test_token_sampling() {
    std::cout << "\nTest 4: Token Sampling" << std::endl;
    
    PowHasher hasher;
    
    // Set up test parameters
    std::unordered_map<std::string, std::string> payload;
    payload["block_hash"] = "0000000000000000000000000000000000000000000000000000000000000001";
    payload["vdf"] = "0000000000000000000000000000000000000000000000000000000000000002";
    payload["tick"] = "100";
    payload["target"] = "00000000ffff0000000000000000000000000000000000000000000000000000";
    
    hasher.update_from_payload(payload);
    
    // Create a simple CDF
    std::vector<float> cdf = {0.1f, 0.3f, 0.6f, 0.8f, 0.9f, 0.95f, 0.99f, 1.0f};
    
    // Test single context
    std::vector<int64_t> context = {1234, 5678};
    auto [token_id, u, digest] = hasher.sample_token(context, 42, cdf);
    
    std::cout << "  Sampling results:" << std::endl;
    std::cout << "    Digest: " << bytes_to_hex(digest) << std::endl;
    if (digest.size() >= 4) {
        std::cout << "    First 4 bytes: ";
        for (int i = 0; i < 4; i++) {
            std::cout << std::hex << std::setw(2) << std::setfill('0') 
                      << static_cast<int>(digest[i]) << " ";
        }
        std::cout << std::dec << std::endl;
        std::cout << "    U calculation: (" << static_cast<int>(digest[0]) 
                  << " + " << static_cast<int>(digest[1]) << "*256"
                  << " + " << static_cast<int>(digest[2]) << "*65536"
                  << " + " << static_cast<int>(digest[3]) << "*16777216) / 4294967296" << std::endl;
    }
    std::cout << "    U value: " << std::fixed << std::setprecision(10) << u << std::endl;
    std::cout << "    Token ID: " << token_id << std::endl;
    
    // Verify u is in [0, 1)
    assert(u >= 0.0f && u < 1.0f);
    
    // Verify token_id is valid
    assert(token_id >= 0 && token_id < static_cast<int64_t>(cdf.size()));
}

// Test 5: Hash target checking
void test_hash_target_check() {
    std::cout << "\nTest 5: Hash Target Checking" << std::endl;
    
    // Test target
    std::vector<uint8_t> target(32, 0);
    target[0] = 0x00;
    target[1] = 0x00;
    target[2] = 0x00;
    target[3] = 0x00;
    target[4] = 0xFF;
    target[5] = 0xFF;
    // Rest are zeros - this is a relatively easy target
    
    // Test hash that meets target (lower than target)
    std::vector<uint8_t> valid_hash(32, 0);
    valid_hash[4] = 0x80;  // Less than 0xFF
    
    // Test hash that doesn't meet target (higher than target)
    std::vector<uint8_t> invalid_hash(32, 0xFF);
    
    auto results = check_hash_against_target({valid_hash, invalid_hash}, target);
    
    assert(results[0] == true);   // Valid hash
    assert(results[1] == false);  // Invalid hash
    
    std::cout << "  Target checking: PASS" << std::endl;
}

// Test 6: Ring buffer operations
void test_ring_buffers() {
    std::cout << "\nTest 6: Ring Buffer Operations" << std::endl;
    
    RingBuffers buffers(256, 4);  // window_size=256, max_rows=4
    
    // Test clear operations
    buffers.clear_row(0);
    assert(buffers.steps[0] == 0);
    
    // Test increment
    buffers.increment_steps({0, 1});
    assert(buffers.steps[0] == 1);
    assert(buffers.steps[1] == 1);
    
    // Test position calculation
    buffers.steps[0] = 257;  // Should wrap to position 1
    auto positions = buffers.get_positions({0});
    assert(positions[0] == 1);  // 257 % 256 = 1
    
    std::cout << "  Ring buffer operations: PASS" << std::endl;
}

// Python test generator
void generate_python_test() {
    std::cout << "\n\nPython Test Code (copy and run this):" << std::endl;
    std::cout << "======================================" << std::endl;
    std::cout << R"(
import torch
import hashlib
from your_pow_module import tok_le_bytes, u32le, str_bytes, build_msg, digest_to_u, hex_to_bytes_tensor

# Test 1: Basic conversions
print("Test 1: Basic conversions")
tokens = torch.tensor([1234, 5678], dtype=torch.int64)
ctx_bytes = tok_le_bytes(tokens)
print(f"  Context bytes: {ctx_bytes.cpu().numpy().tobytes().hex()}")

step = torch.tensor(42, dtype=torch.int32)
j4 = u32le(step.view(-1, 1))
print(f"  Step bytes: {j4.cpu().numpy().tobytes().hex()}")

# Test 2: Message building and hashing
print("\nTest 2: Message building and hashing")
h_b = hex_to_bytes_tensor("0000000000000000000000000000000000000000000000000000000000000001")
v = hex_to_bytes_tensor("0000000000000000000000000000000000000000000000000000000000000002")
T8 = u32le(torch.tensor([100], dtype=torch.uint32))
T8_full = torch.zeros(8, dtype=torch.uint8)
T8_full[:4] = T8[0]

precision = str_bytes("fp16", batch_size=1, device='cpu')

msg = build_msg(h_b, v, T8_full, j4, ctx_bytes, precision)
print(f"  Message: {msg.cpu().numpy().tobytes().hex()}")

# Compute SHA256
msg_bytes = msg.cpu().numpy().tobytes()
digest = hashlib.sha256(msg_bytes).digest()
print(f"  Digest: {digest.hex()}")

# Convert to U
digest_tensor = torch.tensor(list(digest), dtype=torch.uint8)
u = digest_to_u(digest_tensor.unsqueeze(0))
print(f"  U value: {u.item()}")

# Test 3: CDF sampling
print("\nTest 3: CDF sampling")
cdf = torch.tensor([0.1, 0.3, 0.6, 0.8, 0.9, 0.95, 0.99, 1.0])
token_id = torch.searchsorted(cdf, u)
print(f"  Token ID: {token_id.item()}")
)" << std::endl;
    std::cout << "======================================" << std::endl;
}

int main() {
    std::cout << "Running PoW Utilities Equivalency Tests" << std::endl;
    std::cout << "=======================================" << std::endl;
    
    try {
        test_byte_conversions();
        test_sha256();
        test_message_building();
        test_token_sampling();
        test_hash_target_check();
        test_ring_buffers();
        test_sampler_uses_snapped_domain_for_topk();
        test_proof_writer_preserves_recorded_topk_and_pad_mask();
        test_sampler_tracks_post_truncation_logz();
        
        std::cout << "\nAll C++ tests passed!" << std::endl;
        
        generate_python_test();
        
    } catch (const std::exception& e) {
        std::cerr << "Test failed with exception: " << e.what() << std::endl;
        return 1;
    }
    
    return 0;
}
