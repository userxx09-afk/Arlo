"""Motion and clip events, sent live to the BAN phone app (and any open web page).

The phone app keeps one connection open to GET /api/events (server-sent events). When a
camera sees motion it gets a "motion" event straight away, then a "clip" event once the
clip is saved (with its picture), so the notification can be updated with a thumbnail.
"""
import itertools
import json
import queue
import threading
import time

_lock = threading.Lock()
_listeners = set()
_ids = itertools.count(1)
_recent = []          # last few events, so a phone that just reconnected doesn't miss one
RECENT_SECONDS = 120


def publish(kind, **data):
    ev = {"id": next(_ids), "event": kind, "time": int(time.time() * 1000), **data}
    with _lock:
        _recent.append(ev)
        cutoff = ev["time"] - RECENT_SECONDS * 1000
        while _recent and _recent[0]["time"] < cutoff:
            _recent.pop(0)
        for q in list(_listeners):
            try:
                q.put_nowait(ev)
            except queue.Full:
                pass
    return ev


def listeners():
    with _lock:
        return len(_listeners)


def stream(since_ms=0, heartbeat=20):
    """Yields server-sent-event text. Sends events newer than `since_ms` first."""
    q = queue.Queue(maxsize=100)
    with _lock:
        backlog = [e for e in _recent if since_ms and e["time"] > since_ms]
        _listeners.add(q)
    try:
        yield "retry: 5000\n\n"
        for ev in backlog:
            yield _fmt(ev)
        while True:
            try:
                yield _fmt(q.get(timeout=heartbeat))
            except queue.Empty:
                yield ": ping\n\n"   # keeps the connection (and phone Wi-Fi) from timing out
    finally:
        with _lock:
            _listeners.discard(q)


def _fmt(ev):
    return f"id: {ev['id']}\nevent: {ev['event']}\ndata: {json.dumps(ev)}\n\n"
