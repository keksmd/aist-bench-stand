"""Read T11 run output into the stand's common records.

Common task record: ``task_id``, ``condition``, ``repeat``, ``success`` (bool or None while
running), ``reason``, ``llm_calls``, ``total_tokens``, ``duration_s``, ``tool_calls``,
``tool_errors``, ``trace`` (file name under ``traces/``).

Common trace event: ``seq``, ``agent``, ``kind`` (``start``, ``llm``, ``end``), ``ts``, ``model``,
``tokens``, ``latency_s``, ``text`` (the request, the model's answer or the final answer) and
``tool_calls`` (name, arguments, result) for ``llm`` events.
"""
from __future__ import annotations

import json
from pathlib import Path

ALL_CONDITIONS = 10


def expected_runs(params: dict) -> int:
    conds = ALL_CONDITIONS if params["conditions"] == "all" else len(str(params["conditions"]).split(","))
    return int(params["n_tasks"]) * conds * int(params["repeats"])


def progress(out: Path) -> dict:
    traces = out / "traces"
    if not traces.is_dir():
        return {"complete": 0, "partial": 0}
    return {"complete": sum(1 for _ in traces.glob("*.jsonl")), "partial": sum(1 for _ in traces.glob("*.part"))}


def _lines(path: Path):
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.strip():
            yield json.loads(line)


def tasks(out: Path) -> list[dict]:
    rows = []
    for f in sorted((out / "traces").glob("*.jsonl")):
        start = end = None
        for e in _lines(f):
            if e.get("type") == "run_start":
                start = e
            elif e.get("type") == "run_end":
                end = e
        if not start:
            continue
        cond = start["condition"]
        stats = (end or {}).get("stats") or {}
        check = (end or {}).get("check") or {}
        rows.append({
            "task_id": start["task_id"], "condition": f"{cond['api_version']}:{cond['awareness']}",
            "repeat": start.get("repeat_idx", 0), "success": check.get("success") if end else None,
            "reason": check.get("reason"), "llm_calls": stats.get("llm_calls"),
            "total_tokens": stats.get("total_tokens"), "duration_s": (end or {}).get("duration_s"),
            "tool_calls": stats.get("tool_calls"), "tool_errors": stats.get("tool_errors"), "trace": f.name,
        })
    return rows


def _text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(c.get("text", "") for c in content if isinstance(c, dict))
    return "" if content is None else json.dumps(content, ensure_ascii=False)


def events(trace: Path) -> list[dict]:
    out = []
    for e in _lines(trace):
        t = e.get("type")
        if t == "run_start":
            out.append({"agent": "user", "kind": "start", "ts": e.get("started_at"), "model": e.get("model"),
                        "text": e.get("request", ""), "condition": e.get("condition")})
        elif t == "step":
            resp = e.get("response") or {}
            results = e.get("tool_results") or []
            calls = []
            for i, c in enumerate(e.get("tool_calls") or []):
                res = results[i] if i < len(results) else None
                calls.append({"name": c.get("name"), "arguments": c.get("arguments"),
                              "result": res.get("result", res.get("content")) if isinstance(res, dict) else res})
            usage = e.get("usage") or {}
            out.append({"agent": e.get("agent"), "kind": "llm", "ts": e.get("ts"), "model": e.get("model"),
                        "tokens": usage.get("total_tokens") or usage.get("provider_total_tokens") or (usage.get("prompt_tokens", 0) + usage.get("completion_tokens", 0)) or None, "latency_s": e.get("latency_s"),
                        "cached": e.get("cached"), "text": _text(resp.get("content")), "tool_calls": calls})
        elif t == "run_end":
            out.append({"agent": "check", "kind": "end", "ts": e.get("ended_at"), "text": e.get("final_text", ""),
                        "success": (e.get("check") or {}).get("success"), "reason": (e.get("check") or {}).get("reason")})
    for i, ev in enumerate(out):
        ev["seq"] = i
    return out
