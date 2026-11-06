#!/usr/bin/env bash
# Install or update Arlo Keeper as a systemd service (Ubuntu).
# Run from the unpacked project folder:  sudo bash deploy/install.sh
set -euo pipefail

SRC="$(cd "$(dirname "$0")/.." && pwd)"
APP=/opt/arlo-keeper
DATA=/var/lib/arlo-keeper
ETC=/etc/arlo-keeper
PORT="${PORT:-8090}"

apt-get install -y -qq python3-venv ffmpeg git >/dev/null

id arlo-keeper >/dev/null 2>&1 || useradd --system --home "$DATA" --shell /usr/sbin/nologin arlo-keeper
install -d -o arlo-keeper -g arlo-keeper -m 750 "$DATA"
install -d -m 755 "$APP" "$ETC"

# Code (data and settings are never touched by updates)
rm -rf "$APP/app"
cp -r "$SRC/app" "$SRC/requirements.txt" "$APP/"
find "$APP/app" -name __pycache__ -prune -exec rm -rf {} +

[ -d "$APP/venv" ] || python3 -m venv "$APP/venv"
"$APP/venv/bin/pip" install -q --upgrade pip
"$APP/venv/bin/pip" install -q -r "$APP/requirements.txt"

# Settings file: created once, then left alone
if [ ! -f "$ETC/arlo-keeper.env" ]; then
  cat > "$ETC/arlo-keeper.env" <<EOF
# Arlo sign-in, phone numbers and alert accounts are set on the web page.
DATA_DIR=$DATA
WEB_PORT=$PORT
TZ=America/Chicago
CLIP_MAX_SECONDS=20
KEEP_DAYS=90
MAX_STORAGE_GB=4
LIBRARY_DAYS=7
POLL_MINUTES=5
USE_BASE_STATION_STORAGE=true
PUBLIC_URL=http://$(hostname -I | awk '{print $1}'):$PORT
EOF
fi
chown root:arlo-keeper "$ETC/arlo-keeper.env"
chmod 640 "$ETC/arlo-keeper.env"

cp "$SRC/deploy/arlo-keeper.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now arlo-keeper
systemctl restart arlo-keeper

if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then
  ufw allow "$PORT/tcp" >/dev/null
fi

sleep 3
systemctl --no-pager --lines=5 status arlo-keeper || true
echo
echo "Arlo Keeper is running: http://$(hostname -I | awk '{print $1}'):$PORT"
