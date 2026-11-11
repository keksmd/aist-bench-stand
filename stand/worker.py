"""Queue worker: executes queued runs one at a time, oldest first.

On start, runs left in ``running`` by a previous worker process are marked ``failed`` with a
note, because their containers did not survive the restart.
"""
from __future__ import annotations

import logging
import time

from . import db, executor, registry

log = logging.getLogger("stand.worker")


def recover(con) -> None:
    con.execute("UPDATE runs SET status='failed', finished=?, error='worker restarted during the run'"
                " WHERE status='running'", (time.time(),))


def loop(idle_s: float = 5.0) -> None:
    con = db.connect()
    recover(con)
    while True:
        row = db.claim(con)
        if not row:
            time.sleep(idle_s)
            continue
        log.info("run %s: %s on %s", row["id"], row["bench"], row["mas"])
        try:
            code = executor.execute(con, row, registry.catalogue())
            log.info("run %s finished with %s", row["id"], code)
        except Exception:
            log.exception("run %s failed", row["id"])
