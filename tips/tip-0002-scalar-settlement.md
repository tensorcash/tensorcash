```
TIP: 0002
Title: Issuer-published scalar settlement, non-native collateral, and two-sided securitisation
Author: takakuni <takakuni@tensorcash.org>
Type: Standards Track
Status: Draft
Created: 2026-06-25
```

## Abstract

This TIP proposes a generic cash-settled covenant, `OP_SCALAR_CFD_SETTLE`, that
settles against a scalar an asset issuer publishes on-chain, rather than only
against the chain-intrinsic difficulty value the existing `OP_DIFFCFD_SETTLE`
covenant uses. It specifies three orthogonal generalisations that compose into a
single covenant stack: (1) a **scalar publication subsystem** — a dedicated
output TLV and consensus index that records `(underlying asset, feed, epoch) →
scalar` as an immutable, monotonic, O(1) lookup; (2) **non-native collateral**,
allowing the initial margin and settlement payouts to be denominated in any
opt-in "collateral-safe" asset rather than only native units; and (3)
**two-sided securitisation**, tokenising both the long and short legs of a
contract into two independently transferable assets redeemable against covenant
pots, with a permissionless complete-set unwind. The new opcode reuses the
proven resolver-snapshot, burial, and payout machinery of `OP_DIFFCFD_SETTLE`;
it is a parallel covenant, not a modification of the existing one. Activation is
gated by a single script-verification flag tied to a block height, leaving every
existing network unaffected until a coordinated activation height is set.

## Motivation

TensorCash already ships a margined, cash-settled covenant — `OP_DIFFCFD_SETTLE`
(`0xbe`) — but it is narrow in three independent ways:

- It settles only on a **single chain-intrinsic scalar** (`nBits` at a fixing
  height). There is no way to write a covenant that settles on a price, an
  index, an FX rate, a compute/model index, or any value that is not a function
  of the chain itself.
- Both the posted margin and the payout outputs are **native-only**. A contract
  cannot be collateralised or settled in an issued asset.
- Only one of its two legs is securitisable (via option series); there is no way
  to tokenise **both** sides of a contract into fungible, transferable claims.

Each limitation blocks a class of products that a layer-1 with first-class
assets should be able to express: hedges on off-chain references, asset-margined
derivatives, and fully securitised two-sided notes that trade as a complete set.
The existing difficulty covenant is the right pattern — committed-literal leaves,
keeper-settlable, immutable buried fixings read on parallel validation threads —
but it is hard-wired to one scalar, one collateral type, and one securitisable
side. This proposal generalises each axis while preserving the existing covenant
unchanged as the chain-intrinsic special case.

## Specification

The keywords MUST, SHOULD, and MAY are to be interpreted as in RFC 2119.

This specification defines four cooperating components:

1. A new output TLV and consensus index for **scalar publication** (§Publication).
2. The **resolution rule** that turns a published scalar into an immutable
   fixing usable by parallel script validation (§Resolution).
3. The **`OP_SCALAR_CFD_SETTLE` opcode**, including native and non-native
   collateral (§Opcode, §Collateral).
4. The **two-sided securitisation** descriptor and unwind covenant
   (§Securitisation).

Up to four distinct assets participate, and the design keeps them strictly
independent:

| Symbol | Role |
|---|---|
| **U** | the *underlying* — the asset whose issuer publishes the settlement scalar (trusted-issuer oracle) |
| **C** | the *collateral / initial-margin* asset locked in the vaults and paid out at settlement |
| **L** | the *long* securitisation token — a claim on the long side's profit and loss |
| **S** | the *short* securitisation token — a claim on the short side's profit and loss |

### Publication: the `ISSUER_SCALAR` output TLV

A scalar is published by spending the underlying asset's current Issuer Control
UTXO (ICU) and emitting a dedicated carrier output bearing a new output-extension
TLV.

- A new output-extension type `ISSUER_SCALAR = 0x11` MUST be defined, adjacent to
  the existing `ISSUER_REG = 0x10`. Because a `CTxOut` carries at most one
  extension TLV, the scalar MUST NOT share the ICU successor output (which carries
  `ISSUER_REG`); it MUST appear on a separate carrier output in the same
  transaction.
- The carrier output's `scriptPubKey` MUST be provably unspendable (an
  `OP_RETURN` form), so that the durable scalar state lives only in the index
  (below) and the carrier does not bloat the UTXO set.

The TLV body is fixed-width, with no nesting and no textual encoding:

```
underlying_asset_id : 32 bytes   # MUST equal the asset of an ICU spent by this tx
feed_id             : uint32 LE  # which feed of the underlying
scalar_epoch        : uint64 LE  # monotonic per (underlying_asset_id, feed_id)
scalar_format_id    : uint16 LE  # scalar ENCODING only (see §Payoff and encoding)
scalar              : 32 bytes   # raw 256-bit value, interpreted per scalar_format_id
```

