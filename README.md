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
- **Alerts**: text messages (Twilio), ntfy, Pushover, webhook, and the phone app.
- Lays itself out for a phone with a bottom tab bar.
- The photo at the top: tap it to change it, or put `photo-default.jpg` in `server/app/static/`.

## Server

See [server/README.md](server/README.md). On Ubuntu: `sudo bash server/deploy/install.sh`
(runs on port 8090 as a systemd service), or `docker compose up -d --build` in `server/`.

## Android app

See [android/README.md](android/README.md). `android\build.ps1` builds a signed APK and copies
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
