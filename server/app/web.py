"""Small web page for browsing, playing, starring and deleting saved clips."""
import functools
import logging
import os
import re
import time
from datetime import datetime

import secrets
from datetime import timedelta
from urllib.parse import urlsplit

from flask import (Flask, Response, abort, flash, g, jsonify, redirect, render_template, request,
                   send_from_directory, session, url_for)

from . import arlo_login, config, events, garystv, google_auth, ipcam, liveview, notify, remote, store, updater, users

app = Flask(__name__, template_folder="templates")
log_web = logging.getLogger("web")


def init_secret():
    key = store.get_setting("web_secret")
    if not key:
        key = secrets.token_hex(32)
        store.set_setting("web_secret", key)
    app.secret_key = key
recorder = None  # set by main.py
app.permanent_session_lifetime = timedelta(days=365)
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax")

# --- who is looking: Google sign-in, the home network, or the away-from-home password -------
# Endpoints anyone may open (the sign-in pages themselves).
OPEN = {"login", "login_submit", "static", "manifest", "auth_google", "auth_google_callback", "waiting",
        "logout", "health"}
ADMIN = {"admin": True, "uid": None, "email": None, "name": None}     # sees and changes everything
API_PREFIXES = ("/api/", "/media/", "/live/", "/bcn.apk", "/ban.apk", "/photo", "/cameras.json", "/health")


def _signed_in_user():
    """The Google-signed-in person on this browser or phone, if their sign-in is still good."""
    uid = session.get("uid")
    u = users.get(uid)
    if not u or session.get("ustamp") != u.get("stamp"):
        return None, None
    return uid, u


def _password_ok():
    """The away-from-home password: the sign-in page's cookie, or HTTP Basic from an app (GarysTV
    sends https://name:password@bcn.tapper.co). Returns True, False, or a 401 Response."""
    if not remote.enabled():
        return False
    if session.get("remote") == remote.stamp():
        return True
    auth = request.authorization
    if auth and auth.type == "basic":
        ip = remote.client_ip(request)
        if remote.check_basic(ip, auth.username, auth.password):
            return True
        return Response("Too many wrong tries. Wait 15 minutes and try again." if remote.locked(ip)
                        else "That name and password don't match the away-from-home sign-in.", 401)
    return False


def _sign_in_first():
    wants_page = request.method == "GET" and not request.path.startswith(API_PREFIXES)
    if wants_page:
        return redirect(url_for("login", next=request.full_path if request.query_string else request.path))
    return Response("Sign in first", 401)


@app.before_request
def _who():
    g.viewer = ADMIN
    if request.endpoint in OPEN:
        if users.enabled():
            uid, u = _signed_in_user()
            if u:
                g.viewer = _viewer(uid, u)
        return None
    if not users.enabled():
        # Before Google sign-in is set up: open at home, the away-from-home password outside.
        if not remote.is_remote(request):
            return None
        ok = _password_ok()
        return ok if isinstance(ok, Response) else (None if ok else _sign_in_first())
    uid, u = _signed_in_user()
    if u:
        if not u.get("approved"):
            return redirect(url_for("waiting")) if not request.path.startswith(API_PREFIXES) \
                else Response("Waiting for the owner to let you in", 403)
        g.viewer = _viewer(uid, u)
        return None
    if not remote.is_remote(request) and users.home_trusted():
        return None
    ok = _password_ok()
    if isinstance(ok, Response):
        return ok
    return None if ok else _sign_in_first()


def _viewer(uid, u):
    return {"admin": users.is_admin(u), "uid": uid, "email": u["email"], "name": u.get("name"),
            "role": u.get("role")}


def _admin_only(fn):
    """Alerts, storage, moving the server, the people page: admins only."""
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        if not g.viewer["admin"]:
            if request.method == "GET" and not request.path.startswith(API_PREFIXES):
                flash("Only the owner can open that page.", "info")
                return redirect(url_for("index"))
            return Response("Only the owner can do that", 403)
        return fn(*a, **kw)
    return wrapper


@app.get("/login")
def login():
    nxt = request.args.get("next") or "/"
    return render_template("login.html", next=nxt if nxt.startswith("/") else "/", enabled=remote.enabled(),
                           google=users.enabled(), error=None, user="")


@app.post("/login")
def login_submit():
    nxt = request.form.get("next") or "/"
    nxt = nxt if nxt.startswith("/") and not nxt.startswith("//") else "/"
    ip, user = remote.client_ip(request), request.form.get("user", "")
    if remote.check(ip, user, request.form.get("password", "")):
        session.permanent = True
        session["remote"] = remote.stamp()
        return redirect(nxt)
    error = ("Too many wrong tries. Wait 15 minutes and try again." if remote.locked(ip)
             else "That name and password don't match.")
    return render_template("login.html", next=nxt, enabled=remote.enabled(), google=users.enabled(),
                           error=error, user=user), 401


@app.get("/logout")
def logout():
    for k in ("remote", "uid", "ustamp"):
        session.pop(k, None)
    return redirect(url_for("login"))


def _safe_next(nxt):
    return nxt if nxt and nxt.startswith("/") and not nxt.startswith(("//", "/\\")) else "/"


@app.get("/auth/google")
def auth_google():
    """Start Sign in with Google. Always on the public address Google knows (bcn.tapper.co), so
    the sign-in cookie lands where Google sends the browser back to."""
    if not users.enabled():
        return redirect(url_for("login"))
    base = users.google()["base_url"]
    nxt = _safe_next(request.args.get("next"))
    if urlsplit(base).netloc.lower() != request.host.lower():
        return redirect(base + url_for("auth_google", next=nxt))
    session["g_state"], session["g_nonce"], session["g_next"] = secrets.token_urlsafe(24), secrets.token_urlsafe(24), nxt
    return redirect(google_auth.auth_url(session["g_state"], session["g_nonce"]))


