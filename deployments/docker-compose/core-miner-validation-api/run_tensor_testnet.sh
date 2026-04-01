#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE_COMPOSE_VLLM="${ROOT_DIR}/docker-compose.yaml"
BASE_COMPOSE_LLAMA="${ROOT_DIR}/docker-compose_llamacpp.yaml"
BASE_COMPOSE="${BASE_COMPOSE_VLLM}"
OVERRIDE_COMPOSE="${ROOT_DIR}/docker-compose.tensor-test.override.yml"
CONF_SRC="${ROOT_DIR}/bitcoin.tensor-test.conf"
MODE_OVERRIDE_COMPOSE=""
cleanup_mode_override() {
  if [[ -n "${MODE_OVERRIDE_COMPOSE}" && -f "${MODE_OVERRIDE_COMPOSE}" ]]; then
    rm -f "${MODE_OVERRIDE_COMPOSE}"
  fi
}
trap cleanup_mode_override EXIT

ENV_FILE="${ENV_FILE:-}"
NODE_START_FLAGS="${NODE_START_FLAGS:---http}"
NODE_START_FLAGS_OVERRIDE=""
ISOLATED_NODE="${ISOLATED_NODE:-false}"
ISOLATED_NODE_OVERRIDE=""
VIEWER_MODE="false"
QT_MODE="false"
MINER_CPU_VALIDATOR_GPU_MODE="false"
ENV_FILE_SET_KEYS=()
ENV_FILE_SKIPPED_KEYS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --http)
      NODE_START_FLAGS_OVERRIDE="--http"
      shift
      ;;
    --desktop)
      NODE_START_FLAGS_OVERRIDE="--desktop"
      shift
      ;;
    --env-file)
      if [[ $# -lt 2 ]]; then
        echo "ERROR: --env-file requires a file path"
        exit 1
      fi
      ENV_FILE="$2"
      shift 2
      ;;
    --isolated)
      ISOLATED_NODE_OVERRIDE="true"
      shift
      ;;
    --non-isolated)
      ISOLATED_NODE_OVERRIDE="false"
      shift
      ;;
    --viewer)
      VIEWER_MODE="true"
      shift
      ;;
    --qt)
      QT_MODE="true"
      shift
      ;;
    --miner-cpu-validator-gpu)
      MINER_CPU_VALIDATOR_GPU_MODE="true"
      shift
      ;;
    *)
      echo "WARNING: Unknown argument '$1' (ignored)"
      shift
      ;;
  esac
done

