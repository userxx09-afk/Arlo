"""Entry point: starts the recorder in the background and the web viewer in front."""
import logging
import threading

from . import arlo_login, config, store, web
from .recorder import Recorder


def main():
    logging.basicConfig(level=config.LOG_LEVEL,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    log = logging.getLogger("main")
    store.init()
    web.init_secret()

    rec = Recorder()
    web.recorder = rec

    def run_recorder():
        # Never retry a failed login on our own: Arlo rate-limits the account for
        # about a day after repeated failures. Wait for the Connect page instead.
        while True:
            user, pw = arlo_login.credentials()
            if not user:
                rec.status.update(connected=False, error=None, needs_login=True)
                log.info("waiting for Arlo sign-in on the Connect page")
                arlo_login.wait_for_new_credentials()
                continue
            rec.status.update(needs_login=False, signing_in=True, error=None)
            try:
                rec.start(user, pw)
                rec.status["signing_in"] = False
                return
            except Exception as e:
                msg = str(e)
                if any(s in msg for s in ("Password not correct", "Account locked", "9015", "9017")):
                    # Forget the bad password so a reboot doesn't try it again and lock the account.
                    arlo_login.forget_password()
                    if "locked" in msg.lower() or "9017" in msg:
                        msg += " - Arlo locked sign-ins for this login. Wait before trying again, or reset the password at my.arlo.com."
                rec.status.update(signing_in=False, connected=False, error=msg, needs_login=True)
                log.error("Arlo sign-in failed: %s", msg)
                arlo_login.wait_for_new_credentials()

    threading.Thread(target=run_recorder, name="recorder", daemon=True).start()

    from waitress import serve
    log.info("web viewer on port %d", config.WEB_PORT)
    serve(web.app, host="0.0.0.0", port=config.WEB_PORT, threads=48, channel_timeout=300, connection_limit=200)


if __name__ == "__main__":
    main()
