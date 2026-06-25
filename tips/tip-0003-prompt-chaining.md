```
TIP: 0003
Title: Header-Bound Prompt Commitment (Prompt Chaining)
Author: takakuni <takakuni@tensorcash.org>
Type: Standards Track
Status: Draft
Created: 2026-06-25
```

## Abstract

This proposal opens a design question on the proof-of-inference verification contract:
whether the prompt underlying a valid proof can be bound to live chain state so that the
transformer forward passes it produces cannot be computed before the block's header and
VDF output are known. Today the header and VDF are folded into the per-step sampling draw
and, through autoregression, into the realized token sequence; but the forward-pass logits
for a fixed prompt and context are independent of the header, and the prompt itself is
chosen freely by the miner. The protocol therefore relies on a statistical entropy gate
and on incentives — the expectation that a rational miner prefers commercially useful
output — to deter precomputed forward passes and low-entropy prompt grinding. This document
proposes a third proof version (v3) that converts that economic deterrent into a
by-construction property, and presents two candidate mechanisms — header-bound injection of
model-declared inert tokens, and consensus-enforced prompt non-reuse — together with their
costs. It does not select a single construction. Generation proceeds as a continuous
autoregressive stream over a rolling token window, and a registered model's vocabulary is
not freely indexable, so both mechanisms carry real constraints that the analysis must
resolve before this leaves Draft. Activation follows the version-keyed, per-network height
pattern of the v2 reuse-entropy gate.

## Motivation

TensorCash proof-of-work replaces SHA-256 grinding with verifiable inference: a miner runs
a registered model and commits a transcript whose sampling path is bound to the block
header, the VDF output, and a rolling token context. The intended security property is that
the value of a unit of mining work cannot be known without executing the inference path
under the actual chain state.

Two residual paths weaken this property by construction rather than by incentive:

1. **Precomputed forward passes.** The expensive artefact of inference is the transformer
   forward pass that produces per-step logits. For a fixed prompt and context, those logits
   do not depend on the header; only the subsequent token *selection* does. A miner that
   does not value output quality can precompute forward passes for a fixed prompt — at
   minimum the first step, and any near-deterministic continuation — and defer only the
   cheap header-dependent selection until the header and VDF arrive.

2. **Low-entropy prompt grinding.** Because the prompt is supplied freely and is never
   derived from chain state, a miner may deliberately choose a prompt whose path stays
   near-deterministic, minimizing genuine compute per accepted proof.

Both paths are presently deterred, not eliminated. The low-entropy ceiling and the v2
reuse-entropy gate (`REUSE_GATE_VERSION`, enforced in `src/verification/quick_verifier.cpp`
and gated in `src/validation.cpp`) make degenerate transcripts ineligible or low-value, and
the economic argument holds that a rational miner prefers genuinely useful inference, whose
commercial value dominates the marginal advantage of the shortcut. This proposal asks
whether that deterrent can be replaced with a by-construction guarantee, subject to two hard
constraints that any acceptable mechanism MUST respect: it MUST NOT break the continuous
autoregressive generation hot path, in which a proof is produced as a stream over a rolling
`POW_WINDOW_SIZE` token window; and it MUST NOT depend on indexing arbitrary vocabulary
entries, which can select control tokens, derail the model, or induce the very low-entropy
degeneracy the rule exists to prevent.

## Specification

The key words MUST, MUST NOT, REQUIRED, SHOULD, SHOULD NOT, and MAY are to be interpreted as
described in RFC 2119.

### Proof version

A proof MAY declare `version = 3` in the proof object (`version` byte of the proof blob;
see `src/primitives/proofblob.h`). A v3 proof satisfies every v2 validation rule and, in
addition, one or more of the binding rules below as fixed at activation. v3 is a strict
superset of v2.

This section defines a design space, not a single normative construction. Exactly one
construction (or a combination) is to be fixed before this advances past Draft; until then,
the candidate mechanisms and their constraints are specified so they can be analysed and
measured against each other.

### Candidate A — Header-bound inert-token injection

