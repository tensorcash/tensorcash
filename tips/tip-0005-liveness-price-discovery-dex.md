```
TIP: 0005
Title: Liveness, price discovery, and a decentralised exchange (Layer 1.5)
Author: takakuni <takakuni@tensorcash.org>
Type: Informational
Status: Draft
Created: 2026-06-25
```

## Abstract

This Informational TIP frames a "Layer 1.5" market protocol family in which the
existing Bitcoin-derived layer-1 buys, with proof-of-work, a standing
Sybil-resistant validator cohort that runs a fast, fair-ordering exchange and
checkpoints its state back to L1, while L1 itself remains the settlement,
issuance, admission, and bridge-anchoring base layer. Hashpower buys the *right
to compete* for a validator seat; continued service keeps the seat alive; the
matching engine and market consensus live in the L1.5, exogenous to L1 block
production. L1 anchors only validator lifecycle and bond state, periodic state
commitments, data-availability and forced-exit guarantees, and wrapped-asset
mint/burn. Crucially, and following the project's working direction, the
validator committee key is **Schnorr-native on TensorCash and never natively
custodies foreign chains**: non-TSC assets enter the market as TensorCash-native
*wrapped* assets minted through treasury / wrapper bridges, kept separate from
the core validator key scheme. Privacy is public-facing — pooled deposits and
epoch-rotated pseudonyms — not validator-facing. This document fixes the design
decisions already reached, records the alternatives considered and rejected,
states the soft-fork encoding constraints the rollout should respect, and lists
the open engineering questions. It mandates no consensus rule; each component is
to be specified, when its wire format settles, in a future Standards-Track TIP.

## Motivation

TensorCash secures block production with useful proof-of-work, but a fair,
high-throughput marketplace — price discovery, liquidity formation, and
custody-anchored settlement — needs a different surface: a *standing* set of
peers, permissionless to join, costly to Sybil, continuously available, and
collectively trusted to checkpoint market state and authorise settlement, yet
deriving all of its trust from the same layer-1 it settles against. Three
properties make this hard, and no single existing mechanism supplies all three:

- **A standing cohort from a probabilistic process.** Proof-of-work is excellent
  for discovering *one* block but brittle for maintaining a *constant* cohort:
  if admission is a race and membership a fixed term, the set fills and empties
  in bursts and difficulty oscillates with sharp cliffs on entry and exit.
- **Liveness after admission.** Bitcoin requires no miner liveness once a block
  is found. A market-and-settlement cohort is the opposite: a member that stops
  participating in commitments, availability responses, or signing is worse than
  absent, because the funds it checkpoints and the markets it sequences depend on
  its continued presence.
- **Trust-minimised custody without a trusted operator.** If the layer holds
  meaningful value, users must be able to exit even if sequencers censor them or
  the cohort stalls — which requires committed state, data availability, and a
  forced-exit path, not merely a threshold signature.

A system with these properties can make the chain self-sustaining: a
permissionless exchange that can be a price *leader* rather than a follower,
settling native and wrapped assets under decentralised control, without the
centralised choke points that have repeatedly failed the ecosystem. This TIP
frames that system as forward research, fixes the decisions already reached, and
scopes the work into specifiable pieces.

## Specification

This is an Informational TIP. The constraints below are written for the authors
of the future Standards-Track TIPs the pillars map to; RFC-2119 keywords are used
only for **hard safety and scope boundaries** that those TIPs must not violate.
Concrete wire/TLV/opcode formats, validation rules, and reject codes are
deferred to those TIPs. Each pillar states the problem, the proposed design, the
protocol surface it touches, its maturity, and the future TIP that is to specify
it.

### 0. Design constraints for future TIPs

- **C1 — native-coin economics.** Fees, bonds, burns, and slashable deposits MUST
  be denominated in the native coin. Bridged or wrapped assets MUST NOT pay for
  participation.
- **C2 — L1-derived cadence.** Epochs, beacons, admission windows, and rotation
  schedules should be derived from L1 chain state (height and header hashes) so
  the protocol has no clock of its own and every node computes the same schedule
  from the L1 it already follows.
