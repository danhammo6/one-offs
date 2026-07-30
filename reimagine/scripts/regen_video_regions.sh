#!/usr/bin/env bash
# Video pass over the two REGION v2 still sets, sequentially:
#   1. claude-regions-v2      (video prompts by Claude opus — set's origin LLM)
#   2. local-llm-regions-v2   (video prompts by local gemma — set's origin LLM)
# Non-region sets are intentionally omitted for now.
#
# Each run points LoadImage at that set's own host staging subdir (where the
# v2 stills were flattened during the image pass) and stages rendered videos
# under its own --save-subdir so the two runs never collide.
set -uo pipefail
cd "$(dirname "$0")/.."

PY=.venv/bin/python
COMFY="${COMFY:-127.0.0.1:8188}"
COMFYUI_OUTPUT_DIR="${COMFYUI_OUTPUT_DIR:-$HOME/Desktop/MyShare}"

run() {
  local label="$1"; shift
  echo "======================================================================"
  echo ">>> $label  @ $(date '+%H:%M:%S')"
  echo "======================================================================"
  "$PY" animate.py "$@" --comfyui-output-dir "$COMFYUI_OUTPUT_DIR" \
    --comfy-server "$COMFY"
  echo "<<< $label done @ $(date '+%H:%M:%S')  (exit $?)"
  echo
}

# 1) claude-regions-v2 — Claude opus writes the motion prompts (default LLM).
run "video: claude-regions-v2" \
  --set outputs/claude-regions-v2 \
  --load-name-template '../output/reimagine-v2-claude-regions/{base}.jpeg' \
  --save-subdir reimagine-video-claude-regions-v2

# 2) local-llm-regions-v2 — local gemma writes the motion prompts (1 worker).
run "video: local-llm-regions-v2" \
  --set outputs/local-llm-regions-v2 \
  --llm-server 127.0.0.1:9503 \
  --load-name-template '../output/reimagine-v2-local-llm-regions/{base}.jpeg' \
  --save-subdir reimagine-video-local-llm-regions-v2

echo "BOTH REGION VIDEO SETS COMPLETE @ $(date '+%H:%M:%S')"
