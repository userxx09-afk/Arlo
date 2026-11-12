#!/usr/bin/env bash
# Move Arlo Keeper on Ubuntu (the installed copy in /opt/arlo-keeper). Run as root:
#   sudo bash deploy/move.sh export /tmp/ban-export.zip [--no-clips]   (also turns recording off here)
#   sudo bash deploy/move.sh import /tmp/ban-export.zip
# The web page does the same at http://<server>:8090/move.
set -euo pipefail
APP=/opt/arlo-keeper
ENVF=/etc/arlo-keeper/arlo-keeper.env
cmd="${1:-}"; file="${2:-}"; shift 2 || true
[ -n "$cmd" ] && [ -n "$file" ] || { echo "usage: $0 export|import FILE [--no-clips]"; exit 1; }

run() {   # the app's python, with the service's settings, as the service's user
  sudo -u arlo-keeper env $(grep -v '^\s*#' "$ENVF" | grep '=' | xargs) \
    "$APP/venv/bin/python" -c "import sys; sys.path.insert(0, '$APP'); from app import migrate; migrate.main(sys.argv[1:])" "$@"
}

case "$cmd" in
  export)
    echo "Stopping Arlo Keeper for a clean copy..."
    systemctl stop arlo-keeper
    touch "$file"; chown arlo-keeper "$file"
    run export "$file" "$@"
    chown "${SUDO_USER:-root}" "$file"
    # Recording stays off here so the new server can sign in to Arlo (one server per login).
    run_py="import sys; sys.path.insert(0, '$APP'); from app import store; import time; store.init(); store.set_setting('moved_away', {'at': time.strftime('%Y-%m-%d %H:%M'), 'to': ''})"
    sudo -u arlo-keeper env $(grep -v '^\s*#' "$ENVF" | grep '=' | xargs) "$APP/venv/bin/python" -c "$run_py"
    systemctl start arlo-keeper
    echo "Recording is off on this server (clips still viewable). Turn it back on from the Move page if needed."
    ;;
  import)
    systemctl stop arlo-keeper
    chmod o+r "$file" || true
    run import "$file"
    # Portable settings from the export go into $ENVF (root-owned, so done here, not by the service).
    "$APP/venv/bin/python" - "$file" "$ENVF" <<'PY'
import json, sys, zipfile
sys.path.insert(0, "/opt/arlo-keeper")
from app import migrate
m = json.loads(zipfile.ZipFile(sys.argv[1]).read(migrate.MANIFEST))
print("settings brought over:", migrate.merge_settings_file(sys.argv[2], m.get("settings") or {}, m.get("made_by")))
PY
    systemctl start arlo-keeper
    echo "Imported. Delete $file (it holds passwords)."
    ;;
  *) echo "usage: $0 export|import FILE [--no-clips]"; exit 1 ;;
esac
