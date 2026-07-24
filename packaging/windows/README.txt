# TensorCash Windows Wallet — Quick Start

Pre-built Windows desktop wallet built from v1.0.6.

## Contents

- `tensorcash-qt.exe` — Full node with GUI
- `run.bat` — Launches the wallet with fast settings
- `import-key.bat` — Imports a private key via RPC
- `tensorcash-data/` — Local datadir (config + blockchain + wallet)

## How to use

1. **Run** `run.bat` — wallet starts syncing (pruned, no full history)
2. **Import your key:** open a second cmd, run `import-key.bat`, paste your private key
3. **Or** copy your existing `wallet.dat` into `tensorcash-data/`

## What's different from default

- `prune=550` — only ~550MB of blocks, no full chain download
- `dbcache=4096` — 4GB RAM cache for faster sync
- `proxy=` — Tor disabled (was causing slow sync)
- Local datadir — delete it anytime to start fresh
