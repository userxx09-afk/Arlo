"""Arlo sign-in from the web page.

The password and 2FA code are typed into the Connect page instead of a config
file. pyaarlo lets us pass our own 2FA source object; its get() waits here
until the code is entered on the page.
"""
import threading
import time

from . import config, store

CODE_WAIT_SECONDS = 600

_lock = threading.Lock()
_creds_changed = threading.Event()
_code_ready = threading.Event()
_code = None
state = {"waiting_for_code": False, "code_requested_at": None}


def credentials():
    saved = store.get_setting("arlo_login") or {}
    if "password" in saved:  # set from the web page; an empty value means "forgotten"
        user, pw = saved.get("username"), saved.get("password")
    else:
        user, pw = config.ARLO_USERNAME, config.ARLO_PASSWORD
    return (user, pw) if user and pw else (None, None)


def save_credentials(username, password):
    store.set_setting("arlo_login", {"username": username.strip(), "password": password})
    _creds_changed.set()


def forget_password():
    saved = store.get_setting("arlo_login") or {}
    saved["password"] = ""
    store.set_setting("arlo_login", saved)


def wait_for_new_credentials(timeout=None):
    """Block until the Connect form is submitted."""
    got = _creds_changed.wait(timeout)
    _creds_changed.clear()
    return got


def submit_code(code):
    global _code
    with _lock:
        _code = "".join(ch for ch in code if ch.isdigit())
    _code_ready.set()


class WebTFA:
    """pyaarlo 2FA source: shows a code box on the web page and waits for it."""

    def __init__(self, arlo=None):
        self._arlo = arlo

    def start(self):
        global _code
        with _lock:
            _code = None
        _code_ready.clear()
        state.update(waiting_for_code=True, code_requested_at=time.time())
        return True

    def get(self):
        _code_ready.wait(CODE_WAIT_SECONDS)
        with _lock:
            code = _code
        state["waiting_for_code"] = False
        return code or ""

    def stop(self):
        state["waiting_for_code"] = False

    def debug(self, msg):
        pass
