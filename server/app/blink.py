"""Blink cameras, alongside the Arlo ones: motion pop-ups on GarysTV, phone alerts, and the clip.

Uses blinkpy (the library Home Assistant uses for Blink). Blink has no instant motion push like
Arlo's: BAN asks Blink every BLINK_POLL_SECONDS (30 by default). Blink only reports motion once it
has saved a clip, either in its cloud (Blink plan, or an account from before April 2020) or on a
USB drive in a Sync Module 2 (no plan needed). So when BAN hears about motion the clip is ready:

  1. the clip is downloaded and saved with the Arlo clips (Recordings, the GarysTV Arlo screen)
  2. GarysTV gets the same motion event as for an Arlo camera, pointing at that clip, so it pops
     up in the corner of the TV (if the camera is ticked in GarysTV's Settings > Arlo)
  3. the phone app and the alerts (texts, ntfy, ...) go out as for Arlo
If the clip can't be downloaded, the pop-up shows the camera's motion picture instead.

The system (or camera) must be armed in the Blink app: Blink records nothing while disarmed.

Each account is {id, username, password} in the "blink_accounts" setting; its Blink session
(tokens, so no new code is needed after a restart) is in state/blink-<id>.json.
"""
import asyncio
import json
import logging
import os
import secrets
import subprocess
import tempfile
import threading
import time
from datetime import datetime

from . import config, events, garystv, notify, store

log = logging.getLogger("blink")

KEY = "blink_accounts"
POLL_SECONDS = max(15, int(os.environ.get("BLINK_POLL_SECONDS", "30") or 30))
CODE_WAIT_SECONDS = 600
POPUP_DIR = "_popups"           # still-picture pop-ups, under the clips folder (so /media/ serves them)
POPUP_KEEP_SECONDS = 3600
NO_WINDOW = 0x08000000 if os.name == "nt" else 0
MODELS = {"catalina": "Outdoor/Indoor", "sedona": "Outdoor 4", "owl": "Mini", "hawk": "Mini 2",
          "lotus": "Doorbell", "superior": "Floodlight", "xt": "XT", "xt2": "XT2"}

try:
    import aiohttp
    from blinkpy.auth import Auth, BlinkTwoFARequiredError
    from blinkpy.blinkpy import Blink
    AVAILABLE = True
except Exception as e:          # an install without blinkpy keeps working, just without Blink
    AVAILABLE = False
    _IMPORT_ERROR = str(e)


def _slug(name):
    return "".join(ch if ch.isalnum() else "_" for ch in (name or "")).strip("_") or "camera"


