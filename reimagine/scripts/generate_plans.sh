#!/usr/bin/env bash
# Generate all still and video prompt plans while only the LLM server is loaded.
set -euo pipefail
cd "$(dirname "$0")/.."

PY=.venv/bin/python
LLM="${LLM:-127.0.0.1:9503}"
MODEL_TAG="${MODEL_TAG:-gemma4}"

"$PY" generate_prompts.py --stage all --still-mode manual \
  --llm-server "$LLM" --pipeline-suffix "${MODEL_TAG}_manual"

"$PY" generate_prompts.py --stage all --still-mode regions \
  --llm-server "$LLM" --pipeline-suffix "${MODEL_TAG}_regions"
