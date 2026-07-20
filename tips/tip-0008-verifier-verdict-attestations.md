```
TIP: 0008
Title: Verifier verdict attestations and aggregate validation envelopes
Author: takakuni <takakuni@tensorcash.org>
Type: Standards Track
Status: Draft
Created: 2026-07-20
Requires: <none>
Replaces: <none>
```

## Abstract

TensorCash full verification currently returns a bare verdict enum for a
validation request. That verdict is useful operationally, but it is not
attributable to any verifier, cannot be checked against an admitted verifier set,
and cannot support Sybil-resistant aggregation across independent verification
providers. This TIP specifies a backward-compatible attestation layer for
verifier verdicts. A verifier signs a verdict-independent `subject_digest` plus
the verdict it produced, the verifier identity, the registry epoch, and a
timestamp. A coordinator or node aggregates those signed verdict envelopes using
a verifier registry snapshot, per-provider independence accounting, per-model
coverage requirements, Amber-as-abstain semantics, and quorum rules that cap the
influence of any one operator.

The proposal is intentionally additive. It does not change the existing
FlatBuffer `ValidationRequest` or `ValidationResponse` wire format used by the
node-to-verifier transport, and it does not introduce a new consensus rule by
itself. Implementations expose and consume the attested data at the HTTP/JSON
layer and may continue to return the legacy enum internally. Nodes that opt into
community or third-party validation can require aggregate signatures before
accepting a remote full-verification verdict; older nodes and local sovereign
verifier deployments are unaffected.

## Motivation

The existing validation API is designed around a trusted verifier service. The
request contains the proof or model-validation material to check, and the
response is a compact enum such as `Full_Green`, `Full_Amber`, or `Full_Red`.
That shape is adequate when the node operator controls the verifier process, or
when a client knowingly trusts one hosted service, but it is not enough for a
community verification market.

Four limitations are load-bearing:

1. **No attribution.** A bare enum cannot prove which verifier produced it. If a
   verifier lies, equivocates, or freeloads by signing expected answers without
   running the model, there is no portable evidence.

2. **No Sybil resistance.** A coordinator can ask many endpoints for answers, but
   without a registry of admitted identities and independence accounting, many
   endpoints may still represent one operator.

3. **No per-model coverage signal.** A model that is supported by one honest
   provider and a model supported by ten independent providers look identical at
   the verdict surface unless the registry and aggregate response expose coverage
   explicitly.

4. **No sovereign trust boundary.** A mining node that uses a remote fallback
   needs to know whether it is accepting one operator's unsigned assertion or an
   independently-verifiable aggregate of admitted signers.

This TIP fixes the attestable protocol surface. It does not claim to make model
verification trustless. A node that accepts an aggregate remote verdict is
trusting an admitted verifier quorum, not replaying the model computation or
checking a succinct proof. Succinct or ZK proof-of-inference remains the
trustless endgame; this TIP is the practical attestation layer needed before that
exists.

## Specification

The keywords MUST, MUST NOT, SHOULD, and MAY are to be interpreted as in RFC
2119. This TIP specifies an off-chain validation attestation protocol. It
introduces no new block encoding, opcode, transaction rule, or on-chain
consensus rule.

### 1. Scope

This TIP defines:

- the canonical subject being attested;
- the signed verifier verdict envelope;
- verifier registry snapshots;
- aggregate validation envelopes;
- quorum, coverage, and abstention semantics;
- monitoring evidence for equivocation, canaries, and reputation;
- node-side verification requirements for clients that choose to accept an
  aggregate remote verdict.

This TIP does not define a particular hosted coordinator, admission business
process, payment system, staking contract, or public endpoint hostname. Those are
deployment concerns.

### 2. Terminology

- **Validation subject** - the proof, model validation item, challenge item, or
  logits validation item being verified.
- **Subject digest** - a verdict-independent digest identifying the validation
  subject under one registry epoch and chain context.
- **Verifier** - an admitted signing identity that produces verdict envelopes.
- **Provider** - an independently controlled operator identity. One provider MAY
  operate multiple verifier workers or signing keys, but those workers count as
  one provider for independence and per-provider weight caps.
- **Coordinator** - an optional service that routes validation subjects to
  verifiers, collects signed verdicts, and publishes aggregate envelopes.
- **Registry epoch** - a signed snapshot of admitted verifier identities,
  provider identities, weights, and supported models.
