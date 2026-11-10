#!/bin/sh
# Double-click to install mtnews-dl on macOS (runs install.sh next to this file).
# If macOS says it cannot be opened: right-click > Open, or System Settings > Privacy & Security > Open Anyway.
cd "$(dirname "$0")" || exit 1
sh ./install.sh
printf '\nPress Return to close this window. '
read -r _
