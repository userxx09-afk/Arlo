"""Move Arlo Keeper to another server: export everything from the old one, import it on the new one.

An export is one .zip file:
    ban-export.json   what's in it (made by, when, clip count) and the portable settings
    clips.db          clip list + everything set on the web page: Arlo accounts and passwords,
                      phone numbers, Twilio/ntfy/Pushover, away-from-home password, GarysTV
    state/...         saved Arlo sessions (so the new server usually needs no new 2FA code)
    photo.jpg         Barb's photo, ban.apk the phone app the Alerts page offers
    clips/...         the saved clips and their pictures (optional)

Importing replaces the database, Arlo sessions and photo, and adds clips the new server doesn't
have yet. It has to happen while Arlo Keeper isn't running, so the web page saves the upload as
import-pending.zip and restarts; main.py applies it before anything else starts.

From a shell (the service stopped first):
    python -m app.migrate export ban-export.zip [--no-clips]
    python -m app.migrate import ban-export.zip [--settings-file PATH]
"""
import ipaddress
import json
import logging
import os
import shutil
import socket
import sqlite3
import sys
import tempfile
import time
import zipfile
from urllib.parse import urlparse

from . import config

log = logging.getLogger("migrate")

FORMAT = 1
MANIFEST = "ban-export.json"
PENDING = "import-pending.zip"
CHUNK = 1024 * 1024

# Settings from the env file that mean the same thing on any server. Machine-specific ones
# (DATA_DIR, WEB_PORT, PUBLIC_URL, TZ) stay as the new server has them.
PORTABLE = (
    "CLIP_MAX_SECONDS", "KEEP_DAYS", "MAX_STORAGE_GB", "LIBRARY_DAYS", "POLL_MINUTES",
    "USE_BASE_STATION_STORAGE", "CAPTURE_LIVE", "CAPTURE_CLOUD", "CAPTURE_COOLDOWN_SECONDS",
    "TRANSCODE_H264", "NOTIFY_ON_MOTION", "NOTIFY_ON_CLIP", "NOTIFY_COOLDOWN_SECONDS",
    "NTFY_URL", "NTFY_TOKEN", "PUSHOVER_TOKEN", "PUSHOVER_USER", "TWILIO_SID", "TWILIO_TOKEN",
    "TWILIO_FROM", "SMS_TO", "WEBHOOK_URL", "WEB_USER", "WEB_PASSWORD", "LOG_LEVEL",
    "ARLO_USERNAME", "ARLO_PASSWORD", "TFA_SOURCE", "TFA_TYPE", "TFA_HOST", "TFA_USERNAME", "TFA_PASSWORD",
)

# Files in DATA_DIR that are never copied (they belong to this machine or are rebuilt).
SKIP_TOP = {"live", "logs", "arlo-keeper.env", PENDING, "import-result.json"}


# --- what an export holds ------------------------------------------------------
def _db_snapshot(dest):
    """A consistent copy of clips.db, safe while Arlo Keeper is running (SQLite backup API)."""
    src = sqlite3.connect(config.DB_PATH, timeout=30)
    out = sqlite3.connect(dest)
    try:
        src.backup(out)
    finally:
        out.close()
        src.close()


def _files():
    """(path inside the zip, path on disk) for everything but the database and clips."""
    out = []
    for top in ("photo.jpg", "ban.apk"):
        p = os.path.join(config.DATA_DIR, top)
        if os.path.isfile(p):
            out.append((top, p))
    for root, _dirs, names in os.walk(config.STATE_DIR):
        for n in names:
            p = os.path.join(root, n)
            out.append(("state/" + os.path.relpath(p, config.STATE_DIR).replace(os.sep, "/"), p))
    return out


def _clip_files():
    out = []
    for root, _dirs, names in os.walk(config.CLIPS_DIR):
        for n in names:
            if n.endswith((".part", ".tmp")):
                continue
            p = os.path.join(root, n)
            out.append(("clips/" + os.path.relpath(p, config.CLIPS_DIR).replace(os.sep, "/"), p))
    return out


