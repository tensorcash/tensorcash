```
TIP: 0009
Title: Community verification service reference deployment
Author: takakuni <takakuni@tensorcash.org>
Type: Informational
Status: Draft
Created: 2026-07-20
Requires: 0008
Replaces: <none>
```

## Abstract

This TIP describes `verify.tensorcash.org` as a reference community verification
service for TensorCash clients. The service is a public coordinator and verdict
cache, not a consensus authority. Its first deployment phase exposes only
keyless, read-only public endpoints compatible with the existing hosted
verification status surface: health at `/healthz` and verdict-status lookup
under `/v1/public/*`. It does not expose authenticated submit endpoints and
therefore cannot start fresh model verification for a proof that no miner,
broker, or other authorized service has already submitted. A missing public
verdict is an `unknown`, not a negative result.

The longer-term deployment implements the verifier verdict attestation protocol
defined by the required Standards-Track TIP (TIP-0008). In that mode,
`verify.tensorcash.org` terminates client traffic, privately fans validation
subjects out to vetted community verifiers, aggregates signed verdicts with
Sybil-resistant quorum rules, and serves aggregate validation envelopes from a
rate-limited durable cache. The public surface exposes only aggregated answers
and aggregate model-coverage counts. Verifier addresses, live load, and
per-verifier model residency remain private to the coordinator.

## Motivation

Light clients, desktop clients, and nodes whose own verifier is temporarily
unavailable benefit from a default place to look for existing full-verification
results. Today that role is operationally similar to a single hosted verifier:
clients submit or poll one service and receive one answer. That is useful, but it
does not pool community GPU capacity, does not expose per-model coverage, and
does not give sovereign miners a clear way to distinguish one operator's answer
from a quorum of admitted verifiers.

The community service has two practical goals:

1. **A low-risk stopgap.** Expose the existing public verdict cache under a
   TensorCash community hostname with no API key and no secrets. This gives
   light clients and desktop defaults a stable read-only lookup service while
   keeping fresh submit paths closed.

2. **A path to pooled verification.** Evolve that endpoint into a coordinator
   that fans work out to vetted community verifiers and returns signed aggregate
   envelopes. This lets different providers hold different models in VRAM, makes
   per-model coverage observable, and bounds Sybil influence through registry and
   quorum rules.

This document is Informational. It describes one reference deployment of an open
protocol. It does not bless one coordinator as the only valid source of remote
verification.

## Specification

This is an Informational TIP. It specifies the intended behavior of the
`verify.tensorcash.org` reference deployment, but it does not introduce consensus
rules. RFC-2119 keywords are used to make the deployment contract precise.

### 1. Deployment phases

The service has two phases.

#### 1.1 Phase 0: keyless public lookup

Phase 0 exposes only read-only public endpoints:

```
GET /healthz
GET /v1/public/status/{hash}
```

The service MUST NOT expose authenticated submit endpoints under
`verify.tensorcash.org` in Phase 0. In particular, requests to paths such as:

```
POST /v1/verify/full/request/submit
POST /v1/verify/model/request/submit
POST /v1/verify/challenge/request/submit
```

MUST NOT be publicly routable through the Phase 0 hostname.

Phase 0 is implemented as a CloudFront distribution in front of the existing
verification origin. CloudFront provides a stable swap-later indirection point
for the public hostname and handles double-slash path normalization/mitigation
before requests reach the origin.

```
host = verify.tensorcash.org
forwarded paths = /healthz, /v1/public/*
allowed methods = GET, HEAD, OPTIONS
caching = disabled
origin = existing verification-service origin
tls = tensorcash.org certificate
```

No public API key, shared secret, or client credential is required for Phase 0.

The Phase 0 service is a public verdict lookup service. It can return a verdict
only if the corresponding proof or validation subject has already been verified
and cached or stored by an authorized path. It MUST treat a missing result as
`unknown` or `NAN`; it MUST NOT treat a missing result, submit rejection, 403, or
404 as `Full_Red`.

#### 1.2 Phase 1: signed community coordinator

Phase 1 implements the required verifier verdict attestation TIP. The service:

- receives client validation requests;
- privately routes them to admitted verifiers;
- collects signed verifier verdict envelopes;
- applies registry, committee, coverage, and quorum rules;
- stores aggregate validation envelopes durably;
- serves aggregate status from public rate-limited endpoints.

In Phase 1 the coordinator MAY expose submit endpoints, but those endpoints MUST
either require an authenticated quota or enforce a public anti-abuse policy. The
public no-key surface MAY remain lookup-only even after Phase 1 if the operator
chooses not to accept anonymous fresh work.

### 2. Public API surface

The public API surface MUST be deliberately small.

#### 2.1 Health

`GET /healthz` returns service health suitable for client endpoint selection. It
SHOULD include:

```
{
  "status": "ok",
  "service": "community-verification",
  "phase": "lookup-only",
  "registry_epoch": 12345
}
```

