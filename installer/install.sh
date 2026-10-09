#!/usr/bin/env bash
set -euo pipefail
if [[ $EUID -ne 0 ]]; then echo "Run with sudo: sudo ./installer/install.sh"; exit 1; fi
SRC="$(cd "$(dirname "$0")/.." && pwd)"
VERSION="$(cat "$SRC/VERSION")"
BASE=/opt/kairix-reallines
RELEASE="$BASE/releases/$VERSION"
DATA=/var/lib/kairix-reallines

echo "Installing Kairix RealLines $VERSION"
apt-get update
apt-get install -y python3-venv python3-pip
id -u kairix >/dev/null 2>&1 || useradd --system --home "$DATA" --shell /usr/sbin/nologin kairix
mkdir -p "$BASE/releases" "$DATA"
rm -rf "$RELEASE"
mkdir -p "$RELEASE"
cp -a "$SRC"/. "$RELEASE"/
rm -rf "$RELEASE/.git" "$RELEASE/data"
python3 -m venv "$BASE/venv"
"$BASE/venv/bin/pip" install --upgrade pip
"$BASE/venv/bin/pip" install -r "$RELEASE/requirements.txt"
ln -sfn "$RELEASE" "$BASE/current"
chown -R kairix:kairix "$DATA"
install -m 0644 "$SRC/installer/kairix-reallines.service" /etc/systemd/system/kairix-reallines.service
systemctl daemon-reload
systemctl enable --now kairix-reallines.service
sleep 1
systemctl --no-pager --full status kairix-reallines.service || true
IP=$(hostname -I | awk '{print $1}')
echo
echo "Installed. Open: http://${IP:-<pi-ip>}:8080/engineering"
