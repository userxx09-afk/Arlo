import faulthandler; faulthandler.dump_traceback_later(240, exit=True)
"""Fake Laview (Hikvision-type) camera + ONVIF device, to exercise app/ipcam.py end to end."""
import os, sys, tempfile, threading, time, subprocess, base64, json
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

D = tempfile.mkdtemp()
os.environ.update(DATA_DIR=D, CLIP_MAX_SECONDS="4", TRANSCODE_H264="true")
sys.path.insert(0, sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
os.makedirs(os.path.join(D, "clips"), exist_ok=True)
os.makedirs(os.path.join(D, "state"), exist_ok=True)

# test videos: a plain one, and one with a flashing box from t=6s (for video motion)
plain = os.path.join(D, "cam.ts"); flash = os.path.join(D, "flash.ts")
subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=s=640x360:r=10:d=30", "-c:v", "libx264", "-g", "10", "-f", "mpegts", plain], check=True)
subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=black:s=320x240:r=10:d=14",
                "-vf", "drawbox=x=60:y=60:w=200:h=120:color=white:t=fill:enable='gt(t,7)*lt(mod(t,1),0.5)'", "-c:v", "libx264", "-f", "mpegts", flash], check=True)

USER, PW = "admin", "Secret#1"
GOOD = "Basic " + base64.b64encode(f"{USER}:{PW}".encode()).decode()
fired = {"alerts": 0}

DEVINFO = "<?xml version='1.0'?><DeviceInfo><deviceName>Porch</deviceName><model>LV-PB932F4</model><serialNumber>LV123</serialNumber><firmwareVersion>V5.5.0</firmwareVersion><manufacturer>LaView</manufacturer></DeviceInfo>"
ALERT = ("--boundary\r\nContent-Type: application/xml\r\n\r\n<EventNotificationAlert version='2.0'><channelID>1</channelID>"
         "<eventType>{t}</eventType><eventState>{s}</eventState></EventNotificationAlert>\r\n")


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.startswith("/video"):
            f = flash if "flash" in self.path else plain
            data = open(f, "rb").read()
            self.send_response(200); self.send_header("Content-Type", "video/mp2t"); self.send_header("Content-Length", str(len(data))); self.end_headers()
            self.wfile.write(data); return
        if self.headers.get("Authorization") != GOOD:
            self.send_response(401); self.send_header("WWW-Authenticate", 'Basic realm="cam"'); self.end_headers(); return
        if self.path == "/ISAPI/System/deviceInfo":
            b = DEVINFO.encode(); self.send_response(200); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b); return
        if self.path == "/ISAPI/Event/notification/alertStream":
            self.send_response(200); self.send_header("Content-Type", "multipart/mixed; boundary=boundary"); self.end_headers()
            try:
                self.wfile.write(ALERT.format(t="videoloss", s="inactive").encode()); self.wfile.flush(); time.sleep(1.5)
                self.wfile.write(ALERT.format(t="VMD", s="active").encode()); self.wfile.flush(); fired["alerts"] += 1
                time.sleep(40)
            except OSError:
                pass
            return
        self.send_response(404); self.end_headers()


srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()

from app import store, ipcam, events, garystv, config
store.init()
popups = []
garystv.motion_started = lambda ev: popups.append(ev)
pub = []
_orig = events.publish
events.publish = lambda kind, **kw: (pub.append(kind), _orig(kind, **kw))

ok = True
def check(name, cond, extra=""):
    global ok
    ok &= bool(cond)
    print(("PASS " if cond else "FAIL ") + name + (f"  {extra}" if extra else ""))

# 1. parsers
check("hik alert parse", ipcam.parse_hik_alerts(ALERT.format(t="fielddetection", s="active")) == [("fielddetection", True, 1)])
check("dahua parse", ipcam.parse_dahua_events("Code=VideoMotion;action=Start;index=0\r\n") == [("videomotion", "start", 0)])
check("login into rtsp", ipcam.with_login("rtsp://10.0.0.5:554/Streaming/Channels/101", "admin", "p@ss:w") == "rtsp://admin:p%40ss%3Aw@10.0.0.5:554/Streaming/Channels/101")
check("hide login", ipcam.hide_login("rtsp://a:b@h/x") == "rtsp://h/x")
check("paths dahua first", ipcam.rtsp_paths("dahua", 2, False)[0] == "/cam/realmonitor?channel=2&subtype=0")
check("paths hik sub", ipcam.rtsp_paths("hikvision", 3, True)[0] == "/Streaming/Channels/302")

