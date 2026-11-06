"""SQLite index of saved clips, plus retention cleanup."""
import json
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


def query(camera_id=None, day=None, starred=False, limit=200, offset=0):
    sql, args = "SELECT * FROM clips WHERE 1=1", []
    if camera_id:
        sql += " AND camera_id=?"
        args.append(camera_id)
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


def cleanup():
    """Apply KEEP_DAYS and MAX_STORAGE_GB. Starred clips are never removed."""
    removed = 0
    with connect() as c:
        if config.KEEP_DAYS > 0:
            cutoff = int((time.time() - config.KEEP_DAYS * 86400) * 1000)
            for r in c.execute("SELECT * FROM clips WHERE created_ms<? AND starred=0", (cutoff,)).fetchall():
                delete(r["key"])
                removed += 1
        if config.MAX_STORAGE_GB > 0:
            cap = config.MAX_STORAGE_GB * 1024 ** 3
            total = stats()["bytes"]
            if total > cap:
                for r in c.execute("SELECT * FROM clips WHERE starred=0 ORDER BY created_ms").fetchall():
                    if total <= cap:
                        break
                    total -= r["size_bytes"] or 0
                    delete(r["key"])
                    removed += 1
    if removed:
        log.info("retention removed %d clip(s)", removed)
    return removed
