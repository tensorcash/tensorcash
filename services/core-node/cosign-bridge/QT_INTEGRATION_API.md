# Qt Integration API - Cosign Bridge

**Protocol:** newline-delimited JSON over stdio

---

## Overview

The TensorCash Cosign Bridge is a standalone process that the Bitcoin Core Qt
wallet drives over stdio. It runs the SPAKE2 + Noise cosigning ceremony between
two parties so they can exchange PSBTs, signatures, and other payloads over an
end-to-end encrypted channel without trusting the transport. This document
describes the wire contract, the command surface, the data formats, and the
error model that a Qt integration depends on.

### Transport Modes

The bridge supports two peer-to-peer transport modes:

#### 1. WebSocket (relay-based)
- **Requires:** a WebSocket relay server (e.g. `wss://relay.tensorcash.org`).
- **Trade-offs:** easy to set up and works behind NAT/firewalls, but the relay
  sees connection metadata and must be reachable by both peers.
- **Best for:** quick setup, mobile wallets, restrictive networks.

#### 2. Tor hidden services (relay-free)
- **Requires:** a system Tor daemon running locally.
- **Trade-offs:** no relay server, peer IP addresses hidden, slower initial
  connection while the circuit builds.
- **Best for:** privacy-focused use with no trusted third party.

The default transport is WebSocket. When a session is created with
`transport: "auto"`, the bridge resolves it to WebSocket.

### Wire Protocol

**Transport:** stdio (stdin/stdout)
**Framing:** one JSON object per line (newline-delimited)
**Encoding:** UTF-8

The bridge reads one request object per line from stdin and writes exactly one
response object per line to stdout. This is **not** JSON-RPC: there is no
`jsonrpc`/`id` envelope, no `method` field, and no structured `result`/`error`
object.

**Request format:**

```json
{
  "command": "init",
  "params": {
    "transport": "websocket"
  }
}
```

- `command` is the **bare** command name. The bridge dispatches on this string
  verbatim (`stdio.rs`, the `match command` block) — there is no `cosign.`
  prefix and none is stripped. Sending `"command": "cosign.init"` is rejected as
  an unknown command.
- `params` is an arbitrary JSON object (defaults to `{}` when omitted). Most
  commands read named keys from it; a few also accept positional array form.

**Response format:**

```json
{
  "field1": "value1",
  "field2": "value2"
}
```

The response is the command's result object **flattened at the top level** — its
keys (e.g. `session_id`, `sas`, `ok`) appear directly on the response object. On
failure, the bridge instead returns a top-level `error` key whose value is a
**human-readable string**:

```json
{
  "error": "Session not found: session_123"
}
```

There is no numeric error code and no nested `error` object. The presence of the
`error` key is the failure signal; on success the `error` key is absent. A
parse failure on an unreadable input line also produces `{"error": "Invalid
JSON: ..."}`.

**Qt detection pattern:**

```cpp
QJsonObject response = sendBridgeRequest(request);
if (response.contains("error")) {
    QString message = response["error"].toString();
    // handle failure (see Error Handling)
} else {
    // success: read result fields directly off `response`
}
```

---

## Command Surface

The bridge dispatches a single flat set of commands (`stdio.rs`,
`handle_command`). The cosigning-session commands are:

| Command | Purpose |
|---------|---------|
| `version` | Bridge/API version and build info |
| `ping` | Liveness, uptime, advertised capabilities |
| `init` | Create a session and produce an invite (initiator) |
| `join` | Join a session from an invite link (responder) |
| `handshake` | Begin the SPAKE2 exchange (manual flow) |
| `handshake_finish` | Complete SPAKE2 and start Noise (manual flow) |
| `handshake_complete` | Process the peer's Noise message (manual flow) |
| `handshake_auto` | Run the full handshake over the live transport |
| `attest` | Optional BIP-322 address attestation |
| `send` | Send an encrypted payload to the peer |
| `recv` | Receive a decrypted payload from the peer |
| `status` | Current session state and counters |
| `close` | Terminate a session and free resources |
| `resume` | Replay buffered messages after a reconnect |
| `metrics` | Process-level metrics |

