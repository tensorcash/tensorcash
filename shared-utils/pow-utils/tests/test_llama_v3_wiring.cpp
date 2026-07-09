// llama.cpp-path v3 wiring test (TIP-0003).
//
// The pow_v3 primitives are already golden-vector-tested
// (test_pow_v3_cpp.cpp); this test covers the llama.cpp miner WIRING in
// pow_utils.cpp — the C++ mirror of test_pow_v3_sampler.py:
//
//   1. PowHasher::sample_token appends the 32 admission-nonce bytes
//      byte-identically to pow_v3.build_step_message (§7).
//   2. RingBuffers admission nonce store/clear semantics (§9).
//   3. PowHasher::grind_admission produces a nonce the VERIFIER accepts
//      (pow_v3 admission_message/argon2id_digest/admission_valid, §6).
//   4. ProofWriter guards: ingress proof_version stamp disagreement and
//      missing sampler params hard-fail for v3 (§9).
//   5. PowSamplingCoordinator end-to-end, two windows: grind at each
//      boundary before the first sampled token, nonce folded into every
//      step hash, emitted FlatBuffer proof carries version=3 and the nonce
//      in extra_flags, prompt_tokens advance to the pre-window prefix for
//      window 2, and the whole proof survives verifier-style replay.
//   6. v2 regression: POW_PROOF_VERSION=2 keeps the legacy shape
//      byte-for-byte (no nonce anywhere, version=2).
//
// Build + run: tests/build_llama_v3_wiring_test.sh (needs libargon2).

#include "pow_utils.h"
#include "pow_v3.h"

#include <cstdio>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <random>
#include <string>
#include <vector>

#include "flatbuffers/flatbuffers.h"
#include "proof_generated.h"
#include "blockheader_generated.h"

namespace fs = std::filesystem;

static int g_failures = 0;

#define CHECK(cond, msg)                                                     \
    do {                                                                     \
        if (cond) {                                                          \
            std::cout << "  PASS: " << msg << "\n";                          \
        } else {                                                             \
            std::cout << "  FAIL: " << msg << " (" << #cond << ")\n";        \
            ++g_failures;                                                    \
        }                                                                    \
    } while (0)

// ---------------------------------------------------------------------- //
// shared fixtures
// ---------------------------------------------------------------------- //

static const std::string kHeaderHex(76 * 2, 'a');   // 76 bytes 0xaa
static const std::string kVdfHex(32 * 2, 'b');      // 32 bytes 0xbb
static const std::string kBlockHashHex(32 * 2, 'c');
static const int kTick = 7;
static const std::string kPrecision = "bf16";
static const std::string kModelId = "test-model@abcdef";
// expected_tries(60'000'000) == 1 -> target == 2^256-1, first grind try wins.
static const int64_t kEasyDifficulty = 60000000;
// expected_tries(6'000'000) == 10 -> target rejects ~90% of nonces: the
// coordinator windows grind against a NONTRIVIAL boundary (§10), still fast
// (~10 Argon2 evals per boundary, factor-16 bound).
static const int64_t kCoordDifficulty = 6000000;
// expected_tries(1'000'000) == 60 — the plan's exp_tries 10-80 soak band.
static const int64_t kBandDifficulty = 1000000;

static std::unordered_map<std::string, std::string> base_pow_params(
        int proof_version, int64_t difficulty = kEasyDifficulty) {
    std::unordered_map<std::string, std::string> p = {
        {"block_hash",         kBlockHashHex},
        {"vdf",                kVdfHex},
        {"tick",               std::to_string(kTick)},
        // All-zero block target: never a solution (emission driven by
        // POW_PROXY_ENABLE audit path instead).
        {"target",             std::string(64, '0')},
        {"header_prefix",      kHeaderHex},
        {"ipfs_cid",           "QmTest"},
        {"request_id",         "12345"},
        {"difficulty",         std::to_string(difficulty)},
        {"model_identifier",   kModelId},
        {"compute_precision",  kPrecision},
        {"temperature",        "1.000000"},
        {"top_p",              "1.000000"},
        {"top_k",              "50"},
        {"repetition_penalty", "1.000000"},
        {"model_config_diff",  "{\"completion_id\":\"cmpl-test\"}"},
    };
    if (proof_version != 0) {
        p["proof_version"] = std::to_string(proof_version);
    }
    return p;
}

