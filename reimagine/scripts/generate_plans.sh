#!/usr/bin/env bash
# Generate all still and video prompt plans while only the LLM server is loaded.
set -euo pipefail
cd "$(dirname "$0")/.."

PY=.venv/bin/python
LLM="${LLM:-127.0.0.1:9503}"

"$PY" generate_prompts.py --stage all --still-mode manual \
  --llm-server "$LLM" --output-dir outputs/local-llm

"$PY" generate_prompts.py --stage all --still-mode regions \
  --llm-server "$LLM" --output-dir outputs/local-llm-regions