The TLV is purely structural at the codec layer; all semantic checks are
performed at block connection (below). The `scalar` field is stored at a fixed
32-byte width regardless of `scalar_format_id`; the index is encoding-agnostic
and decoding is the opcode's responsibility.

#### State: a dedicated consensus index

Published scalars MUST be recorded in a new index namespace `DB_ASSET_SCALAR`,
distinct from the rotation history and from the ICU payload namespace, both of
which it MUST NOT use:

- **Per-epoch record** — key `(underlying_asset_id:32, feed_id:4, scalar_epoch:8)`
  → value `{ scalar, publication_height:int32, scalar_format_id:uint16 }`. This
  is a single point lookup (O(1)), the same cost class as a coin read.
- **Head record** — key `(underlying_asset_id:32, feed_id:4)` → `last_epoch:uint64`.
  This record is REQUIRED. It makes the monotonicity check and "latest epoch"
  lookup O(1), avoiding any range scan.

Both records MUST be staged and committed atomically in the same database batch
as the best-block / asset-best-block markers, and MUST be reversible through a
dedicated undo channel that, on disconnect, erases the per-epoch record and
restores the prior head (or removes the head if the publication created the
feed).

#### Publication validation (at block connection)

A transaction is a valid scalar publication for `(U, feed_id)` if and only if all
of the following hold; each failure produces the indicated reject reason:

1. It spends `U`'s current ICU (issuer authentication). The ICU rotation rules
   continue to apply to the successor output.
2. It contains exactly one `ISSUER_SCALAR` carrier output whose
   `underlying_asset_id` equals the asset of an ICU actually spent by the
   transaction (`carrier↔asset` disambiguation).
3. `scalar_format_id` is a known format (`scalar-bad-format`).
4. The carrier output is provably unspendable (`scalar-carrier-spendable`).
5. `scalar_epoch != 0` — epoch 0 is reserved and invalid (`scalar-zero-epoch`).
6. `scalar_epoch == head.last_epoch + 1`, treating a missing head as 0; the
   addition MUST be checked for overflow (`scalar-epoch-overflow`,
   `scalar-nonmonotonic`). Publications are append-only and monotonic.
7. `(U, feed_id, scalar_epoch)` does not already exist (`scalar-duplicate-epoch`).
   Epochs are immutable and MUST NOT be overwritten.

The validation order is normative:
`BadFormat → CarrierSpendable → UnknownAsset → NoIcuAuth → ZeroEpoch →
EpochOverflow → NonMonotonic → DuplicateEpoch`.

A published epoch's *value* is fixed forever once written with
`publication_height = the connecting block height`; only its burial status
subsequently evolves.

**Allowlist sequencing (consensus-critical).** Block connection enforces an
output-TLV allowlist that rejects any extension TLV it cannot parse as a known
type. Adding `ISSUER_SCALAR` to that allowlist makes carriers consensus-valid.
Because the structural parser is unauthenticated, the allowlist entry MUST be
added **atomically with** the publication validation above and **height-gated**
behind the activation height (§Activation): below the height, `0x11` MUST remain
rejected exactly as today, so pre-activation blocks validate identically.

**Relay and batching.** Because a publication spends and recreates the asset's
ICU, it emits an `ISSUER_REG` successor, and at most one `ISSUER_REG` per asset
per block is permitted. Therefore standard relay admits at most one publication
per asset per block, and exactly one `ISSUER_SCALAR` carrier per transaction
(more than one provably-unspendable output is non-standard). Multiple epochs for
one feed in a single block are possible only as a single transaction with one
ICU successor and multiple carriers; such a transaction is block-valid but not
relay-standard. Block order then output order is the deterministic publication
order in all cases.

### Resolution: immutable fixing snapshot

Script validation runs on parallel worker threads that MUST NOT read the mutable
coin cache. As with the difficulty covenant — which is safe only because it reads
immutable block-index ancestry — scalar resolution MUST be frozen before parallel
dispatch:

- Single-threaded, before the script-check queue is dispatched, a witness
  pre-scan MUST detect each revealed leaf containing `OP_SCALAR_CFD_SETTLE` and
  read its committed `(source_type, underlying_asset_id, feed_id, fixing_ref,
  publication_deadline_height, fallback_scalar)` operands.
- For each, the engine MUST resolve the scalar from the registry view, apply
  burial and the deadline/fallback rule (below), and freeze the **effective**
  result into an immutable snapshot that is passed by const reference to every
  script check. The opcode reads only the snapshot, never a live cache.

**Burial.** A real fixing is usable only if `publication_height <=
context_height - MATURITY` and `publication_height < context_height`.
Same-block publications are therefore never usable, eliminating intra-block
dependency races.

**Deadline / missing-fixing fallback.** To guarantee that collateral can never be
locked forever by a feed that goes silent, the leaf commits a
`publication_deadline_height` and a `fallback_scalar`, and the snapshot applies
this deterministic three-way rule, where `context_height` is the connecting block
(or, for mempool, tip) height:

