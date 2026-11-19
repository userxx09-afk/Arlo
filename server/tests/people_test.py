"""Google sign-in and each person seeing only their own cameras (app/users.py, app/web.py).

Runs without Arlo, Google or a network: a pretend recorder with three cameras, and Google's
token endpoint answered in-process.  python tests/people_test.py
"""
import base64
import json
import os
import sys
import tempfile
import time
from urllib.parse import parse_qs, urlsplit

D = tempfile.mkdtemp()
os.environ.update(DATA_DIR=D)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
os.makedirs(os.path.join(D, "clips"), exist_ok=True)

from app import events, google_auth, store, users, web  # noqa: E402

store.init()
web.init_secret()
web.app.config["TESTING"] = True

CAMS = [{"id": "A1", "name": "Barb porch", "brand": "arlo", "account": "barb@home.com"},
        {"id": "B1", "name": "Bob garage", "brand": "arlo", "account": "bob@gmail.com"},
        {"id": "ip-10-0-0-5-1", "name": "Bob yard", "brand": "ipcam", "account": "Laview & IP cameras"}]


class FakeBlink:
    def accounts_view(self):
        return []


class FakeIP:
    def cameras_view(self):
        return [{**c, "online": True, "model": "LV-PB932F4", "detail": {}, "last_motion": None}
                for c in CAMS if c["brand"] == "ipcam"]

    def any(self):
        return True

    def connected(self):
        return True


class FakeRecorder:
    status = {"connected": True}
    blink = FakeBlink()
    ipcams = FakeIP()
    added = []

    def cameras_view(self):
        return CAMS

    def accounts_view(self):
        return [{"id": "main", "username": "barb@home.com", "connected": True, "waiting_for_code": False,
                 "signing_in": False, "error": None, "cameras": ["Barb porch"], "shared": []},
                {"id": "b0b", "username": "bob@gmail.com", "connected": True, "waiting_for_code": False,
                 "signing_in": False, "error": None, "cameras": ["Bob garage"], "shared": []}]

    def modes(self):
        return {"barb@home.com: Home": "armed"}

    def record_now(self, cam_id):
        return True

    def add(self, user, pw):
        self.added.append(user)


web.recorder = FakeRecorder()
# arlo_login.accounts() is what "already on BCN" checks against
store.set_setting("arlo_accounts", [{"id": "main", "username": "barb@home.com", "password": "x"},
                                    {"id": "b0b", "username": "bob@gmail.com", "password": "y"}])
now = int(time.time() * 1000)
for i, c in enumerate(CAMS):
    store.add({"key": f"{c['id']}:{now - i}", "camera_id": c["id"], "camera_name": c["name"], "created_ms": now - i,
               "video_path": f"2026/{c['id']}.mp4", "thumb_path": f"2026/{c['id']}.jpg", "size_bytes": 10})

fails = []


def check(what, ok):
    print(("ok   " if ok else "FAIL ") + what)
    if not ok:
        fails.append(what)


HOME = {"REMOTE_ADDR": "192.168.5.20"}
AWAY = {"HTTP_CF_CONNECTING_IP": "8.8.8.8"}


def client():
    return web.app.test_client()


# --- 1. before Google sign-in is set up: exactly as before ---------------------------------
c = client()
r = c.get("/api/status", environ_base=HOME)
check("off: home sees everything", r.status_code == 200 and len(r.json["cameras"]) == 3)
r = c.get("/", environ_base=AWAY)
check("off: away from home has to sign in", r.status_code == 302 and "/login" in r.location)

# --- 2. turn it on ---------------------------------------------------------------------------
users.save_google(client_id="cid.apps.googleusercontent.com", client_secret="sec", owner_email="Barb@Home.com",
                  base_url="https://bcn.tapper.co")
check("on once all three are filled in", users.enabled())
r = client().get("/api/status", environ_base=HOME)
check("on: home network still trusted by default", r.status_code == 200 and len(r.json["cameras"]) == 3)
r = client().get("/", environ_base=AWAY)
check("on: away from home goes to sign-in", r.status_code == 302 and "/login" in r.location)
r = client().get("/login", environ_base=AWAY)
check("sign-in page has the Google button", b"Sign in with Google" in r.data)

# Google's token endpoint, answered here
_next_claims = {}


class FakeResp:
    status_code = 200

    def __init__(self, body):
        self.body = body

    def json(self):
        return self.body


def fake_post(url, timeout=None, data=None):
    assert url == google_auth.TOKEN_URL and data["client_secret"] == "sec" and data["code"] == "the-code"
    claims = {"iss": "https://accounts.google.com", "aud": "cid.apps.googleusercontent.com",
              "exp": time.time() + 300, "email_verified": True, **_next_claims}
    tok = "h." + base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=") + ".sig"
    return FakeResp({"id_token": tok})


google_auth.requests.post = fake_post


