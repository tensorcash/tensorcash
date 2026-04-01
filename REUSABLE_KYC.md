# Reusable / Delegated KYC for TensorCash Assets

A KYC asset normally carries its **own** compliance Merkle root, and its issuer
must rotate the asset's ICU every time the whitelist changes. An issuer running
assets A, B and C maintains three independent whitelists and pays three
rotations per cohort change.

**Delegated KYC** lets an asset *follow another asset's compliance whitelist*
instead of maintaining its own. A delegating asset **B** keeps its own identity
(`asset_id`, `tfr_flags`) but resolves its **identity proof material** — verifying
key, compliance root, and root history — from a **source asset A**. A's rotations
propagate to every follower with zero follower-side action. A consensus-blessed
**canonical VK allowlist** constrains which verifying keys may be delegated to.

This document describes the consensus data model, resolution rules, guardrails,
reject codes and RPC surface of delegated KYC, together with the underlying
self-maintained (non-delegating) KYC mechanism it builds on.

The companion document [`KYC_v2.md`](KYC_v2.md) describes the HDv1 circuit and the
self-maintained whitelist model in detail.

---

## 1. Base architecture

### 1.1 On-chain data model

Per-asset consensus state lives in `AssetRegistryEntry`
(`services/core-node/bcore/src/assets/registry.h`), keyed by `asset_id` in
LevelDB (prefix `'R'`). KYC-relevant fields:

```
bool      has_kyc;
uint256   zk_vk_commitment;          // hash of the Groth16 verifying key
uint32_t  max_root_age;              // freshness window for HISTORICAL roots, in blocks
uint32_t  tfr_flags;                 // transfer-reporting policy (asset-level)
uint256   compliance_root_commit;    // active whitelist Merkle root (opaque 32 bytes)
std::deque<ComplianceRootHistory> compliance_root_history;        // ring buffer, MAX_ROOT_HISTORY = 32
std::deque<uint256>               compliance_root_history_vk;     // per-historical-root VK (lockstep)
int32_t   active_root_activation_height; // block height the active root became active
uint256   compliance_delegate_asset_id;  // null = self (default); non-null = follow that asset
```

`ComplianceRootHistory` (`registry.h`) stores `{root_commit, activation_height,
txid}`. The parallel `compliance_root_history_vk` deque stores, for each
historical root, the verifying key that root was committed under (§4).

The active root is committed via the `IssuerReg` TLV (`ISSUER_REG = 0x10`) inside
an ICU-spending transaction (`assets/asset_parser_v1.cpp`, `assets/asset.h`).
Authority to change it is whoever can spend the asset's current ICU UTXO
(`AssetRegistryEntry.icu_outpoint`). Set/rotate logic lives in `validation.cpp`
via `assets::ApplyComplianceRootUpdate` (`assets/kyc_delegation.cpp`). RPCs:
`updatecomplianceroot`, `getassetcomplianceroot`, `listassetcomplianceroots`,
`generatecomplianceroot` (`wallet/rpc/compliance_root.cpp`).

### 1.2 The circuit (HDv1)

`shared-utils/kyc-prover/internal/circuit/circuit_hd_v1.go`. Six public inputs:

| Index | Name | In-circuit binding |
|------:|------|--------------------|
| 0 | `ChainSeparator` | `AssertIsDifferent(.,0)` only |
| 1 | `AssetID` | `AssertIsDifferent(.,0)` only — otherwise **unconstrained** |
| 2 | `ComplianceRoot` | bound: `MiMC(P.x,P.y)` → Merkle path → root |
| 3 | `TfrAnchor` | **unconstrained in-circuit** |
| 4 | `OutputKeyHigh` | bound to child x-only key (upper 128 bits) |
| 5 | `OutputKeyLow` | bound to child x-only key (lower 128 bits) |

Private witness: master pubkey `P=(x,y)`, HD derivation params, child pubkey `Q`,
and the Merkle path (`MerkleSiblings [8]`, `circuit_hd_v1.go`). The circuit proves
`Q = P + H(commitment‖path)·G` (HD derivation) and that `leaf = MiMC(P.x,P.y)` is
in the tree. **No master private key in the witness** — key control comes from the
Taproot spend signature, so server-side / remote proving is non-custodial.

Two properties the delegation design rests on:

- **`AssetID` (input 1) and `TfrAnchor` (input 3) are free public inputs.** The
  circuit does not tie them to membership. A holder enrolled in asset A's tree can
  produce a valid proof naming *any* `asset_id` and *any* anchor; the values are
  pinned only by **consensus**, not by the proof. This is why delegation is a pure
  consensus-resolution change with no circuit change.
- **The membership root and the asset identity are independent.** Swapping which
  root a spend is checked against does not touch the asset-id binding.

The proof and public inputs are carried by the `ZK_PROOF_PAYLOAD` TLV (`0x22`),
one per asset spend, matched by `asset_id`. The HDv1 layout is six 32-byte
big-endian field elements (192 bytes); the legacy layout is four (128 bytes).

### 1.3 Enforcement path

KYC is enforced in `Consensus::CheckTxInputs`
(`services/core-node/bcore/src/consensus/tx_verify.cpp`):

1. **Effective-policy resolution.** For each spending KYC asset, the registry
   entry is resolved into a `KycPolicySnapshot` via
   `assets::ResolveEffectiveKycPolicy` (§2.2). For a self-maintained asset this is
   simply the asset's own fields; for a delegating asset it follows the source.
2. **Asset binding:** `public_inputs[1] == spending asset id`, else
   `zk-asset-mismatch`.
3. **Root requirement:** `zk-root-not-set` if the effective root is null.
4. **Heartbeat (delegated assets only, §2.4):** if the effective active root is
   older than the effective window, `kyc-delegate-source-stale`.
5. **Root binding:**
   - **Active root:** `public_inputs[2] == compliance_root_commit` — no age check.
   - Else **historical scan:** match any ring-buffer entry whose
     `age = nSpendHeight - activation_height` is in `[0, max_root_age]`. A
     historical match selects that root's own VK from `compliance_root_history_vk`
     (§4).
   - else `zk-root-mismatch`.
6. **VK fetch:** `inputs.ReadZkVerifyingKey(verify_vk, …)` is performed **after**
   the root match (§4), so a historical-root spend verifies under that root's VK;
   `zk-vk-missing` if absent.
7. **TFR anchor:** `public_inputs[3] == tfr_anchors[aid].tfr_commit` (asset-keyed),
   else `zk-anchor-mismatch`.
8. **Output-key binding:** every input's Taproot x-only key must match
   `public_inputs[4:5]`, else `kyc-proof-output-mismatch`. This makes a proof
   single-use per output and prevents proof theft.
9. **Groth16 verify** under the chosen VK (`crypto/groth16.cpp`,
   `groth16::VerifyGroth16WithPolicy`).

### 1.4 What is immutable vs. mutable

Frozen after first issuance (`issued_total > 0`) via
`core_policy_commit = SHA256("ASSET/V2_CORE", allowed_spk_families ‖
policy_bits&IMMUTABLE_MASK ‖ kyc_flags ‖ tfr_flags)` (`assets/asset.cpp`,
enforced in `validation.cpp` → `policy-core-changed`):

- **Immutable:** whether KYC is required (`kyc_flags`), allowed script families,
  `tfr_flags`, immutable policy bits.
- **Mutable via ICU rotation:** `compliance_root_commit` (+ history),
  `zk_vk_commitment` (the circuit/VK itself), `max_root_age`, ICU text/visibility,
  governance params (quorum-gated), and the delegation pointer (§2).

"Once a KYC asset, always a KYC asset" — but the verifier, the whitelist and the
delegation pointer are all rotatable.

### 1.5 Tree size

The HDv1 circuit is depth 8 → **256 leaves**: `MerkleSiblings [8]`
(`circuit_hd_v1.go`) and the tree builder `const size_t TREE_SIZE = 256`
(`wallet/rpc/compliance_root.cpp`). A single committed root commits to ≤256
*master pubkeys* (each derives unlimited child addresses). Depth is a compile-time
property of the circuit and its trusted setup. The canonical VK allowlist (§3) is
a **metadata map** keyed by VK hash, carrying each circuit's depth, public-input
count and shape flags, so consensus can describe and bless additional circuit
shapes without re-deriving them.

### 1.6 Shared roots without delegation

Because `asset_id` is free in-circuit, a holder in A's tree can already produce a
proof for asset B. The only thing stopping a B spend is that consensus checks
`public_inputs[2]` against **B's** committed root. So if B commits the **same 32
bytes** as A's root, A's whitelisted members can spend B with no protocol
extension at all. Delegation replaces that manual copy with an **auto-follow
pointer**: B commits a *reference* to A instead of a *copy* of A's bytes, so A's
later rotations propagate to B automatically.

