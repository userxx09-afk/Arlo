"""Small web page for browsing, playing, starring and deleting saved clips."""
import functools
import os
import re
import time
from datetime import datetime

import secrets

from flask import (Flask, Response, abort, flash, jsonify, redirect, render_template, request,
                   send_from_directory, url_for)

from . import arlo_login, config, events, garystv, liveview, notify, store

app = Flask(__name__, template_folder="templates")


def init_secret():
    key = store.get_setting("web_secret")
    if not key:
        key = secrets.token_hex(32)
        store.set_setting("web_secret", key)
    app.secret_key = key
recorder = None  # set by main.py


def _auth(fn):
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        if config.WEB_USER:
            auth = request.authorization
            if not auth or auth.username != config.WEB_USER or auth.password != config.WEB_PASSWORD:
                return Response("Login required", 401, {"WWW-Authenticate": 'Basic realm="Arlo Keeper"'})
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
    """BAN: cameras and recordings. The page fills itself in from the /api/ calls below."""
    return render_template("index.html", status=recorder.status if recorder else {},
                           in_app="BAN-Android" in (request.headers.get("User-Agent") or ""))


@app.post("/camera/<cam_id>/record")
@_auth
def record_now(cam_id):
    if recorder and recorder.record_now(cam_id):
        flash("Recording a test clip. It shows up here in about 30–40 seconds.", "ok")
    else:
        flash("That camera is already recording, or isn't available.", "info")
    return redirect(url_for("index"))


@app.get("/cameras.json")
@_auth
def cameras_json():
    return jsonify(recorder.cameras_view() if recorder and hasattr(recorder, "cameras_view") else [])


# --- GarysTV (motion clips pop up on the TV) ---------------------------------
@app.get("/api/cameras")
@_auth
def api_cameras():
    """The cameras, for GarysTV's Settings > Arlo."""
    s = recorder.status if recorder else {}
    cams = recorder.cameras_view() if recorder else []
    return jsonify({"connected": bool(s.get("connected")), "modes": recorder.modes() if recorder else {},
                    "cameras": [{k: c.get(k) for k in ("id", "name", "model", "battery", "unavailable",
                                                       "last_motion", "last_clip")} for c in cams]})


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
def api_subscribe():
    url = _subscriber_url()
    garystv.subscribe(url)
    return jsonify({"ok": True, "url": url})


@app.delete("/api/subscribe")
@_auth
def api_unsubscribe():
    garystv.unsubscribe(_subscriber_url())
    return jsonify({"ok": True})


@app.post("/api/test-motion/<cam_id>")
@_auth
def api_test_motion(cam_id):
    ok = bool(recorder and recorder.test_motion(cam_id))
    return jsonify({"ok": ok, "error": None if ok else "That camera is already recording, or isn't available."})


@app.get("/live/<live_id>.ts")
@_auth
def live_clip(live_id):
    """A motion clip while it records: sent as it grows, ending when the recording does.

    Can be asked for before the first picture has arrived; it waits for it.
    """
    if not re.fullmatch(r"[A-Za-z0-9_-]+", live_id) or not recorder:
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
            "pretty_number": notify.pretty_number}


def active_numbers_or_channel(s):
    return bool(notify.active_numbers(s) or notify.has_any_channel(s))


# --- Arlo sign-in ------------------------------------------------------------
@app.get("/connect")
@_auth
def connect():
    user = (store.get_setting("arlo_login") or {}).get("username") or config.ARLO_USERNAME
    return render_template("connect.html", saved_user=user, login=arlo_login.state,
                           status=recorder.status if recorder else {})


@app.post("/connect")
@_auth
def connect_submit():
    user = request.form.get("username", "").strip()
    pw = request.form.get("password", "")
    if not user or not pw:
        flash("Enter both the Arlo email and password.", "error")
        return redirect(url_for("connect"))
    if recorder and recorder.status.get("connected"):
        flash("Already connected. Restart the service to sign in as someone else.", "info")
        return redirect(url_for("connect"))
    arlo_login.save_credentials(user, pw)
    flash("Signing in to Arlo…", "ok")
    return redirect(url_for("connect"))


@app.post("/connect/code")
@_auth
def connect_code():
    arlo_login.submit_code(request.form.get("code", ""))
    flash("Code sent to Arlo.", "ok")
    return redirect(url_for("connect"))


@app.get("/connect/state")
@_auth
def connect_state():
    s = recorder.status if recorder else {}
    return jsonify({"waiting_for_code": arlo_login.state["waiting_for_code"],
                    "connected": bool(s.get("connected")), "signing_in": bool(s.get("signing_in")),
                    "error": s.get("error")})


# --- notification settings ---------------------------------------------------
@app.get("/settings")
@_auth
def settings():
    return render_template("settings.html", s=notify.current(), sms_ready=notify.sms_ready(),
                           results=None)


@app.post("/settings/number")
@_auth
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


@app.post("/settings")
@_auth
def save_settings():
    _apply_form()
    flash("Saved.", "ok")
    return redirect(url_for("settings"))


@app.post("/settings/test")
@_auth
def test_alert():
    _apply_form()  # test what's on screen
    return render_template("settings.html", s=notify.current(), sms_ready=notify.sms_ready(),
                           results=notify.send_test())


@app.route("/clip/<path:key>")
@_auth
def clip(key):
    row = store.get(key)
    if not row:
        abort(404)
    return render_template("clip.html", c=row)


@app.route("/media/<path:rel>")
@_auth
def media(rel):
    return send_from_directory(config.CLIPS_DIR, rel, conditional=True)


