Asset RPC Process Guide (IssuerReg, Mint, Burn, Query)

Overview
- This guide documents the RPCs and steps to interact with the on‑chain asset system built on per‑output TLV extensions (vExt).
- You can register an asset issuer (IssuerReg), mint asset UTXOs (AssetTag), transfer them like normal UTXOs, and burn them (reduce supply) under policy.
- All flows use standard Core RPCs (create/fund/sign/send raw transaction) plus a few helper RPCs added in this fork.

Helper RPCs (new)
- `rawtxaddoutext <hex> <vout> <tlv_hex>`
  - Attach/replace a raw TLV at `vout` in the given transaction hex. Pass empty string `""` to clear.

- `rawtxattachissuerreg <hex> <vout> <asset_id_hex> <policy_bits> [allowed_spk_families]`
  - Attach an IssuerReg TLV to output `vout` of `hex`.
  - `asset_id_hex` is 32 bytes (no `0x`).
  - `policy_bits` is a 32‑bit mask (see Policy Bits below).
  - `allowed_spk_families` is a 16‑bit mask of allowed script families for AssetTag outputs (default: P2WPKH | P2WSH | P2TR, i.e., exclude P2SH by default).

- `rawtxattachassettag <hex> <vout> <asset_id_hex> <amount_u64> [flags_u32]`
  - Attach an AssetTag TLV to output `vout` of `hex`.
  - `amount_u64` is the asset amount to assign to this output (must be > 0).
  - `flags_u32` optional asset flags (default 0).

- `getassetpolicy <asset_id_hex>`
  - Reads the registry (LevelDB) for the current entry of `asset_id`:
    - `policy_bits`, `allowed_spk_families`, `icu_outpoint` (txid + vout) if present.

Other RPCs used
- Standard Core RPCs: `createrawtransaction`, `fundrawtransaction`, `signrawtransactionwithwallet`, `sendrawtransaction`, `decoderawtransaction`, `getrawtransaction`.
  - `decoderawtransaction` includes `vout[N].outext` hex when the output carries TLV.

Script Families (consensus‑recognizable)
- Families used in policy enforcement and registry masks:
  - `P2PKH = 1 << 0`, `P2SH = 1 << 1`, `P2WPKH = 1 << 2`, `P2WSH = 1 << 3`, `P2TR = 1 << 4`.
- Shortcuts:
  - Holder‑only families: `P2PKH | P2WPKH` (renders unilateral issuer burn infeasible without holder key).
  - Default allowed families for IssuerReg (if not specified): `P2WPKH | P2WSH | P2TR` (excludes P2SH by default).

Policy Bits (IssuerReg)
- Bits enforced by consensus when known (from local IssuerReg in the tx or the registry):
  - `MINT_ALLOWED = 0x0001`
  - `BURN_ALLOWED = 0x0002`
  - Note: burns always require ICU authorization in this fork. Joint/holder‑consent is enforced by allowed families on AssetTag outputs.

End‑to‑end Flows

1) Register an Asset (IssuerReg)
- Build an initial raw transaction with your desired BTC output script (this will carry the IssuerReg TLV):

  - Create a skeleton tx:
    - `createhex=$(bitcoin-cli -named createrawtransaction inputs='[]' outputs='{"<your_address>":0.001}')`
  - Attach IssuerReg TLV at vout 0 (replace `<asset_id_hex>`, `<policy_bits>`, `[allowed_mask]`):
    - `hex1=$(bitcoin-cli rawtxattachissuerreg "$createhex" 0 <asset_id_hex> <policy_bits> [allowed_mask])`
  - Fund and sign:
    - `funded=$(bitcoin-cli fundrawtransaction "$hex1" | jq -r .hex)`
    - `signed=$(bitcoin-cli signrawtransactionwithwallet "$funded" | jq -r .hex)`
  - Broadcast:
    - `txid=$(bitcoin-cli sendrawtransaction "$signed")`
  - Confirm registry entry (after inclusion):
    - `bitcoin-cli getassetpolicy <asset_id_hex>`

Tips:
- For “issuer‑unilateral burn” assets, set `policy_bits = (MINT_ALLOWED|BURN_ALLOWED)` and keep `allowed_spk_families` default.
- For “issuer + holder consent” assets, set `policy_bits = (MINT_ALLOWED|BURN_ALLOWED)` and `allowed_spk_families = (P2PKH|P2WPKH)`.

2) Mint Asset UTXOs
- You must include the current ICU outpoint as an input to authorize mint (ICU can be found via `getassetpolicy`).
- Steps:
  - Build a raw tx that spends the ICU and adds one or more outputs (with minimal BTC `nValue`) for recipients.
  - Attach AssetTag TLVs to recipient outputs using `rawtxattachassettag` for the minted amounts.
  - Fund any change in BTC, sign, and broadcast.

