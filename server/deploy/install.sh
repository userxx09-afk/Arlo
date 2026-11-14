#!/usr/bin/env bash
# Install or update Camera Keeper as a systemd service (Ubuntu).
# Run from the unpacked project folder:  sudo bash deploy/install.sh
set -euo pipefail

SRC="$(cd "$(dirname "$0")/.." && pwd)"
APP=/opt/camera-keeper
DATA=/var/lib/camera-keeper
ETC=/etc/camera-keeper
PORT="${PORT:-8090}"

apt-get install -y -qq python3-venv ffmpeg git >/dev/null

# Before the rename to Camera Keeper / BCN it was "arlo-keeper": move that install over once.
# The clips, settings and Arlo/Blink sign-ins come along; the old code and venv are removed.
if [ -f /etc/systemd/system/arlo-keeper.service ]; then
  echo "Moving Arlo Keeper to its new name, Camera Keeper..."
  systemctl stop arlo-keeper || true
  systemctl disable arlo-keeper >/dev/null 2>&1 || true
  rm -f /etc/systemd/system/arlo-keeper.service
  systemctl daemon-reload
fi
if id arlo-keeper >/dev/null 2>&1 && ! id camera-keeper >/dev/null 2>&1; then
  usermod -l camera-keeper -d "$DATA" arlo-keeper          # same user id, so file owners stay right
  getent group arlo-keeper >/dev/null && groupmod -n camera-keeper arlo-keeper
fi
if [ -d /var/lib/arlo-keeper ] && [ ! -L /var/lib/arlo-keeper ] && [ ! -e "$DATA" ]; then
  mv /var/lib/arlo-keeper "$DATA"
fi
if [ -d /etc/arlo-keeper ] && [ ! -e "$ETC" ]; then
  mv /etc/arlo-keeper "$ETC"
  [ -f "$ETC/arlo-keeper.env" ] && mv "$ETC/arlo-keeper.env" "$ETC/camera-keeper.env"
  sed -i "s#/var/lib/arlo-keeper#$DATA#g" "$ETC/camera-keeper.env"
fi
rm -rf /opt/arlo-keeper

id camera-keeper >/dev/null 2>&1 || useradd --system --home "$DATA" --shell /usr/sbin/nologin camera-keeper
install -d -o camera-keeper -g camera-keeper -m 750 "$DATA"
install -d -m 755 "$APP" "$ETC"

# Code (data and settings are never touched by updates)
rm -rf "$APP/app"
cp -r "$SRC/app" "$SRC/requirements.txt" "$APP/"
find "$APP/app" -name __pycache__ -prune -exec rm -rf {} +

[ -d "$APP/venv" ] || python3 -m venv "$APP/venv"
"$APP/venv/bin/pip" install -q --upgrade pip
"$APP/venv/bin/pip" install -q -r "$APP/requirements.txt"

# Settings file: created once, then left alone
if [ ! -f "$ETC/camera-keeper.env" ]; then
  cat > "$ETC/camera-keeper.env" <<EOF
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
chown root:camera-keeper "$ETC/camera-keeper.env"
chmod 640 "$ETC/camera-keeper.env"

cp "$SRC/deploy/camera-keeper.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now camera-keeper
systemctl restart camera-keeper

if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then
  ufw allow "$PORT/tcp" >/dev/null
fi

sleep 3
systemctl --no-pager --lines=5 status camera-keeper || true
echo
echo "Camera Keeper is running: http://$(hostname -I | awk '{print $1}'):$PORT"
