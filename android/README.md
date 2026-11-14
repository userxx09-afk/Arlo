# BAN for Android

Barb's Arlo Notifications on a phone: the Arlo Keeper page laid out for a phone, plus a
notification the moment a camera sees motion (updated with the picture once the clip is saved).

- **Finding the server:** home Wi-Fi first (the last address that worked, then `http://10.0.0.156:8090`,
  then a quick scan of the Wi-Fi for port 8090), then `https://ban.tapper.co`, then an address typed
  in by hand. It asks for one only if none answers. The background alerts follow the same order and
  look again whenever the phone changes network.
- **Updates:** on opening (at most twice a day) and from Alerts > This phone > Check for updates, the
  app reads `https://raw.githubusercontent.com/userxx09-afk/prod/main/arlo/latest.json` (the public
  prod repo; this repo is private, so phones can't read it). If `versionCode` is newer, it downloads
  the APK named there, checks its SHA-256 and hands it to Android's installer. The **Publish BAN
  release** workflow copies `downloads/BAN.apk` and `latest.json` to `prod/arlo/` on every change.
  BAN 1.2 and older still look in this private repo and never find an update: install 1.3 by hand
  once (`/ban.apk` on Arlo Keeper, or the prod link below), then it updates itself.
- `MotionService` keeps one connection open to `/api/events` and posts the notifications.
  It works wherever the phone can reach the server: home Wi-Fi, or TapperVPN when away.
- Alerts page > "This phone": notifications on/off, test, server address, battery setting.

## Publishing an update

1. Raise the version: `$env:BAN_VERSION_CODE="4"; $env:BAN_VERSION_NAME="1.3"`, then run `build.ps1`.
2. Copy `build\BAN.apk` to `downloads/BAN.apk` in the repo, and update `downloads/latest.json`
   (`versionCode`, `versionName`, `sha256` of the APK, `notes`). Push. Phones pick it up on their next check.
3. Merging to `main` runs **Publish BAN release** by itself (or run it from the Actions tab). It copies
   both files to `prod/arlo/`, where phones look, and makes the `arlo-ban-v<version>` release there.
   Install link: https://raw.githubusercontent.com/userxx09-afk/prod/main/arlo/BAN.apk

## Build

    build.ps1            # signed release APK -> build\BAN.apk, and copies it to tapper

`signing.properties` (not checked in) points at BAN's own key in `..\..\BAN-signing-key`.
Keep that key: Android only updates the app if every version is signed with it.