@app.get("/auth/google/callback")
def auth_google_callback():
    def fail(msg):
        return render_template("login.html", next="/", enabled=remote.enabled(), google=users.enabled(),
                               error=msg, user=""), 400
    state, nonce = session.pop("g_state", None), session.pop("g_nonce", None)
    nxt = _safe_next(session.pop("g_next", "/"))
    if request.args.get("error"):
        return fail("Google sign-in was cancelled.")
    if not state or request.args.get("state") != state:
        return fail("That sign-in didn't start here (or took too long). Try again.")
    try:
        who = google_auth.exchange(request.args.get("code", ""), nonce)
    except google_auth.SignInError as e:
        return fail(str(e))
    uid, u = users.sign_in(who["email"], who["name"])
    session.permanent = True
    session["uid"], session["ustamp"] = uid, u["stamp"]
    log_web.info("signed in with Google: %s (%s)", u["email"], u["role"] if u["approved"] else "waiting")
    return redirect(nxt if u["approved"] else url_for("waiting"))


@app.get("/waiting")
def waiting():
    uid, u = _signed_in_user() if users.enabled() else (None, None)
    if not u:
        return redirect(url_for("login"))
    if u.get("approved"):
        return redirect(url_for("index"))
    return render_template("waiting.html", u=u)


def _auth(fn):
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        if config.WEB_USER:
            auth = request.authorization
            if not auth or auth.username != config.WEB_USER or auth.password != config.WEB_PASSWORD:
                return Response("Login required", 401, {"WWW-Authenticate": 'Basic realm="Camera Keeper"'})
        return fn(*a, **kw)
    return wrapper


@app.template_filter("when")
def _when(ms):
    return datetime.fromtimestamp(ms / 1000).strftime("%a %b %d, %I:%M:%S %p")


@app.template_filter("ago")
def _ago(ms):
    if not ms:
        return "never"
    s = int(time.time() - ms / 1000)
    for unit, n in (("d", 86400), ("h", 3600), ("m", 60)):
        if s >= n:
            return f"{s // n}{unit} ago"
    return "just now"


@app.template_filter("size")
def _size(b):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if b < 1024:
            return f"{b:.0f} {unit}" if unit in ("B", "KB") else f"{b:.1f} {unit}"
        b /= 1024
    return f"{b:.1f} PB"


@app.route("/")
@_auth
def index():
    """BCN: cameras and recordings. The page fills itself in from the /api/ calls below."""
    return render_template("index.html", status=recorder.status if recorder else {},
                           in_app=any(a in (request.headers.get("User-Agent") or "") for a in ("BCN-Android", "BAN-Android")))


@app.post("/camera/<cam_id>/record")
@_auth
def record_now(cam_id):
    if recorder and _can_cam(cam_id) and recorder.record_now(cam_id):
        flash("Recording a test clip. It shows up here in about 30–40 seconds.", "ok")
    else:
        flash("That camera is already recording, or isn't available.", "info")
    return redirect(url_for("index"))


@app.get("/cameras.json")
@_auth
def cameras_json():
    return jsonify(_visible() if recorder and hasattr(recorder, "cameras_view") else [])


# --- each person sees only their own cameras ---------------------------------------
def _all_cams():
    return recorder.cameras_view() if recorder else []


def _is_tv():
    """GarysTV (the household's TVs) says so in its User-Agent."""
    return (request.headers.get("User-Agent") or "").startswith("GarysTV")


def _allowed():
    """Camera ids the viewer may see, or None for everything (admins, and before sign-in is on).

    The household's TVs (GarysTV) don't show cameras that belong to someone who isn't an admin,
    like their motion pop-ups (garystv.motion_started): not in the list, the clips or live view."""
    cams = _all_cams()
    a = users.allowed_cameras(g.viewer, cams)
    if _is_tv():
        a = {c["id"] for c in cams if not users.private_camera(c["id"])} if a is None \
            else {i for i in a if not users.private_camera(i)}
    return a


def _slash(row):
    """A clip row with "/" in its paths: clips saved on Windows before 1.0.14 used "\\", which
    GarysTV doesn't accept in a /media/ link (and older Camera Keepers didn't serve)."""
    for k in ("video_path", "thumb_path"):
        if row.get(k):
            row[k] = row[k].replace("\\", "/")
    return row


def _visible(cams=None):
    cams = _all_cams() if cams is None else cams
    a = _allowed()
    return cams if a is None else [c for c in cams if c["id"] in a]


def _can_cam(cam_id):
    a = _allowed()
    return a is None or cam_id in a


def _can_clip(key):
    row = store.get(key)
    return bool(row) and _can_cam(row["camera_id"])


def _mine(keys):
    """The clip keys from this list the viewer may change."""
    a = _allowed()
    if a is None:
        return keys
    return [k for k in keys if (r := store.get(k)) and r["camera_id"] in a]


def _query(camera_id=None, day=None, starred=False, limit=200, offset=0, camera_ids=None):
    """store.query, limited to the viewer's cameras."""
    a = _allowed()
    if a is not None:
        camera_ids = a if camera_ids is None else [c for c in camera_ids if c in a]
        if not camera_ids or (camera_id and camera_id not in camera_ids):
            return []
    return store.query(camera_id, day, starred, limit=limit, offset=offset, camera_ids=camera_ids)


def _own_account(brand, username):
    return users.can_use(users.account_key(brand, username), g.viewer)


# --- GarysTV (motion clips pop up on the TV) ---------------------------------
@app.get("/api/cameras")
@_auth
def api_cameras():
    """The cameras, for GarysTV's Settings > Cameras."""
    s = recorder.status if recorder else {}
    cams = _visible()
    return jsonify({"connected": bool(s.get("connected")),
                    "modes": recorder.modes() if recorder and g.viewer["admin"] and not _is_tv() else {},
                    "cameras": [_tv_camera(c) for c in cams],
                    "accounts": _accounts_list()})


def _tv_camera(c):
    """One camera for GarysTV: what Settings > Cameras and Home > Cameras show.

    live: it can be watched live now (not Blink, not offline). thumb: the newest clip's picture,
    shown while the live picture starts. viewing: someone is watching it live already."""
    brand = c.get("brand") or ("blink" if str(c.get("id")).startswith("blink-") else "arlo")
    rows = store.query(c["id"], limit=1)
    r = _slash(rows[0]) if rows else None
    return ({k: c.get(k) for k in ("id", "name", "model", "battery", "unavailable", "last_motion", "last_clip",
                                   "account", "motion", "capturing")}
            | {"brand": brand,
               "live": brand != "blink" and not c.get("no_live") and not c.get("unavailable"),
               "viewing": liveview.viewing(c["id"]),
               "thumb": f"/media/{r['thumb_path']}" if r and r.get("thumb_path") else None,
               "last_clip_ms": r["created_ms"] if r else None})


