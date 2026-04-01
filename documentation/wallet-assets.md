# TensorCash Wallet Asset Integration

## Overview

The TensorCash wallet provides comprehensive support for per-output TLV assets (stablecoins/tokens) with full UTXO-native integration. Assets are treated as first-class citizens alongside BTC, with automatic coin selection, metadata persistence, and PSBT compatibility.

## Core Concepts

### Asset UTXOs
- Asset-bearing UTXOs contain both BTC value (for dust prevention) and asset units via TLV extensions
- Each asset UTXO carries exactly one asset type (no mixing)
- Asset UTXOs are automatically isolated from BTC-only operations by default

### Asset Metadata
The wallet persists asset metadata for user-friendly display:
- `asset_id`: 32-byte identifier (authoritative)
- `ticker`: Human-readable symbol (3-11 chars, optional)
- `decimals`: Display precision (0-18, optional)
- `is_issuer_credential`: ICU flag for issuer control

## Coin Control and Selection

### Default Behavior
```cpp
CCoinControl control;
control.m_avoid_asset_utxos = true;        // Default: exclude asset UTXOs
control.m_allow_icu_selection = false;     // Default: exclude ICU UTXOs
control.m_required_asset_id = std::nullopt; // Default: no asset filter
```

### Coin Control Flags

#### `m_avoid_asset_utxos` (default: `true`)
Controls whether asset-bearing UTXOs are included in automatic coin selection:
- `true`: Asset UTXOs excluded from BTC-only operations (recommended)
- `false`: Asset UTXOs included (use with caution)

```bash
# BTC-only send (asset UTXOs automatically avoided)
bitcoin-cli sendtoaddress "bc1q..." 0.1

# Mixed operation (must explicitly disable avoidance)
bitcoin-cli fundrawtransaction <hex> '{"avoid_asset_utxos": false}'
```

#### `m_allow_icu_selection` (default: `false`)
Controls whether Issuer Credential UTXOs can be automatically selected:
- `false`: ICU UTXOs excluded (prevents accidental issuer key spending)
- `true`: ICU UTXOs included (for mint/burn operations)

```bash
# Mint operation requiring ICU spending
bitcoin-cli mintasset <icu_txid> <icu_vout> ...
```

#### `m_required_asset_id` (default: `std::nullopt`)
Restricts automatic selection to specific asset types:
- `std::nullopt`: No asset restriction
- `uint256`: Only select UTXOs of specified asset

```bash
# Asset-specific operation
bitcoin-cli sendasset "GOLD" "bc1q..." 1000000
```

## High-Level Asset RPCs

### Asset Balance and Discovery

#### `getassetbalance`
Returns wallet balance summary for all or specified assets:

```bash
# All assets
bitcoin-cli getassetbalance

# Specific assets by ticker or ID
bitcoin-cli getassetbalance '["GOLD", "USD", "aaa...aaa"]'
```

Response format:
```json
[
  {
    "asset_id": "aaa...aaa",
    "ticker": "GOLD",
    "decimals": 8,
    "balance": 100000000,
    "balance_decimal": "1.00000000",
    "pending": 50000,
    "locked": 0,
    "utxo_count": 3
  }
]
```

#### `listassetutxos`
Lists individual asset-bearing UTXOs with metadata:

```bash
# All asset UTXOs
bitcoin-cli listassetutxos

# Filter by assets and confirmations
bitcoin-cli listassetutxos '["GOLD", "SILVER"]' 6 999999
```

#### `listassets`
Discovers all assets known to the wallet:

```bash
# Simple list
bitcoin-cli listassets

# Verbose with registry data
bitcoin-cli listassets false true

# Filtered by criteria
bitcoin-cli listassets false false '{"has_ticker": true, "min_balance": 1000000}'
```

### Asset Transfers

#### `sendasset` (Wallet-Level)
Automatic asset sending with UTXO selection and change handling:

```bash
# Send by ticker
bitcoin-cli sendasset "GOLD" "bc1q..." 50000000

# Send by asset ID with options
bitcoin-cli sendasset "aaa...aaa" "bc1q..." 1000000 '{
  "fee_rate": 10,
  "replaceable": true,
  "broadcast": false
}'
```

Response:
```json
{
  "txid": "abc...",
  "fee": 0.00001,
  "asset_id": "aaa...aaa",
  "ticker": "GOLD",
  "asset_inputs": 100000000,
  "asset_outputs": 100000000,
  "asset_change": 50000000
}
```

### Asset Lifecycle (Issuer Operations)

#### `registerasset`
Create new asset registration:

```bash
bitcoin-cli registerasset \
  "bc1q..." \           # ICU address
  5.1 \                 # ICU bond (min 5 BTC)
  "aaa...aaa" \         # Asset ID
  3 \                   # Policy bits (MINT_ALLOWED | BURN_ALLOWED)
  28 \                  # Allowed families (P2WPKH | P2WSH | P2TR)
  510000000 \           # Unlock threshold
  "GOLD" \              # Ticker
  8 \                   # Decimals
  '{"broadcast": true}'
```

#### `mintasset`
Mint new asset units (requires ICU):

```bash
bitcoin-cli mintasset \
  <icu_txid> <icu_vout> \     # Current ICU
  "bc1q..." 5.1 \             # New ICU
  "bc1q..." 0.001 \           # Asset output
  "aaa...aaa" 1000000 \       # Asset ID and units
  3 28 \                      # Policy settings
  '{"broadcast": true}'
```

#### `burnasset`
Burn asset units (requires ICU):

```bash
bitcoin-cli burnasset \
  <icu_txid> <icu_vout> \     # Current ICU
  <asset_txid> <asset_vout> \ # Asset UTXO to burn
  "bc1q..." 5.1 \             # New ICU
  "aaa...aaa" 3 28 \          # Asset ID and policy
  '{"broadcast": true}'
```

