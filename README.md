# BAN — Barb's Arlo Notifications

Keeps local copies of Arlo camera clips, shows them on a web page, lets you watch any camera
live, and notifies a phone the moment a camera sees motion. No Arlo subscription needed.

```
server/    Arlo Keeper: the service on the home server (Python, Flask) and the BAN web page
android/   BAN for Android: the web page laid out for a phone, plus motion notifications
```

## The web page

- **Cameras**: battery, last motion, **Watch live** on demand, **Record clip**.
  Live view stops 5 s after the last viewer leaves (or after 3 minutes) to save batteries.
- **Recordings**: newest first, grouped by day or month, filter by camera, starred only,
  select several to star or delete. New clips appear without reloading.
- **Arlo**: one or more Arlo accounts, each with **Sign out**, plus **Add another Arlo account**.
  A camera both accounts can see is recorded once (by the account added first).
- **Alerts**: text messages (Twilio), ntfy, Pushover, webhook, and the phone app.
- Lays itself out for a phone with a bottom tab bar.
- The photo at the top: tap it to change it, or put `photo-default.jpg` in `server/app/static/`.

## Blink cameras

Blink cameras can sit next to the Arlo ones (**Arlo** page → **Add Blink cameras**; email, password,
then the code Blink sends). Their motion pops up on GarysTV like an Arlo camera's (tick them in
GarysTV's Settings > Arlo), goes to the phone app, and sends the usual alerts; the clip is saved
with the others. Uses [blinkpy](https://github.com/fronzbot/blinkpy).

- Blink has no instant motion push: BAN checks every 30 s (`BLINK_POLL_SECONDS`).
- Blink only reports motion once it has saved a clip, in its cloud (Blink plan, or an account from
  before April 2020) or on a USB drive in a **Sync Module 2** (no plan). The system must be armed.
- If the clip can't be downloaded, the GarysTV pop-up shows the camera's motion picture instead.
- No live view in BAN for Blink (use the Blink app). **Record clip** works.

## Laview and other IP cameras

**Arlo → Laview & IP cameras** (`/connect/ip`) adds cameras that send video on the home network:
Laview IP cameras and recorders (NVR/DVR), and most RTSP or ONVIF cameras. They work like Arlo ones:
motion pops up on GarysTV (tick them in Settings › Arlo; they have their own "Laview & IP cameras"
account tab), goes to the phone app and the alerts, records a clip, and Live view works.

Enter the camera's address, user name (usually `admin`) and the camera's own password. BAN works out
the rest (`server/app/ipcam.py`):

| Camera | How BAN knows it | Video | Motion |
|---|---|---|---|
| Laview / Hikvision-type (most Laview IP cameras and NVRs) | `/ISAPI/System/deviceInfo` | `rtsp://…:554/Streaming/Channels/101` (`102` small; channel N = `N01`) | `/ISAPI/Event/notification/alertStream` (motion, line crossing, intrusion, PIR, person/vehicle) |
| Laview / Dahua-type | `/cgi-bin/magicBox.cgi` | `rtsp://…/cam/realmonitor?channel=1&subtype=0` | `/cgi-bin/eventManager.cgi?action=attach` (VideoMotion, SmartMotion, tripwire, intrusion) |
| Any ONVIF camera | ONVIF GetDeviceInformation (ports 80, 8000, 8080, 8899, 2020, 5000, 10080) | GetStreamUri, biggest and smallest profile | PullPoint events (cell motion, motion alarm, line/field detector, people/vehicle) |
| RTSP only | the usual addresses on ports 554, 8554, 10554 (Hikvision, Dahua, Reolink, TP-Link and no-name paths), or one typed in | as found | BAN watches the small stream (ffmpeg scene change, sensitivity Low/Normal/High) |

- **Motion:** *From the camera, else BAN watches the video* (default), *Only from the camera*,
  *BAN watches the video*, or *Off* (Live view and Record clip only). If the camera's events stop
  working, BAN falls back to watching the video.
- **Recorders:** enter the NVR/DVR address and *How many channels to add*: each channel is a camera.
- **Find cameras on this network** asks ONVIF (WS-Discovery) and Hikvision SADP (what Laview's tools use).
  It also points out cameras that haven't been activated yet.
- **Before adding a Laview camera:** activate it (Laview ONE app or SADP tool) to set its password; give
  it a fixed address in the router; in its web page turn on RTSP and (for camera motion events) ONVIF
  with a user, and turn on motion detection. Cloud-only Wi-Fi models with no RTSP/ONVIF can't be added.
- Clips are converted to H.264, so H.265 cameras play everywhere. Passwords are kept with the other
  settings (and go into exports, like the Arlo and Blink ones).
- Tests with a fake Laview camera and a fake ONVIF camera: `python server/tests/fakecam_test.py` and
  `python server/tests/fakeonvif_test.py` (need flask, requests and ffmpeg).

## Moving to another server

**Alerts → Move to another server** (`/move`, home network only):

1. Install Arlo Keeper on the new server (Windows: the setup below). Don't sign in to Arlo there.
2. Old server: **Download export** (one .zip: Arlo and Blink accounts, sessions, phone numbers,
   alert settings, the away-from-home password, Barb's photo, the clips; or settings only).
3. Old server: **Turn off recording here** (Arlo allows one server per login).
4. New server, at `http://localhost:8090/move`: **Import** the .zip. It restarts and brings it all in.
5. New server: **Point ban.tapper.co here**, with a Cloudflare API token (Account · Cloudflare
   Tunnel · Edit and Zone · DNS · Edit). It makes a tunnel for this server, moves the
   ban.tapper.co DNS record to it, takes the name off the old tunnel (trains.tapper.co etc. stay)
   and installs cloudflared as a Windows service. Or paste a tunnel token made in the dashboard.
6. Delete the export file: it holds the passwords.

Command line instead (handy for very big exports): Ubuntu `sudo bash server/deploy/move.sh export FILE`
/ `import FILE`; Windows (admin) `"C:\Program Files\Arlo Keeper\import.ps1" FILE`.
If the old tunnel was set up from a config file on tapper (not in the Cloudflare dashboard), the DNS
move still sends ban.tapper.co to the new server; remove the ban line from tapper's config.yml to tidy up.

## Away from home

Remote access goes through a Cloudflare Tunnel (no port forwarding). Requests that arrive through
the tunnel (they carry `Cf-Connecting-IP`) must sign in; set the name and password at home on
**Alerts → Away from home**. Until one is set, outside visits are turned away. At home nothing
needs a sign-in, so other apps on the home network (GarysTV) keep working.

## Server

See [server/README.md](server/README.md). Both builds are made by GitHub Actions on every push to
`main` that touches `server/`, and published to the public prod repo:

| | Download | Workflow |
|---|---|---|
| Windows | [ArloKeeper-Setup.exe](https://github.com/userxx09-afk/prod/releases/download/arlo-windows-installer/ArloKeeper-Setup.exe) | `.github/workflows/windows-installer.yml` |
| Ubuntu | [ArloKeeper-linux.tar.gz](https://github.com/userxx09-afk/prod/releases/download/arlo-ubuntu/ArloKeeper-linux.tar.gz) | `.github/workflows/ubuntu-package.yml` |

**On Ubuntu** (22.04/24.04), install or update, leaving nothing behind in /tmp:

```
cd /tmp && curl -fLO https://github.com/userxx09-afk/prod/releases/download/arlo-ubuntu/ArloKeeper-linux.tar.gz
tar xzf ArloKeeper-linux.tar.gz && sudo bash arlo-keeper/deploy/install.sh
rm -rf arlo-keeper ArloKeeper-linux.tar.gz
```

It runs as the systemd service `arlo-keeper` on port 8090; data is in `/var/lib/arlo-keeper`,
settings in `/etc/arlo-keeper/arlo-keeper.env`. Updates never touch either. Or
`docker compose up -d --build` in `server/`.

**On Windows:** download [ArloKeeper-Setup.exe](https://github.com/userxx09-afk/prod/releases/download/arlo-windows-installer/ArloKeeper-Setup.exe)
and run it (if Windows says it protected your PC: **More info → Run anyway**). It brings its own
Python and ffmpeg, starts Arlo Keeper at every boot with no window (scheduled task "Arlo Keeper",
running as SYSTEM, restarted if it stops), lets the home network reach port 8090, includes
cloudflared for ban.tapper.co (see Moving to another server), and opens
http://localhost:8090 to sign in to Arlo. The Start menu has Restart, the log and the settings file.
Clips, settings and the Arlo sign-in are in `C:\ProgramData\ArloKeeper` and survive updates and
uninstalling. To update, run the newest setup over the old one. GitHub Actions builds it
(`.github/workflows/windows-installer.yml`, files in `server/windows/`) whenever the server changes.

Only one Arlo Keeper can use an Arlo login at a time: Arlo signs out the other one.

## DNS and addresses

`ban.tapper.co` is a Cloudflare DNS record (zone tapper.co) pointing at a **Cloudflare Tunnel**,
not at an IP address. cloudflared on the Arlo Keeper server carries it to `http://localhost:8090`.

- **Getting it right after a move:** on the new server, **Alerts → Move to another server →
  Point ban.tapper.co here** (needs a Cloudflare API token with Account · Cloudflare Tunnel · Edit
  and Zone · DNS · Edit). It makes the tunnel, changes the `ban` CNAME to
  `<tunnel-id>.cfargotunnel.com` (proxied, orange cloud), and installs cloudflared as a service.
- **Check it:** `nslookup ban.tapper.co` gives Cloudflare addresses (104.21.x / 172.67.x), never a
  home IP. `https://ban.tapper.co` shows the sign-in page from outside. In the Cloudflare dashboard,
  Zero Trust → Networks → Tunnels shows the tunnel **Healthy**, with the public hostname
  `ban.tapper.co → http://localhost:8090`. Only one tunnel may carry the name: remove it from the
  old server's tunnel (or its `config.yml` on tapper) so the two don't take turns.
- **No port on the Cloudflare name.** Cloudflare only passes the web ports, so
  `http://ban.tapper.co:8090` never answers. Use `https://ban.tapper.co`.
- **Not for motion pop-ups.** Arlo Keeper pushes motion to GarysTV at the address it was asked
  from, and through the tunnel that's only Cloudflare. A GarysTV that needs pop-ups uses the home
  or VPN address: `http://<server>:8090` on the home network, or the TapperVPN address (tapper is
  `http://100.64.0.2:8090`). Those need no password.

**GarysTV through ban.tapper.co** (clips only): Settings › Arlo › address
`https://name:password@ban.tapper.co`, with the away-from-home name and password from
**Alerts → Away from home**. GarysTV sends them with every request (HTTP Basic); characters like
`@` or `:` in the password are written `%40` and `%3A`. Eight wrong tries lock that address out
for 15 minutes.

## Android app

**Download on the phone:** [BAN.apk](https://raw.githubusercontent.com/userxx09-afk/prod/main/arlo/BAN.apk) (version 1.2, Android 8 or newer). Open the download to install; if Android asks, allow installs from your browser. After that, the app updates itself from the public prod repo: it reads `arlo/latest.json` there (copied from this repo's `downloads/`) and offers the new APK.

See [android/README.md](android/README.md) to build it. `android\build.ps1` builds a signed APK and copies
it to the server, where the Alerts page offers it (`/ban.apk`) to Android phones.

The app keeps one connection open to `GET /api/events` (server-sent events) and posts a
notification on motion, then updates it with the picture when the clip is saved. It needs to
reach the server: on the home network, or through whatever remote access you set up.

## API used by the page and the app

| Call | What |
|---|---|
| `GET /api/status` | connection, cameras (with last clip picture), clip counts |
| `GET /api/clips?camera=&starred=1&limit=&offset=` | saved clips, newest first |
| `POST /api/camera/<id>/live` · `GET …/live` · `GET …/live.mjpg` | live view |
| `POST /api/camera/<id>/record` | record a clip now |
| `POST /api/clip/<key>/star` · `DELETE /api/clip/<key>` · `POST /api/clips/bulk` | manage clips |
| `GET /api/events` | motion and clip events as they happen |
| `POST /api/photo` | change the photo |

Unofficial; uses [pyaarlo](https://github.com/twrecked/pyaarlo) to talk to Arlo.