- **Aggregate verdict** - a final verdict derived from a quorum of signed
  verifier verdict envelopes.
- **Terminal verdict** - a verdict that can finalize an aggregate result.
- **Abstain verdict** - a verdict that records non-finality or uncertainty and
  MUST NOT be cached or aggregated as success or failure.

### 3. Verdict classes

Implementations MUST preserve the following semantics for full verification:

- `Full_Green` is a terminal positive full-verification verdict.
- `Full_Red` is a terminal negative full-verification verdict only after the Red
  quorum and expansion rules in this TIP are satisfied.
- `Full_Amber` is an abstain verdict. It MUST NOT be mapped to a successful
  quick/smell verdict, MUST NOT be persisted as a terminal full-verification
  result, and MUST NOT by itself reject a proof.

For model, challenge, quick, smell, and logits validation, the registry or
profile used by an implementation MUST define which enum values are terminal and
which are abstain or pending. Pending, timeout, failed, review, and ambiguous
states MUST be treated as non-terminal unless explicitly promoted by a later
Standards-Track TIP.

### 4. Validation subject digest

Every signed verdict envelope MUST bind to a `subject_digest` that is independent
of:

- the verdict;
- the verifier identity;
- the verifier signature;
- the coordinator that routed the request;
- the wall-clock time at which the verdict was produced.

This separation is required so that equivocation is detectable. Two
contradictory verdicts from the same verifier for the same subject MUST collide
on the same `subject_digest`.

The v1 subject digest is:

```
subject_payload_hash = SHA256(canonical_validation_subject_bytes)

subject_digest = SHA256(
    "TENSORCASH_VERIFIER_SUBJECT_V1" || 0x00 ||
    u16le(len(chain_id)) || chain_id ||
    u64le(registry_epoch) ||
    u8(validation_type) ||
    u16le(len(target_override_bytes)) || target_override_bytes ||
    subject_payload_hash
)
```

`chain_id` MUST identify the TensorCash network context, for example `main`,
`test`, or a deployment-specific chain identifier. Implementations MUST reject an
envelope whose `chain_id` does not match the validating node's active chain.

`registry_epoch` MUST identify the verifier registry snapshot used for committee
selection and signature verification.

`validation_type` MUST be the numeric validation type from the existing
validation schema.

`target_override_bytes` MUST be empty when no target override is used. When a
target override is used, it MUST be the exact byte string used by the validation
service to alter the subject's target comparison.

`canonical_validation_subject_bytes` MUST exclude transient request-routing
fields such as coordinator job IDs, HTTP request IDs, and cache keys.

**Draft-gating canonicalization requirement.** This draft does not yet fully
specify the byte-level canonicalization required for cross-implementation
signatures. Before this TIP can advance beyond Draft, this subsection MUST be
replaced with exact byte layouts for:

- `canonical_validation_subject_bytes` for every supported `validation_type`;
- the precise `target_override_bytes` encoding;
- the treatment of omitted optional fields, default values, byte order, and
  FlatBuffer table ordering;
- the reject behavior for malformed or non-canonical subjects.

Until those bytes are fixed, an implementation MUST NOT claim interoperable
conformance to this TIP. For a full proof verification subject, the intended v1
input is the canonical serialized proof payload committed by the block. For
other validation types, the intended v1 input is the canonical byte
representation of the item whose model verdict is being attested.

### 5. Verifier verdict envelope

A verifier verdict envelope is the signed statement produced by one verifier for
one validation subject.

The JSON representation MUST contain:

```
{
  "version": 1,
  "chain_id": "main",
  "registry_epoch": 12345,
  "validation_type": 1,
  "subject_digest": "<32-byte hex>",
  "verifier_id": "<32-byte hex>",
  "verdict": "Full_Green",
  "verdict_code": 6,
  "produced_at_unix_ms": 1784567890123,
  "signature_alg": "ed25519",
  "signature": "<64-byte hex>"
}
```

`signature_alg: "ed25519"` means plain Ed25519 over the raw payload bytes defined
below. It does not mean Ed25519ph, and implementations MUST NOT externally
prehash the payload with SHA-512 before signing.

The verdict signature payload is:

```
"TENSORCASH_VERIFIER_VERDICT_V1" || 0x00 ||
u16le(len(chain_id)) || chain_id ||
u64le(registry_epoch) ||
u8(validation_type) ||
subject_digest ||
verifier_id ||
u8(verdict_code) ||
u64le(produced_at_unix_ms)
```

