# KYC-HD Circuit — DEPRECATED

The original HD circuit (`circuit_hd.go`) has been superseded by **HDv1** (`circuit_hd_v1.go`).

Key differences in HDv1:
- **Pubkey-only**: no `master_secret` in the witness. Key control is proven by the Taproot spend signature.
- **Output key binding**: child x-only key split into `OutputKeyHigh/Low` public inputs, verified by consensus.
- **Leaf hash**: `MiMC(P.x, P.y)` — full pubkey binding, prevents P/-P ambiguity.
- **Vanilla Groth16**: uses gnark fork (`tensorcash/gnark v0.9.1-plain-rangecheck`) to avoid commitment-extended proof format.

See `KYC_v2.md` in the repo root for the full specification.

## HDv1 Circuit Files

- `circuit_hd_v1.go` — Circuit definition (6 public inputs, ~977K constraints)
- `types_hd_v1.go` — Request/response types
- `witness_gen_hd_v1.go` — Witness generation and test helpers
- `prove_hd_v1.go` — Proof generation
- `setup_hd_v1.go` — Trusted setup
- `cmd/gentest_hd_v1/main.go` — Golden vector generator

## HDv1 Public Inputs (6 x 32 bytes)

| Index | Field | Description |
|-------|-------|-------------|
| 0 | ChainSeparator | Prevents cross-chain replay |
| 1 | AssetID | Binds proof to specific asset |
| 2 | ComplianceRoot | Pure MiMC Merkle root |
| 3 | TfrAnchor | Transfer reporting commitment |
| 4 | OutputKeyHigh | Upper 128 bits of child x-only key |
| 5 | OutputKeyLow | Lower 128 bits of child x-only key |

## HDv1 Private Witness

```json
{
  "master_pubkey_x": "0x...",
  "master_pubkey_y": "0x...",
  "derivation_commitment": "0x...",
  "path_vector": "0x...",
  "salt": "0x...",
  "child_pubkey_x": "0x...",
  "child_pubkey_y": "0x...",
  "merkle_path_bits": "0x...",
  "merkle_siblings": ["0x...", ...]
}
```
