"""A password for visits from outside the home (through the Cloudflare tunnel).

At home nothing changes: the page, GarysTV and the phone app on the home Wi-Fi work without
signing in. A request that came in through Cloudflare (it carries a Cf-Connecting-IP header,
which cloudflared adds) must be signed in. Until a password is set on the Alerts page, outside
visits are turned away entirely, so a new tunnel never exposes the cameras by accident.

Signing in sets a cookie that lasts a year, so the phone app signs in once.
"""
import hashlib
import secrets
import threading
import time

from werkzeug.security import check_password_hash, generate_password_hash

from . import store

KEY = "remote_access"
MAX_FAILS = 8              # wrong passwords from one address before it has to wait
LOCK_SECONDS = 15 * 60
_fails = {}
_ok_cache = {}          # right Basic sign-ins -> expiry, so apps that send one with every request stay fast
CACHE_SECONDS = 10 * 60
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


def check_basic(ip, user, password):
    """HTTP Basic sign-in: an app with the name and password in the address, like GarysTV's
    https://name:password@ban.tapper.co. Same name, password and lockout as the sign-in page.
    A right answer is remembered for a few minutes, since apps send it with every request."""
    key = hashlib.sha256(f"{(user or '').strip().lower()}\0{password or ''}\0{stamp()}".encode()).hexdigest()
    now = time.time()
    with _lock:
        if _ok_cache.get(key, 0) > now:
            return True
    if not check(ip, user, password):
        return False
    with _lock:
        for k in [k for k, t in _ok_cache.items() if t <= now]:
            _ok_cache.pop(k, None)
        _ok_cache[key] = now + CACHE_SECONDS
    return True
