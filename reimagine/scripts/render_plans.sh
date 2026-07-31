#!/usr/bin/env bash
# Render all saved stills, then videos, while only ComfyUI is loaded.
set -euo pipefail
cd "$(dirname "$0")/.."

PY=.venv/bin/python
COMFY="${COMFY:-127.0.0.1:8188}"
COMFYUI_OUTPUT_DIR="${COMFYUI_OUTPUT_DIR:-$HOME/Desktop/MyShare}"

for SET in outputs/local-llm outputs/local-llm-regions; do
  NAME="$(basename "$SET")"
  "$PY" render_media.py --stage all --output-dir "$SET" \
    --comfy-server "$COMFY" --comfyui-output-dir "$COMFYUI_OUTPUT_DIR" \
    --still-save-subdir "reimagine-$NAME" \
    --video-save-subdir "reimagine-video-$NAME"
done
