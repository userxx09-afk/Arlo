"""Logs into Arlo, watches for new recordings, and saves each one locally."""
import logging
import os
import shutil
import subprocess
import tempfile
import threading
import time
from datetime import datetime

import pyaarlo

from . import arlo_login, config, events, notify, store
from .capture import LiveCapture

log = logging.getLogger("recorder")

# Seconds to wait for a clip after motion before sending a "no recording" alert.
FALLBACK_SECONDS = 120


def _slug(name):
    return "".join(ch if ch.isalnum() else "_" for ch in name).strip("_") or "camera"


def _created_ms(video):
    ts = video.created_at or 0
    # cloud library returns ms; base-station library is adjusted to ms by pyaarlo
    return int(ts if ts > 10_000_000_000 else ts * 1000)


def _trim(src, dst, seconds):
    """Cut to the first N seconds without re-encoding. Falls back to a plain copy."""
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", src, "-t", str(seconds),
             "-c", "copy", "-movflags", "+faststart", dst],
            check=True, timeout=120,
        )
        return True
    except Exception as e:
        log.warning("trim failed (%s), keeping full clip", e)
        shutil.copyfile(src, dst)
        return False


def _probe_duration(path):
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
            capture_output=True, text=True, timeout=30,
        ).stdout.strip()
        return float(out)
    except Exception:
        return None


def _thumb_from_video(video_path, thumb_path):
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-ss", "1", "-i", video_path,
             "-frames:v", "1", "-vf", "scale=480:-2", thumb_path],
            check=True, timeout=60,
        )
        return os.path.exists(thumb_path)
    except Exception:
        return False