The verifier MUST sign the exact verdict signature payload with the Ed25519
private key corresponding to the public key in the registry snapshot for
`verifier_id`. Clients MUST verify the Ed25519 signature against that public key
using the same raw payload bytes. Clients MUST reject an envelope that declares
`signature_alg: "ed25519"` but only verifies under Ed25519ph or another
prehashed variant. `verdict` is a human-readable copy of `verdict_code`; clients
MUST use `verdict_code` as the normative value and MUST reject an envelope if the
name and code disagree.

A verifier MUST NOT sign two contradictory terminal verdicts for the same
`(subject_digest, validation_type)` pair. A verifier MAY sign an abstain verdict
and later sign a terminal verdict for the same subject only if the implementation
profile explicitly permits escalation from abstain to terminal. It MUST NOT sign
both terminal positive and terminal negative verdicts for the same subject.

### 6. Verifier registry snapshot

A registry snapshot binds verifier signing keys to provider identities, weights,
and durable model support for one epoch.

The JSON representation MUST contain:

```
{
  "version": 1,
  "chain_id": "main",
  "registry_epoch": 12345,
  "valid_from_height": 100000,
  "valid_until_height": 101000,
  "created_at_unix_ms": 1784560000000,
  "selection_beacon": {
    "type": "future_block_hash",
    "reference": "main:100010"
  },
  "quorum_profile": {
    "committee_size": 7,
    "min_distinct_providers": 5,
    "min_weight": 5,
    "max_weight_per_provider": 1,
    "red_expansion_size": 4,
    "min_model_providers_fit_for_full": 5
  },
  "verifiers": [
    {
      "verifier_id": "<32-byte hex>",
      "ed25519_pubkey": "<32-byte hex>",
      "provider_id": "<32-byte hex>",
      "provider_weight": 1,
      "models_supported": [
        "model-name@commit"
      ],
      "status": "active"
    }
  ],
  "registry_authority_sig": "<signature hex>"
}
```

The registry authority MAY be an operator-curated key in a bootstrap deployment.
Long-term deployments SHOULD replace discretionary authority with a
stake-governed registry or another credibly neutral admission mechanism.

`verifier_id` MUST be derived from the verifier public key and epoch context:

```
verifier_id = SHA256(
    "TENSORCASH_VERIFIER_ID_V1" || 0x00 ||
    u64le(registry_epoch) ||
    ed25519_pubkey
)
```

`provider_id` MUST identify independent operator control. Multiple verifier
workers, machines, or signing keys under common operator control MUST share one
`provider_id` for quorum and weight-cap accounting.

The registry snapshot MAY publish epoch-pseudonymous signing keys instead of
long-lived verifier keys. If pseudonymous keys are used, the registry MUST still
bind them to provider identities for that epoch, and the aggregate verification
rules MUST count provider independence, not key count.

The registry snapshot MUST NOT include network locations, IP addresses, live
worker load, or per-verifier live VRAM residency. Durable `models_supported` is
public because clients need it to verify committee eligibility. Ephemeral
residency and capacity are private coordinator telemetry and MAY be exposed only
as aggregate counts by deployment-specific APIs.

### 7. Committee selection

An implementation that claims Sybil-resistant aggregation MUST select verifiers
from a registry snapshot using an unpredictable or miner-uncontrolled seed. A
committee seed derived only from miner-controlled proof or block material MUST
NOT be described as grind-resistant.

The committee selection seed SHOULD combine:

- the registry epoch;
- the validation subject payload hash or subject digest;
- a beacon fixed outside the miner's control at proof-grinding time, such as a
  future block hash, VDF output, or external randomness beacon;
- the validation type.

The exact beacon source and latency trade-off MUST be specified by the
implementation profile or a future revision of this TIP. If an implementation
uses miner-influenced entropy, it MUST publish a cost model explaining how many
grind attempts are required to materially steer the committee and why the
registry, stake, and coverage parameters make that attack uneconomic.

Committee selection MUST sample admitted verifiers from the registry snapshot and
MUST NOT increase a provider's selection probability merely because that provider
runs more endpoints. Provider-level caps MUST apply before final quorum
acceptance.

### 8. Aggregation rule

To accept an aggregate verdict, a client MUST verify all of the following:

1. The registry snapshot is valid for the envelope's `registry_epoch` and
   `chain_id`.
