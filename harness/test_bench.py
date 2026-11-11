"""Offline checks of the benchmark declarations and parameter handling."""
import unittest

import bench


class BenchTest(unittest.TestCase):
    def test_declared_benchmarks_are_valid(self):
        spec, sha = bench.load("t11-drift")
        self.assertEqual(len(sha), 64)
        self.assertEqual(spec["name"], "t11-drift")

    def test_override_is_quoted(self):
        spec, _ = bench.load("t11-drift")
        cmd, params = bench.render(spec, {"experiment": "x; rm -rf /"})
        self.assertIn("'x; rm -rf /'", cmd)
        self.assertEqual(params["provider"], "fake")

    def test_unknown_parameter_rejected(self):
        spec, _ = bench.load("t11-drift")
        with self.assertRaises(SystemExit):
            bench.render(spec, {"model": "x"})

    def test_short_ref_rejected(self):
        spec, _ = bench.load("t11-drift")
        with self.assertRaises(SystemExit):
            bench.validate({**spec, "ref": "66bea70"}, "t11-drift.yaml")

    def test_secret_scan_counts_values(self):
        import pathlib
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            pathlib.Path(d, "a.txt").write_text("token=abcdefgh12345678 ok")
            self.assertEqual(bench.secret_hits(pathlib.Path(d), ["abcdefgh12345678"]), 1)


if __name__ == "__main__":
    unittest.main()