```
usable_real = a published (U, feed_id, fixing_ref) record exists
          AND record.scalar_format_id == leaf.scalar_format_id
          AND record.publication_height <= publication_deadline_height
          AND buried(record.publication_height)

if usable_real:
    effective = record.scalar
elif context_height >= publication_deadline_height + max(FALLBACK_GRACE, MATURITY):
    effective = fallback_scalar
else:
    effective = none   # still pending; the opcode fails and the spend waits
```

The following properties are normative:

- A record with `publication_height > publication_deadline_height` (a late
  publication) MUST be ignored. Once past the deadline the fixing is fixed —
  real-if-in-time, else fallback — so there is no race between a late "real"
  publication and a fallback spend. This is why the fallback is a resolution
  rule, not a competing leaf.
- A real record whose `scalar_format_id` differs from the leaf's committed value
  MUST be treated as unusable (ignored, like a late or missing fixing), so the
  contract falls through to the fallback rather than decoding bytes under the
  wrong encoding. Consequently the effective scalar is always in the leaf's
  declared format, and the opcode needs no separate format-equality check.
- The effective grace MUST be `max(FALLBACK_GRACE, MATURITY)`. With grace ≥
  maturity, any fixing published at or before the deadline is buried — and thus
  wins the first branch — before the fallback can ever fire, even if a chain
  parameter mis-sets grace below maturity. This makes the absence of a race a
  property of the code, not of configuration.
- All height arithmetic MUST be performed in 64-bit signed integers so that an
  adversarial near-maximal `publication_deadline_height` cannot overflow.

**Fail-closed pre-scan.** Detection of "this leaf contains
`OP_SCALAR_CFD_SETTLE`" MUST be conservative (it may over-count). Operand
extraction MUST be exact: if a revealed leaf does not match the canonical template
(§Opcode), the pre-scan MUST provide no snapshot entry, and the opcode MUST then
fail (`SCALARCFD_FIXING`). Detection over-approximates and resolution
under-approximates; both directions fail safe, so no parser/interpreter
disagreement can ever validate.

**Non-cacheability.** A settlement transaction that reads a resolved scalar MUST
bypass the script-validity cache: although an epoch's value is immutable while it
exists, a reorg deeper than `MATURITY` can remove or replace the publication on
the new chain, so the same transaction may validate differently.

**One-settle-input rule.** At most one input per transaction may reveal a
settlement covenant (counting `OP_DIFFCFD_SETTLE` and `OP_SCALAR_CFD_SETTLE`
leaves together). Relaxing this to permit multiple settlements with distinct
collateral assets in one transaction is explicitly out of scope for this TIP and
would require a separate written fund-safety analysis.

### Opcode: `OP_SCALAR_CFD_SETTLE`

#### Slot and activation

- The opcode MUST occupy `OP_NOP10` (`0xb9`), the last clean NOP slot, renamed to
  `OP_SCALAR_CFD_SETTLE`. This follows the established `OP_CHECKLOCKTIMEVERIFY` /
  `OP_CHECKSEQUENCEVERIFY` upgrade pattern (repurposing a NOP), and leaves the
  OP_SUCCESS range and the witness pre-scan untouched. A non-NOP slot such as
  `0xbf` is explicitly NOT used, because it would pull the opcode out of the
  OP_SUCCESS sweep and force `IsOpSuccess` and the witness pre-scan to become
  height-aware.
- Enforcement MUST be driven by a **script-verification flag**, not a height read
  inside the interpreter. A new flag `SCRIPT_VERIFY_SCALAR_CFD` MUST be defined
  and set in the per-block script flags when the block is at or above the
  activation height (§Activation), alongside the existing time-lock flags. When
  the flag is unset, `0xb9` MUST behave as the legacy `OP_NOP10` no-op; when set,
  it MUST perform full covenant enforcement. The interpreter MUST NOT read block
  height.
- For mempool relay, the flag MUST be computed dynamically for `tip + 1` (not
  taken from the constant standard-flags set), so that relay enforces the
  covenant one block ahead of the consensus flag-day. The consensus re-check path
  MUST use the same `tip + 1` flags so it agrees with block validation at the
  boundary.

#### Pre-activation safety

While the flag is unset, `0xb9` is an inert no-op, yet a canonical scalar-settle
vault MUST remain unspendable rather than stealable. The leaf (below) pushes all
its committed operands and the inert NOP consumes none; after execution the
tapscript stack holds more than one element, and the tapscript clean-stack rule
rejects the spend. With a NUMS internal key (no key path) and the settle leaf as
the only committed script, such a vault cannot be spent at all until activation.
As belt-and-suspenders (RECOMMENDED, not load-bearing for the canonical leaf), a
wallet SHOULD additionally commit `<settle_lock_height> OP_CHECKLOCKTIMEVERIFY`
with `settle_lock_height >= activation height` (ideally `+ MATURITY`) so that any
non-canonical or multi-leaf vault is also time-fenced past activation. Before an
activation height is chosen, the live chain and UTXO set SHOULD be scanned for any
spendable use of `OP_NOP10`, so the flag-day cannot retroactively invalidate a
real spend.

