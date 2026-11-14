import os, sys, tempfile, threading, time, subprocess, base64, hashlib, re
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
D = tempfile.mkdtemp()
os.environ.update(DATA_DIR=D)
sys.path.insert(0, sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
os.makedirs(os.path.join(D, "clips"), exist_ok=True); os.makedirs(os.path.join(D, "state"), exist_ok=True)
video = os.path.join(D, "v.ts")
subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=s=1280x720:r=10:d=10", "-c:v", "libx264", "-f", "mpegts", video], check=True)
USER, PW = "admin", "pw123"
pulls = {"n": 0}

def env(body):
    return f'<?xml version="1.0"?><s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" xmlns:tt="http://www.onvif.org/ver10/schema" xmlns:wsnt="http://docs.oasis-open.org/wsn/b-2" xmlns:wsa="http://www.w3.org/2005/08/addressing"><s:Body>{body}</s:Body></s:Envelope>'

class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        if self.path.startswith("/live"):
            d = open(video, "rb").read(); self.send_response(200); self.send_header("Content-Length", str(len(d))); self.end_headers(); self.wfile.write(d)
        else:
            self.send_response(404); self.end_headers()
    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"])).decode()
        port = self.server.server_address[1]
        def send(xml, code=200):
            b = env(xml).encode(); self.send_response(code); self.send_header("Content-Type", "application/soap+xml"); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
        if "GetSystemDateAndTime" in body:
            n = time.gmtime(time.time() + 3600)   # camera clock an hour off: the token must follow it
            return send(f"<tds:GetSystemDateAndTimeResponse xmlns:tds='x'><tds:SystemDateAndTime><tt:UTCDateTime><tt:Time><tt:Hour>{n.tm_hour}</tt:Hour><tt:Minute>{n.tm_min}</tt:Minute><tt:Second>{n.tm_sec}</tt:Second></tt:Time><tt:Date><tt:Year>{n.tm_year}</tt:Year><tt:Month>{n.tm_mon}</tt:Month><tt:Day>{n.tm_mday}</tt:Day></tt:Date></tt:UTCDateTime></tds:SystemDateAndTime></tds:GetSystemDateAndTimeResponse>")
        # check the WS-UsernameToken digest, using the camera's (shifted) clock
        nonce = re.search(r"Nonce[^>]*>([^<]+)<", body); created = re.search(r"Created>([^<]+)<", body); dig = re.search(r"#PasswordDigest\">([^<]+)<", body)
        if not (nonce and created and dig) or base64.b64encode(hashlib.sha1(base64.b64decode(nonce.group(1)) + created.group(1).encode() + PW.encode()).digest()).decode() != dig.group(1):
            return send("<s:Fault><s:Reason><s:Text>Sender not Authorized</s:Text></s:Reason></s:Fault>", 400)
        cam_t = time.mktime(time.strptime(created.group(1)[:19], "%Y-%m-%dT%H:%M:%S")) - time.timezone
        if abs(cam_t - (time.time() + 3600)) > 30:
            return send("<s:Fault><s:Reason><s:Text>Sender not Authorized (clock)</s:Text></s:Reason></s:Fault>", 400)
        if "GetDeviceInformation" in body:
            return send("<r><Manufacturer>LaView</Manufacturer><Model>LV-PWF1-W</Model><FirmwareVersion>2.8</FirmwareVersion><SerialNumber>SN9</SerialNumber></r>")
        if "GetCapabilities" in body:
            return send(f"<r><tt:Media><tt:XAddr>http://10.9.9.9:{port}/onvif/Media</tt:XAddr></tt:Media><tt:Events><tt:XAddr>http://10.9.9.9/onvif/Events</tt:XAddr></tt:Events></r>")
        if "GetProfiles" in body:
            return send("<r><trt:Profiles xmlns:trt='m' token='sub'><tt:Name>Sub</tt:Name><tt:Resolution><tt:Width>640</tt:Width><tt:Height>360</tt:Height></tt:Resolution></trt:Profiles>"
                        "<trt:Profiles xmlns:trt='m' token='main'><tt:Name>Main</tt:Name><tt:Resolution><tt:Width>1920</tt:Width><tt:Height>1080</tt:Height></tt:Resolution></trt:Profiles></r>")
        if "GetStreamUri" in body:
            tok = re.search(r"ProfileToken>([^<]+)<", body).group(1)
            return send(f"<r><tt:Uri>http://10.9.9.9:{port}/live/{tok}</tt:Uri></r>")
        if "GetSnapshotUri" in body:
            return send(f"<r><tt:Uri>http://10.9.9.9:{port}/snap</tt:Uri></r>")
        if "CreatePullPointSubscription" in body:
            return send(f"<r><SubscriptionReference><wsa:Address>http://10.9.9.9:{port}/onvif/sub1</wsa:Address></SubscriptionReference></r>")
        if "PullMessages" in body:
            pulls["n"] += 1
            if pulls["n"] == 2:
                return send("<r><wsnt:NotificationMessage><wsnt:Topic>tns1:RuleEngine/CellMotionDetector/Motion</wsnt:Topic><wsnt:Message><tt:Message PropertyOperation='Changed'><tt:Data><tt:SimpleItem Name='IsMotion' Value='true'/></tt:Data></tt:Message></wsnt:Message></wsnt:NotificationMessage></r>")
            time.sleep(1); return send("<r/>")
        return send("<r/>")

srv = ThreadingHTTPServer(("127.0.0.1", 0), H); port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
from app import ipcam, store
store.init()
conf = {"id": "ip-onvif-1", "name": "Garage", "host": "127.0.0.1", "http_port": None, "onvif_port": port, "username": USER, "password": PW,
        "kind": "onvif", "channel": 1, "rtsp_url": "", "sub_url": "", "motion": "auto", "sensitivity": "medium"}
f = ipcam.identify(conf)
print("kind", f["kind"], "model", f["maker"], f["model"], "main", ipcam.hide_login(f["main"]), "sub", ipcam.hide_login(f["sub"]), "events", f["onvif"]["events"])
assert f["kind"] == "onvif" and f["model"] == "LV-PWF1-W" and f["main"].endswith("/live/main") and f["sub"].endswith("/live/sub")
assert "127.0.0.1" in f["onvif"]["events"]
hub = ipcam.Hub(); cam = ipcam.Cam(hub, conf); cam.found = f
seen = []
hub.motion = lambda c, a, w="": seen.append((a, w))
t = threading.Thread(target=hub._onvif_events, args=(cam,), daemon=True); t.start()
for _ in range(40):
    if seen: break
    time.sleep(0.25)
cam.stop_flag.set()
print("onvif motion", seen)
assert seen and seen[0] == (True, "Motion")
print("ONVIF OK")
os._exit(0)