def summary():
    """Counts for the Move page."""
    n, size = 0, 0
    for _, p in _clip_files():
        n += 1
        try:
            size += os.path.getsize(p)
        except OSError:
            pass
    try:
        with sqlite3.connect(config.DB_PATH, timeout=30) as c:
            clips = c.execute("SELECT COUNT(*) FROM clips").fetchone()[0]
    except sqlite3.Error:
        clips = 0
    return {"clips": clips, "clip_files": n, "clip_bytes": size}


def _manifest(include_clips, clip_count):
    return {
        "format": FORMAT,
        "app": "Arlo Keeper / BAN",
        "made": time.strftime("%Y-%m-%d %H:%M:%S"),
        "made_by": socket.gethostname(),
        "os": sys.platform,
        "includes_clips": include_clips,
        "clips": clip_count,
        "settings": {k: os.environ[k] for k in PORTABLE if os.environ.get(k)},
    }


class _Pipe:
    """A write-only stream zipfile can write to; the web page sends what piles up in it."""

    def __init__(self):
        self.buf = bytearray()

    def write(self, b):
        self.buf += b
        return len(b)

    def flush(self):
        pass

    def take(self):
        out, self.buf = bytes(self.buf), bytearray()
        return out


def _write(zf, arcname, path, pipe=None):
    """Add one file. With a pipe, yields what's ready after each chunk (so big files stream)."""
    method = zipfile.ZIP_STORED if arcname.endswith((".mp4", ".jpg", ".apk", ".zip")) else zipfile.ZIP_DEFLATED
    info = zipfile.ZipInfo.from_file(path, arcname)
    info.compress_type = method
    with open(path, "rb") as src, zf.open(info, "w", force_zip64=True) as dst:
        while True:
            chunk = src.read(CHUNK)
            if not chunk:
                break
            dst.write(chunk)
            if pipe is not None and len(pipe.buf) >= CHUNK:
                yield pipe.take()


def stream_export(include_clips=True):
    """The export .zip, piece by piece (for the web page's download). Nothing is written to disk
    except a copy of the database."""
    pipe = _Pipe()
    with tempfile.TemporaryDirectory(dir=config.DATA_DIR) as tmp:
        db = os.path.join(tmp, "clips.db")
        _db_snapshot(db)
        with sqlite3.connect(db) as c:
            count = c.execute("SELECT COUNT(*) FROM clips").fetchone()[0]
        with zipfile.ZipFile(pipe, "w", allowZip64=True) as zf:
            zf.writestr(MANIFEST, json.dumps(_manifest(include_clips, count), indent=2))
            yield from _write(zf, "clips.db", db, pipe)
            for arc, p in _files():
                try:
                    yield from _write(zf, arc, p, pipe)
                except OSError as e:
                    log.warning("export: skipped %s (%s)", p, e)
            if include_clips:
                for arc, p in _clip_files():
                    try:
                        yield from _write(zf, arc, p, pipe)
                    except OSError as e:       # deleted by cleanup while exporting
                        log.warning("export: skipped %s (%s)", p, e)
                    if pipe.buf:
                        yield pipe.take()
        yield pipe.take()


def export_to(path, include_clips=True):
    with open(path, "wb") as f:
        for chunk in stream_export(include_clips):
            f.write(chunk)
    return path


# --- import -------------------------------------------------------------------
def read_manifest(path):
    try:
        with zipfile.ZipFile(path) as zf:
            m = json.loads(zf.read(MANIFEST))
    except (zipfile.BadZipFile, KeyError, ValueError) as e:
        raise ValueError(f"That isn't an Arlo Keeper export ({e}).")
    if m.get("format", 0) > FORMAT:
        raise ValueError("That export was made by a newer Arlo Keeper. Update this one first.")
    return m


