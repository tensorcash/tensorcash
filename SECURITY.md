# Security Policy

TensorCash is a layer-1 network. A vulnerability in consensus, the wallet, the
cryptography, or the proof-of-inference path can put funds or network integrity
at risk. Please disclose responsibly.

## Reporting a vulnerability

- **Email:** `security@tensorcash.org`
- **Encryption:** PGP key fingerprint `TBD` (publish the key in this file before
  the first release; encrypt reports that contain exploit detail).
- **Do not** open a public GitHub issue, pull request, or discussion for a
  suspected vulnerability, and do not disclose it publicly until a fix has
  shipped and the coordinated window has elapsed.

Please include: affected component and version/commit, a description of the
issue and its impact, and reproduction steps or a proof of concept where
possible.

## In scope

- Consensus and validation (`bcore`): script/opcode handling, asset and ICU
  rules, the ZK/KYC enforcement path, PoW/proof-of-inference and VDF
  verification, difficulty and economic parameters.
- Wallet and key handling, the cosign bridge, and the post-quantum signature
  path.
- The verification and miner services where a flaw can affect consensus
  outcomes or funds.

Out of scope: issues in unmodified upstream dependencies (report those
upstream), denial-of-service that requires implausible resources, and purely
cosmetic website issues.

## Coordinated disclosure

- We aim to acknowledge a report within **72 hours** and to provide an initial
  assessment within **7 days**.
- We follow a coordinated-disclosure window (typically up to **90 days**, sooner
  if a fix is ready and deployed, longer for changes that require a coordinated
  network upgrade).
- With your consent we credit reporters in the release notes.

## Handling of consensus issues

Consensus-critical fixes may ship quietly first and be disclosed after the
network has upgraded, to avoid putting live funds at risk. Where a fix changes
consensus rules, it is tracked as a TIP (see [`tips/README.md`](tips/README.md))
once disclosure is safe.
