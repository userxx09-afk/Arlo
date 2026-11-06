"""Send alerts to whichever channels are configured. Every channel is optional.

Settings are edited on the web page (/settings) and saved in the database.
Environment variables are only used as starting values.
"""
import logging
import re
import threading
import time

import requests

from . import config, store

log = logging.getLogger("notify")
_last_sent = {}
_lock = threading.Lock()

SETTINGS_KEY = "notify"
SECRET_FIELDS = ("twilio_token", "ntfy_token", "pushover_token")


def normalize_number(raw):
    """Turn '773-555-0123', '(773) 555 0123', '17735550123' into '+17735550123'.

    Returns None if it isn't a usable number. 10 digits are assumed US/Canada.
    """
    raw = (raw or "").strip()
    digits = re.sub(r"\D", "", raw)
    if raw.startswith("+") and 8 <= len(digits) <= 15:
        return "+" + digits
    if len(digits) == 10 and digits[0] not in "01":
        return "+1" + digits
    if len(digits) == 11 and digits[0] == "1" and digits[1] not in "01":
        return "+" + digits
    return None


def pretty_number(e164):
    d = re.sub(r"\D", "", e164 or "")
    if len(d) == 11 and d[0] == "1":
        return f"({d[1:4]}) {d[4:7]}-{d[7:]}"
    return e164


def _defaults():
    numbers = []
    for n in (config.SMS_TO or "").split(","):
        e = normalize_number(n)
        if e:
            numbers.append({"number": e, "label": "", "enabled": True})
    return {
        "numbers": numbers,
        "twilio_sid": config.TWILIO_SID, "twilio_token": config.TWILIO_TOKEN,
        "twilio_from": normalize_number(config.TWILIO_FROM) or config.TWILIO_FROM,
        "ntfy_url": config.NTFY_URL, "ntfy_token": config.NTFY_TOKEN,
        "pushover_token": config.PUSHOVER_TOKEN, "pushover_user": config.PUSHOVER_USER,
        "webhook_url": config.WEBHOOK_URL,
        "public_url": config.PUBLIC_URL,
        "on_clip": config.NOTIFY_ON_CLIP, "on_motion": config.NOTIFY_ON_MOTION,
        "motion_fallback": True,
        "cooldown": config.NOTIFY_COOLDOWN_SECONDS,
    }


def current():
    s = _defaults()
    s.update(store.get_setting(SETTINGS_KEY, {}) or {})
    return s


def save(s):
    store.set_setting(SETTINGS_KEY, s)


def sms_ready(s=None):
    s = s or current()
    return bool(s["twilio_sid"] and s["twilio_token"] and s["twilio_from"])


def active_numbers(s=None):
    s = s or current()
    return [n["number"] for n in s["numbers"] if n.get("enabled", True)]


def has_any_channel(s=None):
    s = s or current()
    return bool((active_numbers(s) and sms_ready(s)) or s["ntfy_url"]
                or (s["pushover_token"] and s["pushover_user"]) or s["webhook_url"])


# --- channels ---------------------------------------------------------------
def _ntfy(s, title, body, link, thumb):
    headers = {"Title": title, "Tags": "rotating_light"}
    if s["ntfy_token"]:
        headers["Authorization"] = f"Bearer {s['ntfy_token']}"
    if link:
        headers["Click"] = link
    if thumb:
        headers["Filename"] = "snapshot.jpg"
        headers["Message"] = body
        with open(thumb, "rb") as f:
            requests.put(s["ntfy_url"], data=f, headers=headers, timeout=15).raise_for_status()
    else:
        requests.post(s["ntfy_url"], data=body.encode(), headers=headers, timeout=15).raise_for_status()


def _pushover(s, title, body, link, thumb):
    data = {"token": s["pushover_token"], "user": s["pushover_user"], "title": title, "message": body}
    if link:
        data["url"] = link
    files = {"attachment": ("snapshot.jpg", open(thumb, "rb"), "image/jpeg")} if thumb else None
    requests.post("https://api.pushover.net/1/messages.json", data=data, files=files,
                  timeout=15).raise_for_status()


def _sms_one(s, to, text):
    r = requests.post(
        f"https://api.twilio.com/2010-04-01/Accounts/{s['twilio_sid']}/Messages.json",
        auth=(s["twilio_sid"], s["twilio_token"]),
        data={"From": s["twilio_from"], "To": to, "Body": text[:320]},
        timeout=15,
    )
    if r.status_code >= 400:
        try:
            msg = r.json().get("message", r.text)
        except ValueError:
            msg = r.text
        raise RuntimeError(f"Twilio {r.status_code}: {msg}")


def _webhook(s, title, body, link, thumb, extra):
    requests.post(s["webhook_url"], json={"title": title, "message": body, "url": link, **extra},
                  timeout=15).raise_for_status()


def _deliver(s, title, body, link=None, thumb=None, extra=None):
    """Send on every configured channel. Returns [(channel, ok, detail)]."""
    results = []

    def attempt(name, fn):
        try:
            fn()
            results.append((name, True, "sent"))
        except Exception as e:  # one failing channel shouldn't block the others
            log.warning("%s notification failed: %s", name, e)
            results.append((name, False, str(e)))

    numbers = active_numbers(s)
    if numbers:
        if sms_ready(s):
            text = f"{title}: {body}" + (f" {link}" if link else "")
            for n in numbers:
                attempt(f"text {pretty_number(n)}", lambda n=n: _sms_one(s, n, text))
        else:
            results.append(("text", False, "Twilio account not set up yet"))
    if s["ntfy_url"]:
        attempt("ntfy", lambda: _ntfy(s, title, body, link, thumb))
    if s["pushover_token"] and s["pushover_user"]:
        attempt("pushover", lambda: _pushover(s, title, body, link, thumb))
    if s["webhook_url"]:
        attempt("webhook", lambda: _webhook(s, title, body, link, thumb, extra or {}))
    return results


def send(camera_id, kind, title, body, link=None, thumb=None, extra=None):
    """kind is 'motion' or 'clip'; cooldown is tracked per camera per kind."""
    s = current()
    key = (camera_id, kind)
    now = time.time()
    with _lock:
        if now - _last_sent.get(key, 0) < int(s["cooldown"] or 0):
            return []
        _last_sent[key] = now
    return _deliver(s, title, body, link, thumb, extra)


def send_test():
    s = current()
    if not has_any_channel(s) and not active_numbers(s):
        return [("setup", False, "Add a phone number or another channel first")]
    return _deliver(s, "Arlo Keeper test", "Alerts are working.", link=s["public_url"] or None,
                    extra={"event": "test"})