# 2. identify a Hikvision-type camera (video address typed in, so no RTSP server needed)
conf = {"id": "ip-test-1", "name": "Porch", "host": "127.0.0.1", "http_port": port, "username": USER, "password": PW,
        "kind": "auto", "channel": 1, "rtsp_url": f"http://127.0.0.1:{port}/video", "sub_url": "", "motion": "auto", "sensitivity": "medium"}
f = ipcam.identify(conf)
check("identify kind/model", f["kind"] == "hikvision" and f["model"] == "LV-PB932F4" and f["maker"] == "Laview", json.dumps({k: f[k] for k in ("kind", "model", "maker")}))
check("identify video", f["video"] and f["video"]["width"] == 640, str(f["video"]))
bad = dict(conf, password="wrong")
try:
    ipcam.identify(dict(bad, rtsp_url=""), )
    check("wrong password reported", False)
except ValueError as e:
    check("wrong password reported", "name and password" in str(e), str(e))

# 3. the hub: Hik events -> motion -> clip saved + pop-up
store.set_setting(ipcam.KEY, [conf])
hub = ipcam.Hub()
hub.start_all()
deadline = time.time() + 60
while time.time() < deadline and not store.query("ip-test-1"):
    time.sleep(0.5)
rows = store.query("ip-test-1")
view = hub.cameras_view()
check("camera online", view and not view[0]["unavailable"], json.dumps(view[0]["detail"]) if view else "")
check("motion source hik", view and "Hikvision" in (view[0]["detail"]["motion_source"] or ""))
check("motion published", "motion" in pub, str(pub))
check("garystv pop-up", popups and popups[0]["camera_id"] == "ip-test-1" and popups[0]["live"].startswith("/live/"), str(popups[:1]))
check("clip saved", rows and rows[0]["video_path"].endswith(".mp4") and rows[0]["duration_s"], str(rows[:1]))
check("clip published", "clip" in pub)
print("removing", flush=True); hub.remove("ip-test-1"); print("removed", flush=True)

# 4. video motion (no events): flashing box
conf2 = dict(conf, id="ip-test-2", name="Yard", kind="rtsp", http_port=None, rtsp_url=f"http://127.0.0.1:{port}/video-flash", motion="video")
cam = ipcam.Cam(hub, conf2)
cam.found = ipcam.identify(conf2)
hits = []
hub.motion = lambda c, active, what="": hits.append(what)
t = threading.Thread(target=hub._video_motion, args=(cam,), daemon=True); t.start()
t.join(40); cam.stop_flag.set()
check("video motion seen", "video" in hits, f"{len(hits)} hits")

# 5. ONVIF messages
from xml.etree import ElementTree as ET
msg = ET.fromstring("""<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" xmlns:tt="http://www.onvif.org/ver10/schema" xmlns:wsnt="http://docs.oasis-open.org/wsn/b-2"><s:Body><tev:PullMessagesResponse xmlns:tev="http://www.onvif.org/ver10/events/wsdl">
<wsnt:NotificationMessage><wsnt:Topic>tns1:RuleEngine/CellMotionDetector/Motion</wsnt:Topic><wsnt:Message><tt:Message PropertyOperation="Changed"><tt:Data><tt:SimpleItem Name="IsMotion" Value="true"/></tt:Data></tt:Message></wsnt:Message></wsnt:NotificationMessage>
<wsnt:NotificationMessage><wsnt:Topic>tns1:VideoSource/MotionAlarm</wsnt:Topic><wsnt:Message><tt:Message PropertyOperation="Initialized"><tt:Data><tt:SimpleItem Name="State" Value="false"/></tt:Data></tt:Message></wsnt:Message></wsnt:NotificationMessage>
<wsnt:NotificationMessage><wsnt:Topic>tns1:Device/Trigger/DigitalInput</wsnt:Topic><wsnt:Message><tt:Message><tt:Data><tt:SimpleItem Name="LogicalState" Value="true"/></tt:Data></tt:Message></wsnt:Message></wsnt:NotificationMessage>
</tev:PullMessagesResponse></s:Body></s:Envelope>""")
check("onvif messages", ipcam.parse_onvif_messages(msg) == [("tns1:RuleEngine/CellMotionDetector/Motion", True, False), ("tns1:VideoSource/MotionAlarm", False, True)], str(ipcam.parse_onvif_messages(msg)))
print("ALL OK" if ok else "SOME FAILED")
os._exit(0 if ok else 1)