load_env_file() {
  local file="$1"
  if [[ ! -f "${file}" ]]; then
    echo "ERROR: env file not found: ${file}"
    exit 1
  fi

  while IFS= read -r line || [[ -n "${line}" ]]; do
    line="${line%$'\r'}"
    [[ -z "${line}" ]] && continue
    [[ "${line}" =~ ^[[:space:]]*# ]] && continue

    # Support optional "export KEY=VALUE" lines.
    if [[ "${line}" =~ ^[[:space:]]*export[[:space:]]+ ]]; then
      line="${line#export }"
      line="${line#"${line%%[![:space:]]*}"}"
    fi

    if [[ "${line}" =~ ^[[:space:]]*([A-Za-z_][A-Za-z0-9_]*)[[:space:]]*=(.*)$ ]]; then
      local key="${BASH_REMATCH[1]}"
      local value="${BASH_REMATCH[2]}"

      # Trim outer spaces from value.
      value="${value#"${value%%[![:space:]]*}"}"
      value="${value%"${value##*[![:space:]]}"}"

      # Remove matching wrapping quotes.
      if [[ ${#value} -ge 2 ]]; then
        if [[ "${value:0:1}" == "\"" && "${value: -1}" == "\"" ]]; then
          value="${value:1:${#value}-2}"
        elif [[ "${value:0:1}" == "'" && "${value: -1}" == "'" ]]; then
          value="${value:1:${#value}-2}"
        fi
      fi

      # CLI-provided env vars must win over env-file values.
      if [[ -z "${!key+x}" ]]; then
        export "${key}=${value}"
        ENV_FILE_SET_KEYS+=("${key}")
      else
        ENV_FILE_SKIPPED_KEYS+=("${key}")
      fi
    else
      echo "WARNING: Skipping invalid env line in ${file}: ${line}"
    fi
  done < "${file}"
}

if [[ -n "${ENV_FILE}" ]]; then
  load_env_file "${ENV_FILE}"
fi

if [[ -n "${NODE_START_FLAGS_OVERRIDE}" ]]; then
  NODE_START_FLAGS="${NODE_START_FLAGS_OVERRIDE}"
fi
if [[ -n "${ISOLATED_NODE_OVERRIDE}" ]]; then
  ISOLATED_NODE="${ISOLATED_NODE_OVERRIDE}"
fi

DATA_DIR="${DATA_DIR:-./bcore_data}"
CONF_DST="${DATA_DIR}/bitcoin.conf"
TOR_DIR="${TOR_DIR:-./tor_data}"
TOR_MODE="${TOR_MODE:-}"

if [[ ! -f "${CONF_DST}" ]]; then
  echo "Missing ${CONF_DST}."
  echo "Create it from: ${CONF_SRC}"
  echo "  cp \"${CONF_SRC}\" \"${CONF_DST}\""
  exit 1
fi

echo "Starting TensorCash tensor-test stack..."
echo "Using DATA_DIR=${DATA_DIR}"
echo "Using TOR_DIR=${TOR_DIR}"
echo "Using ENV_FILE=${ENV_FILE:-<none>}"
if [[ -n "${ENV_FILE}" ]]; then
  if [[ "${#ENV_FILE_SET_KEYS[@]}" -gt 0 ]]; then
    echo "ENV_FILE loaded key-values:"
    for key in "${ENV_FILE_SET_KEYS[@]}"; do
      echo "  ${key}=${!key-}"
    done
  else
    echo "ENV_FILE loaded keys: <none>"
  fi
  if [[ "${#ENV_FILE_SKIPPED_KEYS[@]}" -gt 0 ]]; then
    echo "ENV_FILE skipped keys (already set in environment): ${ENV_FILE_SKIPPED_KEYS[*]}"
  fi
fi

API_KEY="${API_KEY:-super-secret}"
MODEL_API_KEY="${MODEL_API_KEY:-super-secret}"
ENABLE_VLLM_BACKEND="${ENABLE_VLLM_BACKEND:-true}"
ENABLE_LLAMA_BACKEND="${ENABLE_LLAMA_BACKEND:-false}"
ENABLE_CORE_NODE="${ENABLE_CORE_NODE:-true}"
ENABLE_VERIFICATION_API="${ENABLE_VERIFICATION_API:-false}"
ENABLE_MINER_PROXY="${ENABLE_MINER_PROXY:-true}"
ENABLE_IPFS_MODELS="${ENABLE_IPFS_MODELS:-true}"
GUI_MODE="${GUI_MODE:-false}"

# Enforce verification-api mode by node startup mode, regardless of incoming env.
if [[ "${NODE_START_FLAGS}" == "--desktop" ]]; then
  ENABLE_VERIFICATION_API="true"
  unset VALIDATOR_BASE_UR VALIDATOR_BASE_URL VALIDATOR_API_KEY VALIDATOR_HTTP_TIMEOUT_MS
elif [[ "${NODE_START_FLAGS}" == "--http" ]]; then
  ENABLE_VERIFICATION_API="false"
  if [[ -z "${VALIDATOR_API_KEY:-}" ]]; then
    echo "ERROR: VALIDATOR_API_KEY must be set and non-empty in http mode"
    exit 1
  fi
fi

if [[ "${QT_MODE}" == "true" ]]; then
  GUI_MODE="true"
fi

# Resolve GPU runtime for docker compose services.
# Prefer runtime name "nvidia" because containerd/docker-compose setups often
# ignore binary paths like "/usr/bin/nvidia-container-runtime".
resolve_gpu_runtime() {
  local detected=""
  if [[ -x "/usr/bin/nvidia-container-runtime" ]]; then
    echo "/usr/bin/nvidia-container-runtime"
    return 0
  fi
  local info=""
  if command -v docker >/dev/null 2>&1; then
    info="$(docker info 2>/dev/null || true)"
    if [[ -z "${info}" ]] && command -v sudo >/dev/null 2>&1; then
      info="$(sudo docker info 2>/dev/null || true)"
    fi
    if [[ "${info}" == *" nvidia "* ]] || [[ "${info}" == *$'\n''nvidia'* ]]; then
      detected="nvidia"
    fi
  fi
  echo "${detected}"
}

# Optional split mode: miner on CPU, validator on GPU.
if [[ "${MINER_CPU_VALIDATOR_GPU_MODE}" == "true" ]]; then
  BASE_COMPOSE="${BASE_COMPOSE_LLAMA}"
  ENABLE_VLLM_BACKEND="false"
  ENABLE_LLAMA_BACKEND="true"
  ENABLE_IPFS_MODELS="false"
  export TARGET_URL="${TARGET_URL:-http://llama-backend:8000}"
  export LLAMA_CPP="${LLAMA_CPP:-True}"
  resolved_validator_runtime="$(resolve_gpu_runtime)"
  # In split mode, force GPU-capable runtime. If env file pins plain runc,
  # override it to avoid silently running verifier on CPU.
  if [[ -n "${VALIDATOR_DOCKER_RUNTIME:-}" ]]; then
    case "${VALIDATOR_DOCKER_RUNTIME}" in
      runc|io.containerd.runc.v2)
        echo "WARNING: VALIDATOR_DOCKER_RUNTIME=${VALIDATOR_DOCKER_RUNTIME} disables GPU. Overriding for split mode."
        export VALIDATOR_DOCKER_RUNTIME="${resolved_validator_runtime}"
        ;;
      *)
        export VALIDATOR_DOCKER_RUNTIME
        ;;
    esac
  else
    export VALIDATOR_DOCKER_RUNTIME="${resolved_validator_runtime}"
  fi
  MODE_OVERRIDE_COMPOSE="$(mktemp /tmp/tensor-test.mode-override.XXXXXX.yml)"
  cat > "${MODE_OVERRIDE_COMPOSE}" <<'YAML'
services:
  verification-api:
    gpus: all
    environment:
      NVIDIA_VISIBLE_DEVICES: "all"
      NVIDIA_DRIVER_CAPABILITIES: "compute,utility"
YAML
  if [[ -n "${VALIDATOR_DOCKER_RUNTIME}" ]]; then
    cat >> "${MODE_OVERRIDE_COMPOSE}" <<'YAML'
    runtime: ${VALIDATOR_DOCKER_RUNTIME}
YAML
  fi
else
  # Default mode: vLLM miner on GPU.
  BASE_COMPOSE="${BASE_COMPOSE_VLLM}"
  ENABLE_VLLM_BACKEND="true"
  ENABLE_LLAMA_BACKEND="false"
  ENABLE_IPFS_MODELS="true"
  export TARGET_URL="http://vllm-backend:8000"
  export LLAMA_CPP="false"
  resolved_miner_runtime="$(resolve_gpu_runtime)"
  if [[ -n "${MINER_DOCKER_RUNTIME:-}" ]]; then
    case "${MINER_DOCKER_RUNTIME}" in
      runc|io.containerd.runc.v2)
        echo "WARNING: MINER_DOCKER_RUNTIME=${MINER_DOCKER_RUNTIME} disables GPU. Overriding for default mode."
        export MINER_DOCKER_RUNTIME="${resolved_miner_runtime}"
        ;;
      *)
        export MINER_DOCKER_RUNTIME
        ;;
    esac
  else
    export MINER_DOCKER_RUNTIME="${resolved_miner_runtime}"
  fi
  MODE_OVERRIDE_COMPOSE="$(mktemp /tmp/tensor-test.mode-override.XXXXXX.yml)"
  cat > "${MODE_OVERRIDE_COMPOSE}" <<'YAML'
services:
  vllm-backend:
    gpus: all
    environment:
      NVIDIA_VISIBLE_DEVICES: "all"
      NVIDIA_DRIVER_CAPABILITIES: "compute,utility"
YAML
  if [[ -n "${MINER_DOCKER_RUNTIME:-}" ]]; then
    cat >> "${MODE_OVERRIDE_COMPOSE}" <<'YAML'
    runtime: ${MINER_DOCKER_RUNTIME}
YAML
  fi
  VLLM_DEVICE="${VLLM_DEVICE:-auto}"
fi

# Viewer mode is independent from --http/--desktop and controls miner-side services.
if [[ "${VIEWER_MODE}" == "true" ]]; then
  ENABLE_VLLM_BACKEND="false"
  ENABLE_MINER_PROXY="false"
  if [[ "${NODE_START_FLAGS}" == "--desktop" ]]; then
    ENABLE_IPFS_MODELS="true"
  elif [[ "${NODE_START_FLAGS}" == "--http" ]]; then
    ENABLE_IPFS_MODELS="false"
  fi
fi

echo "ROOT_DIR ${ROOT_DIR}"
echo "BASE_COMPOSE ${BASE_COMPOSE}"
echo "OVERRIDE_COMPOSE ${OVERRIDE_COMPOSE}"
if [[ -n "${MODE_OVERRIDE_COMPOSE}" ]]; then
  echo "MODE_OVERRIDE_COMPOSE ${MODE_OVERRIDE_COMPOSE}"
fi
echo "CONF_SRC ${CONF_SRC}"
echo "DATA_DIR ${DATA_DIR}"
echo "CONF_DST ${CONF_DST}"
echo "API_KEY ${API_KEY}"
echo "MODEL_API_KEY ${MODEL_API_KEY}"
echo "NODE_START_FLAGS ${NODE_START_FLAGS}"
echo "ISOLATED_NODE ${ISOLATED_NODE}"
echo "VIEWER_MODE ${VIEWER_MODE}"
echo "QT_MODE ${QT_MODE}"
echo "MINER_CPU_VALIDATOR_GPU_MODE ${MINER_CPU_VALIDATOR_GPU_MODE}"
echo "GUI_MODE ${GUI_MODE}"
echo "ENABLE_VERIFICATION_API ${ENABLE_VERIFICATION_API}"
echo "ENABLE_CORE_NODE ${ENABLE_CORE_NODE}"
echo "ENABLE_VLLM_BACKEND ${ENABLE_VLLM_BACKEND}"
echo "ENABLE_LLAMA_BACKEND ${ENABLE_LLAMA_BACKEND}"
echo "ENABLE_MINER_PROXY ${ENABLE_MINER_PROXY}"
echo "ENABLE_IPFS_MODELS ${ENABLE_IPFS_MODELS}"

normalize_tor_mode() {
  echo "${1,,}" | tr -d ' '
}

upsert_conf_key() {
  local key="$1"
  local value="$2"
  local file="$3"
  if rg -n "^[[:space:]]*${key}[[:space:]]*=" "${file}" >/dev/null 2>&1; then
    sed -i -E "s|^[[:space:]]*${key}[[:space:]]*=.*$|${key}=${value}|" "${file}"
  else
    echo "${key}=${value}" >> "${file}"
  fi
}

remove_conf_key() {
  local key="$1"
  local file="$2"
  sed -i -E "/^[[:space:]]*${key}[[:space:]]*=.*/d" "${file}"
}

apply_tor_mode() {
  local mode
  mode="$(normalize_tor_mode "${TOR_MODE}")"

  if [[ -z "${mode}" ]]; then
    echo "TOR_MODE not set: keeping existing Tor settings in ${CONF_DST}"
    return 0
  fi

  case "${mode}" in
    static)
      local hs_hostname="${TOR_DIR}/tensorcash-service/hostname"
      if [[ ! -f "${hs_hostname}" ]]; then
        echo "ERROR: TOR_MODE=static requires ${hs_hostname}"
        echo "Start once in dynamic mode (or ensure Tor hidden service is initialized), then retry."
        exit 1
      fi
      local onion_addr
      onion_addr="$(tr -d '\r\n' < "${hs_hostname}")"
      if [[ -z "${onion_addr}" ]]; then
        echo "ERROR: Empty onion hostname in ${hs_hostname}"
        exit 1
      fi
      upsert_conf_key "discover" "1" "${CONF_DST}"
      upsert_conf_key "torcontrol" "127.0.0.1:9051" "${CONF_DST}"
      upsert_conf_key "listenonion" "0" "${CONF_DST}"
      upsert_conf_key "externalip" "${onion_addr}:29241" "${CONF_DST}"
      echo "Applied TOR_MODE=static with externalip=${onion_addr}:29241"
      ;;
    dynamic)
      upsert_conf_key "discover" "1" "${CONF_DST}"
      upsert_conf_key "torcontrol" "127.0.0.1:9051" "${CONF_DST}"
      upsert_conf_key "listenonion" "1" "${CONF_DST}"
      remove_conf_key "externalip" "${CONF_DST}"
      echo "Applied TOR_MODE=dynamic (ephemeral onion via Tor ControlPort)"
      ;;
    *)
      echo "ERROR: Unsupported TOR_MODE='${TOR_MODE}'. Use 'static' or 'dynamic'."
      exit 1
      ;;
  esac
}

