```
TIP: 0006
Title: Multimodal Input/Output Extension for Proof-of-Inference
Author: takakuni <takakuni@tensorcash.org>
Type: Informational
Status: Draft
Created: 2026-06-25
```

## Abstract

TensorCash settles useful proof-of-work by committing a deterministic, replayable
trace of an autoregressive decode into each block. The committed input is a
sequence of integer token identifiers (`prompt_tokens`). Verification has two
tiers: a cheap in-node tier that replays the sampling arithmetic over the
committed logits, and a full tier that re-executes the model forward pass on the
committed token context and statistically checks the freshly produced top-k logits
against those committed in the proof. Both tiers consume the input as integer
token identifiers. This is sound for text, whose canonical form already *is* a
short sequence of vocabulary indices, but it has no representation for modalities —
images, audio, video — whose model input is a sequence of *continuous* feature
vectors rather than discrete vocabulary indices. Because the full tier must feed
the model the exact input the miner used in order to reproduce matching logits, a
continuous input has no compact reproducible identifier and would have to be made
available to verifiers verbatim — heavy relative to the proof and block size
limits. This proposal states the gap precisely and lays out several candidate
encodings — discretization to committed integer codes, carrying continuous
embeddings under a canonical encoding, and on-chain commitment with off-chain
availability — each with its trade-offs. It makes no recommendation among them;
codebook construction, quantization schemes, activation, and the choice of
approach are open for community discussion. The proposal specifies the *input*
path in detail; non-text *output* (multimodal generation) is in scope but left
unresolved, and a future revision would need to address it before generation is
activated.

## Motivation

Proof-of-inference is currently defined only for text. A miner that wishes to
serve a vision or audio model cannot produce a valid proof for a non-text input,
because there is no consensus-recognized way to (a) place a continuous input into
the proof and (b) have a verifier deterministically reconstruct and re-feed it.
Extending the network to multimodal inference broadens the class of useful work
the chain can settle and the class of models that can mine. The constraint is that
any extension has to preserve the properties the two verification tiers rely on.

The remainder of this section states the gap precisely, because the candidate
approaches follow directly from it.

### How the current proof represents input

The proof is the structure `CProofBlob` (`src/primitives/proofblob.h`), serialized
in the block body (`src/primitives/block.h`) and committed by the block header via
`hashPoW` (a four-leaf Merkle root over the blob, so every field including the
input is bound). The input is the field:

```
prompt_tokens : [uint32]   // src/primitives/proofblob.h; shared-utils/fb-schemas/proof.fbs
```

The plaintext input *text* is never stored; only its tokenization survives as
integer vocabulary indices. There is no hash-commitment of an external input and
no off-chain input transport — the integer identifiers are the input, in full, on
chain.

### How verification uses the input

Verification has two tiers, and both consume the input as integer token IDs.
Neither re-tokenizes a plaintext prompt; the committed `prompt_tokens` are used
directly.

**Tier 1 — cheap in-node replay** (`src/verification/quick_verifier.cpp`). This
tier does **not** run the model. For each generated step it reconstructs the
decode context as the integer sequence `prompt_tokens` followed by the
already-chosen tokens. Each context entry is stored in the proof as a `uint32`
token id (`shared-utils/fb-schemas/proof.fbs`, `src/primitives/proofblob.h`) but
is serialized into the hash window as an 8-byte little-endian integer; the window
is a fixed 256-slot buffer (`POW_WINDOW_SIZE`,
`src/verification/quick_verifier.h`; 8-byte slots,
`src/verification/quick_verifier.cpp`). That window is hashed together with the
header prefix, VDF output, tick, step index, and the `compute_precision` tag, and
the digest is mapped to a per-step uniform draw `u`. It then checks, against the committed
top-k and probe logits, that the committed token for that step is the one whose
probability interval contains `u`. This confirms the sampling trace is
self-consistent with the committed logits and the VDF/PoW.

**Tier 2 — full verification** (the GPU verifier service;
`services/verification-api/src/proof_verifier.py`, `full_verify`). This tier
**re-executes the model forward pass.** It binds the model named in the proof,
builds the input as the committed integer context (`prompt_tokens` concatenated
with `chosen_tokens`), runs the model over it, and compares the freshly produced
top-k logits — gathered at the committed `topk_indices` — against the committed
`topk_logits`. The comparison is a statistical (bootstrap / Mahalanobis) test with
hard numeric gates (a grid-snap precision check and a per-logit outlier gate),
yielding a Green / Amber / Red verdict. Under current node behaviour a RED verdict
is **not** an outright rejection: the block data is accepted but contributes zero
local work (`full_red_chainwork`; `src/validation.cpp`), with the RED status
tracked per block. Any multimodal extension's activation would have to define
whether a RED multimodal proof rejects, defers, or zeroes work. This is the tier that checks the
committed logits are numerically what the named model actually produces.