def _accounts_list():
    """The viewer's Arlo and Blink accounts, for GarysTV's filter by account."""
    if not recorder:
        return []
    ipcams = recorder.ipcams.any() and (g.viewer["admin"] or any((c.get("brand") == ipcam.BRAND) for c in _visible()))
    return ([{"username": a["username"], "brand": "arlo", "connected": a["connected"],
              "waiting_for_code": a["waiting_for_code"]} for a in recorder.accounts_view()
             if _own_account("arlo", a["username"])]
            + [{"username": a["username"], "brand": "blink", "connected": a["connected"],
                "waiting_for_code": a["waiting_for_code"]} for a in recorder.blink.accounts_view()
               if _own_account("blink", a["username"])]
            + ([{"username": ipcam.ACCOUNT, "brand": ipcam.BRAND, "connected": recorder.ipcams.connected(),
                 "waiting_for_code": False}] if ipcams else []))


def _account_cameras(account):
    """The camera ids on one account: "arlo:email", "blink:email", or just the email."""
    brand, user = account.split(":", 1) if account.startswith(("arlo:", "blink:", "ipcam:")) else ("", account)
    cams = _visible()
    return [c["id"] for c in cams if (c.get("account") or "").lower() == user.lower()
            and (not brand or (c.get("brand") or "arlo") == brand)]


def _subscriber_url():
    b = request.get_json(silent=True) or {}
    if b.get("url"):
        return str(b["url"])
    host = request.remote_addr or "127.0.0.1"
    if ":" in host:
        host = f"[{host}]"
    return f"http://{host}:{int(b.get('port') or 5480)}"


@app.post("/api/subscribe")
@_auth
@_admin_only
def api_subscribe():
    if remote.is_remote(request) and not (request.get_json(silent=True) or {}).get("url"):
        return jsonify({"ok": False, "error": "Connected through bcn.tapper.co, which works for clips, but motion pop-ups "
                        "can't come back that way. For pop-ups use Camera Keeper's home or VPN address (like http://100.64.0.2:8090)."}), 400
    url = _subscriber_url()
    garystv.subscribe(url)
    return jsonify({"ok": True, "url": url})


@app.delete("/api/subscribe")
@_auth
@_admin_only
def api_unsubscribe():
    garystv.unsubscribe(_subscriber_url())
    return jsonify({"ok": True})


@app.post("/api/test-motion/<cam_id>")
@_auth
def api_test_motion(cam_id):
    ok = bool(recorder and _can_cam(cam_id) and recorder.test_motion(cam_id))
    return jsonify({"ok": ok, "error": None if ok else "That camera is already recording, or isn't available."})


@app.get("/live/<live_id>.ts")
@_auth
def live_clip(live_id):
    """A motion clip while it records: sent as it grows, ending when the recording does.

    Can be asked for before the first picture has arrived; it waits for it.
    """
    if not re.fullmatch(r"[A-Za-z0-9_-]+", live_id) or not recorder or not _can_cam(live_id.rsplit("-", 1)[0]):
        abort(404)
    path, recording = recorder.capture.live_file(live_id)
    if not path:
        abort(404)

    def follow():
        pos, idle_since = 0, time.time()
        while True:
            try:
                if os.path.getsize(path) < pos:
                    pos = 0  # ffmpeg had to start over (camera still waking up)
                with open(path, "rb") as f:
                    f.seek(pos)
                    chunk = f.read(256 * 1024)
            except OSError:
                chunk = b""
            if chunk:
                pos += len(chunk)
                idle_since = time.time()
                yield chunk
                continue
            _, still = recorder.capture.live_file(live_id)
            # The camera can take ~15s to send its first picture; after that, gaps are short.
            if not still or time.time() - idle_since > (40 if pos == 0 else 15):
                return
            time.sleep(0.2)

    return Response(follow(), mimetype="video/mp2t", headers={"Cache-Control": "no-store"})


@app.context_processor
def _globals():
    s = notify.current()
    return {"alerts_need_setup": not active_numbers_or_channel(s), "notify_settings": s,
            "pretty_number": notify.pretty_number, "viewer": getattr(g, "viewer", ADMIN),
            "people_on": users.enabled()}


def active_numbers_or_channel(s):
    return bool(notify.active_numbers(s) or notify.has_any_channel(s))


# --- Arlo accounts and sign-in ------------------------------------------------
def _connect_state(accs=None):
    """Where each account's sign-in is. The Accounts page reloads itself when this changes."""
    if accs is None:
        accs = (_my_arlo() + _my_blink()) if recorder else []
    return [{k: a.get(k) for k in ("id", "connected", "signing_in", "waiting_for_code", "error")} for a in accs]


def _my_arlo():
    return [a for a in recorder.accounts_view() if _own_account("arlo", a["username"])] if recorder else []


def _my_blink():
    return [a for a in recorder.blink.accounts_view() if _own_account("blink", a["username"])] if recorder else []


def _may_add(brand, username, saved):
    """None if the viewer may add (or re-enter the password of) this account, else why not."""
    key = users.account_key(brand, username)
    exists = any(a["username"].lower() == username.strip().lower() for a in saved)
    if users.taken_by_other(key, g.viewer, exists):
        return f"{username} is already on BCN, added by someone else."
    return None


@app.get("/connect")
@_auth
def connect():
    from . import blink
    accounts = _my_arlo()
    blink_accounts = _my_blink()
    return render_template("connect.html", accounts=accounts, blink_accounts=blink_accounts,
                           state=_connect_state(accounts + blink_accounts),
                           blink_available=blink.AVAILABLE, blink_poll=blink.POLL_SECONDS)


