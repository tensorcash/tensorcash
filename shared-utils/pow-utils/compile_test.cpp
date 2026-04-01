// SPDX-License-Identifier: Apache-2.0

// compile_test.cpp - Simple test to verify compilation
#include "pow_utils.h"
#include <iostream>

int main() {
    std::cout << "Testing compilation..." << std::endl;
    
    // Test basic functionality
    std::vector<uint8_t> test_bytes = hex_to_bytes("0123456789abcdef");
    std::cout << "hex_to_bytes test: " << (test_bytes.size() == 8 ? "PASS" : "FAIL") << std::endl;
    
    // Test bytes_to_hex
    std::string hex_str = bytes_to_hex(test_bytes);
    std::cout << "bytes_to_hex test: " << hex_str << std::endl;
    
    // Test digest_to_u
    std::vector<uint8_t> digest = {0x00, 0x00, 0x00, 0x80};
    float u = digest_to_u(digest);
    std::cout << "digest_to_u test: " << u << std::endl;
    
    // Test PowHasher
    PowHasher hasher;
    std::cout << "PowHasher created successfully" << std::endl;
    
    std::cout << "Compilation successful!" << std::endl;
    return 0;
}