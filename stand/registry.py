"""Declared multi-agent systems and benchmarks.

A MAS (``mas/<id>.yaml``) is an implementation at an exact commit: repository, digest-pinned
image, setup command, files overlaid on the checkout, its agents and tools, MAS-level parameters
and the secrets it may read. A benchmark (``benchmarks/<id>.yaml``) is a task set with a run
command, the output directory it produces, the trace adapter that reads that directory and its
own parameters. A benchmark runs on any MAS of the same ``family``: the family is the contract
between them (command-line flags, output layout). Swapping the MAS means another MAS file of the
same family: a fork, another commit, another provider.

A MAS may declare ``components``: named groups of paths in its repository (an agent, its
prompts, the tools). A run may replace any component with a source (see ``parse_source``)
without touching the others; the executor overlays those paths on the base checkout.

The declarations themselves come either from this repository or from a configuration repository
at a commit (``load_config``), conventionally its ``stand/`` directory. The second form lets a
pull request change a component, a benchmark or an analyzer and be tested with that change.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import shlex
from dataclasses import dataclass
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
SHA = re.compile(r"^[0-9a-f]{40}$")
DIGEST = re.compile(r"@sha256:[0-9a-f]{64}$")
MAS_KEYS = {"id", "family", "description", "repo", "ref", "image", "setup", "overlays", "agents",
            "edges", "tools", "params", "choices", "secrets", "components"}
MAS_REQUIRED = {"id", "family", "repo", "ref", "image", "agents"}
BENCH_KEYS = {"id", "family", "description", "run", "outputs", "adapter", "params", "choices",
              "timeout_min", "analyzers"}
ANALYZER_KEYS = {"run", "image", "setup", "description"}
BENCH_REQUIRED = {"id", "family", "run", "outputs", "adapter"}
ADAPTERS = {"t11"}


class SpecError(ValueError):
    pass


@dataclass(frozen=True)
class Spec:
    kind: str
    data: dict
    sha256: str
    root: Path = ROOT

    @property
    def id(self) -> str:
        return self.data["id"]


def _load(kind: str, path: Path, root: Path) -> Spec:
    text = path.read_text()
    data = yaml.safe_load(text)
    if not isinstance(data, dict):
        raise SpecError(f"{path.name}: not a mapping")
    keys, required = (MAS_KEYS, MAS_REQUIRED) if kind == "mas" else (BENCH_KEYS, BENCH_REQUIRED)
    missing, unknown = required - set(data), set(data) - keys
    if missing or unknown:
        raise SpecError(f"{path.name}: missing {sorted(missing)}, unknown {sorted(unknown)}")
    if data["id"] != path.stem:
        raise SpecError(f"{path.name}: id must equal the file name")
    if kind == "mas":
        if not SHA.match(str(data["ref"])):
            raise SpecError(f"{path.name}: ref must be a full 40-character commit SHA")
        if not DIGEST.search(str(data["image"])):
            raise SpecError(f"{path.name}: image must be pinned by @sha256 digest")
        for o in data.get("overlays") or []:
            if not (root / o["from"]).is_file():
                raise SpecError(f"{path.name}: overlay {o['from']} not found")
        for name, comp in (data.get("components") or {}).items():
            paths = comp.get("paths") if isinstance(comp, dict) else None
            if not paths or any(Path(p).is_absolute() or ".." in Path(p).parts for p in paths):
                raise SpecError(f"{path.name}: component {name} needs relative paths")
    else:
        if data["adapter"] not in ADAPTERS:
            raise SpecError(f"{path.name}: unknown adapter {data['adapter']!r}")
        for name, a in (data.get("analyzers") or {}).items():
            if not isinstance(a, dict) or "run" not in a or set(a) - ANALYZER_KEYS:
                raise SpecError(f"{path.name}: analyzer {name} needs run and only {sorted(ANALYZER_KEYS)}")
    return Spec(kind, data, hashlib.sha256(text.encode()).hexdigest(), root)


def catalogue(root: Path = ROOT) -> dict[str, dict[str, Spec]]:
    out = {"mas": {}, "benchmarks": {}}
    for kind, folder in (("mas", "mas"), ("bench", "benchmarks")):
        for p in sorted((root / folder).glob("*.yaml")):
            s = _load(kind, p, root)
            out["mas" if kind == "mas" else "benchmarks"][s.id] = s
    for b in out["benchmarks"].values():
        if not any(m.data["family"] == b.data["family"] for m in out["mas"].values()):
            raise SpecError(f"benchmark {b.id}: no MAS of family {b.data['family']!r}")
        resolve(next(m for m in out["mas"].values() if m.data["family"] == b.data["family"]), b, {})
    return out


def resolve(mas: Spec, bench: Spec, overrides: dict) -> dict:
    """Merge parameters and render the command; reject unknown keys and out-of-choice values."""
    if mas.data["family"] != bench.data["family"]:
        raise SpecError(f"benchmark {bench.id} needs family {bench.data['family']!r}, MAS {mas.id} is {mas.data['family']!r}")
    params = {**(mas.data.get("params") or {}), **(bench.data.get("params") or {})}
    choices = {**(mas.data.get("choices") or {}), **(bench.data.get("choices") or {})}
    unknown = set(overrides) - set(params)
    if unknown:
        raise SpecError(f"unknown parameters {sorted(unknown)}; allowed: {sorted(params)}")
    for k, v in overrides.items():
        if isinstance(params[k], int) and not isinstance(params[k], bool):
            try:
                v = int(v)
            except (TypeError, ValueError):
                raise SpecError(f"parameter {k} must be an integer")
        params[k] = v
    for k, allowed in choices.items():
        if str(params.get(k)) not in map(str, allowed):
            raise SpecError(f"parameter {k}={params.get(k)!r} not in {allowed}")
    safe = {k: shlex.quote(str(v)) for k, v in params.items()}
    try:
        command = " ".join(bench.data["run"].split()).format(**safe)
        outputs = bench.data["outputs"].format(**{k: str(v) for k, v in params.items()})
    except KeyError as e:
        raise SpecError(f"benchmark {bench.id}: placeholder {e} has no parameter")
    if ".." in Path(outputs).parts or Path(outputs).is_absolute():
        raise SpecError("outputs must stay inside the checkout")
    return {"params": params, "command": command, "outputs": outputs}


def parse_source(source: str, base_repo: str) -> dict:
    """A component source: ``<ref>`` (the base repository), ``owner/repo@<ref>`` or ``config:<path>``."""
    source = source.strip()
    if source.startswith("config:"):
        path = source[len("config:"):]
        if not path or Path(path).is_absolute() or ".." in Path(path).parts:
            raise SpecError(f"bad config path in component source {source!r}")
        return {"kind": "config", "path": path}
    if "@" in source:
        repo, ref = source.rsplit("@", 1)
    else:
        repo, ref = base_repo, source
    if not re.fullmatch(r"[\w.-]+/[\w.-]+|https://[\w./-]+", repo) or not re.fullmatch(r"[\w./-]+", ref):
        raise SpecError(f"bad component source {source!r}")
    return {"kind": "git", "repo": repo, "ref": ref}


def check_components(mas: Spec, components: dict) -> dict:
    declared = mas.data.get("components") or {}
    unknown = set(components) - set(declared)
    if unknown:
        raise SpecError(f"unknown components {sorted(unknown)}; declared: {sorted(declared)}")
    return {k: parse_source(v, mas.data["repo"]) for k, v in components.items()}


def _git(args, cwd) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def load_config(repo: str, ref: str, subdir: str = "stand") -> tuple[dict, Path, str]:
    """Fetch a configuration repository at ``ref`` (cached by commit) and load its catalogue.

    Returns the catalogue, the checkout root (for ``config:`` component sources and analyzers)
    and the resolved commit.
    """
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", repo) or not re.fullmatch(r"[\w./-]+", ref):
        raise SpecError(f"bad configuration source {repo}@{ref}")
    cache = Path(os.environ.get("STAND_CONFIG_CACHE", "/srv/bench/configs")) / repo.replace("/", "__")
    cache.mkdir(parents=True, exist_ok=True)
    tmp = cache / f".fetch-{os.getpid()}"
    if tmp.exists():
        shutil.rmtree(tmp)
    tmp.mkdir()
    _git(["init", "-q"], tmp)
    _git(["fetch", "-q", "--depth", "1", f"https://github.com/{repo}.git", ref], tmp)
    _git(["checkout", "-q", "FETCH_HEAD"], tmp)
    sha = _git(["rev-parse", "HEAD"], tmp)
    dest = cache / sha
    if dest.exists():
        shutil.rmtree(tmp)
    else:
        tmp.rename(dest)
    root = dest / subdir
    if not root.is_dir():
        raise SpecError(f"{repo}@{sha[:10]} has no {subdir}/ directory")
    return catalogue(root), dest, sha