apply_tor_mode

apply_isolation_mode() {
  local mode
  mode="$(normalize_tor_mode "${ISOLATED_NODE}")"

  case "${mode}" in
    1|true|yes|y|on)
      upsert_conf_key "listen" "0" "${CONF_DST}"
      upsert_conf_key "discover" "0" "${CONF_DST}"
      upsert_conf_key "dnsseed" "0" "${CONF_DST}"
      upsert_conf_key "fixedseeds" "0" "${CONF_DST}"
      upsert_conf_key "upnp" "0" "${CONF_DST}"
      upsert_conf_key "natpmp" "0" "${CONF_DST}"
      upsert_conf_key "connect" "0" "${CONF_DST}"
      upsert_conf_key "listenonion" "0" "${CONF_DST}"
      remove_conf_key "addnode" "${CONF_DST}"
      remove_conf_key "seednode" "${CONF_DST}"
      remove_conf_key "externalip" "${CONF_DST}"
      echo "Applied isolated mode: no inbound/outbound peers, no seeding/discovery."
      ;;
    0|false|no|n|off|"")
      echo "ISOLATED_NODE disabled: keeping current peer/discovery settings in ${CONF_DST}"
      ;;
    *)
      echo "ERROR: Unsupported ISOLATED_NODE='${ISOLATED_NODE}'. Use true/false."
      exit 1
      ;;
  esac
}

