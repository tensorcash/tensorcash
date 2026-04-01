#!/usr/bin/env bash
# =============================================================================
# TensorCash macOS Code Signing and Notarization
# =============================================================================
#
# Signs and notarizes TensorCash.app for distribution outside the App Store.
#
# Usage:
#   ./sign-and-notarize.sh [options] <app-or-dmg>
#
# Options:
#   --identity ID       Developer ID Application certificate identity
#   --keychain FILE     Keychain containing the signing certificate
#   --team-id ID        Apple Developer Team ID
#   --apple-id EMAIL    Apple ID for notarization
#   --password PASS     App-specific password (or @keychain:ITEM)
#   --skip-notarize     Only sign, don't notarize
#   --staple            Staple notarization ticket after success
#
# Environment variables (alternative to flags):
#   TENSORCASH_SIGNING_IDENTITY
#   TENSORCASH_TEAM_ID
#   APPLE_ID
#   APPLE_APP_PASSWORD
#
# Requirements:
#   - Xcode Command Line Tools
#   - Valid "Developer ID Application" certificate
#   - Apple Developer account for notarization
#
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Defaults from environment
SIGNING_IDENTITY="${TENSORCASH_SIGNING_IDENTITY:-}"
TEAM_ID="${TENSORCASH_TEAM_ID:-}"
APPLE_ID="${APPLE_ID:-}"
APPLE_PASSWORD="${APPLE_APP_PASSWORD:-}"
KEYCHAIN=""
SKIP_NOTARIZE=false
STAPLE=true

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log_info() { echo -e "${GREEN}[INFO]${NC} $*"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $*"; }
log_error() { echo -e "${RED}[ERROR]${NC} $*" >&2; }

usage() {
    head -30 "$0" | tail -25
    exit 1
}

# Parse arguments
POSITIONAL=()
while [[ $# -gt 0 ]]; do
    case $1 in
        --identity) SIGNING_IDENTITY="$2"; shift 2 ;;
        --keychain) KEYCHAIN="$2"; shift 2 ;;
        --team-id) TEAM_ID="$2"; shift 2 ;;
        --apple-id) APPLE_ID="$2"; shift 2 ;;
        --password) APPLE_PASSWORD="$2"; shift 2 ;;
        --skip-notarize) SKIP_NOTARIZE=true; shift ;;
        --staple) STAPLE=true; shift ;;
        --no-staple) STAPLE=false; shift ;;
        -h|--help) usage ;;
        -*) log_error "Unknown option: $1"; usage ;;
        *) POSITIONAL+=("$1"); shift ;;
    esac
done

