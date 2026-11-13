#!/bin/bash
# Installs the build queue: gq on PATH and the launchd agent that keeps the server running.
# The Claude Code hook is added to ~/.claude/settings.json separately (see README).
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
label=com.luizmassa.build-queue
plist="$HOME/Library/LaunchAgents/$label.plist"
python=/opt/homebrew/bin/python3

chmod +x "$here/bq/gq.py" "$here/bq/hook.py"
mkdir -p "$HOME/.local/bin" "$HOME/.local/share/build-queue"
ln -sf "$here/bq/gq.py" "$HOME/.local/bin/gq"

cat > "$plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$label</string>
  <key>ProgramArguments</key>
  <array><string>$python</string><string>$here/bq/server.py</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$HOME/Library/Logs/build-queue.log</string>
  <key>StandardErrorPath</key><string>$HOME/Library/Logs/build-queue.log</string>
</dict>
</plist>
EOF

launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$plist"
echo "installed: http://127.0.0.1:8787 (log: ~/Library/Logs/build-queue.log)"
