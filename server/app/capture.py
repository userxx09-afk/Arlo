"""Live capture: when a camera sees motion, open its live view and record a clip.

Without an Arlo subscription the cameras don't upload recordings anywhere, but
live view still works. So on motion we start a stream, record CLIP_MAX_SECONDS
with ffmpeg, convert it to H.264 (Arlo sends HEVC, which most browsers can't
play), make a thumbnail and save it like any other clip.
"""
import logging
import os
import shutil
import subprocess
import tempfile
import threading
import time
from datetime import datetime

from . import config, events, garystv, liveview, notify, store

log = logging.getLogger("capture")

STREAM_READY_SECONDS = 15   # how long to keep trying to connect to a waking camera
MAX_CHAIN = 3               # back-to-back clips while motion keeps going
LIVE_DIR = os.path.join(config.DATA_DIR, "live")  # clips as they record, for GarysTV's pop-up
LIVE_KEEP_SECONDS = 300     # how long a finished clip's live copy stays
_live_dir_cleared = False


def slug(name):
    return "".join(ch if ch.isalnum() else "_" for ch in name).strip("_") or "camera"


def _ffmpeg_record(url, out, seconds, live=None):
    cmd = ["ffmpeg", "-y", "-loglevel", "error"]
    if url.startswith(("rtsp://", "rtsps://")):
        cmd += ["-rtsp_transport", "tcp", "-timeout", "10000000"]
    # Look at the stream for ~1.5s instead of ffmpeg's default 5s before recording, so the
    # clip (and GarysTV's pop-up) starts sooner. Arlo's stream says up front what's in it.
    cmd += ["-probesize", "1000000", "-analyzeduration", "1500000"]
    cmd += ["-i", url, "-t", str(seconds), "-map", "0:v:0", "-map", "0:a?", "-c", "copy",
            "-f", "mp4", out]
    if live:
        # The same picture as it arrives, readable while it's still recording (MP4 isn't
        # until it's finished). GarysTV shows it on the TV; no sound needed.
        cmd += ["-t", str(seconds), "-map", "0:v:0", "-c", "copy", "-f", "mpegts", live]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=seconds + 40)
    ok = r.returncode == 0 and os.path.exists(out) and os.path.getsize(out) > 20_000
    return ok, (r.stderr.strip().splitlines() or ["no output"])[-1]


def _to_h264(src, dst):
    """Convert to H.264/AAC at most 1280 wide so it plays in any browser and stays small."""
    if not config.TRANSCODE_H264:
        shutil.copyfile(src, dst)
        return
    r = subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", src,
         "-vf", "scale='min(1280,iw)':-2", "-c:v", "libx264", "-preset", "veryfast", "-crf", "26",
         "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "64k", "-movflags", "+faststart", dst],
        capture_output=True, text=True, timeout=300)
    if r.returncode != 0:
        log.warning("convert failed (%s), keeping original", r.stderr.strip()[-120:])
        shutil.copyfile(src, dst)


def _probe_duration(path):
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                              "-of", "csv=p=0", path], capture_output=True, text=True, timeout=30)
        return float(out.stdout.strip())
    except Exception:
        return None


def clip_paths(cam, started):
    """Where a clip that started at `started` is saved: (key, rel_video, rel_thumb)."""
    when = datetime.fromtimestamp(started)
    rel_dir = os.path.join(slug(cam.name), when.strftime("%Y-%m-%d"))
    base = when.strftime("%H-%M-%S")
    return (f"{cam.device_id}:{int(started * 1000)}", os.path.join(rel_dir, base + ".mp4"),
            os.path.join(rel_dir, base + ".jpg"))


def _thumb(video, jpg):
    try:
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", "2", "-i", video,
                        "-frames:v", "1", "-vf", "scale=480:-2", jpg], check=True, timeout=60)
        return os.path.exists(jpg)
    except Exception:
        return False


