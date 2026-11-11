"""SQLite index of runs and task outcomes; files stay in the results directory.

The ``runs`` table is also the queue: the worker takes the oldest ``queued`` row.
"""
from __future__ import annotations

import json
import os
import secrets
import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY, mas TEXT NOT NULL, bench TEXT NOT NULL, overrides TEXT NOT NULL,
  ref TEXT, note TEXT, submitted_by TEXT, status TEXT NOT NULL,
  created REAL NOT NULL, started REAL, finished REAL, exit_code INTEGER,
  done INTEGER DEFAULT 0, total INTEGER, error TEXT, manifest TEXT
);
CREATE INDEX IF NOT EXISTS runs_status ON runs(status, created);
CREATE TABLE IF NOT EXISTS tasks (
  run_id TEXT NOT NULL, trace TEXT NOT NULL, task_id TEXT, condition TEXT, repeat INTEGER,
  success INTEGER, reason TEXT, llm_calls INTEGER, total_tokens INTEGER, duration_s REAL,
  tool_calls INTEGER, tool_errors INTEGER, PRIMARY KEY (run_id, trace)
);
"""
STATUSES = ("queued", "running", "done", "failed", "cancelled")


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    path = Path(path or os.environ.get("STAND_DB", "/srv/bench/stand.db"))
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, timeout=30, isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(SCHEMA)
    return con


def submit(con, mas: str, bench: str, overrides: dict, ref: str | None, note: str, by: str) -> str:
    run_id = f"{time.strftime('%Y%m%d-%H%M%S', time.gmtime())}-{bench}-{mas}-{secrets.token_hex(2)}"
    con.execute("INSERT INTO runs (id, mas, bench, overrides, ref, note, submitted_by, status, created)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (run_id, mas, bench, json.dumps(overrides), ref or None, note, by, "queued", time.time()))
    return run_id


def claim(con) -> sqlite3.Row | None:
    con.execute("BEGIN IMMEDIATE")
    try:
        row = con.execute("SELECT * FROM runs WHERE status='queued' ORDER BY created LIMIT 1").fetchone()
        if row:
            con.execute("UPDATE runs SET status='running', started=? WHERE id=?", (time.time(), row["id"]))
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    return row


def update(con, run_id: str, **fields) -> None:
    if "manifest" in fields and not isinstance(fields["manifest"], str):
        fields["manifest"] = json.dumps(fields["manifest"])
    cols = ", ".join(f"{k}=?" for k in fields)
    con.execute(f"UPDATE runs SET {cols} WHERE id=?", (*fields.values(), run_id))


def put_tasks(con, run_id: str, rows: list[dict]) -> None:
    con.executemany(
        "INSERT OR REPLACE INTO tasks VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        [(run_id, r["trace"], r["task_id"], r["condition"], r["repeat"],
          None if r["success"] is None else int(r["success"]), r["reason"], r["llm_calls"],
          r["total_tokens"], r["duration_s"], r["tool_calls"], r["tool_errors"]) for r in rows])


def get(con, run_id: str) -> dict | None:
    row = con.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    return as_dict(row) if row else None


def as_dict(row) -> dict:
    d = dict(row)
    d["overrides"] = json.loads(d["overrides"] or "{}")
    d["manifest"] = json.loads(d["manifest"]) if d.get("manifest") else None
    return d