Beyond the session commands, the same dispatcher also exposes a bulletin-board
marketplace surface (`init_bb`, `post_offer`, `list_offers`, `request_trade`,
`accept_request`, discussion and governance commands) and a cross-chain/ETH
adapter surface (`eth_init`, `eth_lock_htlc`, `eth_claim_htlc`, …). Those
families are out of scope for this document, which covers the two-party
cosigning ceremony.

---

### `version`

Returns the bridge's API and build identity. Takes no parameters.

**Request:**
```json
{ "command": "version", "params": {} }
```

**Response:**
```json
{
  "api_version": 1,
  "git_commit": "0.1.0",
  "build_flags": ["noise", "spake2"],
  "bridge_version": "0.1.0"
}
```

`api_version` is the integer contract version a wallet checks for
compatibility. `build_flags` lists the crypto features compiled in.

---

### `ping`

Confirms the bridge is alive and reports uptime and advertised capabilities.
Takes no parameters.

**Request:**
```json
{ "command": "ping", "params": {} }
```

**Response:**
```json
{
  "bridge_alive": true,
  "version": "0.1.0",
  "transports": ["ws"],
  "uptime_sec": 3600,
  "capabilities": ["resume", "send_multi", "bip322"]
}
```

Use the absence of an `error` key and `bridge_alive: true` as the health signal
before starting a ceremony.

---

### `init` (initiator)

Creates a new cosigning session and produces an invite for the other signer.

**Parameters:**
- `transport` (optional) — `"websocket"` (default), `"tor"`, or `"auto"`
  (resolves to WebSocket).
- `relay_url` (optional, WebSocket) — overrides the relay. If omitted, the
  bridge uses the `COSIGN_RELAY_URL` environment variable, then a built-in
  default relay list.
- `ttl` (optional) — session lifetime in seconds.

**Request:**
```json
{
  "command": "init",
  "params": { "transport": "websocket" }
}
```

**Response:**
```json
{
  "session_id": "...",
  "invite_link": "cosign:?r=<room>&t=websocket&h=<handshake_id>#c=alpha-bravo-charlie-delta-echo",
  "invite_code": "alpha-bravo-charlie-delta-echo",
  "qr_data": "cosign:?r=<room>&t=websocket&h=<handshake_id>#c=alpha-bravo-charlie-delta-echo",
  "qr_error_correction": "M",
  "sas": "quake-fail-lax-bomb-lobe",
  "sas_numeric": "...",
  "transport_selected": "websocket",
  "transport": "websocket",
  "relay_url": "wss://..."
}
```

For Tor sessions, `r=` carries the freshly-created `.onion` address and
`t=tor`. The `invite_code` is five words drawn from the EFF wordlist; it is the
shared SPAKE2 password and must be exchanged over a channel the peer trusts.

**Note on `sas`:** the `sas` field returned by `init` is computed from this
side's `session_id` and is **not yet** the verifiable SAS. The two sides only
derive a matching SAS from the handshake transcript after the handshake
completes. Verify the SAS that `handshake_auto` returns, not the one from
`init`.

**UI guidance:**
- Render `invite_link` as a QR code (use `qr_data` directly) and also show the
  5-word `invite_code` for manual entry.
- Offer a transport choice (WebSocket vs Tor); for Tor, warn if the daemon is
  not running.
- Store `session_id` for all subsequent commands.

---

### `join` (responder)

Joins an existing session from an invite link. The invite link is required —
the responder cannot derive the handshake nonce (`h=`) from the 5-word code
alone, and `recv` drops any frame that lacks the matching nonce.

**Parameters:**
- `invite_link` (required) — the full `cosign:?…` link from the initiator.

**Request:**
```json
{
  "command": "join",
  "params": {
    "invite_link": "cosign:?r=<room>&t=websocket&h=<handshake_id>#c=alpha-bravo-charlie-delta-echo"
  }
}
```