The response MUST NOT include worker addresses, private verifier identities,
verifier IPs, per-verifier load, or per-verifier residency.

#### 2.2 Status lookup

`GET /v1/public/status/{hash}` returns the best public status for a validation
subject.

In Phase 0, `{hash}` is the existing status key understood by the backing hosted
verification service. Implementations SHOULD migrate toward content-addressed
lookup by proof hash or `subject_digest`, but Phase 0 MAY retain the current
hash-key semantics for compatibility.

In Phase 1, `{hash}` SHOULD be a `subject_digest` or proof payload hash as
defined by the attestation TIP.

Responses MUST distinguish terminal verdicts from misses:

```
{
  "status": "unknown"
}
```

or:

```
{
  "status": "terminal",
  "aggregate_verdict": "Full_Green",
  "aggregate": {
    "...": "aggregate validation envelope"
  }
}
```

`unknown`, `pending`, `abstain`, rate-limited, unauthorized, and not-found
responses MUST NOT be interpreted by clients as `Full_Red`.

#### 2.3 Model coverage

Phase 1 SHOULD expose aggregate model-coverage information:

```
GET /v1/public/model-coverage
GET /v1/public/model-coverage?model_identifier=<model-name@commit>
```

The response SHOULD report aggregate counts only:

```
{
  "registry_epoch": 12345,
  "model_identifier": "model-name@commit",
  "providers_supported": 8,
  "providers_fit_for_full": 6,
  "providers_hot_gpu": 3,
  "workers_online": 11,
  "quorum_ready": true,
  "min_model_providers_fit_for_full": 5,
  "updated_at_unix_ms": 1784567890123
}
```

`providers_supported` counts distinct admitted provider identities that advertise
durable support for the model.

`providers_fit_for_full` counts distinct admitted provider identities that the
coordinator believes can execute full verification for the model within the
profile's resource limits.

`providers_hot_gpu` counts distinct admitted provider identities with the model
currently hot or quickly available on GPU. This is an availability hint only.
It MUST NOT be used as a security input.

`workers_online` is informational and MUST NOT be used for quorum independence.
The same provider may run many workers.

`quorum_ready` MUST be computed from distinct admitted providers and the
`min_model_providers_fit_for_full` profile value. A model below the minimum MUST
return `quorum_ready: false` rather than hiding under-coverage.

The endpoint MUST NOT expose per-verifier rows, IP addresses, network endpoints,
live queue depth, or per-verifier VRAM residency. Raw worker telemetry MUST stay
private to the coordinator.

### 3. Client behavior

#### 3.1 Desktop and light clients

Desktop and light clients MAY use `https://verify.tensorcash.org` as a default
public lookup endpoint. In Phase 0 they should treat it as a read-only verdict
cache, not as a full hosted validation backend.

Clients without an API key SHOULD:

- probe public status;
- use local quick and smell checks when available;
- retry or defer when full verification is missing;
- avoid converting missing public status into a terminal failure.

#### 3.2 Full and mining nodes

Mining nodes that operate their own verifier SHOULD prefer their own verifier.
The community service SHOULD be configured, if at all, as the last endpoint in a
failover list after the operator's own service.

In Phase 0, a mining node MUST NOT treat the lookup-only endpoint as a guarantee
that fresh full verification can be performed. It can only retrieve already
available public verdicts.

In Phase 1, a sovereign or mining node that accepts a community full-verification
verdict SHOULD require the aggregate validation envelope from the attestation TIP
and SHOULD verify the embedded verifier signatures against the registry snapshot
before accepting the remote verdict. A bare coordinator status or legacy enum is
not sufficient evidence for a sovereign node unless the operator explicitly opts
into that trust model.

### 4. Verifier onboarding and privacy

Community verifiers connect to the coordinator through a private verifier
channel. They advertise durable model support, hardware fit, liveness, and
capacity to the coordinator only.

The coordinator MAY use this private telemetry to route work efficiently, for
example by preferring verifiers that already have a requested model resident.
The coordinator MUST NOT expose the raw telemetry publicly.

The public registry used for signature verification may include admitted signing
public keys or epoch-pseudonymous keys, provider identities, weights, and
durable `models_supported`. This is necessary for aggregate verification. It
MUST NOT include network addressability, live load, or live residency.

### 5. Rate limits and abuse control

Public endpoints MUST be rate limited. Phase 0 SHOULD reuse the backing hosted
service's token-bucket limits. Phase 1 SHOULD maintain separate budgets for:

- anonymous status lookup;
- model coverage lookup;
- authenticated submit;
- administrative registry access.

Rate limiting MUST fail closed for abuse but SHOULD return clear retry semantics
so clients can distinguish rate limiting from a negative verdict.

### 6. Caching and durability

Terminal aggregate verdicts SHOULD be stored durably so public status lookup
survives process restarts and short cache eviction. A stored terminal result MUST
NOT be overwritten by a later Amber, timeout, pending, or failed worker result.