The load-bearing consequence: **the full tier must feed the model the exact input
the miner used, or it cannot reproduce matching logits.** For text, the committed
integer token IDs *are* that exact input — small, exact, and reproducible across
hardware. The model is deterministic enough (within the statistical test's
tolerances) given the IDs and the precision tag that the IDs alone suffice for both
tiers.

### Why continuous inputs break this

The model input for a non-text modality is a sequence of *continuous* feature
vectors — for example, patch embeddings from a vision encoder, or framewise
acoustic features. These have no integer vocabulary index. Two problems follow,
one per tier:

1. **Tier 1: no identifier to hash.** The U-value derivation packs each context
   element — a `uint32` token id, widened to an 8-byte little-endian integer —
   into the 256-slot window. A continuous vector has no integer token identity and
   no slot in this scheme. The context-hashing path is undefined for it.

2. **Tier 2: no compact reproducible handle, so the input itself must be
   available.** For text, the small integer ID *is* a faithful, reproducible
   handle from which the verifier reconstructs the exact model input. For a
   continuous embedding there is no such canonical small handle the verifier can
   independently rederive: to re-run the forward pass and reproduce the committed
   logits, the verifier needs the exact continuous input. The only way to give it
   that is to make the continuous input available verbatim — in the proof, or
   otherwise. In other words, a continuous-embedding modality de facto reduces to
   **putting the plain (continuous) input where verifiers can read it.**

This is heavy. A single image's patch-embedding sequence — let alone raw pixels or
spectrogram frames — is large relative to the per-proof ceiling
`MAX_POW_BLOB_SIZE = 1000000` bytes (1 MB) and competes with the block budget
(`MAX_BLOCK_WEIGHT = MAX_BLOCK_SERIALIZED_SIZE = 5000000` bytes,
`src/consensus/consensus.h`). It also raises a determinism question: the current
input path commits *integers*, which are trivially reproducible across hardware
and engines, whereas a continuous input forces a decision about how float data is
encoded and reproduced.

The rest of this document lays out candidate approaches and the requirements each
has to meet. It does not select among them.

## Specification

This is an Informational TIP: it mandates nothing. The material below is
descriptive — it records (1) the requirements a future Standards-Track TIP would
need to address, (2) three candidate encodings with their trade-offs, (3) candidate
reject codes such a TIP could define, and (4) the modality descriptor it would
need. Where this section says an approach "has to" or "would need to" satisfy
something, that describes a design constraint, not an active mandate; RFC-2119
keywords are deliberately avoided. Exact field tags, codebook construction, bound
values, and the choice among the candidates are open (see Open questions). This
document makes no recommendation among the candidates.

### 1. Requirements a future Standards-Track TIP would need to address

Any encoding of a multimodal input into a proof has to satisfy all of the
following. These are constraints the design space imposes, not a preference for any
candidate, and are recorded so a future Standards-Track TIP can state them
normatively.

- **R1 — Tier-1 integer context.** The decode context Tier 1 reconstructs has to be
  a sequence of integers it can hash exactly, identically to the existing text
  path, or Tier 1's context-hashing path has to be extended with a defined,
  bit-exact encoding for the new input elements. Tier-1 verification has to remain
  free of any model forward pass.
- **R2 — Tier-2 input reproduction.** A full verifier has to be able to obtain the
  exact model input for every step, so the forward pass reproduces logits that pass
  the statistical comparison. The input has to be either reconstructable from
  compact chain-committed identifiers, or made available to verifiers in full.
- **R3 — Size bound.** Whatever is carried in the proof, including any multimodal
  input data, has to keep the serialized proof within `MAX_POW_BLOB_SIZE`. Data not
  carried in the proof has to have a defined availability path (R2).
- **R4 — Committed transform.** Any transformation from continuous input to the
  representation actually committed has to be fixed by a chain-committed identifier
  (see §4), so miner and verifier agree on the exact mapping and it cannot be
  silently changed.
- **R5 — Backwards-compatible text path.** A proof carrying no multimodal
  descriptor has to verify exactly as today (see Backwards compatibility).

### 2. Candidate encodings

The three candidates below are presented as viable options for discussion. Each
satisfies the requirements differently and carries different trade-offs. This
proposal does not rank them; community input is invited on which (or which
combination, per modality) to pursue.