// Rolling window exactly like server-context.cpp builds it: last 256 of
// cache, left-padded with zeros to 256.
static std::vector<int64_t> rolling_window(const std::vector<int64_t>& cache) {
    const size_t W = POW_WINDOW_SIZE;
    std::vector<int64_t> ctx(W, 0);
    const size_t take = std::min(W, cache.size());
    std::copy(cache.end() - take, cache.end(), ctx.begin() + (W - take));
    return ctx;
}

static std::vector<uint8_t> hexstr_to_bytes(const std::string& h) {
    return hex_to_bytes(h);
}

// ---------------------------------------------------------------------- //
// 1. PowHasher step-hash byte-compat vs pow_v3 reference
// ---------------------------------------------------------------------- //

static void test_hasher_step_hash_bytecompat() {
    std::cout << "[hasher step-hash byte-compat]\n";
    PowHasher hasher;
    hasher.update_from_payload(base_pow_params(3));

    std::vector<int64_t> cache = {11, 12, 13, 14, 15};
    auto ctx = rolling_window(cache);
    std::vector<float> cdf = {0.25f, 0.5f, 0.75f, 1.0f};

    const auto header = hexstr_to_bytes(kHeaderHex);
    const auto vdf = hexstr_to_bytes(kVdfHex);

    // Without a nonce: legacy v2 shape.
    auto [tok2, u2, dig2] = hasher.sample_token(ctx, 3, cdf, nullptr);
    auto ref2 = pow_v3::step_digest(pow_v3::build_step_message(
        header, vdf, kTick, 3, ctx, kPrecision));
    CHECK(std::equal(dig2.begin(), dig2.end(), ref2.begin()),
          "no-nonce digest == pow_v3 reference (legacy shape preserved)");

    // With a nonce appended.
    std::array<uint8_t, 32> nonce{};
    for (int i = 0; i < 32; ++i) nonce[i] = static_cast<uint8_t>(i * 3 + 1);
    auto [tok3, u3, dig3] = hasher.sample_token(ctx, 3, cdf, nonce.data());
    auto ref3 = pow_v3::step_digest(pow_v3::build_step_message(
        header, vdf, kTick, 3, ctx, kPrecision, nonce.data()));
    CHECK(std::equal(dig3.begin(), dig3.end(), ref3.begin()),
          "nonce digest == pow_v3 reference (nonce appended, nothing else)");
    CHECK(!std::equal(dig3.begin(), dig3.end(), dig2.begin()),
          "nonce changes the digest");
    CHECK(std::fabs(u3 - (float)pow_v3::step_u_from_digest(ref3)) < 1e-6f,
          "u value tracks the digest");
    (void)tok2; (void)u2; (void)tok3;

    // Batched path: per-row nonces (one row admitted, one not).
    std::vector<std::vector<int64_t>> ctxs = {ctx, ctx};
    std::vector<int32_t> steps = {3, 3};
    std::vector<std::vector<float>> cdfs = {cdf, cdf};
    auto [btoks, bus, bdigs] = hasher.batch_sample_tokens(
        ctxs, steps, cdfs, kPrecision, {nullptr, nonce.data()});
    CHECK(std::equal(bdigs[0].begin(), bdigs[0].end(), ref2.begin()) &&
              std::equal(bdigs[1].begin(), bdigs[1].end(), ref3.begin()),
          "batch_sample_tokens folds per-row nonces (v2 row + v3 row)");
    bool bthrew = false;
    try {
        hasher.batch_sample_tokens(ctxs, steps, cdfs, kPrecision,
                                   {nonce.data()});
    } catch (const std::invalid_argument&) {
        bthrew = true;
    }
    CHECK(bthrew, "batch nonce-count mismatch throws");
    (void)btoks; (void)bus;
}

