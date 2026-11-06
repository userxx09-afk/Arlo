"""A password for visits from outside the home (through the Cloudflare tunnel).

At home nothing changes: the page, GarysTV and the phone app on the home Wi-Fi work without
signing in. A request that came in through Cloudflare (it carries a Cf-Connecting-IP header,
which cloudflared adds) must be signed in. Until a password is set on the Alerts page, outside
visits are turned away entirely, so a new tunnel never exposes the cameras by accident.

Signing in sets a cookie that lasts a year, so the phone app signs in once.
"""
import secrets
import threading
import time

from werkzeug.security import check_password_hash, generate_password_hash

from . import store

KEY = "remote_access"
MAX_FAILS = 8              # wrong passwords from one address before it has to wait
LOCK_SECONDS = 15 * 60
_fails = {}
_lock = threading.Lock()


def settings():
    return store.get_setting(KEY) or {}


def enabled():
    s = settings()
    return bool(s.get("user") and s.get("hash"))


def set_login(user, password):
    store.set_setting(KEY, {**settings(), "user": user.strip().lower(), "hash": generate_password_hash(password),
                            "stamp": secrets.token_hex(8)})


def turn_off():
    s = settings()
    s.pop("hash", None)
    s["stamp"] = secrets.token_hex(8)
    store.set_setting(KEY, s)


def stamp():
    """Changes whenever the password does, so old sign-ins stop working."""
    return settings().get("stamp")


def client_ip(request):
    return request.headers.get("Cf-Connecting-IP") or request.remote_addr or "?"


def is_remote(request):
    return bool(request.headers.get("Cf-Connecting-IP"))


def locked(ip):
    with _lock:
        n, until = _fails.get(ip, (0, 0))
        return n >= MAX_FAILS and time.time() < until


def check(ip, user, password):
    """True if right. Wrong answers count toward a 15-minute lockout for that address."""
    if locked(ip):
        return False
    s = settings()
    ok = bool(s.get("hash")) and (user or "").strip().lower() == s.get("user") \
        and check_password_hash(s["hash"], password or "")
    with _lock:
        if ok:
            _fails.pop(ip, None)
        else:
            n, _ = _fails.get(ip, (0, 0))
            _fails[ip] = (n + 1, time.time() + LOCK_SECONDS)
    return ok
