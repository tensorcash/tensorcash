# Contributing to TensorCash

Thank you for considering a contribution. TensorCash is a layer-1 network with
live consensus rules, so this project distinguishes between **code changes**
(handled as ordinary pull requests) and **protocol changes** (which require a
**TensorCash Improvement Proposal — TIP**, see below).

This document is the canonical contribution guide for the whole project. The
component repositories (`bcore`, `vllm`, `llama.cpp`, `vllm-production-stack`,
`website`, `gnark`) point back here.

## Ground rules

- **`main` is protected.** Nobody pushes to it directly — every change lands via
  a reviewed pull request with green CI. Maintainers are not exempt.
- **History is linear.** No merge commits, no force-pushes to protected
  branches, no branch/tag deletion.
- **CI must be green.** All required checks (build + test) run on the umbrella
  repository and must pass before merge, including when a PR only bumps a
  submodule pointer (CI re-checks out the pinned submodule commit and rebuilds).

## Developer workflow

1. Fork the relevant repository and create a topic branch
   (`feature/...`, `fix/...`).
2. Keep changes focused; one logical change per pull request.
3. Match the surrounding code style. Add or update tests for any behaviour change.
4. Open a pull request against `main` and fill in the PR template.
5. Address review feedback by adding commits (don't force-push during review;
   maintainers will squash on merge if needed).

### Commit hygiene

- Write clear, imperative commit subjects (`Add ...`, `Fix ...`), with a body
  explaining *why* when it isn't obvious.
- **Sign off every commit** (Developer Certificate of Origin): `git commit -s`,
  which adds a `Signed-off-by:` trailer asserting you have the right to submit
  the work under the project licence.
- **Maintainers** additionally use **GPG-signed commits and signed release
  tags** (`git commit -S`, `git tag -s`). Signed tags are required for releases
  and for any TIP that activates a consensus change.

## Review and the ACK convention

Review follows the Bitcoin Core convention. When commenting on a PR, lead with:

- **Concept ACK** — you agree with the idea, haven't reviewed the code yet.
- **Approach ACK** — you agree with the implementation approach.
- **ACK `<commit-hash>`** — you reviewed the code at that commit and it looks
  correct (note if you also tested: "tested ACK").
- **utACK `<commit-hash>`** — untested ACK (code review only).
- **NACK `<reason>`** — you object; a NACK must include a concrete technical
  reason.

A maintainer merges only when a change has sufficient ACKs, no unresolved NACKs,
required `CODEOWNERS` review, and green CI. Consensus-affecting changes require
heightened review (see the TIP process).

## When you need a TIP

Open a **TIP** *before* (or alongside) the implementation PR if your change
touches any of:

- consensus/validation rules, script opcodes, or reject codes;
- the asset / TLV wire formats, ICU acceptance, or option-series encoding;
- proof-of-inference / proof-of-work, the VDF, or block format;
- economic parameters (genesis, supply, reward, difficulty retargeting);
- cross-chain settlement or other inter-party protocol surface;
- post-quantum / signature scheme changes.

You do **not** need a TIP for bug fixes that don't change consensus, service or
website code, tooling, tests, or documentation. See [`tips/README.md`](tips/README.md)
for the full process and [`tips/TEMPLATE.md`](tips/TEMPLATE.md) for the template.

## Security issues

**Do not** open public issues or PRs for vulnerabilities. Follow the coordinated
disclosure process in [`SECURITY.md`](SECURITY.md).

## Licensing of contributions

Unless you explicitly state otherwise, any contribution you intentionally submit
for inclusion in the project is licensed under the **Apache License, Version
2.0** (this mirrors Apache-2.0 §5). The Developer Certificate of Origin sign-off
(`git commit -s`) asserts you have the right to submit the work under that
license. Components distributed under a different license (see
[`LICENSING.md`](LICENSING.md)) take contributions under their own stated terms.

## Code of conduct

Participation is governed by [`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md).
