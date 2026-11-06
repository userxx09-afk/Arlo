# Arlo Keeper

Keeps local copies of your Arlo recordings (trimmed to 20 seconds), sends alerts, and gives you a web page to browse them.

```
Arlo cameras ──► Arlo cloud / base-station USB ──► Arlo Keeper ──► ./data/clips/<camera>/<date>/<time>.mp4
                                                       │
                                                       ├──► alerts (ntfy / Pushover / SMS / webhook)
                                                       └──► web viewer on :8080
```

## How it works

- Logs in with [pyaarlo](https://github.com/twrecked/pyaarlo) and listens to Arlo's live event feed.
- **Live capture (main path):** when a camera reports motion, the app opens that camera's live view and records `CLIP_MAX_SECONDS` (20 s) with ffmpeg. It converts the video from HEVC to H.264 so it plays in any browser, makes a thumbnail, saves the clip, and sends the alert. This works without an Arlo subscription because live view is free. If motion is still going when the clip ends, it records again, up to 3 clips in a row. Each camera waits `CAPTURE_COOLDOWN_SECONDS` between captures to save battery.
- **Cloud download (backup):** if Arlo saved its own recording (plan or legacy free storage), that clip is downloaded too.
- The home page shows each camera's battery, last motion, last clip and any error, with a **Record test clip** button.
- A sweep every `POLL_MINUTES` catches anything the live feed missed. On first run it back-fills `LIBRARY_DAYS`.
- If the base station has a USB drive with local recording on, it also pulls from there. Those clips are tagged "USB".
- Retention removes un-starred clips older than `KEEP_DAYS` or past `MAX_STORAGE_GB`. Starred clips are kept forever.

## Setup

1. **In the Arlo app**, set each camera's motion rule to record a fixed 20 seconds. Arlo decides what gets recorded; this app saves it. The 20-second trim here is a backstop.
2. **Make a dedicated Arlo login (recommended).** Create a second Arlo account, then in the main account go to Settings → Grant Access and share the cameras with it. Arlo signs out other sessions on the same account, so this keeps your phone app logged in.
3. Copy `.env.example` to `.env` and fill in at least `ARLO_USERNAME` / `ARLO_PASSWORD`, plus one notification channel.
4. Start it:
   ```
   docker compose up -d --build
   docker attach arlo-keeper     # type the 2FA code Arlo emails you, then Ctrl-P Ctrl-Q
   ```
   The session is saved in `./data/state`, so 2FA is only needed again if Arlo expires it. To skip typing codes, use `TFA_SOURCE=imap` with an email app password.
5. Open `http://<host>:8080`.

## Alerts

The first time you open the page it asks who should get a text. Add numbers any time under **Alerts**, where you can also pause or remove them and send a test. Numbers can be typed any way (`7735550123`, `(773) 555-0123`); 10-digit numbers are treated as US.

Texts go through Twilio, since there's no free way to send SMS reliably. Put your Twilio Account SID, Auth Token and Twilio number on the Alerts page. Until then, numbers are saved but nothing is sent, and the test button says so.

Alerts fire when a clip is saved. If a camera reports motion but no clip shows up within 2 minutes, which is normal for cameras with no subscription and no hub storage, you still get a "no recording available" text.

| Channel | Cost | Notes |
|---|---|---|
| ntfy | Free | Easiest. Install the ntfy app, subscribe to your topic, and set `NTFY_URL`. Thumbnail included. |
| Pushover | $5 once | Thumbnail included. |
| Twilio SMS | ~1¢/text + number | Real text messages. Add numbers on the Alerts page. |
| Webhook | Free | JSON POST, e.g. to a Home Assistant automation. |

The Arlo app's own notifications keep working alongside these.

## Notes and limits

- **No subscription:** clips come from live capture. Cameras must be in an armed mode with motion detection on, or Arlo never sends the motion event.
- **Clips start 2–4 seconds after motion,** while the camera wakes up and the stream connects. The first moment of an event can be missed.
- **Battery:** each capture keeps a battery camera streaming for about 25 seconds. Raise `CAPTURE_COOLDOWN_SECONDS` if batteries drain too fast.
- **Shared logins:** if a camera shows "Arlo refused the live stream (403)", the login doesn't have streaming rights for that camera or its hub. In the owner's Arlo app, go to Settings → Grant Access, edit the user, and turn on Allow Access Rights for that camera and hub.
- Pulling from hub storage uses pyaarlo's local base-station API. It's the least-tested path, so check the log on first run for a line saying the hub's local library was enabled.
- This is unofficial and reverse-engineered. Arlo login changes occasionally break pyaarlo until it's updated, so run `docker compose build --pull` after a pyaarlo release.
- Person/vehicle/animal tags only show up if Arlo's subscription detection is on.

## Layout

```
app/recorder.py   Arlo login, events, cloud download
app/capture.py    live-view recording on motion
app/store.py      SQLite index + retention
app/notify.py     ntfy / Pushover / Twilio / webhook
app/web.py        viewer (Flask + waitress)
data/             clips, clips.db, Arlo session   (created on first run)
```

## BAN (Barb's Arlo Notifications)

The web page is branded BAN: GarysTV's navy look with an orange accent, Barb's photo at the top
(tap it to change; stored as `data/photo.jpg`, default `app/static/photo-default.jpg`).

- **Cameras**: battery, last motion, **Watch live** (on demand) and **Record clip**.
  Live view (`app/liveview.py`) is one shared session per camera, sent as MJPEG so it plays in
  any browser. It stops 5 s after the last viewer leaves, or after 3 minutes, to save batteries.
- **Recordings**: newest first, grouped by day or month, filter by camera, starred only,
  select several to star/delete. New clips appear on their own.
- **Phone app**: `../android` (build with `android\build.ps1`). It shows this page laid out for
  a phone, and notifies on motion through `GET /api/events` (server-sent events, `app/events.py`).
  The Alerts page offers the APK at `/ban.apk` to Android phones.

New API: `/api/status`, `/api/camera/<id>/live` (POST start, GET state), `/api/camera/<id>/live.mjpg`,
`/api/camera/<id>/record`, `/api/clip/<key>/star`, `DELETE /api/clip/<key>`, `/api/clips/bulk`,
`/api/events`, `/api/test-phone`, `/api/photo`. `/api/clips` also takes `offset` and `starred=1`.
GarysTV's calls (`/api/cameras`, `/api/subscribe`, `/api/clips`, `/live/…`) are unchanged.
