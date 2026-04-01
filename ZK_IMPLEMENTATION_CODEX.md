# ZK / KYC Compliance Enforcement for TensorCash Assets

## Overview

TensorCash assets support an opt-in zero-knowledge (ZK) compliance layer that
lets an issuer gate transfers of an asset behind an on-chain, cryptographically
verified compliance proof — without changing the base transaction or script
machinery. A spender of a KYC-gated asset attaches a Groth16 proof attesting
that they belong to the issuer's compliance whitelist, and consensus verifies
that proof against an issuer-published verifying key and an on-chain compliance
root before the spend is accepted.

The feature is entirely opt-in. An asset signals KYC enforcement through its
IssuerReg `kyc_flags` field; assets that leave `kyc_flags` zero use the standard
asset encoding and incur zero overhead. KYC metadata, verifying keys, and proofs
all ride on the existing per-output TLV (`vExt`) channel — there is no separate
asset blockchain, mempool, or message type.

Design principles, as implemented:

- **TLV-based proof transport.** ZK proofs live in an output TLV
  (`ZK_PROOF_PAYLOAD`, type `0x22`), not on the witness stack. The witness of a
  KYC asset input carries only standard spend elements (signature + pubkey). Sighash
  enforcement on KYC inputs prevents output rebinding, and the BIP-141/341 witness
  commitment binds the proof-bearing outputs to the transaction.
- **On-chain verifying keys.** The full Groth16 verifying key (VK) is published
  on-chain, split across `ZK_PARAMS_CHUNK` TLVs (type `0x20`), reassembled at
  block-connect time, hash-checked against the issuer's commitment, and persisted
  in the asset-registry database alongside undo data for reorg safety.
- **On-chain compliance root.** Every KYC asset commits an issuer-controlled
  whitelist root in its IssuerReg. Consensus binds each proof to that root (or a
  recent historical root within a freshness window), so holders cannot prove
  membership against an arbitrary or stale whitelist.
- **Output-key binding (HDv1).** KYC proofs bind to the specific Taproot output
  key being spent, preventing a valid proof from being lifted onto a different
  address.

All consensus validation runs inside the existing asset hooks in
`services/core-node/bcore/src/consensus/tx_verify.cpp` (spend time) and
`services/core-node/bcore/src/validation.cpp` (block connect/disconnect). The
Groth16 verifier lives in
`services/core-node/bcore/src/crypto/groth16.cpp` and uses `blst`
(BLS12-381). Source paths below are repo-relative to the bcore consensus tree.

## Data Formats

### IssuerReg (asset registration)

Every asset is described by an `IssuerReg` TLV (type `0x10`). The registration
record is a single fixed layout that always carries a ZK section, an ICU
(issuance-control unit) section, and a compliance-root field — whether or not the
asset enables KYC. Parsing is implemented in `ParseIssuerRegV1`
(`src/assets/asset_parser_v1.cpp:42`).

Wire layout (offsets in bytes):

```
asset_id              [32]
policy_bits           [4]
allowed_spk_families  [2]
format_version        [1]    // 1 = v1, 2 = v2
ticker_len            [1]    // 0..23
ticker                [ticker_len]   // bare root or one-hop ROOT.SUFFIX
decimals              [1]    // 0..18, or 0xFF = unset
unlock_fees_sats      [8]

--- ZK section (76 bytes, always present) ---
kyc_flags             [4]    // non-zero => KYC enforced for this asset
zk_vk_commitment      [32]   // hash of the assembled verifying key
max_root_age          [4]    // compliance-root freshness window, in blocks
tfr_flags             [4]    // TFR_ANCHOR_REQUIRED lives here
compliance_root_commit[32]   // active whitelist commitment (root||height)

--- ICU section (129 bytes, always present) ---
icu_flags, issuance_cap_units, icu_ctxt_commit, icu_plain_commit,
kdf_salt, icu_version, icu_visibility, core_policy_commit,
policy_epoch, policy_quorum_bps

--- v2 trailing field (32 bytes, format_version == 2 only) ---
compliance_delegate_asset_id [32]   // delegated/reusable KYC pointer
```

Total length is **254–277 bytes** (the spread is the optional ticker, up to a
23-byte `ROOT.SUFFIX` child ticker). A **v2** record appends a non-null
`compliance_delegate_asset_id`, raising the bounds by 32 bytes; a v2 record whose
delegate equals its own `asset_id` is the explicit "clear delegation" sentinel.
Any other length is rejected by the parser.

