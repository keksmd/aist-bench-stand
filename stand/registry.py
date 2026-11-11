"""Declared multi-agent systems and benchmarks.

A MAS (``mas/<id>.yaml``) is an implementation at an exact commit: repository, digest-pinned
image, setup command, files overlaid on the checkout, its agents and tools, MAS-level parameters
and the secrets it may read. A benchmark (``benchmarks/<id>.yaml``) is a task set with a run
command, the output directory it produces, the trace adapter that reads that directory and its
own parameters. A benchmark runs on any MAS of the same ``family``: the family is the contract
between them (command-line flags, output layout). Swapping the MAS means another MAS file of the
same family: a fork, another commit, another provider.
"""
from __future__ import annotations

import hashlib
import re
import shlex
from dataclasses import dataclass
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
SHA = re.compile(r"^[0-9a-f]{40}$")
DIGEST = re.compile(r"@sha256:[0-9a-f]{64}$")
MAS_KEYS = {"id", "family", "description", "repo", "ref", "image", "setup", "overlays", "agents",
            "edges", "tools", "params", "choices", "secrets"}
MAS_REQUIRED = {"id", "family", "repo", "ref", "image", "agents"}
BENCH_KEYS = {"id", "family", "description", "run", "outputs", "adapter", "params", "choices",
              "timeout_min"}
BENCH_REQUIRED = {"id", "family", "run", "outputs", "adapter"}
ADAPTERS = {"t11"}


class SpecError(ValueError):
    pass


@dataclass(frozen=True)
class Spec:
    kind: str
    data: dict
    sha256: str

    @property
    def id(self) -> str:
        return self.data["id"]


def _load(kind: str, path: Path) -> Spec:
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
            if not (ROOT / o["from"]).is_file():
                raise SpecError(f"{path.name}: overlay {o['from']} not found")
    elif data["adapter"] not in ADAPTERS:
        raise SpecError(f"{path.name}: unknown adapter {data['adapter']!r}")
    return Spec(kind, data, hashlib.sha256(text.encode()).hexdigest())


def catalogue(root: Path = ROOT) -> dict[str, dict[str, Spec]]:
    out = {"mas": {}, "benchmarks": {}}
    for kind, folder in (("mas", "mas"), ("bench", "benchmarks")):
        for p in sorted((root / folder).glob("*.yaml")):
            s = _load(kind, p)
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
