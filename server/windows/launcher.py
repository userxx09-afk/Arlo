"""Runs Camera Keeper on Windows, the way systemd runs it on Ubuntu.

Started at boot by the "Camera Keeper" scheduled task (as SYSTEM, no window). It:
  - reads settings from %ProgramData%\\CameraKeeper\\camera-keeper.env (KEY=VALUE lines)
  - puts the bundled ffmpeg first on PATH
  - starts `python -m app.main`, restarts it 10 s after it stops
  - writes its output to %ProgramData%\\CameraKeeper\\logs\\camera-keeper.log (kept under ~10 MB)
  - ties the app and its ffmpeg processes to itself, so ending the task stops all of them
"""
import os
import socket
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PROGRAM_DATA = os.environ.get("ProgramData", r"C:\ProgramData")
DATA_DIR = os.path.join(PROGRAM_DATA, "CameraKeeper")
SETTINGS = os.path.join(DATA_DIR, "camera-keeper.env")
LOG_DIR = os.path.join(DATA_DIR, "logs")
LOG = os.path.join(LOG_DIR, "camera-keeper.log")
LOG_MAX = 5 * 1024 * 1024      # two files of this size: camera-keeper.log and camera-keeper.log.1


def read_settings(path):
    out = {}
    try:
        with open(path, encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                v = v.split(" #", 1)[0].strip().strip('"').strip("'")
                out[k.strip()] = v
    except FileNotFoundError:
        pass
    return out


def local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("192.0.2.1", 80))     # no packet is sent; just picks the outgoing interface
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "localhost"


class Log:
    def __init__(self):
        os.makedirs(LOG_DIR, exist_ok=True)
        self.f = open(LOG, "ab")

    def write(self, data):
        self.f.write(data)
        self.f.flush()
        if self.f.tell() > LOG_MAX:
            self.f.close()
            try:
                os.replace(LOG, LOG + ".1")
            except OSError:
                pass
            self.f = open(LOG, "ab")

    def note(self, msg):
        self.write(time.strftime("%Y-%m-%d %H:%M:%S ").encode() + b"launcher: " + msg.encode() + b"\r\n")


def kill_with_me_job():
    """A job object that ends every process in it when this launcher exits."""
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [(n, ctypes.c_ulonglong) for n in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class BASIC(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class EXTENDED(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", BASIC), ("IoInfo", IO_COUNTERS),
                    ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateJobObjectW.restype = wintypes.HANDLE
    k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    job = k32.CreateJobObjectW(None, None)
    if not job:
        return None
    info = EXTENDED()
    info.BasicLimitInformation.LimitFlags = 0x2000          # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):
        return None
    return lambda proc: k32.AssignProcessToJobObject(job, wintypes.HANDLE(int(proc._handle)))


def move_old_data():
    """Before the rename to Camera Keeper the data lived in %ProgramData%\\ArloKeeper. Setup moves it;
    if a locked file stopped that, try again here, before anything is created in the new folder."""
    old = os.path.join(PROGRAM_DATA, "ArloKeeper")
    if os.path.isdir(old) and not os.path.exists(DATA_DIR):
        try:
            os.rename(old, DATA_DIR)
        except OSError:
            return
        old_env = os.path.join(DATA_DIR, "arlo-keeper.env")
        if os.path.exists(old_env) and not os.path.exists(SETTINGS):
            os.rename(old_env, SETTINGS)


def main():
    move_old_data()
    os.makedirs(DATA_DIR, exist_ok=True)
    log = Log()
    env = dict(os.environ)
    env.update(read_settings(SETTINGS))
    env.setdefault("DATA_DIR", DATA_DIR)
    env.setdefault("WEB_PORT", "8090")
    if not env.get("PUBLIC_URL"):
        env["PUBLIC_URL"] = "http://%s:%s" % (local_ip(), env["WEB_PORT"])
    env["PATH"] = os.path.join(HERE, "ffmpeg") + os.pathsep + env.get("PATH", "")
    env["SETTINGS_FILE"] = SETTINGS                     # the Move page's import adds settings here
    env["CLOUDFLARED"] = os.path.join(HERE, "cloudflared", "cloudflared.exe")   # for bcn.tapper.co
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"

    # python.exe (not pythonw) so the app's output can be read; no window is shown.
    py = os.path.join(os.path.dirname(sys.executable), "python.exe")
    if not os.path.exists(py):
        py = sys.executable
    add_to_job = kill_with_me_job()
    flags = 0x08000000 if os.name == "nt" else 0              # CREATE_NO_WINDOW

    while True:
        log.note("starting Camera Keeper (data in %s, port %s)" % (env["DATA_DIR"], env["WEB_PORT"]))
        proc = subprocess.Popen([py, "-m", "app.main"], cwd=HERE, env=env,
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, creationflags=flags)
        if add_to_job:
            add_to_job(proc)
        for line in proc.stdout:
            log.write(line.rstrip(b"\r\n") + b"\r\n")
        code = proc.wait()
        log.note("Camera Keeper stopped (exit code %s); starting again in 10 seconds" % code)
        time.sleep(10)


if __name__ == "__main__":
    main()