`compliance_root_commit` is packed as `merkle_root[0..27] || capture_height_be[28..31]`:
the first 28 bytes are the whitelist Merkle root, the last 4 are the big-endian
block height at which the whitelist was captured. Binding the capture height into
the commitment prevents a holder from claiming compliance as of an arbitrary
height.

**KYC is signalled by `kyc_flags != 0`, not by the `KYC_REQUIRED` policy bit.**
The `KYC_REQUIRED` (`0x0010`) and `TFR_ANCHOR_REQUIRED` (`0x0020`) bits are part
of the immutable policy mask, but it is the non-zero `kyc_flags` field that turns
on spend-time ZK enforcement (`src/assets/asset.h:252`).

### ZK_PARAMS_CHUNK (type 0x20) — verifying-key distribution

A KYC asset's verifying key is published on-chain, split into chunks carried in
`ZK_PARAMS_CHUNK` TLVs. The structure (`src/assets/asset.h:128`):

```cpp
struct ZkParamsChunk {
    uint256 asset_id;            // guard against cross-asset mix-ups
    uint256 vk_hash;             // must match the IssuerReg commitment
    uint16_t chunk_index;        // 0-based
    uint16_t chunk_count;        // total expected chunks
    std::vector<unsigned char> data;  // <= 512 bytes
};
```

Chunk-shape limits (`src/assets/asset.h:179`, `ValidateChunkParams`
`src/assets/asset.h:302`):

- `chunk_count` in `1..MAX_ZK_CHUNKS` (`MAX_ZK_CHUNKS = 8`),
- `chunk_index < chunk_count`,
- each chunk's `data` is at most `MAX_ZK_CHUNK_SIZE = 512` bytes, so the assembled
  VK is at most `MAX_VK_PAYLOAD_SIZE = 4 KiB`.

A malformed chunk is rejected at output-validation time with `zkchunk-invalid`
(`tx_verify.cpp:410`).

### TFR_ANCHOR (type 0x21) — transfer-reporting anchor

For jurisdictions that require a transfer-reporting breadcrumb, an asset can set
`TFR_ANCHOR_REQUIRED` in `tfr_flags`. When set, transfers must carry one
`TFR_ANCHOR` TLV per AssetTag output for the asset. The structure
(`src/assets/asset.h:136`):

```cpp
struct TfrAnchor {
    uint256 asset_id;            // guard against misbinding
    uint256 tfr_commit;          // hash of the off-chain reporting packet
    uint32_t keyset_id;          // hint for off-chain decrypt infrastructure
    std::vector<unsigned char> locator;  // <= 128 bytes (MAX_TFR_LOCATOR_SIZE)
};
```

The chain only commits to the existence of an anchor; it never fetches or
interprets the off-chain packet. Consensus enforces a **per-output** binding
model: when `TFR_ANCHOR_REQUIRED` is set, the number of `TFR_ANCHOR` TLVs for an
asset must equal the number of AssetTag outputs for that asset. The anchor's
`tfr_commit` is then bound into the proof's public inputs (see below).

### ZK_PROOF_PAYLOAD (type 0x22) — proof transport

A spend of a KYC asset carries exactly one `ZK_PROOF_PAYLOAD` TLV per asset, in a
transaction output. The structure (`src/assets/asset.h:166`):

```cpp
struct ZkProofPayload {
    uint256 asset_id;                          // must match the AssetTag
    std::vector<unsigned char> proof;          // Groth16 proof, 192 bytes
    std::vector<unsigned char> public_inputs;  // N x 32 bytes (see layouts)
};
```

Size constants (`src/assets/asset.h:187`):

- `GROTH16_PROOF_SIZE = 192` bytes — BLS12-381 compressed `A || B || C`
  (48-byte A + 96-byte B + 48-byte C).
- `GROTH16_FR_SIZE = 32` bytes — one field element.
- Public inputs must be a multiple of 32 bytes, at least
  `GROTH16_MIN_PUBLIC_INPUTS = 4` elements (128 bytes), and at most
  `GROTH16_MAX_PUBLIC_INPUTS_SIZE = 256` bytes (8 elements).

#### Public-input layouts

Two public-input layouts exist (`src/crypto/groth16.cpp:14`,
`src/assets/asset.h:152`). The circuit family is detected from the VK's
`gamma_abc` count, which equals the public-input count.

**Legacy (4 inputs, 128 bytes):**