#### Leaf script (committed literals)

```
<contract_id32> OP_DROP                  # per-instance uniqueness for NUMS-shared vaults
<template_version=0x01>                   # 1 byte; FIRST committed field after the id
<settle_lock_height> OP_CHECKLOCKTIMEVERIFY OP_DROP
<source_type>                             # 1 byte: 0x00 ISSUER_PUBLISHED / 0x01 CHAIN_INTRINSIC
<underlying_asset_id32>                   # U; zero if CHAIN_INTRINSIC
<feed_id_le4>                             # ISSUER_PUBLISHED: feed of U; CHAIN_INTRINSIC: metric_id||window_code
<fixing_ref_le8>                          # ISSUER_PUBLISHED: scalar_epoch; CHAIN_INTRINSIC: window-end height
<publication_deadline_height_le4>         # last height a real fixing counts; then fallback applies
<payoff_mode>                             # 1 byte: 0 STRIKE / 1 REALIZED / 2 FIXED-REF (deferred)
<scalar_format_id_le2>                    # scalar encoding; settlement requires published == this
<strike_le32>                             # K, in scalar_format_id encoding (canonical)
<fallback_scalar_le32>                    # used iff no real fixing by the deadline; same encoding
<lambda_q_le4>                            # Q16 leverage
<loss_direction>                          # raw 1-byte push: 0x00 long / 0x01 short
<collateral_asset_id32>                   # C; 32 zero bytes = NATIVE_SENTINEL
<vault_im_le8>                            # initial margin, in C units
<owner_key32> <cp_key32>
OP_SCALAR_CFD_SETTLE
```

All economic parameters are committed literals, so they enter the tapleaf hash
and the vault address and are tamper-proof. The witness reveals only the leaf and
control block — no economic arguments and no signature (the covenant is
keeper-settlable). The scalar is never on the stack; it is folded from the
resolution snapshot.

`template_version` MUST be the first committed field. v1 is `0x01`. An unknown
version MUST fail closed (no canonical template → no snapshot entry → opcode
fails). Future fields (for example, a `FIXED-REF` denominator, §Payoff) land as a
clean `template_version = 0x02` rather than an ambiguous parsing fork.

**Canonical push encoding (consensus).** Each operand has exactly one legal push
form, which the pre-scan and interpreter both require byte-for-byte:

- Fixed-width blobs (`feed_id`, `fixing_ref`, `publication_deadline_height`,
  `strike`, `fallback_scalar`, `lambda_q`, `vault_im`, asset ids, keys): a direct
  data push of exactly that length, never `OP_PUSHDATA*`, never a minimal-number
  or `OP_n` shortcut.
- `template_version`, `source_type`, `payoff_mode` (1 byte each) and
  `scalar_format_id` (2 bytes): direct data pushes.
- `loss_direction`: a raw 1-byte data push of `0x00` or `0x01` — NOT `OP_1` /
  `OP_TRUE` (which are opcodes and would change the leaf bytes and the tapleaf
  hash).

Any deviation MUST cause the pre-scan to find no canonical template, yielding no
snapshot entry and an opcode failure.

#### Evaluation

When `SCRIPT_VERIFY_SCALAR_CFD` is set, the opcode MUST proceed as follows; each
failure produces the indicated `SCALARCFD_*` reject code:

0. **Context guard.** MUST be tapscript, witness version 1. The stack MUST hold
   all operands with exact lengths and canonical encodings, else
   `SCALARCFD_ENCODING`.
1. **Resolve.** Fetch the effective scalar `S` from the snapshot for
   `(source_type, underlying_asset_id, feed_id, fixing_ref)`. If absent (pending,
   or unparseable leaf), fail `SCALARCFD_FIXING`. By construction `S` is already
   in the leaf's `scalar_format_id` (§Resolution).
2. **Decode and canonicality.** Decode `K` (strike) and `S` per
   `scalar_format_id`. Fail on invalid decode. Re-encoding `K` MUST reproduce the
   committed `strike` (canonicality is enforced on the strike only; the realized
   value is consensus-read). `lambda_q` MUST be non-zero; `vault_im` MUST meet the
   asset minimum.
3. **Payout.** Compute `{payout_owner, payout_cp}` from `(payoff_mode, K, S,
   lambda_q, vault_im, loss_direction)` (§Payoff). The two legs sum to `vault_im`.
4. **Collateral policy guard** (non-native only, §Collateral). If
   `collateral_asset_id != NATIVE_SENTINEL`, the resolved collateral policy MUST
   exist (`SCALARCFD_COLLATERAL`) and MUST carry the `COLLATERAL_SAFE` bit with
   `kyc_flags == 0`, `tfr_flags == 0`, and `WRAP_REQUIRED` unset, else
   `SCALARCFD_COLLATERAL`.
