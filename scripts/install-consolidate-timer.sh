#!/usr/bin/env bash
# Install systemd user timer for weekly consolidation
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
mkdir -p "$HOME/.config/systemd/user"
cp "$SCRIPT_DIR/total-recall-consolidate.service" "$HOME/.config/systemd/user/"
cp "$SCRIPT_DIR/total-recall-consolidate.timer" "$HOME/.config/systemd/user/"
systemctl --user daemon-reload
systemctl --user enable total-recall-consolidate.timer
systemctl --user start total-recall-consolidate.timer
echo "✓ Installed. Run 'systemctl --user list-timers' to verify."
