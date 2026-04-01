#!/usr/bin/env bash
set -euo pipefail

# =============================================================================
# Windows Authenticode Code Signing via osslsigncode + PKCS#11 USB HSM
#
# Dual-signs: SHA-256 primary + SHA-1 nested (for Windows 7 compatibility)
# Uses RFC 3161 timestamping via Sectigo TSA
#
# Usage:
#   sign-windows.sh <file.exe>           Sign a single file
#   sign-windows.sh --batch <dir>        Sign all .exe/.dll in directory
#   sign-windows.sh --verify <file.exe>  Verify signature only
#
# Environment:
#   PKCS11_MODULE   Path to PKCS#11 .so (default: /usr/lib/libeTPkcs11.so)
#   PKCS11_PIN      Token PIN (required)
#   PKCS11_KEY      PKCS#11 key URI (default: pkcs11:token=TensorCash)
#   CERT_FILE       PEM certificate chain (required)
#   TSA_URL         Timestamp authority URL (default: http://timestamp.sectigo.com)
# =============================================================================

# --- Configuration ---
PKCS11_MODULE="${PKCS11_MODULE:-/usr/lib/libeTPkcs11.so}"
PKCS11_PIN="${PKCS11_PIN:?PKCS11_PIN is required}"
PKCS11_KEY="${PKCS11_KEY:-pkcs11:token=TensorCash}"
CERT_FILE="${CERT_FILE:?CERT_FILE is required}"
TSA_URL="${TSA_URL:-http://timestamp.sectigo.com}"

# --- Colors ---
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log()  { echo -e "${GREEN}[sign]${NC} $*"; }
warn() { echo -e "${YELLOW}[sign]${NC} $*"; }
err()  { echo -e "${RED}[sign]${NC} $*" >&2; }

# --- Functions ---

check_prerequisites() {
  local ok=true

  if ! command -v osslsigncode &>/dev/null; then
    err "osslsigncode not found in PATH"
    ok=false
  fi

  if [[ ! -f "$PKCS11_MODULE" ]]; then
    err "PKCS#11 module not found: $PKCS11_MODULE"
    ok=false
  fi

  if [[ ! -f "$CERT_FILE" ]]; then
    err "Certificate file not found: $CERT_FILE"
    ok=false
  fi

  if [[ "$ok" != "true" ]]; then
    exit 1
  fi
}

sign_file() {
  local input="$1"
  local filename
  filename=$(basename "$input")

  if [[ ! -f "$input" ]]; then
    err "File not found: $input"
    return 1
  fi

  log "Signing: $filename"

  # Temporary file for intermediate result
  local tmp_signed
  tmp_signed=$(mktemp "${input}.signed.XXXXXX")

  # Step 1: Primary SHA-256 signature
  log "  SHA-256 primary signature..."
  osslsigncode sign \
    -pkcs11module "$PKCS11_MODULE" \
    -key "$PKCS11_KEY" \
    -certs "$CERT_FILE" \
    -pass "$PKCS11_PIN" \
    -h sha256 \
    -n "TensorCash" \
    -i "https://tensorcash.com" \
    -ts "$TSA_URL" \
    -in "$input" \
    -out "$tmp_signed"

  # Step 2: Nested SHA-1 signature (Windows 7 compatibility)
  log "  SHA-1 nested signature..."
  osslsigncode sign \
    -pkcs11module "$PKCS11_MODULE" \
    -key "$PKCS11_KEY" \
    -certs "$CERT_FILE" \
    -pass "$PKCS11_PIN" \
    -h sha1 \
    -n "TensorCash" \
    -i "https://tensorcash.com" \
    -ts "$TSA_URL" \
    -nest \
    -in "$tmp_signed" \
    -out "$input"

  rm -f "$tmp_signed"

  log "  Signed: $filename"
}

verify_file() {
  local input="$1"
  local filename
  filename=$(basename "$input")

  log "Verifying: $filename"

  if osslsigncode verify -in "$input" 2>&1; then
    log "  Verification passed: $filename"
    return 0
  else
    err "  Verification FAILED: $filename"
    return 1
  fi
}

sign_and_verify() {
  local input="$1"
  sign_file "$input"
  verify_file "$input"
}

batch_sign() {
  local dir="$1"
  local count=0
  local failed=0

  if [[ ! -d "$dir" ]]; then
    err "Directory not found: $dir"
    return 1
  fi

  log "Batch signing all .exe/.dll in: $dir"

  while IFS= read -r -d '' file; do
    if sign_and_verify "$file"; then
      ((count++))
    else
      ((failed++))
    fi
  done < <(find "$dir" -maxdepth 2 -type f \( -name '*.exe' -o -name '*.dll' \) -print0)

  log "Batch complete: $count signed, $failed failed"

  if [[ $failed -gt 0 ]]; then
    return 1
  fi

  if [[ $count -eq 0 ]]; then
    warn "No .exe or .dll files found in $dir"
  fi

  return 0
}

# --- Main ---

usage() {
  cat <<EOF
Usage: $(basename "$0") [OPTIONS] <file|dir>

Options:
  --batch <dir>     Sign all .exe/.dll files in directory
  --verify <file>   Verify signature only (no signing)
  -h, --help        Show this help

Environment:
  PKCS11_MODULE     PKCS#11 library path (default: /usr/lib/libeTPkcs11.so)
  PKCS11_PIN        Token PIN (required)
  PKCS11_KEY        PKCS#11 key URI (default: pkcs11:token=TensorCash)
  CERT_FILE         Certificate chain PEM (required)
  TSA_URL           Timestamp authority (default: http://timestamp.sectigo.com)
EOF
  exit 1
}

if [[ $# -lt 1 ]]; then
  usage
fi

check_prerequisites

case "${1:-}" in
  --batch)
    [[ $# -lt 2 ]] && { err "--batch requires a directory argument"; usage; }
    batch_sign "$2"
    ;;
  --verify)
    [[ $# -lt 2 ]] && { err "--verify requires a file argument"; usage; }
    verify_file "$2"
    ;;
  -h|--help)
    usage
    ;;
  *)
    sign_and_verify "$1"
    ;;
esac