```
[0] chain/domain separator   — prevents cross-chain replay
[1] asset_id (big-endian)    — binds the proof to this asset
[2] compliance_root || height — upper 28 bytes root, lower 4 bytes height (BE)
[3] tfr_commit               — transfer-reporting anchor, or zero
```

**HDv1 (6 inputs, 192 bytes):**

```
[0] chain/domain separator
[1] asset_id (big-endian)
[2] compliance_root          — pure 32-byte MiMC root (freshness checked on-chain)
[3] tfr_commit
[4] output_key_high          — upper 128 bits of the child x-only key, left-padded
[5] output_key_low           — lower 128 bits of the child x-only key, left-padded
```

KYC asset spends are required to use the **HDv1 (>= 6-input) interface**: a
4-input proof has no `[4]`/`[5]` and cannot be bound to the output key, so it is
rejected with `kyc-proof-not-hdv1` (`tx_verify.cpp:915`). The legacy 4-input
layout remains a recognized circuit family at the verifier level, but consensus
will not accept it for a KYC asset spend.

### Registry storage

Each asset's registry entry (`AssetRegistryEntry`) carries the persisted KYC
metadata: `has_kyc`, `zk_vk_commitment`, `max_root_age`, `tfr_flags`, the active
`compliance_root_commit`, a bounded history of prior compliance roots
(`compliance_root_history`, a FIFO ring buffer), a lockstep per-root VK history
(`compliance_root_history_vk`, for rolling circuit migration), and the
`compliance_delegate_asset_id` for delegated KYC.

The assembled verifying-key payload is stored separately, keyed by its
`vk_commitment`. `ConnectBlock` stages the assembled VK into the view
(`view.StageZkVerifyingKey`, `validation.cpp:5375`); spend validation reads it
back via `view.ReadZkVerifyingKey` (`tx_verify.cpp:1015`). Both the registry
update and the VK insert/erase are recorded in block undo data, so a reorg rolls
both back together.

## Consensus Validation

### Policy bits

The immutable policy mask includes `MINT_ALLOWED (0x0001)`,
`BURN_ALLOWED (0x0002)`, `BURN_REQUIRE_ICU (0x0004)`,
`BURN_JOINT_REQUIRED (0x0008)`, `KYC_REQUIRED (0x0010)` and
`TFR_ANCHOR_REQUIRED (0x0020)` (`src/assets/asset.h:264`). An IssuerReg
presenting policy bits outside the recognized layout is rejected.

KYC assets are restricted to output-binding script families. The default allowed
mask (`SPK_DEFAULT_ALLOWED`) is P2WPKH | P2WSH | P2TR; KYC and PQ (witness-v2,
ML-DSA) families are mutually exclusive — a registration that mixes
`kyc_flags != 0` with `SPK_P2TR_V2` is rejected (`KycPqFamilyConflict`,
`src/assets/asset.h:258`), because KYC holders are v1-Taproot-only under the HDv1
output-key binding.

### Block connect / disconnect (`src/validation.cpp`)

While connecting a block:

1. Collect all `ZK_PARAMS_CHUNK` payloads, keyed by `(asset_id, vk_hash)`
   (`validation.cpp:4669`).
2. When an IssuerReg with `kyc_flags != 0` is processed, require the full chunk
   set for its `zk_vk_commitment` to be present in the same block. A missing or
   incomplete set is rejected with `zkchunk-missing`
   (`validation.cpp:5324`/`5330`/`5334`).
3. Reassemble the VK by concatenating chunks in index order and hashing the
   concatenation; the digest must equal `zk_vk_commitment`, else
   `zkchunk-badhash` (`validation.cpp:5350`).
4. Enforce the per-block install cap (`MAX_ZK_PROOFS_PER_BLOCK = 400`); exceeding
   it rejects the block with `zkchunk-block-cap` (`validation.cpp:5357`).
5. Stage the assembled VK, set `has_kyc = true` and the ZK/TFR metadata on the
   registry entry, and apply the compliance-root update (push the prior root onto
   the history ring buffer, record the new active root and its activation height).
6. On disconnect, undo data restores the prior VK entry and the full registry
   state (including the ring buffer) via `view.StageZkVerifyingKey` /
   `view.StageEraseZkVerifyingKey` (`validation.cpp:3554`).

### Spend-time validation (`CheckTxInputs`, `src/consensus/tx_verify.cpp`)

Output TLVs are first type-checked: only recognized TLV types are permitted on a
`vExt`, otherwise the transaction is rejected with `outext`
(`tx_verify.cpp:354`). During this pass, `ZK_PROOF_PAYLOAD` TLVs are collected
and shape-checked — proof must be exactly 192 bytes (`zk-proof-size`,
`tx_verify.cpp:427`) and public inputs must satisfy the size/multiple-of-32 rules
(`zk-public-inputs-size`, `tx_verify.cpp:435`).

