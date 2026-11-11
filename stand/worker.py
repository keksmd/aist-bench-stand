"""Queue worker: executes queued runs one at a time, oldest first.

SIGTERM is graceful: the worker finishes the run in progress and then exits, so a deploy that
restarts the service never kills a run (the unit allows an unlimited stop timeout). On start,
runs left in ``running`` by a worker that was killed are marked ``failed`` with a note.
"""
from __future__ import annotations

import logging
import signal
import time

from . import db, executor

log = logging.getLogger("stand.worker")
stopping = False


def _stop(signum, frame):
    global stopping
    stopping = True
    log.info("stop requested; finishing the current run first")


def recover(con) -> None:
    con.execute("UPDATE runs SET status='failed', finished=?, error='worker stopped during the run'"
                " WHERE status='running'", (time.time(),))


def loop(idle_s: float = 5.0) -> None:
    signal.signal(signal.SIGTERM, _stop)
    con = db.connect()
    recover(con)
    while not stopping:
        row = db.claim(con)
        if not row:
            time.sleep(idle_s)
            continue
        log.info("run %s: %s on %s", row["id"], row["bench"], row["mas"])
        try:
            code = executor.execute(con, row)
            log.info("run %s finished with %s", row["id"], code)
        except Exception:
            log.exception("run %s failed", row["id"])
    log.info("worker stopped")