5. **Input binding.** If native, the spent input amount MUST equal `vault_im`. If
   an asset, the spent coin MUST carry an asset TLV with `asset_id ==
   collateral_asset_id` and `asset_amount == vault_im`, else `SCALARCFD_AMOUNT`.
6. **Output binding (exact-distinct).** For each non-zero leg there MUST be a
   distinct output whose `scriptPubKey` is exactly `OP_1 <leg_key>` and:
   - native: `nValue == payout_leg` exactly, with no asset TLV;
   - asset: `asset_id == collateral_asset_id` and `asset_amount == payout_leg`
     exactly, with `nValue >= kMinAssetOutputDust` (NOT exact — the keeper funds
     the native dust carried alongside the asset).
   One output MUST NOT satisfy two legs (`SCALARCFD_OUTPUTS` / `SCALARCFD_CONTEXT`).
7. Pop all operands and push true (VERIFY-style).

The set of reject codes is `SCALARCFD_{CONTEXT, FIXING, ENCODING, TERMS, OUTPUTS,
AMOUNT, COLLATERAL}`.

#### Payoff and encoding

The publication carries only the **encoding** (`scalar_format_id`); the leaf
carries both the encoding (which MUST match the publication) and the **payoff
mode** (which the publication does not constrain). One published feed can thus
back contracts with different payoff modes.

The payout generalises the difficulty payout, which uses a realized-value
denominator. The denominator is selected by `payoff_mode`:

| mode | denominator | `f_loss` | use |
|---|---|---|---|
| **0 STRIKE** (default) | committed `K` | `clamp(λ·\|S−K\|/K, 0, 1)` | percent move from strike; symmetric, linear in `S`, fully deterministic |
| 1 REALIZED | resolved `S` | `clamp(λ·\|S−K\|/S, 0, 1)` | reproduces difficulty semantics; for rate/intensity feeds |
| 2 FIXED-REF (deferred) | committed `R` | `clamp(λ·\|S−K\|/R, 0, 1)` | absolute move scaled by a fixed notional |

`payout_cp = floor(f_loss · vault_im)`, the remainder goes to the owner, and a
sub-dust leg is snapped per the asset floor. Modes 0 and 1 are specified by this
TIP; mode 1 requires no extra operand. **Mode 2 is deferred**: it needs a
committed denominator `R` that the v1 leaf does not carry, so it MUST NOT be added
to v1 and requires a `template_version` bump.

`scalar`, `strike`, and `fallback_scalar` are fixed-width unsigned integers
interpreted per `scalar_format_id`. v1 defines exactly one format,
`SCALAR_FORMAT_RAW_U256_LE = 0x0001`. A richer Q-format catalogue MAY extend the
set of known formats later without touching the opcode. Payout arithmetic MUST use
the same wide (512-bit) accumulator envelope as the difficulty payout so that all
products are representable.

### Collateral: non-native asset C

When `collateral_asset_id != NATIVE_SENTINEL`, the margin and payouts are
denominated in an issued asset:

- The signature checker MUST expose the spent coin's `{asset_id, asset_amount}`
  to the opcode (a new resolver surface), plumbed like the existing fixing
  context.
- A settlement spends one asset-tagged vault of `(C, vault_im)` and emits at most
  two `C`-tagged outputs summing to `vault_im`. The net asset delta is therefore
  zero — a pure transfer — so no ICU spend is required. Native fees come from a
  separate input; the margin split is never shaved.
- The per-leg floor reuses the existing minimum asset-output dust constant.

#### The `COLLATERAL_SAFE` rule (consensus)

A keyless covenant cannot supply KYC proofs or wrap material, so a collateral
asset that carries KYC, transfer-restriction (TFR), or wrap-required constraints
would make settlement impossible and trap the funds. KYC, TFR, and the core
policy bits are already immutable after issuance. The wrap-required flag, which
lives in the governance-mutable ICU flags, is **not** frozen by a zero governance
quorum — it can be enabled at any time by the ICU holder. A settlement-time
policy check alone is therefore insufficient: it gives safety but not liveness,
because a clean collateral asset could drift into a constrained state before a
long-dated note settles.

This TIP therefore proposes a registration-time immutability guarantee:

1. A new opt-in profile bit `COLLATERAL_SAFE = 0x0040`. It MUST NOT be folded
   into the core policy commitment (it MUST be excluded from the immutable policy
   bit mask). Folding it in would make it part of a value recomputed
   unconditionally at every registration, so a pre-activation registration
   carrying the bit would cause upgraded and un-upgraded nodes to derive
   different registry state — divergence before the flag-day.
2. Instead, immutability is enforced by a single height-gated rotation rule.
   **Below** the activation height, `0x40` is an ordinary ignored policy bit and
   the policy commitment is byte-identical on every binary (no divergence).
   **At or above** the activation height, a rotation MUST NOT (a) toggle the
   `COLLATERAL_SAFE` bit (add or remove), nor (b) change the ICU flags of any
   asset that carries it.

