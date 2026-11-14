# BCN for Android

Barb's Camera Notifications on a phone: the Camera Keeper page laid out for a phone, plus a
notification the moment a camera sees motion (updated with the picture once the clip is saved).

- **Finding the server:** home Wi-Fi first (the last address that worked, then `http://10.0.0.156:8090`,
  then a quick scan of the Wi-Fi for port 8090), then `https://bcn.tapper.co` (and the old `https://ban.tapper.co`), then an address typed
  in by hand. It asks for one only if none answers. The background alerts follow the same order and
  look again whenever the phone changes network.
- **Updates:** on opening (at most twice a day) and from Alerts > This phone > Check for updates, the
  app reads `https://raw.githubusercontent.com/userxx09-afk/prod/main/bcn/latest.json` (the public
  prod repo; this repo is private, so phones can't read it). If `versionCode` is newer, it downloads
  the APK named there, checks its SHA-256 and hands it to Android's installer. The **Publish BCN
  release** workflow copies `downloads/BCN.apk` and `latest.json` to `prod/bcn/` on every change, and `latest.json`
  to `prod/arlo/` too, where phones still on BAN look.
  BCN 1.2 and older still look in this private repo and never find an update: install 1.3 by hand
  once (`/bcn.apk` on Camera Keeper, or the prod link below), then it updates itself.
- `MotionService` keeps one connection open to `/api/events` and posts the notifications.
  It works wherever the phone can reach the server: home Wi-Fi, or TapperVPN when away.
- **Renamed from BAN:** the package name (`local.ban`) and signing key (`BAN-signing-key`, alias
  `ban`) stay as they were, so BCN installs over BAN as an update. Don't change either.
- Alerts page > "This phone": notifications on/off, test, server address, battery setting.

## Publishing an update

1. Raise the version: `$env:BAN_VERSION_CODE="4"; $env:BAN_VERSION_NAME="1.3"`, then run `build.ps1`.
2. Copy `build\BCN.apk` to `downloads/BCN.apk` in the repo, and update `downloads/latest.json`
   (`versionCode`, `versionName`, `sha256` of the APK, `notes`). Push. Phones pick it up on their next check.
3. Merging to `main` runs **Publish BCN release** by itself (or run it from the Actions tab). It copies
   both files to `prod/bcn/` (and `latest.json` to `prod/arlo/` for phones still on BAN), and makes
   the `bcn-v<version>` release there.
   Install link: https://raw.githubusercontent.com/userxx09-afk/prod/main/bcn/BCN.apk

## Build

    build.ps1            # signed release APK -> build\BCN.apk, and copies it to tapper

`signing.properties` (not checked in) points at BCN's own key in `..\..\BCN-signing-key`.
Keep that key: Android only updates the app if every version is signed with it.
