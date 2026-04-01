// SPDX-License-Identifier: Apache-2.0
// debug_messages.cpp - Debug message construction
#include "pow_utils.h"
#include <iostream>
#include <iomanip>
#include <openssl/sha.h>

int main() {
    std::cout << "=== C++ Debug Output ===" << std::endl;
    
    // Test exact same values as Python
    auto h_b = hex_to_bytes("0000000000000000000000000000000000000000000000000000000000000001");
    auto v = hex_to_bytes("0000000000000000000000000000000000000000000000000000000000000002");
    int tick = 100;
    int32_t step = 42;
    
    std::cout << "h_b bytes: " << bytes_to_hex(h_b) << std::endl;
    std::cout << "v bytes: " << bytes_to_hex(v) << std::endl;
    
    // Context tokens
    std::vector<int64_t> context = {1234, 5678};
    std::cout << "Context tokens: [" << context[0] << ", " << context[1] << "]" << std::endl;
    
    auto ctx_bytes = tok_le_bytes(context);
    std::cout << "Context bytes (" << ctx_bytes.size() << " bytes): " << bytes_to_hex(ctx_bytes) << std::endl;
    
    // Step bytes
    auto j4 = u32le(step);
    std::cout << "Step (j4) bytes: " << bytes_to_hex(j4) << std::endl;
    
    // Tick bytes - IMPORTANT: Should be 8 bytes total!
    auto T8_32 = u32le(tick);
    std::vector<uint8_t> T8(8, 0);  // 8 bytes, initialized to 0
    std::copy(T8_32.begin(), T8_32.end(), T8.begin());  // Copy first 4 bytes
    std::cout << "Tick (T8) bytes (8 bytes total): " << bytes_to_hex(T8) << std::endl;
    
    // Precision bytes
    auto precision = str_bytes("fp16");
    std::cout << "Precision bytes: " << bytes_to_hex(precision) << std::endl;
    
    // Build message components
    std::cout << "\nMessage components in order:" << std::endl;
    std::cout << "  h_b (32): " << bytes_to_hex(h_b) << " (" << h_b.size() << " bytes)" << std::endl;
    std::cout << "  v (32): " << bytes_to_hex(v) << " (" << v.size() << " bytes)" << std::endl;
    std::cout << "  T8 (8): " << bytes_to_hex(T8) << " (" << T8.size() << " bytes)" << std::endl;
    std::cout << "  j4 (4): " << bytes_to_hex(j4) << " (" << j4.size() << " bytes)" << std::endl;
    std::cout << "  ctx_bytes (16): " << bytes_to_hex(ctx_bytes) << " (" << ctx_bytes.size() << " bytes)" << std::endl;
    std::cout << "  precision (4): " << bytes_to_hex(precision) << " (" << precision.size() << " bytes)" << std::endl;
    
    // Build complete message
    auto msg = build_msg(h_b, v, T8, j4, ctx_bytes, precision);
    
    std::cout << "\nTotal message length: " << msg.size() << " bytes" << std::endl;
    std::cout << "Complete message hex:\n" << bytes_to_hex(msg) << std::endl;
    
    // Compute hash
    unsigned char hash[SHA256_DIGEST_LENGTH];
    SHA256_CTX sha256;
    SHA256_Init(&sha256);
    SHA256_Update(&sha256, msg.data(), msg.size());
    SHA256_Final(hash, &sha256);
    
    std::vector<uint8_t> digest(hash, hash + SHA256_DIGEST_LENGTH);
    std::cout << "\nSHA256 digest: " << bytes_to_hex(digest) << std::endl;
    
    // Compute U value
    float u = digest_to_u(digest);
    std::cout << "U value: " << std::fixed << std::setprecision(10) << u << std::endl;
    
    // Test CDF sampling
    std::vector<float> cdf = {0.1f, 0.3f, 0.6f, 0.8f, 0.9f, 0.95f, 0.99f, 1.0f};
    auto it = std::lower_bound(cdf.begin(), cdf.end(), u);
    int64_t token_id = std::distance(cdf.begin(), it);
    std::cout << "Token ID: " << token_id << std::endl;
    
    return 0;
}