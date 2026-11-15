#!/usr/bin/env python3
"""Relay for after BCN moved off tapper without changing Cloudflare.

The Cloudflare tunnel on tapper sends bcn.tapper.co to http://localhost:8090. Once Camera Keeper
on tapper is off, this listens there and passes every connection, unchanged, to the server BCN
moved to (over the VPN). Visits keep their Cloudflare headers, so the new server still treats
them as away-from-home (password). Not needed once bcn.tapper.co is pointed at the new server
from its Move page; then: sudo systemctl disable --now bcn-relay

Usage: bcn-relay.py TARGET_HOST TARGET_PORT LISTEN_PORT     (see bcn-relay.service)
"""
import asyncio
import sys

HOST, PORT, LISTEN = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])


async def pipe(r, w):
    try:
        while True:
            d = await r.read(65536)
            if not d:
                break
            w.write(d)
            await w.drain()
    except Exception:
        pass
    finally:
        try:
            w.close()
        except Exception:
            pass


async def handle(cr, cw):
    try:
        tr, tw = await asyncio.wait_for(asyncio.open_connection(HOST, PORT), 10)
    except Exception:
        cw.close()
        return
    await asyncio.gather(pipe(cr, tw), pipe(tr, cw))


async def main():
    srv = await asyncio.start_server(handle, ["127.0.0.1", "::1"], LISTEN)
    async with srv:
        await srv.serve_forever()


asyncio.run(main())