The asset Δ-accounting loop loads each spent asset's policy. For an asset with
`has_kyc`, the **effective** policy is resolved (`ResolveEffectiveKycPolicy`,
`tx_verify.cpp:528`): for a delegating (v2) asset the VK / root / history are
sourced from the delegate while the asset's own id and TFR flags are kept. The
remaining KYC checks then run per asset:

1. **VK commitment present.** A KYC asset with a null `vk_commitment` is rejected
   with `zkchunk-missing` (`tx_verify.cpp:645`).
2. **TFR anchor count.** If `TFR_ANCHOR_REQUIRED` is set, the number of
   `TFR_ANCHOR` TLVs must be non-zero (`tfr-anchor-missing`,
   `tx_verify.cpp:663`) and equal to the AssetTag output count for the asset
   (`tfr-anchor-count-mismatch`, `tx_verify.cpp:666`).
3. **ICU sighash enforcement.** Every input that spends an ICU must sign with an
   output-binding sighash (SIGHASH_ALL, or Taproot SIGHASH_DEFAULT). A missing
   signature yields `icu-missing-signature`; a weak sighash
   (ANYONECANPAY/SINGLE/NONE) yields `icu-invalid-sighash`
   (`tx_verify.cpp:325`/`328`). This prevents grafting inputs/outputs onto a
   mint/burn after the issuer signs.
4. **KYC-input sighash enforcement.** Every KYC asset input must likewise use an
   output-binding sighash, else `zk-invalid-sighash` (`tx_verify.cpp:742`). The
   single exception is a genuine rotation ballot input — identified structurally
   (rotation tx shape, `vin > 1`, `vout[0]` is the asset's IssuerReg, and the
   input is a validated self-bounce committing to its own proposal hash) — which
   is permitted a ballot sighash (strict, or SIGHASH_SINGLE|ANYONECANPAY that
   commits the input to its own output). The waiver is per-input, so a rotation
   of one asset cannot smuggle a weak-sighash spend of another.
5. **Segwit family + witness layout.** Each KYC asset input must spend a witness
   script type (`kyc-spend-nonsegwit`, `tx_verify.cpp:763`) and present a valid
   witness layout (signature present; `zk-witness-empty`, `tx_verify.cpp:772`).
   No ZK data lives in the witness — proofs are in output TLVs.
6. **Proof presence.** Exactly one `ZK_PROOF_PAYLOAD` per asset is required:
   missing yields `zk-proof-missing` (`tx_verify.cpp:797`), more than one yields
   `zk-proof-duplicate` (`tx_verify.cpp:801`).

The Groth16 verification pass then runs per KYC asset
(`tx_verify.cpp:834` onward):

7. **Asset-id binding.** The proof payload's `asset_id` must match the asset
   (`zk-proof-asset-mismatch`, `tx_verify.cpp:865`).
8. **HDv1 interface.** Public inputs must be at least 6 field elements; fewer is
   rejected with `kyc-proof-not-hdv1` (`tx_verify.cpp:915`).
9. **Chain separator.** Consensus computes the chain/domain separator from the
   network params (`ComputeChainSeparatorBytes`) and byte-compares it to
   `public_inputs[0]`; mismatch is `zk-chain-mismatch` (`tx_verify.cpp:931`).
10. **Asset field.** `public_inputs[1]` must equal the big-endian asset id
    (`zk-asset-mismatch`, `tx_verify.cpp:938`).
11. **Compliance root.** `public_inputs[2]` must match either the active
    `compliance_root_commit` or a historical root in the ring buffer whose age is
    within `max_root_age` blocks. The issuer must have committed a root
    (`zk-root-not-set`, `tx_verify.cpp:952`); no valid match yields
    `zk-root-mismatch` (`tx_verify.cpp:1009`). A historical-root match selects
    that root's own VK from `compliance_root_history_vk` (rolling circuit
    migration); the active-root case uses the active VK.
12. **VK fetch.** The chosen VK is read from the registry store
    (`view.ReadZkVerifyingKey`); a missing key yields `zk-vk-missing`
    (`tx_verify.cpp:1017`).
13. **TFR anchor binding.** `public_inputs[3]` must equal the big-endian
    `tfr_commit` of the asset's first anchor, or zero when none is present
    (`zk-anchor-mismatch`, `tx_verify.cpp:1031`).
