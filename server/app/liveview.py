"""Watch a camera live, on demand, from the BAN web page or phone app.

One live session per camera, shared by everyone watching it: it opens the camera's Arlo
live view, and ffmpeg turns it into JPEG pictures (about 8 a second). Each viewer gets them as
an MJPEG stream (multipart/x-mixed-replace), which plays in a plain <img> in every browser
and Android WebView, whatever video format the camera sends.

A session stops when nobody has been watching for a few seconds, or after MAX_SECONDS, so a
forgotten phone doesn't drain a battery camera. If the camera is already recording a motion
clip, viewers see that recording as it happens instead of opening a second stream.
"""
import logging
import subprocess
import threading
import time

log = logging.getLogger("liveview")

MAX_SECONDS = 180        # one session at most; the page offers "Keep watching"
IDLE_SECONDS = 5         # stop this long after the last viewer leaves
START_SECONDS = 25       # give a sleeping camera this long to send its first picture
FPS = 8

_sessions = {}
_lock = threading.Lock()


def _read_parts(stdout):
    """Yield JPEG frames from ffmpeg's mpjpeg output (each part has a Content-length)."""
    while True:
        length = None
        while True:
            line = stdout.readline()
            if not line:
                return
            line = line.strip()
            if not line:
                if length is not None:
                    break
                continue
            if line.lower().startswith(b"content-length:"):
                length = int(line.split(b":", 1)[1])
        data = stdout.read(length)
        if len(data) < length:
            return
        yield data


class Session(threading.Thread):
    def __init__(self, recorder, cam):
        super().__init__(daemon=True, name=f"live-{cam.name}")
        self.rec, self.cam = recorder, cam
        self.state = "starting"       # starting, live, ended, error
        self.error = None
        self.frame, self.frame_no = None, 0
        self.started = time.time()
        self.viewers = 0
        self.last_viewer = time.time()
        self.cond = threading.Condition()
        self.proc = None
        self.opened_stream = False
        self.following = False      # showing a motion clip as it records (ends with the clip)
        self._stop = threading.Event()

    # --- for viewers ---
    def seconds_left(self):
        return max(0, int(MAX_SECONDS - (time.time() - self.started)))

    def frames(self):
        """Generator of multipart chunks for one viewer."""
        with self.cond:
            self.viewers += 1
        last = 0
        try:
            while True:
                with self.cond:
                    if self.frame_no == last and self.state in ("starting", "live"):
                        self.cond.wait(timeout=2)
                    if self.state in ("ended", "error") and self.frame_no == last:
                        return
                    frame, last = self.frame, self.frame_no
                if frame is not None and last:
                    yield (b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                           + str(len(frame)).encode() + b"\r\n\r\n" + frame + b"\r\n")
        finally:
            with self.cond:
                self.viewers -= 1
                self.last_viewer = time.time()

    def stop(self):
        self._stop.set()
        if self.proc and self.proc.poll() is None:
            self.proc.kill()

    # --- the session itself ---
    def run(self):
        try:
            self._run()
        except Exception as e:
            log.warning("%s: live view failed: %s", self.cam.name, e)
            self.error = str(e)
        finally:
            with self.cond:
                self.state = "error" if self.error and not self.frame_no else "ended"
                self.cond.notify_all()
            if self.opened_stream and not self.rec.capture.busy(self.cam.device_id):
                try:
                    self.cam.stop_stream()
                except Exception:
                    pass
            with _lock:
                if _sessions.get(self.cam.device_id) is self:
                    del _sessions[self.cam.device_id]
            log.info("%s: live view ended", self.cam.name)

    def _source(self):
        """(ffmpeg input args) for the camera: its recording in progress, or a new live stream."""
        recording = self.rec.capture.recording_file(self.cam.device_id)
        if recording:
            log.info("%s: live view follows the clip being recorded", self.cam.name)
            self.following = True
            return ["-follow", "1", "-i", recording]
        url = self.rec.capture.open_stream(self.cam)
        if not url:
            raise RuntimeError("Arlo didn't start a live stream. Is the camera online?")
        self.opened_stream = True
        args = []
        if url.startswith(("rtsp://", "rtsps://")):
            args += ["-rtsp_transport", "tcp", "-timeout", "10000000"]
        return args + ["-probesize", "1000000", "-analyzeduration", "1500000", "-i", url]

    def _run(self):
        log.info("%s: live view starting", self.cam.name)
        src = self._source()
        deadline = time.time() + START_SECONDS
        while not self._stop.is_set():
            remaining = max(5, int(MAX_SECONDS - (time.time() - self.started)))
            if self.following:
                remaining = min(remaining, 30)  # the clip is 20 s; "Keep watching" opens the live view
            cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", *src, "-t", str(remaining), "-an",
                   "-vf", f"fps={FPS},scale='min(960,iw)':-2", "-q:v", "6", "-f", "mpjpeg", "pipe:1"]
            self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            threading.Thread(target=self._watch_viewers, daemon=True).start()
            for jpg in _read_parts(self.proc.stdout):
                with self.cond:
                    self.frame, self.frame_no, self.state = jpg, self.frame_no + 1, "live"
                    self.cond.notify_all()
                if self._stop.is_set():
                    break
            self.proc.wait()
            err = self.proc.stderr.read().decode(errors="replace").strip().splitlines()
            if self.frame_no or self._stop.is_set():
                return                      # played and finished (time up, or nobody watching)
            if time.time() > deadline:     # never got a picture
                last = err[-1] if err else "no picture"
                if "403" in last:
                    raise RuntimeError("Arlo refused the live stream (403). Give this login access "
                                       "rights to the camera and its hub in the Arlo app.")
                raise RuntimeError(f"No picture from the camera ({last})")
            time.sleep(1.5)                 # camera still waking up; try again

    def _watch_viewers(self):
        proc = self.proc
        while proc.poll() is None:
            time.sleep(1)
            with self.cond:
                idle = self.viewers == 0 and time.time() - self.last_viewer > IDLE_SECONDS
            if idle:
                self.stop()
                return


def start(recorder, cam):
    """The camera's live session, started if needed."""
    with _lock:
        s = _sessions.get(cam.device_id)
        if s and s.state in ("starting", "live") and s.seconds_left() > 3:
            return s
        if s:
            s.stop()
        s = _sessions[cam.device_id] = Session(recorder, cam)
        s.start()
        return s


def get(cam_id):
    with _lock:
        return _sessions.get(cam_id)


def viewing(cam_id):
    s = get(cam_id)
    return bool(s and s.state in ("starting", "live"))