@app.post("/connect")
@_auth
def connect_submit():
    """Add an account, or retry one with a new password."""
    user = request.form.get("username", "").strip()
    pw = request.form.get("password", "")
    if not user or not pw:
        flash("Enter both the Arlo email and password.", "error")
        return redirect(url_for("connect"))
    why = _may_add("arlo", user, arlo_login.accounts())
    if why:
        flash(why, "error")
        return redirect(url_for("connect"))
    recorder.add(user, pw)
    users.claim(users.account_key("arlo", user), g.viewer)
    flash(f"Signing in to Arlo as {user}…", "ok")
    return redirect(url_for("connect"))


@app.post("/connect/<aid>/code")
@_auth
def connect_code(aid):
    a = arlo_login.get(aid)
    if not a or not _own_account("arlo", a["username"]):
        abort(404)
    arlo_login.submit_code(aid, request.form.get("code", ""))
    flash("Code sent to Arlo.", "ok")
    return redirect(url_for("connect"))


@app.post("/connect/<aid>/signout")
@_auth
def connect_signout(aid):
    a = arlo_login.get(aid)
    if not a or not _own_account("arlo", a["username"]):
        abort(404)
    recorder.sign_out(aid)
    flash(f"Signed out of {a['username'] if a else 'that account'}. Its saved clips are still here.", "ok")
    return redirect(url_for("connect"))


@app.get("/connect/state")
@_auth
def connect_state():
    return jsonify(_connect_state())


@app.post("/connect/blink")
@_auth
def connect_blink():
    user = request.form.get("username", "").strip()
    pw = request.form.get("password", "")
    if not user or not pw:
        flash("Enter both the Blink email and password.", "error")
        return redirect(url_for("connect") + "#blink")
    from . import blink
    why = _may_add("blink", user, blink.accounts())
    if why:
        flash(why, "error")
        return redirect(url_for("connect") + "#blink")
    try:
        recorder.blink.add(user, pw)
    except RuntimeError as e:
        flash(str(e), "error")
        return redirect(url_for("connect") + "#blink")
    users.claim(users.account_key("blink", user), g.viewer)
    flash(f"Signing in to Blink as {user}…", "ok")
    return redirect(url_for("connect") + "#blink")


@app.post("/connect/blink/<aid>/code")
@_auth
def connect_blink_code(aid):
    from . import blink
    a = blink.get_account(aid)
    if not a or not _own_account("blink", a["username"]):
        abort(404)
    recorder.blink.submit_code(aid, request.form.get("code", ""))
    flash("Code sent to Blink.", "ok")
    return redirect(url_for("connect") + "#blink")


@app.post("/connect/blink/<aid>/signout")
@_auth
def connect_blink_signout(aid):
    from . import blink
    a = blink.get_account(aid)
    if not a or not _own_account("blink", a["username"]):
        abort(404)
    recorder.blink.sign_out(aid)
    flash(f"Signed out of Blink ({a['username'] if a else 'that account'}). Its saved clips are still here.", "ok")
    return redirect(url_for("connect") + "#blink")


# --- Laview and other IP cameras (RTSP / ONVIF) -------------------------------
@app.get("/connect/ip")
@_auth
def ipcams_page():
    return render_template("ipcams.html", cams=_my_ipcams(),
                           saved={c["id"]: c for c in ipcam.cameras() if _own_ipcam(c["id"])})


def _own_ipcam(cid):
    return users.can_use(users.ipcam_key(cid), g.viewer)


def _my_ipcams():
    return [c for c in recorder.ipcams.cameras_view() if _own_ipcam(c["id"])] if recorder else []


@app.get("/connect/ip/state")
@_auth
def ipcams_state():
    return jsonify(_my_ipcams())


@app.post("/connect/ip")
@_auth
def ipcams_add():
    # The same address (and channel) as a camera someone else added would replace theirs.
    host, _ = ipcam.clean_host(request.form.get("host"))
    host = host or urlsplit((request.form.get("rtsp_url") or "").strip()).hostname or ""
    clash = [c for c in ipcam.cameras() if host and c.get("host") == host and not _own_ipcam(c["id"])]
    if clash:
        flash(f"{host} is already on BCN, added by someone else.", "error")
        return redirect(url_for("ipcams_page"))
    try:
        ids = recorder.ipcams.add(request.form)
    except ValueError as e:
        flash(str(e), "error")
        return redirect(url_for("ipcams_page"))
    for cid in ids:
        users.claim(users.ipcam_key(cid), g.viewer)
    flash(f"Added {len(ids)} camera{'' if len(ids) == 1 else 's'}. BCN is looking for the video now; "
          "this page updates on its own (finding an unknown camera can take a minute).", "ok")
    return redirect(url_for("ipcams_page"))


@app.post("/connect/ip/<cid>/<action>")
@_auth
def ipcams_action(cid, action):
    if not ipcam.get(cid) or not _own_ipcam(cid):
        abort(404)
    if action == "remove":
        c = ipcam.get(cid)
        recorder.ipcams.remove(cid)
        flash(f"Removed {c['name'] if c else 'that camera'}. Its saved clips stay.", "ok")
    elif action == "retry":
        recorder.ipcams.restart(cid)
        flash("Looking at that camera again.", "ok")
    elif action == "rename":
        ipcam.rename(cid, request.form.get("name", ""))
        recorder.ipcams.restart(cid)
    else:
        abort(404)
    return redirect(url_for("ipcams_page"))


@app.post("/connect/ip/find")
@_auth
@_admin_only
def ipcams_find():
    """Cameras on this network that answer ONVIF or Hikvision/Laview SADP discovery."""
    return jsonify(ipcam.discover())


# --- People: Google sign-in, who may get in, and what they added ---------------------------
def _added_by():
    """uid -> the accounts and IP cameras that person added."""
    out = {}
    for key, uid in users.owners().items():
        brand, _, name = key.partition(":")
        if brand == "ipcam":
            c = ipcam.get(name)
            name = c["name"] if c else name
        out.setdefault(uid, []).append({"brand": brand, "name": name})
    return out


@app.get("/people")
@_auth
@_admin_only
def people():
    gs = users.google()
    people = sorted(users.users().items(), key=lambda kv: (kv[1].get("approved", False), kv[1].get("created", 0)))
    return render_template("people.html", g_settings=gs, on=users.enabled(), people=people, added=_added_by(),
                           redirect_uri=users.redirect_uri(), has_secret=bool(gs["client_secret"]))


