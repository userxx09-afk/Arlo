"""Entry point: starts the recorder in the background and the web viewer in front."""
import logging
import threading

from . import config, store, web
from .accounts import Fleet


def main():
    logging.basicConfig(level=config.LOG_LEVEL,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    log = logging.getLogger("main")
    store.init()
    web.init_secret()

    fleet = Fleet()          # one Recorder per Arlo account
    web.recorder = fleet
    fleet.start_all()

    from waitress import serve
    log.info("web viewer on port %d", config.WEB_PORT)
    serve(web.app, host="0.0.0.0", port=config.WEB_PORT, threads=48, channel_timeout=300, connection_limit=200)


if __name__ == "__main__":
    main()
