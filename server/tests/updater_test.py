"""Windows self-update (app/updater.py) without Windows or a network.  python tests/updater_test.py"""
import os
import sys
import tempfile

D = tempfile.mkdtemp()
os.environ.update(DATA_DIR=D)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
os.makedirs(os.path.join(D, "clips"), exist_ok=True)

from app import store, updater, web  # noqa: E402

store.init()
web.init_secret()
fails = []


def check(what, ok):
    print(("ok   " if ok else "FAIL ") + what)
    if not ok:
        fails.append(what)


check("1.0.20 is newer than 1.0.9", updater.newer("1.0.20", "1.0.9"))
check("same version isn't newer", not updater.newer("1.0.9", "1.0.9"))
check("junk isn't newer", not updater.newer("x", "1.0.9"))


class Resp:
    def __init__(self, text):
        self.text = text

    def raise_for_status(self):
        pass


installed = []
updater.requests.get = lambda url, timeout=None: Resp("1.0.30\nsha256=abc")
updater._install = lambda v, sha: installed.append((v, sha))
updater.current = lambda: "1.0.20"
st = updater.check()
check("finds the newer version and installs it", st["latest"] == "1.0.30" and installed == [("1.0.30", "abc")])
updater.current = lambda: "1.0.30"
installed.clear()
updater.check()
check("up to date: installs nothing", installed == [])
updater.requests.get = lambda url, timeout=None: Resp("<html>")
check("a bad version file is an error, not an install", updater.check()["error"] and installed == [])

updater.supported = lambda: True
web.app.config["TESTING"] = True
r = web.app.test_client().get("/settings", environ_base={"REMOTE_ADDR": "192.168.5.20"})
check("Alerts page shows Updates", r.status_code == 200 and b"Check for updates" in r.data and b"1.0.30" in r.data)
r = web.app.test_client().post("/settings/update", data={"action": "toggle", "on": "0"}, environ_base={"REMOTE_ADDR": "192.168.5.20"})
check("automatic updates can be turned off", r.status_code == 302 and (store.get_setting("auto_update") or {}).get("on") is False)

print("\nall passed" if not fails else f"\n{len(fails)} failed")
sys.exit(1 if fails else 0)
