"""All the Arlo accounts BAN watches, behind one object the web page uses like a single recorder.

Each account gets its own Recorder (its own pyaarlo sign-in and session folder). If two accounts
can see the same camera, the first account that has it owns it: only that one records it, so a
shared camera isn't recorded twice.
"""
import logging
import threading

from . import arlo_login
from .recorder import Recorder

log = logging.getLogger("accounts")


class _Captures:
    """Clips recording right now, across every account (for GarysTV's /live/ pop-ups)."""

    def __init__(self, fleet):
        self.fleet = fleet

    def live_file(self, live_id):
        for r in self.fleet.recorders():
            path, recording = r.capture.live_file(live_id)
            if path:
                return path, recording
        return None, False

    def busy(self, cam_id):
        o = self.fleet.owner(cam_id)
        return bool(o and o.capture.busy(cam_id))


class Fleet:
    def __init__(self):
        self._recs = {}          # account id -> Recorder, in the order accounts were added
        self._lock = threading.Lock()
        self.capture = _Captures(self)

    # --- accounts ---------------------------------------------------------
    def start_all(self):
        for a in arlo_login.accounts():
            self._start(a["id"])

    def recorders(self):
        with self._lock:
            return list(self._recs.values())

    def _start(self, aid):
        with self._lock:
            if aid in self._recs:
                return self._recs[aid]
            rec = self._recs[aid] = Recorder(aid, self)
        threading.Thread(target=self._run, args=(aid, rec), name=f"signin-{aid}", daemon=True).start()
        return rec

    def _run(self, aid, rec):
        # Never retry a failed sign-in on our own: Arlo locks the account for about a day after
        # repeated failures. Wait for a new password on the Arlo page instead.
        lg = arlo_login.login(aid)
        while not lg.removed:
            user, pw = arlo_login.credentials(aid)
            rec.username = user or rec.username
            if not user or not pw:
                rec.status.update(connected=False, error=rec.status.get("error"), needs_login=True, signing_in=False)
                arlo_login.wait_for_new_credentials(aid)
                continue
            rec.status.update(needs_login=False, signing_in=True, error=None)
            try:
                rec.start(user, pw)
                rec.status["signing_in"] = False
                if lg.removed:      # signed out while signing in
                    rec.stop()
                return
            except Exception as e:
                msg = str(e)
                if any(s in msg for s in ("Password not correct", "Account locked", "9015", "9017")):
                    arlo_login.forget_password(aid)   # so a restart doesn't retry it and lock the account
                    if "locked" in msg.lower() or "9017" in msg:
                        msg += " - Arlo locked sign-ins for this login. Wait before trying again, or reset the password at my.arlo.com."
                rec.status.update(signing_in=False, connected=False, error=msg, needs_login=True)
                log.error("Arlo sign-in failed for %s: %s", user, msg)
                arlo_login.wait_for_new_credentials(aid)

    def add(self, username, password):
        """Add an account (or retry one with a new password). Returns its id."""
        aid = arlo_login.add(username, password)
        rec = self._start(aid)
        if rec.arlo:
            log.info("%s is already signed in", username)
        return aid

    def sign_out(self, aid):
        with self._lock:
            rec = self._recs.pop(aid, None)
        arlo_login.remove(aid)
        if rec:
            log.info("signing out of %s", rec.username)
            try:
                rec.stop()
            except Exception as e:
                log.warning("stopping %s: %s", rec.username, e)

    def accounts_view(self):
        """For the Arlo page: each account and how its sign-in is going."""
        out = []
        for a in arlo_login.accounts():
            rec = self._recs.get(a["id"])
            st = rec.status if rec else {}
            lg = arlo_login.login(a["id"])
            cams = list(rec.arlo.cameras) if rec and rec.arlo else []
            mine = [c.name for c in cams]
            # cameras another (earlier) account also has; that one records them
            shared = [c.name for c in cams if not self.owns(rec, c.device_id)]
            out.append({"id": a["id"], "username": a["username"], "connected": bool(st.get("connected")),
                        "signing_in": bool(st.get("signing_in")), "error": st.get("error"),
                        "needs_login": bool(st.get("needs_login")) or not a.get("password"),
                        "waiting_for_code": lg.state["waiting_for_code"],
                        "cameras": mine, "shared": shared})
        return out

    # --- what the web page uses (same as a single Recorder) -----------------
    @property
    def arlo(self):
        return any(r.arlo for r in self.recorders())

    @property
    def status(self):
        recs = self.recorders()
        connected = [r for r in recs if r.status.get("connected")]
        errors = [r.status.get("error") for r in recs if r.status.get("error") and not r.status.get("connected")]
        sweeps = [r.status.get("last_sweep") for r in recs if r.status.get("last_sweep")]
        return {"connected": bool(connected),
                "signing_in": any(r.status.get("signing_in") for r in recs),
                "needs_login": not recs or any(r.status.get("needs_login") for r in recs),
                "error": errors[0] if errors else None,
                "last_sweep": max(sweeps) if sweeps else None,
                "accounts": len(recs),
                "cameras": [c for r in connected for c in r.status.get("cameras", [])]}

    def owner(self, cam_id):
        """The account that records this camera: the first one (in the order added) that has it."""
        for r in self.recorders():
            if r.arlo and any(c.device_id == cam_id for c in r.arlo.cameras):
                return r
        return None

    def owns(self, rec, cam_id):
        o = self.owner(cam_id)
        return o is None or o is rec

    def camera(self, cam_id):
        o = self.owner(cam_id)
        return o.camera(cam_id) if o else None

    def cameras_view(self):
        out, seen = [], set()
        for r in self.recorders():
            for c in r.cameras_view():
                if c["id"] in seen:
                    continue
                seen.add(c["id"])
                out.append({**c, "account": r.username})
        return out

    def modes(self):
        recs = [r for r in self.recorders() if r.arlo]
        out = {}
        for r in recs:
            for k, v in r.modes().items():
                out[f"{r.username}: {k}" if len(recs) > 1 else k] = v
        return out

    def record_now(self, cam_id):
        o = self.owner(cam_id)
        return bool(o and o.record_now(cam_id))

    def test_motion(self, cam_id):
        o = self.owner(cam_id)
        return bool(o and o.test_motion(cam_id))

    def sweep(self, refresh=True):
        for r in self.recorders():
            if r.arlo:
                r.sweep(refresh)
