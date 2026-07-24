#!/usr/bin/env bash
set -euo pipefail

# Configuration
MOUNT_POINT="$HOME/Desktop/MyShare"
MOUNT_CMD="mount_smbfs '//story@192.168.33.101/e$/lib/ComfyUI_windows_portable/ComfyUI/output' ~/Desktop/MyShare"

# Check if the ComfyUI output samba share is mounted
check_samba() {
  echo "Checking samba share..."

  if mount | grep -q "$MOUNT_POINT"; then
    echo "✓ Samba share mounted at $MOUNT_POINT"
    return 0
  else
    echo "✗ Samba share NOT mounted"
    echo ""
    echo "To mount the ComfyUI output directory, run:"
    echo "  $MOUNT_CMD"
    echo ""
    return 1
  fi
}

# Main setup checks
main() {
  check_samba

  # Add more setup checks below as needed
  # check_python_venv
  # check_dependencies
  # etc.

  echo ""
  echo "All checks passed! Ready to go."
}

main "$@"