@app.post("/people/google")
@_auth
@_admin_only
def people_google():
    f = request.form
    if f.get("action") == "off":
        users.turn_off()
        flash("Google sign-in is off. BCN works like before (home network, and the away-from-home password).", "ok")
        return redirect(url_for("people"))
    email = f.get("owner_email", "").strip()
    if f.get("action") == "home":
        users.save_google(home_trusted=f.get("home_trusted") == "1")
        flash("Saved.", "ok")
        return redirect(url_for("people") + "#home")
    if email and "@" not in email:
        flash("The owner's Google account is an email address.", "error")
        return redirect(url_for("people"))
    base = f.get("base_url", "").strip()
    if base and not base.startswith("https://"):
        flash("The public address starts with https:// (Google only sends people back to https).", "error")
        return redirect(url_for("people"))
    users.save_google(client_id=f.get("client_id", ""), client_secret=f.get("client_secret", ""),
                      owner_email=email, base_url=base or None)
    flash("Saved. Google sign-in is on: sign in with the owner's Google account to finish."
          if users.enabled() else "Saved. Fill in all three to turn Google sign-in on.", "ok")
    return redirect(url_for("people"))


@app.post("/people/<uid>/<action>")
@_auth
@_admin_only
def people_action(uid, action):
    u = users.get(uid)
    if not u:
        abort(404)
    if action == "approve":
        users.update(uid, approved=True)
        flash(f"{u['email']} can sign in now and add their own cameras.", "ok")
    elif action == "block":
        users.update(uid, approved=False)
        flash(f"{u['email']} can't get in any more.", "ok")
    elif action in ("admin", "user"):
        users.update(uid, role=action)
        flash(f"{u['email']} {'sees every camera now' if action == 'admin' else 'sees only their own cameras now'}.", "ok")
    elif action == "remove":
        users.remove(uid)
        flash(f"Removed {u['email']}. The accounts they added stay and are yours now.", "ok")
    else:
        abort(404)
    return redirect(url_for("people"))


# --- notification settings ---------------------------------------------------
@app.get("/settings")
@_auth
@_admin_only
def settings():
    return render_template("settings.html", s=notify.current(), sms_ready=notify.sms_ready(),
                           results=None, storage=store.storage_state(), remote=remote.settings(), remote_on=remote.enabled(),
                           outside=remote.is_remote(request), update=updater.state())


# --- updates (Windows): Alerts page > Updates ----------------------------------
@app.get("/api/update")
@_auth
@_admin_only
def api_update():
    return jsonify(updater.state())


@app.post("/settings/update")
@_auth
@_admin_only
def update_settings():
    f = request.form
    if f.get("action") == "toggle":
        updater.set_enabled(f.get("on") == "1")
        flash("Automatic updates are on." if f.get("on") == "1" else "Automatic updates are off.", "ok")
    elif not updater.supported():
        flash("Updates install by themselves only on Camera Keeper for Windows.", "info")
    else:
        st = updater.check()
        if st["error"]:
            flash(f"Couldn't check for updates: {st['error']}", "error")
        elif st["installing"]:
            flash(f"Installing Camera Keeper {st['latest']}. It restarts by itself in about a minute.", "ok")
        else:
            flash(f"Camera Keeper {st['current']} is the newest.", "ok")
    return redirect(url_for("settings") + "#updates")


@app.post("/settings/number")
@_auth
@_admin_only
def add_number():
    raw = request.form.get("number", "")
    num = notify.normalize_number(raw)
    back = request.form.get("next") or url_for("settings")
    if not num:
        flash(f"“{raw}” isn't a phone number I can use. Enter 10 digits, like 773-555-0123.", "error")
        return redirect(back)
    s = notify.current()
    if any(n["number"] == num for n in s["numbers"]):
        flash(f"{notify.pretty_number(num)} is already on the list.", "info")
    else:
        s["numbers"].append({"number": num, "label": request.form.get("label", "").strip()[:40],
                             "enabled": True})
        notify.save(s)
        msg = f"Added {notify.pretty_number(num)}."
        if not notify.sms_ready(s):
            msg += " Texts start once the Twilio account below is filled in."
        flash(msg, "ok")
    return redirect(back)


@app.post("/settings/number/<path:num>/<action>")
@_auth
@_admin_only
def number_action(num, action):
    s = notify.current()
    for n in s["numbers"]:
        if n["number"] == num:
            if action == "toggle":
                n["enabled"] = not n.get("enabled", True)
            elif action == "remove":
                s["numbers"].remove(n)
            break
    notify.save(s)
    return redirect(url_for("settings"))


def _apply_form():
    s = notify.current()
    f = request.form
    for field in ("twilio_sid", "twilio_from", "ntfy_url", "pushover_user", "webhook_url", "public_url"):
        s[field] = f.get(field, "").strip()
    if s["twilio_from"]:
        s["twilio_from"] = notify.normalize_number(s["twilio_from"]) or s["twilio_from"]
    for field in notify.SECRET_FIELDS:  # blank means "keep what's saved"
        if f.get(field, "").strip():
            s[field] = f.get(field).strip()
        if f.get(f"clear_{field}"):
            s[field] = ""
    for field in ("on_clip", "on_motion", "motion_fallback"):
        s[field] = f.get(field) == "1"
    try:
        s["cooldown"] = max(0, int(f.get("cooldown", s["cooldown"])))
    except ValueError:
        pass
    notify.save(s)


@app.post("/settings/storage")
@_auth
@_admin_only
def storage_settings():
    f = request.form
    try:
        store.set_storage_rules(f.get("max_gb") or 0, f.get("keep_days") or 0, f.get("when_full") or "overwrite")
        store.cleanup()
        flash("Saved.", "ok")
    except ValueError:
        flash("Enter whole numbers.", "error")
    return redirect(url_for("settings") + "#storage")


