"""Laview and other IP cameras, next to the Arlo and Blink ones.

Laview sells cameras and recorders built on Hikvision and on Dahua hardware, and their newer
cameras speak ONVIF. So one camera here is "anything with a local video stream", and BAN works
out how to talk to it:

  * Hikvision-type (most Laview IP cameras and NVRs): ISAPI on the web port
    (/ISAPI/System/deviceInfo), video at rtsp://.../Streaming/Channels/101 (102 = small),
    motion from /ISAPI/Event/notification/alertStream.
  * Dahua-type (Laview "Dahua inside" models): /cgi-bin/magicBox.cgi, video at
    rtsp://.../cam/realmonitor?channel=1&subtype=0, motion from /cgi-bin/eventManager.cgi.
  * Anything ONVIF (Profile S/T): the video address from GetStreamUri, motion from a
    PullPoint subscription.
  * Plain RTSP: a list of the usual addresses is tried, or one typed in by hand.
If the camera can't tell BAN about motion (or that's turned off), BAN watches the small stream
itself with ffmpeg's scene detection.

On motion it is the same as an Arlo camera: capture.LiveCapture records a clip from the stream
(converted to H.264, so HEVC cameras play everywhere), GarysTV gets the pop-up while it records,
the phone app and the alerts go out, and Live view works from the page.

Each camera is a dict in the "ip_cameras" setting: id, name, host, http_port, rtsp_port,
onvif_port, username, password, kind (auto|hikvision|dahua|onvif|rtsp), channel, rtsp_url and
sub_url (optional, typed in), motion (auto|camera|video|off), sensitivity (low|medium|high).
What was found out about it is kept in "found" so a restart doesn't probe everything again.
"""
import base64
import hashlib
import logging
import os
import re
import secrets
import socket
import subprocess
import threading
import time
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urlsplit, urlunsplit

import requests
from requests.auth import HTTPBasicAuth, HTTPDigestAuth

from . import config, events, notify, store
from .capture import LiveCapture

log = logging.getLogger("ipcam")

KEY = "ip_cameras"
BRAND = "ipcam"
ACCOUNT = "Laview & IP cameras"
MOTION_HOLD = 12            # seconds motion still counts as going after the camera last said so
RETRY_SECONDS = (15, 30, 60, 120, 300)
NO_WINDOW = 0x08000000 if os.name == "nt" else 0
THRESHOLDS = {"low": 0.035, "medium": 0.018, "high": 0.009}   # ffmpeg scene score for "something moved"

# Event names that mean "something is moving", per camera family (lower case).
HIK_MOTION = {"vmd", "motion", "fielddetection", "linedetection", "regionentrance", "regionexiting",
              "pir", "humandetection", "vehicledetection", "facedetection", "smartdetection",
              "pedestrian", "intrusion", "loitering"}
DAHUA_MOTION = {"videomotion", "smartmotionhuman", "smartmotionvehicle", "crosslinedetection",
                "crossregiondetection", "intelliframe", "leftdetection", "parkingdetection",
                "videomotioninfo", "humantrait", "faceDetection".lower()}
ONVIF_MOTION = re.compile(r"motion|intrusion|linedetector|fielddetector|linecross|objectdetection|"
                          r"human|person|people|vehicle|pir|tripwire|loiter", re.I)


# --- saved cameras --------------------------------------------------------------------
_lock = threading.Lock()


def cameras():
    return store.get_setting(KEY) or []


def _save(cams):
    store.set_setting(KEY, cams)


def get(cid):
    return next((c for c in cameras() if c["id"] == cid), None)


def _slug(s):
    return re.sub(r"[^A-Za-z0-9]+", "-", str(s or "")).strip("-").lower() or "cam"


def clean_host(h):
    """'http://192.168.4.50:8000/' -> ('192.168.4.50', 8000); '192.168.4.50' -> (..., None)."""
    h = (h or "").strip()
    if "://" in h:
        u = urlsplit(h)
        return u.hostname or "", u.port
    if h.count(":") == 1:
        host, port = h.split(":")
        return host, int(port) if port.isdigit() else None
    return h.strip("/"), None


def add(form):
    """Add a camera (or one per channel of a recorder). Returns the new ids."""
    host, port = clean_host(form.get("host"))
    if not host and not form.get("rtsp_url"):
        raise ValueError("Enter the camera's address (like 192.168.4.60) or its RTSP address.")
    if not host:
        host = urlsplit(form["rtsp_url"]).hostname or ""
    name = (form.get("name") or "").strip() or f"Camera {host}"
    try:
        channels = max(1, min(64, int(form.get("channels") or 1)))
        first = max(1, int(form.get("channel") or 1))
    except ValueError:
        raise ValueError("Channel and number of channels are numbers.")

    def num(k, default=None):
        v = str(form.get(k) or "").strip()
        return int(v) if v.isdigit() else default

    ids = []
    with _lock:
        cams = cameras()
        for n in range(first, first + channels):
            cid = f"ip-{_slug(host)}-{n}"
            base = {"id": cid, "name": name if channels == 1 else f"{name} {n}", "host": host,
                    "http_port": num("http_port", port), "rtsp_port": num("rtsp_port"), "onvif_port": num("onvif_port"),
                    "username": (form.get("username") or "").strip(), "password": form.get("password") or "",
                    "kind": form.get("kind") or "auto", "channel": n,
                    "rtsp_url": (form.get("rtsp_url") or "").strip() if channels == 1 else "",
                    "sub_url": (form.get("sub_url") or "").strip() if channels == 1 else "",
                    "motion": form.get("motion") or "auto", "sensitivity": form.get("sensitivity") or "medium",
                    "found": None}
            old = next((c for c in cams if c["id"] == cid), None)
            if old:
                if not base["password"]:
                    base["password"] = old.get("password", "")   # left blank: keep the saved one
                old.update(base)
            else:
                cams.append(base)
            ids.append(cid)
        _save(cams)
    return ids