Amber, timeout, pending-review, and failed worker states MAY be cached for
backoff and observability, but MUST NOT be served as terminal full-verification
results.

### 7. Operational monitoring

The coordinator SHOULD store:

- signed verifier verdict envelopes;
- aggregate envelopes;
- red-disagreement evidence;
- equivocation evidence;
- canary results;
- verifier availability and latency;
- aggregate per-model coverage history.

Public monitoring SHOULD expose service-level and aggregate model-level health.
Per-verifier operational internals SHOULD remain private unless a verifier is
being publicly evicted or slashed under the rules of the registry.

### 8. Naming

This TIP uses `verify.tensorcash.org` for the reference deployment. If the
project chooses to use `verify.tensorcash.io` or another hostname instead, this
TIP SHOULD be updated before publication. The hostname is a reference deployment
name, not part of consensus.

## Rationale

Phase 0 is intentionally small. A host-and-path rule that exposes only
`/v1/public/*` gives clients a keyless public lookup endpoint without making any
authenticated submit path public and without distributing API keys in desktop
software. It is honest about its limit: it can only serve results that already
exist.

Phase 1 keeps the coordinator thin. The coordinator routes, aggregates, caches,
and monitors; community verifiers do the model work. This matches the resource
goal: not every verifier has to keep every model resident in VRAM, and the fleet
can cover more models than any one machine.

The public model-coverage endpoint reports aggregate provider counts because
coverage is meaningful to users and operators. It avoids per-verifier details
because those details make niche models easier to DoS and reveal operational
information that is not needed for client decisions.

The service is framed as a reference deployment rather than a blessed authority.
The attestation protocol should support multiple coordinators over time.

## Backwards compatibility

Phase 0 is compatible with current clients that already understand public status
lookup. Clients with no API key should continue to skip submit and poll public
status. Submit attempts routed to `verify.tensorcash.org` in Phase 0 will fail or
not route; clients MUST handle that as unavailable hosted submit, not as proof
failure.

Existing local verifier deployments are unaffected. Existing authenticated hosted
services may continue to expose their full submit-and-poll API under their own
hostnames.

Phase 1 is additive when implemented with the attestation TIP. Older clients may
ignore aggregate envelopes and read legacy statuses; newer clients can require
aggregate verification.

## Activation parameters

N/A. This Informational TIP describes a service deployment and does not activate
consensus rules.

If client packages change their default endpoint to `verify.tensorcash.org`, the
release notes SHOULD state that the default is lookup-only until Phase 1 submit
support is explicitly enabled.

## Reference implementation

TBD.

Expected implementation areas:

- CloudFront distribution, DNS alias, and origin behavior for Phase 0;
- public status and `/healthz` route verification in the hosted verification service;
- client fallback tests for no-key lookup-only mode;
- aggregate envelope support after the attestation TIP implementation;
- aggregate model-coverage endpoint;
- private verifier registration and telemetry channel.

## Security considerations

**Lookup-only confusion.** The main Phase 0 risk is product confusion. A public
status endpoint is not a submit-and-verify backend. Documentation and client
behavior MUST treat missing status as unknown.

**False negative from missing status.** Clients MUST NOT map 403, 404, rate
limit, timeout, `unknown`, `NAN`, or `pending` to `Full_Red`.

**Centralization.** A default hostname can become a de facto authority. This TIP
therefore frames the hostname as a reference deployment and relies on the
attestation TIP for portable verifier evidence.

**Coordinator censorship.** A coordinator can decline to serve, delay, or censor
requests. In Phase 0 there is no fresh work path to censor through the public
host; in Phase 1 clients can mitigate by using more than one coordinator when
available.

**Verifier targeting.** Public per-verifier residency or load would help an
attacker target providers for niche models. Public coverage MUST be aggregate
only.

**Sybil coverage inflation.** Counts MUST be over admitted provider identities,
not worker processes or endpoints.

**Privacy.** Fresh validation requests in Phase 1 expose proof material to the
selected verifier committee. Sovereign miners should prefer their own verifier
and use the community service only as an explicit fallback unless they accept
that exposure.

**Trust limitation.** The community service improves availability and
decentralizes trust in the model verdict. It does not make remote full
verification trustless. A valid aggregate envelope proves that admitted verifiers
signed a verdict, not that the node re-executed the model.

## Test vectors

N/A for Phase 0.

Before Phase 1 is treated as security-sensitive infrastructure, the deployment
SHOULD publish integration tests for:

- no-key public status lookup;
- submit blocked on the public hostname;
- missing status handled as unknown;
- Amber not served as terminal;
- public rate-limit behavior;
- model coverage aggregation over provider identities;
- aggregate envelope verification using the required attestation TIP vectors.

## Copyright

This TIP is released into the public domain (CC0), unless stated otherwise.
