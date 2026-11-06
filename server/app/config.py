"""Settings, all read from environment variables (see .env.example)."""
import os


def _bool(name, default=False):
    return os.environ.get(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


def _int(name, default):
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


# --- Arlo account -----------------------------------------------------------
ARLO_USERNAME = os.environ.get("ARLO_USERNAME", "")
ARLO_PASSWORD = os.environ.get("ARLO_PASSWORD", "")

# 2FA: "console" (type the code in the container log/terminal on first run),
# "imap" (read the code from an email inbox automatically).
TFA_SOURCE = os.environ.get("TFA_SOURCE", "console")
TFA_TYPE = os.environ.get("TFA_TYPE", "email")
TFA_HOST = os.environ.get("TFA_HOST", "imap.gmail.com")
TFA_USERNAME = os.environ.get("TFA_USERNAME") or None
TFA_PASSWORD = os.environ.get("TFA_PASSWORD") or None

# --- Storage ----------------------------------------------------------------
DATA_DIR = os.environ.get("DATA_DIR", "/data")
CLIPS_DIR = os.path.join(DATA_DIR, "clips")
STATE_DIR = os.path.join(DATA_DIR, "state")
DB_PATH = os.path.join(DATA_DIR, "clips.db")

# Clips longer than this are trimmed after download (0 = never trim).
CLIP_MAX_SECONDS = _int("CLIP_MAX_SECONDS", 20)
# Delete clips older than this many days (0 = keep forever).
KEEP_DAYS = _int("KEEP_DAYS", 90)
# Delete oldest clips once the folder passes this size in GB (0 = no cap).
MAX_STORAGE_GB = _int("MAX_STORAGE_GB", 0)

# Record clips ourselves from live view when a camera sees motion
# (the only way to get clips from cameras with no subscription and no hub storage).
CAPTURE_LIVE = _bool("CAPTURE_LIVE", True)
# Also download any clips Arlo itself saved (cloud plan or older free storage).
CAPTURE_CLOUD = _bool("CAPTURE_CLOUD", True)
# Minimum seconds between live captures on the same camera (saves battery).
CAPTURE_COOLDOWN_SECONDS = _int("CAPTURE_COOLDOWN_SECONDS", 30)
# Convert Arlo's HEVC video to H.264 so clips play in every browser.
TRANSCODE_H264 = _bool("TRANSCODE_H264", True)

# How many days back to pull on the first run.
LIBRARY_DAYS = _int("LIBRARY_DAYS", 7)
# Safety-net sweep interval, in minutes, for anything the event stream missed.
POLL_MINUTES = _int("POLL_MINUTES", 5)
# Also pull clips recorded to a base station's USB drive (older base stations).
USE_BASE_STATION_STORAGE = _bool("USE_BASE_STATION_STORAGE", True)

# --- Notifications ----------------------------------------------------------
NOTIFY_ON_MOTION = _bool("NOTIFY_ON_MOTION", False)   # instant, before the clip exists
NOTIFY_ON_CLIP = _bool("NOTIFY_ON_CLIP", True)        # once the clip is saved
NOTIFY_COOLDOWN_SECONDS = _int("NOTIFY_COOLDOWN_SECONDS", 60)  # per camera

NTFY_URL = os.environ.get("NTFY_URL", "")            # e.g. https://ntfy.sh/my-secret-topic
NTFY_TOKEN = os.environ.get("NTFY_TOKEN", "")

PUSHOVER_TOKEN = os.environ.get("PUSHOVER_TOKEN", "")
PUSHOVER_USER = os.environ.get("PUSHOVER_USER", "")

TWILIO_SID = os.environ.get("TWILIO_SID", "")
TWILIO_TOKEN = os.environ.get("TWILIO_TOKEN", "")
TWILIO_FROM = os.environ.get("TWILIO_FROM", "")      # +1XXXXXXXXXX
SMS_TO = os.environ.get("SMS_TO", "")                # +1XXXXXXXXXX, comma-separate for several

WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "")      # generic JSON POST (Home Assistant, Discord bridge, etc.)

# Link included in notifications, e.g. http://192.168.5.50:8080
PUBLIC_URL = os.environ.get("PUBLIC_URL", "").rstrip("/")

# --- Web viewer -------------------------------------------------------------
WEB_PORT = _int("WEB_PORT", 8080)
WEB_USER = os.environ.get("WEB_USER", "")
WEB_PASSWORD = os.environ.get("WEB_PASSWORD", "")

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")
