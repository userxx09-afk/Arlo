"""Instant motion alerts (app/notify.py) without any alert service.  python tests/alerts_test.py"""
import os
import sys
import tempfile
import time

D = tempfile.mkdtemp()
os.environ.update(DATA_DIR=D)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from app import notify, store  # noqa: E402

store.init()
fails = []


def check(what, ok):
    print(("ok   " if ok else "FAIL ") + what)
    if not ok:
        fails.append(what)


check("new installs alert the instant motion starts", notify.current()["on_motion"] is True)
store.set_setting(notify.SETTINGS_KEY, {"on_motion": False, "on_clip": True, "cooldown": 60})
store.set_setting(notify.INSTANT_KEY, None)
check("saved settings from before get instant alerts once", notify.current()["on_motion"] is True)
s = store.get_setting(notify.SETTINGS_KEY)
s["on_motion"] = False
notify.save(s)
check("...and turning it off again sticks", notify.current()["on_motion"] is False)
notify.save({**s, "on_motion": True})
check("no instant alert yet: the clip alert goes", notify.clip_alert_needed("A1"))
notify._last_sent[("A1", "motion")] = time.time()
check("just sent an instant alert: the clip alert is skipped", not notify.clip_alert_needed("A1"))
check("other cameras still get theirs", notify.clip_alert_needed("B1"))
notify.save({**s, "on_motion": True, "clip_too": True})
check("'also send the clip alert' sends both", notify.clip_alert_needed("A1"))
notify._last_sent[("A1", "motion")] = time.time() - 600
notify.save({**s, "on_motion": True})
check("a later clip (new event) alerts again", notify.clip_alert_needed("A1"))

print("\nall passed" if not fails else f"\n{len(fails)} failed")
sys.exit(1 if fails else 0)