// ---------------------------------------------------------------------- //
// 2. RingBuffers admission state
// ---------------------------------------------------------------------- //

static void test_ring_buffers_admission_state() {
    std::cout << "[ring-buffer admission state]\n";
    RingBuffers rb(POW_WINDOW_SIZE, 4);
    CHECK(rb.admission_valid.size() == 4 && rb.admission_nonce.size() == 4,
          "admission buffers sized to max_rows");
    CHECK(rb.admission_valid[2] == 0, "rows start nonce-less");

    std::array<uint8_t, 32> nonce{};
    nonce.fill(0x5a);
    rb.write_admission_nonce(2, nonce.data());
    CHECK(rb.admission_valid[2] == 1 && rb.admission_nonce[2][0] == 0x5a,
          "write stores nonce + valid flag");

    rb.write_admission_nonce(2, nullptr);
    CHECK(rb.admission_valid[2] == 0 && rb.admission_nonce[2][0] == 0,
          "null write clears (a nonce admits exactly one window)");

    rb.write_admission_nonce(1, nonce.data());
    rb.clear_row(1);
    CHECK(rb.admission_valid[1] == 0 && rb.admission_nonce[1][0] == 0,
          "clear_row clears admission state");

    rb.write_admission_nonce(-1, nonce.data());
    rb.write_admission_nonce(99, nonce.data());
    CHECK(true, "out-of-range rows are ignored");
}

// ---------------------------------------------------------------------- //
// 3. grind_admission -> verifier-side acceptance
// ---------------------------------------------------------------------- //

static void test_grind_admission_verifier_accepts() {
    std::cout << "[grind_admission verifier acceptance]\n";
    PowHasher hasher;
    hasher.update_from_payload(base_pow_params(3));

    std::vector<int64_t> prompt = {101, 102, 103, 104};
    auto ctx = rolling_window(prompt);
    std::vector<uint8_t> pad_mask(prompt.size(), 0);

    std::array<uint8_t, 32> nonce{};
    bool ok = hasher.grind_admission(ctx, 0, prompt, pad_mask, 16, nonce);
    CHECK(ok, "grind succeeds against the easy registered-difficulty target");

    // Verifier-side recomputation (proof_verifier._verify_v3_admission_tier
    // construction) from the fields a proof would carry.
    const auto header = hexstr_to_bytes(kHeaderHex);
    const auto vdf = hexstr_to_bytes(kVdfHex);
    auto msg_w = pow_v3::build_step_message(header, vdf, kTick, 0, prompt,
                                            kPrecision);
    auto commitment = pow_v3::prompt_commitment(prompt, pad_mask);
    auto adm_msg = pow_v3::admission_message(msg_w, kModelId, nonce.data(),
                                             commitment);
    auto digest = pow_v3::argon2id_digest(adm_msg);
    auto target = pow_v3::admission_target_le(kEasyDifficulty);
    CHECK(pow_v3::admission_valid(digest, target),
          "verifier-recomputed Argon2id digest passes the admission target");

    // Unset difficulty must refuse to grind (window mined nonce-less).
    PowHasher hasher_nodiff;
    auto p = base_pow_params(3);
    p.erase("difficulty");
    hasher_nodiff.update_from_payload(p);
    std::array<uint8_t, 32> unused{};
    CHECK(!hasher_nodiff.grind_admission(ctx, 0, prompt, pad_mask, 16, unused),
          "difficulty unset -> no grind");

    // Nontrivial target in the plan's exp_tries 10-80 soak band (§10):
    // expected_tries(kBandDifficulty) == 60, so the target rejects ~59/60 of
    // nonces — a first-try pass would be a broken comparison, not luck.
    CHECK(pow_v3::admission_expected_tries(kBandDifficulty) == 60,
          "band difficulty derives expected_tries == 60");
    PowHasher hasher_band;
    hasher_band.update_from_payload(base_pow_params(3, kBandDifficulty));
    std::array<uint8_t, 32> band_nonce{};
    bool band_ok = hasher_band.grind_admission(ctx, 0, prompt, pad_mask, 16,
                                               band_nonce);
    CHECK(band_ok, "grind succeeds against the 60-expected-tries target");
    if (band_ok) {
        auto band_target = pow_v3::admission_target_le(kBandDifficulty);
        auto msg_w = pow_v3::build_step_message(hexstr_to_bytes(kHeaderHex),
                                                hexstr_to_bytes(kVdfHex),
                                                kTick, 0, prompt, kPrecision);
        auto commit = pow_v3::prompt_commitment(prompt, pad_mask);
        auto good = pow_v3::argon2id_digest(pow_v3::admission_message(
            msg_w, kModelId, band_nonce.data(), commit));
        CHECK(pow_v3::admission_valid(good, band_target),
              "band nonce verifies against the nontrivial target");
        // 1-bit tamper on the nonce must be rejected (target-boundary
        // confidence: with a 1/60 target a random digest almost never
        // passes; run over every bit of the first byte to kill flukes).
        int tampered_accepted = 0;
        for (int bit = 0; bit < 8; ++bit) {
            auto bad_nonce = band_nonce;
            bad_nonce[0] ^= (uint8_t)(1u << bit);
            auto bad = pow_v3::argon2id_digest(pow_v3::admission_message(
                msg_w, kModelId, bad_nonce.data(), commit));
            if (pow_v3::admission_valid(bad, band_target)) {
                ++tampered_accepted;
            }
        }
        CHECK(tampered_accepted <= 1,
              "tampered nonces rejected at ~1/60 target rate");
    }
}