---

## 2. Follow-A delegation

### 2.1 The pointer

`IssuerReg` and `AssetRegistryEntry` each carry a delegation pointer:

```
uint256 compliance_delegate_asset_id;   // null = self (default); non-null = follow that asset
```

- The pointer is changed only by an **`IssuerReg` format v2** (`ISSUER_REG_FORMAT_V2
  = 2`, `assets/asset.h`). The v2 wire layout is the v1 layout plus a trailing
  32-byte `compliance_delegate_asset_id` (`assets/asset_parser_v1.cpp`).
- Delegation is **height-gated** by `consensus.AssetsDelegationHeight`. Before
  activation, any `IssuerReg` carrying a non-null delegate field is rejected with
  `kyc-delegate-inactive` (`validation.cpp`) — old nodes must not silently ignore
  it.
- `AssetRegistryEntry` carries `compliance_delegate_asset_id`,
  `active_root_activation_height` and `compliance_root_history_vk` at the end of
  its `SERIALIZE_METHODS` (`registry.h`). The try/catch-EOF deserialization makes
  this DB-forward-compatible — legacy rows read the new fields as null/zero/empty.

### 2.2 Effective policy resolution (field-wise — not a snapshot swap)

For a spend of delegating asset **B** with `compliance_delegate_asset_id = A`,
`assets::ResolveEffectiveKycPolicy` (`assets/kyc_delegation.cpp`) resolves the
policy **field by field**:

```
expected_asset_id  = B.asset_id              // public_inputs[1] still pinned to B
tfr_flags          = B.tfr_flags             // frozen ASSET semantics stay with B (anchor keyed by aid=B)
vk_commitment      = A.zk_vk_commitment      // identity proof material from A
compliance_root    = A.compliance_root_commit
root_history       = A.compliance_root_history (+ A.compliance_root_history_vk)
max_root_age       = EffectiveMaxRootAge(A.max_root_age, B.max_root_age)
active_root_height = A.active_root_activation_height
```

The current `KycPolicySnapshot` conflates **identity material** (→ A) with
**asset obligations** (→ B). The resolver never replaces B's snapshot wholesale
with A's: `expected_asset_id` and `tfr_flags` are set from B on **every** path,
including failure paths. This is consistent because `public_inputs[1]` and `[3]`
are free in-circuit (§1.2): a holder enrolled in A's tree proves under A's circuit
while consensus pins `asset_id = B` and `anchor = B.tfr_commit`.

`EffectiveMaxRootAge(source, follower)` (`kyc_delegation.cpp`) treats `0` as "no
bound from that side": it returns the min of the positive values, or `0`
(unbounded) when both are `0`. So the follower can only **tighten** the staleness
window, never loosen it, and an unset follower value does not brick delegation.

A non-delegating asset resolves `ok=true` from its own fields directly; a null own
root is left for the downstream `zk-root-not-set` check rather than failed here.

### 2.3 Registration and spend-time guardrails

Delegation is checked twice: at **registration** (ConnectBlock,
`assets::ValidateDelegateRegistration`) and again at **spend** (CheckTxInputs, via
the resolver). The spend-time check is authoritative because the source can change
state after B installs the pointer.

The delegate pointer an `IssuerReg` produces is computed by
`assets::ResolveRegDelegate(reg, asset_id, prev_delegate)` — the single source of
truth shared by the registry-update path and the in-flight-reg spend path:

- **v1 reg** (mint / `rotatezk` / governance) → **inherits** `prev_delegate`. This
  is load-bearing: those operations emit v1 IssuerRegs that know nothing about the
  delegate field, so consensus carries the prior pointer forward — otherwise
  minting a follower would silently clear its delegation.
- **v2 reg, `delegate == own asset_id`** → **clears** the pointer (explicit opt-out
  sentinel). Real self-delegation is therefore impossible.
- **v2 reg, `delegate != own asset_id`** → installs / changes the pointer.

Consensus-enforced guardrails (reject reasons in code font):

- **Self-delegation rejected.** `A == B` → `kyc-delegate-self`.
- **One-hop only, checked at SPEND time.** If the resolved source A itself has a
  non-null `compliance_delegate_asset_id`, fail closed → `kyc-delegate-multi-hop`.
  A can start delegating *after* B opted in, so the registration-time check alone
  is insufficient.