@app.post("/clip/<path:key>/star")
@_auth
def star(key):
    store.set_starred(key, request.form.get("on") == "1")
    return redirect(request.referrer or url_for("index"))


@app.post("/clip/<path:key>/delete")
@_auth
def delete(key):
    store.delete(key)
    return redirect(url_for("index"))


@app.post("/clips/bulk")
@_auth
def bulk():
    keys = request.form.getlist("key")
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
    """Saved clips, newest first. GarysTV uses camera and limit; BAN also pages with offset."""
    a = request.args
    try:
        limit = min(max(int(a.get("limit", 100)), 1), 500)
        offset = max(int(a.get("offset", 0)), 0)
    except ValueError:
        abort(400)
    return jsonify(store.query(a.get("camera") or None, a.get("day") or None, a.get("starred") == "1",
                               limit=limit, offset=offset))


# --- BAN (the web page and the phone app) -------------------------------------
def _cams_full():
    """Cameras with everything the BAN page shows on each card."""
    cams = recorder.cameras_view() if recorder else []
    last = {}
    for c in cams:
        rows = store.query(c["id"], limit=1)
        last[c["id"]] = rows[0] if rows else None
    out = []
    for c in cams:
        r = last[c["id"]]
        out.append({k: c.get(k) for k in ("id", "name", "model", "battery", "unavailable", "motion",
                                         "capturing", "last_error", "last_motion", "last_clip")}
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
                    "last_sweep": s.get("last_sweep"), "modes": recorder.modes() if recorder else {},
                    "stats": store.stats(), "cameras": _cams_full(),
                    "clip_cameras": store.cameras(),
                    "alerts_need_setup": not active_numbers_or_channel(notify.current()),
                    "has_photo": True, "custom_photo": os.path.exists(_photo_path()),
                    "has_apk": os.path.exists(_apk_path()),
                    "phones": events.listeners()})


@app.post("/api/camera/<cam_id>/record")
@_auth
def api_record(cam_id):
    ok = bool(recorder and recorder.record_now(cam_id))
    return jsonify({"ok": ok, "error": None if ok else "That camera is already recording, or isn't available."})


@app.post("/api/sync")
@_auth
def api_sync():
    if recorder and recorder.arlo:
        import threading
        threading.Thread(target=recorder.sweep, daemon=True).start()
    return jsonify({"ok": True})


@app.post("/api/clip/<path:key>/star")
@_auth
def api_star(key):
    on = bool((request.get_json(silent=True) or {}).get("on", True))
    store.set_starred(key, on)
    return jsonify({"ok": True, "starred": on})


@app.delete("/api/clip/<path:key>")
@_auth
def api_delete(key):
    return jsonify({"ok": store.delete(key)})


@app.post("/api/clips/bulk")
@_auth
def api_bulk():
    b = request.get_json(silent=True) or {}
    keys, action = [str(k) for k in b.get("keys") or []], b.get("action")
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
    cam = recorder.camera(cam_id) if recorder else None
    if not cam:
        return jsonify({"state": "error", "error": "That camera isn't available."}), 404
    return jsonify(_live_json(liveview.start(recorder, cam)))


@app.get("/api/camera/<cam_id>/live")
@_auth
def api_live_state(cam_id):
    return jsonify(_live_json(liveview.get(cam_id)))


@app.get("/api/camera/<cam_id>/live.mjpg")
@_auth
def api_live_mjpg(cam_id):
    s = liveview.get(cam_id)
    if not s:
        cam = recorder.camera(cam_id) if recorder else None
        if not cam:
            abort(404)
        s = liveview.start(recorder, cam)
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
    return Response(events.stream(since), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


@app.post("/api/test-phone")
@_auth
def api_test_phone():
    """Send a pretend motion alert to the phone app, using the newest clip's picture."""
    rows = store.query(limit=1)
    r = rows[0] if rows else None
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
    return os.path.join(config.DATA_DIR, "ban.apk")


@app.get("/photo")
@_auth
def photo():
    if os.path.exists(_photo_path()):
        r = send_from_directory(config.DATA_DIR, "photo.jpg", conditional=True)
        r.headers["Cache-Control"] = "no-cache"
        return r
    default = "photo-default.jpg" if os.path.exists(os.path.join(app.static_folder, "photo-default.jpg")) else "ban.svg"
    return redirect(url_for("static", filename=default))


@app.post("/api/photo")
@_auth
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
def api_photo_delete():
    if os.path.exists(_photo_path()):
        os.remove(_photo_path())
    return jsonify({"ok": True})


@app.get("/ban.apk")
@_auth
def apk():
    if not os.path.exists(_apk_path()):
        abort(404)
    return send_from_directory(config.DATA_DIR, "ban.apk", as_attachment=True,
                               mimetype="application/vnd.android.package-archive")


@app.get("/manifest.webmanifest")
def manifest():
    return jsonify({"name": "BAN - Barb's Arlo Notifications", "short_name": "BAN", "start_url": "/",
                    "display": "standalone", "background_color": "#04070d", "theme_color": "#0a1424",
                    "icons": [{"src": url_for("static", filename="ban-192.png"), "sizes": "192x192", "type": "image/png"},
                              {"src": url_for("static", filename="ban-512.png"), "sizes": "512x512", "type": "image/png"}]})


@app.get("/health")
def health():
    s = recorder.status if recorder else {}
    return jsonify({"ok": bool(s.get("connected")), **{k: v for k, v in s.items() if k != "cameras"}})