Like the rest of this Informational TIP, this subsection mandates nothing; it
sketches the design space. A future Standards-Track TIP that selects a candidate
would replace it with a normative specification — exact field tags, byte/TLV
layouts, bound values, and validation rules — at which point the activation
parameters can be filled in.

#### Candidate A — discretized multimodal tokens (committed codebook)

The continuous input is discretized to a sequence of integer codes drawn from a
fixed, content-addressed **codebook**, and those codes are carried in the existing
integer input field (or a modality-tagged sibling of it). This is the move neural
audio and image codecs already make — vector-quantizing continuous frames or
patches into discrete code indices (e.g. acoustic tokens, VQ image tokens); the
codebook plays the role of the text vocabulary.

- *How it meets the requirements:* a discrete code occupies a `uint32` proof slot,
  indistinguishable to Tier 1's context-hashing path from a text token id (R1); the
  codes are compact, so the proof stays small (R3); the codebook is the committed
  transform (R4). R2 is only **partially** addressed as stated: the full verifier
  today rebuilds its forward-pass input from `prompt_tokens` + `chosen_tokens` as
  token ids and calls the model with token ids
  (`services/verification-api/src/proof_verifier.py`). Feeding codebook-derived
  embeddings instead requires specifying how Tier 2 maps committed codes to actual
  model input — a gap this candidate would need to close.
- *Pros:* reuses the Tier-1 integer hashing/sampling proof surface (context
  hashing, sampler replay, logit commitment, entropy gate) with no change to the
  context-hashing core; smallest on-chain footprint.
- *Cons:* introduces a codebook as a consensus object that needs to be constructed,
  versioned, and agreed; discretization is lossy, which may bound the fidelity of
  the settled work; the discretizer's reproducibility across engines needs to be
  handled (either deterministic, or resolved by treating the committed codes as
  authoritative and verifying replay over them); the Tier-2 full-verifier input
  path currently assumes token ids and would need to be specified to consume
  codebook embeddings.

#### Candidate B — continuous embedding carried in the proof (canonical encoding)

The continuous input is carried verbatim in a new optional proof field under a
canonical, deterministic float encoding (fixed dtype, byte order, rounding), so it
is bit-exact across engines.

- *How it meets the requirements:* the canonicalized bytes — not native floats —
  become the object Tier 1 hashes, restoring R1; the input is in the proof, so
  Tier 2 has it directly (R2); R4 is satisfied trivially (the input is carried as
  is); R3 is the binding constraint.
- *Pros:* no fidelity loss; no codebook to construct or govern; usable for models
  with no faithful discrete tokenizer.
- *Cons:* large — bounded by `MAX_POW_BLOB_SIZE`, so only small inputs fit, and it
  competes with the block budget; requires a portable bit-exact float
  canonicalization, which is the hard part and a soundness risk if it is not truly
  reproducible across hardware; makes float data, rather than integers, part of
  the hashed input.

#### Candidate C — on-chain commitment with off-chain input availability

The proof carries only a commitment (hash) to the continuous input; the input
itself is distributed off-chain through a data-availability path to verifiers.

- *How it meets the requirements:* the commitment is compact (R3); Tier 2 fetches
  the input from the availability layer and checks it against the commitment before
  re-running the forward pass (R2); the commitment scheme is the committed
  transform (R4). R1 requires a defined rule for how Tier 1 treats a
  commitment-only input (e.g. hashing the commitment into the context, or deferring
  the input-dependent check to Tier 2).
- *Pros:* keeps the on-chain proof small regardless of input size; no fidelity
  loss; no codebook governance.
- *Cons:* introduces a data-availability assumption — a verifier that cannot fetch
  the input cannot complete Tier 2; weakens the current self-contained property
  (today a proof needs nothing external); requires defining the availability
  layer, retention, and what a verifier does when data is missing.

Combinations are possible (e.g. discretized codes on-chain plus an off-chain
reference for higher-fidelity re-verification). All three candidates concern the
*input*; none of them changes the output-side machinery (the top-k/probe logit
commitment and the entropy gate), which is unaffected by the input candidate
chosen. Non-text *output* is a separate question, addressed in §5.

### 3. Candidate reject codes

Candidate consensus reject codes a future Standards-Track TIP could define (final
spelling open), in the style of the existing proof reject codes. Applicability
depends on the candidate adopted:

- `mm-input-undeclared` — multimodal codes/data present without a valid modality
  descriptor (§4), or vice versa.
- `mm-codebook-unknown` — referenced codebook identifier is not recognized
  (Candidate A).