def google_sign_in(email, name, tamper_nonce=False):
    c = client()
    r = c.get("/auth/google?next=/connect", base_url="https://bcn.tapper.co", environ_base=AWAY)
    assert r.status_code == 302 and r.location.startswith(google_auth.AUTH_URL), r.location
    q = parse_qs(urlsplit(r.location).query)
    assert q["redirect_uri"] == ["https://bcn.tapper.co/auth/google/callback"]
    _next_claims.clear()
    _next_claims.update(email=email, name=name, nonce="wrong" if tamper_nonce else q["nonce"][0])
    r = c.get(f"/auth/google/callback?state={q['state'][0]}&code=the-code", base_url="https://bcn.tapper.co",
              environ_base=AWAY)
    return c, r


r = client().get("/auth/google", base_url="http://192.168.5.207:8090", environ_base=HOME)
check("Google sign-in starts on the public address", r.status_code == 302 and r.location.startswith("https://bcn.tapper.co/auth/google"))

_, r = google_sign_in("bob@gmail.com", "Bob", tamper_nonce=True)
check("a sign-in that didn't start here is refused", r.status_code == 400)

barb, r = google_sign_in("barb@home.com", "Barb")
check("owner signs in and goes where she was headed", r.status_code == 302 and r.location.endswith("/connect"))
r = barb.get("/api/status", base_url="https://bcn.tapper.co", environ_base=AWAY)
check("owner sees every camera", r.status_code == 200 and len(r.json["cameras"]) == 3)
check("owner sees the modes", bool(r.json["modes"]))

bob, r = google_sign_in("bob@gmail.com", "Bob")
check("someone new waits for approval", r.status_code == 302 and r.location.endswith("/waiting"))
r = bob.get("/api/status", base_url="https://bcn.tapper.co", environ_base=AWAY)
check("waiting: no cameras", r.status_code == 403)
r = bob.get("/waiting", base_url="https://bcn.tapper.co", environ_base=AWAY)
check("waiting page opens", r.status_code == 200 and b"bob@gmail.com" in r.data)
r = bob.get("/people", base_url="https://bcn.tapper.co", environ_base=AWAY)
check("waiting: no People page", r.status_code == 302 and "/waiting" in r.location)

bob_uid, _ = users.by_email("bob@gmail.com")
r = barb.post(f"/people/{bob_uid}/approve", base_url="https://bcn.tapper.co", environ_base=AWAY)
check("owner lets Bob in", r.status_code == 302 and users.get(bob_uid)["approved"])
# Approving changed Bob's stamp: his old cookie is signed out, he signs in again
r = bob.get("/api/status", base_url="https://bcn.tapper.co", environ_base=AWAY)
check("approval signs the old cookie out", r.status_code == 401)
bob, r = google_sign_in("bob@gmail.com", "Bob")

r = barb.get("/people", base_url="https://bcn.tapper.co", environ_base=AWAY)
check("People page shows Bob", r.status_code == 200 and b"bob@gmail.com" in r.data)
r = barb.get("/connect", base_url="https://bcn.tapper.co", environ_base=AWAY)
check("owner's Accounts page lists both Arlo logins", r.status_code == 200 and b"bob@gmail.com" in r.data)
r = bob.get("/connect", base_url="https://bcn.tapper.co", environ_base=AWAY)
check("Bob's Accounts page opens", r.status_code == 200)

# --- 3. Bob sees only what he added ------------------------------------------------------------
users.set_owner(users.account_key("arlo", "bob@gmail.com"), bob_uid)
users.set_owner(users.ipcam_key("ip-10-0-0-5-1"), bob_uid)


def bget(path):
    return bob.get(path, base_url="https://bcn.tapper.co", environ_base=AWAY)


