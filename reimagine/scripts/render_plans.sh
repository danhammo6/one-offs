#!/usr/bin/env bash
# Render all saved stills, then videos, while only ComfyUI is loaded.
set -euo pipefail
cd "$(dirname "$0")/.."

PY=.venv/bin/python
COMFY="${COMFY:-192.168.33.101:8188}"
COMFYUI_OUTPUT_DIR="${COMFYUI_OUTPUT_DIR:-$HOME/Desktop/MyShare}"
MODEL_TAG="${MODEL_TAG:-gemma4}"

for MODE in manual regions; do
  NAME="${MODEL_TAG}-${MODE}"
  SET="outputs/$NAME"
  "$PY" render_media.py --stage all --output-dir "$SET" \
    --pipeline-suffix "${MODEL_TAG}_${MODE}" \
    --comfy-server "$COMFY" --comfyui-output-dir "$COMFYUI_OUTPUT_DIR" \
    --still-save-subdir "reimagine-$NAME" \
    --video-save-subdir "reimagine-video-$NAME"
done
