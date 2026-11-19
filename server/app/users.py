"""People who sign in with Google, and which cameras each one may see.

Off until the owner fills in Google sign-in on the People page (or the GOOGLE_CLIENT_ID,
GOOGLE_CLIENT_SECRET and BCN_OWNER_EMAIL settings). Until then BCN works exactly as before.

Once on:
- Everyone signs in with Google. The owner's Google account (BCN_OWNER_EMAIL) gets in straight
  away and sees everything. Anyone else waits until the owner approves them on the People page.
- The owner can make other people admins: admins also see everything.
- Everyone else sees only the cameras on the Arlo/Blink accounts and IP cameras they added
  themselves. Accounts nobody claimed (everything added before sign-in was turned on) belong to
  the owner, so other people never see them.
- At home, the page, GarysTV and the phone app can still get in without signing in (as admin),
  until the owner turns "Trust the home network" off. The away-from-home password still works
  too, as admin, until it's turned off on the Alerts page.
"""
import os
import secrets
import threading
import time

from . import store

USERS_KEY = "users"                 # {uid: {email, name, role, approved, created, last_seen, stamp}}
GOOGLE_KEY = "google_signin"        # {client_id, client_secret, owner_email, base_url, home_trusted}
OWNERS_KEY = "account_owners"       # {"arlo:email" | "blink:email" | "ipcam:<camera id>": uid}
SEEN_KEY = "camera_accounts"        # {camera id: account key}, so old clips stay with their owner
DEFAULT_BASE = "https://bcn.tapper.co"
ROLES = ("owner", "admin", "user")

_lock = threading.Lock()


# --- Google sign-in settings -----------------------------------------------------------
def google():
    s = store.get_setting(GOOGLE_KEY) or {}
    return {"client_id": s.get("client_id") or os.environ.get("GOOGLE_CLIENT_ID", ""),
            "client_secret": s.get("client_secret") or os.environ.get("GOOGLE_CLIENT_SECRET", ""),
            "owner_email": (s.get("owner_email") or os.environ.get("BCN_OWNER_EMAIL", "")).strip().lower(),
            "base_url": (s.get("base_url") or os.environ.get("BCN_PUBLIC_URL") or DEFAULT_BASE).rstrip("/"),
            "home_trusted": s.get("home_trusted", True)}


def enabled():
    # BCN_GOOGLE_SIGNIN=off in the settings file turns it off whatever the page says (if you're locked out).
    if os.environ.get("BCN_GOOGLE_SIGNIN", "").strip().lower() in ("0", "off", "false", "no"):
        return False
    g = google()
    return bool(g["client_id"] and g["client_secret"] and g["owner_email"])


def home_trusted():
    return bool(google()["home_trusted"])


def save_google(client_id=None, client_secret=None, owner_email=None, base_url=None, home_trusted=None):
    """Blank client_secret keeps the saved one."""
    with _lock:
        s = store.get_setting(GOOGLE_KEY) or {}
        if client_id is not None:
            s["client_id"] = client_id.strip()
        if client_secret:
            s["client_secret"] = client_secret.strip()
        if owner_email is not None:
            s["owner_email"] = owner_email.strip().lower()
        if base_url is not None:
            s["base_url"] = base_url.strip().rstrip("/")
        if home_trusted is not None:
            s["home_trusted"] = bool(home_trusted)
        store.set_setting(GOOGLE_KEY, s)


def turn_off():
    """Back to how BCN was before: no Google sign-in. People and who owns what are kept."""
    with _lock:
        s = store.get_setting(GOOGLE_KEY) or {}
        s.pop("client_secret", None)
        store.set_setting(GOOGLE_KEY, s)


def redirect_uri():
    return google()["base_url"] + "/auth/google/callback"


# --- people ----------------------------------------------------------------------------
def users():
    return store.get_setting(USERS_KEY) or {}


def _save(us):
    store.set_setting(USERS_KEY, us)


def get(uid):
    return users().get(uid) if uid else None


def by_email(email):
    email = (email or "").strip().lower()
    return next(((uid, u) for uid, u in users().items() if u["email"] == email), (None, None))


def is_admin(u):
    return bool(u) and u.get("role") in ("owner", "admin") and u.get("approved")


