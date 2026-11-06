"""Tells GarysTV (the TV/DVR app) when a motion clip starts, so it can pop the clip up on the TV.

A GarysTV with Arlo turned on (its Settings > Arlo) signs itself up here every few minutes
(POST /api/subscribe). Sign-ups nobody renews for 30 minutes are dropped, so a GarysTV
that was switched off or had Arlo turned off stops getting calls.
"""
import logging
import threading
import time

import requests

from . import store

log = logging.getLogger("garystv")

KEY = "garystv_subscribers"
EXPIRE_SECONDS = 30 * 60
_lock = threading.Lock()


def _load():
    return store.get_setting(KEY) or {}


def subscribe(url):
    url = url.rstrip("/")
    with _lock:
        subs = _load()
        new = url not in subs
        subs[url] = time.time()
        store.set_setting(KEY, subs)
    if new:
        log.info("GarysTV at %s will get motion clips", url)


def unsubscribe(url):
    with _lock:
        subs = _load()
        if subs.pop(url.rstrip("/"), None) is not None:
            store.set_setting(KEY, subs)
            log.info("GarysTV at %s no longer gets motion clips", url)


def active():
    now = time.time()
    with _lock:
        subs = _load()
        keep = {u: t for u, t in subs.items() if now - t < EXPIRE_SECONDS}
        if keep != subs:
            store.set_setting(KEY, keep)
    return sorted(keep)


def motion_started(event):
    """event: {camera, camera_id, time (ms), file, key, live, clip}. Sent in the background."""
    for url in active():
        threading.Thread(target=_post, args=(url, event), daemon=True, name="garystv-post").start()


def _post(url, event):
    try:
        r = requests.post(f"{url}/api/arlo/motion", json=event, timeout=5)
        if r.status_code >= 300:
            log.warning("GarysTV at %s answered %s: %s", url, r.status_code, r.text[:200])
        else:
            log.info("told GarysTV at %s: motion on %s", url, event.get("camera"))
    except Exception as e:
        log.warning("couldn't reach GarysTV at %s: %s", url, e)