- **C3 — finalised randomness.** Randomness for admission, cohort sampling, and
  per-user validator assignment MUST be anchored to a *finalised* (sufficiently
  deep) L1 header, never the current tip, so an L1 block producer cannot bias it
  by withholding or grinding a single block (see Security considerations).
- **C4 — L1 verifies commitments, not internals.** L1 MUST NOT be required to
  verify the L1.5's internal cryptography (BFT votes, ordering proofs, threshold
  signatures). L1 anchors validator lifecycle and bond state, periodic
  commitments, data-availability and forced-exit guarantees, and wrapped-asset
  mint/burn; the rest is verified by the cohort and by independent watchers
  against those commitments.
- **C5 — no native foreign custody in the base design.** The base protocol MUST
  NOT make validator keys directly custody foreign chains, and MUST NOT take a
  dependency on native threshold ECDSA. Non-TSC assets enter as wrapped
  TensorCash assets via treasury / wrapper bridges maintained separately from the
  core validator key scheme (pillar 6, Rationale R5).
- **C6 — soft-fork-oriented encoding.** A rollout intended to be soft-fork-like
  should encode new meaning as *stricter validation over transaction forms old
  nodes already accept* — tx-version or witness-program encodings, commitments
  carried in already-valid script forms, height-gated or versionbits-signalled
  additional checks. It should avoid new block-header fields and new output TLV
  families that legacy nodes reject (the reference node rejects unknown extension
  TLV types, which would push the design to a hard fork). This is a constraint,
  not a guarantee: a design that ignores it can still ship, but as a hard fork.

### 1. Bonded proof-of-work admission with a self-targeting cohort size

**Problem.** Select a candidate pool from a permissionless population in
proportion to real work, deterministically at each epoch boundary, with bounded
L1 load and without the difficulty cliffs a "race to register, fixed term, all
expire together" scheme produces.

**Proposed design.**

- Admission is an ancillary protocol over the L1 cadence. Each epoch is a fixed
  L1 block span; the **epoch beacon** is a hash over one or more finalised L1
  headers (C3).
- A candidate first registers an identity and posts a slashable **bond** in the
  native coin, then grinds ticket attempts during the epoch. A ticket is
  admissible only if `H(beacon ‖ epoch_id ‖ candidate_pubkey ‖ nonce) < T_e`,
  domain-separated by `epoch_id` so a solution cannot be replayed across epochs.
- **Commit then reveal.** A candidate commits `C = H(candidate_pubkey ‖ nonce)`
  before the commit deadline and reveals `nonce` before the reveal deadline; the
  ticket scores only if the revealed value clears `T_e` and matches the commit.
  This denies a late entrant the ability to observe others' tickets before
  choosing whether to reveal (anti-snipe).
- **Bounded load.** Each reveal carries a small per-submission fee/burn in the
  native coin in addition to the bond; the admissibility threshold `T_e` is tuned
  to an **expected-share target** so the number of admissible tickets per epoch
  stays manageable for L1. Fee plus threshold together bound chain load
  regardless of total hashpower.
- **Selection.** At the epoch boundary the **lowest admissible tickets**
  (a global order statistic across all submissions) deterministically fill the
  eligible pool toward the target cohort size. A participant MAY submit under as
  many identities as it wishes: because the global minimum over all trials is
  owned with probability proportional to the number of trials, identity splitting
  faithfully *represents* hashpower rather than gaming the result, and the bond +
  per-submission fee — not an identity cap — is what limits spam (Rationale R1).
- **Self-targeting size.** `T_e` is retargeted on the observed admissible-ticket
  count and the count of outstanding members, expressed as a quantile so cohort
  size moves smoothly: admission is easy below the lower band and effectively
  impossible above the upper band.
- The bond is slashable for objectively-checkable misbehaviour defined by later
  pillars (ordering equivocation, refusing to contribute signing material,
  invalid commitments, failing forced-exit duties).

**Surface touched.** New admission record type and validation; commit-reveal
windows; a difficulty/threshold controller; economic parameters. **Maturity.**
Near-term; this is the foundation everything else depends on. **Future TIP.**
Standards-Track: "Bonded validator admission and cohort difficulty control."

### 2. Liveness as a proof-of-service membership property