14. **Output-key binding.** For every input of the asset, the prevout must be a
    Taproot-v1 output (`kyc-proof-output-not-taproot`, `tx_verify.cpp:1065`), and
    the 32-byte x-only key, split into high/low 128-bit halves left-padded to two
    field elements, must equal `public_inputs[4]` and `public_inputs[5]`
    (`kyc-proof-output-mismatch`, `tx_verify.cpp:1082`). Multi-input same-asset
    spends must therefore share one Taproot key.
15. **Pairing check.** `groth16::VerifyGroth16WithPolicy(proof, public_inputs,
    vk, ctx)` (`src/crypto/groth16.cpp`) runs the BLS12-381 pairing check plus
    policy-layer freshness/anchor checks. Outcomes map to consensus errors:
    `InvalidProofFormat`/`PairingFailed` → `zk-proof-bad`
    (`tx_verify.cpp:1112`/`1130`), `InvalidVerifyingKey` → `zk-vk-invalid`,
    `InvalidPublicInputs` → `zk-public-inputs`, `RootTooOld` → `zk-epoch-stale`
    (`tx_verify.cpp:1121`), `AnchorMismatch` → `tfr-anchor-mismatch`,
    `OutputKeyMismatch` → `kyc-proof-output-mismatch`.

Finally the per-transaction proof count is bounded by
`MAX_ZK_PROOFS_PER_TX = 2`; exceeding it yields `zk-proof-cap`
(`tx_verify.cpp:1135`).

### Trust model note on output binding

Consensus byte-compares `public_inputs[4]`/`[5]` to the prevout x-only key, which
guarantees the *values* in the proof equal the key being spent. It does not by
itself prove the circuit *constrains* those inputs to the enrolled-key
derivation; that property is trusted from the issuer's registered VK. The scheme
is sound under the threat model "issuer honest (already trusted for the
compliance root), spender adversarial" — a spender cannot substitute the VK, and
without the `[4]`/`[5]` binding a stolen proof is a bearer token. An operator who
also wants to guard against an issuer registering a non-binding VK would need a
canonical-VK allowlist or circuit-family attestation, which is a deployment-time
trust choice.

## DoS Mitigations

- Per-transaction ZK verification cap: `MAX_ZK_PROOFS_PER_TX = 2`
  (`zk-proof-cap`).
- Per-block VK install / verification cap: `MAX_ZK_PROOFS_PER_BLOCK = 400`
  (`zkchunk-block-cap`).
- Chunked VK payloads are bounded to 4 KiB (8 × 512 B); TFR locators to 128 B;
  public inputs to 256 B. Proof bytes are fixed at 192.
- Witness sizes already enter fee/weight accounting; no special fee logic beyond
  these caps is required.

## Proof Lifecycle and Revocation

A Groth16 proof attests that a holder is on the issuer's whitelist as of a
specific compliance root. The same proof may be reused for the same asset and the
same output key while that root remains the active root, or a historical root
within `max_root_age` blocks of its activation.

Revocation works by rotation: the issuer publishes a new `compliance_root_commit`
(a new IssuerReg with the rotated root). The prior root moves into the ring
buffer and remains valid only until it ages out of the `max_root_age` window,
giving compliant holders a grace period to regenerate proofs against the new
root. Assets that need tighter revocation latency configure a smaller
`max_root_age`.

## Compliance-Root Commitment

Binding every KYC spend to an issuer-committed whitelist root is what stops a
holder from generating a proof against an arbitrary or outdated whitelist.
Without it, the chain could verify that a proof is internally valid but not which
whitelist it was made against.

### Data model

The active commitment is stored in `AssetRegistryEntry.compliance_root_commit`
(packed `root[0..27] || height_be[28..31]`). Prior commitments are kept in a FIFO
ring buffer (`compliance_root_history`), each entry recording the packed root, its
activation height, and the txid that committed it. The buffer is bounded, so deep
reorgs roll back multiple rotations deterministically from undo data.

### Validation

On connect, the new root is written to the active slot and the prior root is
pushed onto the ring buffer (`ApplyComplianceRootUpdate`, `validation.cpp:5385`).
At spend time, `public_inputs[2]` is matched against the active root, then against
the ring buffer with the `max_root_age` freshness check (see step 11 above), and
a historical match additionally selects that root's archived VK so a proof made
under an older circuit still verifies during the migration window.

### Security guarantees

- **Deterministic validation.** Consensus derives the expected root entirely from
  on-chain state — no issuer RPC dependency at validation time.