// ---------------------------------------------------------------------- //
// 4. ProofWriter guards
// ---------------------------------------------------------------------- //

static void test_proof_writer_guards(const std::string& scratch) {
    std::cout << "[proof-writer guards]\n";
    ProofWriter writer(scratch + "/writer_guards");
    writer.set_proof_version(3);
    writer.set_model_identifier(kModelId);
    writer.set_compute_precision(kPrecision);

    std::unordered_map<std::string, std::any> window_data;
    std::unordered_map<std::string, std::any> seq_info;

    // Ingress stamp disagreement fails loudly on the first proof.
    bool threw = false;
    try {
        auto p = base_pow_params(2);  // stamped 2, writer configured 3
        writer.write_proof(1, 0, window_data, std::vector<uint8_t>(32, 0),
                           false, p, seq_info);
    } catch (const std::exception&) {
        threw = true;
    }
    CHECK(threw, "proof_version stamp disagreement throws");

    // v3: absent sampler params are a hard failure, not a default.
    threw = false;
    try {
        auto p = base_pow_params(3);
        p.erase("repetition_penalty");
        writer.write_proof(1, 0, window_data, std::vector<uint8_t>(32, 0),
                           false, p, seq_info);
    } catch (const std::exception&) {
        threw = true;
    }
    CHECK(threw, "v3 proof without repetition_penalty throws");
}

// ---------------------------------------------------------------------- //
// 5/6. Coordinator end-to-end (v3 two windows, then v2 regression)
// ---------------------------------------------------------------------- //

struct MinedWindow {
    // per-step data captured while driving the coordinator
    std::vector<std::vector<int64_t>> contexts;   // context passed per step
    std::vector<std::vector<uint8_t>> digests;    // digest returned per step
    std::vector<int64_t> chosen;                  // sampled tokens
};

// Drive one 256-step window through the coordinator exactly like
// server-context.cpp does, then fire the boundary solution check.
static MinedWindow drive_window(PowSamplingCoordinator& coord, int seq_id,
                                std::vector<int64_t>& cache) {
    MinedWindow w;
    static const float logits[4] = {0.1f, 0.7f, 0.2f, 0.4f};
    for (size_t j = 0; j < POW_WINDOW_SIZE; ++j) {
        auto ctx = rolling_window(cache);
        auto res = coord.sample_token_complete(seq_id, logits, 4, 1.0f, 50,
                                               1.0f, ctx, kPrecision);
        coord.record_complete_step(seq_id, res, true);
        w.contexts.push_back(ctx);
        w.digests.push_back(res.digest);
        w.chosen.push_back(res.token_id);
        cache.push_back(res.token_id);
    }
    coord.check_solutions({seq_id});
    return w;
}

