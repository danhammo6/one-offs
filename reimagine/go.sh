#!/usr/bin/env bash
set -euo pipefail

# Override this when the ComfyUI output directory is mounted elsewhere.
COMFYUI_OUTPUT_DIR="${COMFYUI_OUTPUT_DIR:-$HOME/Desktop/MyShare}"

# Check if the ComfyUI output directory is mounted
check_comfyui_output_dir() {
  echo "Checking ComfyUI output directory..."

  if mount | grep -q "$COMFYUI_OUTPUT_DIR"; then
    echo "✓ ComfyUI output directory mounted at $COMFYUI_OUTPUT_DIR"
    return 0
  else
    echo "✗ ComfyUI output directory NOT mounted"
    echo ""
    echo "Mount or otherwise expose ComfyUI's output directory at:"
    echo "  $COMFYUI_OUTPUT_DIR"
    echo "Then pass it to the pipeline with --comfyui-output-dir."
    echo ""
    return 1
  fi
}

# Main setup checks
main() {
  check_comfyui_output_dir

  # Add more setup checks below as needed
  # check_python_venv
  # check_dependencies
  # etc.

  echo ""
  echo "All checks passed! Ready to go."
}

main "$@"
