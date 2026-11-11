"""Run one declared benchmark in a container and record a manifest.

A benchmark is a YAML file under ``benchmarks/``. It names the target repository and an exact
commit, the container image, the setup and run commands, default parameters, the secrets it may
read and the output directory to collect. Parameters are substituted into the run command with
``str.format``; ``--set key=value`` overrides a default. Unknown keys are rejected.

Each run gets an ID ``<bench>-<UTC timestamp>-<target sha[:8]>`` and a directory under the
results root with ``outputs/``, ``log.txt`` and ``manifest.json`` (benchmark definition and its
sha256, target commit, parameters, stand commit, image digest, wall time, exit code, sha256 per
output file, secret scan). Secrets are passed through an env file that exists only while the
container runs; their values are never written to the results.

Usage::

    bench.py validate
    bench.py run <name> [--set key=value ...] [--ref <commit>] [--results <dir>] [--dry-run]
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
BENCH_DIR = ROOT / "benchmarks"
REQUIRED = {"name", "repo", "ref", "image", "run", "outputs"}
OPTIONAL = {"description", "setup", "params", "secrets", "overlays", "timeout_min"}
SHA = re.compile(r"^[0-9a-f]{40}$")
SECRET_PATTERNS = re.compile(
    r"(sk-[A-Za-z0-9_-]{20,}|AIza[0-9A-Za-z_-]{30,}|gh[pousr]_[A-Za-z0-9]{30,}"
    r"|Bearer\s+[A-Za-z0-9._-]{20,}|[0-9a-f]{32}\.[A-Za-z0-9]{16})")


def load(name: str) -> tuple[dict, str]:
    path = BENCH_DIR / f"{name}.yaml"
    if not path.is_file():
        raise SystemExit(f"unknown benchmark {name!r}; known: {', '.join(sorted(p.stem for p in BENCH_DIR.glob('*.yaml')))}")
    text = path.read_text()
    spec = yaml.safe_load(text)
    validate(spec, path.name)
    return spec, hashlib.sha256(text.encode()).hexdigest()


def validate(spec: dict, where: str) -> None:
    if not isinstance(spec, dict):
        raise SystemExit(f"{where}: not a mapping")
    missing, unknown = REQUIRED - set(spec), set(spec) - REQUIRED - OPTIONAL
    if missing or unknown:
        raise SystemExit(f"{where}: missing {sorted(missing)}, unknown {sorted(unknown)}")
    if not SHA.match(str(spec["ref"])):
        raise SystemExit(f"{where}: ref must be a full 40-character commit SHA")
    if spec["name"] != Path(where).stem:
        raise SystemExit(f"{where}: name must equal the file name")
    for o in spec.get("overlays") or []:
        if not (ROOT / o["from"]).is_file():
            raise SystemExit(f"{where}: overlay source {o['from']} not found")
    try:
        render(spec, {})
    except KeyError as e:
        raise SystemExit(f"{where}: run command uses {e} without a default parameter")


def render(spec: dict, overrides: dict) -> tuple[str, dict]:
    params = dict(spec.get("params") or {})
    unknown = set(overrides) - set(params)
    if unknown:
        raise SystemExit(f"unknown parameters {sorted(unknown)}; allowed: {sorted(params)}")
    params.update(overrides)
    safe = {k: shlex.quote(str(v)) for k, v in params.items()}
    return spec["run"].format(**safe), params


def sh(cmd: list[str], **kw) -> str:
    return subprocess.run(cmd, check=True, capture_output=True, text=True, **kw).stdout.strip()


def checkout(repo: str, ref: str, dest: Path) -> str:
    url = repo if "://" in repo else f"https://github.com/{repo}.git"
    dest.mkdir(parents=True)
    sh(["git", "init", "-q"], cwd=dest)
    sh(["git", "fetch", "-q", "--depth", "1", url, ref], cwd=dest)
    sh(["git", "checkout", "-q", "FETCH_HEAD"], cwd=dest)
    return sh(["git", "rev-parse", "HEAD"], cwd=dest)


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


def stand_commit() -> str:
    try:
        return sh(["git", "rev-parse", "HEAD"], cwd=ROOT)
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "unknown"


def run(args) -> int:
    spec, spec_sha = load(args.name)
    overrides = dict(kv.split("=", 1) for kv in args.set)
    command, params = render(spec, overrides)
    ref = args.ref or spec["ref"]
    secrets = {k: os.environ[k] for k in spec.get("secrets") or [] if os.environ.get(k)}
    if args.dry_run:
        print(json.dumps({"bench": spec["name"], "ref": ref, "image": spec["image"], "command": command,
                          "secrets_present": sorted(secrets)}, indent=1))
        return 0
    started = time.time()
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    work = Path(tempfile.mkdtemp(prefix="bench-"))
    try:
        target_sha = checkout(spec["repo"], ref, work / "src")
        run_id = f"{spec['name']}-{stamp}-{target_sha[:8]}"
        out_dir = Path(args.results) / run_id
        out_dir.mkdir(parents=True)
        for o in spec.get("overlays") or []:
            dst = work / "src" / o["to"]
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / o["from"], dst)
        env_file = work / "env"
        env_file.touch(mode=0o600)
        env_file.write_text("".join(f"{k}={v}\n" for k, v in secrets.items()))
        script = " && ".join(c for c in (spec.get("setup"), command) if c)
        docker = ["docker", "run", "--rm", "--env-file", str(env_file), "-v", f"{work / 'src'}:/work",
                  "-v", "bench-pip-cache:/root/.cache/pip", "-w", "/work", spec["image"], "sh", "-c", script]
        timeout = int(spec.get("timeout_min", 360)) * 60
        with open(out_dir / "log.txt", "w") as log:
            try:
                code = subprocess.run(docker, stdout=log, stderr=subprocess.STDOUT, timeout=timeout).returncode
            except subprocess.TimeoutExpired:
                code = 124
        env_file.unlink()
        produced = work / "src" / spec["outputs"].format(**params)
        if produced.exists():
            shutil.copytree(produced, out_dir / "outputs") if produced.is_dir() else shutil.copy(produced, out_dir / "outputs")
        image_digest = sh(["docker", "image", "inspect", "--format", "{{index .RepoDigests 0}}", spec["image"]])
        hits = secret_hits(out_dir, list(secrets.values()))
        manifest = {
            "run_id": run_id, "bench": spec["name"], "bench_sha256": spec_sha, "stand_commit": stand_commit(),
            "target": {"repo": spec["repo"], "commit": target_sha, "declared_ref": spec["ref"]},
            "image": image_digest, "command": command, "params": params, "secrets_used": sorted(secrets),
            "exit_code": code, "wall_time_s": round(time.time() - started, 1),
            "started_utc": stamp, "secret_scan_hits": hits,
            "files": file_hashes(out_dir / "outputs") if (out_dir / "outputs").exists() else {},
        }
        (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=1, sort_keys=True) + "\n")
        print(json.dumps({"run_id": run_id, "exit_code": code, "dir": str(out_dir), "secret_scan_hits": hits}))
        if hits:
            print("secret scan found matches; do not publish this run", file=sys.stderr)
            return 3
        return code
    finally:
        shutil.rmtree(work, ignore_errors=True)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("validate")
    r = sub.add_parser("run")
    r.add_argument("name")
    r.add_argument("--set", action="append", default=[], metavar="KEY=VALUE")
    r.add_argument("--ref", default=None, help="override the declared target commit")
    r.add_argument("--results", default=os.environ.get("BENCH_RESULTS", "/srv/bench/results"))
    r.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    if args.cmd == "validate":
        for p in sorted(BENCH_DIR.glob("*.yaml")):
            validate(yaml.safe_load(p.read_text()), p.name)
            print(f"ok {p.name}")
        return 0
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
