"""Arlo accounts and their sign-in, from the web page.

BAN can watch several Arlo accounts at once. Each one is saved as {id, username, password}; the
first ("main") keeps the session folder Arlo Keeper always used, so an existing sign-in carries
over without a new code. Passwords and 2FA codes are typed into the Arlo page. pyaarlo lets us
pass our own 2FA source object; its get() waits here until the code is entered on the page.
"""
import os
import secrets
import shutil
import threading
import time

from . import config, store

CODE_WAIT_SECONDS = 600
KEY = "arlo_accounts"
_lock = threading.Lock()


class Login:
    """Sign-in state of one account: waiting for a password change or a code."""

    def __init__(self):
        self.creds_changed = threading.Event()
        self.code_ready = threading.Event()
        self.code = None
        self.removed = False
        self.state = {"waiting_for_code": False, "code_requested_at": None}


_logins = {}


def login(aid):
    with _lock:
        return _logins.setdefault(aid, Login())


# --- saved accounts ----------------------------------------------------------
def accounts():
    """The saved accounts, oldest first. Moves an older single sign-in over the first time."""
    saved = store.get_setting(KEY)
    if saved is None:
        old = store.get_setting("arlo_login") or {}
        if "password" in old:
            user, pw = old.get("username"), old.get("password")
        else:
            user, pw = config.ARLO_USERNAME, config.ARLO_PASSWORD
        saved = [{"id": "main", "username": user, "password": pw or ""}] if user else []
        store.set_setting(KEY, saved)
    return saved


def get(aid):
    return next((a for a in accounts() if a["id"] == aid), None)


def _save(accs):
    store.set_setting(KEY, accs)


def add(username, password):
    """Add an account, or give an existing one (same email) a new password. Returns its id."""
    username = username.strip()
    with _lock:
        accs = accounts()
        for a in accs:
            if a["username"].lower() == username.lower():
                a["password"] = password
                _save(accs)
                break
        else:
            aid = "main" if not any(a["id"] == "main" for a in accs) else secrets.token_hex(4)
            a = {"id": aid, "username": username, "password": password}
            accs.append(a)
            _save(accs)
    login(a["id"]).creds_changed.set()
    return a["id"]


def credentials(aid):
    a = get(aid)
    return (a["username"], a["password"]) if a and a.get("username") and a.get("password") else (None, None)


def forget_password(aid):
    with _lock:
        accs = accounts()
        for a in accs:
            if a["id"] == aid:
                a["password"] = ""
        _save(accs)


def remove(aid):
    """Sign out: forget the account, its password and its saved Arlo session."""
    with _lock:
        _save([a for a in accounts() if a["id"] != aid])
    lg = login(aid)
    lg.removed = True
    lg.creds_changed.set()
    lg.code_ready.set()
    d = state_dir(aid)
    if aid == "main":   # the original folder also holds the other accounts' folders
        for name in os.listdir(d) if os.path.isdir(d) else []:
            p = os.path.join(d, name)
            if os.path.isfile(p):
                os.remove(p)
            elif not _is_account_dir(name):
                shutil.rmtree(p, ignore_errors=True)
    else:
        shutil.rmtree(d, ignore_errors=True)
    with _lock:
        _logins.pop(aid, None)


def _is_account_dir(name):
    return name.startswith("acct-")


def state_dir(aid):
    return config.STATE_DIR if aid == "main" else os.path.join(config.STATE_DIR, f"acct-{aid}")


def wait_for_new_credentials(aid, timeout=None):
    """Block until a new password is entered for this account (or it's signed out)."""
    lg = login(aid)
    got = lg.creds_changed.wait(timeout)
    lg.creds_changed.clear()
    return got


def submit_code(aid, code):
    lg = login(aid)
    lg.code = "".join(ch for ch in code if ch.isdigit())
    lg.code_ready.set()


class WebTFA:
    """pyaarlo 2FA source: shows a code box for this account on the Arlo page and waits for it."""

    def __init__(self, aid):
        self.aid = aid

    def start(self):
        lg = login(self.aid)
        lg.code = None
        lg.code_ready.clear()
        lg.state.update(waiting_for_code=True, code_requested_at=time.time())
        return True

    def get(self):
        lg = login(self.aid)
        lg.code_ready.wait(CODE_WAIT_SECONDS)
        lg.state["waiting_for_code"] = False
        return lg.code or ""

    def stop(self):
        login(self.aid).state["waiting_for_code"] = False

    def debug(self, msg):
        pass