- `mm-codebook-stale` — referenced codebook is not active at the proof's height
  (Candidate A).
- `mm-code-out-of-range` — a discretized code lies outside the declared codebook's
  index space (Candidate A).
- `mm-embedding-oversize` — carried continuous input exceeds the proof or block
  size bound (Candidate B).
- `mm-noncanonical-encoding` — carried continuous input is not in the canonical
  encoding (Candidate B).
- `mm-input-unavailable` — committed input cannot be obtained from the availability
  layer (Candidate C).
- `mm-commitment-mismatch` — fetched input does not match the on-chain commitment
  (Candidate C).

### 4. Modality descriptor

A multimodal proof would need a modality descriptor binding the input to its
encoding. At minimum such a descriptor would declare: the modality, the
candidate/encoding in use, and any parameters that encoding needs (for Candidate A,
the codebook identifier and version; for Candidate C, the commitment scheme). It
would be part of the proof and therefore covered by the existing header commitment.
The exact field layout (a new TLV in the proof, versus extension of existing
metadata fields) is open, and would be chosen to keep text-only proofs
byte-identical to today (R5).

### 5. Output modalities (scope)

The two verification tiers commit and check both sides of the decode: the input
(`prompt_tokens`) and the *output* — the generated tokens (`chosen_tokens`) and
their committed top-k/probe logits. Today the output, like the input, is integer
token ids plus logits, which is why text output verifies under both tiers
unchanged.

Non-text *output* (generated images, audio, code emitted as a non-text modality)
raises a question symmetric to the input gap: a continuous generated output has no
integer token identity to place in `chosen_tokens`, and the per-step logit
commitment assumes a discrete vocabulary distribution. The same design space
applies — discretized output codes (cf. Candidate A), a carried/committed
continuous output (cf. Candidate B/C) — and the same two-tier reproduction
requirement holds for the output trace.

This draft discusses the **input** path in detail and treats output modalities as
in-scope but unresolved. A future Standards-Track TIP would need to address how
non-text output is represented in `chosen_tokens` and the logit commitment before
multimodal *generation* could be activated; an input-only extension would leave
generated output in the existing text-token form. Output representation is
otherwise left open for discussion (see Open questions).

## Rationale

The requirements in §1 are derived from the two verification tiers, not from a
preference for any candidate: Tier 1 hashes integers, and Tier 2 re-runs the
forward pass and therefore needs the exact input. Every candidate is a different
answer to "how does the verifier obtain the exact input cheaply." Candidate A
discretizes so the input becomes integers; Candidate B carries the input so no
reconstruction is needed; Candidate C commits and distributes the input out of
band. They trade on-chain size, fidelity, governance burden, and self-containment
against each other, and the right balance may differ per modality. This document
deliberately leaves the choice open: it fixes the interface and the requirements so
that competing approaches can be specified, prototyped, compared, and discussed
under a stable surface.

Prior art for Candidate A is the discrete tokenization used by neural audio and
image codecs (VQ, residual VQ, product quantization, discrete acoustic tokens);
for Candidate C it is data-availability designs that keep large payloads off-chain
behind an on-chain commitment.

## Backwards compatibility

Any adopted approach would be a consensus-affecting change to the proof format and
its validation. The design target is that a proof with no modality descriptor (§4)
serializes and verifies byte-identically to a proof under the current rules (R5):
the text path is the special case "no multimodal input." New optional fields would
default to absent. Older nodes would not understand multimodal proofs and would
treat the extension as the activation gate dictates (see Activation parameters);
until activation, multimodal proofs would be non-standard and not accepted.

## Activation parameters

N/A for this Informational document — it specifies no consensus change to
activate. It records that **any** adopted approach would change consensus
validation and therefore require a coordinated activation, to be defined in a
later Standards-Track TIP that selects a candidate. Such a revision would need to
specify, at minimum:

- **Activation mechanism** — block height, median-time-past, or versionbits-style
  signalling, with exact threshold and window.
- **Minimum node version** required to enforce the new rules.
- **Rollback statement** — a consensus activation of this kind is normally
  non-rollback; that revision would need to state so explicitly.
- **Monitoring / rollout plan.**

Per-candidate consensus parameters (Candidate A: codebook registration and
lifecycle; Candidate C: the data-availability layer and retention) would be part
of that activation design; they are open questions here.

## Reference implementation

TBD — Draft. No reference implementation is proposed at this stage. The proof
structure, the two verification tiers, and the context-hashing path referenced
above (`src/primitives/proofblob.h`, `src/primitives/block.h`,
`src/verification/quick_verifier.cpp`,
`services/verification-api/src/proof_verifier.py`, `src/consensus/consensus.h`)
define the surfaces a reference implementation would extend.