**Response:**
```json
{
  "session_id": "...",
  "sas": "quake-fail-lax-bomb-lobe",
  "transport": "websocket"
}
```

**Invite link format:** `cosign:?r=<room_or_onion>&t=<transport>&h=<handshake_id>#c=<invite_code>`
- `r=` — relay room id (WebSocket) or `.onion` address (Tor).
- `t=` — `websocket` or `tor`.
- `h=` — per-handshake nonce (hex); used by `recv` to drop stale or
  self-echoed frames.
- `#c=` — the 5-word invite code (a URL fragment, so it must come last).

---

### Handshake

The handshake establishes the encrypted channel: a SPAKE2 password exchange
(keyed by the invite code) followed by a Noise `NNpsk0` handshake. There are two
ways to drive it.

#### Automatic — `handshake_auto`

Runs the entire exchange over the live transport (WebSocket or Tor) and returns
when the channel is established. This is the recommended path.

**Parameters:** `session_id` (required), `is_initiator` (bool).

**Response:**
```json
{
  "handshake_complete": true,
  "sas": "quake-fail-lax-bomb-lobe"
}
```

The `sas` returned here is the transcript-derived SAS — the value both sides
must compare out-of-band.

#### Manual — `handshake` / `handshake_finish` / `handshake_complete`

For transports where the wallet relays handshake bytes itself (e.g. via QR
codes), the exchange is driven in steps:

1. `handshake` (`session_id`, `is_initiator`) → `{ "spake2_message": "<hex>",
   "state": "awaiting_peer_spake2" }`. Send `spake2_message` to the peer.
2. `handshake_finish` (`session_id`, `peer_spake2_message`, `is_initiator`) —
   completes SPAKE2 and initializes Noise. The initiator receives
   `{ "noise_message": "<hex>", "state": "awaiting_peer_noise" }`; the responder
   receives `{ "state": "awaiting_peer_noise" }` (it writes only after reading,
   per `NNpsk0`).
3. `handshake_complete` (`session_id`, `peer_noise_message`) → processes the
   peer's Noise message and returns
   `{ "handshake_complete": true, "response_message": "<hex>|null" }`. A non-null
   `response_message` must be sent to the peer to finish their side.

All handshake messages are hex-encoded.

---

### SAS verification

There is no separate command to fetch the SAS — it is returned inline by `init`,
`join`, and `handshake_auto`. The SAS from `init`/`join` is provisional; only the
value `handshake_auto` returns is regenerated from the completed handshake
transcript and is the one to verify. After the handshake completes, both parties
compare that SAS out-of-band (voice, video, or in person). The SAS is five words,
enough to detect a man-in-the-middle. If the strings do not match, abort the
session and start over.

**UI guidance:**
- Display the SAS prominently with clear "Codes match" / "Codes don't match"
  actions.
- Instruct the user to confirm the code over a separate channel.
- On mismatch, terminate the session immediately.

---

### `attest` (optional)

Binds a wallet address to the session via a BIP-322 challenge, so each side can
prove control of a key. Called in two steps:

1. With `session_id` + `address` (no signature) → returns
   `{ "challenge": "cosign|<session_id>|<sas>" }`.
2. With `session_id` + `address` + `signature` over that challenge → returns
   `{ "verified": true, "peer": { "address": "<address>" } }`.

---

### `send`

Encrypts a payload and sends it to the peer over the live transport. Requires a
completed handshake.

**Parameters:**
- `session_id` (required).
- `payload` (required) — an arbitrary JSON value. It is serialized and Noise-
  encrypted; it is **not** base64 and is not interpreted by the bridge.

**Request:**
```json
{
  "command": "send",
  "params": {
    "session_id": "...",
    "payload": { "psbt": "cHNidP8B..." }
  }
}
```

**Response:**
```json
{ "ok": true, "seq": 5 }
```

`seq` is the per-session message sequence number. The ciphertext is already on
the wire; the caller does not handle it.

**Limits:** the session enforces a 10 msg/sec rate limit and a 5 MB per-session
bandwidth budget. Exceeding either returns an `error` string
(`COSIGN_RATE_LIMIT: …` or `COSIGN_PAYLOAD_BUDGET_EXCEEDED: …`).