if [[ ${#POSITIONAL[@]} -ne 1 ]]; then
    log_error "Expected exactly one argument: path to .app or .dmg"
    usage
fi

TARGET="${POSITIONAL[0]}"

if [[ ! -e "${TARGET}" ]]; then
    log_error "Target not found: ${TARGET}"
    exit 1
fi

# Validate signing identity
if [[ -z "${SIGNING_IDENTITY}" ]]; then
    log_error "Signing identity required. Set --identity or TENSORCASH_SIGNING_IDENTITY"
    log_info "Available identities:"
    security find-identity -v -p codesigning
    exit 1
fi

# =============================================================================
# Determine what we're signing
# =============================================================================

APP_BUNDLE=""
DMG_FILE=""

if [[ -d "${TARGET}" && "${TARGET}" == *.app ]]; then
    APP_BUNDLE="${TARGET}"
elif [[ -f "${TARGET}" && "${TARGET}" == *.dmg ]]; then
    DMG_FILE="${TARGET}"
    # Mount DMG and find app
    log_info "Mounting DMG..."
    MOUNT_POINT=$(mktemp -d)
    hdiutil attach "${DMG_FILE}" -mountpoint "${MOUNT_POINT}" -nobrowse -quiet
    APP_BUNDLE=$(find "${MOUNT_POINT}" -maxdepth 1 -name "*.app" | head -1)
    if [[ -z "${APP_BUNDLE}" ]]; then
        hdiutil detach "${MOUNT_POINT}" -quiet
        log_error "No .app found in DMG"
        exit 1
    fi
else
    log_error "Target must be a .app bundle or .dmg file"
    exit 1
fi

log_info "Signing: ${APP_BUNDLE}"

# =============================================================================
# Create entitlements file
# =============================================================================

ENTITLEMENTS="${SCRIPT_DIR}/entitlements.plist"
if [[ ! -f "${ENTITLEMENTS}" ]]; then
    log_info "Creating default entitlements..."
    cat > "${ENTITLEMENTS}" << 'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <!-- Hardened Runtime -->
    <key>com.apple.security.cs.allow-unsigned-executable-memory</key>
    <false/>
    <key>com.apple.security.cs.disable-library-validation</key>
    <false/>

    <!-- Network access for P2P and validator communication -->
    <key>com.apple.security.network.client</key>
    <true/>
    <key>com.apple.security.network.server</key>
    <true/>

    <!-- File access for blockchain data -->
    <key>com.apple.security.files.user-selected.read-write</key>
    <true/>

    <!-- Allow JIT for potential crypto optimizations (if needed) -->
    <key>com.apple.security.cs.allow-jit</key>
    <false/>

    <!-- Disable unnecessary entitlements -->
    <key>com.apple.security.app-sandbox</key>
    <false/>
</dict>
</plist>
EOF
fi

# =============================================================================
# Sign all components
# =============================================================================

CODESIGN_BASE=(
    --force
    --sign "${SIGNING_IDENTITY}"
    --options runtime
    --timestamp
)

if [[ -n "${KEYCHAIN}" ]]; then
    CODESIGN_BASE+=(--keychain "${KEYCHAIN}")
fi

# Entitlements only for executables and app bundle, NOT for dylibs/frameworks
CODESIGN_EXE=("${CODESIGN_BASE[@]}" --entitlements "${ENTITLEMENTS}")

# Retry wrapper: --timestamp contacts timestamp.apple.com which intermittently
# rate-limits or stalls (server ACKs the POST then never responds; codesign
# eventually times out as errSecInternalComponent). With multiple parallel
# macOS jobs all hitting Apple TSA at once, throttling probability rises.
#
# Strategy: long backoffs with random jitter so retries de-correlate across
# parallel jobs and give TSA time to recover. Worst-case total wait ~10 min,
# acceptable for a CI step that otherwise has to be entirely re-run.
codesign_retry() {
    local attempt delay=30 max=5
    for (( attempt = 1; attempt <= max; attempt++ )); do
        if codesign "$@"; then
            return 0
        fi
        if (( attempt < max )); then
            # Jitter: random 50%-150% of delay, so parallel jobs don't sync up.
            local jittered=$(( delay / 2 + RANDOM % delay ))
            log_warn "codesign failed (attempt ${attempt}/${max}), retrying in ${jittered}s..."
            sleep "${jittered}"
            delay=$((delay * 2))
            # Re-unlock in case the keychain timed out mid-loop
            if [[ -n "${KEYCHAIN}" ]]; then
                security unlock-keychain -p "" "${KEYCHAIN}" 2>/dev/null || true
            fi
        fi
    done
    return 1
}

# Sign frameworks first (inside-out) — no entitlements for libraries
log_info "Signing frameworks..."
while IFS= read -r lib; do
    log_info "  Signing: $(basename "${lib}")"
    codesign_retry "${CODESIGN_BASE[@]}" "${lib}"
done < <(find "${APP_BUNDLE}/Contents/Frameworks" -type f \( -name "*.dylib" -o -name "*.so" \) 2>/dev/null)

# Sign framework bundles — no entitlements
while IFS= read -r framework; do
    log_info "  Signing framework: $(basename "${framework}")"
    codesign_retry "${CODESIGN_BASE[@]}" "${framework}"
done < <(find "${APP_BUNDLE}/Contents/Frameworks" -name "*.framework" -type d 2>/dev/null)

# Sign plugins
while IFS= read -r plugin; do
    log_info "  Signing plugin: $(basename "${plugin}")"
    codesign_retry "${CODESIGN_EXE[@]}" "${plugin}"
done < <(find "${APP_BUNDLE}/Contents/PlugIns" -type f -perm +111 2>/dev/null)

# Sign all nested executables inside the app bundle. This covers bundled
# helpers such as bitcoin-cli, cosign-bridge, tor, and any future additions.
while IFS= read -r executable; do
    log_info "Signing executable: $(basename "${executable}")"
    codesign_retry "${CODESIGN_EXE[@]}" "${executable}"
done < <(find "${APP_BUNDLE}/Contents/MacOS" -mindepth 1 -maxdepth 1 -type f -perm +111 2>/dev/null | sort)

# Sign the main app bundle
log_info "Signing main app bundle..."
codesign_retry "${CODESIGN_EXE[@]}" "${APP_BUNDLE}"

# Verify signature
log_info "Verifying signature..."
codesign --verify --deep --strict --verbose=2 "${APP_BUNDLE}"

# Check for notarization compatibility
log_info "Checking Gatekeeper acceptance..."
spctl --assess --type execute --verbose "${APP_BUNDLE}" || {
    log_warn "Gatekeeper check failed. This may be expected before notarization."
}

# =============================================================================
# Notarization
# =============================================================================

if [[ "${SKIP_NOTARIZE}" == true ]]; then
    log_info "Skipping notarization (--skip-notarize)"
else
    if [[ -z "${APPLE_ID}" || -z "${APPLE_PASSWORD}" ]]; then
        log_error "Notarization requires --apple-id and --password (or set APPLE_ID and APPLE_APP_PASSWORD)"
        log_info "Skipping notarization."
    else
        # Create ZIP for notarization
        log_info "Creating ZIP for notarization..."
        NOTARIZE_ZIP=$(mktemp).zip
        ditto -c -k --keepParent "${APP_BUNDLE}" "${NOTARIZE_ZIP}"

        log_info "Submitting for notarization..."
        NOTARIZE_ARGS=(
            --apple-id "${APPLE_ID}"
            --password "${APPLE_PASSWORD}"
            --wait
        )
        if [[ -n "${TEAM_ID}" ]]; then
            NOTARIZE_ARGS+=(--team-id "${TEAM_ID}")
        fi

        xcrun notarytool submit "${NOTARIZE_ZIP}" "${NOTARIZE_ARGS[@]}" || {
            log_error "Notarization failed!"
            rm -f "${NOTARIZE_ZIP}"
            exit 1
        }

        rm -f "${NOTARIZE_ZIP}"

        # Staple the ticket
        if [[ "${STAPLE}" == true ]]; then
            log_info "Stapling notarization ticket..."
            xcrun stapler staple "${APP_BUNDLE}"
        fi

        log_info "Notarization complete!"
    fi
fi

# =============================================================================
# Handle DMG
# =============================================================================

if [[ -n "${DMG_FILE}" ]]; then
    # Unmount and re-create DMG with signed app
    log_info "Re-creating signed DMG..."
    hdiutil detach "${MOUNT_POINT}" -quiet

    SIGNED_DMG="${DMG_FILE%.dmg}-signed.dmg"
    rm -f "${SIGNED_DMG}"

    # Create new DMG from signed app
    hdiutil create -volname "$(basename "${APP_BUNDLE}" .app)" \
        -srcfolder "${APP_BUNDLE}" \
        -ov -format UDZO \
        "${SIGNED_DMG}"

    # Sign the DMG itself
    log_info "Signing DMG..."
    codesign --sign "${SIGNING_IDENTITY}" --timestamp "${SIGNED_DMG}"

    # Notarize DMG if requested
    if [[ "${SKIP_NOTARIZE}" != true && -n "${APPLE_ID}" && -n "${APPLE_PASSWORD}" ]]; then
        log_info "Notarizing DMG..."
        xcrun notarytool submit "${SIGNED_DMG}" "${NOTARIZE_ARGS[@]}"

        if [[ "${STAPLE}" == true ]]; then
            xcrun stapler staple "${SIGNED_DMG}"
        fi
    fi

    log_info "Signed DMG: ${SIGNED_DMG}"

    rmdir "${MOUNT_POINT}" 2>/dev/null || true
fi

# =============================================================================
# Done
# =============================================================================

log_info "Signing complete!"
log_info ""
log_info "Verify with:"
log_info "  codesign -dv --verbose=4 '${APP_BUNDLE}'"
log_info "  spctl -a -t execute -vv '${APP_BUNDLE}'"
