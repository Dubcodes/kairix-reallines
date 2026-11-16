#!/usr/bin/env bash
set -euo pipefail
if [[ $EUID -ne 0 ]]; then echo "Run with sudo from inside the repository." >&2; exit 1; fi
DEV_USER="${SUDO_USER:-}"
if [[ -z "$DEV_USER" || "$DEV_USER" == "root" ]]; then echo "Run with sudo from the normal RealLines account." >&2; exit 1; fi
REPO="$(cd "$(dirname "$0")/.." && pwd)"
apt-get update
apt-get install -y cage chromium wlr-randr libdrm-tests
usermod -a -G video,render,input "$DEV_USER"
install -d -o root -g root -m 0755 /usr/local/lib/kairix-reallines
install -o root -g root -m 0755 installer/kairix-hdmi-launcher /usr/local/lib/kairix-reallines/kairix-hdmi-launcher
install -o root -g root -m 0755 installer/kairix-hdmi-helper /usr/local/sbin/kairix-hdmi-helper
install -o root -g root -m 0440 installer/kairix-hdmi-sudoers /etc/sudoers.d/kairix-reallines-hdmi
visudo -cf /etc/sudoers.d/kairix-reallines-hdmi
sed -e "s|User=reallines|User=${DEV_USER}|" -e "s|Group=reallines|Group=${DEV_USER}|" -e "s|/home/reallines/kairix-reallines|${REPO}|g" installer/kairix-reallines-hdmi.service > /etc/systemd/system/kairix-reallines-hdmi.service
chown root:root /etc/systemd/system/kairix-reallines-hdmi.service
chmod 0644 /etc/systemd/system/kairix-reallines-hdmi.service
systemctl daemon-reload
systemctl disable kairix-reallines-hdmi.service >/dev/null 2>&1 || true
echo "HDMI components installed but NOT started or enabled. Review modes with: modetest -M vc4 -c"