apply_isolation_mode

# Prefer sudo when docker socket isn't accessible.
DOCKER_BIN="docker"
if ! docker info >/dev/null 2>&1; then
  DOCKER_BIN="sudo docker"
fi

is_enabled() {
  case "${1,,}" in
    1|true|yes|y|on) return 0 ;;
    0|false|no|n|off) return 1 ;;
    *) return 0 ;;
  esac
}

services=()
if is_enabled "${ENABLE_VLLM_BACKEND}"; then services+=("vllm-backend"); fi
if is_enabled "${ENABLE_LLAMA_BACKEND}"; then
  services+=("llama-model-prep")
  services+=("llama-backend")
fi
if is_enabled "${ENABLE_CORE_NODE}"; then services+=("core-node"); fi
if is_enabled "${ENABLE_VERIFICATION_API}"; then services+=("verification-api"); fi
if is_enabled "${ENABLE_MINER_PROXY}"; then services+=("miner-proxy"); fi
if is_enabled "${ENABLE_IPFS_MODELS}"; then services+=("ipfs-models"); fi

if [[ "${#services[@]}" -eq 0 ]]; then
  echo "No services enabled. Set ENABLE_* variables to true."
  exit 1
fi

compose_env_vars=(
  "API_KEY=${API_KEY}"
  "MODEL_API_KEY=${MODEL_API_KEY}"
  "VALIDATOR_API_KEY=${VALIDATOR_API_KEY:-}"
  "VALIDATOR_BASE_URL=${VALIDATOR_BASE_URL:-}"
  "VALIDATOR_HTTP_TIMEOUT_MS=${VALIDATOR_HTTP_TIMEOUT_MS:-}"
  "OPERATOR_API_KEY=${OPERATOR_API_KEY:-}"
  "OPERATOR_API_BASE_URL=${OPERATOR_API_BASE_URL:-}"
  "DATA_DIR=${DATA_DIR}"
  "NODE_START_FLAGS=${NODE_START_FLAGS}"
  "GUI_MODE=${GUI_MODE}"
  "ISOLATED_NODE=${ISOLATED_NODE}"
  "MINER_DOCKER_RUNTIME=${MINER_DOCKER_RUNTIME:-}"
  "VLLM_DEVICE=${VLLM_DEVICE:-}"
  "VALIDATOR_DOCKER_RUNTIME=${VALIDATOR_DOCKER_RUNTIME:-}"
  "TARGET_URL=${TARGET_URL:-}"
  "LLAMA_CPP=${LLAMA_CPP:-}"
)
for key in "${ENV_FILE_SET_KEYS[@]}"; do
  compose_env_vars+=("${key}=${!key-}")
