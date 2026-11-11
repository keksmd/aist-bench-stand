"""Execute one queued run: check out the MAS, run the benchmark in a container, collect results.

Layout of ``<results>/<run_id>/``: ``outputs/`` (the benchmark's output directory, traces
included), ``log.txt`` (container output) and ``manifest.json`` (MAS and benchmark definitions
with their sha256, MAS commit, image digest, parameters, command, exit code, wall time, sha256
per output file, secret scan). Secrets reach the container through an env file that exists only
while the container runs. The container is named ``bench-<run_id>`` so a run can be cancelled.
"""
from __future__ import annotations

import hashlib
import importlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from . import db, registry

SECRET_PATTERNS = re.compile(
    r"(sk-[A-Za-z0-9_-]{20,}|AIza[0-9A-Za-z_-]{30,}|gh[pousr]_[A-Za-z0-9]{30,}"
    r"|Bearer\s+[A-Za-z0-9._-]{20,}|[0-9a-f]{32}\.[A-Za-z0-9]{16})")


def results_root() -> Path:
    return Path(os.environ.get("BENCH_RESULTS", "/srv/bench/results"))


def adapter(name: str):
    return importlib.import_module(f"stand.adapters.{name}")


def _sh(cmd, **kw) -> str:
    return subprocess.run(cmd, check=True, capture_output=True, text=True, **kw).stdout.strip()


def checkout(repo: str, ref: str, dest: Path) -> str:
    url = repo if "://" in repo else f"https://github.com/{repo}.git"
    dest.mkdir(parents=True)
    _sh(["git", "init", "-q"], cwd=dest)
    _sh(["git", "fetch", "-q", "--depth", "1", url, ref], cwd=dest)
    _sh(["git", "checkout", "-q", "FETCH_HEAD"], cwd=dest)
    return _sh(["git", "rev-parse", "HEAD"], cwd=dest)


def file_hashes(root: Path) -> dict[str, str]:
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file()}


def secret_hits(root: Path, values: list[str]) -> int:
    hits = 0
    for p in root.rglob("*"):
        if p.is_file() and p.stat().st_size < 50_000_000:
            text = p.read_bytes().decode("utf-8", "replace")
            hits += len(SECRET_PATTERNS.findall(text)) + sum(text.count(v) for v in values if len(v) >= 8)
    return hits


def read_env_file(path: str | None) -> dict[str, str]:
    """KEY=VALUE lines, read at every run so updated keys apply without a restart."""
    if not path or not Path(path).is_file():
        return {}
    out = {}
    for line in Path(path).read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def stand_commit() -> str:
    try:
        return _sh(["git", "rev-parse", "HEAD"], cwd=registry.ROOT)
    except (subprocess.CalledProcessError, FileNotFoundError):
        return os.environ.get("STAND_COMMIT", "unknown")


def catalogue_for(row) -> tuple[dict, Path | None, str | None]:
    """The catalogue a run was submitted against: a configuration repository at a commit, or this one."""
    keys = row.keys() if hasattr(row, "keys") else row
    if "config_repo" in keys and row["config_repo"]:
        return registry.load_config(row["config_repo"], row["config_ref"] or "main")
    return registry.catalogue(), None, None


def apply_components(mas, components: dict, src: Path, config_root: Path | None, work: Path) -> dict:
    """Overlay each replaced component's paths on the base checkout; return what was used."""
    used = {}
    for name, source in components.items():
        paths = mas.data["components"][name]["paths"]
        if source["kind"] == "config":
            if config_root is None:
                raise registry.SpecError(f"component {name}: config: source needs a configuration repository")
            origin = config_root / source["path"]
            if len(paths) != 1:
                raise registry.SpecError(f"component {name}: config: source replaces one path, declared {paths}")
            pairs = [(origin, src / paths[0])]
            info = {"source": f"config:{source['path']}"}
        else:
            other = work / f"component-{name}"
            commit = checkout(source["repo"], source["ref"], other)
            pairs = [(other / p, src / p) for p in paths]
            info = {"source": f"{source['repo']}@{source['ref']}", "commit": commit}
        for a, b in pairs:
            if not a.exists():
                raise registry.SpecError(f"component {name}: {a.name} missing in its source")
            if b.is_dir():
                shutil.rmtree(b)
            if a.is_dir():
                shutil.copytree(a, b)
            else:
                b.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(a, b)
        info["files"] = {str(f.relative_to(src)): h for b in [q for _, q in pairs]
                         for f, h in ([(b, hashlib.sha256(b.read_bytes()).hexdigest())] if b.is_file()
                                      else [(x, hashlib.sha256(x.read_bytes()).hexdigest()) for x in sorted(b.rglob("*")) if x.is_file()])}
        used[name] = info
    return used


