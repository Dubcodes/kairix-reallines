#!/usr/bin/env bash
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "Run from inside the repo with: sudo ./installer/install-dev.sh"
  exit 1
fi

DEV_USER="${SUDO_USER:-}"
if [[ -z "$DEV_USER" || "$DEV_USER" == "root" ]]; then
  echo "Run this with sudo from your normal Pi user, not from a root shell."
  exit 1
fi

REPO="$(cd "$(dirname "$0")/.." && pwd)"
VENV="$REPO/.venv"
SERVICE=/etc/systemd/system/kairix-reallines-dev.service

echo "Configuring Kairix RealLines development service"
echo "User: $DEV_USER"
echo "Repo: $REPO"

apt-get update
apt-get install -y python3-venv python3-pip samba

sudo -u "$DEV_USER" python3 -m venv "$VENV"
sudo -u "$DEV_USER" "$VENV/bin/pip" install --upgrade pip
sudo -u "$DEV_USER" "$VENV/bin/pip" install -r "$REPO/requirements.txt"

cat > "$SERVICE" <<SERVICE_EOF
[Unit]
Description=Kairix RealLines development server
After=network.target

[Service]
Type=simple
User=$DEV_USER
Group=$DEV_USER
WorkingDirectory=$REPO
Environment=KAIRIX_DATA_DIR=$REPO/data
ExecStart=$VENV/bin/uvicorn app.main:app --host 0.0.0.0 --port 8080 --reload
Restart=always
RestartSec=2

[Install]
WantedBy=multi-user.target
SERVICE_EOF

# Add a focused writable project share. This is the recommended day-to-day development share.
if ! grep -q '^\[reallines-dev\]' /etc/samba/smb.conf; then
cat >> /etc/samba/smb.conf <<SMB_EOF

[reallines-dev]
   comment = Kairix RealLines development repo
   path = $REPO
   browseable = yes
   read only = no
   guest ok = no
   valid users = $DEV_USER
   create mask = 0664
   directory mask = 0775
SMB_EOF
fi

systemctl daemon-reload
systemctl enable --now kairix-reallines-dev.service
systemctl restart smbd

IP=$(hostname -I | awk '{print $1}')
echo
echo "Development service is running."
echo "Engineering: http://${IP:-<pi-ip>}:8080/engineering"
echo "SMB project share: \\\\${IP:-<pi-ip>}\\reallines-dev"
echo
echo "Set/refresh the Samba password for $DEV_USER with:"
echo "  sudo smbpasswd -a $DEV_USER"
echo
echo "Service commands:"
echo "  sudo systemctl status kairix-reallines-dev"
echo "  sudo journalctl -u kairix-reallines-dev -f"