@app.post("/settings/remote")
@_auth
@_admin_only
def remote_settings():
    user, pw = request.form.get("remote_user", "").strip(), request.form.get("remote_password", "")
    if request.form.get("action") == "off":
        remote.turn_off()
        flash("Away-from-home access is off. Outside visits are turned away.", "ok")
    elif not user or len(pw) < 8:
        flash("Enter a name and a password of at least 8 characters.", "error")
    else:
        remote.set_login(user, pw)
        if remote.is_remote(request):
            session.permanent = True
            session["remote"] = remote.stamp()   # don't sign yourself out
        flash("Saved. Away from home, sign in with that name and password (once per phone or browser).", "ok")
    return redirect(url_for("settings") + "#remote")


@app.post("/settings")
@_auth
@_admin_only
def save_settings():
    _apply_form()
    flash("Saved.", "ok")
    return redirect(url_for("settings"))


@app.post("/settings/test")
@_auth
@_admin_only
def test_alert():
    _apply_form()  # test what's on screen
    return render_template("settings.html", s=notify.current(), sms_ready=notify.sms_ready(),
                           results=notify.send_test(), storage=store.storage_state(), remote=remote.settings(), remote_on=remote.enabled(),
                           outside=remote.is_remote(request))


@app.route("/clip/<path:key>")
@_auth
def clip(key):
    row = store.get(key)
    if not row or not _can_cam(row["camera_id"]):
        abort(404)
    return render_template("clip.html", c=row)


@app.route("/media/<path:rel>")
@_auth
def media(rel):
    if _allowed() is not None:
        row = store.by_file(rel)
        if not row or not _can_cam(row["camera_id"]):
            abort(404)
    return send_from_directory(config.CLIPS_DIR, rel, conditional=True)


@app.post("/clip/<path:key>/star")
@_auth
def star(key):
    if not _can_clip(key):
        abort(404)
    store.set_starred(key, request.form.get("on") == "1")
    return redirect(request.referrer or url_for("index"))


@app.post("/clip/<path:key>/delete")
@_auth
def delete(key):
    if not _can_clip(key):
        abort(404)
    store.delete(key)
    return redirect(url_for("index"))


@app.post("/clips/bulk")
@_auth
def bulk():
    keys = _mine(request.form.getlist("key"))
    action = request.form.get("action")
    back = request.form.get("next") or url_for("index")
    if not back.startswith("/"):
        back = url_for("index")
    if not keys:
        flash("No clips selected.", "info")
        return redirect(back)
    if action == "delete":
        n = sum(1 for k in keys if store.delete(k))
        flash(f"Deleted {n} clip{'s' if n != 1 else ''}.", "ok")
    elif action in ("star", "unstar"):
        for k in keys:
            store.set_starred(k, action == "star")
        flash(f"{'Starred' if action == 'star' else 'Unstarred'} {len(keys)} clip{'s' if len(keys) != 1 else ''}.", "ok")
    return redirect(back)


@app.post("/sync")
@_auth
def sync():
    if recorder and recorder.arlo:
        import threading
        threading.Thread(target=recorder.sweep, daemon=True).start()
    return redirect(url_for("index"))


@app.get("/api/clips")
@_auth
def api_clips():
    """Saved clips, newest first. GarysTV uses camera and limit; BCN also pages with offset."""
    a = request.args
    try:
        limit = min(max(int(a.get("limit", 100)), 1), 500)
        offset = max(int(a.get("offset", 0)), 0)
    except ValueError:
        abort(400)
    cams = _account_cameras(a["account"]) if a.get("account") else None
    if cams is not None and not cams:
        return jsonify([])
    return jsonify([_slash(r) for r in _query(a.get("camera") or None, a.get("day") or None, a.get("starred") == "1",
                                              limit=limit, offset=offset, camera_ids=cams)])


# --- BCN (the web page and the phone app) -------------------------------------
def _cams_full():
    """Cameras with everything the BCN page shows on each card."""
    cams = _visible()
    last = {}
    for c in cams:
        rows = store.query(c["id"], limit=1)
        last[c["id"]] = rows[0] if rows else None
    out = []
    for c in cams:
        r = last[c["id"]]
        out.append({k: c.get(k) for k in ("id", "name", "model", "battery", "unavailable", "motion",
                                         "capturing", "last_error", "last_motion", "last_clip", "no_live")}
                   | {"viewing": liveview.viewing(c["id"]),
                      "thumb": f"/media/{r['thumb_path']}" if r and r.get("thumb_path") else None,
                      "last_key": r["key"] if r else None,
                      "last_clip_ms": r["created_ms"] if r else None})
    return out


@app.get("/api/status")
@_auth
def api_status():
    s = recorder.status if recorder else {}
    return jsonify({"connected": bool(s.get("connected")), "signing_in": bool(s.get("signing_in")),
                    "needs_login": bool(s.get("needs_login")), "error": s.get("error"),
                    "waiting_for_code": [f'{a["brand"].capitalize()} ({a["username"]})' for a in _accounts_list()
                                         if a["waiting_for_code"]],
                    "last_sweep": s.get("last_sweep"),
                    "modes": recorder.modes() if recorder and g.viewer["admin"] else {},
                    "stats": store.stats(_allowed()), "storage": store.storage_state(), "cameras": _cams_full(),
                    "clip_cameras": [c for c in store.cameras() if _can_cam(c["camera_id"])],
                    "alerts_need_setup": not active_numbers_or_channel(notify.current()),
                    "has_photo": True, "custom_photo": os.path.exists(_photo_path()),
                    "has_apk": os.path.exists(_apk_path()),
                    "phones": events.listeners()})


@app.post("/api/camera/<cam_id>/record")
@_auth
def api_record(cam_id):
    ok = bool(recorder and _can_cam(cam_id) and recorder.record_now(cam_id))
    return jsonify({"ok": ok, "error": None if ok else "That camera is already recording, or isn't available."})


@app.post("/api/sync")
@_auth
def api_sync():
    if recorder and recorder.arlo:
        import threading
        threading.Thread(target=recorder.sweep, daemon=True).start()
    return jsonify({"ok": True})


# Storage rules: GarysTV Settings > Cameras reads and changes them here.
@app.get("/api/storage")
@_auth
def api_storage():
    return jsonify(store.storage_state())


