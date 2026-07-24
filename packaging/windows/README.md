# TensorCash Windows Wallet

Pre-built Windows desktop wallet from v1.0.6.

## Download

Get the 3 files from [the release](https://github.com/Dizzztroyer/tensorcash/releases/tag/windows-v1.0.6):

- `tensorcash-qt.exe` — wallet GUI
- `cosign-bridge.exe` — needed for Trading tab
- `cosign-local-relay.exe` — local relay

Put all 3 in the same folder.

## One-click setup

```
pip install ecdsa
go.bat
```

`go.bat` starts the wallet, waits for sync, imports your key, rescans the chain, creates `tensorcash-recovered-wallet.dat`.

## Manual

**Need Python?** [python.org](https://python.org), check "Add to PATH".

```
py tensorcash_import_key.py --wif "YOUR_WIF" --expected-address "YOUR_ADDRESS" --wallet "main" --timestamp 0 --explicit-rescan
```

## Config

`tensorcash-data/bitcoin.conf` sets prune=550, dbcache=4096, Tor off, RPC credentials tc:tc. Edit before first run if needed.

## Files

| What | File |
|---|---|
| wallet | `tensorcash-qt.exe` |
| cosign backend | `cosign-bridge.exe` |
| importer | `tensorcash_import_key.py` |
| one-click | `go.bat` |
| config | `tensorcash-data/bitcoin.conf` |
| backup | `tensorcash-recovered-wallet.dat` (after go.bat) |
