"""Keeps Camera Keeper on Windows up to date by itself, like GarysTV.

Every few hours (and when "Check for updates" is pressed on the Alerts page) it reads the newest
version from the camera-keeper-windows release in the public prod repo (version.txt, uploaded
after CameraKeeper-Setup.exe). If it's newer than this install (VERSION, written by the build),
it downloads the setup and runs it silently through a one-off scheduled task, so the installer
isn't stopped along with Camera Keeper (its first step stops Camera Keeper's own task). Setup
puts the new code in place and starts Camera Keeper again; clips and settings are kept.

Only on an installed Windows build (it has a VERSION file). Turn it off on the Alerts page.
"""
import hashlib
import logging
import os
import re
import subprocess
import sys
import threading
import time

import requests

from . import config, store

log = logging.getLogger("updater")

RELEASE = "https://github.com/userxx09-afk/prod/releases/download/camera-keeper-windows"
SETUP_URL = f"{RELEASE}/CameraKeeper-Setup.exe"
VERSION_URL = f"{RELEASE}/version.txt"
CHECK_SECONDS = 6 * 3600
FIRST_CHECK_SECONDS = 10 * 60      # not right at start: let it settle (and not loop on a bad build)
TASK = "Camera Keeper Update"
KEY = "auto_update"                 # {"on": bool}

_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # the install folder
_state = {"checked": None, "latest": None, "error": None, "installing": False}
_lock = threading.Lock()


def current():
    """This install's version ("1.0.17"), or None when it isn't an installed Windows build."""
    try:
        with open(os.path.join(_HERE, "VERSION"), encoding="utf-8-sig") as f:
            v = f.read().strip()
        return v if re.fullmatch(r"\d+(\.\d+){1,3}", v) else None
    except OSError:
        return None


def supported():
    return sys.platform == "win32" and current() is not None


def enabled():
    return supported() and (store.get_setting(KEY) or {}).get("on", True)


def set_enabled(on):
    store.set_setting(KEY, {"on": bool(on)})


def _num(v):
    return tuple(int(x) for x in v.split("."))


def newer(a, b):
    """True if version a is newer than b."""
    try:
        return _num(a) > _num(b)
    except (ValueError, AttributeError):
        return False


def state():
    with _lock:
        return {**_state, "current": current(), "supported": supported(), "on": enabled()}


def check(install=True):
    """Look for a newer version; install it if there is one (and install is True). Returns state()."""
    cur = current()
    try:
        r = requests.get(VERSION_URL, timeout=20)
        r.raise_for_status()
        lines = r.text.strip().splitlines()
        latest = lines[0].strip() if lines else ""
        sha = next((ln.split("=", 1)[1].strip().lower() for ln in lines[1:] if ln.lower().startswith("sha256=")), None)
        if not re.fullmatch(r"\d+(\.\d+){1,3}", latest):
            raise ValueError(f"odd version {latest!r}")
        with _lock:
            _state.update(checked=time.time(), latest=latest, error=None)
        if install and cur and newer(latest, cur):
            _install(latest, sha)
    except Exception as e:
        log.warning("update check failed: %s", e)
        with _lock:
            _state.update(checked=time.time(), error=str(e))
    return state()


def _install(version, sha):
    with _lock:
        if _state["installing"]:
            return
        _state["installing"] = True
    try:
        folder = os.path.join(config.DATA_DIR, "updates")
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, f"CameraKeeper-Setup-{version}.exe")
        log.info("updating %s -> %s: downloading", current(), version)
        h = hashlib.sha256()
        with requests.get(SETUP_URL, stream=True, timeout=60) as r:
            r.raise_for_status()
            with open(path + ".part", "wb") as f:
                for chunk in r.iter_content(1 << 20):
                    f.write(chunk)
                    h.update(chunk)
        if sha and h.hexdigest() != sha:
            os.remove(path + ".part")
            raise RuntimeError("the download didn't match its checksum (the release may still be uploading)")
        os.replace(path + ".part", path)
        for old in os.listdir(folder):
            if old.endswith(".exe") and old != os.path.basename(path):
                try:
                    os.remove(os.path.join(folder, old))
                except OSError:
                    pass
        log_file = os.path.join(config.DATA_DIR, "logs", "update-setup.log")
        cmd = f'"{path}" /VERYSILENT /SUPPRESSMSGBOXES /NORESTART /LOG="{log_file}"'
        # A scheduled task of its own (as SYSTEM): the setup outlives Camera Keeper being stopped.
        subprocess.run(["schtasks", "/Create", "/F", "/TN", TASK, "/TR", cmd, "/SC", "ONCE", "/ST", "00:00",
                        "/RU", "SYSTEM", "/RL", "HIGHEST"], check=True, capture_output=True, timeout=30)
        subprocess.run(["schtasks", "/Run", "/TN", TASK], check=True, capture_output=True, timeout=30)
        log.info("updating to %s: setup started; Camera Keeper restarts in about a minute", version)
    except Exception:
        with _lock:
            _state["installing"] = False
        raise


def start():
    """The background check, on an installed Windows build."""
    if not supported():
        return

    def loop():
        time.sleep(FIRST_CHECK_SECONDS)
        while True:
            if enabled():
                check()
            time.sleep(CHECK_SECONDS)

    threading.Thread(target=loop, daemon=True, name="updater").start()
    log.info("Camera Keeper %s; checks for updates every %d hours", current(), CHECK_SECONDS // 3600)