def _when(stamp):
    """Blink's clip time (ISO, usually UTC) as a local timestamp."""
    try:
        return datetime.fromisoformat(str(stamp).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return time.time()


# --- saved accounts ------------------------------------------------------------
_lock = threading.Lock()


def accounts():
    return store.get_setting(KEY) or []


def _save_accounts(accs):
    store.set_setting(KEY, accs)


def get_account(aid):
    return next((a for a in accounts() if a["id"] == aid), None)


def _session_file(aid):
    return os.path.join(config.STATE_DIR, f"blink-{aid}.json")


# --- one Blink account ------------------------------------------------------------
class Account:
    def __init__(self, hub, aid):
        self.hub = hub
        self.id = aid
        self.status = {"connected": False, "signing_in": False, "waiting_for_code": False,
                       "error": None, "last_poll": None}
        self.cams = {}               # our camera id -> {"cam": BlinkCamera, "status": {...}}
        self.removed = False
        self._loop = None
        self._code = None
        self._code_ready = threading.Event()
        self._wake = threading.Event()
        self._jobs = []              # callables to run on the Blink loop (record, snapshot)
        self._seen = set()           # clip addresses already handled
        self.thread = threading.Thread(target=self._thread, name=f"blink-{aid}", daemon=True)
        self.thread.start()

    @property
    def username(self):
        a = get_account(self.id)
        return a["username"] if a else "?"

    # called from the web page
    def submit_code(self, code):
        self._code = "".join(ch for ch in code if ch.isdigit())
        self._code_ready.set()

    def new_password(self):
        self.status["error"] = None
        self._wake.set()

    def stop(self):
        self.removed = True
        self._code_ready.set()
        self._wake.set()

    def run_job(self, coro_fn):
        self._jobs.append(coro_fn)
        self._wake.set()

    # the Blink thread
    def _thread(self):
        self._loop = asyncio.new_event_loop()
        try:
            self._loop.run_until_complete(self._main())
        except Exception:
            log.exception("Blink %s stopped", self.username)
        finally:
            self.status.update(connected=False)
            self._loop.close()

    async def _sleep(self, seconds):
        await asyncio.get_running_loop().run_in_executor(None, self._wake.wait, seconds)
        self._wake.clear()

    async def _main(self):
        while not self.removed:
            a = get_account(self.id)
            if not a or not a.get("password"):
                self.status.update(connected=False, signing_in=False)
                await self._sleep(30)
                continue
            async with aiohttp.ClientSession() as session:
                blink = await self._sign_in(a, session)
                if blink:
                    await self._watch(blink)
            if not self.removed:
                await self._sleep(60 if self.status.get("error") else 5)

    def _login_data(self, a):
        data = {}
        try:
            with open(_session_file(self.id)) as f:
                data = json.load(f)
        except (OSError, ValueError):
            pass
        if data.get("username", "").lower() != a["username"].lower():
            data = {}                                    # a different login: start fresh
        data.update(username=a["username"], password=a["password"])
        return data

    async def _save_session(self, blink):
        try:
            os.makedirs(config.STATE_DIR, exist_ok=True)
            tmp = _session_file(self.id) + ".new"
            with open(tmp, "w") as f:
                json.dump(blink.auth.login_attributes, f)
            os.replace(tmp, _session_file(self.id))
        except Exception as e:
            log.warning("couldn't save Blink session: %s", e)

    async def _sign_in(self, a, session):
        self.status.update(signing_in=True, error=None)
        blink = Blink(session=session, refresh_rate=POLL_SECONDS, motion_interval=2)
        blink.auth = Auth(self._login_data(a), no_prompt=True, session=session,
                          callback=lambda: asyncio.ensure_future(self._save_session(blink)))
        try:
            try:
                ok = await blink.start()
            except BlinkTwoFARequiredError:
                self._code, ok = None, False
                self._code_ready.clear()
                self.status.update(waiting_for_code=True, code_requested_at=time.time())
                log.info("Blink %s wants a verification code", a["username"])
                got = await asyncio.get_running_loop().run_in_executor(None, self._code_ready.wait, CODE_WAIT_SECONDS)
                self.status["waiting_for_code"] = False
                if self.removed:
                    return None
                if not got or not self._code:
                    raise RuntimeError("No verification code was entered in time. Sign in again.")
                ok = await blink.send_2fa_code(self._code)
                if not ok:
                    raise RuntimeError("Blink didn't accept that code. Sign in again for a new one.")
            if not ok:
                raise RuntimeError("Blink sign-in failed. Check the email and password.")
        except Exception as e:
            msg = str(e) or type(e).__name__
            log.warning("Blink sign-in for %s failed: %s", a["username"], msg)
            if "password" in msg.lower() or "login" in msg.lower() or "Unauthorized" in msg:
                _forget_password(self.id)                 # don't keep retrying a wrong one
            self.status.update(signing_in=False, connected=False, error=msg)
            return None
        await self._save_session(blink)
        self.status.update(signing_in=False, connected=True, error=None)
        log.info("Blink %s connected: %s", a["username"], ", ".join(blink.cameras.keys()) or "no cameras")
        return blink

    def _cam_id(self, cam):
        return f"blink-{cam.network_id}-{cam.camera_id}"

    def _index(self, blink):
        for name, cam in blink.cameras.items():
            cid = self._cam_id(cam)
            entry = self.cams.setdefault(cid, {"status": {"last_motion": None, "last_clip": None, "last_error": None}})
            entry["cam"] = cam

    async def _watch(self, blink):
        self._index(blink)
        first = True
        while not self.removed and get_account(self.id) and get_account(self.id).get("password"):
            try:
                await blink.refresh(force=True)
                self._index(blink)
                self.status.update(connected=True, error=None, last_poll=time.time())
            except Exception as e:
                log.warning("Blink refresh failed: %s", e)
                self.status.update(error=f"Couldn't reach Blink: {e}")
                if "401" in str(e) or "Unauthorized" in type(e).__name__:
                    return                             # sign in again
                await self._sleep(POLL_SECONDS)
                continue
            for cid, entry in list(self.cams.items()):
                cam = entry.get("cam")
                if not cam:
                    continue
                recent = list(getattr(cam, "recent_clips", []) or [])
                for rec in recent:
                    addr = rec.get("clip")
                    if not addr or addr in self._seen:
                        continue
                    self._seen.add(addr)
                    if first:                          # clips from before BAN started: just note them
                        continue
                    try:
                        await self._on_clip(cid, cam, rec)
                    except Exception:
                        log.exception("Blink clip for %s", cam.name)
                if recent:
                    cam.recent_clips = [r for r in cam.recent_clips if r.get("clip") not in self._seen]
            first = False
            while self._jobs:
                job = self._jobs.pop(0)
                try:
                    await job(blink)
                except Exception as e:
                    log.warning("Blink request failed: %s", e)
            if len(self._seen) > 2000:
                self._seen = set(list(self._seen)[-500:])
            await self._sleep(POLL_SECONDS)

    # --- a new Blink clip ------------------------------------------------------------
    async def _download(self, cam, addr, attempts=4):
        for i in range(attempts):
            try:
                r = await cam.get_video_clip(addr)
                if r is not None and r.status == 200:
                    data = await r.read()
                    if len(data) > 1000:
                        return data
            except Exception as e:
                log.debug("Blink clip download try %d: %s", i + 1, e)
            await asyncio.sleep(5)               # local-storage clips take a moment to upload
        return None

    async def _picture(self, cam):
        try:
            r = await cam.get_thumbnail()
            if r is not None and r.status == 200:
                return await r.read()
        except Exception as e:
            log.debug("Blink picture: %s", e)
        return getattr(cam, "_cached_image", None)

    async def _on_clip(self, cid, cam, rec, test=False):
        started = _when(rec.get("time"))
        st = self.cams[cid]["status"]
        st["last_motion"] = started
        log.info("Blink motion%s: %s", " (test)" if test else "", cam.name)
        if not test:
            events.publish("motion", camera=cam.name, camera_id=cid, started=int(started * 1000))
        video = await self._download(cam, rec["clip"]) if rec.get("clip") else None
        loop = asyncio.get_running_loop()
        if video:
            saved = await loop.run_in_executor(None, self.hub.save_clip, cid, cam.name, started, video)
        else:
            saved = None
        if saved:
            key, rel_video, rel_thumb = saved
            st["last_clip"] = time.time()
            popup = rel_video
        else:
            st["last_error"] = "Blink's clip couldn't be downloaded; showed its picture instead"
            pic = await self._picture(cam)
            popup = await loop.run_in_executor(None, self.hub.still_popup, cid, cam.name, started, pic) if pic else None
            key, rel_thumb = f"{cid}:{int(started * 1000)}", None
        if popup:
            garystv.motion_started({"event": "motion", "camera": cam.name, "camera_id": cid,
                                    "time": int(started * 1000), "key": key, "file": popup,
                                    "clip": f"/media/{popup}", "source": "blink"})
        if test:
            return
        when = datetime.fromtimestamp(started)
        s = notify.current()
        if saved:
            events.publish("clip", camera=cam.name, camera_id=cid, key=key, reason="motion",
                           started=int(started * 1000), video=f"/media/{rel_video}",
                           thumb=f"/media/{rel_thumb}" if rel_thumb else None)
        if s["on_motion"] or s["on_clip"]:      # Blink's clip and motion arrive together: one alert
            pub = (s["public_url"] or "").rstrip("/")
            notify.send(cid, "clip", f"Motion: {cam.name}",
                        f"{'Clip saved' if saved else 'Motion'} at {when:%I:%M:%S %p}",
                        link=(f"{pub}/clip/{key}" if saved else pub) if pub else None,
                        thumb=os.path.join(config.CLIPS_DIR, *rel_thumb.split("/")) if rel_thumb else None,
                        extra={"camera": cam.name, "event": "clip" if saved else "motion", "key": key})


def _forget_password(aid):
    with _lock:
        accs = accounts()
        for a in accs:
            if a["id"] == aid:
                a["password"] = ""
        _save_accounts(accs)


# --- every Blink account -------------------------------------------------------------
class Hub:
    def __init__(self):
        self._accts = {}
        self._lock = threading.Lock()

    def start_all(self):
        if not AVAILABLE:
            if accounts():
                log.warning("Blink accounts are set up but blinkpy isn't installed: %s", _IMPORT_ERROR)
            return
        for a in accounts():
            self._start(a["id"])
        threading.Thread(target=self._tidy_popups, name="blink-tidy", daemon=True).start()

    def _start(self, aid):
        with self._lock:
            if aid not in self._accts or not self._accts[aid].thread.is_alive():
                self._accts[aid] = Account(self, aid)
            return self._accts[aid]

    def add(self, username, password):
        if not AVAILABLE:
            raise RuntimeError("This Arlo Keeper was installed without Blink support (blinkpy). Update it.")
        username = username.strip()
        with _lock:
            accs = accounts()
            a = next((x for x in accs if x["username"].lower() == username.lower()), None)
            if a:
                a["password"] = password
            else:
                a = {"id": secrets.token_hex(4), "username": username, "password": password}
                accs.append(a)
            _save_accounts(accs)
        acct = self._start(a["id"])
        acct.new_password()
        return a["id"]

    def submit_code(self, aid, code):
        a = self._accts.get(aid)
        if a:
            a.submit_code(code)

    def sign_out(self, aid):
        with _lock:
            _save_accounts([a for a in accounts() if a["id"] != aid])
        a = self._accts.pop(aid, None)
        if a:
            a.stop()
        try:
            os.remove(_session_file(aid))
        except OSError:
            pass

    def accounts_view(self):
        out = []
        for a in accounts():
            acct = self._accts.get(a["id"])
            st = acct.status if acct else {}
            out.append({"id": a["id"], "username": a["username"], "connected": bool(st.get("connected")),
                        "signing_in": bool(st.get("signing_in")), "error": st.get("error"),
                        "needs_login": not a.get("password"),
                        "waiting_for_code": bool(st.get("waiting_for_code")),
                        "cameras": [e["cam"].name for e in (acct.cams.values() if acct else []) if e.get("cam")],
                        "last_poll": st.get("last_poll")})
        return out

    def connected(self):
        return any(a.status.get("connected") for a in list(self._accts.values()))

    def _find(self, cid):
        for acct in list(self._accts.values()):
            e = acct.cams.get(cid)
            if e and e.get("cam"):
                return acct, e
        return None, None

    def owns(self, cid):
        return str(cid).startswith("blink-")

    def cameras_view(self):
        out = []
        for acct in list(self._accts.values()):
            for cid, e in list(acct.cams.items()):
                cam = e.get("cam")
                if not cam:
                    continue
                batt = getattr(cam, "battery", None)   # Blink says "ok" or "low", not a percentage
                batt = 10 if str(batt).lower() == "low" else None
                online = True
                try:
                    online = cam.online is not False
                except Exception:
                    pass
                out.append({"id": cid, "name": cam.name, "model": "Blink " + MODELS.get(getattr(cam, "product_type", None), "camera"),
                            "battery": batt, "unavailable": not online or not acct.status.get("connected"),
                            "motion": False, "capturing": False, "brand": "blink", "no_live": True,
                            "account": acct.username, **e["status"]})
        return out

    def record_now(self, cid):
        acct, e = self._find(cid)
        if not acct:
            return False

        async def job(_blink):
            await e["cam"].record()            # the clip shows up on a later check, like motion
        acct.run_job(job)
        return True

    def test_motion(self, cid):
        """Pop up the newest Blink clip (or picture) on GarysTV, with no alerts."""
        acct, e = self._find(cid)
        if not acct:
            return False
        cam = e["cam"]

        rows = store.query(cid, limit=1)
        if rows:                               # the newest saved clip: no download needed
            r = rows[0]
            garystv.motion_started({"event": "motion", "camera": cam.name, "camera_id": cid,
                                    "time": int(time.time() * 1000), "key": r["key"], "file": r["video_path"],
                                    "clip": f"/media/{r['video_path']}", "source": "blink", "test": True})
            return True

        async def job(_blink):
            rec = {"clip": cam.clip, "time": datetime.now().astimezone().isoformat()}
            await acct._on_clip(cid, cam, rec, test=True)
        acct.run_job(job)
        return True

    # --- files (run in a worker thread) ---------------------------------------------
    def save_clip(self, cid, name, started, data):
        from .capture import _thumb
        from . import recorder as rec_mod
        when = datetime.fromtimestamp(started)
        rel_dir = f"{_slug(name)}/{when:%Y-%m-%d}"
        base = when.strftime("%H-%M-%S")
        rel_video, rel_thumb = f"{rel_dir}/{base}.mp4", f"{rel_dir}/{base}.jpg"
        key = f"{cid}:{int(started * 1000)}"
        if store.has(key):
            return key, rel_video, rel_thumb
        abs_video = os.path.join(config.CLIPS_DIR, *rel_video.split("/"))
        abs_thumb = os.path.join(config.CLIPS_DIR, *rel_thumb.split("/"))
        os.makedirs(os.path.dirname(abs_video), exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=config.DATA_DIR, suffix=".mp4", delete=False) as tmp:
            tmp.write(data)
        try:
            duration = rec_mod._probe_duration(tmp.name)
            if config.CLIP_MAX_SECONDS and duration and duration > config.CLIP_MAX_SECONDS + 1:
                out = tmp.name + ".trim.mp4"
                r = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", tmp.name, "-t",
                                    str(config.CLIP_MAX_SECONDS), "-c", "copy", "-movflags", "+faststart", out],
                                   timeout=120, creationflags=NO_WINDOW)
                if r.returncode == 0:
                    os.replace(out, tmp.name)
                    duration = float(config.CLIP_MAX_SECONDS)
            os.replace(tmp.name, abs_video)
        finally:
            if os.path.exists(tmp.name):
                os.remove(tmp.name)
        thumb_ok = _thumb(abs_video, abs_thumb)
        store.add({"key": key, "camera_id": cid, "camera_name": name, "created_ms": int(started * 1000),
                   "duration_s": duration, "trigger": "motion", "object_type": None, "source": "blink",
                   "video_path": rel_video, "thumb_path": rel_thumb if thumb_ok else None,
                   "size_bytes": os.path.getsize(abs_video), "starred": 0})
        log.info("saved Blink clip %s (%.0fs)", rel_video, duration or 0)
        return key, rel_video, rel_thumb if thumb_ok else None

    def still_popup(self, cid, name, started, jpg):
        """A few seconds of video made from one picture, for GarysTV's pop-up."""
        rel = f"{POPUP_DIR}/{_slug(name)}-{int(started)}.mp4"
        dest = os.path.join(config.CLIPS_DIR, *rel.split("/"))
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=config.DATA_DIR, suffix=".jpg", delete=False) as f:
            f.write(jpg)
        try:
            r = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-loop", "1", "-i", f.name, "-t", "8",
                                "-r", "5", "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2,format=yuv420p",
                                "-c:v", "libx264", "-preset", "veryfast", "-movflags", "+faststart", dest],
                               timeout=60, creationflags=NO_WINDOW)
            return rel if r.returncode == 0 else None
        finally:
            os.remove(f.name)

    def _tidy_popups(self):
        while True:
            d = os.path.join(config.CLIPS_DIR, POPUP_DIR)
            for n in os.listdir(d) if os.path.isdir(d) else []:
                p = os.path.join(d, n)
                try:
                    if time.time() - os.path.getmtime(p) > POPUP_KEEP_SECONDS:
                        os.remove(p)
                except OSError:
                    pass
            time.sleep(600)