def remove(cid):
    with _lock:
        _save([c for c in cameras() if c["id"] != cid])


def _remember(cid, found):
    with _lock:
        cams = cameras()
        for c in cams:
            if c["id"] == cid:
                c["found"] = found
        _save(cams)


def rename(cid, name):
    with _lock:
        cams = cameras()
        for c in cams:
            if c["id"] == cid and name.strip():
                c["name"] = name.strip()
        _save(cams)


# --- small helpers ----------------------------------------------------------------------
def _local(tag):
    return tag.rsplit("}", 1)[-1]


def _all(el, name):
    return [e for e in el.iter() if _local(e.tag) == name]


def _first_text(el, name):
    for e in el.iter():
        if _local(e.tag) == name and (e.text or "").strip():
            return e.text.strip()
    return None


def with_login(url, user, pw):
    """Put the camera's name and password into an rtsp:// address (unless it has one)."""
    if not url or not user:
        return url
    u = urlsplit(url)
    if u.username:
        return url
    host = u.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    netloc = f"{quote(user, safe='')}:{quote(pw or '', safe='')}@{host}" + (f":{u.port}" if u.port else "")
    return urlunsplit((u.scheme, netloc, u.path, u.query, u.fragment))


def hide_login(url):
    return re.sub(r"://[^@/]*@", "://", url or "")


def probe(url, seconds=8):
    """What ffprobe sees in a stream: {"codec", "width", "height"}, or None (and why)."""
    cmd = ["ffprobe", "-v", "error"]
    if url.startswith(("rtsp://", "rtsps://")):
        cmd += ["-rtsp_transport", "tcp", "-timeout", str(seconds * 1_000_000)]
    elif url.startswith(("http://", "https://")):
        cmd += ["-rw_timeout", str(seconds * 1_000_000)]
    cmd += ["-select_streams", "v:0", "-show_entries", "stream=codec_name,width,height", "-of", "csv=p=0", url]
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                                text=True, errors="replace", creationflags=NO_WINDOW)
    except FileNotFoundError:
        return None, "ffprobe isn't installed"
    try:
        out, err_text = proc.communicate(timeout=seconds + 6)
    except subprocess.TimeoutExpired:
        proc.kill()                 # and don't wait on its pipes: a stuck camera must never stall BAN
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        return None, "no answer"
    r = subprocess.CompletedProcess(cmd, proc.returncode, out, err_text)
    line = (r.stdout.strip().splitlines() or [""])[0]
    parts = line.split(",")
    if r.returncode == 0 and parts and parts[0]:
        try:
            return {"codec": parts[0], "width": int(parts[1]), "height": int(parts[2])}, None
        except (IndexError, ValueError):
            return {"codec": parts[0], "width": None, "height": None}, None
    err = (r.stderr.strip().splitlines() or ["no video"])[-1]
    return None, "wrong name or password" if "401" in err else err


def port_open(host, port, seconds=2.0):
    try:
        with socket.create_connection((host, port), timeout=seconds):
            return True
    except OSError:
        return False


def rtsp_paths(kind, ch, sub):
    """The usual RTSP addresses, most likely first for this kind of camera."""
    hik = f"/Streaming/Channels/{ch}0{2 if sub else 1}"
    hik_old = f"/h264/ch{ch}/{'sub' if sub else 'main'}/av_stream"
    dahua = f"/cam/realmonitor?channel={ch}&subtype={1 if sub else 0}"
    other = [f"/h264Preview_{ch:02d}_{'sub' if sub else 'main'}",          # Reolink-type
             "/live/ch00_1" if sub else "/live/ch00_0",                     # many no-name cameras
             f"/ch{ch:02d}/{1 if sub else 0}", "/stream2" if sub else "/stream1",   # TP-Link-type
             "/12" if sub else "/11", "/onvif2" if sub else "/onvif1",
             f"/ch{ch - 1}_{1 if sub else 0}.264", "/videoSub" if sub else "/videoMain",
             "/media/video2" if sub else "/media/video1", "/live", "/h264", "/"]
    if kind == "dahua":
        return [dahua, hik, hik_old] + other
    if kind == "hikvision":
        return [hik, hik_old, dahua] + other
    return [hik, dahua, hik_old] + other


# --- HTTP with digest or basic sign-in ------------------------------------------------------
class Http:
    def __init__(self, host, port, user, pw, timeout=6):
        self.base = f"http://{host}:{port or 80}"
        self.user, self.pw, self.timeout = user, pw or "", timeout
        self.auth = None

    def get(self, path, stream=False, timeout=None):
        tries = [self.auth] if self.auth else ([HTTPDigestAuth(self.user, self.pw), HTTPBasicAuth(self.user, self.pw)]
                                               if self.user else [None])
        r = None
        for a in tries:
            r = requests.get(self.base + path, auth=a, stream=stream, timeout=timeout or self.timeout)
            if r.status_code != 401:
                self.auth = a
                return r
        return r


