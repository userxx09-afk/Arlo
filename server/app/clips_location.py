"""Where clips are kept, and moving them somewhere else (another drive, say).

The clips folder is CLIPS_DIR in the settings file (default: DATA_DIR/clips). The database,
settings and Arlo sessions stay in DATA_DIR; clip paths in the database are relative to the clips
folder, so moving it changes nothing else.

Moving copies every clip to the new folder (in the background, with progress for the page), copies
whatever was recorded meanwhile, saves CLIPS_DIR to the settings file and restarts. The next start
deletes the old copies (only files that made it to the new folder), see finish_move().

Changing it needs the settings file to be writable by the app: true on Windows (the launcher's
camera-keeper.env). On Ubuntu and Docker, set CLIPS_DIR in the env file / compose file instead.
"""
import json
import logging
import os
import shutil
import string
import threading
import time

from . import config

log = logging.getLogger("clips")
MARKER = "clips-moved-from.json"
_state = {"moving": False}
_lock = threading.Lock()


def _marker():
    return os.path.join(config.DATA_DIR, MARKER)


def _usage(path):
    p = path
    while p and not os.path.exists(p):         # a folder that doesn't exist yet: its drive
        parent = os.path.dirname(p)
        if parent == p:
            break
        p = parent
    try:
        u = shutil.disk_usage(p or path)
        return {"free": u.free, "total": u.total}
    except OSError:
        return {"free": None, "total": None}


def _folder_bytes(path):
    n = 0
    for root, _d, files in os.walk(path):
        for f in files:
            try:
                n += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return n


def drives():
    """Places the clips could go: Windows drives, or Linux mount points."""
    out = []
    if os.name == "nt":
        for letter in string.ascii_uppercase:
            root = f"{letter}:\\"
            if os.path.exists(root):
                u = _usage(root)
                if u["total"]:
                    out.append({"path": root, **u})
    else:
        seen = set()
        try:
            with open("/proc/mounts") as f:
                for line in f:
                    dev, mnt, fs = line.split()[:3]
                    if fs in ("ext4", "xfs", "btrfs", "ntfs", "ntfs3", "exfat", "vfat", "zfs", "nfs", "nfs4", "cifs") \
                            and mnt not in seen and not mnt.startswith(("/boot", "/snap")):
                        seen.add(mnt)
                        out.append({"path": mnt, **_usage(mnt)})
        except OSError:
            pass
    return out


def can_change():
    return bool(os.environ.get("SETTINGS_FILE"))


def state():
    return {"clips_dir": config.CLIPS_DIR, "default": os.path.join(config.DATA_DIR, "clips"),
            "used_bytes": _folder_bytes(config.CLIPS_DIR) if not _state.get("moving") else _state.get("total_bytes"),
            **_usage(config.CLIPS_DIR), "drives": drives(), "can_change": can_change(),
            "move": {k: v for k, v in _state.items()}}


def _check_target(path):
    path = os.path.abspath(os.path.expandvars(os.path.expanduser(path.strip().strip('"'))))
    cur = os.path.abspath(config.CLIPS_DIR)
    if os.path.normcase(path) == os.path.normcase(cur):
        raise ValueError("That's where the clips already are.")
    if os.path.normcase(path).startswith(os.path.normcase(cur) + os.sep):
        raise ValueError("The new folder can't be inside the current clips folder.")
    if os.path.normcase(cur).startswith(os.path.normcase(path) + os.sep):
        raise ValueError("The new folder can't contain the current clips folder.")
    if os.name == "nt" and path.startswith("\\\\"):
        raise ValueError("Use a drive on this computer (network shares aren't reachable by the service).")
    os.makedirs(path, exist_ok=True)
    probe = os.path.join(path, ".camera-keeper-write-test")
    try:
        with open(probe, "w") as f:
            f.write("ok")
        os.remove(probe)
    except OSError as e:
        raise ValueError(f"Can't write there: {e}")
    if os.listdir(path):
        known = any(n.lower().endswith((".mp4", ".jpg")) or os.path.isdir(os.path.join(path, n)) for n in os.listdir(path))
        if not known:
            raise ValueError("That folder isn't empty. Pick an empty or new folder.")
    need = _folder_bytes(config.CLIPS_DIR)
    free = _usage(path)["free"]
    if free is not None and free < need + 1024 ** 3:
        raise ValueError(f"Not enough room there: the clips need {need / 1e9:.1f} GB, {free / 1e9:.1f} GB is free.")
    return path


