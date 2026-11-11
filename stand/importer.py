"""Store an existing benchmark output directory as a finished run.

The manifest says ``imported: true`` and records the source path and sha256 per file; it has no
container exit code or image digest, because the stand did not execute the run.
"""
from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

from . import db, executor, registry


def import_dir(src: str, bench_id: str, mas_id: str, overrides: dict, note: str, user: str) -> str:
    cat = registry.catalogue()
    mas, bench = cat["mas"][mas_id], cat["benchmarks"][bench_id]
    plan = registry.resolve(mas, bench, overrides)
    ad = executor.adapter(bench.data["adapter"])
    src_path = Path(src).expanduser().resolve()
    rows = ad.tasks(src_path)
    if not rows:
        raise SystemExit(f"no task records under {src_path}")
    con = db.connect()
    run_id = db.submit(con, mas_id, bench_id, overrides, None, note, user)
    out = executor.results_root() / run_id
    shutil.copytree(src_path, out / "outputs")
    manifest = {"run_id": run_id, "imported": True, "source": str(src_path), "stand_commit": executor.stand_commit(),
                "mas": {"id": mas.id, "sha256": mas.sha256, "repo": mas.data["repo"], "commit": mas.data["ref"],
                        "declared_ref": mas.data["ref"]},
                "benchmark": {"id": bench.id, "sha256": bench.sha256}, "image": None, "command": None,
                "params": plan["params"], "exit_code": 0, "wall_time_s": None,
                "secret_scan_hits": executor.secret_hits(out, []), "files": executor.file_hashes(out / "outputs")}
    (out / "manifest.json").write_text(json.dumps(manifest, indent=1, sort_keys=True) + "\n")
    db.put_tasks(con, run_id, rows)
    now = time.time()
    db.update(con, run_id, status="done", started=now, finished=now, exit_code=0, manifest=manifest,
              total=len(rows), done=sum(1 for r in rows if r["success"] is not None))
    return run_id