- **Source must be usable:** A exists (`kyc-delegate-source-missing`), `A.has_kyc`
  (`kyc-delegate-source-no-kyc`), `A.compliance_root_commit != null`
  (`kyc-delegate-source-no-root`), and `A.zk_vk_commitment ∈ canonical allowlist`
  (`kyc-delegate-source-noncanonical`, §3).
- **Source resolution uses confirmed state.** A's policy is read from the committed
  registry state in the current view; intra-block updates to A (e.g. a same-block
  A rotation) are staged and applied later in block connection and are not relied
  upon during input validation.
- **While `delegate != 0`, B's own `compliance_root_commit` is ignored** — B's
  identity material comes entirely from A.
- **Opt-out is an explicit v2 reg whose `delegate == own asset_id`.** It must land
  a coherent self-config in that one tx: a **canonical own VK**
  (`kyc-optout-own-vk-noncanonical`) and a **non-null own root**
  (`kyc-optout-no-root`). Operationally B must have a tree ready before opting out.
- **Delegation requires `has_kyc`.** A v2 delegate on a non-KYC reg →
  `kyc-delegate-requires-kyc`.
- **Own VK must be canonical on install** (`kyc-delegate-own-vk-noncanonical`), so
  a later opt-out can never fall back to arbitrary VK material.
- **Governance still applies.** If B's ICU rotations are quorum-gated
  (`policy_quorum_bps > 0`), installing or changing the delegate pointer is itself
  a governed change.

### 2.4 Staleness heartbeat for delegated assets

The active root is honored with no age check; `max_root_age` ages out only
*historical* roots after a rotation. For a self-maintained asset this is correct:
an issuer with a deliberately static whitelist keeps working against its last
active root.

For a **delegated** asset this default would be unsafe: if A abandoned the cohort,
B would keep honoring a stale whitelist and revocations A would have made would
never propagate. To make freshness enforceable, consensus applies an **active-root
staleness heartbeat scoped to delegated assets** (`tx_verify.cpp`): when
`compliance_delegate_asset_id != 0`, `max_root_age > 0` and the source's active
root has an activation height, a spend is rejected if

```
nSpendHeight - active_root_activation_height > max_root_age
```

with reject code `kyc-delegate-source-stale`. This converts A's maintenance into
an enforced heartbeat: A going dark *does* freeze its followers, forcing A to keep
rotating. A self-maintained (non-delegated) issuer is unaffected.

The heartbeat reads the **source's** `active_root_activation_height`.
`assets::DeriveActiveRootActivationHeight` returns the explicit field when set, and
otherwise (legacy entries) derives it by scanning `compliance_root_history` for the
entry matching the active root. The explicit field is maintained by
`ApplyComplianceRootUpdate` whenever the active root is set or rotated; it is not
re-derived from history at spend time, because the "root unchanged" branch does not
push a history entry, so the back entry is not guaranteed to correspond to the
active root.

### 2.5 Trust bound — "A goes rogue"

Even if A rotated to a malicious or permissive VK, the worst case is **KYC bypass
for B's asset** (non-whitelisted keys pass the gate) — **never theft**, because
the proof authorizes nothing; key control is the Taproot signature. B detects this
and opts out in one transaction under its own authority. The canonical allowlist
(§3) removes the bypass vector as well by constraining which VKs A may rotate to.

A root provider is, in protocol terms, **just an ordinary asset issuer**: there is no
separate compliance-root registry and no on-chain bonding or slashing of providers.
KYC correctness is not objectively provable on-chain, so there is no objective fault
to slash; accountability is the follower's one-transaction **exit**. Nothing in the protocol distinguishes a pure root-provider
asset from a circulating token; an issuer that wants a provider-only asset configures
its issuance and script policy so it does not circulate as a tradable balance.

### 2.6 RPC / UI

- **`updatecomplianceroot`** (`wallet/rpc/compliance_root.cpp`,
  `wallet/rpc/assets.cpp`) carries the delegation controls:
  - `delegate_asset` — install / follow this source asset's compliance cohort
    (emits an `IssuerReg` v2). Omitting it leaves delegation **unchanged** (a v1
    reg preserves any existing delegate, so mint/rotate never clear it).
  - `clear_delegation` — opt out: emit a v2 self reg that clears the delegate.
    Requires a canonical own VK and the non-null `compliance_root_commit` passed in
    the same call. `delegate_asset` and `clear_delegation` are mutually exclusive.
