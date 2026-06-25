# TensorCash Improvement Proposals (TIPs)

A **TIP** is a design document that describes a change to the TensorCash
protocol, its consensus rules, or its economic parameters — anything that
multiple independent implementations or node operators must agree on. TIPs are
the analogue of Bitcoin's BIPs, adapted to TensorCash.

Ordinary code changes do **not** need a TIP and go through the normal pull
request flow in [`../CONTRIBUTING.md`](../CONTRIBUTING.md). A TIP is the extra,
heavier gate reserved for protocol-level change.

## When a TIP is required

- Consensus or validation rule changes; new/changed script opcodes or reject codes.
- Asset / TLV wire formats, ICU acceptance records, option-series encoding.
- Proof-of-inference / proof-of-work, the VDF, or block/header format.
- Economic parameters: genesis, total supply, block reward, difficulty retargeting.
- Cross-chain settlement and other inter-party protocol surface.
- Signature-scheme / post-quantum changes.

If in doubt, open a draft TIP and ask — it's cheaper than re-litigating a merged PR.

## TIP types

- **Standards Track** — any change affecting consensus, the wire protocol, or
  interoperability between implementations.
- **Informational** — design guidance or conventions; does not mandate anything.
- **Process** — changes to a process around the project (this document is one).

## Statuses and lifecycle

```
Draft ──► Proposed ──► Active        (Standards/Process that are adopted)
  │           │     └─► Final        (Informational, once stable)
  │           └───────► Rejected / Withdrawn
  └─ Replaced (superseded by a later TIP)
```

- **Draft** — under discussion; may change freely.
- **Proposed** — author considers it complete; has a reference implementation or
  a concrete plan for one; seeking ACKs.
- **Active / Final** — accepted. *Active* for Standards/Process that govern the
  live network; *Final* for Informational that have stabilised.
- **Rejected / Withdrawn / Replaced** — closed; kept for the historical record.

## Workflow

1. **Draft.** Copy [`TEMPLATE.md`](TEMPLATE.md) to `tip-XXXX-short-title.md`
   (leave `XXXX` as the placeholder), fill it in, and open a pull request to
   this `tips/` directory.
2. **Discussion.** Reviewers use the ACK/NACK convention. The TIP editor checks
   the proposal is complete, technically coherent, and not a duplicate.
3. **Number assignment.** Once the draft is coherent, the editor assigns the
   next free TIP number and the PR is merged with status **Draft**.
4. **Reference implementation.** Standards-Track TIPs need a working reference
   implementation, submitted as ordinary PR(s) against the relevant repo
   (usually `bcore`). That PR follows the full review + CI gate and references
   the TIP. The TIP moves to **Proposed**.
5. **Activation.** When the implementation is merged and (for consensus changes)
   any activation mechanism is defined, the TIP becomes **Active**. Consensus
   activations are accompanied by a GPG-signed release tag.

A TIP that changes consensus is not "done" when merged — it is done when the
network can adopt it safely. State the activation/backwards-compatibility plan
explicitly.

## Acceptance criteria

A Standards-Track TIP is **accepted** — the gate the pull-request template refers
to — when all of the following hold:

- the TIP document is merged (at least as **Draft**) and is complete per the template;
- it has sufficient review ACKs and no unresolved NACKs;
- a reference implementation has passed review and CI;
- for consensus TIPs, the **Security considerations** and **Activation parameters**
  sections are filled and the activation mechanism is defined;
- the editor records the status transition.

The implementation PR and the TIP are reviewed together — submitting an
implementation is often how a TIP reaches **Proposed**. The gate does not forbid
an implementation PR existing; it forbids *merging or activating* a consensus
change before the criteria above are met.

## Editors

The TIP editor assigns numbers, enforces the template and process, and merges
TIP documents (not the implementations). Editorial merge is not an endorsement
of the idea — that comes from review ACKs and, ultimately, network adoption.

- Current editor: **@takakuni**

## Index

| TIP | Title | Type | Status |
|----:|-------|------|--------|
| [0001](tip-0001-tip-process.md) | TIP purpose and guidelines | Process | Active |
| [0002](tip-0002-scalar-settlement.md) | Issuer-published scalar settlement, non-native collateral, and two-sided securitisation | Standards Track | Draft |
| [0003](tip-0003-prompt-chaining.md) | Header-Bound Prompt Commitment (Prompt Chaining) | Standards Track | Draft |
| [0004](tip-0004-operator-blindness.md) | Model-operator blindness in verifiable proof-of-inference | Informational | Draft |
