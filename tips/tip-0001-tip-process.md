```
TIP: 0001
Title: TIP purpose and guidelines
Author: takakuni <takakuni@tensorcash.org>
Type: Process
Status: Active
Created: 2026-04-01
```

## Abstract

A TensorCash Improvement Proposal (TIP) is a design document that specifies a
change to the TensorCash protocol, its consensus rules, or its economic
parameters, or that documents a process or convention for the project. This TIP
defines what a TIP is, the proposal lifecycle, and the responsibilities of
authors, reviewers, and the TIP editor. It is the foundational Process TIP and
is itself Active.

## Motivation

TensorCash is a layer-1 network: changes to consensus, the wire protocol, or the
economics must be agreed on by independent node operators and implementations,
not merged on the strength of a single pull request. A lightweight but explicit
proposal process gives those changes a stable specification, a public review
record, and a clear activation path — and keeps them distinct from ordinary code
changes, which do not warrant the same ceremony.

## Specification

- A change MUST be accompanied by a TIP if it falls into any category listed in
  [`README.md`](README.md) ("When a TIP is required"). Other changes MUST NOT be
  blocked on a TIP.
- A TIP MUST follow [`TEMPLATE.md`](TEMPLATE.md) and carry the header fields
  defined there.
- TIPs are numbered sequentially. The editor assigns the number when a draft is
  coherent and non-duplicative; the author leaves `XXXX` until then.
- Statuses and transitions are as defined in `README.md`. A Standards-Track TIP
  MUST have a reference implementation before it can become **Active**, and a
  consensus-affecting TIP MUST define its activation and backwards-compatibility
  plan.
- Editorial merge of a TIP document records it for discussion; it is NOT an
  endorsement. Acceptance is established through review ACKs and, for consensus
  changes, network adoption.
- Discussion and review use the ACK/NACK convention in `CONTRIBUTING.md`.

## Rationale

The model follows Bitcoin's BIP process (BIP-1/BIP-2) because it is well
understood and battle-tested for a consensus system, but is deliberately
trimmed: a single editor, a small status set, and no separate proposals
repository while the maintainer set is small. The structure scales — more
editors and a dedicated repository can be added later via a Process TIP — without
imposing bureaucracy on a young project.

## Backwards compatibility

None. This TIP introduces a process and does not affect consensus, existing
nodes, or wallets.

## Security considerations

The process itself is a security control: it forces consensus changes through a
written specification, public review, a reference implementation, and an
explicit activation plan, reducing the risk of an under-reviewed rule change
reaching the live network. Vulnerability handling is governed by
[`../SECURITY.md`](../SECURITY.md); consensus fixes may be specified as a TIP
only once disclosure is safe.

## Copyright

This document is released into the public domain (CC0).
