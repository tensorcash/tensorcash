#!/usr/bin/env bash
# =============================================================================
# Local Validation Script for Desktop Packaging
# =============================================================================
#
# Run this BEFORE committing to catch obvious errors in:
#   - tor_prod.Dockerfile
#   - packaging/macos/build-macos.sh
#   - packaging/windows/build-windows.sh
#
# Usage:
#   ./packaging/validate-local.sh [--quick|--full]
#
# Options:
#   --quick   Syntax/lint checks only (default, ~30s)
#   --full    Also attempt Docker build and script dry-run (~5-10min)
#
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

ERRORS=0
WARNINGS=0
MODE="${1:---quick}"

log_pass() { echo -e "${GREEN}✓${NC} $*"; }
log_fail() { echo -e "${RED}✗${NC} $*"; ((ERRORS++)); }
log_warn() { echo -e "${YELLOW}!${NC} $*"; ((WARNINGS++)); }
log_info() { echo -e "  $*"; }

echo "=============================================="
echo "TensorCash Packaging Validation (${MODE})"
echo "=============================================="
echo ""

# =============================================================================
# 1. Check required files exist
# =============================================================================
echo "--- Checking required files ---"

REQUIRED_FILES=(
    "services/core-node/tor_prod.Dockerfile"
    "services/core-node/tor.Dockerfile"
    "services/core-node/src/api_server.py"
    "services/core-node/cosign-bridge/Cargo.toml"
    "services/core-node/bcore/CMakeLists.txt"
    "shared-utils/fb-schemas/proof.fbs"
    "shared-utils/fb-schemas/validation.fbs"
    "shared-utils/pow-utils/uint256_arithmetics.py"
    "packaging/macos/build-macos.sh"
    "packaging/windows/build-windows.sh"
    "packaging/common/validator-config.json"
)

for f in "${REQUIRED_FILES[@]}"; do
    if [[ -f "${REPO_ROOT}/${f}" ]]; then
        log_pass "${f}"
    else
        log_fail "${f} NOT FOUND"
    fi
done

echo ""

# =============================================================================
# 2. Validate Dockerfile syntax
# =============================================================================
echo "--- Validating Dockerfiles ---"