---

### `recv`

Receives and decrypts the next payload from the peer. Requires a completed
handshake.

**Parameters:**
- `session_id` (required).
- `timeout_ms` (optional) — how long to wait for a frame.

**Response (payload available):**
```json
{ "payload": { "psbt": "cHNidP8B..." } }
```

**Response (no frame within the timeout):**
```json
{ "timeout": true }
```

An empty object `{}` is returned when the transport produced nothing to deliver.
`recv` returns at most one payload per call; poll it to drain a stream.

If a Noise AEAD decrypt fails, the session is marked poisoned and the bridge
returns `{"error": "COSIGN_SESSION_DESYNCED: ..."}`. Once poisoned, all further
`send`/`recv` on that session fail the same way — the wallet must abandon the
session and restart the ceremony.

**Polling guidance:** poll every 1–2 seconds while awaiting the peer, stop once
the expected payload arrives, and apply an overall ceremony timeout.

---

### `status`

Returns the current session state and counters.

**Parameters:** `session_id` (required).

**Response:**
```json
{
  "state": "open",
  "peer_verified": false,
  "messages_sent": 5,
  "messages_received": 3,
  "age_sec": 60,
  "ttl_sec": 1800,
  "transport": "websocket",
  "relay_url": "wss://...",
  "room_id": "...",
  "onion_address": null,
  "bandwidth_used_bytes": 12345,
  "bandwidth_remaining_bytes": 5230979
}
```

---

### `resume`

After a transient disconnect within the recovery window, replays buffered
messages so the ceremony can continue without restarting.

**Parameters:** `session_id` (required), `from_seq` (optional, default 0).

**Response:**
```json
{
  "missed_messages": [
    { "seq": 4, "timestamp": 1609459200, "payload": { "...": "..." } }
  ],
  "current_seq": 5,
  "buffer_size": 2,
  "recoverable": true
}
```

A session outside the recovery window returns
`{"error": "COSIGN_SESSION_UNRECOVERABLE: ..."}`.

---

### `close`

Terminates a session and tears down its transports.

**Parameters:** `session_id` (required).

**Response:**
```json
{ "ok": true }
```

Always close sessions when the ceremony finishes, on cancel, or on a fatal
error.

---

### `metrics`

Returns process-level metrics. Takes no parameters.

**Response:**
```json
{
  "active_sessions": 2,
  "total_messages": 0,
  "bridge_restarts": 0,
  "transport_failures": { "ws": 0 },
  "avg_latency_ms": 42,
  "p95_latency_ms": 85,
  "p99_latency_ms": 150
}
```

---

## Tor Setup and Configuration

To use the Tor transport, a system Tor daemon must be installed and running. The
bridge creates a fresh hidden service when a session is initialized with
`transport: "tor"`.

### Installation

**Linux (Debian/Ubuntu):**
```bash
sudo apt-get update
sudo apt-get install tor
tor --version
```

**macOS:**
```bash
brew install tor
brew services start tor
```

**Windows:** install the Tor Expert Bundle from
<https://www.torproject.org/download/tor/>, or use the Tor daemon embedded in
Tor Browser.

### Verifying the SOCKS5 proxy

```bash
# Default Tor SOCKS5 proxy on localhost:9050
curl --socks5 localhost:9050 https://check.torproject.org/api/ip
```

If Tor is not available, `init` with `transport: "tor"` returns
`{"error": "Tor init failed: ..."}`. A Qt integration can probe for Tor support
by attempting a Tor `init` and inspecting the `error` string, then closing the
session if it succeeded.

### WebSocket vs Tor

| Property | WebSocket | Tor |
|----------|-----------|-----|
| Setup | None | Requires Tor daemon |
| Metadata | Visible to relay | Hidden |
| Third party | Requires a relay server | None |
| Connection time | Fast (<1s) | Slower (circuit build) |
| NAT/firewall | Works everywhere | Works everywhere |

---

## Error Handling

