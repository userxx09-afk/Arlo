"""Entry point: starts the recorder in the background and the web viewer in front."""
import logging
import threading

from . import clips_location, config, migrate, store, web
from .accounts import Fleet


def main():
    logging.basicConfig(level=config.LOG_LEVEL,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    log = logging.getLogger("main")
    migrate.apply_pending()  # an export uploaded on the Move page, applied before anything opens the database
    clips_location.finish_move()   # clips moved to another folder: remove the old copies
    store.init()
    web.init_secret()

    fleet = Fleet()          # one Recorder per Arlo account
    web.recorder = fleet
    moved = store.get_setting("moved_away")
    if moved:
        log.warning("recording is off here: moved to another server on %s (turn it back on from the Move page)",
                    moved.get("at"))
    else:
        fleet.start_all()

    from waitress import serve
    log.info("web viewer on port %d", config.WEB_PORT)
    serve(web.app, host="0.0.0.0", port=config.WEB_PORT, threads=48, channel_timeout=300, connection_limit=200,
          max_request_body_size=1 << 40)   # the Move page uploads whole exports


if __name__ == "__main__":
    main()