done

if [[ "${DOCKER_BIN}" == "sudo docker" ]]; then
  preserve_env_keys=(
    API_KEY MODEL_API_KEY
    VALIDATOR_API_KEY VALIDATOR_BASE_URL VALIDATOR_HTTP_TIMEOUT_MS
    OPERATOR_API_KEY OPERATOR_API_BASE_URL
    DATA_DIR NODE_START_FLAGS GUI_MODE ISOLATED_NODE
    MINER_DOCKER_RUNTIME VLLM_DEVICE VALIDATOR_DOCKER_RUNTIME TARGET_URL LLAMA_CPP
    "${ENV_FILE_SET_KEYS[@]}"
  )
  preserve_env_csv="$(IFS=,; echo "${preserve_env_keys[*]}")"
  compose_args=(-f "${BASE_COMPOSE}" -f "${OVERRIDE_COMPOSE}")
  if [[ -n "${MODE_OVERRIDE_COMPOSE}" ]]; then
    compose_args+=(-f "${MODE_OVERRIDE_COMPOSE}")
  fi
  env "${compose_env_vars[@]}" sudo --preserve-env="${preserve_env_csv}" docker compose "${compose_args[@]}" up -d "${services[@]}"
else
  compose_args=(-f "${BASE_COMPOSE}" -f "${OVERRIDE_COMPOSE}")
  if [[ -n "${MODE_OVERRIDE_COMPOSE}" ]]; then
    compose_args+=(-f "${MODE_OVERRIDE_COMPOSE}")
  fi
  env "${compose_env_vars[@]}" docker compose "${compose_args[@]}" up -d "${services[@]}"
