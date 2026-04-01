```
TIP: XXXX            (leave as XXXX; the editor assigns the number)
Title: <concise title>
Author: <name or pseudonym> <contact>
Type: Standards Track | Informational | Process
Status: Draft
Created: YYYY-MM-DD
Requires: <TIP numbers, if any>
Replaces: <TIP numbers, if any>
```

## Abstract

A short (≈200 word) technical summary of the change.

## Motivation

Why the existing protocol is insufficient. What problem this solves, and for
whom. For consensus changes, state the threat or limitation concretely.

## Specification

The normative part. Be precise enough that an independent implementation could
be written from this section alone:

- exact wire/TLV/opcode formats (byte layouts, field sizes, endianness);
- validation rules and the reject codes they produce;
- new RPCs or interfaces and their semantics;
- edge cases and failure behaviour.

Use MUST/SHOULD/MAY (RFC 2119) for normative requirements.

## Rationale

Why this design over the alternatives considered. Note prior art and any
contentious decisions and how they were resolved.

## Backwards compatibility

How this interacts with existing nodes, wallets, and assets, and what happens to
nodes that do not upgrade.

## Activation parameters

**Required for consensus Standards-Track TIPs** (otherwise state "N/A"):

- **Activation mechanism** — block height, median-time-past, or
  versionbits/BIP9-style signalling, with the exact threshold and window.
- **Minimum node version** required to enforce the new rules.
- **Rollback statement** — explicitly state whether activation is reversible.
  Consensus activations are normally non-rollback; if so, say so and justify.
- **Monitoring / rollout plan** — what is watched during activation (signalling,
  fork detection, upgrade adoption) and the abort/response plan if something goes
  wrong before lock-in.

Defining these is what distinguishes "implementation merged" from "network
safely activated."

## Reference implementation

Link to the PR(s) implementing this TIP. Standards-Track TIPs must have one
before reaching **Active**.

## Security considerations

New attack surface, failure modes, and how they are mitigated. Required for
every TIP; for consensus changes, this is the most scrutinised section.

## Test vectors

Where applicable, golden vectors sufficient to validate an independent
implementation.

## Copyright

This TIP is released into the public domain (CC0), unless stated otherwise.
