#!/usr/bin/env bash
# Create signed and notarized DMG from TensorMiner.app
# Requires: APPLE_ID, APPLE_APP_PASSWORD, APPLE_TEAM_ID environment variables

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUILD_DIR="${BUILD_DIR:-${SCRIPT_DIR}/../build}"
APP_DIR="${BUILD_DIR}/TensorMiner.app"
DMG_NAME="${DMG_NAME:-TensorMiner}"
VERSION="${VERSION:-1.0.0}"

# Apple credentials for notarization
APPLE_ID="${APPLE_ID:-}"
APPLE_APP_PASSWORD="${APPLE_APP_PASSWORD:-}"
APPLE_TEAM_ID="${APPLE_TEAM_ID:-YOUR_APPLE_TEAM_ID}"

DMG_PATH="${BUILD_DIR}/${DMG_NAME}-${VERSION}-arm64.dmg"
DMG_TEMP="${BUILD_DIR}/dmg-temp"

echo "=== Creating DMG ==="

if [ ! -d "${APP_DIR}" ]; then
    echo "ERROR: App bundle not found at ${APP_DIR}"
    echo "Run build-app.sh first"
    exit 1
fi

# -----------------------------------------------------------------------------
# Create DMG staging area
# -----------------------------------------------------------------------------
echo "=== Preparing DMG contents ==="

rm -rf "${DMG_TEMP}"
mkdir -p "${DMG_TEMP}"

# Copy app
cp -R "${APP_DIR}" "${DMG_TEMP}/"

# Create Applications symlink
ln -s /Applications "${DMG_TEMP}/Applications"

# Create background and .DS_Store for nice appearance (optional)
# For now, keep it simple

# -----------------------------------------------------------------------------
# Create DMG
# -----------------------------------------------------------------------------
echo "=== Creating DMG image ==="

rm -f "${DMG_PATH}"

hdiutil create \
    -volname "${DMG_NAME}" \
    -srcfolder "${DMG_TEMP}" \
    -ov \
    -format UDZO \
    "${DMG_PATH}"

echo "DMG created: ${DMG_PATH}"

# -----------------------------------------------------------------------------
# Sign DMG
# -----------------------------------------------------------------------------
CODESIGN_IDENTITY="${CODESIGN_IDENTITY:-}"

if [ -n "${CODESIGN_IDENTITY}" ]; then
    echo "=== Signing DMG ==="

    codesign --force --sign "${CODESIGN_IDENTITY}" "${DMG_PATH}"
    echo "DMG signed"

    # Verify
    codesign --verify "${DMG_PATH}"
    echo "DMG signature verified"
else
    echo "WARNING: CODESIGN_IDENTITY not set, DMG not signed"
fi

# -----------------------------------------------------------------------------
# Notarize
# -----------------------------------------------------------------------------
if [ -n "${APPLE_ID}" ] && [ -n "${APPLE_APP_PASSWORD}" ]; then
    echo "=== Notarizing DMG ==="

    # Submit for notarization
    echo "Submitting to Apple notary service..."

    NOTARIZE_OUTPUT=$(xcrun notarytool submit "${DMG_PATH}" \
        --apple-id "${APPLE_ID}" \
        --password "${APPLE_APP_PASSWORD}" \
        --team-id "${APPLE_TEAM_ID}" \
        --wait \
        --timeout 30m \
        2>&1) || {
        echo "Notarization failed!"
        echo "${NOTARIZE_OUTPUT}"
        exit 1
    }

    echo "${NOTARIZE_OUTPUT}"

    # Check if notarization succeeded
    if echo "${NOTARIZE_OUTPUT}" | grep -q "status: Accepted"; then
        echo "Notarization succeeded!"

        # Staple the ticket
        echo "=== Stapling ticket ==="
        xcrun stapler staple "${DMG_PATH}"
        echo "Ticket stapled"

        # Verify
        xcrun stapler validate "${DMG_PATH}"
        echo "Staple validated"
    else
        echo "WARNING: Notarization may not have succeeded"
        echo "Check the output above for details"
    fi
else
    echo "WARNING: Apple credentials not set, skipping notarization"
    echo "Set APPLE_ID, APPLE_APP_PASSWORD, APPLE_TEAM_ID to enable"
fi

# -----------------------------------------------------------------------------
# Cleanup
# -----------------------------------------------------------------------------
rm -rf "${DMG_TEMP}"

# -----------------------------------------------------------------------------
# Output
# -----------------------------------------------------------------------------
echo "=== DMG creation complete ==="
echo "Output: ${DMG_PATH}"
ls -la "${DMG_PATH}"

# Generate SHA256
shasum -a 256 "${DMG_PATH}" | tee "${DMG_PATH}.sha256"