def sign_in(email, name=""):
    """A verified Google sign-in. Returns (uid, user). New people wait for approval."""
    email = email.strip().lower()
    owner = email == google()["owner_email"]
    now = int(time.time())
    with _lock:
        us = users()
        uid = next((k for k, u in us.items() if u["email"] == email), None)
        if uid is None:
            uid = secrets.token_hex(6)
            us[uid] = {"email": email, "name": name or email, "role": "user", "approved": False,
                       "created": now, "stamp": secrets.token_hex(8)}
        u = us[uid]
        u["name"] = name or u.get("name") or email
        u["last_seen"] = now
        if owner:
            u["role"], u["approved"] = "owner", True
        elif u.get("role") == "owner":       # the owner email was changed to someone else
            u["role"] = "admin"
        _save(us)
        return uid, dict(u)


def update(uid, approved=None, role=None):
    with _lock:
        us = users()
        u = us.get(uid)
        if not u or u.get("role") == "owner":
            return False                     # the owner is set by BCN_OWNER_EMAIL, not here
        if approved is not None:
            u["approved"] = bool(approved)
        if role in ("admin", "user"):
            u["role"] = role
        u["stamp"] = secrets.token_hex(8)     # signs them out everywhere, so the change takes effect now
        _save(us)
        return True


def remove(uid):
    """Forget a person. The accounts they added go back to the owner (they keep recording)."""
    with _lock:
        us = users()
        if uid not in us or us[uid].get("role") == "owner":
            return False
        us.pop(uid)
        _save(us)
        owners = store.get_setting(OWNERS_KEY) or {}
        store.set_setting(OWNERS_KEY, {k: v for k, v in owners.items() if v != uid})
        return True


# --- who owns which camera account ---------------------------------------------------------
def account_key(brand, username):
    return f"{brand}:{(username or '').strip().lower()}"


def ipcam_key(cid):
    return f"ipcam:{cid}"


def camera_key(cam):
    brand = cam.get("brand") or "arlo"
    return ipcam_key(cam["id"]) if brand == "ipcam" else account_key(brand, cam.get("account"))


def owners():
    return store.get_setting(OWNERS_KEY) or {}


def owner_of(key):
    """The uid that added this account, or None (nobody claimed it: it's the owner's)."""
    return owners().get(key)


def set_owner(key, uid):
    if not uid:
        return
    with _lock:
        o = owners()
        if o.get(key) != uid:
            o[key] = uid
            store.set_setting(OWNERS_KEY, o)



def can_use(key, viewer):
    """May this viewer see / change this account? Admins: always. Others: only their own."""
    if viewer.get("admin"):
        return True
    return bool(viewer.get("uid")) and owner_of(key) == viewer["uid"]


def taken_by_other(key, viewer, exists):
    """Someone else already has this account here, so this viewer may not add it or change
    its password. `exists`: an account with this key is already saved. Admins may always."""
    if viewer.get("admin"):
        return False
    o = owner_of(key)
    return (o is not None and o != viewer.get("uid")) or (o is None and exists)


def claim(key, viewer):
    """Whoever adds an account owns it. Admins don't take over an account someone else added."""
    if viewer.get("uid") and not (viewer.get("admin") and owner_of(key)):
        set_owner(key, viewer["uid"])


def allowed_cameras(viewer, cams):
    """The camera ids this viewer may see, or None for everything (admins)."""
    if viewer.get("admin"):
        _remember(cams)
        return None
    uid = viewer.get("uid")
    if not uid:
        return set()
    o = owners()
    seen = _remember(cams)
    return {cid for cid, key in seen.items() if o.get(key) == uid}


def _remember(cams):
    """camera id -> account key, kept after an account signs out so its clips stay private."""
    with _lock:
        seen = store.get_setting(SEEN_KEY) or {}
        changed = False
        for c in cams:
            k = camera_key(c)
            if seen.get(c["id"]) != k:
                seen[c["id"]] = k
                changed = True
        if changed:
            store.set_setting(SEEN_KEY, seen)
        return seen


def private_camera(cam_id):
    """True if this camera belongs to someone who isn't an admin. The household's alerts (texts,
    ntfy, Pushover...) skip it: they'd tell the owner about someone else's cameras. Each person
    getting their own alerts comes next (docs/global-bcn.md, step 3)."""
    if not enabled():
        return False
    key = (store.get_setting(SEEN_KEY) or {}).get(cam_id)
    uid = owners().get(key) if key else None
    u = users().get(uid) if uid else None
    return bool(u) and not is_admin(u)