def run_analyzers(bench, mas, plan, run_id: str, src: Path, config_root: Path | None, out_dir: Path) -> dict:
    """Run the benchmark's analyzers on the finished run; each writes report.json in its directory."""
    results = {}
    for name, a in (bench.data.get("analyzers") or {}).items():
        adir = out_dir / "analyzers" / name
        adir.mkdir(parents=True)
        cmd_text = " ".join(a["run"].split()).format(outputs=plan["outputs"], run_id=run_id, report="/report/report.json",
                                                     mas="/mas", config="/config")
        script = " && ".join(" ".join(c.split()) for c in (a.get("setup"), cmd_text) if c and c.strip())
        mounts = ["-v", f"{src}:/mas", "-v", f"{adir}:/report", "-v", "bench-pip-cache:/root/.cache/pip"]
        if config_root is not None:
            mounts += ["-v", f"{config_root}:/config"]
        image = a.get("image") or mas.data["image"]
        started = time.time()
        with open(adir / "log.txt", "w") as log:
            try:
                code = subprocess.run(["docker", "run", "--rm", "--name", f"bench-{run_id}-{name}", *mounts,
                                       "-w", "/config" if config_root is not None else "/mas", image, "sh", "-c", script],
                                      stdout=log, stderr=subprocess.STDOUT, timeout=3600).returncode
            except subprocess.TimeoutExpired:
                subprocess.run(["docker", "rm", "-f", f"bench-{run_id}-{name}"], capture_output=True)
                code = 124
        subprocess.run(["docker", "run", "--rm", "-v", f"{adir}:/report", image, "chown", "-R",
                        f"{os.getuid()}:{os.getgid()}", "/report"], capture_output=True)
        report = adir / "report.json"
        results[name] = {"exit_code": code, "wall_time_s": round(time.time() - started, 1),
                         "report": report.is_file(), "description": a.get("description", "")}
    return results


