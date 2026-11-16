# Global BCN: sign-in with Google, each person sees only their own cameras (plan)

Decided Nov 15 2026: **everyone signs in with Google**, and **each person adds
their own Arlo/Blink account**; they only ever see the cameras, clips, live
view and alerts from accounts they added. You (the owner) approve which Google
accounts may get in, and can see everything.

## What's there today

- One shared web password (`WEB_USER` / `WEB_PASSWORD` in `config.py`,
  checked by `_auth` in `web.py`).
- Several Arlo and Blink accounts can already be added (`accounts.py`,
  `blink.py`), but every account's cameras and clips are shown to everyone.
- Clips are stored with a `camera_id` (`store.py`); cameras belong to the
  account that found them.

## Changes

1. **Users table** (`store.py`): Google email, name, role (`owner` or
   `user`), approved yes/no, created/last-seen. The first sign-in by the
   owner's Google account makes it the owner.
2. **Sign in with Google** (`web.py`): OAuth "Sign in with Google" on the web
   page; a signed session cookie afterward. Unknown Google accounts land on a
   "waiting for approval" page and show up for the owner to approve.
3. **Ownership**: every Arlo/Blink account gets an `owner` (user id). The
   accounts page only lists and adds accounts for the signed-in person.
   Existing accounts are assigned to the owner on first start (migration in
   `migrate.py`).
4. **Filtering everywhere**: clips, days/months, cameras, live view, starring
   and deleting all check that the camera belongs to one of the signed-in
   person's accounts, on the server side, not just hidden in the page.
5. **Alerts**: each person sets their own alert phone number and push
   devices; a motion clip only alerts the people who own that camera.
6. **Android app**: "Sign in with Google" on first open (Android's own
   account picker), then it sends a token with each request. Same server
   finder as now (home network, then bcn.tapper.co).
7. **GarysTV pop-ups**: keep working through a server-to-server key; each TV
   is linked to a user so it only pops up that person's cameras.
8. **Owner page**: approve/remove people; see which accounts each one added.

## Needed from you

- A Google Cloud project with an OAuth client (web + Android) for
  `bcn.tapper.co`. I can walk you through it; the client secret goes in the
  private repo, not here.

## Build order

1. Users + Google sign-in on the web page (replacing the shared password).
2. Account ownership + server-side filtering + migration.
3. Per-person alerts.
4. Android sign-in and app release; GarysTV link per user.
