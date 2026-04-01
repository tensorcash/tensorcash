# TensorCash Desktop Packaging

This directory contains build scripts and resources for creating distributable
desktop applications for macOS, Windows, and Linux.

## Architecture

Desktop TensorCash wallets are **light validation nodes** that:
- Run a full `bitcoin-qt` node locally (sync chain, wallet, P2P)
- Verify **Quick/Smell locally** in C++ for fast responsiveness
- Delegate **Full/Model** validation over **HTTPS** to the Gateway Verification Service
- Bundle all necessary helper binaries (cosign-bridge, optionally Tor)

```
┌─────────────────────────────────────────────────────────────────┐
│                    User's Desktop (macOS/Windows)               │
│  ┌───────────────────────────────────────────────────────────┐  │
│  │                   TensorCash.app / .exe                   │  │
│  │  ┌─────────────┐  ┌──────────────┐  ┌─────────────────┐   │  │
│  │  │ bitcoin-qt  │  │cosign-bridge │  │ tor (optional)  │   │  │
│  │  │  (GUI+Node) │  │ (co-signing) │  │  (anonymity)    │   │  │
│  │  └──────┬──────┘  └──────────────┘  └─────────────────┘   │  │
│  │         │                                                  │  │
│  │         │ HTTPS (full/model) / local quick-smell          │  │
│  └─────────┼─────────────────────────────────────────────────┘  │
└────────────┼────────────────────────────────────────────────────┘
             │
             │ Internet (TLS/CURVE)
             ▼
┌─────────────────────────────────────────────────────────────────┐
│              TensorCash Validator Infrastructure                │
│  ┌─────────────────┐  ┌──────────────┐  ┌──────────────────┐   │
│  │verification-api │  │ vllm-backend │  │   miner-proxy    │   │
│  │ (VDF + ML eval) │  │  (GPU infer) │  │  (work routing)  │   │
│  └─────────────────┘  └──────────────┘  └──────────────────┘   │
└─────────────────────────────────────────────────────────────────┘
```

## Directory Structure

```
packaging/
├── README.md                    # This file
├── common/
│   ├── validator-config.json    # Default validator endpoints
│   └── default-bitcoin.conf     # Default node configuration
├── macos/
│   ├── build-macos.sh          # Main build script
│   ├── sign-and-notarize.sh    # Code signing + Apple notarization
│   ├── Info.plist.in           # App bundle metadata template
│   ├── entitlements.plist      # macOS sandbox entitlements
│   ├── TensorCash.icns         # App icon
│   └── dmg/
│       ├── background.png      # DMG background image
│       └── dmg-settings.json   # create-dmg configuration
├── windows/
│   ├── build-windows.sh        # Cross-compile from Linux/macOS
│   ├── sign-windows.sh         # Authenticode signing
│   ├── installer.nsi           # NSIS installer script
│   ├── TensorCash.ico          # App icon
│   └── resources.rc            # Windows resource file
└── linux/
    ├── build-linux.sh          # AppImage/Flatpak build
    ├── tensorcash.desktop      # XDG desktop entry
    └── AppDir/                 # AppImage structure
```

## Build Requirements

### macOS Native Build
- Xcode Command Line Tools
- CMake 3.22+
- Qt 6.2+ (`brew install qt@6`)
- Homebrew dependencies: `zeromq boost gmp flint sqlite`
- Rust toolchain (for cosign-bridge)
- Apple Developer ID certificate (for signing)

### Windows Cross-Compile (from Linux/macOS)
- mingw-w64 toolchain
- NSIS installer compiler
- osslsigncode (for Authenticode signing)
- Windows code signing certificate

### Linux
- Standard build tools
- AppImageTool or Flatpak builder

## Quick Start

### macOS
```bash
# Install dependencies
brew install qt@6 zeromq boost gmp flint sqlite cmake

# Build
cd packaging/macos
./build-macos.sh --release

# Sign and notarize (requires Apple Developer ID)
./sign-and-notarize.sh --identity "Developer ID Application: Your Name"
```

### Windows (cross-compile)
```bash
# From Linux with mingw-w64 installed
cd packaging/windows
./build-windows.sh --release

# Sign (requires Windows code signing cert)
./sign-windows.sh --key path/to/key.p12
```

## Output Artifacts

| Platform | Artifact | Contents |
|----------|----------|----------|
| macOS | `TensorCash-x.y.z.dmg` | Signed/notarized .app bundle |
| Windows | `TensorCash-x.y.z-setup.exe` | NSIS installer with Authenticode |
| Linux | `TensorCash-x.y.z.AppImage` | Self-contained AppImage |

## Validator Delegation

Desktop clients use a hybrid strategy:

1. **Quick/Smell**: In-process `QuickVerifier` (no network).
2. **Full + Model**: HTTPS calls to the Gateway Verification Service which forwards to the validator over ZMQ.
3. **Mining**: External miner bridge is **off by default** in desktop packages; can be enabled manually.

Configuration in `~/.tensorcash/bitcoin.conf` or GUI settings:
```ini
# Desktop defaults
validationapi=desktop
validatorhttpurl=https://verify.tensorcash.io
# validatorapikey=your_api_key_here
```

You can still opt into the legacy ZMQ validator path (`validationapi=real`) or mock mode (`validationapi=mock`) for tests.

## Security Considerations

1. **Code Signing**: All releases must be signed
   - macOS: Apple Developer ID + notarization
   - Windows: Authenticode with EV certificate

2. **Validator Communication**: ZMQ connections should use:
   - TLS termination proxy, or
   - ZMQ CURVE encryption

3. **Sandboxing**:
   - macOS: App Sandbox entitlements (network access only)
   - Windows: Consider MSIX packaging with capabilities