**Problem.** Winning a ticket once must not buy a full-term seat regardless of
behaviour, and a fixed term cliffs the cohort. The onus for liveness should be on
the incentivised member to participate automatically; non-participants should
lose standing — without punishing honest peers briefly isolated by a partition or
eclipse.

**Proposed design.**

- Tickets (§1) admit identities into an **eligible pool**; the **active cohort**
  is selected from that pool by a rolling, low-pass **service-health score**
  computed on L1 from validator receipts and objective failures. Because the
  score is a filtered time series with a quantile activation threshold, a surge of
  admissions raises the bar smoothly and weak participants fall below it
  gradually — no synchronised expiry, hence no cliff (Rationale R2).
- The score is fed by verifiable **service artifacts** that are exactly the duties
  a member must already perform: state-root commitments (pillar 3), availability
  responses (pillar 3), nonce commitments for committee signing (pillar 4),
  forced-exit responsiveness (pillar 3), and bridge attestations where applicable
  (pillar 6). Nonce material counts only when *consumed* by a real signature, so
  it cannot be faked by posting unused material.
- A new admission MAY **displace a non-participating incumbent**: an admission
  opens a short challenge window in which incumbents must produce a fresh service
  artifact. To stay partition/eclipse-safe this MUST include (i) a grace fraction
  of tolerated misses within a long scoring window, (ii) challenge selection
  anchored to a finalised beacon (C3) so it cannot be pre-computed, and (iii)
  cross-member corroboration (a member's receipts reference work also witnessed by
  others in the same per-user subset) so fabricated receipts are detectable even
  under partition (Security considerations).
- Score decay is demotion (loss of voting/signing rights); only an objective
  fault slashes the bond.

**Surface touched.** Consensus-tracked per-member score; receipt record formats;
challenge protocol; interaction with §1 difficulty and pillars 3–4. **Maturity.**
Near-term in shape, but the precise score function and corroboration rules are
the least settled — expect iteration. **Future TIP.** Standards-Track:
"Proof-of-service membership and liveness scoring."

### 3. State commitments, data availability, and forced exits

**Problem.** If the L1.5 custodies meaningful value, users must be able to recover
it even if sequencers censor them, validators stall, part of the cohort
disappears, or the market layer halts. Without this the system is just a trusted
exchange with rotating operators. This is mandatory, not optional, and is more
foundational than the privacy layer.

**Proposed design.**

- The cohort periodically posts a **compact state-root commitment** to L1 (market
  state / balances accumulator + epoch metadata), carried in an already-valid
  script form per C6.
- A **data-availability discipline** MUST be sufficient to reconstruct user
  balances and claims from published data, so that a user (or any watcher) can
  prove their entitlement against a committed root without the cooperation of a
  live sequencer.
- A **forced-exit path** back to L1 settlement MUST exist: a user who is censored
  or stalled can present a proof of entitlement against the latest committed,
  available state root and withdraw to L1 after a challenge delay, independent of
  the current proposer. Forced-exit responsiveness is itself a scored service
  artifact (pillar 2) and a slashable duty.
- Where the design admits fraud/invalid-state evidence, an invalid commitment that
  a member signs must be objectively provable and slashable.

**Surface touched.** Commitment record format; availability rules; forced-exit
transaction and challenge mechanics; dispute/fraud-evidence path; slashing.
**Maturity.** Near-term and high-priority — it is the trust-minimisation backbone
for everything that holds value. **Future TIP.** Standards-Track: "L1.5 state
commitments, data availability, and forced exits."

### 4. Native committee key and threshold settlement

**Problem.** The active cohort must collectively authorise settlement and
wrapped-asset mint/burn under threshold control, with rotation as the cohort
churns, and without contorting base consensus around foreign-chain signatures.

**Proposed design.**

- The committee key is **Schnorr-native on TensorCash** (secp256k1, BIP-340
  style), threshold around three-fifths of the committee (≈60% of *K*). It
  authorises L1.5 settlement hooks, bond reclaim, and wrapped-asset mint/burn — it
  does **not** custody foreign chains (C5).