for dockerfile in \
    "services/core-node/tor_prod.Dockerfile" \
    "services/core-node/tor.Dockerfile"; do

    DFILE="${REPO_ROOT}/${dockerfile}"
    if [[ ! -f "${DFILE}" ]]; then
        continue
    fi

    # Check for basic syntax issues
    if grep -qE '^\s*COPY\s+--from=' "${DFILE}"; then
        # Check that COPY --from references exist as build stages
        STAGES=$(grep -oE 'FROM\s+\S+\s+AS\s+\S+' "${DFILE}" | awk '{print $4}' || true)
        COPY_FROMS=$(grep -oE 'COPY\s+--from=([a-zA-Z0-9_-]+)' "${DFILE}" | sed 's/COPY --from=//' || true)

        for ref in ${COPY_FROMS}; do
            if echo "${STAGES}" | grep -qw "${ref}"; then
                log_pass "${dockerfile}: COPY --from=${ref} references valid stage"
            else
                log_fail "${dockerfile}: COPY --from=${ref} references unknown stage"
            fi
        done
    fi

    # Check for common mistakes
    if grep -qE 'apt-get install.*&&.*rm -rf /var/lib/apt/lists' "${DFILE}"; then
        log_pass "${dockerfile}: apt cleanup pattern OK"
    elif grep -q 'apt-get install' "${DFILE}"; then
        log_warn "${dockerfile}: apt-get install without cleanup in same RUN"
    fi

    # Check heredoc syntax (<<'EOF' vs <<EOF)
    if grep -qE "<<[^'\"]*EOF" "${DFILE}"; then
        # Unquoted heredoc - variables will expand
        if grep -qE '<<EOF' "${DFILE}" && grep -qE '\$\{' "${DFILE}"; then
            log_warn "${dockerfile}: Unquoted heredoc with variables - may cause issues"
        fi
    fi

    # Check COPY sources exist
    COPY_SRCS=$(grep -E '^\s*COPY\s+' "${DFILE}" | grep -v -- '--from=' | awk '{print $2}' | grep -v '<<' || true)
    for src in ${COPY_SRCS}; do
        # Skip patterns and absolute paths
        if [[ "${src}" == /* ]] || [[ "${src}" == *'*'* ]]; then
            continue
        fi
        if [[ -e "${REPO_ROOT}/${src}" ]]; then
            log_pass "${dockerfile}: COPY source exists: ${src}"
        else
            log_fail "${dockerfile}: COPY source missing: ${src}"
        fi
    done
done

echo ""

# =============================================================================
# 3. Validate shell scripts
# =============================================================================
echo "--- Validating shell scripts ---"

SHELL_SCRIPTS=(
    "packaging/macos/build-macos.sh"
    "packaging/macos/sign-and-notarize.sh"
    "packaging/windows/build-windows.sh"
)

for script in "${SHELL_SCRIPTS[@]}"; do
    SPATH="${REPO_ROOT}/${script}"
    if [[ ! -f "${SPATH}" ]]; then
        continue
    fi

    # Check shebang
    if head -1 "${SPATH}" | grep -qE '^#!/'; then
        log_pass "${script}: has shebang"
    else
        log_fail "${script}: missing shebang"
    fi

    # Check for bash syntax errors
    if bash -n "${SPATH}" 2>/dev/null; then
        log_pass "${script}: bash syntax OK"
    else
        log_fail "${script}: bash syntax errors"
        bash -n "${SPATH}" 2>&1 | head -5 | while read -r line; do
            log_info "  ${line}"
        done
    fi

    # Check for shellcheck if available
    if command -v shellcheck &>/dev/null; then
        SC_ERRORS=$(shellcheck -S error "${SPATH}" 2>&1 | wc -l || echo "0")
        if [[ "${SC_ERRORS}" -eq 0 ]]; then
            log_pass "${script}: shellcheck OK"
        else
            log_warn "${script}: shellcheck found ${SC_ERRORS} issues"
        fi
    fi

    # Check executable bit
    if [[ -x "${SPATH}" ]]; then
        log_pass "${script}: is executable"
    else
        log_warn "${script}: not executable (run: chmod +x ${script})"
    fi
done

echo ""

# =============================================================================
# 4. Validate JSON files
# =============================================================================
echo "--- Validating JSON files ---"

JSON_FILES=(
    "packaging/common/validator-config.json"
)

for jfile in "${JSON_FILES[@]}"; do
    JPATH="${REPO_ROOT}/${jfile}"
    if [[ ! -f "${JPATH}" ]]; then
        continue
    fi

    if python3 -m json.tool "${JPATH}" >/dev/null 2>&1; then
        log_pass "${jfile}: valid JSON"
    else
        log_fail "${jfile}: invalid JSON"
    fi
done

echo ""

# =============================================================================
# 5. Check Python imports (api_server.py dependencies)
# =============================================================================
echo "--- Checking Python imports ---"

PYTHON_CHECK=$(cat <<'PYEOF'
import sys
errors = []

# Check api_server.py can at least parse
try:
    import ast
    with open('services/core-node/src/api_server.py', 'r') as f:
        ast.parse(f.read())
    print('✓ api_server.py: syntax OK')
except SyntaxError as e:
    print(f'✗ api_server.py: syntax error: {e}')
    errors.append('api_server.py')

# Check required modules are importable (in a real env)
required_modules = ['fastapi', 'uvicorn', 'httpx', 'pydantic']
for mod in required_modules:
    try:
        __import__(mod)
        print(f'✓ {mod}: importable')
    except ImportError:
        print(f'! {mod}: not installed (OK if just validating syntax)')

sys.exit(1 if errors else 0)
PYEOF
)

(cd "${REPO_ROOT}" && python3 -c "${PYTHON_CHECK}") || ((ERRORS++))

echo ""

# =============================================================================
# 6. Full validation (optional)
# =============================================================================
if [[ "${MODE}" == "--full" ]]; then
    echo "--- Full validation (Docker build test) ---"

    # Test tor_prod.Dockerfile builds (first stage only)
    echo "Testing tor_prod.Dockerfile (rust-builder stage)..."
    if docker build \
        -f "${REPO_ROOT}/services/core-node/tor_prod.Dockerfile" \
        --target rust-builder \
        -t tensorcash-validate:rust-builder \
        "${REPO_ROOT}" 2>&1 | tail -20; then
        log_pass "tor_prod.Dockerfile: rust-builder stage builds"
    else
        log_fail "tor_prod.Dockerfile: rust-builder stage failed"
    fi

    # Test build-macos.sh --help works
    echo ""
    echo "Testing build-macos.sh --help..."
    if "${REPO_ROOT}/packaging/macos/build-macos.sh" --help >/dev/null 2>&1; then
        log_pass "build-macos.sh: --help works"
    else
        log_warn "build-macos.sh: --help failed (may be OK)"
    fi
fi

echo ""

# =============================================================================
# Summary
# =============================================================================
echo "=============================================="
echo "Validation Summary"
echo "=============================================="
echo ""

if [[ ${ERRORS} -eq 0 ]] && [[ ${WARNINGS} -eq 0 ]]; then
    echo -e "${GREEN}All checks passed!${NC}"
    exit 0
elif [[ ${ERRORS} -eq 0 ]]; then
    echo -e "${YELLOW}${WARNINGS} warning(s), 0 errors${NC}"
    echo "Warnings are informational - you can proceed with commit."
    exit 0
else
    echo -e "${RED}${ERRORS} error(s), ${WARNINGS} warning(s)${NC}"
    echo "Please fix errors before committing."
    exit 1
fi