@app.post("/api/storage")
@_auth
@_admin_only
def api_storage_set():
    b = request.get_json(silent=True) or {}
    try:
        store.set_storage_rules(b.get("max_gb"), b.get("keep_days"), b.get("when_full"))
    except (TypeError, ValueError):
        abort(400)
    store.cleanup()
    return jsonify(store.storage_state())


@app.post("/api/clip/<path:key>/star")
@_auth
def api_star(key):
    on = bool((request.get_json(silent=True) or {}).get("on", True))
    if not _can_clip(key):
        abort(404)
    store.set_starred(key, on)
    return jsonify({"ok": True, "starred": on})


@app.delete("/api/clip/<path:key>")
@_auth
def api_delete(key):
    return jsonify({"ok": _can_clip(key) and store.delete(key)})


@app.post("/api/clips/bulk")
@_auth
def api_bulk():
    b = request.get_json(silent=True) or {}
    keys, action = _mine([str(k) for k in b.get("keys") or []]), b.get("action")
    if action == "delete":
        n = sum(1 for k in keys if store.delete(k))
    elif action in ("star", "unstar"):
        for k in keys:
            store.set_starred(k, action == "star")
        n = len(keys)
    else:
        abort(400)
    return jsonify({"ok": True, "count": n})


# Live view: POST starts (or joins) a session, GET .../live says how it's going, and
# .../live.mjpg is the picture. The page shows the picture in an <img>.
def _live_json(s):
    if not s:
        return {"state": "off"}
    return {"state": s.state, "error": s.error, "seconds_left": s.seconds_left(), "viewers": s.viewers}


@app.post("/api/camera/<cam_id>/live")
@_auth
def api_live_start(cam_id):
    cam = recorder.camera(cam_id) if recorder and _can_cam(cam_id) else None
    if not cam and str(cam_id).startswith("blink-"):
        return jsonify({"state": "error", "error": "Live view isn't available for Blink cameras here. Use the Blink app."}), 404
    if not cam:
        return jsonify({"state": "error", "error": "That camera isn't available."}), 404
    return jsonify(_live_json(liveview.start(recorder.owner(cam_id), cam)))


@app.get("/api/camera/<cam_id>/live")
@_auth
def api_live_state(cam_id):
    if not _can_cam(cam_id):
        abort(404)
    return jsonify(_live_json(liveview.get(cam_id)))


@app.get("/api/camera/<cam_id>/live.mjpg")
@_auth
def api_live_mjpg(cam_id):
    if not _can_cam(cam_id):
        abort(404)
    s = liveview.get(cam_id)
    if not s:
        cam = recorder.camera(cam_id) if recorder else None
        if not cam:
            abort(404)
        s = liveview.start(recorder.owner(cam_id), cam)
    return Response(s.frames(), mimetype="multipart/x-mixed-replace; boundary=frame",
                    headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


@app.get("/api/events")
@_auth
def api_events():
    """Motion and clip events as they happen (server-sent events), for the phone app."""
    try:
        since = int(request.args.get("since") or request.headers.get("Last-Event-ID-Time") or 0)
    except ValueError:
        since = 0
    viewer = g.viewer
    if viewer["admin"]:
        allow = None
    else:
        def allow(ev):   # checked per event, so cameras added later show up without reconnecting
            return users.allowed_cameras(viewer, _all_cams()).__contains__(ev.get("camera_id"))
    return Response(events.stream(since, allow=allow), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


@app.post("/api/test-phone")
@_auth
def api_test_phone():
    """Send a pretend motion alert to the phone app, using the newest clip's picture."""
    rows = _query(limit=1)
    r = rows[0] if rows else None
    if not r and not g.viewer["admin"]:
        return jsonify({"ok": False, "error": "There's no clip from your cameras to send yet."}), 400
    ev = events.publish("clip", camera=r["camera_name"] if r else "Test camera",
                        camera_id=r["camera_id"] if r else "test", key=r["key"] if r else None,
                        reason="test", started=int(time.time() * 1000),
                        video=f"/media/{r['video_path']}" if r else None,
                        thumb=f"/media/{r['thumb_path']}" if r and r.get("thumb_path") else None)
    return jsonify({"ok": True, "phones": events.listeners(), "event": ev["id"]})


# The picture at the top of the page (Barb's photo). Uploaded from the page.
def _photo_path():
    return os.path.join(config.DATA_DIR, "photo.jpg")


def _apk_path():
    path = os.path.join(config.DATA_DIR, "bcn.apk")
    old = os.path.join(config.DATA_DIR, "ban.apk")     # the name it had before the rename to BCN
    if not os.path.exists(path) and os.path.exists(old):
        try:
            os.replace(old, path)
        except OSError:
            return old
    return path


@app.get("/photo")
@_auth
def photo():
    if os.path.exists(_photo_path()):
        r = send_from_directory(config.DATA_DIR, "photo.jpg", conditional=True)
        r.headers["Cache-Control"] = "no-cache"
        return r
    default = "photo-default.jpg" if os.path.exists(os.path.join(app.static_folder, "photo-default.jpg")) else "bcn.svg"
    return redirect(url_for("static", filename=default))


@app.post("/api/photo")
@_auth
@_admin_only
def api_photo():
    import subprocess
    import tempfile
    f = request.files.get("photo")
    if not f:
        return jsonify({"ok": False, "error": "No picture was sent."}), 400
    with tempfile.NamedTemporaryFile(dir=config.DATA_DIR, delete=False) as tmp:
        f.save(tmp)
    out = _photo_path() + ".new.jpg"
    try:
        # Square, centered, 512 px: it's shown in a circle.
        r = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", tmp.name, "-frames:v", "1",
                            "-vf", "crop='min(iw,ih)':'min(iw,ih)',scale=512:512", "-q:v", "3", out],
                           capture_output=True, text=True, timeout=60)
        if r.returncode != 0 or not os.path.exists(out):
            return jsonify({"ok": False, "error": "That file isn't a picture I can read."}), 400
        os.replace(out, _photo_path())
    finally:
        for p in (tmp.name, out):
            if os.path.exists(p):
                os.remove(p)
    return jsonify({"ok": True})


@app.delete("/api/photo")
@_auth
@_admin_only
def api_photo_delete():
    if os.path.exists(_photo_path()):
        os.remove(_photo_path())
    return jsonify({"ok": True})