- **Practical rollout, not blocked on the most ambitious variant.** A first usable
  release MAY use simpler committee key management; a non-interactive threshold
  DKG and proactive resharing are a later hardening once the validator market is
  stable (Rationale R3). When a DKG is used, commitments and per-recipient
  encrypted-share roots are posted on-chain, bulk share ciphertext served
  off-chain bound by the on-chain root so misbehaviour is objectively provable;
  PVSS (post-and-prove, no complaint rounds) is preferred at small cohort sizes.
- **Rotation.** Membership changes are batched to epoch boundaries to bound key
  churn; on-chain bond reclaim is tied to a member cooperating in rotation. A
  rotation either refreshes shares for the unchanged key or moves authority to a
  successor key. Critical settlement paths MUST carry a timelocked fallback so a
  stalled epoch cannot permanently freeze funds — and note the forced-exit path
  (pillar 3) is the ultimate backstop, independent of the committee key.
- **Liveness coupling.** The nonce commitments members emit for liveness (pillar
  2) are the material consumed by threshold signing, so the key stays usable
  precisely while the cohort is live, with no separate keep-alive ceremony.

**Surface touched.** Native threshold-Schnorr acceptance for settlement hooks;
DKG/rotation record types; slashing for invalid shares; child-key derivation for
per-purpose native keys. **Maturity.** Simpler committee management near-term;
threshold-DKG hardening longer-dated. **Future TIP.** Standards-Track: "Committee
key management, threshold signing, and rotation."

### 5. The L1.5 market layer (fair ordering and matching)

**Problem.** Provide permissionless, front-running-resistant trading at high
throughput, with fair sequencing under no central authority, owning matching and
risk while committing state to L1.

**Proposed design.** The intended model is closer to a high-speed semi-off-chain
order book with periodic L1 commitments, strong forced exits, and
validator-funded sequencing than to putting every trade on L1 as a covenant. The
L1.5 owns order admission, sequencing, matching, fills, market state,
liquidations/risk rules, and funding/fee accounting; L1 does not see every order.

- **Sequencing** adopts the *transaction-propagation and fair-ordering* mechanism
  of the TensorCash DEX whitepaper and adapts its anchors to this TIP: each user
  is assigned a rotating, unpredictable subset of the active cohort by hashing the
  user address against a **finalised beacon** (C3) — replacing the whitepaper's
  previous-block assignment; each transaction payload is encrypted and its symmetric
  decryption key is Shamir-split 6-of-10 across the user's assigned validator
  subset (one fragment per validator, at least 6 of the 10 required to
  reconstruct), so members timestamp, sequence, sign, and (after a short delay)
  gossip their fragment receipts before any of them can reconstruct the content,
  making reordering or withholding unprofitable; a
  deterministically-chosen proposer normalises latency by a regression over
  receipts, produces a fair ordering, runs matching, and the block is finalised by
  BFT supermajority with proposer rotation on failure.
- **Service coupling.** The ordering duty *is* the liveness duty (pillar 2); the
  market state it produces is what pillar 3 commits, makes available, and exposes
  to forced exit.

**Surface touched.** Gossip/transport protocol, ordering and matching/risk rules,
and the L1 commitment interface (pillar 3). **Maturity.** The ordering mechanism
is the most developed (whitepaper-level); matching/risk/funding semantics are
longer-dated. **Future TIP(s).** Standards-Track: "Fair-ordering transaction
propagation and sequencing"; and a separate matching/risk/settlement
specification.

### 6. Wrapped-asset issuance, redemption, and the bridge interface

**Problem.** Bring non-TSC assets into the market without validator keys
custodying foreign chains and without a native threshold-ECDSA dependency.

**Proposed design.**

- Non-TSC assets enter the L1.5 as **TensorCash-native wrapped assets**. Treasury
  / wrapper operators or bridge modules — maintained separately from the core
  validator key scheme — lock or custody the external asset; TensorCash mints
  corresponding wrapped units; the L1.5 trades those wrapped units; redemption
  burns wrapped units and releases the external asset on the bridge side.
- L1 sees wrapped-asset **mint / burn / treasury events**; the validator committee
  authority (pillar 4) gates mint/burn against bridge attestations, but does not
  itself hold foreign-chain keys. The bridge's own custody model (federated,
  MPC-on-the-bridge-side, or otherwise) is out of scope here and is its own design
  surface.