r = bget("/api/status")
ids = sorted(c["id"] for c in r.json["cameras"])
check("Bob sees only his cameras", ids == ["B1", "ip-10-0-0-5-1"])
check("Bob doesn't see the household's modes", r.json["modes"] == {})
check("Bob's clip counts are his own", r.json["stats"]["count"] == 2)
check("Bob's recordings list is his", sorted(c["camera_id"] for c in r.json["clip_cameras"]) == ["B1", "ip-10-0-0-5-1"])
r = bget("/api/clips")
check("Bob's clips only", sorted(x["camera_id"] for x in r.json) == ["B1", "ip-10-0-0-5-1"])
r = bget("/api/clips?camera=A1")
check("asking for Barb's camera by name gives nothing", r.json == [])
r = bget("/api/cameras")
check("GarysTV-style camera list is Bob's", sorted(c["id"] for c in r.json["cameras"]) == ["B1", "ip-10-0-0-5-1"])
check("account list is Bob's", [a["username"] for a in r.json["accounts"] if a["brand"] == "arlo"] == ["bob@gmail.com"])
r = bget(f"/clip/A1:{now}")
check("Barb's clip page: not found", r.status_code == 404)
r = bget("/media/2026/A1.mp4")
check("Barb's video file: not found", r.status_code == 404)
r = bget("/media/2026/A1.jpg")
check("Barb's picture: not found", r.status_code == 404)
r = bob.delete(f"/api/clip/A1:{now}", base_url="https://bcn.tapper.co", environ_base=AWAY)
check("Bob can't delete Barb's clip", r.json["ok"] is False and store.get(f"A1:{now}"))
r = bob.post("/api/clips/bulk", json={"action": "delete", "keys": [f"A1:{now}"]}, base_url="https://bcn.tapper.co", environ_base=AWAY)
check("…not even several at once", r.json["count"] == 0 and store.get(f"A1:{now}"))
r = bob.post("/api/camera/A1/record", base_url="https://bcn.tapper.co", environ_base=AWAY)
check("Bob can't record Barb's camera", r.json["ok"] is False)
r = bob.post("/api/camera/B1/record", base_url="https://bcn.tapper.co", environ_base=AWAY)
check("Bob can record his own", r.json["ok"] is True)
r = bget("/api/camera/A1/live")
check("no live view of Barb's camera", r.status_code == 404)
r = bget("/live/A1-123.ts")
check("no motion pop-up stream of Barb's camera", r.status_code == 404)
r = bget("/connect")
check("Bob's Accounts page lists only his login", b"bob@gmail.com" in r.data and b"barb@home.com" not in r.data)
check("Bob's menu has no Alerts or People", b'href="/people"' not in r.data and b'href="/settings"' not in r.data)
r = bget("/connect/ip")
check("Bob's IP cameras page opens", r.status_code == 200)
r = bget("/settings")
check("Alerts page: owner only", r.status_code == 302)
r = bget("/people")
check("People page: owner only", r.status_code == 302)
r = bob.post("/api/storage", json={"max_gb": 1}, base_url="https://bcn.tapper.co", environ_base=AWAY)
check("storage rules: owner only", r.status_code == 403)
r = bob.post("/connect", data={"username": "barb@home.com", "password": "guess"}, base_url="https://bcn.tapper.co", environ_base=AWAY)
check("Bob can't take over Barb's Arlo login", "barb@home.com" not in FakeRecorder.added)
r = bob.post("/connect", data={"username": "bob2@gmail.com", "password": "pw"}, base_url="https://bcn.tapper.co", environ_base=AWAY)
check("Bob adds his own new account and owns it", "bob2@gmail.com" in FakeRecorder.added
      and users.owner_of(users.account_key("arlo", "bob2@gmail.com")) == bob_uid)
r = bob.post("/connect/main/signout", base_url="https://bcn.tapper.co", environ_base=AWAY)
check("Bob can't sign Barb's account out", r.status_code == 404)

# --- 4. alerts, GarysTV pop-ups and the phone event stream -------------------------------------
check("household alerts skip Bob's camera", users.private_camera("B1"))
check("household alerts still cover Barb's", not users.private_camera("A1"))
viewer = {"admin": False, "uid": bob_uid}
allow = lambda ev: ev.get("camera_id") in users.allowed_cameras(viewer, CAMS)  # noqa: E731
gen = events.stream(0, heartbeat=0.05, allow=allow)
next(gen)
events.publish("motion", camera="Barb porch", camera_id="A1")
events.publish("motion", camera="Bob garage", camera_id="B1")
got = next(gen)
check("Bob's phone gets his camera's motion, not Barb's", '"B1"' in got and '"A1"' not in got)
gen.close()

# --- 5. Make Bob an admin; then remove him -------------------------------------------------------
barb.post(f"/people/{bob_uid}/admin", base_url="https://bcn.tapper.co", environ_base=AWAY)
bob, _ = google_sign_in("bob@gmail.com", "Bob")
r = bget("/api/status")
check("an admin sees everything", len(r.json["cameras"]) == 3)
check("admins' cameras alert the household again", not users.private_camera("B1"))
barb.post(f"/people/{bob_uid}/remove", base_url="https://bcn.tapper.co", environ_base=AWAY)
r = bget("/api/status")
check("removed: signed out", r.status_code == 401)
check("removed: his accounts go back to the owner", users.owner_of(users.account_key("arlo", "bob@gmail.com")) is None)

# --- 6. home network no longer trusted; the escape hatch ---------------------------------------
users.save_google(home_trusted=False)
r = client().get("/", environ_base=HOME)
check("home not trusted: sign in at home too", r.status_code == 302 and "/login" in r.location)
os.environ["BCN_GOOGLE_SIGNIN"] = "off"
r = client().get("/api/status", environ_base=HOME)
check("BCN_GOOGLE_SIGNIN=off opens it at home again", r.status_code == 200)

print()
print(f"{len(fails)} failed" if fails else "all passed")
sys.exit(1 if fails else 0)