fi

# In split mode (miner CPU + validator GPU), force-recreate verifier so runtime switch is guaranteed.
if [[ "${MINER_CPU_VALIDATOR_GPU_MODE}" == "true" ]] && is_enabled "${ENABLE_VERIFICATION_API}"; then
  echo "Recreating verification-api to enforce GPU runtime: ${VALIDATOR_DOCKER_RUNTIME:-<unset>}"
  if [[ "${DOCKER_BIN}" == "sudo docker" ]]; then
    compose_args=(-f "${BASE_COMPOSE}" -f "${OVERRIDE_COMPOSE}")
    if [[ -n "${MODE_OVERRIDE_COMPOSE}" ]]; then
      compose_args+=(-f "${MODE_OVERRIDE_COMPOSE}")
    fi
    env "${compose_env_vars[@]}" sudo --preserve-env="${preserve_env_csv}" docker compose "${compose_args[@]}" up -d --force-recreate verification-api
  else
    compose_args=(-f "${BASE_COMPOSE}" -f "${OVERRIDE_COMPOSE}")
    if [[ -n "${MODE_OVERRIDE_COMPOSE}" ]]; then
      compose_args+=(-f "${MODE_OVERRIDE_COMPOSE}")
    fi
    env "${compose_env_vars[@]}" docker compose "${compose_args[@]}" up -d --force-recreate verification-api
  fi
