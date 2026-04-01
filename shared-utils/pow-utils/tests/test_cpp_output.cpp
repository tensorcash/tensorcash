// SPDX-License-Identifier: Apache-2.0
// test_cpp_output.cpp - Outputs test vectors for cross-language comparison
#include <iostream>
#include <vector>
#include <iomanip>
#include <sstream>
#include <cstring>

// Simple implementations to test without dependencies
std::string bytes_to_hex(const std::vector<uint8_t>& bytes) {
    std::stringstream ss;
    for (uint8_t b : bytes) {
        ss << std::hex << std::setw(2) << std::setfill('0') << (int)b;
    }
    return ss.str();
}

std::vector<uint8_t> hex_to_bytes(const std::string& hex) {
    std::vector<uint8_t> bytes;
    for (size_t i = 0; i < hex.length(); i += 2) {
        std::string byteString = hex.substr(i, 2);
        uint8_t byte = (uint8_t)strtol(byteString.c_str(), nullptr, 16);
        bytes.push_back(byte);
    }
    return bytes;
}

std::vector<uint8_t> tok_le_bytes(int64_t token) {
    std::vector<uint8_t> bytes(8);
    for (int i = 0; i < 8; i++) {
        bytes[i] = (token >> (i * 8)) & 0xFF;
    }
    return bytes;
}

std::vector<uint8_t> u32le(uint32_t value) {
    std::vector<uint8_t> bytes(4);
    bytes[0] = value & 0xFF;
    bytes[1] = (value >> 8) & 0xFF;
    bytes[2] = (value >> 16) & 0xFF;
    bytes[3] = (value >> 24) & 0xFF;
    return bytes;
}

float digest_to_u(const std::vector<uint8_t>& digest) {
    // First 4 bytes as little-endian uint32
    uint32_t val = 0;
    if (digest.size() >= 4) {
        val = digest[0] | (digest[1] << 8) | (digest[2] << 16) | (digest[3] << 24);
    }
    return (float)val / 4294967296.0f;
}

int main() {
    // Output JSON test vectors that Python can verify
    std::cout << "{" << std::endl;
    
    // Test 1: hex_to_bytes
    {
        std::string input = "0123456789abcdef";
        auto result = hex_to_bytes(input);
        std::cout << "  \"hex_to_bytes\": {" << std::endl;
        std::cout << "    \"input\": \"" << input << "\"," << std::endl;
        std::cout << "    \"output\": \"" << bytes_to_hex(result) << "\"" << std::endl;
        std::cout << "  }," << std::endl;
    }
    
    // Test 2: tok_le_bytes
    {
        int64_t token = 0x0123456789ABCDEF;
        auto result = tok_le_bytes(token);
        std::cout << "  \"tok_le_bytes\": {" << std::endl;
        std::cout << "    \"input\": " << token << "," << std::endl;
        std::cout << "    \"output\": \"" << bytes_to_hex(result) << "\"" << std::endl;
        std::cout << "  }," << std::endl;
    }
    
    // Test 3: u32le
    {
        uint32_t value = 0x12345678;
        auto result = u32le(value);
        std::cout << "  \"u32le\": {" << std::endl;
        std::cout << "    \"input\": " << value << "," << std::endl;
        std::cout << "    \"output\": \"" << bytes_to_hex(result) << "\"" << std::endl;
        std::cout << "  }," << std::endl;
    }
    
    // Test 4: digest_to_u
    {
        std::vector<uint8_t> digest = {0x00, 0x00, 0x00, 0x01};
        float u_value = digest_to_u(digest);
        std::cout << "  \"digest_to_u\": {" << std::endl;
        std::cout << "    \"input\": \"" << bytes_to_hex(digest) << "\"," << std::endl;
        std::cout << "    \"output\": " << std::fixed << std::setprecision(10) << u_value << std::endl;
        std::cout << "  }" << std::endl;
    }
    
    std::cout << "}" << std::endl;
    
    return 0;
}