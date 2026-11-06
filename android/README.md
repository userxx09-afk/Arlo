# BAN for Android

Barb's Arlo Notifications on a phone: the Arlo Keeper page laid out for a phone, plus a
notification the moment a camera sees motion (updated with the picture once the clip is saved).

- The app shows `http://<server>:8090` (asked on first start; default `http://10.0.0.156:8090`).
- `MotionService` keeps one connection open to `/api/events` and posts the notifications.
  It works wherever the phone can reach the server: home Wi-Fi, or TapperVPN when away.
- Alerts page > "This phone": notifications on/off, test, server address, battery setting.

## Build

    build.ps1            # signed release APK -> build\BAN.apk, and copies it to tapper

`signing.properties` (not checked in) points at BAN's own key in `..\..\BAN-signing-key`.
Keep that key: Android only updates the app if every version is signed with it.