def execute(con, row, cat=None, poll_s: float = 5.0) -> int:
    run_id = row["id"]
    config_root = config_commit = None
    if cat is None:
        cat, config_root, config_commit = catalogue_for(row)
    mas, bench = cat["mas"][row["mas"]], cat["benchmarks"][row["bench"]]
    plan = registry.resolve(mas, bench, json.loads(row["overrides"]))
    components = registry.check_components(mas, json.loads(row["components"] or "{}") if "components" in row.keys() else {})
    ad = adapter(bench.data["adapter"])
    db.update(con, run_id, total=ad.expected_runs(plan["params"]))
    available = {**os.environ, **read_env_file(os.environ.get("STAND_SECRETS_FILE"))}
    secrets = {k: available[k] for k in mas.data.get("secrets") or [] if available.get(k)}
    out_dir = results_root() / run_id
    out_dir.mkdir(parents=True)
    started = time.time()
    work = Path(tempfile.mkdtemp(prefix="bench-"))
    src = work / "src"
    name = f"bench-{run_id}"
    try:
        commit = checkout(mas.data["repo"], row["ref"] or mas.data["ref"], src)
        for o in mas.data.get("overlays") or []:
            dst = src / o["to"]
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(mas.root / o["from"], dst)
        used_components = apply_components(mas, components, src, config_root, work)
        env_file = work / "env"
        env_file.touch(mode=0o600)
        env_file.write_text("".join(f"{k}={v}\n" for k, v in secrets.items()))
        script = " && ".join(" ".join(c.split()) for c in (mas.data.get("setup"), plan["command"]) if c and c.strip())
        cmd = ["docker", "run", "--rm", "--name", name, "--env-file", str(env_file),
               "-v", f"{src}:/work", "-v", "bench-pip-cache:/root/.cache/pip", "-w", "/work",
               mas.data["image"], "sh", "-c", script]
        deadline = started + int(bench.data.get("timeout_min", 360)) * 60
        produced = src / plan["outputs"]
        (out_dir / "live").symlink_to(produced, target_is_directory=True)
        polls = 0
        with open(out_dir / "log.txt", "w") as log:
            proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT)
            while proc.poll() is None:
                time.sleep(poll_s)
                polls += 1
                db.update(con, run_id, done=ad.progress(produced)["complete"])
                if polls % 6 == 0 and produced.is_dir():
                    try:
                        db.put_tasks(con, run_id, ad.tasks(produced))
                    except (OSError, ValueError):
                        pass
                status = con.execute("SELECT status FROM runs WHERE id=?", (run_id,)).fetchone()["status"]
                if status == "cancelled" or time.time() > deadline:
                    subprocess.run(["docker", "kill", name], capture_output=True)
            code = proc.wait()
        env_file.unlink()
        subprocess.run(["docker", "run", "--rm", "-v", f"{src}:/work", mas.data["image"],
                        "chown", "-R", f"{os.getuid()}:{os.getgid()}", "/work"], capture_output=True)
        (out_dir / "live").unlink(missing_ok=True)
        if produced.is_dir():
            shutil.copytree(produced, out_dir / "outputs")
            rows = ad.tasks(out_dir / "outputs")
            db.put_tasks(con, run_id, rows)
            db.update(con, run_id, done=sum(1 for r in rows if r["success"] is not None))
        status = con.execute("SELECT status FROM runs WHERE id=?", (run_id,)).fetchone()["status"]
        analyzers = None
        if code == 0 and status != "cancelled" and bench.data.get("analyzers"):
            analyzers = run_analyzers(bench, mas, plan, run_id, src, config_root, out_dir)
            db.update(con, run_id, analyzers=analyzers)
        hits = secret_hits(out_dir, list(secrets.values()))
        try:
            digest = _sh(["docker", "image", "inspect", "--format", "{{index .RepoDigests 0}}", mas.data["image"]])
        except subprocess.CalledProcessError:
            digest = None
        manifest = {
            "run_id": run_id, "stand_commit": stand_commit(),
            "config": {"repo": row["config_repo"], "ref": row["config_ref"], "commit": config_commit} if config_commit else None,
            "mas": {"id": mas.id, "sha256": mas.sha256, "repo": mas.data["repo"], "commit": commit,
                    "declared_ref": mas.data["ref"]},
            "components": used_components,
            "benchmark": {"id": bench.id, "sha256": bench.sha256},
            "image": digest, "command": plan["command"], "params": plan["params"],
            "secrets_used": sorted(secrets), "exit_code": code, "wall_time_s": round(time.time() - started, 1),
            "analyzers": analyzers, "secret_scan_hits": hits,
            "files": file_hashes(out_dir / "outputs") if (out_dir / "outputs").exists() else {},
        }
        (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=1, sort_keys=True) + "\n")
        final = "cancelled" if status == "cancelled" else ("done" if code == 0 and not hits else "failed")
        db.update(con, run_id, status=final, exit_code=code, finished=time.time(), manifest=manifest,
                  error="secret scan matched; do not publish" if hits else None)
        return code
    except Exception as e:
        db.update(con, run_id, status="failed", finished=time.time(), error=f"{type(e).__name__}: {e}"[:2000])
        raise
    finally:
        (out_dir / "live").unlink(missing_ok=True)
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)
        shutil.rmtree(work, ignore_errors=True)