def _safe_target(base, rel):
    """Where a zip entry goes, refusing anything that would land outside base."""
    rel = rel.replace("\\", "/")
    parts = [p for p in rel.split("/") if p not in ("", ".")]
    if not parts or any(p == ".." or ":" in p for p in parts):
        return None
    dest = os.path.join(base, *parts)
    if not os.path.abspath(dest).startswith(os.path.abspath(base) + os.sep):
        return None
    return dest


def _extract(zf, info, dest):
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    tmp = dest + ".importing"
    with zf.open(info) as src, open(tmp, "wb") as out:
        shutil.copyfileobj(src, out, CHUNK)
    os.replace(tmp, dest)
    try:
        t = time.mktime(info.date_time + (0, 0, -1))
        os.utime(dest, (t, t))
    except (OverflowError, ValueError, OSError):
        pass


def _is_lan(url):
    host = urlparse(url or "").hostname
    if not host:
        return False
    if host in ("localhost",) or host.endswith((".local", ".lan", ".home")):
        return True
    try:
        ip = ipaddress.ip_address(host)
        return ip.is_private or ip.is_loopback
    except ValueError:
        return "." not in host          # a bare machine name like "tapper"


def _fix_db(db_path):
    """Make the imported database right for this machine."""
    with sqlite3.connect(db_path) as c:
        # Clip paths are relative to the clips folder; store them with '/' (works on both systems).
        c.execute("UPDATE clips SET video_path=REPLACE(video_path, '\\', '/'), "
                  "thumb_path=REPLACE(thumb_path, '\\', '/')")
        # The old server's home-network address in alert links would point at the old machine.
        r = c.execute("SELECT value FROM settings WHERE key='notify'").fetchone()
        if r:
            s = json.loads(r[0])
            if _is_lan(s.get("public_url")):
                s.pop("public_url", None)            # this server's own address is used instead
                c.execute("UPDATE settings SET value=? WHERE key='notify'", (json.dumps(s),))
        # A server that was turned off after its export should record here.
        c.execute("DELETE FROM settings WHERE key IN ('moved_away', 'tunnel')")


def merge_settings_file(path, settings, source):
    """Put the portable settings into this server's env file (replacing lines for the same keys)."""
    if not path or not settings:
        return []
    try:
        with open(path, encoding="utf-8-sig") as f:
            lines = f.read().splitlines()
    except FileNotFoundError:
        lines = []
    changed, seen = [], set()
    for i, line in enumerate(lines):
        s = line.strip()
        if s.startswith("#") or "=" not in s:
            continue
        k = s.split("=", 1)[0].strip()
        if k in settings:
            seen.add(k)
            new = f"{k}={settings[k]}"
            if s != new:
                lines[i] = new
                changed.append(k)
    rest = [k for k in settings if k not in seen]
    if rest:
        lines += ["", f"# Brought over from {source} on {time.strftime('%Y-%m-%d')}"]
        lines += [f"{k}={settings[k]}" for k in rest]
        changed += rest
    if changed:
        with open(path + ".new", "w", encoding="utf-8", newline="\r\n" if os.name == "nt" else "\n") as f:
            f.write("\n".join(lines) + "\n")
        os.replace(path + ".new", path)
    return changed


