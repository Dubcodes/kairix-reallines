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
install -d -o "$DEV_USER" -g "$DEV_USER" -m 0750 "$REPO/data"
if [[ ! -e "$REPO/data/hdmi_output.json" ]]; then
  CONFIG_TMP="$(mktemp)"
  trap 'rm -f "$CONFIG_TMP"' EXIT
  printf '%s\n' '{' '  "connector": "HDMI-A-2",' '  "mode": "1920x1080@50.00"' '}' > "$CONFIG_TMP"
  install -o "$DEV_USER" -g "$DEV_USER" -m 0640 "$CONFIG_TMP" "$REPO/data/hdmi_output.json"
fi
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
echo "Verify HDMI-A-2 offers 1920x1080 at exactly 50.00 Hz before starting the service."