Because vaults exist only after activation — where both the KYC/TFR freeze and
this rule are active — the collateral policy resolved at settlement always equals
the policy at vault creation. A conforming vault therefore always settles (live)
and cannot be griefed by drift (safe). The trade-off is intentional: an asset
that did not declare `COLLATERAL_SAFE` at registration cannot opt in later by
rotation; it must be reissued. Only assets that choose to be collateral pay the
immutability cost.

### Securitisation: two-sided tokens L and S

The existing option-series machinery cannot express this: it hard-wires
native-only vaults and pots, a single token, one token per pot, and one sink per
lot. This TIP proposes a new "scalar note pair" descriptor family that derives
two tokens together.

**Vault topology — number of vaults equals number of payoff ramps.** A single
`OP_SCALAR_CFD_SETTLE` evaluation is one strike, one direction, one cap — one
monotone ramp. Therefore:

- **Capped spread (common case).** One ramp → one vault. `vault_im` is the full
  collateral; `owner_key` and `cp_key` are the two pots. The opcode's
  `payout_owner + payout_cp == vault_im` invariant *is* the long/short split:
  the long token is the upper tail, the short token its complement. The note is
  fully funded and settles in a single transaction.
- **Two-sided payoff (collar/straddle).** Loses both downward to a floor and
  upward to a cap, each its own ramp → two vaults. Then `long_pot` accrues from
  `owner(long vault)` and `cp(short vault)`, and `short_pot` from
  `owner(short vault)` and `cp(long vault)`.

In all cases both keys of each vault are pots (there is no natural-counterparty
key); `L`-holders redeem `long_pot` and `S`-holders redeem `short_pot`.

**Descriptor and derivation.** One descriptor commits the underlying, feed,
fixing reference, deadline, payoff mode, scalar format, per-leg strike and
fallback and leverage, collateral asset, both margins, settle lock height, and
the two token asset ids `L` and `S` (which MAY be derived deterministically from
the series). Two pot families (long and short) are derived as output-match asset
covenants bound to the respective token id; pots and vaults are asset-`C`-tagged,
not native. `L` and `S` are issued as sponsored child assets with a fixed cap,
immutable policy, and mint-allowed only, sharing the inert-ICU invariants of the
existing option-series tokens.

**Permissionless complete-set unwind.** The vault taptree MUST carry a second
leaf alongside the settle leaf: anyone holding one `L` and one `S` MAY spend the
vault by retiring both tokens to their burn sinks and reclaiming the full
collateral — before or after maturity, with no fixing required. This is the
complete-set identity `L + S = collateral`:

```
unwind_leaf = <tapmatch(L_sink)> <L_asset_id> <1> OP_OUTPUTMATCH_ASSET OP_VERIFY
              <tapmatch(S_sink)> <S_asset_id> <1> OP_OUTPUTMATCH_ASSET
```

Because `OP_OUTPUTMATCH_ASSET` pushes a boolean rather than verifying, the two
legs are joined by `OP_VERIFY` so the first is checked fail-fast and the second is
the single terminal element the clean-stack rule requires; both legs are
mandatory. The unwind is the primary liveness backstop: even if the feed dies,
collateral is never trapped, because the market can always collapse the pair.

**Creation is issuer-gated; collapse is not.** Minting a new unit funds a new
vault and mints one `L` and one `S`, which (a positive asset delta) requires
spending the `L` and `S` issuer ICUs — issuer-controlled and capped at the
committed lot count. Redemption and unwind are permissionless (zero asset delta,
no ICU). A permissionless mint-factory is not expressible under current asset
rules and is out of scope for this TIP.

### Source types

The leaf's `source_type` selects the resolver, so one covenant, payout, and
securitisation stack serves both objective and oracle feeds:

- `0x00 ISSUER_PUBLISHED` — resolved from the `DB_ASSET_SCALAR` index above; trust
  is the underlying asset's issuer. Suitable for off-chain prices, indices, FX
  rates, and compute/model indices. This is the primary subject of this TIP.
- `0x01 CHAIN_INTRINSIC` — resolved from immutable block-index data by a
  chain reader (exactly how difficulty reads `nBits` at a height), requiring no
  oracle and no `ISSUER_SCALAR` index. The existing `OP_DIFFCFD_SETTLE` is the
  first such feed. Other objective feeds (for example, an average block-fee
  index over a window, maintained as a per-block cumulative aggregate read as an
  O(1) difference) can be added later as new resolvers with zero change to the
  covenant, payout math, securitisation, or interfaces. The `source_type` byte is
  specified from day one for this reason; in v1 only `ISSUER_PUBLISHED` need be
  wired, and an unresolvable `CHAIN_INTRINSIC` leaf MUST fail closed.

### Interfaces

The reference implementation SHOULD expose, at parity with the existing
difficulty and option-series surfaces:

- **Feed:** publish a scalar (issuer spends the ICU and emits the carrier), list
  feeds for an asset, and read `(asset_id, feed_id, epoch)` → value, height, and
  burial state.