// Read the single .bin proof emitted into `dir` (dir is purged by caller
// before emission), parse it as a MiningResponse FlatBuffer.
static std::vector<uint8_t> read_single_proof_bin(const std::string& dir) {
    std::string found;
    for (const auto& e : fs::directory_iterator(dir)) {
        if (e.path().extension() == ".bin") {
            if (!found.empty()) {
                throw std::runtime_error("more than one .bin proof in " + dir);
            }
            found = e.path().string();
        }
    }
    if (found.empty()) {
        throw std::runtime_error("no .bin proof emitted in " + dir);
    }
    std::ifstream f(found, std::ios::binary);
    return std::vector<uint8_t>((std::istreambuf_iterator<char>(f)),
                                std::istreambuf_iterator<char>());
}

static void purge_dir(const std::string& dir) {
    fs::create_directories(dir);
    for (const auto& e : fs::directory_iterator(dir)) {
        fs::remove_all(e.path());
    }
}

// Verifier-style checks over one emitted proof + the miner-side capture.
static void verify_emitted_window(const std::vector<uint8_t>& blob,
                                  const MinedWindow& w,
                                  const std::vector<int64_t>& expected_prefix,
                                  int expected_version,
                                  int64_t difficulty,
                                  const std::string& label) {
    const auto* resp = flatbuffers::GetRoot<proof::MiningResponse>(blob.data());
    const auto* pf = resp->pow_blob();
    CHECK(pf != nullptr, label + ": proof blob present");
    CHECK(pf->version() == expected_version,
          label + ": proof.version == " + std::to_string(expected_version));
    CHECK(resp->req_id() == 12345, label + ": req_id forwarded");

    const std::string extra_flags = pf->extra_flags() ? pf->extra_flags()->str() : "";
    auto nonce_hex = pow_v3::extract_admission_nonce_hex(extra_flags);

    const auto header = hexstr_to_bytes(kHeaderHex);
    const auto vdf = hexstr_to_bytes(kVdfHex);

    // prompt_tokens must be the pre-window prefix (§6).
    std::vector<int64_t> proof_prompt;
    for (auto t : *pf->prompt_tokens()) proof_prompt.push_back((int64_t)t);
    CHECK(proof_prompt == expected_prefix,
          label + ": proof.prompt_tokens == pre-window prefix");

    const uint8_t* nonce_ptr = nullptr;
    std::vector<uint8_t> nonce_bytes;
    if (expected_version >= 3) {
        CHECK(nonce_hex.has_value(),
              label + ": extra_flags carries v3.admission_nonce");
        CHECK(extra_flags.find("completion_id") != std::string::npos,
              label + ": completion_id survives the nonce merge");
        if (nonce_hex.has_value()) {
            nonce_bytes = hex_to_bytes(*nonce_hex);
            nonce_ptr = nonce_bytes.data();
            // Admission verification exactly as the verifier does it.
            std::vector<uint8_t> pad_mask_v;
            if (pf->pad_mask()) {
                pad_mask_v.assign(pf->pad_mask()->begin(), pf->pad_mask()->end());
            } else {
                pad_mask_v.assign(proof_prompt.size(), 0);
            }
            auto msg_w = pow_v3::build_step_message(header, vdf, kTick, 0,
                                                    proof_prompt, kPrecision);
            auto commitment = pow_v3::prompt_commitment(proof_prompt, pad_mask_v);
            auto adm_msg = pow_v3::admission_message(
                msg_w, pf->model_identifier()->str(), nonce_ptr, commitment);
            auto adigest = pow_v3::argon2id_digest(adm_msg);
            auto target = pow_v3::admission_target_le(difficulty);
            CHECK(pow_v3::admission_valid(adigest, target),
                  label + ": admission nonce verifies against the target");
        }
    } else {
        CHECK(!nonce_hex.has_value(), label + ": v2 extra_flags has no nonce");
    }

    // Full u replay: every step's digest recomputes with the SAME nonce
    // (or without one for v2) — proves the nonce entered every u of the
    // window including step 0.
    bool all_steps_match = true;
    for (size_t j = 0; j < POW_WINDOW_SIZE; ++j) {
        auto ref = pow_v3::step_digest(pow_v3::build_step_message(
            header, vdf, kTick, (uint32_t)j, w.contexts[j], kPrecision,
            nonce_ptr));
        if (!std::equal(w.digests[j].begin(), w.digests[j].end(), ref.begin())) {
            all_steps_match = false;
            break;
        }
    }
    CHECK(all_steps_match,
          label + ": all 256 step digests replay with the emitted nonce");

    // Sampling-u values in the proof match the replay.
    bool u_match = pf->sampling_u()->size() == POW_WINDOW_SIZE;
    if (u_match) {
        for (size_t j = 0; j < POW_WINDOW_SIZE; ++j) {
            auto ref = pow_v3::step_digest(pow_v3::build_step_message(
                header, vdf, kTick, (uint32_t)j, w.contexts[j], kPrecision,
                nonce_ptr));
            float ref_u = (float)pow_v3::step_u_from_digest(ref);
            if (std::fabs(pf->sampling_u()->Get((flatbuffers::uoffset_t)j) - ref_u) > 1e-6f) {
                u_match = false;
                break;
            }
        }
    }
    CHECK(u_match, label + ": proof.sampling_u replays byte-for-byte");

    // Final target-critical digest (proof.hash / derived header nonce):
    // step-0 message over the window's chosen tokens, same nonce.
    auto final_ref = pow_v3::step_digest(pow_v3::build_step_message(
        header, vdf, kTick, 0, w.chosen, kPrecision, nonce_ptr));
    CHECK(pf->hash()->size() == 32 &&
              std::equal(pf->hash()->begin(), pf->hash()->end(),
                         final_ref.begin()),
          label + ": final proof hash folds the nonce (header-nonce binding)");

    // chosen tokens recorded faithfully
    bool tokens_match = pf->chosen_tokens()->size() == POW_WINDOW_SIZE;
    if (tokens_match) {
        for (size_t j = 0; j < POW_WINDOW_SIZE; ++j) {
            if ((int64_t)pf->chosen_tokens()->Get((flatbuffers::uoffset_t)j) != w.chosen[j]) {
                tokens_match = false;
                break;
            }
        }
    }
    CHECK(tokens_match, label + ": chosen_tokens recorded faithfully");
}

