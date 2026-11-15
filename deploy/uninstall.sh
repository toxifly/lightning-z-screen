#!/usr/bin/env bash
# Remove the service, udev rule and installed files (the service user is left in place).
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "Run with sudo."; exit 1; }
systemctl disable --now lightning-display.service 2>/dev/null || true
rm -f /etc/systemd/system/lightning-display.service /etc/udev/rules.d/70-msi-lightning-display.rules
systemctl daemon-reload
udevadm control --reload
rm -rf /opt/lightning-display /var/cache/lightning-display-uv
echo "Removed. To also delete the service user: userdel lightning-display; rm /etc/sysusers.d/lightning-display.conf"