- **Bilateral contract lifecycle:** propose / accept / import-acceptance /
  build-open / record-open / build-settlement / build-coop-close / finalize, with
  a forgery guard on settlement.
- **Securitisation lifecycle (two-sided):** register both `L` and `S` and their
  pot/sink families, issuer-gated issuance, listing, build-settlement (real or
  fallback), per-side redemption, and the permissionless complete-set unwind.

These are interface conveniences and are not consensus-normative.

## Rationale

- **A parallel opcode, not a mutation of `0xbe`.** The difficulty covenant is in
  use; changing its bytes would change its tapleaf hashes and break existing
  vaults. A new opcode that shares the payout math and resolver pattern keeps the
  chain-intrinsic special case exactly as-is.
- **Repurpose `OP_NOP10`.** This is the CLTV/CSV upgrade pattern and avoids
  making the OP_SUCCESS sweep and witness pre-scan height-aware, which a non-NOP
  slot would force.
- **Flag, not interpreter height read.** `EvalScript` has no block height by
  design; CLTV/CSV are flag-driven for exactly this reason. A flag set in the
  per-block flags keeps the interpreter context-free and makes activation a clean
  flag-day.
- **A dedicated index, not rotation history or ICU payload.** The rotation
  history is capped and nested inside a large blob that must be fully
  deserialised; the ICU payload namespace is opaque and variable-length. A
  feed is a separate concern and deserves an O(1) point-lookup index that never
  pollutes either.
- **Immutable, monotonic epochs.** They bound the trusted issuer's freedom to
  "value at publication plus timing," and make resolution a stable read during
  block connection. A position references a *future* epoch and trusts the issuer
  for it.
- **Deadline/fallback as a resolution rule, not a leaf.** Folding it into
  resolution — with late publications ignored and grace ≥ maturity — removes any
  keeper/bribery race between a late real publication and a fallback spend, which
  a second competing leaf would reintroduce.
- **Registration-time `COLLATERAL_SAFE`, not a settlement-time check.** A
  policy-equality check at settlement does not buy liveness: if a mutable
  collateral asset flips wrap-required, the funds are trapped either way. Only
  registration-time immutability guarantees a long-dated note can always settle.
  Keeping the bit out of the core policy commitment and gating its semantics
  purely on height is robust under both an immediate coordinated fork and a
  genuine future activation height, with no retroactivity to reason about.
- **Folded scalar resolution.** As with difficulty (which folds `nBits` rather
  than using a separate read opcode), folding resolution into settlement keeps
  the design to a single opcode and preserves the last NOP slot. A separate
  generic scalar-read primitive would have to take `0xbf` and re-open the
  OP_SUCCESS question; it is deferred and not needed for the covenant.

## Backwards compatibility

Below the activation height, this proposal is fully inert:

- `OP_NOP10` (`0xb9`) continues to evaluate as a consensus no-op; the
  `SCRIPT_VERIFY_SCALAR_CFD` flag is unset.
- The `ISSUER_SCALAR` (`0x11`) output TLV remains rejected by the output-TLV
  allowlist exactly as today, so pre-activation blocks validate identically on
  upgraded and un-upgraded nodes.
- The `COLLATERAL_SAFE` (`0x40`) profile bit is an ignored policy bit excluded
  from the core policy commitment, so the commitment is byte-identical on every
  binary and there is no pre-activation divergence.

The existing `OP_DIFFCFD_SETTLE` covenant, option series, and all existing assets
are unaffected.

Nodes that do not upgrade past the activation height will reject the new
consensus rules and fork off; this is a consensus change and requires a
coordinated upgrade.

**Disk format.** Adding the publication undo channel bumps the on-disk undo
record format. A node upgrading to a build that includes this change therefore
requires a reindex, exactly as for previous undo-record additions. This MUST be
stated in the release notes and deployment runbook for the activation, because
disconnecting a pre-upgrade block with an old undo record would otherwise fail to
deserialise.

## Activation parameters

This is a consensus Standards-Track change.

- **Activation mechanism.** A single new consensus parameter, the activation
  block height (`ScalarCfdHeight`). At or above this height, block validation
  (a) sets `SCRIPT_VERIFY_SCALAR_CFD` in the per-block script flags, (b)
  allowlists the `ISSUER_SCALAR` output TLV together with publication validation,
  and (c) enforces the `COLLATERAL_SAFE` rotation-immutability rule. Mempool relay
  computes the script flag for `tip + 1`, so relay leads the consensus flag-day
  by one block. The specific height is TBD and set per network at activation; it
  is `INT_MAX` (inert) on all production networks until set, and is settable on
  regression-test networks for testing.
- **Minimum node version.** The first released node version that implements this
  TIP; to be recorded when the reference implementation is tagged.
- **Rollback statement.** The consensus activation is non-rollback once the height
  is reached: blocks valid under the new rules would be rejected by older nodes.
  However, because every rule is inert below the height, *deployment of the binary
  is reversible until the height is chosen and reached* — operators can run the
  capable binary with the height unset without any consensus effect.
