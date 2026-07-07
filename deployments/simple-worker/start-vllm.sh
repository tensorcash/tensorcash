#!/usr/bin/env bash
set -e

# Get model from environment variable or use default
: ${MODEL_NAME:=Qwen/Qwen3-8B}
: ${MAX_MODEL_LEN:=8192}
: ${DEVICE:=auto}
: ${GPU_MEM_UTIL:=0.9}
: ${API_KEY:=internal-secret}

# Set environment variables for PoW
export VLLM_ENABLE_POW=1

# PoW ZMQ publishing — broker egress: the single destination is the
# miner-proxy ProofCollector. POW_PROXY_ENABLE must be falsy here; the
# writer hard-rejects broker mode + proxy dual-publish
# (zmq_pow_writer.py PowEgressConfigError). Audit proofs from this
# instance ride the same socket with proof_purpose=audit.
export POW_EGRESS_MODE=broker
export POW_PROXY_ENABLE=false
export ZMQ_PUSH_HOST=127.0.0.1
export ZMQ_PUSH_PORT=${PROOF_COLLECTOR_PORT:-7002}
# C++ proof assembly — REQUIRED. This is the primary (e.g. 27B) instance
# whose audit proofs must be assembled identically to the mining fleet;
# the Python assembler is a dev-only fallback. common_sampler_helper now
# fails hard if cpp is requested but proof_processor.so is missing (unless
# POW_PROCESSOR_FALLBACK=1), so a broken build surfaces at startup rather
# than silently downgrading the audit path to Python.
export POW_PROCESSOR_MODE=${POW_PROCESSOR_MODE:-cpp}
# Required for the miner-proxy's background-response dummy/audit pool
# (POST /v1/responses store=True); vLLM 400s without it. k8 fleet sets it.
export VLLM_ENABLE_RESPONSES_API_STORE=1

echo "[vLLM] Starting with model: $MODEL_NAME (max_length: $MAX_MODEL_LEN, device: $DEVICE)"
echo "[vLLM] PoW publishing to tcp://${ZMQ_PUSH_HOST}:${ZMQ_PUSH_PORT} (processor: ${POW_PROCESSOR_MODE})"

# Build vLLM command — Qwen3.5-specific recipe.
#
#  --tool-call-parser qwen3_xml
#       Qwen3.5 emits the Qwen-XML <tool_call>/<function=>/<parameter=>
#       grammar. ``qwen3_coder`` is regex-based and breaks on long inputs +
#       special chars (vLLM #36769). ``qwen3_xml`` uses Python's
#       xml.parsers.expat C engine for native streaming — ~90% reliability
#       vs ~50% for qwen3_coder per allanchan339 + DGX Spark forum testing.
#       ``hermes`` is wrong here — it's the base Qwen2.5/Qwen3 parser.
#
#  --reasoning-parser qwen3 (REMOVED 2026-05-15)
#       Originally routed <think>...</think> into message.reasoning_content
#       so the FE doesn't render raw tags. In practice this caused silent
#       chunks=0 failures on /v1/responses tool-call followups: when the
#       model spent its entire output inside <think>...</think> (long
#       thinking, no answer), every delta had content=null and the worker
#       discarded it. Without the parser, <think> tags come through inline
#       as content; the FE handles hiding them. Worker_client.py also
#       carries a reasoning_content fallback as belt-and-suspenders.
#
#  --chat-template /opt/chat-template/qwen3.5-enhanced.jinja
#       Stock qwen3.5_official.jinja fires <|im_end|> prematurely when the
#       model transitions from </think> to <tool_call> — observed in the
#       wild as ``chunks=2, parse_failures=0, tool_calls=0`` (the model
#       stopped before emitting anything to parse). The enhanced template
#       (allanchan339/vLLM-Qwen3-3.5-3.6-chat-template-fix) closes <think>
#       cleanly and gates tool_call emission on a non-EOS state.
#
#  --enable-auto-tool-choice
#       Gate to accept tool_choice:"auto" from clients (without this, every
#       tool_choice:"auto" request 400s).
: ${TOOL_CALL_PARSER:=qwen3_xml}
: ${CHAT_TEMPLATE_PATH:=/opt/chat-template/qwen3.5-enhanced.jinja}

VLLM_CMD="vllm serve $MODEL_NAME \
  --served-model-name $MODEL_NAME \
  --trust-remote-code \
  --tensor-parallel-size 1 \
  --max-num-seqs 32 \
  --host 0.0.0.0 \
  --port 8000 \
  --api-key ${API_KEY} \
  --download-dir /models/hub \
  --load-format safetensors \
  --max-model-len $MAX_MODEL_LEN \
  --enable-auto-tool-choice \
  --tool-call-parser ${TOOL_CALL_PARSER} \
  --chat-template ${CHAT_TEMPLATE_PATH} \
  --enable-prompt-tokens-details"

# Add optional model commit if defined
if [ -n "$MODEL_COMMIT" ]; then
  VLLM_CMD="$VLLM_CMD --revision $MODEL_COMMIT"
  echo "[vLLM] Using model commit: $MODEL_COMMIT"
fi

# Only add GPU memory utilization if not using CPU
if [ "$DEVICE" != "cpu" ]; then
  VLLM_CMD="$VLLM_CMD --gpu-memory-utilization $GPU_MEM_UTIL"
fi

echo "[vLLM] Executing: $VLLM_CMD"
exec $VLLM_CMD