class Recorder:
    def __init__(self):
        self.arlo = None
        self._sweep_lock = threading.Lock()
        self._sweep_timer = None
        self._sweep_due = 0
        self._stop = threading.Event()
        self._last_clip = {}  # camera_id -> time a clip was last saved
        self._motion = {}     # camera_id -> motion currently active
        self._cam_status = {}
        self.capture = LiveCapture(self)
        self.status = {"connected": False, "last_sweep": None, "cameras": [], "error": None}

    # --- per-camera status (shown on the home page) -------------------------
    def cam_status(self, cam):
        return self._cam_status.setdefault(cam.device_id, {
            "last_motion": None, "last_clip": None, "last_error": None, "capturing": False})

    def motion_active(self, cam_id):
        return bool(self._motion.get(cam_id))

    def note_clip(self, cam_id):
        self._last_clip[cam_id] = time.time()

    def camera(self, cam_id):
        if not self.arlo:
            return None
        return next((c for c in self.arlo.cameras if c.device_id == cam_id), None)

    def cameras_view(self):
        if not self.arlo:
            return []
        out = []
        for c in self.arlo.cameras:
            st = self.cam_status(c)
            try:
                batt = c.battery_level
            except Exception:
                batt = None
            out.append({"id": c.device_id, "name": c.name, "model": c.model_id, "battery": batt,
                        "unavailable": bool(getattr(c, "is_unavailable", False)),
                        "motion": self.motion_active(c.device_id), **st})
        return out

    def test_motion(self, cam_id):
        """Act as if the camera saw motion: record a clip and tell GarysTV, but send no alerts."""
        cam = self.camera(cam_id)
        if not cam:
            return False
        log.info("motion (test): %s", cam.name)
        self.cam_status(cam)["last_motion"] = time.time()
        return self.capture.trigger(cam, "motion", force=True)

    def modes(self):
        """Each Arlo location's mode (Arm Away, Stand By...). No motion is reported in Stand By."""
        out = {}
        for loc in (getattr(self.arlo, "locations", None) or []) if self.arlo else []:
            try:
                out[loc.name] = loc.mode_name
            except Exception as e:
                out[loc.name] = f"unknown ({e})"
        return out

    def record_now(self, cam_id):
        cam = self.camera(cam_id)
        return bool(cam) and self.capture.trigger(cam, "manual", force=True)

    # --- lifecycle ----------------------------------------------------------
    def start(self, username, password):
        os.makedirs(config.CLIPS_DIR, exist_ok=True)
        os.makedirs(config.STATE_DIR, exist_ok=True)
        log.info("signing in to Arlo as %s", username)
        # 2FA: the code is typed into the web page unless IMAP is configured.
        tfa = arlo_login.WebTFA() if config.TFA_SOURCE == "console" else config.TFA_SOURCE
        arlo = pyaarlo.PyArlo(
            username=username,
            password=password,
            tfa_source=tfa,
            tfa_type=config.TFA_TYPE,
            tfa_host=config.TFA_HOST,
            tfa_username=config.TFA_USERNAME,
            tfa_password=config.TFA_PASSWORD,
            storage_dir=config.STATE_DIR,
            save_session=True,
            library_days=config.LIBRARY_DAYS,
            refresh_devices_every=3,
            stream_timeout=180,
            synchronous_mode=False,
        )
        if not arlo.is_connected:
            err = getattr(arlo, "last_error", None) or "sign-in failed - check email, password and code"
            try:
                arlo.stop()
            except Exception:
                pass
            raise RuntimeError(err)
        self.arlo = arlo

        self.status["connected"] = True
        self.status["cameras"] = [{"id": c.device_id, "name": c.name, "model": c.model_id}
                                  for c in self.arlo.cameras]
        log.info("connected; cameras: %s", ", ".join(c.name for c in self.arlo.cameras) or "none")

        if config.USE_BASE_STATION_STORAGE:
            for base in self.arlo.base_stations:
                # Wi-Fi cameras show up as their own "base station"; only real hubs have storage.
                if getattr(base, "device_type", "") != "basestation":
                    continue
                try:
                    if hasattr(base, "build_ratls") and base.ml is None:
                        base.build_ratls()
                        log.info("hub %s: local USB library enabled", base.name)
                except Exception as e:
                    log.info("hub %s: no local library (%s)", base.name, e)

        for cam in self.arlo.cameras:
            cam.add_attr_callback("motionDetected", self._on_motion)
            cam.add_attr_callback("lastCapture", self._on_capture)

        threading.Thread(target=self._poll_loop, name="poll", daemon=True).start()

    def stop(self):
        self._stop.set()
        if self.arlo:
            self.arlo.stop()

    # --- events -------------------------------------------------------------
    def _on_motion(self, device, attr, value):
        self._motion[device.device_id] = bool(value)
        if not value:
            return
        log.info("motion: %s", device.name)
        started = time.time()
        self.cam_status(device)["last_motion"] = started
        events.publish("motion", camera=device.name, camera_id=device.device_id,
                       started=int(started * 1000))
        if config.CAPTURE_LIVE:
            self.capture.trigger(device, "motion")
        s = notify.current()
        if s["on_motion"]:
            notify.send(device.device_id, "motion", f"Motion: {device.name}",
                        f"Motion detected at {datetime.now():%I:%M:%S %p}",
                        link=s["public_url"] or None,
                        extra={"camera": device.name, "event": "motion"})
        elif s["on_clip"] and s["motion_fallback"]:
            # Cameras with no subscription and no hub storage never produce a clip,
            # so alert on the motion itself if nothing shows up in time.
            t = threading.Timer(FALLBACK_SECONDS, self._fallback_alert, args=(device, started))
            t.daemon = True
            t.start()
        if config.CAPTURE_CLOUD:
            self._schedule_sweep(45)  # some systems never send lastCapture; look anyway

    def _fallback_alert(self, device, started):
        if config.CAPTURE_CLOUD:
            self.sweep(refresh=True)
        if self._last_clip.get(device.device_id, 0) >= started - 5:
            return
        s = notify.current()
        notify.send(device.device_id, "clip", f"Motion: {device.name}",
                    f"{datetime.fromtimestamp(started):%I:%M %p}, no recording available",
                    link=s["public_url"] or None,
                    extra={"camera": device.name, "event": "motion_no_clip"})

    def _on_capture(self, device, attr, value):
        # A new recording finished; Arlo needs a moment to publish it.
        log.info("new capture reported by %s", device.name)
        self._schedule_sweep(15)

    def _schedule_sweep(self, delay):
        due = time.time() + delay
        if self._sweep_timer and self._sweep_timer.is_alive():
            if self._sweep_due <= due:
                return
            self._sweep_timer.cancel()  # a sooner sweep was asked for
        self._sweep_due = due
        self._sweep_timer = threading.Timer(delay, self.sweep, kwargs={"refresh": True})
        self._sweep_timer.daemon = True
        self._sweep_timer.start()

    def _poll_loop(self):
        if config.CAPTURE_CLOUD:
            self.sweep(refresh=False)  # first run: back-fill LIBRARY_DAYS
        while not self._stop.wait(config.POLL_MINUTES * 60):
            if config.CAPTURE_CLOUD:
                self.sweep(refresh=True)
            try:
                store.cleanup()
            except Exception as e:
                log.warning("cleanup failed: %s", e)

    # --- downloading --------------------------------------------------------
    def _libraries(self):
        libs = [("cloud", self.arlo.ml)]
        if config.USE_BASE_STATION_STORAGE:
            for base in self.arlo.base_stations:
                ml = getattr(base, "ml", None)
                if ml is not None:
                    libs.append(("base_station", ml))
        return libs

    def sweep(self, refresh=True):
        if not self._sweep_lock.acquire(blocking=False):
            return
        try:
            for source, ml in self._libraries():
                if refresh:
                    try:
                        ml.update()
                    except Exception as e:
                        log.warning("%s library refresh failed: %s", source, e)
                _, videos = ml.videos
                for v in list(videos):
                    try:
                        self._save(v, source)
                    except Exception as e:
                        log.warning("could not save clip from %s: %s", getattr(v.camera, "name", "?"), e)
            self.status["last_sweep"] = time.time()
        finally:
            self._sweep_lock.release()

    def _save(self, video, source):
        cam = video.camera
        created = _created_ms(video)
        key = f"{cam.device_id}:{created}"
        if store.has(key):
            return

        when = datetime.fromtimestamp(created / 1000)
        rel_dir = os.path.join(_slug(cam.name), when.strftime("%Y-%m-%d"))
        os.makedirs(os.path.join(config.CLIPS_DIR, rel_dir), exist_ok=True)
        base_name = when.strftime("%H-%M-%S") + ("_usb" if source == "base_station" else "")
        rel_video = os.path.join(rel_dir, base_name + ".mp4")
        rel_thumb = os.path.join(rel_dir, base_name + ".jpg")
        abs_video = os.path.join(config.CLIPS_DIR, rel_video)
        abs_thumb = os.path.join(config.CLIPS_DIR, rel_thumb)

        with tempfile.TemporaryDirectory(dir=config.DATA_DIR) as tmp:
            raw = os.path.join(tmp, "raw.mp4")
            if not video.download_video(raw) or not os.path.exists(raw) or os.path.getsize(raw) == 0:
                raise RuntimeError("download returned nothing")
            duration = video.media_duration_seconds or _probe_duration(raw)
            if config.CLIP_MAX_SECONDS and duration and duration > config.CLIP_MAX_SECONDS + 1:
                _trim(raw, abs_video, config.CLIP_MAX_SECONDS)
                duration = float(config.CLIP_MAX_SECONDS)
            else:
                shutil.move(raw, abs_video)

        thumb_ok = False
        if source == "cloud" and video.thumbnail_url:
            try:
                thumb_ok = bool(video.download_thumbnail(abs_thumb)) and os.path.exists(abs_thumb)
            except Exception:
                thumb_ok = False
        if not thumb_ok:
            thumb_ok = _thumb_from_video(abs_video, abs_thumb)

        store.add({
            "key": key,
            "camera_id": cam.device_id,
            "camera_name": cam.name,
            "created_ms": created,
            "duration_s": duration,
            "trigger": video.triggered_by,
            "object_type": video.object_type,
            "source": source,
            "video_path": rel_video,
            "thumb_path": rel_thumb if thumb_ok else None,
            "size_bytes": os.path.getsize(abs_video),
            "starred": 0,
        })
        log.info("saved %s (%s, %.0fs)", rel_video, source, duration or 0)

        self.note_clip(cam.device_id)

        # Only alert on fresh clips, not the first-run back-fill.
        s = notify.current()
        if s["on_clip"] and time.time() * 1000 - created < 15 * 60 * 1000:
            what = video.object_type or "Motion"
            pub = (s["public_url"] or "").rstrip("/")
            link = f"{pub}/clip/{key}" if pub else None
            notify.send(cam.device_id, "clip", f"{what.title()}: {cam.name}",
                        f"Clip saved at {when:%I:%M:%S %p}",
                        link=link, thumb=abs_thumb if thumb_ok else None,
                        extra={"camera": cam.name, "event": "clip", "key": key})