- **External finality is probabilistic, and is the dominant bridge risk.** Because
  a source chain (BTC, ETH, or another) can reorganise, wrapped-asset minting MUST
  observe a **chain-specific finality horizon** (a per-source confirmation depth or
  finality-gadget condition) before a deposit is treated as final, MUST handle
  source-chain reorgs by deferring or reversing any mint that has not yet crossed
  that horizon, and SHOULD treat redemption as unsettled until the external release
  is final. The finality horizon — not the committee threshold — is
  the dominant safety parameter of each wrapper, and each bridge integration states
  its own.
- Native TSC and TensorCash-native assets remain first-class L1 assets and need no
  wrapping.

**Surface touched.** Wrapped-asset mint/burn records; treasury/bridge attestation
interface; committee authorisation of issuance. **Maturity.** Longer-dated;
follows the validator market and anchors. **Future TIP.** Standards-Track:
"Wrapped-asset issuance/redemption and the treasury bridge interface."

### 7. Deposit pools and epoch-rotated pseudonym privacy

**Problem.** Hide the public mapping between depositors and traders without
pretending to give validators zero knowledge.

**Proposed design.** Privacy is public-facing, not validator-facing — validators
will usually know more than the public, and the target is that *the public* sees
only pooled, rotating, batched state.

- Users deposit into **shared pool addresses** for an asset and time bucket, then
  submit **private claims** to validators; validators credit balances to
  **pseudonyms that are fresh for the epoch**; at rotation, balances are carried
  privately to new pseudonyms for the next epoch.
- This deliberately avoids the one-deposit-to-one-public-pseudonym design, which
  is privacy theatre because timing and amount correlation re-identify the owner.
  The anonymity set is the pool and the time bucket; old epoch mappings MAY be
  revealed later if the protocol requires auditability.
- The masking is temporal and set-bounded, not a cryptographic unlinkability
  guarantee; a thin pool gives thin privacy and this must be stated honestly to
  users (Security considerations).

**Surface touched.** Pool-deposit and private-claim records; per-epoch pseudonym
crediting; rotation carry-over; optional delayed revelation. **Maturity.**
Longer-dated; the last hardening layer. **Future TIP.** Standards-Track:
"Pooled deposits, rotating pseudonyms, and withdrawal privacy."

### Suggested specification order

The pillars have a natural dependency order, which is also the order in which the
child Standards-Track TIPs should be written: (1) bonded admission and cohort
difficulty; (2)/(3) proof-of-service membership together with state commitments,
data availability, and forced exits; (5) fair-ordering propagation and
sequencing; (6) wrapped-asset issuance and the treasury bridge; (4)
threshold-key hardening; (7) pooled-deposit privacy and rotation. Admission and
the forced-exit/availability backbone are foundational; threshold hardening and
privacy are deliberately last so the market is not blocked on the most ambitious
cryptography.

### Open engineering questions

These are recorded deliberately and each must be resolved before the relevant
Standards-Track TIP can leave Draft:

1. **Scope of consensus change.** How much of admission, bond/slashing, score
   tracking, commitments, and a native threshold-Schnorr acceptance rule belongs
   in L1 consensus versus the L1.5 layer with L1 carrying only commitments. The
   minimal-surgery position is that L1 owns identity (admission + bond), commitment
   anchoring, forced-exit settlement, and wrapped-asset mint/burn, and nothing
   more (C4).
2. **Wrapped-asset bridge custody model.** Pillar 6 fixes that foreign assets are
   wrapped via treasury/wrapper bridges, but the bridge's own custody (federated
   signer, MPC, optimistic, etc.) and its attestation/dispute interface to the
   committee are unresolved and are the main external-trust surface.
3. **Codebase for high-throughput L1.5.** Whether the throughput layer (QUIC
   point-to-point routing, gossip fan-out, fragment reassembly, the latency
   regression and matching/risk engine) extends the reference node or is a
   separate implementation that consumes L1 state and posts commitments back. The
   admission, bond/slash, commitment-verification, and forced-exit logic plausibly
   live in the L1 node (repo-relative `src/...`); the matching engine plausibly
   does not.
