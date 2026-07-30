#!/usr/bin/env bash
# One-shot: regenerate all four output sets as v2 siblings (images only, no
# video). Originals are left untouched. Each run uses a distinct host
# --save-subdir so their ComfyUI-side staging never collides. Sequential
# because ComfyUI renders serially regardless.
set -uo pipefail
cd "$(dirname "$0")/.."

PY=.venv/bin/python
COMFY="${COMFY:-127.0.0.1:8188}"
COMFYUI_OUTPUT_DIR="${COMFYUI_OUTPUT_DIR:-$HOME/Desktop/MyShare}"
COMMON=(--comfyui-output-dir "$COMFYUI_OUTPUT_DIR" --comfy-server "$COMFY")

run() {
  local label="$1"; shift
  echo "======================================================================"
  echo ">>> $label  @ $(date '+%H:%M:%S')"
  echo "======================================================================"
  "$PY" reimagine.py "$@"
  echo "<<< $label done @ $(date '+%H:%M:%S')  (exit $?)"
  echo
}

# Claude (opus) — manual, then regions.
run "claude-v2" \
  --output-dir outputs/claude-v2 --save-subdir reimagine-v2-claude "${COMMON[@]}"

run "claude-regions-v2" --regions \
  --output-dir outputs/claude-regions-v2 --save-subdir reimagine-v2-claude-regions "${COMMON[@]}"

# Local gemma — manual, then regions (auto-pinned to 1 worker).
run "local-llm-v2" --llm-server 127.0.0.1:9503 \
  --output-dir outputs/local-llm-v2 --save-subdir reimagine-v2-local-llm "${COMMON[@]}"

run "local-llm-regions-v2" --regions --llm-server 127.0.0.1:9503 \
  --output-dir outputs/local-llm-regions-v2 --save-subdir reimagine-v2-local-llm-regions "${COMMON[@]}"

echo "ALL v2 SETS COMPLETE @ $(date '+%H:%M:%S')"
