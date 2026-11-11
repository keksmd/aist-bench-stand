"""HTTP API and UI for the stand.

Read endpoints serve the run index, per-condition pass counts, task outcomes, traces in the
common event format, logs and manifests. ``POST /api/runs`` queues a run; ``POST
/api/runs/{id}/cancel`` cancels one. Authentication is done by the reverse proxy in front of
this app, which passes the user name in ``X-Stand-User``.
"""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel, Field

from . import db, executor, registry

app = FastAPI(title="AIST benchmark stand")
UI = registry.ROOT / "ui" / "index.html"
LIST_COLS = "id, mas, bench, overrides, ref, note, submitted_by, status, created, started, finished, exit_code, done, total, error"


def con():
    return db.connect()


class RunRequest(BaseModel):
    mas: str
    bench: str
    overrides: dict[str, str | int] = Field(default_factory=dict)
    ref: str | None = None
    note: str = ""


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(UI)


@app.get("/api/catalog")
def catalog():
    cat = registry.catalogue()
    pick = lambda s, keys: {k: s.data.get(k) for k in keys} | {"sha256": s.sha256}
    return {"mas": [pick(s, ("id", "family", "description", "repo", "ref", "agents", "edges", "tools", "params", "choices"))
                    for s in cat["mas"].values()],
            "benchmarks": [pick(s, ("id", "family", "description", "params", "choices", "adapter"))
                           for s in cat["benchmarks"].values()]}


def _pass_counts(c, run_ids):
    if not run_ids:
        return {}
    q = ",".join("?" * len(run_ids))
    out = defaultdict(lambda: {"pass": 0, "fail": 0})
    for r in c.execute(f"SELECT run_id, success, COUNT(*) n FROM tasks WHERE run_id IN ({q}) AND success IS NOT NULL"
                       f" GROUP BY run_id, success", run_ids):
        out[r["run_id"]]["pass" if r["success"] else "fail"] += r["n"]
    return out


@app.get("/api/runs")
def runs(limit: int = 100, status: str | None = None):
    c = con()
    sql, args = f"SELECT {LIST_COLS} FROM runs", []
    if status:
        sql, args = sql + " WHERE status=?", [status]
    rows = [dict(r) for r in c.execute(sql + " ORDER BY created DESC LIMIT ?", (*args, min(limit, 500)))]
    counts = _pass_counts(c, [r["id"] for r in rows])
    for r in rows:
        r["overrides"] = json.loads(r["overrides"] or "{}")
        r["outcomes"] = counts.get(r["id"], {"pass": 0, "fail": 0})
    return rows


@app.get("/api/runs/{run_id}")
def run(run_id: str):
    c = con()
    r = db.get(c, run_id)
    if not r:
        raise HTTPException(404, "no such run")
    tasks = [dict(t) for t in c.execute("SELECT * FROM tasks WHERE run_id=? ORDER BY condition, task_id, repeat", (run_id,))]
    by_cond = defaultdict(lambda: {"pass": 0, "fail": 0, "tokens": 0})
    for t in tasks:
        if t["success"] is None:
            continue
        b = by_cond[t["condition"]]
        b["pass" if t["success"] else "fail"] += 1
        b["tokens"] += t["total_tokens"] or 0
    r["conditions"] = dict(sorted(by_cond.items()))
    r["tasks"] = tasks
    return r


def _run_dir(run_id: str) -> Path:
    d = (executor.results_root() / run_id).resolve()
    if d.parent != executor.results_root().resolve() or not d.is_dir():
        raise HTTPException(404, "no such run directory")
    return d


def _outputs(run_id: str) -> Path:
    d = _run_dir(run_id)
    for name in ("outputs", "live"):
        if (d / name).exists():
            return d / name
    raise HTTPException(404, "no outputs yet")


@app.get("/api/runs/{run_id}/trace/{trace}")
def trace(run_id: str, trace: str):
    c = con()
    r = db.get(c, run_id)
    if not r:
        raise HTTPException(404, "no such run")
    bench = registry.catalogue()["benchmarks"].get(r["bench"])
    if "/" in trace or not trace.endswith(".jsonl"):
        raise HTTPException(400, "bad trace name")
    path = _outputs(run_id) / "traces" / trace
    if not path.is_file():
        raise HTTPException(404, "no such trace")
    return executor.adapter(bench.data["adapter"]).events(path)


@app.get("/api/runs/{run_id}/log", response_class=PlainTextResponse)
def log(run_id: str, tail: int = 300):
    p = _run_dir(run_id) / "log.txt"
    if not p.is_file():
        return ""
    return "\n".join(p.read_text(errors="replace").splitlines()[-min(tail, 5000):])


@app.get("/api/runs/{run_id}/manifest")
def manifest(run_id: str):
    p = _run_dir(run_id) / "manifest.json"
    if not p.is_file():
        raise HTTPException(404, "no manifest yet")
    return json.loads(p.read_text())


@app.post("/api/runs", status_code=201)
def submit(req: RunRequest, request: Request):
    cat = registry.catalogue()
    if req.mas not in cat["mas"] or req.bench not in cat["benchmarks"]:
        raise HTTPException(400, "unknown MAS or benchmark")
    try:
        registry.resolve(cat["mas"][req.mas], cat["benchmarks"][req.bench], dict(req.overrides))
    except registry.SpecError as e:
        raise HTTPException(400, str(e))
    user = request.headers.get("x-stand-user") or "anonymous"
    run_id = db.submit(con(), req.mas, req.bench, dict(req.overrides), req.ref, req.note[:500], user)
    return {"id": run_id}


@app.post("/api/runs/{run_id}/cancel")
def cancel(run_id: str):
    c = con()
    n = c.execute("UPDATE runs SET status='cancelled', finished=COALESCE(finished, strftime('%s','now'))"
                  " WHERE id=? AND status IN ('queued','running')", (run_id,)).rowcount
    if not n:
        raise HTTPException(409, "run is not queued or running")
    return {"id": run_id, "status": "cancelled"}
