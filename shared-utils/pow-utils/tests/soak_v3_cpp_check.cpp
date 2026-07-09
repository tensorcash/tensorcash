// C++ cross-check of the offline-replay soak (TIP-0003).
//
// Reads the REALLY-GROUND nonces the Python soak accepted (embedded via
// tests/gen_soak_cases_header.py -> tests/vectors/soak_grinded_cases_embedded.h)
// and, for each case, recomputes pow_v3::admission_message + argon2id_digest +
// admission_target_le and asserts:
//   * C++ argon2id digest == the Python digest, byte-exact (real + tamper);
//   * C++ admission_target_le == the Python target_le, byte-exact;
//   * C++ admission_valid verdict == the Python verdict (accept real, reject tamper).
// This is the Python==C++ proof on non-trivial, grinded inputs — not fixed vectors.
//
// Build: tests/build_soak_v3_cpp_check.sh (reuses build_v3_cpp_test.sh's
// compiler/flag/lib detection; requires OpenSSL + libargon2).

#include "pow_v3.h"
#include "vectors/soak_grinded_cases_embedded.h"

#include <array>
#include <cstdio>
#include <cstring>
#include <string>
#include <vector>

static int g_checks = 0;
static int g_failures = 0;

#define CHECK(cond, ...)                                             \
    do {                                                            \
        ++g_checks;                                                 \
        if (!(cond)) {                                              \
            ++g_failures;                                           \
            std::printf("FAIL %s:%d  %s\n    ", __FILE__, __LINE__, \
                        #cond);                                     \
            std::printf(__VA_ARGS__);                               \
            std::printf("\n");                                      \
        }                                                           \
    } while (0)

static std::string to_hex(const uint8_t* data, size_t len) {
    static const char* digits = "0123456789abcdef";
    std::string out;
    out.reserve(2 * len);
    for (size_t i = 0; i < len; ++i) {
        out.push_back(digits[data[i] >> 4]);
        out.push_back(digits[data[i] & 0x0F]);
    }
    return out;
}

static std::array<uint8_t, 32> to_arr32(const uint8_t* p) {
    std::array<uint8_t, 32> a{};
    std::memcpy(a.data(), p, 32);
    return a;
}

// Recompute one (nonce) through the committed C++ admission math and cross-check
// against the embedded Python digest + verdict.
static void check_nonce(const soak_cases::SoakCase& c,
                        const std::vector<uint8_t>& msg_w,
                        const std::array<uint8_t, 32>& commitment,
                        const std::array<uint8_t, 32>& target_le,
                        const uint8_t* nonce, const uint8_t* py_digest,
                        bool py_valid, const char* label) {
    std::vector<uint8_t> msg = pow_v3::admission_message(
        msg_w, c.model_identifier, nonce, commitment);
    auto digest = pow_v3::argon2id_digest(msg);

    // 1) byte-exact digest equality with Python.
    std::string got = to_hex(digest.data(), digest.size());
    std::string want = to_hex(py_digest, 32);
    CHECK(got == want, "[%s/%s] C++ argon2 digest %s != Python %s",
          c.name, label, got.c_str(), want.c_str());

    // 2) verdict equality with Python (accept real, reject tamper).
    bool cpp_valid = pow_v3::admission_valid(digest, target_le);
    CHECK(cpp_valid == py_valid,
          "[%s/%s] C++ admission_valid=%d != Python=%d", c.name, label,
          cpp_valid ? 1 : 0, py_valid ? 1 : 0);
}

int main() {
    for (size_t i = 0; i < soak_cases::kSoakCount; ++i) {
        const auto& c = soak_cases::kSoak[i];
        std::vector<uint8_t> msg_w(c.msg_w, c.msg_w + c.msg_w_len);
        auto commitment = to_arr32(c.commitment);
        auto py_target_le = to_arr32(c.target_le);

        // C++ target derivation must match Python's embedded target, byte-exact.
        auto cpp_target_le = pow_v3::admission_target_le(c.difficulty);
        CHECK(cpp_target_le == py_target_le,
              "[%s] C++ target_le %s != Python %s", c.name,
              to_hex(cpp_target_le.data(), 32).c_str(),
              to_hex(py_target_le.data(), 32).c_str());

        // Real ground nonce: C++ must ACCEPT (py_valid==true).
        check_nonce(c, msg_w, commitment, cpp_target_le, c.nonce, c.digest,
                    c.real_valid, "real");
        // Tampered nonce: C++ must REJECT (py_valid==false).
        check_nonce(c, msg_w, commitment, cpp_target_le, c.tamper_nonce,
                    c.tamper_digest, c.tamper_valid, "tamper");
    }

    if (g_failures == 0) {
        std::printf("OK: %d checks passed over %zu soak cases "
                    "(C++ verdicts == Python on really-ground nonces)\n",
                    g_checks, soak_cases::kSoakCount);
        return 0;
    }
    std::printf("FAILED: %d of %d checks\n", g_failures, g_checks);
    return 1;
}