## PSBT Asset Integration

### Asset Metadata Preservation
Asset extensions (`vExt`) are automatically preserved through PSBT workflows:

1. **Collection**: `CollectOutputExtensionSnapshots()` captures TLV data
2. **Processing**: PSBT operations maintain metadata separately
3. **Reapplication**: `ReapplyOutputExtensionSnapshots()` restores TLV data

### PSBT Workflow Example
```bash
# Create funded PSBT with asset outputs
bitcoin-cli walletcreatefundedpsbt '[]' '[{"bc1q...": {"btc": 0.001, "assets": {"GOLD": 1000000}}}]'

# Process and sign
bitcoin-cli walletprocesspsbt <psbt_hex>

# Finalize
bitcoin-cli finalizepsbt <processed_psbt>

# Verify TLV preservation
bitcoin-cli decoderawtransaction <final_hex>
```

### Asset Consistency Validation
PSBT processing includes asset validation:
- **Single-asset rule**: Transactions restricted to one asset type per operation
- **Conservation check**: Asset inputs must equal asset outputs (Δ=0 for transfers)
- **ICU authorization**: Mint/burn operations require corresponding ICU inputs

## Error Handling and Troubleshooting

### Common Error Messages

#### `"Insufficient funds for asset <asset_id>"`
**Cause**: Not enough spendable asset UTXOs for the requested operation
**Solution**:
- Check balance: `bitcoin-cli getassetbalance '["<asset_id>"]'`
- List UTXOs: `bitcoin-cli listassetutxos '["<asset_id>"]'`
- Verify UTXOs are spendable and unlocked

#### `"Asset not registered"`
**Cause**: Attempting to use an unregistered asset ID
**Solution**:
- Verify asset ID: `bitcoin-cli getassetinfo "<asset_id>"`
- Check registry: `bitcoin-cli listassets false true`

#### `"Ticker not found"`
**Cause**: Ticker lookup failed or ambiguous
**Solution**:
- Use asset ID instead of ticker
- Check ticker bindings: `bitcoin-cli listassets false false '{"has_ticker": true}'`

### Asset UTXO Debugging

#### Check Coin Selection Behavior
```bash
# Verify asset UTXOs are properly isolated
bitcoin-cli listunspent  # Should not show asset UTXOs by default
bitcoin-cli listassetutxos  # Shows only asset UTXOs

# Check specific asset availability
bitcoin-cli listassetutxos '["GOLD"]' 1 999999
```

#### Trace Transaction Asset Content
```bash
# Decode transaction to see TLV extensions
bitcoin-cli decoderawtransaction <hex>

# Validate asset conservation
bitcoin-cli validateassetconservation <hex>
```

## Best Practices

### Operational Guidelines

1. **Asset Isolation**: Keep asset and BTC operations separate by default
2. **Ticker Usage**: Prefer tickers for user interfaces, asset IDs for precision
3. **UTXO Management**: Monitor asset UTXO fragmentation and consolidate when needed
4. **Fee Planning**: Ensure adequate BTC for fees in mixed operations

### Security Considerations

1. **ICU Protection**: Never accidentally spend ICU UTXOs outside mint/burn flows
2. **Asset Verification**: Always verify asset IDs when accepting transfers
3. **Metadata Trust**: Validate ticker/decimals from registry, not just wallet metadata
4. **Conservation Checks**: Verify asset conservation in custom transaction building

### Performance Optimization

1. **UTXO Consolidation**: Periodically consolidate small asset UTXOs
2. **Metadata Caching**: Leverage ticker resolution caching for bulk operations
3. **Selective Scanning**: Use asset filters to reduce computation overhead

## Integration Examples

### Custom Wallet Integration
```cpp
// Asset-aware coin selection
CCoinControl control;
control.m_avoid_asset_utxos = false;
control.m_required_asset_id = target_asset_id;

CoinsResult coins = AvailableCoins(wallet, &control);
// ... select appropriate asset UTXOs
```

### Exchange Integration
```bash
#!/bin/bash
# Deposit detection script
for txid in $(bitcoin-cli listsinceblock | jq -r '.transactions[].txid'); do
  decoded=$(bitcoin-cli decoderawtransaction $(bitcoin-cli getrawtransaction $txid))
  # Check for asset outputs to monitored addresses
  echo "$decoded" | jq '.vout[] | select(.outext != null)'
done
```

### Multi-Asset Atomic Swaps
```bash
# Build multi-asset transaction template
bitcoin-cli createassettransaction \
  '[{"txid":"...","vout":0,"asset":"GOLD","asset_units":1000}]' \
  '{"bc1q...": {"btc": 0.001, "assets": {"USD": 1500}}}' \
  '{"locktime": 750000}'
```

## Migration and Compatibility

### Upgrading from Pre-Asset Wallets
1. Existing BTC UTXOs remain unaffected
2. New asset operations require explicit opt-in
3. Legacy RPC methods continue to work unchanged

### Backup and Recovery
- Wallet backups include asset metadata
- Private keys control both BTC and asset UTXOs
- Seed phrases restore full asset capability

### Cross-Wallet Compatibility
- Asset transactions readable by any asset-aware node
- Metadata may vary between wallet implementations
- Registry data provides canonical asset information

---

## Technical References

- [Asset System Documentation](../services/core-node/bcore/asset-readme.md) - Complete technical implementation and test coverage
- Asset source code: `src/assets/`, `src/wallet/rpc/assets.cpp`
- Test coverage: `test/functional/wallet_asset_*.py`, `src/wallet/test/spend_tests.cpp`