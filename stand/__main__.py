"""Stand command line.

``python -m stand validate`` checks every MAS and benchmark declaration.
``python -m stand submit <bench> --mas <id> [--set k=v ...] [--ref R] [--component name=source ...]
[--config owner/repo@ref] [--note N] [--wait]`` queues a run. ``--config`` takes the MAS and benchmark
declarations from that repository's ``stand/`` directory at that ref.
``python -m stand work`` runs the queue worker; ``python -m stand serve`` runs the API and UI.
``python -m stand run <bench> --mas <id> ...`` queues and executes a run in this process.
``python -m stand import <dir> <bench> --mas <id> [--set k=v ...] --note N`` stores an existing output
directory (for example an earlier local grid) as a finished run, marked as imported in its manifest.
"""
from __future__ import annotations

import argparse
import getpass
import json
import logging
import sys
import time

from . import db, executor, registry


def _overrides(pairs):
    return dict(kv.split("=", 1) for kv in pairs)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="stand")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("validate")
    for name in ("submit", "run"):
        p = sub.add_parser(name)
        p.add_argument("bench")
        p.add_argument("--mas", required=True)
        p.add_argument("--set", action="append", default=[], metavar="KEY=VALUE")
        p.add_argument("--ref", default=None)
        p.add_argument("--note", default="")
        p.add_argument("--component", action="append", default=[], metavar="NAME=SOURCE")
        p.add_argument("--config", default=None, metavar="OWNER/REPO@REF")
        if name == "submit":
            p.add_argument("--wait", action="store_true")
    imp = sub.add_parser("import")
    imp.add_argument("dir")
    imp.add_argument("bench")
    imp.add_argument("--mas", required=True)
    imp.add_argument("--set", action="append", default=[], metavar="KEY=VALUE")
    imp.add_argument("--note", required=True)
    sub.add_parser("work")
    s = sub.add_parser("serve")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8765)
    args = ap.parse_args(argv)
    if args.cmd == "validate":
        cat = registry.catalogue()
        for kind, items in cat.items():
            for i in items:
                print(f"ok {kind}/{i}")
        return 0
    if args.cmd in ("submit", "run"):
        config_repo = config_ref = None
        if args.config:
            config_repo, _, config_ref = args.config.partition("@")
            cat = registry.load_config(config_repo, config_ref or "main")[0]
        else:
            cat = registry.catalogue()
        if args.mas not in cat["mas"] or args.bench not in cat["benchmarks"]:
            raise SystemExit(f"unknown MAS or benchmark; MAS: {sorted(cat['mas'])}, benchmarks: {sorted(cat['benchmarks'])}")
        overrides, components = _overrides(args.set), _overrides(args.component)
        registry.resolve(cat["mas"][args.mas], cat["benchmarks"][args.bench], overrides)
        registry.check_components(cat["mas"][args.mas], components)
        con = db.connect()
        run_id = db.submit(con, args.mas, args.bench, overrides, args.ref, args.note, getpass.getuser(),
                           config_repo, config_ref, components)
        print(run_id, flush=True)
        if args.cmd == "run":
            con.execute("UPDATE runs SET status='running', started=? WHERE id=?", (time.time(), run_id))
            row = con.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            return executor.execute(con, row)
        if args.wait:
            while (r := db.get(con, run_id))["status"] in ("queued", "running"):
                time.sleep(10)
            print(json.dumps({k: r[k] for k in ("id", "status", "exit_code", "done", "total", "error")}))
            return 0 if r["status"] == "done" else 1
        return 0
    if args.cmd == "import":
        from .importer import import_dir
        print(import_dir(args.dir, args.bench, args.mas, _overrides(args.set), args.note, getpass.getuser()))
        return 0
    if args.cmd == "work":
        from .worker import loop
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
        loop()
        return 0
    if args.cmd == "serve":
        import uvicorn
        uvicorn.run("stand.api:app", host=args.host, port=args.port, log_level="info")
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