def apply_import(path, settings_file=None):
    """Import an export into DATA_DIR. Arlo Keeper must not be running."""
    m = read_manifest(path)
    os.makedirs(config.DATA_DIR, exist_ok=True)
    result = {"from": m.get("made_by"), "made": m.get("made"), "clips_added": 0, "clips_kept": 0,
              "settings_changed": []}
    with zipfile.ZipFile(path) as zf:
        names = {i.filename: i for i in zf.infolist() if not i.is_dir()}
        if "clips.db" not in names:
            raise ValueError("The export has no database in it.")

        # Keep the current database next to it, in case this has to be undone by hand.
        if os.path.exists(config.DB_PATH):
            backup = config.DB_PATH + time.strftime(".before-import-%Y%m%d-%H%M%S")
            shutil.copy2(config.DB_PATH, backup)
            result["backup"] = backup
        for ext in ("", "-wal", "-shm"):
            try:
                os.remove(config.DB_PATH + ext)
            except FileNotFoundError:
                pass
        _extract(zf, names["clips.db"], config.DB_PATH)
        _fix_db(config.DB_PATH)

        # Arlo sessions: replace, so they match the accounts in the imported database.
        if any(n.startswith("state/") for n in names):
            shutil.rmtree(config.STATE_DIR, ignore_errors=True)
        for name, info in names.items():
            if name.startswith("state/"):
                dest = _safe_target(config.STATE_DIR, name[len("state/"):])
                if dest:
                    _extract(zf, info, dest)
            elif name in ("photo.jpg", "ban.apk"):
                _extract(zf, info, os.path.join(config.DATA_DIR, name))
            elif name.startswith("clips/"):
                dest = _safe_target(config.CLIPS_DIR, name[len("clips/"):])
                if not dest:
                    continue
                if os.path.exists(dest) and os.path.getsize(dest) == info.file_size:
                    result["clips_kept"] += 1
                    continue
                _extract(zf, info, dest)
                if name.endswith(".mp4"):
                    result["clips_added"] += 1

    result["settings_changed"] = merge_settings_file(settings_file, m.get("settings") or {}, m.get("made_by") or "the old server")
    log.info("import from %s done: %s", m.get("made_by"), result)
    return result


# --- web page upload: applied at the next start ----------------------------------
def pending_path():
    return os.path.join(config.DATA_DIR, PENDING)


def result_path():
    return os.path.join(config.DATA_DIR, "import-result.json")


def apply_pending():
    """Called by main.py first thing. Applies an uploaded export, if there is one."""
    p = pending_path()
    if not os.path.exists(p):
        return None
    log.info("importing %s", p)
    try:
        result = {"ok": True, **apply_import(p, os.environ.get("SETTINGS_FILE"))}
    except Exception as e:                 # never leave a bad upload blocking the start
        log.exception("import failed")
        result = {"ok": False, "error": str(e)}
    result["finished"] = time.strftime("%Y-%m-%d %H:%M:%S")
    with open(result_path(), "w") as f:
        json.dump(result, f, indent=2)
    try:
        os.remove(p)
    except OSError:
        pass
    return result


def last_result():
    try:
        with open(result_path()) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def restart_soon(delay=1.5):
    """End the process; systemd (Ubuntu) or the launcher (Windows) starts it again in 10 s."""
    import threading

    def bye():
        time.sleep(delay)
        logging.getLogger("main").info("restarting for the move")
        os._exit(75)
    threading.Thread(target=bye, daemon=True).start()


# --- command line ---------------------------------------------------------------------
def main(argv=None):
    import argparse
    logging.basicConfig(level="INFO", format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    ap = argparse.ArgumentParser(prog="python -m app.migrate", description="Move Arlo Keeper to another server.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("export", help="write everything to a .zip")
    e.add_argument("file")
    e.add_argument("--no-clips", action="store_true", help="settings, accounts and sessions only")
    i = sub.add_parser("import", help="bring a .zip in (stop Arlo Keeper first)")
    i.add_argument("file")
    i.add_argument("--settings-file", default=os.environ.get("SETTINGS_FILE"),
                   help="env file to put the portable settings into")
    a = ap.parse_args(argv)
    if a.cmd == "export":
        s = summary()
        print(f"Exporting {s['clips']} clips ({s['clip_bytes'] / 1e9:.1f} GB) from {config.DATA_DIR} ..."
              if not a.no_clips else f"Exporting settings and sign-ins from {config.DATA_DIR} ...")
        export_to(a.file, include_clips=not a.no_clips)
        print(f"Done: {a.file} ({os.path.getsize(a.file) / 1e9:.2f} GB)")
        print("It holds the Arlo and Twilio passwords: delete it once the new server is running.")
    else:
        r = apply_import(a.file, a.settings_file)
        print(json.dumps(r, indent=2))


if __name__ == "__main__":
    main()