A model declares, as part of its registration commitment, an **inert binding alphabet**
`B = {b_0, …, b_{m-1}}` — a set of token identifiers chosen so that injecting them has
non-negligible computational effect on the forward pass while keeping bounded, non-catastrophic
semantic impact. Members of `B` MUST NOT be control or special tokens and MUST NOT be tokens
that induce a near-deterministic continuation. `B` and its size `m` are committed at model
generation and bound into the model identifier so that the consensus verifier and every
independent inference engine agree on the alphabet; header bytes are never used to index the
full vocabulary.

For binding position `p`, the injected token is:

```
seed_p = SHA256( header_prefix || vdf || tick || domain_tag || LE32(p) )
b_idx  = LE32(seed_p[0:4]) mod m
token  = B[b_idx]
```

The derivation folds in `vdf`, so — as with the sampling draw — the bound tokens cannot be
known before the sequential VDF output exists, even though `header_prefix` is largely
miner-chosen.

Injection MUST occur at rolling-window boundaries (positions ≡ 0 mod `POW_WINDOW_SIZE`), so
that binding refreshes each window without interrupting in-window autoregression; a v3 proof
MUST carry the injected tokens at those positions. The count of bound tokens per boundary and
the boundary cadence are open parameters. The verifier recomputes the expected bound tokens
from the proof's own `header_prefix`, `vdf`, `tick`, and the model's registered alphabet, and
MUST reject a proof whose injected tokens are absent or do not match with `bad-prompt-binding`.

### Candidate B — Consensus-enforced prompt non-reuse

Let `H = SHA256(canonical(prompt_tokens))` be the commitment of a proof's prompt. Consensus
maintains a set of previously accepted prompt commitments over a bounded retention scope
(an epoch, a rolling block window, or an accumulator). A v3 proof whose `H` is already present
in scope MUST be rejected with `prompt-reused`.

This forces a fresh prompt for each accepted proof, so a single precomputed transcript cannot
be amortized across blocks. It does not, on its own, prevent a miner from generating many
*distinct* low-entropy prompts; uniqueness is not entropy. It is therefore a partial measure,
potentially complementary to Candidate A. The retention scope, the state-growth bound, and
resistance to prompt front-running (an adversary publishing a victim's prompt commitment to
block it) are open parameters.

### Validation and reject codes

Once v3 is active at a given height, in addition to existing checks:

- Candidate A: injected bound tokens absent or not equal to the recomputed sequence →
  `bad-prompt-binding`.
- Candidate B: prompt commitment already present in retention scope → `prompt-reused`.
- A proof with `version < 3` submitted at or above the activation height MUST be rejected with
  the existing `bad-proof-version` code, mirroring the v2 activation behaviour in
  `src/validation.cpp`.

Checks are enforced at the same consensus sites that today enforce `bad-reuse-entropy` and
`bad-proof-version`, for any block whose proof carries inference fields.

## Rationale

**Why bind the prompt rather than only the sampling draw.** The header and VDF already
determine token *selection* and the realized sequence; they do not determine the *logits*,
which are the costly artefact. Forcing the forward-pass *inputs* to depend on the live header
and VDF removes the precomputation surface at its root and denies the miner control over the
low-entropy choice — but only if it can be done without breaking the generation stream or the
vocabulary contract.

**Why injection cannot be a suffix.** Generation is a continuous autoregressive stream rolled
in `POW_WINDOW_SIZE` chunks. A one-shot suffix has no place in a stream, and splicing tokens
mid-window forces the generator off its hot path. Re-binding at window boundaries is the only
placement that refreshes the header dependence each window while leaving in-window
autoregression intact — at the cost of a more intricate boundary protocol, which is part of
what must be evaluated.

**Why the binding alphabet must be declared, not derived modulo V.** Indexing the full
vocabulary by header bytes can land on control/special tokens, derail the model, or create a
degenerate low-entropy attractor that defeats the entropy gate. A curated inert alphabet,
committed at model registration, bounds the semantic blast radius while preserving a real
computational perturbation. The cost is a registration-time obligation to define and certify
inertness, and a model-identity coupling so the verifier and engines agree on `B`.

