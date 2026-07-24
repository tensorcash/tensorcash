# TensorCash Windows Wallet

Pre-built `tensorcash-qt.exe` for Windows (v1.0.6, mingw cross-compile).

## Files

| File | What |
|------|------|
| `tensorcash-qt.exe` | Wallet GUI |
| `cosign-bridge.exe` | Cosign backend (Trading tab) |
| `cosign-local-relay.exe` | Local relay |
| `tensorcash_import_key.py` | WIF key importer (needs `pip install ecdsa`) |
| `tensorcash-data/bitcoin.conf` | Wallet config |

## Usage

**1. Start the wallet:**
```
run.bat
```

**2. Import your key (after sync finishes):**
```
import-key.bat
```
Or use one command:
```
go.bat
```

**3. Advanced CLI import:**
```
py tensorcash_import_key.py --wif "YOUR_WIF" --wallet "main" --backup backup.dat
```

## Config

`tensorcash-data/bitcoin.conf` — edit before first run if needed:
- `prune=550` — saves disk space (~550 MB)
- `dbcache=4096` — 4 GB RAM cache
- `proxy=` — Tor is OFF (direct sync)
- RPC credentials: `tc` / `tc`

## Build from source

```
git clone https://github.com/tensorcash/tensorcash
cd tensorcash
make -C depends HOST=x86_64-w64-mingw32 -j$(nproc)
./autogen.sh && ./configure --prefix=$PWD/depends/x86_64-w64-mingw32 && make -j$(nproc)
```