# --- ONVIF (just the calls BAN needs, with a WS-UsernameToken) -------------------------------
NS_DEV = "http://www.onvif.org/ver10/device/wsdl"
NS_MEDIA = "http://www.onvif.org/ver10/media/wsdl"
NS_EVENTS = "http://www.onvif.org/ver10/events/wsdl"


class OnvifError(Exception):
    pass


class Onvif:
    def __init__(self, host, port, user, pw):
        self.host, self.port, self.user, self.pw = host, port, user or "", pw or ""
        self.device = f"http://{host}:{port}/onvif/device_service"
        self.offset = timedelta(0)        # camera clock minus ours: the token must use its time
        self.media = self.events = None

    def _security(self):
        if not self.user:
            return ""
        nonce = secrets.token_bytes(16)
        created = (datetime.now(timezone.utc) + self.offset).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        digest = base64.b64encode(hashlib.sha1(nonce + created.encode() + self.pw.encode()).digest()).decode()
        return ('<wsse:Security s:mustUnderstand="1" xmlns:wsse="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd" '
                'xmlns:wsu="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd"><wsse:UsernameToken>'
                f"<wsse:Username>{_xml(self.user)}</wsse:Username>"
                '<wsse:Password Type="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0#PasswordDigest">'
                f"{digest}</wsse:Password>"
                '<wsse:Nonce EncodingType="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-soap-message-security-1.0#Base64Binary">'
                f"{base64.b64encode(nonce).decode()}</wsse:Nonce><wsu:Created>{created}</wsu:Created></wsse:UsernameToken></wsse:Security>")

    def call(self, url, body, action=None, to=None, auth=True, timeout=8):
        head = (self._security() if auth else "")
        if action or to:
            head += (f'<wsa:Action xmlns:wsa="http://www.w3.org/2005/08/addressing">{action}</wsa:Action>' if action else "")
            head += (f'<wsa:To xmlns:wsa="http://www.w3.org/2005/08/addressing">{_xml(to)}</wsa:To>' if to else "")
        env = ('<?xml version="1.0" encoding="UTF-8"?><s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope">'
               f"<s:Header>{head}</s:Header><s:Body>{body}</s:Body></s:Envelope>")
        ctype = "application/soap+xml; charset=utf-8" + (f'; action="{action}"' if action else "")
        r = requests.post(url, data=env.encode(), headers={"Content-Type": ctype}, timeout=timeout)
        try:
            root = ET.fromstring(r.content)
        except ET.ParseError:
            raise OnvifError(f"not an ONVIF answer (HTTP {r.status_code})")
        fault = _all(root, "Fault")
        if fault or r.status_code >= 400:
            why = _first_text(fault[0], "Text") if fault else None
            raise OnvifError(why or f"HTTP {r.status_code}")
        return root

    def sync_clock(self):
        root = self.call(self.device, f'<GetSystemDateAndTime xmlns="{NS_DEV}"/>', auth=False)
        utc = next(iter(_all(root, "UTCDateTime")), None)
        if utc is None:
            return
        d = {k: int(_first_text(utc, k) or 0) for k in ("Year", "Month", "Day", "Hour", "Minute", "Second")}
        try:
            cam = datetime(d["Year"], d["Month"], d["Day"], d["Hour"], d["Minute"], d["Second"], tzinfo=timezone.utc)
            self.offset = cam - datetime.now(timezone.utc)
        except ValueError:
            pass

    def info(self):
        root = self.call(self.device, f'<GetDeviceInformation xmlns="{NS_DEV}"/>')
        return {k: _first_text(root, k) for k in ("Manufacturer", "Model", "FirmwareVersion", "SerialNumber")}

    def services(self):
        root = self.call(self.device, f'<GetCapabilities xmlns="{NS_DEV}"><Category>All</Category></GetCapabilities>')
        for el in root.iter():
            name = _local(el.tag)
            if name in ("Media", "Events"):
                addr = _first_text(el, "XAddr")
                if addr:
                    setattr(self, name.lower(), self._fix(addr))
        self.media = self.media or self.device.replace("device_service", "Media")
        self.events = self.events or self.device.replace("device_service", "Events")

    def _fix(self, addr):
        """Cameras behind a port forward (or with a wrong IP set) give their own idea of the address."""
        u = urlsplit(addr)
        return urlunsplit((u.scheme or "http", f"{self.host}:{u.port or self.port}", u.path, u.query, ""))

    def profiles(self):
        root = self.call(self.media, f'<GetProfiles xmlns="{NS_MEDIA}"/>')
        out = []
        for p in _all(root, "Profiles"):
            w, h = _first_text(p, "Width"), _first_text(p, "Height")
            out.append({"token": p.get("token"), "name": _first_text(p, "Name"),
                        "width": int(w) if w and w.isdigit() else 0, "height": int(h) if h and h.isdigit() else 0,
                        "encoding": _first_text(p, "Encoding")})
        return [p for p in out if p["token"]]

    def stream_uri(self, token):
        root = self.call(self.media, f'<GetStreamUri xmlns="{NS_MEDIA}"><StreamSetup><Stream xmlns="http://www.onvif.org/ver10/schema">RTP-Unicast</Stream>'
                                     '<Transport xmlns="http://www.onvif.org/ver10/schema"><Protocol>RTSP</Protocol></Transport></StreamSetup>'
                                     f"<ProfileToken>{_xml(token)}</ProfileToken></GetStreamUri>")
        uri = _first_text(root, "Uri")
        if uri:
            u = urlsplit(uri)       # same address fix as the services: the camera may give a wrong IP
            uri = urlunsplit((u.scheme, f"{self.host}:{u.port or 554}", u.path, u.query, ""))
        return uri

    def snapshot_uri(self, token):
        try:
            return _first_text(self.call(self.media, f'<GetSnapshotUri xmlns="{NS_MEDIA}"><ProfileToken>{_xml(token)}</ProfileToken></GetSnapshotUri>'), "Uri")
        except Exception:
            return None

    # --- motion ---
    def subscribe(self):
        root = self.call(self.events, f'<CreatePullPointSubscription xmlns="{NS_EVENTS}"><InitialTerminationTime>PT120S</InitialTerminationTime></CreatePullPointSubscription>',
                         action=f"{NS_EVENTS}/EventPortType/CreatePullPointSubscriptionRequest")
        ref = next(iter(_all(root, "SubscriptionReference")), None)
        addr = _first_text(ref, "Address") if ref is not None else None
        if not addr:
            raise OnvifError("the camera gave no event subscription")
        return self._fix(addr)

    def pull(self, sub):
        root = self.call(sub, f'<PullMessages xmlns="{NS_EVENTS}"><Timeout>PT5S</Timeout><MessageLimit>32</MessageLimit></PullMessages>',
                         action=f"{NS_EVENTS}/PullPointSubscription/PullMessagesRequest", to=sub, timeout=15)
        return parse_onvif_messages(root)

    def renew(self, sub):
        self.call(sub, '<Renew xmlns="http://docs.oasis-open.org/wsn/b-2"><TerminationTime>PT120S</TerminationTime></Renew>',
                  action="http://docs.oasis-open.org/wsn/bw-2/SubscriptionManager/RenewRequest", to=sub)

    def unsubscribe(self, sub):
        try:
            self.call(sub, '<Unsubscribe xmlns="http://docs.oasis-open.org/wsn/b-2"/>',
                      action="http://docs.oasis-open.org/wsn/bw-2/SubscriptionManager/UnsubscribeRequest", to=sub, timeout=4)
        except Exception:
            pass


