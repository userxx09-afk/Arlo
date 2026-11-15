"""SQLite index of saved clips, plus retention cleanup."""
import json
import shutil
import logging
import os
import sqlite3
import threading
import time

from . import config

log = logging.getLogger("store")
_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS clips (
    key          TEXT PRIMARY KEY,   -- camera_id:created_ms, unique per recording
    camera_id    TEXT NOT NULL,
    camera_name  TEXT NOT NULL,
    created_ms   INTEGER NOT NULL,
    duration_s   REAL,
    trigger      TEXT,               -- motion, audio, ...
    object_type  TEXT,               -- person, vehicle, animal (subscription only)
    source       TEXT,               -- cloud or base_station
    video_path   TEXT NOT NULL,      -- relative to CLIPS_DIR
    thumb_path   TEXT,
    size_bytes   INTEGER,
    starred      INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_clips_created ON clips(created_ms DESC);
CREATE INDEX IF NOT EXISTS idx_clips_camera ON clips(camera_id, created_ms DESC);
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL              -- JSON
);
"""


# --- settings (edited from the web page; env vars are only defaults) --------
def get_setting(key, default=None):
    with connect() as c:
        r = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return json.loads(r["value"]) if r else default


def set_setting(key, value):
    with _lock, connect() as c:
        c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, json.dumps(value)))


def connect():
    os.makedirs(config.DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(config.DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init():
    with _lock, connect() as c:
        c.executescript(SCHEMA)


def has(key):
    with connect() as c:
        return c.execute("SELECT 1 FROM clips WHERE key=?", (key,)).fetchone() is not None


def add(row):
    cols = ",".join(row)
    marks = ",".join("?" for _ in row)
    with _lock, connect() as c:
        c.execute(f"INSERT OR REPLACE INTO clips ({cols}) VALUES ({marks})", tuple(row.values()))


def get(key):
    with connect() as c:
        r = c.execute("SELECT * FROM clips WHERE key=?", (key,)).fetchone()
        return dict(r) if r else None


def query(camera_id=None, day=None, starred=False, limit=200, offset=0, camera_ids=None):
    sql, args = "SELECT * FROM clips WHERE 1=1", []
    if camera_id:
        sql += " AND camera_id=?"
        args.append(camera_id)
    if camera_ids:  # one account's cameras
        sql += f" AND camera_id IN ({','.join('?' * len(camera_ids))})"
        args += list(camera_ids)
    if day:  # YYYY-MM-DD in local time
        start = int(time.mktime(time.strptime(day, "%Y-%m-%d")) * 1000)
        sql += " AND created_ms>=? AND created_ms<?"
        args += [start, start + 86_400_000]
    if starred:
        sql += " AND starred=1"
    sql += " ORDER BY created_ms DESC LIMIT ? OFFSET ?"
    args += [limit, offset]
    with connect() as c:
        return [dict(r) for r in c.execute(sql, args)]


def cameras():
    with connect() as c:
        return [dict(r) for r in c.execute(
            "SELECT camera_id, camera_name, COUNT(*) n, MAX(created_ms) last_ms "
            "FROM clips GROUP BY camera_id ORDER BY camera_name")]


def stats():
    with connect() as c:
        r = c.execute("SELECT COUNT(*) n, COALESCE(SUM(size_bytes),0) b FROM clips").fetchone()
        return {"count": r["n"], "bytes": r["b"]}


def set_starred(key, starred):
    with _lock, connect() as c:
        c.execute("UPDATE clips SET starred=? WHERE key=?", (1 if starred else 0, key))


def _remove_files(row):
    for p in (row["video_path"], row["thumb_path"]):
        if p:
            try:
                os.remove(os.path.join(config.CLIPS_DIR, p))
            except FileNotFoundError:
                pass


def delete(key):
    row = get(key)
    if not row:
        return False
    _remove_files(row)
    with _lock, connect() as c:
        c.execute("DELETE FROM clips WHERE key=?", (key,))
    return True


# --- storage rules (Settings > Storage; GarysTV Settings > Cameras) ------------------------
# max_gb: the clips folder's size cap (0 = none). keep_days: clips older than this go (0 = keep
# forever). when_full: "overwrite" deletes the oldest unstarred clips to make room, "stop" saves
# no new clips until there is room again. Either way the disk itself is "full" once less than
# MIN_FREE_GB is left. Starred clips are never deleted.
STORAGE_KEY = "storage"
WHEN_FULL = ("overwrite", "stop")
MIN_FREE_GB = 1


def storage_rules():
    saved = get_setting(STORAGE_KEY) or {}
    return {"max_gb": int(saved.get("max_gb", config.MAX_STORAGE_GB) or 0),
            "keep_days": int(saved.get("keep_days", config.KEEP_DAYS) or 0),
            "when_full": saved.get("when_full") if saved.get("when_full") in WHEN_FULL else "overwrite"}


def set_storage_rules(max_gb=None, keep_days=None, when_full=None):
    r = storage_rules()
    if max_gb is not None:
        r["max_gb"] = max(0, min(100000, int(max_gb)))
    if keep_days is not None:
        r["keep_days"] = max(0, min(36500, int(keep_days)))
    if when_full is not None:
        if when_full not in WHEN_FULL:
            raise ValueError("when_full is overwrite or stop")
        r["when_full"] = when_full
    set_setting(STORAGE_KEY, r)
    return r


def _disk_free():
    try:
        return shutil.disk_usage(config.DATA_DIR).free
    except OSError:
        return None


def _over(used, free, cap):
    return (cap > 0 and used >= cap) or (free is not None and free < MIN_FREE_GB * 1024 ** 3)


def storage_state():
    """The rules plus how full it is, for the settings pages and GarysTV."""
    r = storage_rules()
    used, free, cap = stats()["bytes"], _disk_free(), r["max_gb"] * 1024 ** 3
    full = _over(used, free, cap)
    return {**r, "used_bytes": used, "disk_free_bytes": free, "full": full,
            "saving": not (full and r["when_full"] == "stop")}


_full_logged = False


def room_for_clip():
    """False when storage is full and the rule is "stop": the caller saves nothing."""
    global _full_logged
    r = storage_rules()
    if r["when_full"] != "stop":
        return True
    full = _over(stats()["bytes"], _disk_free(), r["max_gb"] * 1024 ** 3)
    if full and not _full_logged:
        log.warning("storage is full and set to stop: new clips aren't saved until there is room")
    _full_logged = full
    return not full


def cleanup():
    """Apply the storage rules. Starred clips are never removed."""
    r = storage_rules()
    removed = 0
    with connect() as c:
        if r["keep_days"] > 0:
            cutoff = int((time.time() - r["keep_days"] * 86400) * 1000)
            for row in c.execute("SELECT * FROM clips WHERE created_ms<? AND starred=0", (cutoff,)).fetchall():
                delete(row["key"])
                removed += 1
        if r["when_full"] == "overwrite":
            cap, total, free = r["max_gb"] * 1024 ** 3, stats()["bytes"], _disk_free()
            if _over(total, free, cap):
                for row in c.execute("SELECT * FROM clips WHERE starred=0 ORDER BY created_ms").fetchall():
                    if not _over(total, free, cap):
                        break
                    size = row["size_bytes"] or 0
                    total -= size
                    if free is not None:
                        free += size
                    delete(row["key"])
                    removed += 1
    if removed:
        log.info("retention removed %d clip(s)", removed)
    return removed