- **`getassetcomplianceroot`** surfaces both the declared
  `compliance_delegate_asset_id` and a resolved `effective_kyc_policy` object
  (`ok`, and when ok: `source_asset_id`, `vk_commitment`,
  `compliance_root_commit`, `max_root_age`; when not ok: `reason`). This lets a
  wallet show that a follower's effective cohort comes from A.

---

## 3. Canonical VK allowlist

Outside delegation, VK trust is pure hash-pinning: any issuer installs any VK as
long as `SHA256(SHA256(vk_data)) == zk_vk_commitment` (`validation.cpp`). That is
fine for self-maintained assets but unsafe for **delegated** shared compliance,
where A could otherwise rotate followers onto a permissive circuit.

The consensus allowlist (`assets/canonical_vk.{h,cpp}`) is a **metadata map**, not
just a set:

```
std::map<uint256, CanonicalVkInfo>   // vk_hash -> {circuit_id, depth, public_input_count, flags}
```

`CanonicalVkInfo.flags` records the circuit's shape so the resolver and tooling do
not have to re-derive it:

```
CANON_HD_OUTPUT_BOUND = 0x0001  // binds the spend output key (HDv1)
CANON_ASSET_ID_FREE   = 0x0002  // public_inputs[1] unconstrained in-circuit
CANON_TFR_FREE        = 0x0004  // public_inputs[3] unconstrained in-circuit
```

`assets::IsCanonicalVk(vk_hash)` is the consensus predicate: a null hash is never
canonical; otherwise membership in the map decides. Because the VK store is
content-addressed by hash, "asset uses a canonical circuit" is exactly
`zk_vk_commitment ∈ allowlist.keys()`, and the whole fleet referencing one
canonical circuit dedups to a single stored VK — no per-asset VK bloat.

The blessed entry is the depth-8 HDv1 verifying key, with metadata
`{circuit_id = "hd_v1_depth8", depth = 8, public_input_count = 6,
CANON_HD_OUTPUT_BOUND | CANON_ASSET_ID_FREE | CANON_TFR_FREE}`. Its commitment is
`SHA256(SHA256(vk_data))` over the on-chain custom-serialized VK bytes (the
`vk_hex` in `shared-utils/kyc-prover/vectors_hd_v1/golden_vectors_hd_v1.json`,
identical across all golden vectors — one trusted setup). Changing the allowlist
is a consensus change.

Delegation is gated on the allowlist on both sides: a delegation install requires
**A's VK** to be canonical (`kyc-delegate-source-noncanonical`) and **B's own VK**
to be canonical (`kyc-delegate-own-vk-noncanonical`), and the spend-time resolver
re-checks A's VK against the allowlist. A follower **follows A's VK, constrained to
the allowlist** — so if A later rotates from one canonical circuit to another,
every follower inherits it with zero follower-side action.

---

## 4. Rolling circuit migration — per-root VK history

A circuit migration is a VK change, and a VK change instantly invalidates every
cached or in-flight proof built for the old circuit. Under delegation this would be
a synchronized fleet-wide hard cutover across all followers at the migration block.
Two consensus features avoid that thundering herd:

1. **Per-root VK history.** Each historical root carries the VK it was committed
   under, stored in the `compliance_root_history_vk` deque parallel to
   `compliance_root_history` (`registry.h`). The two deques are maintained in exact
   lockstep by `assets::ApplyComplianceRootUpdate` — appended together, trimmed
   together to `MAX_ROOT_HISTORY = 32`, and cleared together. A legacy entry that
   has root history but no VK history is migrated by left-padding the VK deque with
   the prior entry's **active** VK (`prev.zk_vk_commitment`) — the VK every
   historical root was verified under before this upgrade. Padding with null would
   re-create the very hard cutover this feature avoids, so null is reserved to mean
   "truly no VK available." Validation rejects a malformed entry whose VK history is
   longer than its root history with `asset-registry-malformed`.

2. **VK chosen after the root match.** `CheckTxInputs` defaults `verify_vk` to the
   active VK, and on a **historical-root** match selects that root's own
   `compliance_root_history_vk[i]` (falling back to the active VK if that slot is
   null). `inputs.ReadZkVerifyingKey` is then performed **after** the root match.
   So a proof built for an older circuit still verifies under that circuit's VK
   while its root remains within `max_root_age`.