fi

# Split mode safety check: verification-api must actually see CUDA.
if [[ "${MINER_CPU_VALIDATOR_GPU_MODE}" == "true" ]] && is_enabled "${ENABLE_VERIFICATION_API}"; then
  verify_compose_args=(-f "${BASE_COMPOSE}" -f "${OVERRIDE_COMPOSE}")
  if [[ -n "${MODE_OVERRIDE_COMPOSE}" ]]; then
    verify_compose_args+=(-f "${MODE_OVERRIDE_COMPOSE}")
  fi
  verify_cid="$(${DOCKER_BIN} compose "${verify_compose_args[@]}" ps -q verification-api 2>/dev/null || true)"
  if [[ -n "${verify_cid}" ]]; then
    if ! ${DOCKER_BIN} exec "${verify_cid}" python -c 'import torch,sys; sys.exit(0 if torch.cuda.is_available() else 1)' >/dev/null 2>&1; then
      echo "ERROR: verification-api started but CUDA is not available inside container."
      echo "Expected GPU mode for --miner-cpu-validator-gpu."
      echo "Check NVIDIA runtime/toolkit on host and container launch flags."
      exit 1
    fi
  fi
fi

if [[ "${MINER_CPU_VALIDATOR_GPU_MODE}" != "true" ]] && is_enabled "${ENABLE_VLLM_BACKEND}"; then
  echo "Recreating vllm-backend to enforce GPU runtime: ${MINER_DOCKER_RUNTIME:-<unset>}"
  if [[ "${DOCKER_BIN}" == "sudo docker" ]]; then
    compose_args=(-f "${BASE_COMPOSE}" -f "${OVERRIDE_COMPOSE}")
    if [[ -n "${MODE_OVERRIDE_COMPOSE}" ]]; then
      compose_args+=(-f "${MODE_OVERRIDE_COMPOSE}")
    fi
    env "${compose_env_vars[@]}" sudo --preserve-env="${preserve_env_csv}" docker compose "${compose_args[@]}" up -d --force-recreate vllm-backend
  else
    compose_args=(-f "${BASE_COMPOSE}" -f "${OVERRIDE_COMPOSE}")
    if [[ -n "${MODE_OVERRIDE_COMPOSE}" ]]; then
      compose_args+=(-f "${MODE_OVERRIDE_COMPOSE}")
    fi
    env "${compose_env_vars[@]}" docker compose "${compose_args[@]}" up -d --force-recreate vllm-backend
  fi
fi

# Default mode safety check: vLLM backend must actually see CUDA.
if [[ "${MINER_CPU_VALIDATOR_GPU_MODE}" != "true" ]] && is_enabled "${ENABLE_VLLM_BACKEND}"; then
  verify_compose_args=(-f "${BASE_COMPOSE}" -f "${OVERRIDE_COMPOSE}")
  if [[ -n "${MODE_OVERRIDE_COMPOSE}" ]]; then
    verify_compose_args+=(-f "${MODE_OVERRIDE_COMPOSE}")
  fi
  vllm_cid="$(${DOCKER_BIN} compose "${verify_compose_args[@]}" ps -q vllm-backend 2>/dev/null || true)"
  if [[ -n "${vllm_cid}" ]]; then
    if ! ${DOCKER_BIN} exec "${vllm_cid}" python -c 'import torch,sys; sys.exit(0 if torch.cuda.is_available() else 1)' >/dev/null 2>&1; then
      echo "ERROR: vllm-backend started but CUDA is not available inside container."
      echo "Expected GPU mode when --miner-cpu-validator-gpu is NOT set."
      echo "Check NVIDIA runtime/toolkit on host and container launch flags."
      exit 1
    fi
  fi
fi

# Wait for miner backend to become healthy.
MINER_BACKEND_SERVICE=""
if is_enabled "${ENABLE_VLLM_BACKEND}"; then
  MINER_BACKEND_SERVICE="vllm-backend"