**Why also consider non-reuse.** Candidate B needs no tokenizer or model-generation change and
no hot-path surgery; it directly defeats reuse of a precomputed transcript. Its weakness is
that distinct low-entropy prompts evade it, so it is best read as a cheap complement to A
rather than a standalone fix.

**The open question.** Which family (or combination); for A, the per-boundary count and the
inert-alphabet certification process; for B, the retention/accumulator design and
front-running resistance; and for both, the interaction with the rolling window such that the
precomputation and low-entropy advantages are removed without degrading honest throughput.
This is left open for analysis and reference measurement.

## Backwards compatibility

v3 is opt-in by proof version and gated by an activation height, exactly as the v2
reuse-entropy gate was introduced. Before the height, v1 and v2 proofs remain valid and v3
proofs are accepted under v2 rules. At and after the height, proofs MUST be v3 or later and
legacy versions are rejected with `bad-proof-version`. No change is made to the header format,
the VDF, or the block-hash derivation, so non-proof consensus is unaffected. Candidate A
additionally requires that models intended for use after activation carry a registered binding
alphabet.

## Activation parameters

- **Activation mechanism** — proof `version` byte bump to 3, combined with a per-network
  activation height `prompt_binding_height` and predicate `IsPromptBindingActive(height)` in
  `src/consensus/params.h`, set per network in `src/kernel/chainparams.cpp` — mirroring the
  existing `reuse_entropy_height` / `IsReuseEntropyActive()` precedent. Exact heights: TBD —
  Draft.
- **Minimum node version** — TBD — Draft.
- **Rollback statement** — until the activation height the rule is inert and a node enforcing
  it produces an identical view to one that does not; the height MAY be set to an unreachable
  value to disable the rule on a given network, as is done for the reuse-entropy gate on test
  networks. After lock-in the version floor is non-rollback, consistent with other consensus
  activations.
- **Monitoring / rollout plan** — operators SHOULD track the share of v3 proofs and the rate
  of `bad-prompt-binding` / `prompt-reused` rejections, and — for Candidate A — model readiness
  to publish a registered binding alphabet, ahead of the activation height.

## Reference implementation

TBD — Draft.

## Security considerations

The objective is to convert a deterred shortcut into one that is infeasible by construction:
under a binding rule, no forward pass underlying a v3 proof can be computed before the header
and VDF output exist, and the miner cannot select the low-entropy path because the bound input
is not under its control. These properties must be established without weakening the existing
gates or honest throughput. The analysis MUST resolve, before this leaves Draft:

- **Inert-alphabet integrity (A).** If alphabet members are not genuinely inert, injection can
  itself induce low-entropy continuations or semantic derailment; certification of `B` at
  registration is load-bearing and must be specified, not assumed.
- **Binding sufficiency (A).** Too few bound tokens, or a boundary cadence that minimally
  perturbs downstream logits, may leave a partial precomputation surface; the count and cadence
  MUST be chosen against measured logit sensitivity.
- **State and griefing (B).** The retention scope MUST bound consensus state growth, and the
  rule MUST resist an adversary front-running a victim's prompt commitment to deny its use.
- **Hot-path integrity (both).** No mechanism may force honest miners off the continuous
  autoregressive generation path; boundary-aligned injection and prompt-commitment checks MUST
  be expressible without mid-window interruption.
- **Vocabulary/model coupling (A).** The verifier and every independent engine MUST agree on
  `B` and `m` via the model identity, or proofs will diverge across implementations.

This document describes a forward hardening of attack surfaces already discussed publicly in
the project's verification analysis; it does not disclose an unmitigated weakness in the live
network and does not quantify any exploit.

## Test vectors

To be provided with the reference implementation: for a fixed
`(header_prefix, vdf, tick, B, m, domain_tag)` and boundary schedule, the expected injected
token sequence; a canonical prompt commitment `H`; and worked proofs that pass and
counterexamples that trigger `bad-prompt-binding` and `prompt-reused`.

## Copyright

This TIP is released into the public domain (CC0).