Every failure is reported as a top-level `error` string. There are no numeric
error codes; branch on message content where you need to distinguish cases.

Representative error strings (from `BridgeError`):
- `Invalid JSON: ...` — the input line was not valid JSON.
- `Invalid command: <name>` — unknown command (e.g. a stale `cosign.`-prefixed
  name).
- `Session not found: <id>` — unknown or expired session.
- `Invalid parameters: ...` / `Invalid <cmd> params: ...` — missing or
  mistyped fields.
- `COSIGN_HANDSHAKE_REQUIRED: ...` — `send`/`recv` before the handshake
  completed.
- `COSIGN_RATE_LIMIT: ...` — more than 10 msg/sec.
- `COSIGN_PAYLOAD_BUDGET_EXCEEDED: ...` — past the 5 MB session budget.
- `COSIGN_SESSION_DESYNCED: ...` — a Noise decrypt failed; the session is
  poisoned and must be abandoned.
- `COSIGN_SESSION_UNRECOVERABLE: ...` — `resume` outside the recovery window.
- `Crypto initialization failed: ...` — handshake/encryption failure.

**Qt handling pattern:**

```cpp
QJsonObject response = sendBridgeRequest(request);
if (response.contains("error")) {
    QString message = response["error"].toString();
    qWarning() << "Bridge error:" << message;

    if (message.startsWith("Session not found")) {
        // Session expired or closed — restart
    } else if (message.startsWith("COSIGN_RATE_LIMIT")) {
        // Back off and retry
    } else if (message.startsWith("COSIGN_SESSION_DESYNCED")) {
        // Abandon this session; restart the ceremony
    } else {
        // Generic failure
    }
    return false;
}
return true;
```

---

## Transaction Signing Flow

A typical two-party PSBT signing ceremony using the automatic handshake:

**Initiator:**
1. `init` → obtain `session_id`, `invite_link`, `invite_code`. Show the invite
   to the peer.
2. `handshake_auto` (`is_initiator: true`) → obtain the verifiable `sas`.
3. Verify the SAS with the peer out-of-band.
4. `send` the PSBT as the `payload`.
5. `recv` (poll) for the peer's signed PSBT.
6. `close`.

**Responder:**
1. `join` with the `invite_link`.
2. `handshake_auto` (`is_initiator: false`) → obtain the `sas`.
3. Verify the SAS out-of-band.
4. `recv` the PSBT, sign it locally, `send` the signed PSBT back.
5. `close`.

For transports where the wallet relays handshake bytes itself, substitute the
manual `handshake` → `handshake_finish` → `handshake_complete` sequence for
`handshake_auto`.

---

## Security Considerations

**Channel security:** payloads are end-to-end encrypted with Noise after a
SPAKE2 password exchange keyed by the invite code. The transport (relay or Tor)
never sees plaintext, and a relay cannot read or forge session payloads.

**SAS verification is mandatory.** The SAS is the only defense against a
man-in-the-middle who relays both sides of the handshake. Always compare it over
an independent channel and abort on mismatch.

**Handling of sensitive data:**
- Invite codes are the SPAKE2 password — exchange them over a channel the peer
  trusts; do not log or persist them.
- The SAS must be compared on a different channel than the data channel.
- Session IDs and handshake messages are not secret.
- Encrypted payloads are opaque to the bridge.

**Poisoned sessions:** an AEAD decrypt failure desynchronizes the Noise cipher
state irrecoverably. The bridge poisons the session and refuses further traffic;
the wallet must surface "session desynchronized, restart ceremony" and stop
polling that session id.

---

## Debugging

Run the bridge with verbose logging:

```bash
RUST_LOG=debug ./cosign-bridge
```

Log each request/response pair from the Qt side while integrating:

```cpp
qDebug() << "Bridge request:" << QJsonDocument(request).toJson();
QJsonObject response = sendBridgeRequest(request);
qDebug() << "Bridge response:" << QJsonDocument(response).toJson();
```

The bridge is a Rust crate (edition 2021); build it with the standard Cargo
toolchain.