@app.get("/bcn.apk")
@app.get("/ban.apk")            # the old name, for links saved before the rename
@_auth
def apk():
    path = _apk_path()
    if not os.path.exists(path):
        abort(404)
    return send_from_directory(config.DATA_DIR, os.path.basename(path), as_attachment=True,
                               download_name="BCN.apk", mimetype="application/vnd.android.package-archive")


@app.get("/manifest.webmanifest")
def manifest():
    return jsonify({"name": "BCN - Barb's Camera Notifications", "short_name": "BCN", "start_url": "/",
                    # Phone apps from before the rename look for "Arlo Notifications" here to know
                    # they've found the server, so the old name stays in the description.
                    "description": "Barb's Camera Notifications (formerly Barb's Arlo Notifications)",
                    "display": "standalone", "background_color": "#04070d", "theme_color": "#0a1424",
                    "icons": [{"src": url_for("static", filename="bcn-192.png"), "sizes": "192x192", "type": "image/png"},
                              {"src": url_for("static", filename="bcn-512.png"), "sizes": "512x512", "type": "image/png"}]})


@app.get("/health")
def health():
    s = recorder.status if recorder else {}
    return jsonify({"ok": bool(s.get("connected")), **{k: v for k, v in s.items() if k != "cameras"}})


# --- moving to another server (export / import / bcn.tapper.co) -----------------------
def _home_only(fn):
    """Exports hold every password; imports replace everything. Only from the home network."""
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        if remote.is_remote(request):
            return Response("Moving Camera Keeper only works from the home network.", 403)
        return fn(*a, **kw)
    return wrapper


def _move_page(results=None, error=None):
    from . import migrate, tunnel
    import platform
    return render_template("move.html", info=migrate.summary(), last=migrate.last_result(),
                           moved=store.get_setting("moved_away"), tunnel=tunnel.settings(),
                           hostname=tunnel.hostname(), svc=tunnel.service_state(), results=results,
                           error=error, this_host=platform.node(), port=config.WEB_PORT,
                           windows=os.name == "nt")


@app.get("/move")
@_auth
@_admin_only
@_home_only
def move():
    return _move_page()


@app.get("/move/export.zip")
@_auth
@_admin_only
@_home_only
def move_export():
    from . import migrate
    clips = request.args.get("clips", "1") != "0"
    import platform
    name = f"bcn-export-{platform.node() or 'server'}-{time.strftime('%Y%m%d-%H%M')}{'' if clips else '-no-clips'}.zip"
    return Response(migrate.stream_export(include_clips=clips), mimetype="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store",
                             "X-Accel-Buffering": "no"})


@app.post("/api/move/import")
@_auth
@_admin_only
@_home_only
def move_import():
    """The export .zip as the raw request body (the page sends it with fetch, with progress)."""
    from . import migrate
    dest = migrate.pending_path()
    part = dest + ".part"
    try:
        with open(part, "wb") as f:
            while True:
                chunk = request.stream.read(1024 * 1024)
                if not chunk:
                    break
                f.write(chunk)
        m = migrate.read_manifest(part)
    except ValueError as e:
        os.remove(part)
        return jsonify({"ok": False, "error": str(e)}), 400
    except OSError as e:
        return jsonify({"ok": False, "error": f"Couldn't save the upload: {e}"}), 500
    os.replace(part, dest)
    try:
        os.remove(migrate.result_path())
    except OSError:
        pass
    migrate.restart_soon()
    return jsonify({"ok": True, "from": m.get("made_by"), "made": m.get("made"), "clips": m.get("clips")})


@app.get("/api/move/state")
@_auth
@_admin_only
@_home_only
def move_state():
    from . import migrate
    return jsonify({"last": migrate.last_result(), "pending": os.path.exists(migrate.pending_path())})


@app.post("/move/tunnel")
@_auth
@_admin_only
@_home_only
def move_tunnel():
    from . import tunnel
    host = request.form.get("hostname", "").strip() or None
    try:
        if request.form.get("api_token", "").strip():
            r = tunnel.setup_with_api(request.form["api_token"], host)
        elif request.form.get("tunnel_token", "").strip():
            r = tunnel.setup_with_token(request.form["tunnel_token"], host)
        else:
            return _move_page(error="Paste a Cloudflare API token (or a tunnel token).")
    except tunnel.TunnelError as e:
        return _move_page(error=str(e))
    except Exception as e:
        log_web.exception("tunnel setup failed")
        return _move_page(error=f"Something went wrong: {e}")
    return _move_page(results=r["steps"])


@app.post("/move/retire")
@_auth
@_admin_only
@_home_only
def move_retire():
    """This server's job moved: stop signing in to Arlo here (only one place can at a time)."""
    from . import migrate
    if request.form.get("action") == "back":
        with store.connect() as c:
            c.execute("DELETE FROM settings WHERE key='moved_away'")
        flash("Recording is turning back on here. Make sure the other server is off.", "ok")
    else:
        store.set_setting("moved_away", {"at": time.strftime("%Y-%m-%d %H:%M"),
                                         "to": request.form.get("to", "").strip()[:80]})
        flash("Recording is off here. Camera Keeper restarts in a moment without signing in to Arlo.", "ok")
    migrate.restart_soon()
    return redirect(url_for("move"))


# --- where clips are kept (Storage on the Alerts page; the move routine's -ClipsFolder) ----------
@app.get("/api/storage/location")
@_auth
@_admin_only
@_home_only
def api_clips_location():
    from . import clips_location
    return jsonify(clips_location.state())


@app.post("/api/storage/location")
@_auth
@_admin_only
@_home_only
def api_clips_location_set():
    """{"path": "D:\\BCN clips"}: copies the clips there, switches to it and restarts."""
    from . import clips_location, migrate
    b = request.get_json(silent=True)
    if not isinstance(b, dict) or not str(b.get("path") or "").strip():
        return jsonify({"ok": False, "error": "Send JSON {\"path\": ...}."}), 400
    try:
        st = clips_location.start_move(str(b["path"]), migrate.restart_soon)
    except (ValueError, OSError) as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    return jsonify({"ok": True, **st})