## Security considerations

- **Tier-2 input reproduction is the security property.** Full verification re-runs
  the forward pass and compares logits statistically; it can only do so if it has
  the exact input. Candidate A relies on a specified Tier-2 mapping from committed
  codes and codebook to the model input; Candidate B on a bit-exact float
  canonicalization; Candidate C on the availability layer plus a sound commitment. A
  non-reproducible transform (a non-deterministic discretizer under A, a
  non-portable float encoding under B) would be a soundness break, not a quality
  issue.
- **Committed transform integrity.** Under A, a maliciously constructed or
  ambiguous codebook could let distinct inputs collide to the same codes, weakening
  the binding between proof and claimed work; under C, a weak commitment scheme has
  the same effect. A future spec would need the transform identifier to be
  content-addressed so it cannot be swapped under a fixed identifier (R4), and
  codebooks/commitment schemes to be auditable.
- **Size as a DoS surface.** Multimodal inputs are larger than text. A future spec
  would need to enforce the proof and block size bounds (R3) before any expensive
  validation; Candidate B in particular would need an early size check to avoid
  amplification.
- **Availability as an attack surface (Candidate C).** Withholding the off-chain
  input prevents Tier-2 completion. A future spec would need to define verifier
  behaviour when data is missing (the `mm-input-unavailable` path) so withholding
  cannot be used to force acceptance or stall the chain.
- **Modality confusion.** Under A, discretized codes that are not unambiguously
  separated from text vocabulary indices (§2) could make two different inputs hash
  to the same Tier-1 context; a future spec would need a tagging/namespacing rule to
  prevent this.
- **Output commitment unchanged by input modality.** The top-k/probe logit
  commitment and the entropy gate operate on output logits and are unaffected by
  the *input* candidate chosen; this proposal does not weaken them, and any
  multimodal-input proof remains subject to both verification tiers unchanged on the
  output side. Non-text *output* is out of scope for this draft and gated per §5.

## Test vectors

TBD — Draft. Test vectors are not applicable until a candidate and its parameters
are fixed. When fixed, vectors would cover: a text-only proof unchanged under the
new rules (R5); for A, a discretized multimodal proof with a known codebook, an
out-of-range code, and an unknown/stale codebook; for B, a canonical and a
deliberately non-canonical continuous encoding; for C, a matching and a mismatching
commitment and an unavailable-input case.

## Open questions

This proposal fixes the interface and requirements and explicitly invites
discussion on the following. None is settled here.

1. **Which candidate(s), per modality.** A, B, C, or a combination — and whether
   the choice should differ for images, audio, and video.
2. **Discretizer choice (Candidate A).** Which discrete tokenization (VQ, residual
   VQ, product quantization, discrete acoustic-token style bucketing) best balances
   fidelity, code length (R3), and cross-engine reproducibility.
3. **Quantization / bucketing granularity (Candidate A).** How coarse quantization
   can be — along the lines of discrete acoustic tokenization — before it materially
   degrades the usefulness of the settled work; whether per-modality or per-model
   granularity is warranted.
4. **Codebook registration and lifecycle (Candidate A).** How a codebook is
   proposed, content-addressed, activated at a height, versioned, and retired as a
   consensus parameter.
5. **Code/vocabulary namespacing (Candidate A).** Separate proof field versus
   offset/high-bit namespacing of the existing integer input field — which keeps the
   text path byte-identical (R5) at least cost.
6. **Continuous canonicalization (Candidate B).** Whether a portable bit-exact
   float canonicalization is achievable across the conformant engines and hardware,
   and for what maximum input size B remains within R3.
7. **Data-availability design (Candidate C).** What availability layer, retention
   policy, commitment scheme, and missing-data behaviour are acceptable.
8. **Transform reproducibility vs committed-output.** Whether the input transform
   needs to be deterministic, or whether consensus should only ever check
   replay/forward-pass against the committed representation, leaving transform
   reproducibility to engine conformance tests.
9. **Full-verifier input construction.** How Tier 2 builds the model forward-pass
   input when the committed representation is not token ids (codebook codes under A,
   continuous data under B/C), given that the current full verifier constructs
   input from `prompt_tokens` + `chosen_tokens` as token ids.
10. **Output modalities (§5).** How non-text generated output is represented in
    `chosen_tokens` and the per-step logit commitment, and whether output reuses the
    same candidate space (A/B/C) as input.

## Copyright

This TIP is released into the public domain (CC0).