class LiveCapture:
    def __init__(self, recorder):
        self.rec = recorder
        self._busy = set()
        self._last_start = {}
        self._lock = threading.Lock()
        self.live = {}  # live id -> {"path", "done"}: clips recording now (or just finished)
        global _live_dir_cleared
        if not _live_dir_cleared:   # once per start-up (there's one LiveCapture per Arlo account)
            shutil.rmtree(LIVE_DIR, ignore_errors=True)
            _live_dir_cleared = True
        os.makedirs(LIVE_DIR, exist_ok=True)

    def live_file(self, live_id):
        """(path, still recording?) for a live id, or (None, False)."""
        item = self.live.get(live_id)
        return (item["path"], not item["done"]) if item else (None, False)

    def trigger(self, cam, reason="motion", chain=0, force=False):
        """Start a capture in the background. Returns False if this camera is already busy."""
        now = time.time()
        with self._lock:
            if cam.device_id in self._busy:
                return False
            if not force and now - self._last_start.get(cam.device_id, 0) < config.CAPTURE_COOLDOWN_SECONDS:
                return False
            self._busy.add(cam.device_id)
            self._last_start[cam.device_id] = now
        threading.Thread(target=self._run, args=(cam, reason, chain), daemon=True,
                         name=f"capture-{cam.name}").start()
        return True

    def busy(self, cam_id):
        return cam_id in self._busy

    def recording_file(self, cam_id):
        """The .ts file of a clip this camera is recording right now, if any (for live view)."""
        for live_id, item in list(self.live.items()):
            if live_id.startswith(cam_id + "-") and not item["done"] and os.path.exists(item["path"]):
                return item["path"]
        return None

    def open_stream(self, cam):
        return self._open_stream(cam)

    def _stop_stream(self, cam):
        # Someone is watching this camera live in BAN: leave the stream running for them.
        if liveview.viewing(cam.device_id):
            return
        try:
            cam.stop_stream()
        except Exception:
            pass

    def _run(self, cam, reason, chain):
        st = self.rec.cam_status(cam)
        st.update(capturing=True, last_error=None)
        started = time.time()
        ok = False
        try:
            path = self._capture(cam, started, reason, tell_garystv=(reason == "motion" and chain == 0))
            st.update(last_clip=time.time(), last_clip_key=path)
            ok = True
        except Exception as e:
            st["last_error"] = str(e)
            log.warning("%s: capture failed: %s", cam.name, e)
        finally:
            st["capturing"] = False
            with self._lock:
                self._busy.discard(cam.device_id)

        # Motion still going? Keep recording, a few clips at most.
        if ok and reason == "motion" and chain + 1 < MAX_CHAIN and self.rec.motion_active(cam.device_id):
            self.trigger(cam, "motion", chain + 1, force=True)

    def _capture(self, cam, started, reason, tell_garystv=False):
        log.info("%s: recording %ds (%s)", cam.name, config.CLIP_MAX_SECONDS, reason)
        live_id = f"{cam.device_id}-{int(started * 1000)}"
        live = os.path.join(LIVE_DIR, live_id + ".ts")
        self.live[live_id] = {"path": live, "done": False}
        if tell_garystv:
            self._tell_garystv(cam, started, live_id)
        try:
            return self._capture_stream(cam, started, reason, live)
        finally:
            self.live[live_id]["done"] = True
            t = threading.Timer(LIVE_KEEP_SECONDS, self._drop_live, args=(live_id,))
            t.daemon = True
            t.start()

    def _drop_live(self, live_id):
        item = self.live.pop(live_id, None)
        if item:
            try:
                os.remove(item["path"])
            except OSError:
                pass

    def _tell_garystv(self, cam, started, live_id):
        """Tell GarysTV a motion clip is starting: camera, time, and where the clip goes.

        Sent straight away: Arlo cameras take ~10s to send their first picture, so GarysTV
        shows the camera name at once and the picture as it arrives (from `live`).
        """
        key, rel_video, _ = clip_paths(cam, started)
        garystv.motion_started({
            "event": "motion", "camera": cam.name, "camera_id": cam.device_id,
            "time": int(started * 1000), "key": key, "file": rel_video,
            "live": f"/live/{live_id}.ts", "clip": f"/media/{rel_video}",
        })

    def _open_stream(self, cam):
        """Start the camera's live view and return its address (None if Arlo won't).

        On motion the camera starts recording its own clip ("alertStreamActive"). pyaarlo then
        counts the camera as already streaming and hands back no address, which is why motion
        clips failed while "Record test clip" (camera idle) worked. Clear that and ask Arlo.
        """
        deadline = time.time() + STREAM_READY_SECONDS
        while True:
            url = cam.get_stream()
            if url:
                return url
            lock, users = getattr(cam, "_lock", None), getattr(cam, "_local_users", None)
            if lock is not None and users is not None and not getattr(cam, "_stream_url", None):
                with lock:
                    # "streaming" too: the failed call above left it behind with no stream.
                    users.discard("remote")
                    users.discard("streaming")
            if time.time() > deadline:
                return None
            log.info("%s: no live stream yet (camera busy recording?), asking again", cam.name)
            time.sleep(1.5)

    def _capture_stream(self, cam, started, reason, live):
        url = self._open_stream(cam)
        if not url:
            raise RuntimeError("Arlo didn't start a live stream")
        try:
            if hasattr(cam, "wait_for_user_stream"):
                try:
                    cam.wait_for_user_stream(timeout=10)
                except Exception:
                    pass
            with tempfile.TemporaryDirectory(dir=config.DATA_DIR) as tmp:
                raw = os.path.join(tmp, "raw.mp4")
                deadline = time.time() + STREAM_READY_SECONDS
                ok, err = False, ""
                while not ok:
                    log.debug("%s: ffmpeg starting", cam.name)
                    ok, err = _ffmpeg_record(url, raw, config.CLIP_MAX_SECONDS, live)
                    if not ok:
                        log.debug("%s: ffmpeg attempt failed: %s", cam.name, err)
                    if ok or time.time() > deadline:
                        break
                    time.sleep(1.5)
                self._stop_stream(cam)
                if not ok:
                    if "403" in err:
                        raise RuntimeError("Arlo refused the live stream (403). Give this login "
                                           "access rights to the camera and its hub in the Arlo app.")
                    raise RuntimeError(f"couldn't record the live stream: {err}")
                return self._save(cam, raw, started, reason)
        finally:
            self._stop_stream(cam)

    def _save(self, cam, raw, started, reason):
        when = datetime.fromtimestamp(started)
        created = int(started * 1000)
        key, rel_video, rel_thumb = clip_paths(cam, started)
        os.makedirs(os.path.join(config.CLIPS_DIR, os.path.dirname(rel_video)), exist_ok=True)
        abs_video = os.path.join(config.CLIPS_DIR, rel_video)
        abs_thumb = os.path.join(config.CLIPS_DIR, rel_thumb)

        _to_h264(raw, abs_video)
        thumb_ok = _thumb(abs_video, abs_thumb)
        store.add({
            "key": key, "camera_id": cam.device_id, "camera_name": cam.name,
            "created_ms": created, "duration_s": _probe_duration(abs_video),
            "trigger": reason, "object_type": None, "source": "live",
            "video_path": rel_video, "thumb_path": rel_thumb if thumb_ok else None,
            "size_bytes": os.path.getsize(abs_video), "starred": 0,
        })
        log.info("%s: saved %s", cam.name, rel_video)
        self.rec.note_clip(cam.device_id)
        events.publish("clip", camera=cam.name, camera_id=cam.device_id, key=key, reason=reason,
                       started=created, video=f"/media/{rel_video}",
                       thumb=f"/media/{rel_thumb}" if thumb_ok else None)

        s = notify.current()
        if reason == "motion" and s["on_clip"]:
            pub = (s["public_url"] or "").rstrip("/")
            notify.send(cam.device_id, "clip", f"Motion: {cam.name}", f"Clip saved at {when:%I:%M:%S %p}",
                        link=f"{pub}/clip/{key}" if pub else None,
                        thumb=abs_thumb if thumb_ok else None,
                        extra={"camera": cam.name, "event": "clip", "key": key})
        return key