- **Monitoring / rollout plan.** Before fixing the height, scan the live chain and
  UTXO set for any spendable `OP_NOP10` usage (none is expected in tapscript) so
  the flag-day cannot invalidate a real spend. During rollout, monitor upgrade
  adoption and watch for any fork at the activation height; the abort path before
  lock-in is to leave the height unset (the rules stay inert). The activation
  SHOULD be accompanied by a signed release tag.

## Reference implementation

TBD — Draft. A reference implementation is developed against `bcore` as a series
of pull requests, each gated behind `ScalarCfdHeight` so it can land incrementally
without affecting live validation. This section will link the pull requests when
the TIP advances to Proposed. The principal source surfaces are:

- consensus / opcode: `src/script/script.h`, `src/script/interpreter.{h,cpp}`,
  `src/script/script_error.{h,cpp}`, `src/consensus/scalar_cfd.{h,cpp}`,
  `src/consensus/params.h`, `src/policy/policy.h`, `src/validation.cpp`;
- publication and index: `src/assets/asset.h`, `src/coins_asset_delta.h`,
  `src/txdb.cpp`, `src/coins.{h,cpp}`, `src/undo.h`, `src/validation.cpp`;
- wallet and interfaces: `src/wallet/` contract and note-pair builders and the
  scalar RPC surface.

## Security considerations

- **Trusted-issuer oracle.** For `ISSUER_PUBLISHED` feeds the issuer can publish
  any value; this is weaker than the objective difficulty feed and is an accepted
  trust model. Immutability and monotonic epochs bound the issuer's freedom to
  "value at publication plus timing"; a position references a future epoch and
  trusts the issuer for it. This trade-off MUST be documented prominently for
  users of such contracts.
- **Parallel-validation safety.** Resolution MUST be frozen single-threaded into
  an immutable snapshot before queue dispatch; the opcode MUST NOT read the
  mutable coin cache on worker threads. Burial (≥ `MATURITY`) and
  non-cacheability protect against reorg-induced divergence.
- **Fail-closed resolution.** A detected-but-unparseable settle leaf yields no
  snapshot entry and the opcode fails; a parser/interpreter disagreement can
  never validate.
- **No deadline race.** Late publications (after the deadline) are ignored, and
  the effective grace is `max(FALLBACK_GRACE, MATURITY)`, so an in-time fixing is
  always buried before the fallback can fire — there is no keeper/bribery race
  between a late real publication and a fallback spend.
- **Collateral liveness and griefing.** The `COLLATERAL_SAFE` registration-time
  immutability rule guarantees that the collateral policy at settlement equals
  the policy at vault creation, so a conforming vault always settles and cannot be
  trapped by an issuer enabling wrap-required, KYC, or TFR after the fact. A
  settlement-time check alone would not give this liveness.
- **Pre-activation funds.** Before activation, a canonical scalar-settle vault is
  unspendable by the tapscript clean-stack rule (no theft), and wallets SHOULD
  additionally time-fence vaults with a CLTV at or above the activation height.
- **UTXO hygiene.** The `ISSUER_SCALAR` carrier MUST be provably unspendable so
  feeds do not bloat the UTXO set.
- **Fund-safety scope.** The strict one-settle-input-per-transaction rule is
  retained; relaxing it for multiple distinct-collateral settlements would require
  a separate written proof and is out of scope.
- **Issuer-gated minting.** Creating new `L`/`S` units requires the `L`/`S`
  issuer ICUs; only redemption and unwind are permissionless, bounding supply to
  the committed cap.

## Test vectors

To be supplied with the reference implementation. At minimum they will cover:

- the `ISSUER_SCALAR` TLV wire layout (exact byte offsets and little-endian
  fields) and a non-canonical-encoding rejection;
- index atomicity, monotonic and immutable rejection paths, and reorg
  head-restore;
- per-mode golden payout vectors (sum-to-margin, clamp boundary, dust-snap) and
  strike canonicality;
- burial enforcement (block and mempool), the one-settle-input rejection, and
  non-cacheable re-evaluation after a simulated deep reorg;
- native-sentinel and asset collateral input/output binding, dust tolerance, and
  the collateral-gate rejections (missing `COLLATERAL_SAFE`; `COLLATERAL_SAFE`
  with non-zero KYC/TFR or wrap-required; rotation of ICU flags on a
  `COLLATERAL_SAFE` asset after activation);
- deadline/fallback resolution, including a late publication being ignored and
  the grace ≥ maturity property;
- the capped-spread single-vault split (long = upper tail, short = complement,
  sum = collateral), per-side redemption with cap, and the complete-set unwind
  (one `L` + one `S` → full collateral, pre- and post-maturity, no fixing);
- the activation boundary (no-op below the height, enforced at/above, mempool
  `tip + 1`) and the pre-activation unspendability of a canonical vault.

## Copyright

This document is released into the public domain (CC0).