- **Reorg safety.** The full ring buffer is captured in undo data, so historical
  spends remain valid after reorganizations.
- **Bounded revocation latency.** A rotation invalidates old proofs only after
  `max_root_age` blocks.
- **Cross-asset isolation.** `public_inputs[1]` (asset id) prevents reuse of a
  proof across assets.
- **Height-tamper resistance.** The capture height packed into the commitment
  prevents claiming compliance at an arbitrary height.

### Delegated / reusable KYC

A v2 IssuerReg can point at another asset via `compliance_delegate_asset_id`. The
delegating asset (B) then follows the source asset (A)'s VK, compliance root, and
root history, while keeping its own asset id and TFR flags. The delegate pointer
is validated both at registration and at spend time (the source can mutate after
B installs the pointer). For delegated assets a staleness heartbeat applies: if
the source's active root is older than the effective window, the follower is
frozen with `kyc-delegate-source-stale` (`tx_verify.cpp:970`) until the source
rotates.

## RPC Surface

KYC / compliance-root management is exposed through wallet RPCs:

- `generatecomplianceroot` (`src/wallet/rpc/compliance_root.cpp`) — build a
  whitelist Merkle root commitment.
- `getassetcomplianceroot <asset_id_or_ticker>`
  (`src/wallet/rpc/assets.cpp`) — read the active commitment, its embedded capture
  height, the freshness window, and the associated VK commitment.
- `listassetcomplianceroots <asset_id_or_ticker> [count]`
  (`src/wallet/rpc/assets.cpp`) — list historical commitments from the ring buffer
  (root, height, activation height, committing txid).
- `updatecomplianceroot <asset_id_or_ticker> <root_commit_hex> [options]`
  (`src/wallet/rpc/assets.cpp`) — rotate the active compliance root by building an
  IssuerReg rotation transaction that preserves the asset's other registry fields
  (VK, `max_root_age`, `tfr_flags`, ICU metadata) and signs with the ICU authority.
  The `options` object carries `max_root_age`, `broadcast`, `delegate_asset`, and
  `clear_delegation`.

`generatehdwitnessdata` (`src/wallet/rpc/hd_witness.cpp:309`) produces the HDv1
witness data — the proof public inputs bound to the spending output key — used to
populate a `ZK_PROOF_PAYLOAD`.

## Consensus Error Codes

The validation paths above use these reject codes (asserted by the test suites):

`outext`, `zkchunk-invalid`, `zkchunk-missing`, `zkchunk-badhash`,
`zkchunk-block-cap`, `tfr-anchor-size`, `tfr-anchor-missing`,
`tfr-anchor-count-mismatch`, `tfr-anchor-mismatch`, `icu-missing-signature`,
`icu-invalid-sighash`, `zk-invalid-sighash`, `kyc-spend-nonsegwit`,
`zk-witness-empty`, `zk-proof-size`, `zk-public-inputs-size`,
`zk-public-inputs-short`, `zk-proof-missing`, `zk-proof-duplicate`,
`zk-proof-asset-mismatch`, `kyc-proof-not-hdv1`, `zk-chain-mismatch`,
`zk-asset-mismatch`, `zk-root-not-set`, `zk-root-mismatch`, `zk-vk-missing`,
`zk-vk-invalid`, `zk-anchor-mismatch`, `kyc-proof-output-not-taproot`,
`kyc-proof-output-mismatch`, `zk-epoch-stale`, `zk-proof-bad`,
`kyc-delegate-source-stale`, `zk-proof-cap`.

## Security Summary

- **No output rebinding.** Output-binding sighashes on KYC and ICU inputs, plus
  the BIP-141/341 witness commitment, prevent a third party from re-pointing a
  proof-bearing output after signing.
- **No cross-asset reuse.** Asset-id binding in both the proof payload and
  `public_inputs[1]`.
- **No cross-chain replay.** Chain/domain separator in `public_inputs[0]`.
- **No address transfer.** HDv1 output-key binding ties the proof to the Taproot
  output being spent.
- **No arbitrary whitelist.** On-chain compliance-root commitment with a bounded
  freshness window.
- **Bounded cost.** Per-tx and per-block verification caps, fixed proof size, and
  bounded VK/anchor/public-input sizes.

KYC/ZK enforcement is an optional layer over the base asset system. Consensus
accepts non-KYC assets unchanged, while issuers that set `kyc_flags` get
on-chain-verified compliance gating backed by Groth16 proofs.
