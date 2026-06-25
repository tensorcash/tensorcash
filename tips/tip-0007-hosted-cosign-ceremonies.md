```
TIP: 0007
Title: Hosted multi-tenant cosign and watch-only spot/adaptor ceremonies
Author: takakuni <takakuni@tensorcash.org>
Type: Standards Track
Status: Draft
Created: 2026-06-25
```

## Abstract

The cosign bridge, the Nostr bulletin board, and the contract-settlement
ceremonies (spot atomic swaps, the Schnorr adaptor-signature reveal protocol,
encrypted-payload commitment proofs) were designed for a single desktop node
that holds both the trading identity and the signing keys. That shape admits
exactly one identity per node and assumes the node can sign, which prevents a
hosted, non-custodial operator from serving many independent users from shared
node infrastructure. This proposal specifies two backward-compatible extensions
that remove those assumptions without changing any consensus rule. First, a
**tenant namespace**: every bulletin-board and cosign command carries an opaque
`tenant_id`, the bridge maintains a distinct Nostr identity and state store per
tenant, ownership/attestation proofs bind to the per-tenant identity, and a
hosted deployment authenticates the caller and authorises the requested tenant.
The absence of a tenant defaults to a single reserved tenant, so existing desktop
nodes are unaffected. Second, a **watch-only external-signer split**: a parallel
family of ceremony RPCs performs only public work — PSBT manipulation, policy
enforcement, sighash computation, and the lock-step reveal check — while every
private-key operation is delegated to an external signer (the user's client), and
all ceremony state travels in PSBT proprietary fields or explicit parameters
rather than node-resident wallet memory. The on-chain artefact is byte-identical
to the desktop path, so the two interoperate in the same ceremony. This TIP fixes
the tenant addressing, the proprietary-field layout, the BIP-322 construction,
the watch-only RPC schemas, and the reject codes for single-signer spot swaps; it
mandates no consensus change.

## Motivation

The contract layer's coordination plumbing — the cosign bridge that relays
ceremony messages over Nostr, the bulletin board that publishes tradable offers,
and the multi-round adaptor-signature ceremony that settles a swap atomically —
currently assumes a one-node, one-identity, key-holding deployment. Three
assumptions are load-bearing and each blocks hosting:

1. **One identity per node.** Bulletin-board ownership proofs embed the holder's
   Nostr public key (a proof of the form `TENSORCASH_HOLDER:{proposal_id}:{pubkey}`).
   The bridge holds a single Nostr keypair, so every user served by one node
   would share one identity and proof-of-ownership would be meaningless: any user
   could claim another's offer.

2. **The node signs.** The ceremony RPCs call into wallet key storage to derive
   keys, generate nonces, and produce partial signatures, and they keep ephemeral
   ceremony secrets in node-resident wallet memory. A hosted operator that ran
   these RPCs would have to hold user signing keys — making it custodial, which is
   precisely the trust model a non-custodial offering must avoid.

3. **State is node-local and singular.** Cosign session state is in memory, and
   per-identity key material and replay/dedup stores live on the node's local
   filesystem. A naive horizontally-scaled deployment that load-balanced a user
   across nodes would fork the user's identity and lose session state mid-ceremony.

The desktop (Qt) wallet works precisely because all three hold for one local
user. To let a hosted, non-custodial provider expose the same offers, swaps, and
ceremonies through a thin web or mobile client — with keys held only by the user
and never transmitted to the server — the protocol must (a) namespace identity
and state per user, (b) split each key-touching ceremony step into a public part
the operator runs and a private part the client runs, and (c) define how a tenant
binds to a node so identity and session state stay coherent. None of this changes
what lands on chain; it changes *where the private keys live* and *how many
identities one node can host*. The desktop path and the hosted path MUST remain
interoperable so a desktop user and a hosted user can be the two counterparties
of the same swap.

## Specification

The keywords MUST, MUST NOT, SHOULD, and MAY are to be interpreted as in
RFC 2119. This TIP defines off-chain coordination and wallet-interface protocol;
it introduces no new consensus rule, opcode, or on-chain encoding. Code is cited
repo-relative to the node tree (`src/...`).

**Scope.** This TIP specifies the hosted/multi-tenant substrate (tenant
namespace, external-signer split, cosign session affinity) and applies it
concretely to single-signer (`n = 1`) Taproot key-path **spot atomic swaps** and
their adaptor-signature settlement. Multi-party signing (MuSig2, `n ≥ 2`) and
other contract families (e.g. lending, repo, governance, asset issuance) are out
of scope here. They reuse the same bulletin board, cosign relay, external-signer
split, and proprietary-field machinery and are to be specified in future
Standards-Track TIPs; the proprietary-field layout (§4) reserves the multi-party
key suffixes so the `n = 1` layout is forward-compatible with the `n ≥ 2`
extension.

### 1. Terminology and roles

- **Tenant** — an independently identified user of a deployment. On a desktop
  node there is exactly one tenant. On a hosted deployment there is one tenant per
  served user wallet.
- **Cosign bridge** — the node-side component that maintains Nostr identities,
  publishes/reads the bulletin board, and relays encrypted cosign session traffic.
- **External signer** — the holder of the private keys (in the hosted model, the
  user's client: browser or mobile app). The external signer is the only party
  that performs private-key operations.
- **Watch-only node** — a node that holds descriptors and can observe, build, and
  validate transactions for a wallet but does **not** hold its private keys; such
  a wallet reports `ISMINE_WATCH_ONLY` for its own addresses.
- **Ceremony** — the multi-round protocol that settles a contract (here, a spot
  atomic swap) between two counterparties, carried over a cosign session, with the
  PSBT as the canonical state carrier.

### 2. Tenant namespace

#### 2.1 Tenant identifier

A `tenant_id` is an opaque, case-sensitive ASCII string identifying a tenant
within one deployment. It:

- MUST match `^[A-Za-z0-9._-]{1,128}$` (no path separators, no whitespace, no
  control characters); a value failing this check MUST be rejected with
  `tenant-id-invalid`.
- MUST be stable for the life of the wallet it identifies and globally unique
  within the deployment, so that it never collides across users, chains, or
  wallet instances.
- MUST be treated as opaque by the bridge and node: no semantics are derived from
  its structure. The hosting layer is responsible for minting a unique, stable
  label per served wallet and for keeping the mapping from end user to
  `tenant_id` outside this protocol.

The reserved value `default` denotes the single implicit tenant. A command that
omits `tenant_id` MUST be processed as if `tenant_id = "default"`.

#### 2.2 Caller authentication and tenant authorisation

The `tenant_id` parameter selects *which* identity acts; it is not, by itself,
proof that the caller is entitled to act as that identity. A multi-tenant
deployment therefore:

- MUST authenticate the caller before forwarding any bulletin-board, cosign,
  spot, or adaptor command to the bridge or node.
- MUST authorise the requested `tenant_id` against the authenticated caller and
  MUST reject, with `tenant-unauthorized`, any command whose `tenant_id` the
  caller is not entitled to use. A caller MUST NOT be able to act as, read the
  state of, or publish under a tenant it does not own.
- SHOULD derive or look up the `tenant_id` server-side from the authenticated
  principal rather than trusting a client-supplied value verbatim.

A single-tenant (desktop) deployment that exposes the bridge only to its local
operator MAY treat all access as the `default` tenant and omit this step.

#### 2.3 Per-tenant identity and state

Within the cosign bridge:

- Each tenant MUST own a distinct Nostr keypair. The first reference to a
  previously-unseen tenant MUST cause the bridge to generate and persist a fresh
  keypair for it. Keypairs MUST be isolated per tenant such that one tenant can
  neither read nor sign with another tenant's key.
- Each tenant MUST own distinct, namespaced state stores (private-payload cache,
  governance/replay-dedup store, session state). State for one tenant MUST NOT be
  visible to another.
- Ownership and attestation proofs (§5) MUST bind to the requesting tenant's
  Nostr public key, so that a proof produced for one tenant cannot be replayed to
  claim an entity owned by another.

#### 2.4 RPC and bridge plumbing

- Every bulletin-board-facing and cosign-facing RPC (`src/rpc/cosign.cpp`:
  `init_bb`, `post_offer`, `list_offers`, `request_trade`, `list_requests`,
  `accept_request`, the governance commands, and the session RPCs `init`, `join`,
  `send`, `recv`, `handshake`, `attest`, `status`) MUST accept an optional
  `tenant_id` parameter defaulting to `"default"`, and MUST forward it to the
  bridge.
- The bridge command interface MUST route every command to the addressed
  tenant's identity and stores.
- The desktop wallet MAY omit `tenant_id` entirely; doing so selects the
  `default` tenant and preserves its existing single-identity behaviour
  unchanged.

#### 2.5 Offer/request ownership

Because the publication identity is now per tenant, a deployment that fronts the
bridge for many tenants MUST be able to answer "which tenant owns this offer or
request" authoritatively rather than by inspection of shared state. A deployment
SHOULD maintain a durable ownership map:

```
ownership(entity_id) -> { entity_type ∈ {offer, request}, tenant_id, created_at }
```

This map MUST be consulted to annotate listings with an `is_mine` flag for the
requesting tenant and MUST be consulted to authorise `accept_request` (a tenant
MUST NOT accept a request against an offer it does not own; a violation MUST be
rejected with `ownership-violation`). The map MUST be durable across process
restarts and MUST be reachable from every node instance permitted to serve the
owning tenant (see §8).

### 3. Watch-only external-signer split

#### 3.1 Invariant

A hosted deployment MUST NOT require the node to hold a tenant's signing keys.
For every ceremony step that, in the desktop path, derives a key, generates a
nonce, or produces a signature, this TIP defines a watch-only variant in which:

- the node performs only **public** work: decoding/validating/mutating the PSBT,
  computing sighashes, enforcing policy guards, public-metadata bookkeeping, and
  the lock-step reveal verification (§7); and
- the external signer performs **all** private-key work and feeds results back as
  explicit parameters or PSBT proprietary fields.

The watch-only variants MUST NOT store wallet signing secrets, nonce secrets,
tweaked private keys, or per-input ephemeral signing state in node-resident wallet
memory; that material lives only with the external signer. All ceremony state that
survives between calls MUST be carried in the PSBT (§4) or supplied as explicit
parameters. The protocol-level adaptor scalar is the one exception and is **not**
a wallet signing key: it is generated and persisted by the node in the contract
record as specified in §3.4.

#### 3.2 Ownership-check relaxation

Ceremony RPCs that establish a leg (offer proposal and acceptance) check that the
receive address is spendable by the wallet. The watch-only variants MUST accept
an address the wallet *knows* even if it cannot spend it: the ownership predicate
MUST be relaxed from `ISMINE_SPENDABLE` to `ISMINE_WATCH_ONLY | ISMINE_SPENDABLE`.
A request for an address the wallet does not know at all MUST still be rejected
with `ismine-unknown-address`.

#### 3.3 The watch-only RPC family (overview)

The following variants are defined in `src/wallet/rpc/`. Each mirrors its
desktop counterpart's validation and output but sources private material
externally. Implementations MUST keep the public validation logic (PSBT
structure checks, sighash derivation, signature verification, commit-reveal
enforcement, policy guards) identical between the desktop and watch-only paths so
that the two produce byte-identical witnesses. Normative schemas follow in §3.5.

| RPC | Public work the node performs | Private work delegated to the external signer |
|-----|-------------------------------|-----------------------------------------------|
| `spot.propose_wo` / `spot.accept_wo` | Term parsing, offer registration, relaxed ownership check (§3.2); persists the protocol-level adaptor scalar in the contract record | None (no signing) |
| `spot.add_commitment_proof_wo` | Validates PSBT/offer/asset-output consistency; embeds a client-supplied commitment hash in an `OP_RETURN` output | Decrypts the encrypted payload and computes the commitment hash (§6) |
| `adaptor.prepare_wo` | Fills the PSBT (no signing), validates inputs are Taproot, writes the adaptor point into proprietary fields, enforces policy guards, returns per-input signing tasks | None yet (produces no signature) |
| `adaptor.inject_presig` | Pure PSBT manipulation: writes a client-produced adaptor pre-signature and the derived commitment into proprietary fields | Produces the adaptor pre-signature |
| `adaptor.commit_final_wo` | Computes `H(final_sig)` from supplied or contract-record secrets, with mandatory adaptor-secret normalisation (§4.3) | Optionally supplies the adaptor secret if not held in the contract record |
| `adaptor.complete_wo` | Recomputes sighash/keys from the PSBT, applies `s = s' + t`, verifies the final signature, enforces the lock-step reveal check (§7), writes the witness | Supplies the adaptor secret (or it is read from the contract record) |

The read-only encrypted-payload fetch (§6), the BIP-322 message-signing path
(§5), and the standard `combinepsbt`/`finalizepsbt` paths complete the set; none
require node-held keys.

#### 3.4 Adaptor-secret custody

The adaptor secret is the protocol-level shared scalar of the adaptor point — it
is **not** a wallet signing key. `spot.propose_wo` / `spot.accept_wo` generate it
and persist it in the contract record on the node. `adaptor.commit_final_wo` and
`adaptor.complete_wo` MUST, when the caller does not supply a secret for a given
input, read it from the contract record (`SpotOfferRecord` /
`SpotAcceptanceRecord`) via the offer identifier. This keeps contract-state
management on the node and avoids exposing the adaptor secret to the client in
the common case. Implementations MUST cleanse any secret material from memory
after use.

#### 3.5 Normative RPC schemas

All 32-byte values are lower-case hex unless stated; x-only public keys are
32-byte BIP-340 x-only encodings; signatures are BIP-340 (Schnorr) for key-path
spends. `psbt` is base64.

```
adaptor.prepare_wo
  params: { psbt }
  result: {
    psbt,                       # adaptor points written to proprietary fields
    signing_tasks: [ {
      input_index,              # int
      message_digest,           # 32-byte sighash
      adaptor_point,            # 32-byte x-only T
      sighash_type,             # int (0 = SIGHASH_DEFAULT)
      is_keypath,               # bool
      tap_internal_key,         # 32-byte x-only
      tap_output_key            # 32-byte x-only (P, the verification key)
    } ]
  }
  node MUST NOT generate a nonce or call any key-deriving routine.

adaptor.inject_presig                          # pure PSBT manipulation, n=1
  params: {
    psbt,
    input_index,                # int, MUST be a Taproot input
    pre_sig,                    # 64 bytes (R' || s')
    nonce_parity,              # 0 or 1; MUST be 0 (even Y) — else reject adaptor-nonce-parity
    adaptor_point               # 32-byte x-only T
  }
  effect: write R' to `nonce_pub`, pre_sig to `adaptor_sig`, T to `adaptor_point`,
          and commitment = H_tag("fs/adaptor")(R' || T || P || m) to `commitment`
          (§4.2), all in the input's proprietary fields.
  result: { psbt }

adaptor.commit_final_wo
  params: {
    psbt,
    secrets?: [ { index, pre_sig?, secret } ],  # pre_sig 64B, secret 32B
    offer_id?                                    # contract-record fallback (§3.4)
  }
  effect: per input, resolve secret, normalise it (§4.3), compute
          s_final = s' + t (mod n), final_sig = R' || s_final, then
          commitment = SHA256(final_sig); cleanse secrets.
  result: { commitments: [ { index, commitment } ] }   # 32-byte each

adaptor.complete_wo
  params: {
    psbt,
    peer_commitments: [ { index, commitment } ],        # required, 32-byte each
    secrets?: [ { index, secret } ],
    input_metadata?: [ { index, pre_sig, nonce_parity } ],  # overrides PSBT fields
    offer_id?
  }
  effect: per Taproot input, recompute m, P, is_keypath from the PSBT; read
          pre_sig from `adaptor_sig` (or override) and T from `adaptor_point`;
          resolve+normalise secret; s_final = s' + t; verify final_sig as BIP-340;
          verify SHA256(final_sig) == peer_commitment[index] else reject
          adaptor-commit-reveal-mismatch; write the witness.
  result: { psbt }

spot.add_commitment_proof_wo
  params: {
    psbt,
    id,                         # spot offer id, 32-byte
    commitment_hash,            # 32-byte, SHA256(canonical_text || receive_address)
    canonical_text?             # optional, audit only
  }
  effect: validate PSBT/offer/asset-output consistency (incl. keywrap tag on the
          counterparty output); embed OP_RETURN <commitment_hash>; the node MUST
          NOT verify the preimage (§6).
  result: { psbt }

spot.propose_wo / spot.accept_wo
  params: standard propose/accept terms (legs, amounts, receive addresses,
          require_commitment_proof) plus optional tenant_id.
  effect: as the desktop RPCs, but the ownership check is relaxed per §3.2 and the
          node holds no signing key. The adaptor scalar is generated and persisted
          in the contract record (§3.4) and is NOT returned.
  result: { offer_id, psbt }
```

### 4. PSBT proprietary fields for the adaptor ceremony

All public ceremony state MUST travel in PSBT proprietary key-value pairs so that
the desktop and hosted paths interoperate without shared node memory. A
proprietary entry uses the standard PSBT proprietary key type `0xFC`, encoded as:

```
0xFC | compactsize(len(identifier)) | identifier | compactsize(subtype) | key-suffix    => value
```

For all ceremony fields defined here:

- `identifier` MUST be the two ASCII bytes `fs` (`0x66 0x73`).
- `subtype` MUST be `0`.
- `key-suffix` is the ASCII string naming the field, listed below.

Entries carrying advisory (non-load-bearing) data MAY use the advisory identifier
`x` (`0x78`); a verifying implementation MUST NOT depend on advisory entries.

| Level | Key-suffix | Value |
|-------|------------|-------|
| Input | `nonce_pub` | `R'`, 32-byte x-only public key |
| Input | `adaptor_point` | `T`, 32-byte x-only public key |
| Input | `adaptor_sig` | adaptor pre-signature `R' ‖ s'`, 64 bytes |
| Input | `commitment` | 32-byte per-input ceremony commitment (§4.2) |
| Global | `policy` | serialised contract policy |
| Global | `contract_meta` | serialised contract metadata |
| Output | `is_change` | change-output marker |
| Output | `asset` | per-output asset binding |

The following input key-suffixes are **reserved** for the multi-party (`n ≥ 2`)
extension and MUST NOT be emitted by an `n = 1` implementation, but MUST be left
untouched if present: `musig_pubkeys`, `musig_aggnonce`, `musig_pubnonce/<i>`,
`musig_partial/<i>`. Reserving them now keeps the `n = 1` layout
forward-compatible.

#### 4.1 Nonce parity

The nonce point `R'` MUST have even Y. `adaptor.inject_presig` MUST reject a
pre-signature whose declared `nonce_parity` is odd (`!= 0`) with
`adaptor-nonce-parity`. An external signer that derives an odd-Y nonce MUST negate
its nonce scalar and recompute, matching the node's convention, before submitting.

#### 4.2 Reveal commitment

The per-input ceremony commitment written at `inject_presig` time MUST be the
tagged hash `commitment = H_tag("fs/adaptor")(R' ‖ T ‖ P ‖ m)`, where `H_tag` is
the BIP-340-style tagged hash with tag `"fs/adaptor"`, `P` is the input's Taproot
output (verification) key, and `m` is the sighash. This per-input commitment is
distinct from the lock-step *reveal* commitment of §7, which is the plain
`SHA256` of the final signature.

#### 4.3 Mandatory adaptor-secret normalisation

Before computing `s = s' + t`, both the node-side `commit_final_wo`/`complete_wo`
and any client-side helper that holds the secret MUST normalise the adaptor
secret to the even-Y lift of the adaptor point: if `t·G` has odd Y, replace `t`
with `n − t` (the curve-order complement). Omitting this normalisation produces
the wrong final scalar for one parity branch and causes the lock-step commitment
to mismatch. The normalisation rule mirrors the node's
`NormalizeAdaptorSecretToAdaptorX` and MUST be byte-for-byte equivalent across
implementations.

### 5. BIP-322 message signing (ownership proofs and peer attestation)

Bulletin-board ownership proofs (e.g. `TENSORCASH_PROOF:{offer_id}:{role}:{asset_id}`
and the holder binding of §2.3) and cosign peer attestation are BIP-322
signatures over a message string for a wallet address. On a watch-only node these
MUST be produced by the external signer, because the node holds no keys. A
`message_sign` operation therefore takes `(address, message)` and returns the
encoded signature.

The construction MUST match the node's verifier (`src/rpc/bip322.cpp`)
**byte-for-byte**, including the following deviation from the textbook BIP-322:

1. **Message hash.** `message_hash` MUST be the double-SHA256 of the *raw* UTF-8
   message bytes — i.e. `SHA256(SHA256(message))`. The node does **not** apply the
   `"BIP0322-signed-message"` tagged hash and does **not** length-prefix the
   message as a Bitcoin varstring. An interoperating signer MUST replicate this
   untagged double-SHA256; applying the standard tagged hash will produce
   signatures the node rejects.
2. **`to_spend` transaction.** `nVersion = 0`, `nLockTime = 0`; one input with a
   null prevout, `nSequence = 0`, and `scriptSig = OP_0 <message_hash>`; one
   output with `value = 0` and `scriptPubKey` = the address's script.
3. **`to_sign` transaction.** `nVersion = 0`, `nLockTime = 0`; one input spending
   `to_spend` output 0, `nSequence = 0`, carrying the produced `scriptSig`/witness;
   one output with `value = 0` and `scriptPubKey = OP_RETURN`.
4. **Signing.** Sign the `to_sign` input according to the address type: P2TR
   key-path MUST use a BIP-340 Schnorr signature; P2WPKH MUST use an ECDSA witness
   signature. The witness/scriptSig is then serialised and base64-encoded.

The node verifies the result via full script verification with the P2SH, witness,
and taproot flags enabled; an implementation MUST produce signatures that pass
that verifier for the relevant address type.

### 6. Read-only encrypted-payload fetch and client-side commitment

The commitment proof requires the canonical text inside the counterparty's
encrypted asset payload (the ICU keywrap payload), which lives in the coins
database, not in the PSBT. To keep this watch-only:

- A read-only fetch MUST return the ciphertext and its context (asset id,
  commitment hash, script-pubkey hash, KDF salt) **without** attempting
  decryption and without touching private keys. The node already returns the
  ciphertext unconditionally; the watch-only path MUST simply not invoke the
  decryption branch.

```
icu.fetch_payload                              # read-only proxy of geticupayload
  params: { asset_id }                          # 32-byte asset id; no decryption options
  result: {
    ciphertext,                 # hex-encoded encrypted ICU payload
    icu_ctxt_commit,            # hex commitment hash
    spk_hash32?,                # script-pubkey hash, if present
    kdf_salt?,                  # hex, if present
    decrypted: false            # always false on this path
  }
  the node MUST NOT derive a key or attempt decryption.
```

- The external signer MUST perform decryption locally: derive the recipient key,
  unwrap the data-encryption key (ECDH + AEAD), decrypt and decompress the
  payload, parse its canonical TLV form, extract the canonical text, and compute
  `commitment_hash = SHA256(canonical_text ‖ receive_address)`.
- The external signer then calls `spot.add_commitment_proof_wo` with the
  resulting `commitment_hash`. The node MUST validate PSBT/offer/asset-output
  consistency (including that the counterparty output carries the keywrap tag) and
  MUST embed the hash in the contract's `OP_RETURN` output, but MUST NOT attempt
  to verify the hash's preimage. A party that computes the wrong hash harms only
  itself, because the on-chain proof is meaningful only to parties who can decrypt
  the payload.

### 7. Lock-step reveal commitment (node-enforced)

The lock-step reveal protocol prevents a free-option attack in which a party
learns the counterparty's completed signature without revealing its own. It MUST
remain enforced by the node regardless of where keys live:

1. Each party computes `H(final_sig)` for each of its inputs
   (`adaptor.commit_final_wo`), where `final_sig = R' ‖ (s' + t_normalised)` and
   `H = SHA256`.
2. The parties exchange these commitments over the cosign session **before** any
   final signature is revealed.
3. Each party calls `adaptor.complete_wo` supplying the peer's commitments. For
   every input, the node MUST recompute the revealed `final_sig`, verify it as a
   valid BIP-340 signature, and verify `SHA256(final_sig) == peer_commitment`. If
   the commitment does not match, the node MUST reject with
   `adaptor-commit-reveal-mismatch` and MUST NOT write the witness.

A watch-only ceremony MUST NOT offer any path that completes a signature without
this check; skipping it MUST be impossible by construction, not merely by
convention.

### 8. Tenant affinity and state locality

A tenant's Nostr identity, namespaced stores, in-memory cosign session state, and
contract records are node-local. Operations that touch them MUST be pinned to one
node per tenant:

- All bulletin-board, cosign-session, spot, and adaptor operations for a tenant
  MUST be routed to a single designated node ("primary-only"). A deployment MUST
  NOT silently fail these operations over to another node, because doing so would
  present a different Nostr identity and lose session and contract state.
- If the designated node is unavailable, these operations MUST fail explicitly
  (`tenant-node-unavailable`) rather than producing a divergent identity.
- Operations that do not depend on node-local ceremony state (balance, history,
  address derivation, funding) MAY continue to use ordinary redundancy/failover.
- The ownership map of §2.5 MUST be reachable from the designated node; if it is
  stored off-node it MUST be consistent across the deployment.

### 9. Reject codes

The following identifiers are introduced for the RPC/coordination layer (they are
not consensus reject codes and do not appear in block validation):

| Code | Condition |
|------|-----------|
| `tenant-id-invalid` | `tenant_id` fails the §2.1 grammar |
| `tenant-unauthorized` | The authenticated caller is not entitled to the requested `tenant_id` (§2.2) |
| `tenant-node-unavailable` | A pinned per-tenant operation reached a node that cannot serve the tenant (§8) |
| `ownership-violation` | A tenant attempted `accept_request` (or equivalent) against an entity it does not own (§2.5) |
| `ismine-unknown-address` | A ceremony leg referenced an address the wallet does not know (§3.2) |
| `adaptor-nonce-parity` | A submitted adaptor pre-signature declared odd nonce parity (§4.1) |
| `adaptor-commit-reveal-mismatch` | A revealed final signature did not match the peer commitment (§7) |

## Rationale

**Why a tenant string rather than a per-tenant bridge process.** Keying identity
and state by an opaque label inside one bridge keeps the desktop path a special
case (`default`) and avoids a process-per-user deployment. The label is opaque so
that the protocol imposes no policy on how a hosting layer names users; uniqueness
and stability are the only requirements, and authorisation (§2.2) is what binds a
label to an entitled caller.

**Why an external-signer split rather than server-side key custody with
encryption.** Any design in which the node can produce a signature — even from
encrypted-at-rest keys — is custodial during the moment of signing. Splitting the
ceremony so the node only ever sees public material makes "the operator cannot
sign for the user" a structural property, not a key-management promise.

**Why PSBT proprietary fields carry ceremony state, and why this exact layout.**
The desktop path keeps ephemeral ceremony state in wallet memory. For two
differently-deployed parties to run the same ceremony, the state must live in the
artefact they exchange. The PSBT is already the canonical carrier, and the
`fs`/subtype-0/string-suffix layout of §4 is the one the existing desktop wallet
already emits and consumes. Adopting it verbatim — rather than minting a new
encoding — is what lets a desktop party and a hosted party be the two sides of one
swap with no migration.

**Why document bcore's BIP-322 deviation rather than the standard.** The node's
verifier hashes the raw message with an untagged double-SHA256. A signer that
followed the textbook tagged-hash construction would produce signatures the node
rejects. Interoperability requires specifying what the verifier actually checks.

**Why n = 1 first.** The single-signer key-path case removes nonce-aggregation
rounds and is sufficient for the watch-only swap. Multi-party (MuSig2, n ≥ 2)
aggregation is a strict extension of the same proprietary-field and reveal
machinery (the reserved `musig_*` suffixes of §4) and is left to a future TIP.

**Why primary-only routing.** Silent failover of a node-local identity is worse
than an explicit error: it would fork the user's trading identity and could orphan
an in-flight ceremony. Failing loudly preserves a single coherent identity per
tenant.

## Backwards compatibility

- **No consensus impact.** The on-chain transaction produced by the watch-only
  path is byte-identical to the desktop path. Nodes that do not implement this TIP
  validate those transactions exactly as before.
- **Desktop nodes are unaffected.** A node that never sends `tenant_id` operates
  on the `default` tenant with its existing single-identity behaviour. The
  watch-only RPC variants are additive; the original RPCs are unchanged.
- **Proprietary-field compatibility.** The `fs`/subtype-0 layout of §4 is the
  layout the existing desktop wallet already uses, so a hosted-built PSBT and a
  desktop-built PSBT carry identical ceremony fields and combine without
  translation.
- **Cross-deployment interoperability.** A desktop counterparty and a hosted
  counterparty can complete the same ceremony, because all shared state is in the
  PSBT and the bulletin-board/Nostr formats are unchanged apart from which
  per-tenant key signs.
- **Older clients** that predate the watch-only variants continue to use the
  original key-holding RPCs; they simply cannot be served from a watch-only node.

## Activation parameters

N/A — this TIP introduces no consensus rule and therefore has no block-height,
median-time-past, or versionbits activation.

- **Activation mechanism.** None. Adoption is per deployment: a deployment gains
  the capability when its node, bridge, and client components support the
  `tenant_id` parameter, the watch-only RPC family, and the proprietary-field
  layout of §4.
- **Minimum component versions.** A hosted deployment requires a node/bridge build
  exposing the §3 RPCs and §2 tenant routing/authorisation, and a client
  implementing the external-signer operations (§4.3, §5, §6) with BIP-322/BIP-340
  signing matching the node's constructions byte-for-byte.
- **Rollback statement.** Fully reversible. Because nothing is committed to
  consensus, a deployment may disable the hosted path and fall back to desktop
  operation at any time; the `default`-tenant behaviour is the original behaviour.
- **Rollout/monitoring.** A deployment SHOULD verify cross-path interoperability
  (desktop↔hosted and hosted↔hosted swaps) before exposing the hosted path, and
  SHOULD monitor tenant-affinity violations (`tenant-node-unavailable`),
  authorisation failures (`tenant-unauthorized`), and commit-reveal mismatches as
  health signals.

## Reference implementation

TBD — Draft. The reference implementation comprises: tenant routing in the cosign
bridge and `src/rpc/cosign.cpp`; the watch-only RPC family
(`adaptor.prepare_wo`, `adaptor.inject_presig`, `adaptor.commit_final_wo`,
`adaptor.complete_wo`, `spot.propose_wo`, `spot.accept_wo`,
`spot.add_commitment_proof_wo`) in `src/wallet/rpc/adaptor.cpp` and
`src/wallet/rpc/contracts.cpp`; the proprietary-field constants of §4 as already
defined in `src/wallet/fairsign.h` (identifier `fs`, subtype 0, suffixes
`nonce_pub`/`adaptor_point`/`adaptor_sig`/`commitment`); and a client-side
external signer implementing adaptor pre-signature production, adaptor-secret
normalisation, encrypted-payload decryption, and BIP-322 message signing matching
`src/rpc/bip322.cpp`.

## Security considerations

- **Identity isolation is the security boundary.** Per-tenant Nostr keys are what
  make proof-of-ownership meaningful in a shared deployment. A bug that leaked one
  tenant's key to another, that let `accept_request` bypass the ownership map, or
  that let a caller select a `tenant_id` it is not authorised for (§2.2), would
  let one user claim another's offers. Tenant key isolation, the ownership check
  (§2.3, §2.5), and caller authorisation MUST be tested adversarially.
- **The operator must remain unable to sign.** The whole point of the split is
  that the node sees only public material. Any watch-only RPC that inadvertently
  accepted or derived a signing key, or that fell back to a key-holding code path,
  would silently make the deployment custodial. The invariant of §3.1 MUST be
  enforced and tested (e.g. that the watch-only variants never call key-deriving
  routines).
- **Nonce hygiene.** The external signer's adaptor nonce MUST be generated from a
  cryptographically secure source and used exactly once. A ceremony restart MUST
  regenerate the nonce; reuse across two messages leaks the private key.
  Implementations SHOULD zero nonce material immediately after use.
- **Free-option attack.** The lock-step reveal check (§7) is the mitigation; it
  MUST be node-enforced and unskippable. An adversarial test MUST confirm that
  calling complete without a prior committed-and-matching reveal is rejected.
- **Adaptor-secret parity.** Skipping the normalisation of §4.3 is not just a
  correctness bug; an implementation that normalised inconsistently could produce
  a final signature that verifies for one party but mismatches the commitment for
  the other, stalling the swap. Cross-implementation test vectors covering both
  parity branches are required.
- **BIP-322 construction mismatch.** Because the node's verifier deviates from the
  textbook tagged-hash construction (§5), a client that uses the standard
  construction would generate ownership/attestation proofs the node silently
  rejects, breaking peer attestation. The construction MUST be validated against
  the node's verifier, not against a generic BIP-322 library.
- **Affinity failure mode.** Primary-only routing trades availability for identity
  coherence. A deployment MUST surface `tenant-node-unavailable` to the user
  rather than retry against a node that would mint a divergent identity.
- **Commitment-hash trust.** `spot.add_commitment_proof_wo` trusts a
  client-supplied hash by design (§6). This is safe only because a wrong hash
  harms only the party that supplies it; reviewers MUST confirm that no other
  party's funds or proof depend on that hash being correct.
- **Replay across tenants.** Ownership and attestation proofs bind to the
  per-tenant Nostr key; reviewers MUST confirm a proof minted for one tenant
  cannot be replayed under another tenant's listing.

## Test vectors

Where applicable, an interoperable implementation should be validated against
golden vectors for: (a) the adaptor-secret normalisation of §4.3 across both
adaptor-point parity branches; (b) the per-input tagged commitment of §4.2
(`H_tag("fs/adaptor")(R' ‖ T ‖ P ‖ m)`) and the final-signature commitment of §7
(`SHA256(final_sig)`) for a fixed `(R', s', T, P, m, t)` tuple; (c) a BIP-322
signature over a fixed `(address, message)` for P2TR key-path and P2WPKH,
demonstrating the untagged double-SHA256 message hash of §5; and (d) a complete
`n = 1` key-path swap PSBT carrying the §4 proprietary fields, demonstrating a
desktop-built and a hosted-built party producing byte-identical final witnesses.
Concrete vectors are to be supplied with the reference implementation before this
TIP leaves Draft.

## Copyright

This TIP is released into the public domain (CC0).
```