2. Every included verifier envelope has a valid Ed25519 signature.
3. Every included verifier is active in the registry snapshot.
4. Every included verifier is eligible for the subject's model or validation
   class according to `models_supported` and the implementation profile.
5. All included envelopes have the same `subject_digest`, `chain_id`,
   `registry_epoch`, and `validation_type`.
6. Abstain verdicts are not counted as terminal support.
7. The aggregate has at least `min_distinct_providers` distinct provider IDs for
   the terminal verdict.
8. The aggregate reaches `min_weight` after applying `max_weight_per_provider`.
9. The subject's model has at least `min_model_providers_fit_for_full`
   independent providers available in the registry or coverage profile used by
   the implementation.

`Full_Amber` MUST expand sampling or return non-final. It MUST NOT count toward
`Full_Green` or `Full_Red`.

A single `Full_Red` MUST NOT finalize a negative full-verification aggregate
unless the quorum profile explicitly permits single-signer Red. The default
profile SHOULD require Red confirmation by additional independent providers.
When a Red disagrees with Green or Amber results, the coordinator SHOULD preserve
all signed envelopes and the validation inputs needed for adjudication.

### 9. Aggregate validation envelope

A coordinator MAY publish an aggregate validation envelope. The coordinator's
signature is useful for origin authentication and cache integrity, but it MUST
NOT replace verifier signature verification unless a future TIP defines a
threshold-signature construction with equivalent public verifiability.

The JSON representation MUST contain:

```
{
  "version": 1,
  "chain_id": "main",
  "registry_epoch": 12345,
  "validation_type": 1,
  "subject_digest": "<32-byte hex>",
  "aggregate_verdict": "Full_Green",
  "aggregate_verdict_code": 6,
  "quorum_profile": {
    "committee_size": 7,
    "min_distinct_providers": 5,
    "min_weight": 5,
    "max_weight_per_provider": 1,
    "red_expansion_size": 4,
    "min_model_providers_fit_for_full": 5
  },
  "provider_count": 5,
  "weight": 5,
  "verifier_verdicts": [
    { "...": "verifier verdict envelope" }
  ],
  "coordinator_id": "<optional hex>",
  "coordinator_sig": "<optional signature hex>"
}
```

Clients that rely on the aggregate MUST verify the embedded verifier verdicts
against the registry snapshot. A response that omits the embedded verifier
verdicts MAY be used as an informational cache hit, but MUST NOT be used by a
sovereign or mining node as sufficient evidence for full validation unless an
equivalent verifiable proof is supplied.

### 10. Public status semantics

An implementation that exposes public status lookup for aggregate verdicts MUST
distinguish:

- `unknown` - no terminal aggregate is available;
- `pending` - verification was requested but has not reached quorum;
- `abstain` - enough evidence exists to avoid a terminal result, such as
  persistent Amber;
- `terminal` - a verified aggregate envelope is available.

`unknown`, `pending`, and `abstain` MUST NOT be interpreted as `Full_Red`.
Nodes MAY retry, use a local verifier, or treat the proof as temporarily
unresolved according to their existing validation policy.

### 11. Misbehavior evidence

Implementations SHOULD store signed verdict envelopes durably because they are
the evidence record for monitoring and accountability.

The following evidence is objectively checkable:

- **Equivocation:** two valid signatures from the same `verifier_id` over the
  same `(subject_digest, validation_type)` with contradictory terminal verdicts.

Equivocation evidence SHOULD cause immediate registry eviction and MAY trigger
stake slashing in deployments that bind registry membership to slashable stake.

Canary or trap proofs MAY be used to detect lazy or dishonest verifiers. Canary
failures MUST NOT be treated as credibly neutral on-chain slashing evidence
unless the canary set was precommitted and the expected verdict is later revealed
in a way that independent reviewers can reproduce.

Honest disagreement on Amber-band proofs and abstention MUST NOT be slashed.
Sustained statistical deviation, unavailability, or suspected Sybil correlation
MAY reduce reputation or selection weight after adjudication.

## Rationale

The proposal signs verdicts at the HTTP/JSON layer rather than changing the ZMQ
or FlatBuffer transport because the existing node and verifier paths already move
the compact enum efficiently. Signing an additive envelope lets hosted services,
light clients, and fallback validators gain attribution without forcing every
local verifier operator to change the consensus-adjacent wire immediately.

