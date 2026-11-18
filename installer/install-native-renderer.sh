#!/usr/bin/env bash
set -euo pipefail

DEV_USER="${SUDO_USER:-reallines}"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
BINARY="$REPO/native/build/kairix-native-renderer"
[[ $EUID -eq 0 ]] || { echo "Run with sudo during an authorized maintenance window" >&2; exit 1; }
[[ -x "$BINARY" ]] || { echo "Build native/build/kairix-native-renderer first; this installer never installs packages" >&2; exit 1; }

install -o root -g root -m 0755 "$BINARY" /usr/local/bin/kairix-native-renderer
install -o root -g root -m 0755 "$REPO/installer/kairix-native-helper" /usr/local/sbin/kairix-native-helper
install -o root -g root -m 0440 "$REPO/installer/kairix-native-sudoers" /etc/sudoers.d/kairix-reallines-native
visudo -cf /etc/sudoers.d/kairix-reallines-native
sed -e "s|User=reallines|User=${DEV_USER}|" -e "s|Group=reallines|Group=${DEV_USER}|" \
  "$REPO/installer/kairix-reallines-native.service" > /etc/systemd/system/kairix-reallines-native.service
chown root:root /etc/systemd/system/kairix-reallines-native.service
chmod 0644 /etc/systemd/system/kairix-reallines-native.service
if [[ ! -e /etc/default/kairix-reallines-native ]]; then
  printf '%s\n' 'KAIRIX_NATIVE_OPTIONS=--mode internal --motion sine --diagnostics /run/kairix-native/renderer-diagnostics.json' \
    > /etc/default/kairix-reallines-native
  chmod 0644 /etc/default/kairix-reallines-native
fi
systemctl daemon-reload
systemctl disable kairix-reallines-native.service >/dev/null 2>&1 || true
systemctl stop kairix-reallines-native.service >/dev/null 2>&1 || true
echo "Native renderer installed disabled/stopped. Browser HDMI was not stopped."