def _xml(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def parse_onvif_messages(root):
    """[(topic, active, initial)] for the motion-like messages in a PullMessages answer."""
    out = []
    for m in _all(root, "NotificationMessage"):
        topic = _first_text(m, "Topic") or ""
        if not ONVIF_MOTION.search(topic):
            continue
        initial = any(e.get("PropertyOperation") == "Initialized" for e in m.iter())
        value = None
        for si in _all(m, "SimpleItem"):
            if si.get("Name") in ("IsMotion", "State", "Value", "IsInside", "IsPeople", "IsHuman", "Active", "Alarm"):
                value = (si.get("Value") or "").lower() in ("true", "1", "active", "on")
                break
        if value is not None:
            out.append((topic, value, initial))
    return out


def parse_hik_alerts(text):
    """[(eventType, active, channel)] from Hikvision alertStream XML blocks."""
    out = []
    for block in re.findall(r"<EventNotificationAlert\b.*?</EventNotificationAlert>", text, re.S):
        t = (re.search(r"<eventType>([^<]*)</eventType>", block) or [None, ""])[1].strip().lower()
        st = (re.search(r"<eventState>([^<]*)</eventState>", block) or [None, ""])[1].strip().lower()
        ch = re.search(r"<(?:dynChannelID|channelID)>(\d+)<", block)
        out.append((t, st == "active", int(ch.group(1)) if ch else None))
    return out


def parse_dahua_events(text):
    """[(code, action, index)] from Dahua eventManager attach lines."""
    return [(m.group(1).lower(), m.group(2).lower(), int(m.group(3)))
            for m in re.finditer(r"Code=([A-Za-z]+);action=([A-Za-z]+);index=(\d+)", text)]


# --- finding out what a camera is -------------------------------------------------------------
def identify(c, stop=None):
    """Everything BAN can learn about a camera. Raises ValueError with a plain reason."""
    host, user, pw, ch = c["host"], c.get("username") or "", c.get("password") or "", int(c.get("channel") or 1)
    kind = c.get("kind") or "auto"
    found = {"kind": None, "maker": None, "model": None, "serial": None, "firmware": None,
             "main": None, "sub": None, "snapshot": None, "onvif": None, "video": None, "checked": time.time()}
    login_refused = False

    # Hikvision-type (ISAPI)
    http_ports = [c["http_port"]] if c.get("http_port") else [80, 8000, 8080]
    if kind in ("auto", "hikvision"):
        for port in http_ports:
            try:
                r = Http(host, port, user, pw).get("/ISAPI/System/deviceInfo")
            except requests.RequestException:
                continue
            if r.status_code == 401:
                login_refused = True
                continue
            if r.status_code == 200 and "<DeviceInfo" in r.text:
                t = r.text
                def tag(n):
                    m = re.search(rf"<{n}>([^<]*)</{n}>", t)
                    return m.group(1).strip() if m else None
                found.update(kind="hikvision", http_port=port, model=tag("model"), serial=tag("serialNumber"),
                             firmware=tag("firmwareVersion"), maker="Laview" if "laview" in t.lower() else "Hikvision-type")
                found["snapshot"] = f"http://{host}:{port}/ISAPI/Streaming/channels/{ch}01/picture"
                break

    # Dahua-type
    if not found["kind"] and kind in ("auto", "dahua"):
        for port in http_ports:
            try:
                r = Http(host, port, user, pw).get("/cgi-bin/magicBox.cgi?action=getDeviceType")
            except requests.RequestException:
                continue
            if r.status_code == 401:
                login_refused = True
                continue
            if r.status_code == 200 and "type=" in r.text:
                model = r.text.split("type=", 1)[1].strip().splitlines()[0]
                found.update(kind="dahua", http_port=port, model=model, maker="Dahua-type")
                try:
                    s = Http(host, port, user, pw).get("/cgi-bin/magicBox.cgi?action=getSerialNo")
                    if s.status_code == 200 and "sn=" in s.text:
                        found["serial"] = s.text.split("sn=", 1)[1].strip()
                except requests.RequestException:
                    pass
                found["snapshot"] = f"http://{host}:{port}/cgi-bin/snapshot.cgi?channel={ch}"
                break

    # ONVIF: the video address straight from the camera, and its events
    if kind in ("auto", "onvif", "hikvision", "dahua") and not c.get("rtsp_url"):
        ports = [c["onvif_port"]] if c.get("onvif_port") else list(dict.fromkeys(
            [found.get("http_port") or 80, 80, 8000, 8080, 8899, 2020, 5000, 10080]))
        for port in ports:
            if stop and stop.is_set():
                break
            o = Onvif(host, port, user, pw)
            try:
                o.sync_clock()
            except Exception:
                continue          # nothing ONVIF on this port
            try:
                info = o.info()
                o.services()
                profs = sorted(o.profiles(), key=lambda p: -(p["width"] * p["height"]))
            except OnvifError as e:
                if "auth" in str(e).lower() or "not authorized" in str(e).lower() or "sender" in str(e).lower():
                    login_refused = True
                log.debug("%s: ONVIF on %s: %s", host, port, e)
                continue
            except requests.RequestException:
                continue
            found["onvif"] = {"port": port, "events": o.events, "media": o.media}
            found["maker"] = found["maker"] or info.get("Manufacturer")
            found["model"] = found["model"] or info.get("Model")
            found["serial"] = found["serial"] or info.get("SerialNumber")
            found["firmware"] = found["firmware"] or info.get("FirmwareVersion")
            found["kind"] = found["kind"] or "onvif"
            # A recorder lists every channel's profiles; pick this channel's pair when it can be told.
            chan = [p for p in profs if re.search(rf"(^|\D){ch}(\D|$)", p["name"] or "")] if ch > 1 else []
            pick = chan or profs
            try:
                if pick:
                    found["main"] = with_login(o.stream_uri(pick[0]["token"]), user, pw)
                    if len(pick) > 1:
                        found["sub"] = with_login(o.stream_uri(pick[-1]["token"]), user, pw)
                    found["snapshot"] = found["snapshot"] or o.snapshot_uri(pick[0]["token"])
            except Exception as e:
                log.debug("%s: ONVIF stream address: %s", host, e)
            break

    # The video itself: typed in, from ONVIF, or the usual addresses
    if c.get("rtsp_url"):
        found["main"] = with_login(c["rtsp_url"], user, pw)
        found["sub"] = with_login(c["sub_url"], user, pw) if c.get("sub_url") else None
        found["kind"] = found["kind"] or "rtsp"
    video, err = (probe(found["main"]) if found["main"] else (None, None))
    if not video and not c.get("rtsp_url"):
        rtsp_ports = [c["rtsp_port"]] if c.get("rtsp_port") else [554, 8554, 10554]
        open_ports = [p for p in rtsp_ports if port_open(host, p)]
        if not open_ports and not err:
            err = f"nothing answers on RTSP port {', '.join(map(str, rtsp_ports))}"
        for port in open_ports:
            for path in rtsp_paths(found["kind"], ch, sub=False):
                if stop and stop.is_set():
                    raise ValueError("stopped")
                url = with_login(f"rtsp://{host}:{port}{path}", user, pw)
                video, err = probe(url, seconds=5)
                if video:
                    found["main"] = url
                    sub = url
                    for spath in rtsp_paths(found["kind"], ch, sub=True)[:3]:
                        s = with_login(f"rtsp://{host}:{port}{spath}", user, pw)
                        if s != url and probe(s, seconds=5)[0]:
                            sub = s
                            break
                    found["sub"] = sub if sub != url else None
                    found["kind"] = found["kind"] or "rtsp"
                    break
                if err == "wrong name or password":
                    login_refused = True
                if err and ("Connection refused" in err or "No route" in err or "timed out" in err.lower()
                            or err == "no answer"):
                    break          # this port stopped answering: next port
            if video:
                break
    if not video:
        if login_refused:
            raise ValueError("The camera turned down that name and password.")
        raise ValueError(f"Couldn't find the camera's video at {host}" + (f" ({err})" if err else "") +
                         ". Check the address, that RTSP is turned on in the camera, or type its RTSP address.")
    found["video"] = video
    if found["sub"] and found["sub"] != found["main"]:
        sv, _ = probe(found["sub"], seconds=6)
        if not sv:
            found["sub"] = None
    return found


# --- finding cameras on the network -------------------------------------------------------------
def discover(seconds=3.0):
    """Cameras that answer ONVIF WS-Discovery or Hikvision SADP (Laview's tool uses it) on this network."""
    found = {}
    probe_onvif = ('<?xml version="1.0" encoding="UTF-8"?><e:Envelope xmlns:e="http://www.w3.org/2003/05/soap-envelope" '
                   'xmlns:w="http://schemas.xmlsoap.org/ws/2004/08/addressing" xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery" '
                   'xmlns:dn="http://www.onvif.org/ver10/network/wsdl"><e:Header>'
                   f"<w:MessageID>uuid:{uuid.uuid4()}</w:MessageID><w:To>urn:schemas-xmlsoap-org:ws:2005:04:discovery</w:To>"
                   "<w:Action>http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</w:Action></e:Header>"
                   "<e:Body><d:Probe><d:Types>dn:NetworkVideoTransmitter</d:Types></d:Probe></e:Body></e:Envelope>")
    probe_sadp = f'<?xml version="1.0" encoding="utf-8"?><Probe><Uuid>{str(uuid.uuid4()).upper()}</Uuid><Types>inquiry</Types></Probe>'
    socks = []
    for port, payload in ((3702, probe_onvif), (37020, probe_sadp)):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
            s.settimeout(0.3)
            s.sendto(payload.encode(), ("239.255.255.250", port))
            socks.append((port, s))
        except OSError as e:
            log.debug("discovery on %d: %s", port, e)
    until = time.time() + seconds
    while time.time() < until and socks:
        for port, s in socks:
            try:
                data, (ip, _) = s.recvfrom(65535)
            except (socket.timeout, OSError):
                continue
            text = data.decode("utf-8", "replace")
            d = found.setdefault(ip, {"host": ip, "model": None, "maker": None, "http_port": None, "via": []})
            if port == 3702:
                d["via"].append("ONVIF")
                xaddr = re.search(r"XAddrs>\s*([^<\s]+)", text)
                if xaddr:
                    u = urlsplit(xaddr.group(1))
                    d["onvif_port"] = u.port or 80
                scopes = re.search(r"Scopes>([^<]*)<", text)
                if scopes:
                    for part in scopes.group(1).split():
                        if "/hardware/" in part:
                            d["model"] = d["model"] or requests.utils.unquote(part.rsplit("/", 1)[-1])
                        if "/name/" in part:
                            d["maker"] = d["maker"] or requests.utils.unquote(part.rsplit("/", 1)[-1])
            else:
                d["via"].append("SADP")
                m = re.search(r"<DeviceDescription>([^<]*)<", text) or re.search(r"<DeviceType>([^<]*)<", text)
                d["model"] = d["model"] or (m.group(1) if m else None)
                hp = re.search(r"<HttpPort>(\d+)<", text)
                d["http_port"] = int(hp.group(1)) if hp else None
                d["maker"] = d["maker"] or "Hikvision-type (Laview)"
                act = re.search(r"<Activated>([^<]*)<", text)
                if act and act.group(1).lower() == "false":
                    d["note"] = "Not activated yet: set its password in the Laview app or SADP tool first."
    for _, s in socks:
        s.close()
    known = {c["host"] for c in cameras()}
    out = []
    for d in found.values():
        d["via"] = sorted(set(d["via"]))
        d["added"] = d["host"] in known
        out.append(d)
    return sorted(out, key=lambda d: tuple(int(x) for x in d["host"].split(".") if x.isdigit()))


# --- one camera, as LiveCapture and liveview see it -------------------------------------------
class Cam:
    def __init__(self, hub, conf):
        self.hub = hub
        self.conf = conf
        self.device_id = conf["id"]
        self.found = conf.get("found") or {}
        self.state = {"online": False, "error": None, "motion_source": None, "events_ok": None, "checking": True}
        self.stop_flag = threading.Event()
        self.threads = []

    @property
    def name(self):
        return self.conf.get("name") or self.device_id

    # what capture.LiveCapture and liveview call
    def get_stream(self):
        return self.found.get("main")

    def stop_stream(self):
        pass

    def small_stream(self):
        return self.found.get("sub") or self.found.get("main")


class Hub:
    """Every IP camera: finding its video, listening for motion, and recording like Arlo."""

    def __init__(self):
        self.cams = {}
        self._lock = threading.Lock()
        self._status = {}
        self._motion_until = {}
        self._last_clip = {}
        self._capture = None

    @property
    def capture(self):
        if self._capture is None:          # made on first use: LiveCapture clears the live folder
            self._capture = LiveCapture(self)
        return self._capture

    # --- what LiveCapture needs from a "recorder" ---
    def cam_status(self, cam):
        return self._status.setdefault(cam.device_id, {"last_motion": None, "last_clip": None, "last_error": None, "capturing": False})

    def motion_active(self, cam_id):
        return time.time() < self._motion_until.get(cam_id, 0)

    def note_clip(self, cam_id):
        self._last_clip[cam_id] = time.time()

    # --- cameras ---
    def start_all(self):
        self.capture
        for c in cameras():
            self._start(c)

    def _start(self, conf):
        with self._lock:
            old = self.cams.pop(conf["id"], None)
            if old:
                old.stop_flag.set()
            cam = self.cams[conf["id"]] = Cam(self, conf)
        t = threading.Thread(target=self._run, args=(cam,), name=f"ipcam-{cam.device_id}", daemon=True)
        cam.threads.append(t)
        t.start()
        return cam

    def restart(self, cid):
        conf = get(cid)
        if conf:
            self._start(conf)

    def add(self, form):
        ids = add(form)
        for cid in ids:
            self.restart(cid)
        return ids

    def remove(self, cid):
        remove(cid)
        with self._lock:
            cam = self.cams.pop(cid, None)
        if cam:
            cam.stop_flag.set()

    def owns(self, cid):
        return str(cid).startswith("ip-")

    def camera(self, cid):
        return self.cams.get(cid)

    def connected(self):
        return any(c.state["online"] for c in list(self.cams.values()))

    def any(self):
        return bool(self.cams)

    def record_now(self, cid):
        cam = self.cams.get(cid)
        return bool(cam and cam.state["online"]) and self.capture.trigger(cam, "manual", force=True)

    def test_motion(self, cid):
        cam = self.cams.get(cid)
        if not cam or not cam.state["online"]:
            return False
        log.info("motion (test): %s", cam.name)
        self.cam_status(cam)["last_motion"] = time.time()
        return self.capture.trigger(cam, "motion", force=True)

    def cameras_view(self):
        out = []
        for cam in list(self.cams.values()):
            f, st = cam.found, cam.state
            v = f.get("video") or {}
            model = " ".join(x for x in (f.get("maker"), f.get("model")) if x) or "IP camera"
            out.append({"id": cam.device_id, "name": cam.name, "model": model, "battery": None,
                        "unavailable": not st["online"], "motion": self.motion_active(cam.device_id),
                        "brand": BRAND, "account": ACCOUNT, "no_live": not st["online"],
                        "detail": {"host": cam.conf.get("host"), "kind": f.get("kind"), "checking": st["checking"],
                                   "error": st["error"], "motion_source": st["motion_source"], "events_ok": st["events_ok"],
                                   "video": f"{v.get('codec') or '?'} {v.get('width') or '?'}x{v.get('height') or '?'}" if v else None,
                                   "stream": hide_login(f.get("main")), "small": hide_login(f.get("sub")),
                                   "serial": f.get("serial"), "firmware": f.get("firmware"), "channel": cam.conf.get("channel")},
                        **self.cam_status(cam)})
        return out

    # --- the camera's life: find its video, then listen for motion ---
    def _run(self, cam):
        tries = 0
        while not cam.stop_flag.is_set():
            cam.state.update(checking=True)
            if not cam.found.get("main") or tries:
                try:
                    cam.found = identify(cam.conf, cam.stop_flag)
                    _remember(cam.device_id, cam.found)
                except ValueError as e:
                    if cam.stop_flag.is_set():
                        return
                    cam.state.update(online=False, error=str(e), checking=False)
                    log.warning("%s: %s", cam.name, e)
                    cam.stop_flag.wait(RETRY_SECONDS[min(tries, len(RETRY_SECONDS) - 1)])
                    tries += 1
                    continue
                except Exception as e:
                    log.exception("%s: checking the camera", cam.name)
                    cam.state.update(online=False, error=str(e), checking=False)
                    cam.stop_flag.wait(60)
                    tries += 1
                    continue
            else:
                v, err = probe(cam.found["main"])
                if not v:                     # saved address stopped working: look again
                    tries = 1
                    cam.state.update(online=False, error=f"No video ({err}), looking again…")
                    continue
                cam.found["video"] = v
            cam.state.update(online=True, error=None, checking=False)
            log.info("%s: %s %s, video %s", cam.name, cam.found.get("kind"), cam.found.get("model") or "",
                     hide_login(cam.found["main"]))
            tries = 0
            ended = self._listen(cam)       # returns when the camera stops answering
            if cam.stop_flag.is_set():
                return
            cam.state.update(online=False, error=ended or "The camera stopped answering, trying again…")
            cam.stop_flag.wait(15)
            tries = 1

    def _listen(self, cam):
        want = cam.conf.get("motion") or "auto"
        if want == "off":
            cam.state["motion_source"] = "off"
            return self._keep_checking(cam)
        sources = []
        if want in ("auto", "camera"):
            if cam.found.get("kind") == "hikvision":
                sources.append(("camera (Hikvision events)", self._hik_events))
            if cam.found.get("kind") == "dahua":
                sources.append(("camera (Dahua events)", self._dahua_events))
            if (cam.found.get("onvif") or {}).get("events"):
                sources.append(("camera (ONVIF events)", self._onvif_events))
        if want in ("auto", "video") and (want == "video" or not sources):
            sources.append(("BAN watching the video", self._video_motion))
        if not sources:
            cam.state["motion_source"] = "none: the camera has no motion events (choose “BAN watches the video”)"
            return self._keep_checking(cam)
        for label, fn in sources:
            cam.state.update(motion_source=label, events_ok=None)
            started = time.time()
            why = fn(cam)
            if cam.stop_flag.is_set():
                return None
            if why == "lost":
                return None
            if time.time() - started > 300:        # worked for a while: same source again
                return why
            log.info("%s: motion from %s didn't work (%s), trying the next way", cam.name, label, why)
        return self._video_motion(cam) if want == "auto" and sources[-1][1] != self._video_motion else "no way to get motion"

    def _keep_checking(self, cam):
        while not cam.stop_flag.wait(120):
            if not probe(cam.found["main"])[0]:
                return "lost"
        return None

    def motion(self, cam, active, what="motion"):
        """The camera (or BAN, from the video) says something is moving."""
        cid = cam.device_id
        now = time.time()
        if not active:
            self._motion_until[cid] = min(self._motion_until.get(cid, 0), now + 2)
            return
        was = self.motion_active(cid)
        self._motion_until[cid] = now + MOTION_HOLD
        if was:
            return
        log.info("motion: %s (%s)", cam.name, what)
        self.cam_status(cam)["last_motion"] = now
        events.publish("motion", camera=cam.name, camera_id=cid, started=int(now * 1000))
        if config.CAPTURE_LIVE:
            self.capture.trigger(cam, "motion")
        s = notify.current()
        if s["on_motion"]:
            notify.send(cid, "motion", f"Motion: {cam.name}", f"Motion detected at {datetime.now():%I:%M:%S %p}",
                        link=s["public_url"] or None, extra={"camera": cam.name, "event": "motion"})

    # --- motion from the camera ---
    def _hik_events(self, cam):
        f, c = cam.found, cam.conf
        ch = int(c.get("channel") or 1)
        http = Http(c["host"], f.get("http_port") or c.get("http_port") or 80, c.get("username"), c.get("password"))
        try:
            r = http.get("/ISAPI/Event/notification/alertStream", stream=True, timeout=(6, 120))
        except requests.RequestException as e:
            return f"no events ({e})"
        if r.status_code != 200:
            return f"events answered {r.status_code}"
        buf, cam.state["events_ok"] = "", True
        try:
            for chunk in r.iter_content(4096):
                if cam.stop_flag.is_set():
                    return None
                buf += chunk.decode("utf-8", "replace")
                if "</EventNotificationAlert>" not in buf:
                    buf = buf[-200_000:]
                    continue
                for etype, active, chan in parse_hik_alerts(buf):
                    if etype in HIK_MOTION and (chan is None or chan == ch):
                        self.motion(cam, active, etype)
                buf = buf.rsplit("</EventNotificationAlert>", 1)[1]
        except requests.RequestException as e:
            return f"events stopped ({e})"
        finally:
            r.close()
        return "events ended"

    def _dahua_events(self, cam):
        f, c = cam.found, cam.conf
        ch = int(c.get("channel") or 1)
        http = Http(c["host"], f.get("http_port") or c.get("http_port") or 80, c.get("username"), c.get("password"))
        try:
            r = http.get("/cgi-bin/eventManager.cgi?action=attach&codes=[All]&heartbeat=5", stream=True, timeout=(6, 60))
        except requests.RequestException as e:
            return f"no events ({e})"
        if r.status_code != 200:
            return f"events answered {r.status_code}"
        cam.state["events_ok"] = True
        buf = ""
        try:
            for chunk in r.iter_content(2048):
                if cam.stop_flag.is_set():
                    return None
                buf += chunk.decode("utf-8", "replace")
                for code, action, index in parse_dahua_events(buf):
                    if code in DAHUA_MOTION and index + 1 == ch:
                        if action in ("start", "pulse"):
                            self.motion(cam, True, code)
                        elif action == "stop":
                            self.motion(cam, False, code)
                buf = buf[buf.rfind("index=") + 6:] if "index=" in buf else buf[-4000:]
        except requests.RequestException as e:
            return f"events stopped ({e})"
        finally:
            r.close()
        return "events ended"

    def _onvif_events(self, cam):
        c, o_found = cam.conf, cam.found.get("onvif") or {}
        o = Onvif(c["host"], o_found.get("port") or 80, c.get("username"), c.get("password"))
        o.events = o_found.get("events")
        try:
            o.sync_clock()
            sub = o.subscribe()
        except Exception as e:
            return f"no ONVIF events ({e})"
        cam.state["events_ok"] = True
        renewed, fails = time.time(), 0
        try:
            while not cam.stop_flag.is_set():
                try:
                    for topic, active, initial in o.pull(sub):
                        if not initial:
                            self.motion(cam, active, topic.rsplit("/", 1)[-1])
                    fails = 0
                    if time.time() - renewed > 60:
                        try:
                            o.renew(sub)
                        except OnvifError:
                            sub = o.subscribe()     # some cameras don't renew: start a new one
                        renewed = time.time()
                except (OnvifError, requests.RequestException) as e:
                    fails += 1
                    if fails >= 3:
                        if not probe(cam.found["main"])[0]:
                            return "lost"
                        try:
                            sub, fails = o.subscribe(), 0
                        except Exception:
                            return f"ONVIF events stopped ({e})"
                    cam.stop_flag.wait(3)
        finally:
            o.unsubscribe(sub)
        return None

    # --- motion from the video (cameras with no events) ---
    def _video_motion(self, cam):
        url = cam.small_stream()
        thr = THRESHOLDS.get(cam.conf.get("sensitivity") or "medium", THRESHOLDS["medium"])
        cmd = ["ffmpeg", "-hide_banner", "-nostats", "-loglevel", "info"]
        if url.startswith(("rtsp://", "rtsps://")):
            cmd += ["-rtsp_transport", "tcp", "-timeout", "10000000"]
        else:
            cmd += ["-re"]                    # a file or HTTP stream: read it at normal speed
        cmd += ["-i", url, "-an", "-vf", f"fps=3,scale=320:-2,select='gt(scene,{thr})',metadata=print", "-f", "null", "-"]
        started_at = time.time()
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
                                    errors="replace", creationflags=NO_WINDOW)
        except FileNotFoundError:
            return "ffmpeg isn't installed"
        cam.state["events_ok"] = True
        hits = []
        stopper = threading.Thread(target=lambda: (cam.stop_flag.wait(), proc.kill()), daemon=True)
        stopper.start()
        try:
            for line in proc.stderr:
                if "lavfi.scene_score=" not in line:
                    continue
                now = time.time()
                if now - started_at < 5:          # the first pictures always look like a change
                    continue
                hits = [t for t in hits if now - t < 2.5] + [now]
                if len(hits) >= 2:                # two changes close together: not just noise
                    self.motion(cam, True, "video")
        finally:
            proc.kill()
            proc.wait()
        if cam.stop_flag.is_set():
            return None
        return "lost" if not probe(cam.found["main"])[0] else "the video stopped"
