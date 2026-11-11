"""Offline checks: declarations, parameter handling, the T11 adapter, the queue and the API."""
import json
import os
import tempfile
import unittest
from pathlib import Path

from stand import db, registry
from stand.adapters import t11

TRACE = [
    {"type": "run_start", "started_at": "t0", "run_id": "v2_units_A0_T01_r0_x", "model": "m",
     "condition": {"api_version": "v2_units", "awareness": "A0"}, "task_id": "T01", "repeat_idx": 0,
     "request": "find a trip"},
    {"type": "step", "agent": "executor", "ts": "t1", "model": "m", "usage": {"prompt_tokens": 10, "completion_tokens": 5},
     "latency_s": 1.5, "response": {"content": "calling"},
     "tool_calls": [{"name": "search_flights", "arguments": "{\"origin\":\"A\"}"}],
     "tool_results": [{"name": "search_flights", "result": [{"id": "F1", "price": 18591}]}]},
    {"type": "run_end", "ended_at": "t2", "final_text": "{\"no_option\": true}", "duration_s": 3.0,
     "check": {"success": False, "reason": "false_no_option"},
     "stats": {"llm_calls": 1, "total_tokens": 15, "tool_calls": 1, "tool_errors": 0}},
]


class RegistryTest(unittest.TestCase):
    def setUp(self):
        self.cat = registry.catalogue()
        self.mas, self.bench = self.cat["mas"]["t11"], self.cat["benchmarks"]["drift"]

    def test_defaults_render(self):
        plan = registry.resolve(self.mas, self.bench, {})
        self.assertIn("--provider fake", plan["command"])
        self.assertEqual(plan["outputs"], "runs/fake-stand/fake")

    def test_values_are_quoted_and_typed(self):
        plan = registry.resolve(self.mas, self.bench, {"conditions": "v1:A0; rm x", "n_tasks": "5"})
        self.assertIn("'v1:A0; rm x'", plan["command"])
        self.assertEqual(plan["params"]["n_tasks"], 5)

    def test_rejects_unknown_and_out_of_choice(self):
        with self.assertRaises(registry.SpecError):
            registry.resolve(self.mas, self.bench, {"model": "x"})
        with self.assertRaises(registry.SpecError):
            registry.resolve(self.mas, self.bench, {"provider": "paid-model"})
        with self.assertRaises(registry.SpecError):
            registry.resolve(self.mas, self.bench, {"n_tasks": "many"})

    def test_family_mismatch_rejected(self):
        other = registry.Spec("mas", {**self.mas.data, "family": "other"}, "x")
        with self.assertRaises(registry.SpecError):
            registry.resolve(other, self.bench, {})


class AdapterTest(unittest.TestCase):
    def test_tasks_events_progress(self):
        with tempfile.TemporaryDirectory() as d:
            traces = Path(d, "traces")
            traces.mkdir()
            (traces / "v2_units_A0_T01_r0_x.jsonl").write_text("\n".join(json.dumps(e) for e in TRACE))
            (traces / "v1_A0_T02_r0_y.jsonl.part").write_text("")
            self.assertEqual(t11.progress(Path(d)), {"complete": 1, "partial": 1})
            rows = t11.tasks(Path(d))
            self.assertEqual(rows[0]["condition"], "v2_units:A0")
            self.assertIs(rows[0]["success"], False)
            ev = t11.events(traces / "v2_units_A0_T01_r0_x.jsonl")
            self.assertEqual([e["kind"] for e in ev], ["start", "llm", "end"])
            self.assertEqual(ev[1]["tokens"], 15)
            self.assertEqual(ev[1]["tool_calls"][0]["result"][0]["price"], 18591)
        self.assertEqual(t11.expected_runs({"conditions": "all", "n_tasks": 30, "repeats": 1}), 300)
        self.assertEqual(t11.expected_runs({"conditions": "v1:A0,v2_units:A0", "n_tasks": 3, "repeats": 2}), 12)


class QueueApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["STAND_DB"] = str(Path(self.tmp.name, "stand.db"))
        os.environ["BENCH_RESULTS"] = str(Path(self.tmp.name, "results"))

    def tearDown(self):
        self.tmp.cleanup()

    def test_queue_is_fifo(self):
        con = db.connect()
        a = db.submit(con, "t11", "drift", {}, None, "a", "u")
        b = db.submit(con, "t11", "drift", {}, None, "b", "u")
        self.assertEqual(db.claim(con)["id"], a)
        self.assertEqual(db.claim(con)["id"], b)
        self.assertIsNone(db.claim(con))

    def test_api_submit_list_cancel(self):
        from fastapi.testclient import TestClient
        from stand.api import app
        c = TestClient(app)
        self.assertEqual(c.post("/api/runs", json={"mas": "t11", "bench": "drift", "overrides": {"model": "x"}}).status_code, 400)
        r = c.post("/api/runs", json={"mas": "t11", "bench": "drift", "overrides": {"n_tasks": 2}},
                   headers={"X-Stand-User": "alice"})
        self.assertEqual(r.status_code, 201)
        run_id = r.json()["id"]
        runs = c.get("/api/runs").json()
        self.assertEqual(runs[0]["submitted_by"], "alice")
        self.assertEqual(c.post(f"/api/runs/{run_id}/cancel").json()["status"], "cancelled")
        self.assertEqual(c.post(f"/api/runs/{run_id}/cancel").status_code, 409)
        self.assertEqual(c.get("/api/runs/../../etc/trace/x.jsonl").status_code, 404)
        self.assertIn("mas", c.get("/api/catalog").json())


if __name__ == "__main__":
    unittest.main()