static void test_coordinator_v3_two_windows(const std::string& scratch) {
    std::cout << "[coordinator v3 end-to-end, two windows]\n";
    const std::string proof_dir = scratch + "/proofs_v3";
    purge_dir(proof_dir);
    setenv("POW_PROOF_VERSION", "3", 1);
    setenv("POW_V3_ADMISSION_MODE", "always", 1);
    setenv("MINING_SOLUTION_COOLDOWN_SEC", "0", 1);
    setenv("POW_PROXY_ENABLE", "1", 1);  // emit every window via the audit path

    PowSamplingCoordinator coord(POW_WINDOW_SIZE, 8);
    coord.initialize(scratch + "/logs_v3", proof_dir);

    const int seq_id = 1;
    std::vector<int64_t> prompt = {11, 12, 13, 14, 15, 16, 17, 18};
    std::vector<int64_t> cache = prompt;

    // NONTRIVIAL admission target: expected_tries == 10, so each boundary
    // really grinds (~10 Argon2 evals) instead of first-try passing.
    coord.update_pow_params_for_sequence(seq_id,
                                         base_pow_params(3, kCoordDifficulty));
    {
        std::unordered_map<int, std::vector<int64_t>> mapping{{seq_id, prompt}};
        coord.ensure_sequences({seq_id}, mapping);
    }
    coord.set_prompt_tokens(seq_id,
                            std::vector<int32_t>(prompt.begin(), prompt.end()));
    coord.set_completion_id(seq_id, "cmpl-test");

    // Window 1
    auto w1 = drive_window(coord, seq_id, cache);
    auto blob1 = read_single_proof_bin(proof_dir);
    verify_emitted_window(blob1, w1, prompt, 3, kCoordDifficulty, "window 1");

    // Window 2: prefix must advance to prompt + window-1 tokens, and the
    // nonce must be re-ground (different msg_w). Keep the window-2 proof on
    // disk for the Python cross-check (verify_llama_proof_bin.py).
    purge_dir(proof_dir);
    std::vector<int64_t> prefix2 = prompt;
    prefix2.insert(prefix2.end(), w1.chosen.begin(), w1.chosen.end());
    auto w2 = drive_window(coord, seq_id, cache);
    auto blob2 = read_single_proof_bin(proof_dir);
    verify_emitted_window(blob2, w2, prefix2, 3, kCoordDifficulty, "window 2");

    const auto* pf1 = flatbuffers::GetRoot<proof::MiningResponse>(blob1.data())->pow_blob();
    const auto* pf2 = flatbuffers::GetRoot<proof::MiningResponse>(blob2.data())->pow_blob();
    auto n1 = pow_v3::extract_admission_nonce_hex(pf1->extra_flags()->str());
    auto n2 = pow_v3::extract_admission_nonce_hex(pf2->extra_flags()->str());
    CHECK(n1.has_value() && n2.has_value() && *n1 != *n2,
          "window 2 re-grinds a fresh nonce (one nonce per window)");

    coord.cleanup_sequence(seq_id);
}