def _copy_pass(src, dst, counts):
    for root, _d, files in os.walk(src):
        rel = os.path.relpath(root, src)
        target_dir = dst if rel == "." else os.path.join(dst, rel)
        os.makedirs(target_dir, exist_ok=True)
        for f in files:
            if f.endswith((".part", ".tmp")):
                continue
            s, d = os.path.join(root, f), os.path.join(target_dir, f)
            try:
                size = os.path.getsize(s)
                if not (os.path.exists(d) and os.path.getsize(d) == size):
                    shutil.copy2(s, d + ".moving")
                    os.replace(d + ".moving", d)
                counts["files"] += 1
                counts["bytes"] += size
                _state.update(copied_files=counts["files"], copied_bytes=counts["bytes"])
            except OSError as e:                 # deleted by cleanup meanwhile, or in use
                log.debug("clips move: skipped %s (%s)", s, e)


def start_move(path, restart):
    """Validate and start moving the clips to path. restart() ends the process afterwards."""
    if not can_change():
        raise ValueError("Here the clips folder is set in the env file (CLIPS_DIR), not on this page.")
    with _lock:
        if _state.get("moving"):
            raise ValueError("A move is already running.")
        target = _check_target(path)
        src = config.CLIPS_DIR
        total = _folder_bytes(src)
        _state.clear()
        _state.update(moving=True, to=target, started=time.time(), total_bytes=total, copied_bytes=0,
                      copied_files=0, error=None, done=False)

    def run():
        from . import migrate
        try:
            counts = {"files": 0, "bytes": 0}
            if os.path.isdir(src):
                _copy_pass(src, target, counts)
                _copy_pass(src, target, {"files": 0, "bytes": 0})   # clips recorded during the copy
            changed = migrate.merge_settings_file(os.environ["SETTINGS_FILE"], {"CLIPS_DIR": target}, "the Storage setting")
            with open(_marker(), "w") as f:
                json.dump({"old": src, "new": target, "at": time.strftime("%Y-%m-%d %H:%M:%S")}, f)
            log.info("clips copied to %s (%d files, %.1f GB); settings %s; restarting", target,
                     counts["files"], counts["bytes"] / 1e9, changed)
            _state.update(done=True)
            restart()
        except Exception as e:
            log.exception("moving clips failed")
            _state.update(moving=False, error=str(e))
    threading.Thread(target=run, name="clips-move", daemon=True).start()
    return state()


def finish_move():
    """At startup, after a move: delete the old copies that are safely in the new folder."""
    try:
        with open(_marker()) as f:
            m = json.load(f)
    except (OSError, ValueError):
        return None
    old, new = m.get("old"), m.get("new")
    if not new or os.path.normcase(os.path.abspath(config.CLIPS_DIR)) != os.path.normcase(os.path.abspath(new)):
        log.warning("clips move to %s didn't take effect (clips folder is %s); old clips left at %s",
                    new, config.CLIPS_DIR, old)
        os.remove(_marker())
        return None
    removed = 0
    if old and os.path.isdir(old):
        for root, _d, files in os.walk(old, topdown=False):
            for f in files:
                s = os.path.join(root, f)
                d = os.path.join(new, os.path.relpath(s, old))
                try:
                    if os.path.exists(d) and os.path.getsize(d) == os.path.getsize(s):
                        os.remove(s)
                        removed += 1
                except OSError:
                    pass
            try:
                os.rmdir(root)
            except OSError:
                pass
    os.remove(_marker())
    log.info("clips now in %s; removed %d old copies from %s", new, removed, old)
    return removed
