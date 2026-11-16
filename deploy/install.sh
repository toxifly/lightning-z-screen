#!/usr/bin/env bash
# Install the Lightning Z screen monitor as a system service.
# Run from anywhere:  sudo bash deploy/install.sh
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "Run with sudo."; exit 1; }
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DST=/opt/lightning-display

install -D -m 644 "$SRC/deploy/lightning-display.sysusers" /etc/sysusers.d/lightning-display.conf
systemd-sysusers /etc/sysusers.d/lightning-display.conf

install -d -m 755 "$DST"
install -m 644 "$SRC"/aicast.py "$SRC"/control.py "$SRC"/dashboard.py "$SRC"/hidctl.py "$SRC"/modes.py \
  "$SRC"/monitor.py "$SRC"/claude_board.py "$DST"/
install -d -m 755 "$DST/fonts"
install -m 644 "$SRC"/fonts/*.ttf "$SRC"/fonts/OFL-*.txt "$DST/fonts"/
install -m 755 "$SRC/lightning-screen" "$SRC/lightning-screen-gui" /usr/local/bin/
install -D -m 644 "$SRC/deploy/lightning-screen.desktop" /usr/local/share/applications/io.github.toxifly.LightningScreen.desktop
# Settings: who may switch modes (defaults to the user running sudo) and WireView names.
[ -f /etc/default/lightning-display ] || cat > /etc/default/lightning-display <<EOF
CONTROL_USERS="${SUDO_USER:-}"
# WireView Pro II units, NAME=DEVICE, shown in this order (default: a and b)
#WIREVIEWS="top=/dev/wireview-pro2-a bottom=/dev/wireview-pro2-b"
EOF
[ -f "$SRC/wvread.py" ] && install -m 644 "$SRC/wvread.py" "$DST"/
if [ ! -x "$DST/.venv/bin/python" ]; then
  python3 -m venv "$DST/.venv" 2>/dev/null || { UV="$(command -v uv || echo "${SUDO_USER:+/home/$SUDO_USER/.local/bin/uv}")"; "$UV" venv -q --python /usr/bin/python3 "$DST/.venv"; }
fi
if [ -x "$DST/.venv/bin/pip" ]; then
  "$DST/.venv/bin/pip" install -q -r "$SRC/requirements.txt"
else
  UV="$(command -v uv || echo "/home/${SUDO_USER:-root}/.local/bin/uv")"
  UV_CACHE_DIR=/var/cache/lightning-display-uv "$UV" pip install -q --python "$DST/.venv/bin/python" -r "$SRC/requirements.txt"
fi

# Service-only access to the screen (see the rule file for why there is no uaccess).
install -m 644 "$SRC/deploy/70-msi-lightning-display.rules" /etc/udev/rules.d/70-msi-lightning-display.rules
udevadm control --reload
udevadm trigger --action=add --attr-match=idVendor=0db0 --attr-match=idProduct=7881 --subsystem-match=usb
udevadm trigger --action=add --subsystem-match=hidraw
udevadm settle
for d in /sys/bus/usb/devices/*; do
  [ "$(cat "$d/idVendor" 2>/dev/null)" = 0db0 ] && [ "$(cat "$d/idProduct" 2>/dev/null)" = 7881 ] || continue
  printf -v node '/dev/bus/usb/%03d/%03d' "$(cat "$d/busnum")" "$(cat "$d/devnum")"
  for n in "$node" "$(readlink -f /dev/msi-lightning-display)"; do
    setfacl -b "$n" 2>/dev/null || true   # drop any per-user ACL left by an older rule
    ls -l "$n"
  done
done

# Desktop monitor mode: one EVDI virtual display, if evdi-dkms is installed.
if modinfo evdi >/dev/null 2>&1; then
  echo evdi > /etc/modules-load.d/lightning-display-evdi.conf
  echo "options evdi initial_device_count=1" > /etc/modprobe.d/lightning-display-evdi.conf
  lsmod | grep -q '^evdi ' || modprobe evdi || echo "evdi did not load (Secure Boot key not enrolled?)"
fi

install -m 644 "$SRC/deploy/lightning-display.service" /etc/systemd/system/lightning-display.service
systemctl daemon-reload
systemctl enable lightning-display.service
systemctl restart lightning-display.service
sleep 4
systemctl --no-pager --lines=8 status lightning-display.service