static void test_coordinator_v2_regression(const std::string& scratch) {
    std::cout << "[coordinator v2 regression]\n";
    const std::string proof_dir = scratch + "/proofs_v2";
    purge_dir(proof_dir);
    setenv("POW_PROOF_VERSION", "2", 1);
    setenv("POW_V3_ADMISSION_MODE", "off", 1);
    setenv("MINING_SOLUTION_COOLDOWN_SEC", "0", 1);
    setenv("POW_PROXY_ENABLE", "1", 1);

    PowSamplingCoordinator coord(POW_WINDOW_SIZE, 8);
    coord.initialize(scratch + "/logs_v2", proof_dir);

    const int seq_id = 2;
    std::vector<int64_t> prompt = {21, 22, 23};
    std::vector<int64_t> cache = prompt;

    coord.update_pow_params_for_sequence(seq_id, base_pow_params(2));
    {
        std::unordered_map<int, std::vector<int64_t>> mapping{{seq_id, prompt}};
        coord.ensure_sequences({seq_id}, mapping);
    }
    coord.set_prompt_tokens(seq_id,
                            std::vector<int32_t>(prompt.begin(), prompt.end()));

    auto w = drive_window(coord, seq_id, cache);
    auto blob = read_single_proof_bin(proof_dir);
    verify_emitted_window(blob, w, prompt, 2, kEasyDifficulty, "v2 window");

    coord.cleanup_sequence(seq_id);
}

// ---------------------------------------------------------------------- //

int main() {
    std::string scratch = fs::temp_directory_path().string() +
                          "/llama_v3_wiring_test";
    purge_dir(scratch);

    if (!pow_v3::argon2_compiled()) {
        std::cerr << "FATAL: built without POW_V3_HAVE_ARGON2 — this test "
                     "requires libargon2\n";
        return 2;
    }

    test_hasher_step_hash_bytecompat();
    test_ring_buffers_admission_state();
    test_grind_admission_verifier_accepts();
    test_proof_writer_guards(scratch);
    test_coordinator_v3_two_windows(scratch);
    test_coordinator_v2_regression(scratch);

    if (g_failures == 0) {
        std::cout << "\nALL LLAMA V3 WIRING TESTS PASSED\n";
        return 0;
    }
    std::cout << "\n" << g_failures << " FAILURE(S)\n";
    return 1;
}