4. **Soft-fork vs. hard-fork activation.** Whether the admission and commitment
   surface can be encoded entirely within C6's constraints (tx-version /
   witness-program forms, height-gated checks) or whether any pillar genuinely
   needs a header/encoding change and therefore a hard fork.

## Rationale

- **R1 — global lowest-ticket, not per-identity minimum.** A scheme where each
  identity submits its single lowest hash and the network takes the top-k is
  unsound in permissionless PoW: one can verify a `(nonce, hash)` pair but cannot
  prove it was that identity's *lowest* (one cannot prove a negative), so
  withholding a better hash is undetectable; and capping one seat per identity
  rewards splitting hashpower across identities. Taking the lowest tickets across
  *all* submissions removes both problems — there is nothing to withhold
  profitably, and identity splitting becomes the intended way to represent
  hashpower. The bond and per-submission fee, not an identity cap, bound spam.
  This is the Bobtail/FruitChains family of low-variance, work-proportional
  selection results.
- **R2 — health score over fixed TTL.** A fixed licence term expires the cohort on
  a schedule, cliffing difficulty and failing to compel participation between
  admission and expiry. A low-pass service score with a quantile activation
  threshold turns admission into smooth pressure and makes liveness the thing that
  *keeps* a seat — the property actually wanted.
- **R3 — simpler committee key first, threshold hardening later.** The
  validator-market design must not be blocked on the most ambitious
  threshold-cryptography variant. A first usable release can use simpler committee
  management and add a non-interactive DKG / proactive resharing once the market is
  stable; PVSS-first then complaint-based DKG as the cohort grows.
- **R4 — forced exits before privacy.** A custody layer's first obligation is that
  users can always leave; an exchange with rotating operators and no exit is just a
  trusted exchange. Committed state + data availability + forced exit are therefore
  prioritised above the privacy shield, reversing a tempting but wrong ordering.
- **R5 — no native foreign custody.** Making validator keys directly sign foreign
  chains would force a native threshold-ECDSA dependency and entangle base
  consensus with external signature systems for marginal benefit. Wrapping foreign
  assets through a separate treasury/bridge keeps the core validator key
  Schnorr-native and the base protocol simple; the bridge can be hardened
  independently. This is a deliberate scope exclusion, not an oversight.
- **R6 — soft-fork-oriented encoding.** Encoding new meaning as stricter
  validation over already-valid transaction forms (and height-gated or versionbits
  activation, both of which the reference node already supports) preserves the
  option of a soft-fork-style rollout; introducing new header fields or extension
  TLV families that legacy nodes reject would force a hard fork.
- **R7 — what is deliberately excluded.** A fork-choice "penalty for omission"
  cannot work under pure most-work fork choice — the heavier chain that censors a
  challenge wins and the network forgets the fork carrying it — and would need a
  separate gossiped-challenge-plus-witness overlay and a fork-choice change that is
  its own research programme, not part of this family. Defending against a
  privately-mined long chain on a bad subject is likewise an L1 fork-choice
  question. Recording these stops a future contributor re-deriving the dead ends.

## Backwards compatibility

This TIP is Informational and changes no consensus rule, so nodes are unaffected
by the document itself; each Standards-Track child carries its own analysis. By
construction (C4, C6) the layer-1 is asked to carry additional commitment and
lifecycle records encoded, where possible, as stricter validation over
already-valid transaction forms — height-gated or versionbits-signalled — so that
a soft-fork-style activation remains possible and a non-participating node
continues to validate L1 as before. Existing assets, scripts, and the
proof-of-inference consensus are untouched by the L1.5 layer. External chains
require no changes: they interact only with treasury/wrapper bridges, which
present ordinary addresses and transactions (C5).

## Activation parameters

N/A. This TIP mandates no consensus rule of its own. Each Standards-Track child
MUST define its own activation per TIP-0001. The admission, bond/slashing,
commitment, forced-exit, native threshold-Schnorr acceptance, and wrapped-asset
rules are consensus changes and will each require an activation mechanism
(height-gate or versionbits, both already supported by the reference node), a
minimum node version, an explicit (normally non-rollback) statement, and a
monitoring/rollout plan — and should respect the soft-fork encoding constraints in
C6 / R6 where a soft-fork-style activation is intended.

