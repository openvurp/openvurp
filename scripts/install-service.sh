#!/usr/bin/env bash
# Install openvurp as an always-on systemd service (headless).
# It survives closing the TUI/CLI, crashes (Restart=always), and WSL/system boot.
#
#   Run once, with sudo:   sudo bash scripts/install-service.sh
#   Logs:                  journalctl -u openvurp -f
#   Stop:                  sudo systemctl stop openvurp
#   Don't start on boot:   sudo systemctl disable openvurp
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "Run me with sudo:  sudo bash scripts/install-service.sh" >&2
  exit 1
fi

REPO="$(cd "$(dirname "$0")/.." && pwd)"
RUNUSER="${SUDO_USER:-root}"
PY="$(command -v python3)"
UNIT=/etc/systemd/system/openvurp.service

echo "Installing openvurp service:"
echo "  repo:   $REPO"
echo "  user:   $RUNUSER"
echo "  python: $PY"

cat > "$UNIT" <<EOF
[Unit]
Description=openvurp — personal AI agent (headless)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$RUNUSER
WorkingDirectory=$REPO
ExecStart=$PY $REPO/main.py --headless
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable openvurp.service
systemctl restart openvurp.service

echo
echo "Done. Status:"
systemctl --no-pager status openvurp.service | head -n 12 || true
echo
echo "Follow logs:  journalctl -u openvurp -f"