Example:
  - `createhex=$(bitcoin-cli -named createrawtransaction inputs='[{"txid":"<icu_txid>","vout":<icu_vout>}]' outputs='{"<recipient_addr>":0.0001}')`
  - `hex1=$(bitcoin-cli rawtxattachassettag "$createhex" 0 <asset_id_hex> 1000)`
  - `funded=$(bitcoin-cli fundrawtransaction "$hex1" | jq -r .hex)`
  - `signed=$(bitcoin-cli signrawtransactionwithwallet "$funded" | jq -r .hex)`
  - `txid=$(bitcoin-cli sendrawtransaction "$signed")`

3) Transfer Asset
- Select your existing AssetTag UTXOs and create outputs to recipients with the same AssetTag TLV. Ensure Δ == 0 for the asset.
- Mint/burn rules are not involved if total input amount equals total output amount per asset.

4) Burn Asset (reduce supply)
- Burn is Δ < 0: spend AssetTag UTXOs and the current ICU in the same tx, and do not recreate the full input amount as outputs.
- Always requires ICU in this fork. Registry `BURN_ALLOWED` must be set for the asset (if known).

Example:
  - `createhex=$(bitcoin-cli -named createrawtransaction inputs='[{"txid":"<asset_utxo_txid>","vout":<n>},{"txid":"<icu_txid>","vout":<icu_vout>}]' outputs='{}')`
  - `funded=$(bitcoin-cli fundrawtransaction "$createhex" | jq -r .hex)`
  - `signed=$(bitcoin-cli signrawtransactionwithwallet "$funded" | jq -r .hex)`
  - `txid=$(bitcoin-cli sendrawtransaction "$signed")`

Diagnostics & Troubleshooting
- Decoding:
  - `decoderawtransaction <hex>` shows `vout[N].outext` when present. Ensure `tlv_hex` is well‑formed.
- Consensus errors:
  - `asset-coinbase-forbidden` — coinbase changed asset supply (not allowed).
  - `asset-amount-zero` — AssetTag amount was zero.
  - `asset-mint-unauthorized` — Δ>0 without ICU.
  - `asset-burn-needs-icu` — Δ<0 without ICU.
  - `asset-mint-disallowed` / `asset-burn-disallowed` — registry policy_bits disabled mint/burn.
  - `asset-spk-not-allowed` — AssetTag outputs use a script family not allowed by registry/local IssuerReg.
- Policy errors (mempool):
  - `outext` — vExt present but TLV doesn’t parse as AssetTag or IssuerReg (unknown TLV rejected).
  - `scriptsig-not-pushonly`, `scriptpubkey`, `dust`, `tx-size` — standardness constraints.
- Encoding errors:
  - `Unknown transaction flags` — invalid flags in transaction marker/flags.
  - `invalid output extension TLV` — malformed TLV structure (type + canonical varint + exact payload).

Best Practices
- Always decode (`decoderawtransaction`) before sending to verify vExt placement and contents.
- For joint‑required assets, constrain `allowed_spk_families = P2PKH | P2WPKH` in IssuerReg and only mint/transfer to those addresses.
- Keep BTC fee discipline; current policy may reject transactions with unusual fee patterns.
- Avoid P2SH for AssetTag outputs unless explicitly whitelisted via IssuerReg; default denies P2SH.

Reference
- TLV types:
  - `0x01 AssetTag` — `{asset_id(32), amount(LE8), flags(LE4 opt)}`
  - `0x10 IssuerReg` — `{asset_id(32), policy_bits(LE4), allowed_spk_families(LE2 opt)}`
- Size bounds:
  - Max per output vExt: 16 KiB; max total vExt per tx: 128 KiB.
- Hash commitments:
  - txid/wtxid and Taproot/BIP143 hashOutputs bind vExt bytes.

Examples: Script family masks
- Holder‑only (recommended for “issuer + holder consent”):
  - `P2PKH | P2WPKH = 0x0001 | 0x0004 = 0x0005`
- Default allowed (issuer-unilateral permitted):
  - `P2WPKH | P2WSH | P2TR = 0x0004 | 0x0008 | 0x0010 = 0x001C`

Notes
- The registry persists across blocks and is reorg‑safe via undo entries.
- Mint and burn are subject to registry policy when known; if no registry entry exists, only ICU‑authorized operations in the same tx are accepted.
- Additional mempool policy knobs (feerate floors, multi‑asset floors, ICU anti‑churn) can be enabled in future phases.

