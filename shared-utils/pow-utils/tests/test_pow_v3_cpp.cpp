// Golden-vector + behaviour tests for pow_v3.{h,cpp} (TIP-0003).
//
// Asserts bit-exact agreement with the Python reference (pow_v3.py) through
// the vectors in tests/vectors/v3_vectors.json, embedded at compile time via
// tests/gen_v3_vectors_header.py -> tests/vectors/v3_vectors_embedded.h.
// Build + run: tests/build_v3_cpp_test.sh (requires OpenSSL + libargon2).

#include "pow_v3.h"
#include "vectors/v3_vectors_embedded.h"

#include <cmath>
#include <cstdio>
#include <cstring>
#include <stdexcept>
#include <string>
#include <vector>

static int g_checks = 0;
static int g_failures = 0;

#define CHECK(cond, ...)                                              \
    do {                                                              \
        ++g_checks;                                                   \
        if (!(cond)) {                                                \
            ++g_failures;                                             \
            std::printf("FAIL %s:%d  %s\n    ", __FILE__, __LINE__,   \
                        #cond);                                       \
            std::printf(__VA_ARGS__);                                 \
            std::printf("\n");                                        \
        }                                                             \
    } while (0)

static std::vector<uint8_t> from_hex(const std::string& hex) {
    std::vector<uint8_t> out(hex.size() / 2);
    for (size_t i = 0; i < out.size(); ++i)
        out[i] = static_cast<uint8_t>(
            std::stoul(hex.substr(2 * i, 2), nullptr, 16));
    return out;
}

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

// Rebuild a step vector's message with pow_v3::build_step_message.
static std::vector<uint8_t> build_from_vector(
    const v3_vectors::StepHashVector& v, bool with_nonce) {
    std::vector<uint8_t> header = from_hex(v.header_prefix_hex);
    std::vector<uint8_t> vdf = from_hex(v.vdf_hex);
    std::vector<int64_t> ctx(v.context_tokens, v.context_tokens + v.context_len);
    std::vector<uint8_t> nonce;
    const uint8_t* nonce_ptr = nullptr;
    if (with_nonce && v.admission_nonce_hex != nullptr) {
        nonce = from_hex(v.admission_nonce_hex);
        nonce_ptr = nonce.data();
    }
    return pow_v3::build_step_message(header, vdf, v.tick, v.step, ctx,
                                      v.precision, nonce_ptr);
}

// ---- §7 step hashing -------------------------------------------------------

static void test_step_hash_vectors() {
    for (size_t i = 0; i < v3_vectors::kStepHashCount; ++i) {
        const auto& v = v3_vectors::kStepHash[i];
        std::vector<uint8_t> msg = build_from_vector(v, /*with_nonce=*/true);
        CHECK(msg.size() == v.message_len, "[%s] message_len %zu != %zu",
              v.name, msg.size(), v.message_len);
        auto digest = pow_v3::step_digest(msg);
        std::string digest_hex = to_hex(digest.data(), digest.size());
        CHECK(digest_hex == v.message_sha256_hex, "[%s] sha256 %s != %s",
              v.name, digest_hex.c_str(), v.message_sha256_hex);
        double u = pow_v3::step_u_from_digest(digest);
        CHECK(std::fabs(u - v.u_double) <= 1e-15, "[%s] u %.17g != %.17g",
              v.name, u, v.u_double);
    }
}

// ---- §6 Argon2id admission --------------------------------------------------

static void test_admission_vectors() {
    for (size_t i = 0; i < v3_vectors::kAdmissionCount; ++i) {
        const auto& v = v3_vectors::kAdmission[i];
        // msg_w = the no-nonce step message whose sha256 the vector pins.
        const v3_vectors::StepHashVector* base = nullptr;
        for (size_t j = 0; j < v3_vectors::kStepHashCount; ++j) {
            const auto& s = v3_vectors::kStepHash[j];
            if (s.admission_nonce_hex == nullptr &&
                std::strcmp(s.message_sha256_hex,
                            v.window_first_step_message_sha256_hex) == 0) {
                base = &s;
                break;
            }
        }
        CHECK(base != nullptr, "[%s] no matching msg_w step vector", v.name);
        if (base == nullptr) continue;
        std::vector<uint8_t> msg_w = build_from_vector(*base, false);
        std::vector<uint8_t> nonce = from_hex(v.admission_nonce_hex);
        // §6: the full model-visible prefix binds via prompt_commitment.
        std::vector<int64_t> prompt_tokens(
            v.prompt_tokens, v.prompt_tokens + v.prompt_tokens_count);
        std::vector<uint8_t> pad_mask(v.pad_mask,
                                      v.pad_mask + v.pad_mask_count);
        auto commitment = pow_v3::prompt_commitment(prompt_tokens, pad_mask);
        std::string commitment_hex = to_hex(commitment.data(),
                                            commitment.size());
        CHECK(commitment_hex == v.prompt_commitment_hex,
              "[%s] prompt_commitment %s != %s", v.name,
              commitment_hex.c_str(), v.prompt_commitment_hex);
        std::vector<uint8_t> msg = pow_v3::admission_message(
            msg_w, v.model_identifier, nonce.data(), commitment);
        CHECK(msg.size() == v.admission_message_len,
              "[%s] admission_message_len %zu != %zu", v.name, msg.size(),
              v.admission_message_len);
        auto digest = pow_v3::argon2id_digest(msg);
        std::string digest_hex = to_hex(digest.data(), digest.size());
        CHECK(digest_hex == v.argon2id_digest_hex, "[%s] argon2id %s != %s",
              v.name, digest_hex.c_str(), v.argon2id_digest_hex);
    }

    bool threw = false;
    try {
        (void)pow_v3::prompt_commitment({1, 2, 3}, {0, 1});
    } catch (const std::invalid_argument&) {
        threw = true;
    }
    CHECK(threw, "prompt_commitment must reject pad_mask length mismatch");
}

// ---- §6 target derivation ---------------------------------------------------

static void test_target_vectors() {
    for (size_t i = 0; i < v3_vectors::kTargetCount; ++i) {
        const auto& v = v3_vectors::kTargets[i];
        uint64_t tries = pow_v3::admission_expected_tries(
            v.difficulty, v.normalizer, v.decode_us_at_normalizer,
            v.elig_alpha_num, v.elig_alpha_den, v.argon_ref_us);
        CHECK(tries == v.expected_tries,
              "[difficulty=%lld] tries %llu != %llu",
              static_cast<long long>(v.difficulty),
              static_cast<unsigned long long>(tries),
              static_cast<unsigned long long>(v.expected_tries));
        auto target_le = pow_v3::admission_target_le(
            v.difficulty, v.normalizer, v.decode_us_at_normalizer,
            v.elig_alpha_num, v.elig_alpha_den, v.argon_ref_us);
        // vectors carry the target big-endian; the API is little-endian
        std::vector<uint8_t> expected_be = from_hex(v.admission_target_hex_be);
        CHECK(expected_be.size() == 32, "[difficulty=%lld] bad vector",
              static_cast<long long>(v.difficulty));
        bool match = true;
        for (int k = 0; k < 32; ++k)
            if (target_le[k] != expected_be[31 - k]) match = false;
        CHECK(match, "[difficulty=%lld] target bytes mismatch (got LE %s)",
              static_cast<long long>(v.difficulty),
              to_hex(target_le.data(), 32).c_str());
    }
    bool threw = false;
    try {
        pow_v3::admission_expected_tries(0);
    } catch (const std::invalid_argument&) {
        threw = true;
    }
    CHECK(threw, "difficulty=0 must throw");
}

// ---- §4/§5 B_cred + tier ----------------------------------------------------

static void test_b_cred_vectors() {
    CHECK(pow_v3::B_FLOOR_BITS == v3_vectors::B_FLOOR_BITS, "B_FLOOR drift");
    CHECK(pow_v3::B_FREE_BITS == v3_vectors::B_FREE_BITS, "B_FREE drift");
    CHECK(pow_v3::BCRED_R == v3_vectors::BCRED_R, "BCRED_R drift");
    CHECK(pow_v3::BCRED_N_MAX == v3_vectors::BCRED_N_MAX, "BCRED_N_MAX drift");
    CHECK(pow_v3::ATOL_Q63_CEIL == v3_vectors::ATOL_Q63_CEIL, "ATOL_Q63_CEIL drift");
    CHECK(pow_v3::ATOL == v3_vectors::ATOL, "ATOL drift");
    CHECK(pow_v3::POW_WINDOW_SIZE == v3_vectors::POW_WINDOW_SIZE,
          "window size drift");
    // Cross-language table identity: the compiled table's SHA-256 must equal
    // the constant the Python generator embedded in the vectors.
    CHECK(std::strcmp(pow_v3::BCRED_TABLE_SHA256_HEX,
                      v3_vectors::BCRED_TABLE_SHA256) == 0,
          "table SHA-256 mismatch: %s != %s", pow_v3::BCRED_TABLE_SHA256_HEX,
          v3_vectors::BCRED_TABLE_SHA256);
    for (size_t i = 0; i < v3_vectors::kBCredCount; ++i) {
        const auto& v = v3_vectors::kBCred[i];
        std::vector<double> lower(v.lower, v.lower + v.len);
        std::vector<double> upper(v.upper, v.upper + v.len);
        uint64_t units = pow_v3::b_cred_units_from_bounds(lower, upper);
        CHECK(units == v.b_cred_units, "[%s] units %llu != %llu", v.name,
              static_cast<unsigned long long>(units),
              static_cast<unsigned long long>(v.b_cred_units));
        const char* tier =
            pow_v3::tier_name(pow_v3::tier_for_b_cred_units(units));
        CHECK(std::strcmp(tier, v.tier) == 0, "[%s] tier %s != %s", v.name,
              tier, v.tier);
    }
    // §4 boundary behaviour not covered by the vectors:
    bool threw = false;
    try { pow_v3::mass_q63_for_step(0.5, 0.4); }
    catch (const std::invalid_argument&) { threw = true; }
    CHECK(threw, "upper < lower must throw");
    threw = false;
    try { pow_v3::mass_q63_for_step(std::nan(""), 0.5); }
    catch (const std::invalid_argument&) { threw = true; }
    CHECK(threw, "NaN bound must throw");
    threw = false;
    try { pow_v3::credit_units_for_step(0); }
    catch (const std::invalid_argument&) { threw = true; }
    CHECK(threw, "mass_q63 == 0 must throw (invalid interval)");
    CHECK(pow_v3::credit_units_for_step(pow_v3::mass_q63_for_step(0.0, 1.0)) == 0,
          "mass 1.0 credits 0 units");
    // tiny positive mass hits the per-step cap (table max index).
    CHECK(pow_v3::credit_units_for_step(1) == pow_v3::BCRED_N_MAX,
          "mass_q63 == 1 must cap at N_MAX units");
    // Exact conversion anchors + boundary at threshold[n] +/- 1.
    CHECK(pow_v3::f64_to_q63_floor(0.5) == (1ULL << 62), "floor(0.5*2^63)");
    CHECK(pow_v3::f64_to_q63_ceil(1.0) == pow_v3::BCRED_Q_ONE, "ceil(1.0*2^63)");
    for (size_t n : {size_t{1}, size_t{1024}, size_t{20000}, size_t{32767}}) {
        const uint64_t t = pow_v3::BCRED_THRESHOLD_Q63[n];
        CHECK(pow_v3::credit_units_for_step(t) >= n, "boundary >= n at %zu", n);
        CHECK(pow_v3::credit_units_for_step(t + 1) < n, "boundary < n at %zu", n);
    }
}

// ---- §3 carrier: extraction vectors (consensus parser bounds) ---------------

static void test_carrier_vectors() {
    CHECK(pow_v3::EXTRA_FLAGS_MAX_BYTES == v3_vectors::EXTRA_FLAGS_MAX_BYTES,
          "EXTRA_FLAGS_MAX_BYTES drift");
    CHECK(pow_v3::EXTRA_FLAGS_MAX_DEPTH == v3_vectors::EXTRA_FLAGS_MAX_DEPTH,
          "EXTRA_FLAGS_MAX_DEPTH drift");
    for (size_t i = 0; i < v3_vectors::kCarrierCount; ++i) {
        const auto& v = v3_vectors::kCarrier[i];
        auto got = pow_v3::extract_admission_nonce_hex(v.extra_flags);
        if (v.admission_nonce_hex == nullptr) {
            CHECK(!got.has_value(), "[%s] expected no nonce, got %s", v.name,
                  got ? got->c_str() : "");
        } else {
            CHECK(got.has_value() && *got == v.admission_nonce_hex,
                  "[%s] nonce %s != %s", v.name,
                  got ? got->c_str() : "(none)", v.admission_nonce_hex);
        }
    }
    // never-throw contract on hostile inputs (invalid UTF-8, deep nesting,
    // truncated escapes) — nullopt, no exception
    const char invalid_utf8[] = {'{', '"', 'a', '"', ':', '"',
                                 static_cast<char>(0xC0), static_cast<char>(0x80),
                                 '"', '}', 0};
    CHECK(!pow_v3::extract_admission_nonce_hex(invalid_utf8).has_value(),
          "invalid UTF-8 must be no-nonce");
    std::string deep;
    for (int i = 0; i < 64; ++i) deep += "[";
    for (int i = 0; i < 64; ++i) deep += "]";
    CHECK(!pow_v3::extract_admission_nonce_hex(deep).has_value(),
          "non-object top level must be no-nonce");
    CHECK(!pow_v3::extract_admission_nonce_hex("{\"a\":\"\\u12\"}").has_value(),
          "truncated unicode escape must be no-nonce");
    CHECK(!pow_v3::extract_admission_nonce_hex("{\"a\":01}").has_value(),
          "leading-zero number must be no-nonce");
    CHECK(!pow_v3::extract_admission_nonce_hex("{} {}").has_value(),
          "trailing garbage must be no-nonce");
}

// ---- §3 carrier: producer merge helper --------------------------------------

static const char* kNonceA =
    "000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f";
static const char* kNonceB =
    "ffeeddccbbaa99887766554433221100ffeeddccbbaa99887766554433221100";

static void expect_merge_throws(const std::string& nonce) {
    bool threw = false;
    try { pow_v3::merge_extra_flags_v3("{}", nonce); }
    catch (const std::invalid_argument&) { threw = true; }
    CHECK(threw, "bad nonce '%s' must throw", nonce.c_str());
}

static void test_merge_extra_flags() {
    using pow_v3::extract_admission_nonce_hex;
    using pow_v3::merge_extra_flags_v3;

    // empty input -> bare v3 object
    std::string merged = merge_extra_flags_v3("", kNonceA);
    CHECK(merged == std::string("{\"v3\":{\"admission_nonce\":\"") + kNonceA +
                        "\"}}",
          "empty merge got %s", merged.c_str());
    auto got = extract_admission_nonce_hex(merged);
    CHECK(got && *got == kNonceA, "round-trip on empty input");

    // existing keys preserved
    merged = merge_extra_flags_v3("{\"completion_id\":\"x\"}", kNonceA);
    CHECK(merged.find("\"completion_id\":\"x\"") != std::string::npos,
          "completion_id dropped: %s", merged.c_str());
    got = extract_admission_nonce_hex(merged);
    CHECK(got && *got == kNonceA, "round-trip with completion_id");

    // idempotence: re-merging the same nonce is a fixed point
    std::string twice = merge_extra_flags_v3(merged, kNonceA);
    CHECK(twice == merged, "not idempotent: %s vs %s", twice.c_str(),
          merged.c_str());

    // re-merging a DIFFERENT nonce replaces (single v3 member, new value)
    std::string swapped = merge_extra_flags_v3(merged, kNonceB);
    got = extract_admission_nonce_hex(swapped);
    CHECK(got && *got == kNonceB, "nonce not replaced: %s", swapped.c_str());
    CHECK(swapped.find(kNonceA) == std::string::npos,
          "stale nonce survives: %s", swapped.c_str());
    CHECK(swapped.find("\"completion_id\":\"x\"") != std::string::npos,
          "completion_id dropped on replace: %s", swapped.c_str());

    // non-object input preserved verbatim under _diff (pformat blob idiom)
    merged = merge_extra_flags_v3("{'legacy': pformat}", kNonceA);
    CHECK(merged.find("\"_diff\":\"{'legacy': pformat}\"") != std::string::npos,
          "_diff wrap missing: %s", merged.c_str());
    got = extract_admission_nonce_hex(merged);
    CHECK(got && *got == kNonceA, "round-trip with _diff");

    // _diff values needing JSON escaping
    merged = merge_extra_flags_v3("say \"hi\"\n", kNonceA);
    got = extract_admission_nonce_hex(merged);
    CHECK(got && *got == kNonceA, "round-trip with escaped _diff: %s",
          merged.c_str());

    // Post-merge self-check fallback: blobs the balanced scan CAN walk but
    // that are NOT valid JSON per the strict §3 extractor (duplicate keys,
    // trailing commas, single-quoted values). Without the extractor
    // self-check the splice would emit a string whose nonce is silently
    // unextractable; the fallback must land on the _diff wrap and ALWAYS
    // round-trip.
    for (const char* hostile : {"{\"a\":1,\"a\":2}",
                                "{\"a\":1,}",
                                "{\"a\":'x'}",
                                "{\"a\": +1}",
                                "{'k': 'v', \"j\": 1}"}) {
        merged = merge_extra_flags_v3(hostile, kNonceA);
        got = extract_admission_nonce_hex(merged);
        CHECK(got && *got == kNonceA,
              "merge into hostile blob %s lost the nonce: %s", hostile,
              merged.c_str());
    }

    // audit-marker interplay (proof_processor.cpp splices proof_purpose at
    // the FRONT; the v3 member lands at the END — both must survive)
    merged = merge_extra_flags_v3(
        "{\"proof_purpose\":\"audit\",\"completion_id\":\"x\"}", kNonceA);
    CHECK(merged.find("\"proof_purpose\":\"audit\"") != std::string::npos &&
              merged.find("\"completion_id\":\"x\"") != std::string::npos,
          "audit keys dropped: %s", merged.c_str());
    got = extract_admission_nonce_hex(merged);
    CHECK(got && *got == kNonceA, "round-trip with audit marker");

    // v3 member NOT at the end (whitespace, other member after) still replaced
    merged = merge_extra_flags_v3(
        "{ \"v3\" : {\"admission_nonce\":\"" + std::string(kNonceA) +
            "\"} , \"z\": 1 }",
        kNonceB);
    got = extract_admission_nonce_hex(merged);
    CHECK(got && *got == kNonceB, "mid-object v3 not replaced: %s",
          merged.c_str());
    CHECK(merged.find("\"z\"") != std::string::npos, "sibling key dropped: %s",
          merged.c_str());

    // bad nonce hex throws std::invalid_argument
    expect_merge_throws("");                             // empty
    expect_merge_throws(std::string(63, 'a'));           // short
    expect_merge_throws(std::string(65, 'a'));           // long
    expect_merge_throws(std::string(64, 'A'));           // uppercase
    expect_merge_throws(std::string(64, 'g'));           // non-hex

    CHECK(pow_v3::is_valid_admission_nonce_hex(kNonceA), "valid nonce hex");
    CHECK(!pow_v3::is_valid_admission_nonce_hex(std::string(64, 'G')),
          "uppercase must be invalid");
}

// ---- §9 admission_grind ------------------------------------------------------

static void test_admission_grind() {
    // easiest possible target: all-0xff little-endian is the max uint256, so
    // strict < fails only for an all-0xff digest — the first try wins with
    // overwhelming probability.
    std::array<uint8_t, 32> easy{};
    easy.fill(0xFF);
    std::vector<uint8_t> msg_w = build_from_vector(v3_vectors::kStepHash[0],
                                                   /*with_nonce=*/false);
    auto commitment = pow_v3::prompt_commitment({1, 2, 3}, {0, 0, 1});
    auto found = pow_v3::admission_grind(msg_w, "org/model@abcdef012345", easy,
                                         /*max_tries=*/10, commitment);
    CHECK(found.has_value(), "grind vs trivial target must find a nonce");
    if (found) {
        // the returned nonce must actually satisfy the puzzle
        auto msg = pow_v3::admission_message(msg_w, "org/model@abcdef012345",
                                             found->data(), commitment);
        auto digest = pow_v3::argon2id_digest(msg);
        CHECK(pow_v3::admission_valid(digest, easy),
              "grind returned an inadmissible nonce");
    }
    // max_tries = 0: never evaluates, returns nullopt
    auto none = pow_v3::admission_grind(msg_w, "org/model@abcdef012345", easy,
                                        /*max_tries=*/0, commitment);
    CHECK(!none.has_value(), "max_tries=0 must return nullopt");

    // admission_valid strictness: equal is NOT valid
    std::array<uint8_t, 32> x{};
    x.fill(0xAB);
    CHECK(!pow_v3::admission_valid(x, x), "equal digest/target must fail");
    std::array<uint8_t, 32> smaller = x;
    smaller[31] = 0xAA;  // most significant byte in LE order
    CHECK(pow_v3::admission_valid(smaller, x), "smaller LE value must pass");
}

int main() {
    test_step_hash_vectors();
    test_admission_vectors();
    test_target_vectors();
    test_b_cred_vectors();
    test_carrier_vectors();
    test_merge_extra_flags();
    test_admission_grind();
    if (g_failures == 0) {
        std::printf("OK: %d checks passed (pow_v3 C++ == Python golden vectors)\n",
                    g_checks);
        return 0;
    }
    std::printf("FAILED: %d of %d checks\n", g_failures, g_checks);
    return 1;
}
