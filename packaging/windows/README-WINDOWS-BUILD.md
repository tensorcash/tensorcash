# TensorCash Windows Wallet

A pre-built Windows desktop wallet (`tensorcash-qt.exe`) for TensorCash.

## Download

Grab the latest release from the [Windows Builds](https://github.com/Dizzztroyer/tensorcash/releases/tag/windows-v1.0.6) page on the contributor fork.

**SHA-256:** `1d15b8c521934a24f410fa347405e92ff4a80d8bf1ccfbfaad5f3e821fc8a046`

## What's inside

- **tensorcash-qt.exe** — Full node with GUI (bitcoin-qt equivalent)
- Wallet support, QR codes, ZeroMQ — all enabled
- Qt/OpenSSL/Boost statically linked; only Windows system DLLs required
- Built from tag `v1.0.6` of the umbrella repo

## How it was built

```bash
# On Ubuntu 24.04 (cross-compile from Linux):
sudo apt install mingw-w64 cmake nsis
export DEFAULT_CHAIN_TYPE=tensor
./packaging/windows/build-windows.sh --release
```

This uses the existing cross-compile script in `packaging/windows/build-windows.sh`.

## Safety

- Import or restore seed/private keys **only** inside the local wallet UI
- Never paste a seed phrase or private key into chat, GitHub, logs, or terminal output
