#!/bin/bash
# Installs the build queue on macOS or Linux: a venv with psutil for the server, gq on PATH, and the user
# service that keeps the server running (launchd on macOS, systemd --user on Linux). Windows: install.ps1.
# The Claude Code hook is added to ~/.claude/settings.json separately (see README).
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
label=com.massa.build-queue
data="$HOME/.local/share/build-queue"
venv="$data/venv"
python="${PYTHON:-$(command -v python3)}"

chmod +x "$here/bq/gq.py" "$here/bq/hook.py"
mkdir -p "$HOME/.local/bin" "$data"
ln -sf "$here/bq/gq.py" "$HOME/.local/bin/gq"
"$python" -m venv --clear "$venv"
"$venv/bin/python" -m pip install -q --disable-pip-version-check "psutil>=6"

case "$(uname -s)" in
Darwin)
  plist="$HOME/Library/LaunchAgents/$label.plist"
  logfile="$HOME/Library/Logs/build-queue.log"
  cat > "$plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$label</string>
  <key>ProgramArguments</key>
  <array><string>$venv/bin/python</string><string>$here/bq/server.py</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$logfile</string>
  <key>StandardErrorPath</key><string>$logfile</string>
</dict>
</plist>
EOF
  launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$plist"
  echo "installed: http://127.0.0.1:8787 (log: $logfile)"
  ;;
Linux)
  unit="$HOME/.config/systemd/user/$label.service"
  mkdir -p "$(dirname "$unit")"
  cat > "$unit" <<EOF
[Unit]
Description=build-queue: machine-wide FIFO slots for the Gradle/JVM runs of Claude agents

[Service]
ExecStart="$venv/bin/python" "$here/bq/server.py"
Restart=always
RestartSec=2

[Install]
WantedBy=default.target
EOF
  systemctl --user daemon-reload
  systemctl --user enable "$label.service"
  systemctl --user restart "$label.service"
  echo "installed: http://127.0.0.1:8787 (log: journalctl --user -u $label)"
  ;;
*)
  echo "unsupported system $(uname -s); on Windows run install.ps1" >&2
  exit 1
  ;;
esac