## Reference implementation

TBD — Draft. No reference implementation is proposed by this Informational TIP;
implementations attach to the individual Standards-Track children as those reach
Proposed.

## Security considerations

- **Beacon bias and grinding.** Anchoring admission, cohort sampling, and
  challenge selection to a finalised, deep L1 header (C3) — not the current tip —
  denies an L1 block producer the ability to bias the cohort by withholding or
  grinding one block; a look-back over several headers and/or a verifiable delay
  raises the cost of any residual influence.
- **Sybil and capital capture.** Admission cost is *work* plus a small uniform bond
  and per-submission fee (R1); the bond must stay small enough that the mechanism
  does not become proof-of-stake by accident, yet large enough to rate-limit
  identity spam and fund slashing. Selection probability tracking hashpower is the
  security argument, so identity multiplicity is expected, not a vulnerability.
- **Forced exit is the backstop.** The committee key, the sequencer, and the
  privacy layer can all fail; the committed-state + data-availability + forced-exit
  path (pillar 3) is what prevents any of those failures from becoming loss of user
  funds, and is therefore the most scrutinised surface. Data unavailability is the
  central attack: if committed state cannot be reconstructed, forced exit is
  hollow, so the availability discipline and the penalties for withholding it are
  load-bearing.
- **Liveness vs. partition/eclipse.** Displacement and challenge rules (pillar 2)
  must not eject honest members merely isolated briefly. Mitigations: long scoring
  windows, an explicit grace fraction, unpredictable finalised-beacon challenge
  selection, and cross-member corroboration so fabricated receipts are detectable
  even within a partition; eclipse is bounded by diverse, regularly-randomised
  peering.
- **Committee key safety.** No member holds the whole secret; misbehaving dealers
  are convicted from on-chain commitments and slashed; rotation batched to epoch
  boundaries bounds the partially-rotated window; a stalled epoch must never freeze
  funds, with timelocked fallback and, ultimately, forced exit.
- **Bridge trust and finality.** Wrapped assets are only as safe as the
  treasury/bridge that custodies the external asset (open question 2); the
  committee gates mint/burn but does not remove bridge custody risk. The sharpest
  technical failure mode is **probabilistic source-chain finality**: a mint issued
  against a deposit that is later reorganised away creates unbacked wrapped supply.
  Chain-specific finality horizons and reorg-aware, delayed mint/burn settlement
  (pillar 6) are the mitigation; choosing the horizon too shallow trades safety for
  latency, and this must be communicated as the external-trust surface it is.
- **Privacy limits.** Pooled-deposit / rotating-pseudonym privacy is public-facing,
  temporal, and set-bounded: the anonymity set is the pool and time bucket, not a
  cryptographic guarantee, validators are assumed to know more, and timing/amount
  correlation across deposit and withdrawal remains a residual risk.
- **No live-network disclosure.** Everything here is forward design. Per TIP-0001
  and `../SECURITY.md`, any consensus weakness in the *live* network is handled by
  coordinated disclosure and only specified as a TIP once disclosure is safe;
  nothing here describes an exploitable condition in a deployed network.

## Test vectors

N/A for this Informational TIP. Each Standards-Track child will carry golden
vectors where applicable — admission hash/threshold and commit-reveal vectors,
state-commitment and forced-exit vectors, committee-key derivation vectors, and
ordering/commitment vectors.

## References

- TensorCash DEX whitepaper, *Decentralised Exchange Protocols* (the
  fragment-gossip fair-ordering and sequencing mechanism adapted in pillar 5).
- TIP-0001 (TIP purpose and guidelines); TIP-0002 (scalar settlement), for the
  on-chain market settlement surface this family can interoperate with.
- Prior art referenced in the Rationale: Shamir secret sharing; Feldman/Pedersen
  verifiable secret sharing and publicly verifiable secret sharing; FROST
  threshold Schnorr and MuSig2; BIP-340/341 (Schnorr/Taproot); the Bobtail and
  FruitChains lines of work on low-variance, hashpower-proportional selection;
  rollup-style data-availability and forced-exit constructions; BIP-9
  versionbits-style activation.

## Copyright

This document is released into the public domain (CC0).