The `subject_digest` deliberately excludes the verdict, verifier identity, and
timestamp. If those fields were included, two contradictory verdicts would have
different subject hashes and equivocation would not be self-proving.

The registry publishes verifier public keys and durable model support because
clients cannot verify aggregate signatures or model eligibility otherwise. It
does not publish live endpoint addresses, IPs, load, or VRAM residency because
that information is operationally sensitive and helps attackers target niche
model coverage.

Amber is treated as abstain because full verification is statistical. Penalizing
or terminally caching Amber would push verifiers to guess and can wedge clients
on ambiguous results. A terminal result should require clear Green or clear Red
quorum from independent providers.

This design decentralizes trust in the model verdict; it does not eliminate that
trust. The node still does not replay the model computation when it accepts a
remote aggregate. That limitation is explicit so this TIP does not obscure the
need for future succinct or zero-knowledge verification.

## Backwards compatibility

This TIP is backward compatible with existing blocks, wallets, and verifier
runners. The existing FlatBuffer request and response messages are unchanged.
Legacy nodes that do not know about verdict attestations may continue to accept a
local verifier's bare enum according to existing policy.

Clients and nodes that opt into community validation SHOULD require aggregate
validation envelopes before accepting a remote full-verification verdict. A
deployment MAY expose both legacy and attested responses during migration.

Registry snapshots and aggregate envelopes are additive artifacts. Older clients
ignore them. Newer clients MUST NOT assume that a legacy bare enum is backed by a
quorum unless the aggregate envelope is present and verifies.

## Activation parameters

N/A for the attestation protocol as written. This TIP does not change consensus
validation rules.

If a future revision makes per-model `R_min` coverage, aggregate signature
verification, or remote quorum acceptance mandatory for block admission, that
revision MUST specify activation height or versionbits-style activation,
minimum node version, rollback policy, and monitoring plan.

## Reference implementation

TBD.

Expected implementation areas:

- verifier envelope signing in the verification API runner;
- registry and aggregation logic in the hosted verification service;
- aggregate-envelope verification in `services/core-node/bcore`;
- public status response extension for aggregate envelopes;
- test vectors for digest construction, signature verification, quorum counting,
  Amber handling, and equivocation detection.

Draft blockers before Proposed:

- exact canonical bytes for every supported validation subject type;
- exact target-override encoding;
- test vectors generated from those canonical bytes.

## Security considerations

**Verdict forgery.** Clients MUST verify Ed25519 signatures against the registry
snapshot. A coordinator signature alone is not sufficient.

**Coordinator equivocation or censorship.** A coordinator can withhold requests or
responses. It cannot forge verifier signatures. Clients MAY query multiple
coordinators once more than one deployment exists.

**Sybil admission.** Multiple verifier keys do not imply multiple independent
providers. Quorum rules MUST count provider IDs and apply per-provider weight
caps. Bootstrap allowlists are discretionary and SHOULD migrate to stake or
another credibly neutral admission mechanism.

**Committee grinding.** A committee seed based only on proof or block material is
grindable by miners. Implementations that claim grind resistance MUST use an
uncontrolled beacon or publish a concrete cost model.

**Coverage collapse for niche models.** Aggregate verification is weak when only
one provider can serve a model. Implementations MUST expose or enforce
per-model coverage requirements and MUST return under-coverage as under-coverage,
not as normal quorum.

**Sticky Amber.** `Full_Amber` is an abstain verdict. It MUST NOT be cached or
served as terminal success.

**Privacy.** Verifiers that receive a validation subject can see the proof
material they need to check. Sovereign miners should understand that remote
fallback exposes proof internals to the selected verifier committee.

**Trust limitation.** A valid aggregate proves that admitted verifiers signed a
verdict. It does not prove that the model computation was performed correctly to
a trustless verifier. Misbehavior monitoring, canaries, and slashing reduce the
risk but do not replace succinct verification.

## Test vectors

TBD before this TIP can move beyond Draft.

Required vectors:

- subject digest construction for each validation type;
- verifier ID derivation;
- Ed25519 verdict signature verification;
- rejection of mismatched `verdict` and `verdict_code`;
- aggregate quorum acceptance;
- rejection of insufficient provider independence;
- rejection of insufficient model coverage;
- Amber-as-abstain handling;
- equivocation evidence with two contradictory signed envelopes.

## Copyright

This TIP is released into the public domain (CC0), unless stated otherwise.
