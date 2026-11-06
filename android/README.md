# BAN for Android

Barb's Arlo Notifications on a phone: the Arlo Keeper page laid out for a phone, plus a
notification the moment a camera sees motion (updated with the picture once the clip is saved).

- **Finding the server:** home Wi-Fi first (the last address that worked, then `http://10.0.0.156:8090`,
  then a quick scan of the Wi-Fi for port 8090), then `https://ban.tapper.co`, then an address typed
  in by hand. It asks for one only if none answers. The background alerts follow the same order and
  look again whenever the phone changes network.
- **Updates:** on opening (at most twice a day) and from Alerts > This phone > Check for updates, the
  app reads `downloads/latest.json` in the GitHub repo. If `versionCode` is newer, it downloads the
  APK, checks its SHA-256 and hands it to Android's installer.
- `MotionService` keeps one connection open to `/api/events` and posts the notifications.
  It works wherever the phone can reach the server: home Wi-Fi, or TapperVPN when away.
- Alerts page > "This phone": notifications on/off, test, server address, battery setting.

## Publishing an update

1. Raise the version: `$env:BAN_VERSION_CODE="4"; $env:BAN_VERSION_NAME="1.3"`, then run `build.ps1`.
2. Copy `build\BAN.apk` to `downloads/BAN.apk` in the repo, and update `downloads/latest.json`
   (`versionCode`, `versionName`, `sha256` of the APK, `notes`). Push. Phones pick it up on their next check.
3. Run **Actions > Publish BAN release** so the download link (`releases/latest/download/BAN.apk`) serves the new version too.

## Build

    build.ps1            # signed release APK -> build\BAN.apk, and copies it to tapper

`signing.properties` (not checked in) points at BAN's own key in `..\..\BAN-signing-key`.
Keep that key: Android only updates the app if every version is signed with it.