The old circuit's VK bytes stay resident automatically: the VK store is
content-addressed and a forward rotation does not erase the previous VK. The only
stored-VK erase is the reorg-undo path (`validation.cpp` → `txdb.cpp`); the erase
during normal connection clears the in-memory `vk_chunks` accumulator, not the
persisted VK. Storing the commitment per history entry is therefore sufficient.

Together these make a migration **rolling** rather than a synchronized re-prove: a
grace window spanning the circuit change lets old proofs continue to verify until
their root ages out.

---

## 5. Trust & bonding summary

| Concern | Behavior |
|---------|----------|
| A maintenance burden on follower B | Removed — B points once, A's rotations propagate |
| A goes dark | The delegated-asset heartbeat (§2.4) freezes B once A's active root exceeds the effective window, forcing A to keep rotating |
| A rotates to an evil VK | KYC bypass for B at worst, **never theft**; the canonical allowlist (§3) blocks non-blessed VKs; B exits in one tx |
| Accountability of A | Follower **exit** (one tx) + reputation; there is no on-chain slashing of a provider |
| Liveness concentration | Many followers share fate on A — intrinsic to delegation |
| Shared permission pool | Following A means A's cohort is B's cohort, pooled with all other followers — by design |
| Provider is "just an asset" | Yes; the protocol has no separate root-provider type — configure a provider asset's issuance/script policy so it does not circulate |

---

## 6. Reject codes

Delegation and KYC enforcement surface these consensus reject reasons:

**Registration (ConnectBlock):**
`kyc-delegate-inactive` (before `AssetsDelegationHeight`), `kyc-delegate-requires-kyc`,
`kyc-delegate-source-missing`, `kyc-delegate-source-no-kyc`, `kyc-delegate-multi-hop`,
`kyc-delegate-source-no-root`, `kyc-delegate-source-noncanonical`,
`kyc-delegate-own-vk-noncanonical`, `kyc-optout-own-vk-noncanonical`,
`kyc-optout-no-root`, `asset-registry-malformed`, `policy-core-changed`.

**Spend (CheckTxInputs):**
`zk-asset-mismatch`, `zk-root-not-set`, `kyc-delegate-source-stale`,
`zk-root-mismatch`, `zk-vk-missing`, `zk-anchor-mismatch`,
`kyc-proof-output-mismatch`, plus the resolver's `kyc-delegate-self` /
`-source-missing` / `-source-no-kyc` / `-multi-hop` / `-source-no-root` /
`-source-noncanonical` re-checks, and the Groth16 verifier's `zk-proof-bad`.

---

## 7. Privacy note

Remote proving is non-custodial (no master private key in the witness; key control
via the Taproot signature) but not privacy-free — the prover learns the parent
pubkey ↔ child ↔ spend context. If holders self-prove, this is moot. If a shared
provider runs the prover for followers' holders, it sees their spend activity.
Issuers relying on a shared prover should account for this.

---

## 8. File reference index

| Topic | File |
|-------|------|
| Registry entry / root + VK history / delegate pointer | `services/core-node/bcore/src/assets/registry.h` |
| `IssuerReg` v1/v2 + ZK/ICU TLV structs | `services/core-node/bcore/src/assets/asset.h` |
| `IssuerReg` v2 parser (trailing delegate) | `services/core-node/bcore/src/assets/asset_parser_v1.cpp` |
| Delegation resolver / reg-check / root-update | `services/core-node/bcore/src/assets/kyc_delegation.{h,cpp}` |
| Canonical VK allowlist | `services/core-node/bcore/src/assets/canonical_vk.{h,cpp}` |
| Registration wiring + activation gate | `services/core-node/bcore/src/validation.cpp` |
| Core policy immutability | `services/core-node/bcore/src/assets/asset.cpp`, `validation.cpp` |
| KYC enforcement + heartbeat + VK-after-root | `services/core-node/bcore/src/consensus/tx_verify.cpp` |
| Groth16 verify | `services/core-node/bcore/src/crypto/groth16.cpp` |
| HDv1 circuit | `shared-utils/kyc-prover/internal/circuit/circuit_hd_v1.go` |
| Tree builder / empty + revoked leaf convention | `services/core-node/bcore/src/wallet/rpc/compliance_root.cpp` |
| Compliance-root RPCs (incl. delegate options) | `services/core-node/bcore/src/wallet/rpc/compliance_root.cpp`, `wallet/rpc/assets.cpp` |
| Shared-root note | `KYC_v2.md` |