elif is_enabled "${ENABLE_LLAMA_BACKEND}"; then
  MINER_BACKEND_SERVICE="llama-backend"
fi

MINER_BACKEND_CONTAINER_ID=""
if [[ -n "${MINER_BACKEND_SERVICE}" ]]; then
  backend_compose_args=(-f "${BASE_COMPOSE}" -f "${OVERRIDE_COMPOSE}")
  if [[ -n "${MODE_OVERRIDE_COMPOSE}" ]]; then
    backend_compose_args+=(-f "${MODE_OVERRIDE_COMPOSE}")
  fi
  MINER_BACKEND_CONTAINER_ID="$(${DOCKER_BIN} compose "${backend_compose_args[@]}" ps -q "${MINER_BACKEND_SERVICE}" || true)"
fi

if [[ -n "${MINER_BACKEND_CONTAINER_ID}" ]]; then
  VLLM_HEALTH_TIMEOUT_SEC="${VLLM_HEALTH_TIMEOUT_SEC:-300}"
  VLLM_HEALTH_INTERVAL_SEC="${VLLM_HEALTH_INTERVAL_SEC:-5}"
  VLLM_ELAPSED=0
  while true; do
    VLLM_HEALTH="$(${DOCKER_BIN} inspect --format '{{.State.Health.Status}}' "${MINER_BACKEND_CONTAINER_ID}" 2>/dev/null || true)"
    if [[ "${VLLM_HEALTH}" == "healthy" ]]; then
      echo "${MINER_BACKEND_SERVICE} is healthy."
      break
    fi
    if [[ "${VLLM_ELAPSED}" -ge "${VLLM_HEALTH_TIMEOUT_SEC}" ]]; then
      echo "Timed out waiting for ${MINER_BACKEND_SERVICE} to become healthy."
      break
    fi
    echo "Waiting for ${MINER_BACKEND_SERVICE} health... (${VLLM_ELAPSED}s)"
    sleep "${VLLM_HEALTH_INTERVAL_SEC}"
    VLLM_ELAPSED=$((VLLM_ELAPSED + VLLM_HEALTH_INTERVAL_SEC))
  done
fi

echo ""
echo "Status:"
if [[ -n "${MODE_OVERRIDE_COMPOSE}" ]]; then
  ${DOCKER_BIN} compose -f "${BASE_COMPOSE}" -f "${OVERRIDE_COMPOSE}" -f "${MODE_OVERRIDE_COMPOSE}" ps
else
  ${DOCKER_BIN} compose -f "${BASE_COMPOSE}" -f "${OVERRIDE_COMPOSE}" ps
fi
echo ""
echo "Tips:"
if [[ -n "${MODE_OVERRIDE_COMPOSE}" ]]; then
  echo "  ${DOCKER_BIN} compose -f \"${BASE_COMPOSE}\" -f \"${OVERRIDE_COMPOSE}\" -f \"${MODE_OVERRIDE_COMPOSE}\" logs -f core-node"
  echo "  ${DOCKER_BIN} compose -f \"${BASE_COMPOSE}\" -f \"${OVERRIDE_COMPOSE}\" -f \"${MODE_OVERRIDE_COMPOSE}\" logs -f miner-proxy"
  echo "  ${DOCKER_BIN} compose -f \"${BASE_COMPOSE}\" -f \"${OVERRIDE_COMPOSE}\" -f \"${MODE_OVERRIDE_COMPOSE}\" logs -f verification-api"
else
  echo "  ${DOCKER_BIN} compose -f \"${BASE_COMPOSE}\" -f \"${OVERRIDE_COMPOSE}\" logs -f core-node"
  echo "  ${DOCKER_BIN} compose -f \"${BASE_COMPOSE}\" -f \"${OVERRIDE_COMPOSE}\" logs -f miner-proxy"
  echo "  ${DOCKER_BIN} compose -f \"${BASE_COMPOSE}\" -f \"${OVERRIDE_COMPOSE}\" logs -f verification-api"
fi
