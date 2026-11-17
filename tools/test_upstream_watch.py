import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import upstream_watch


class VersionCompareTests(unittest.TestCase):
    def test_major_minor_parsing(self):
        self.assertEqual(upstream_watch.major_minor("2.1.326"), "2.1")
        self.assertEqual(upstream_watch.major_minor("v0.1.10"), "0.1")
        self.assertEqual(upstream_watch.major_minor(""), "")
        self.assertEqual(upstream_watch.major_minor("weird"), "weird")


class DriftTests(unittest.TestCase):
    baseline = {
        "components": {
            "claude_code": {"version": "2.1.326", "compare": "major.minor"},
            "t3_code": {"version": "v0.1.10", "compare": "exact"},
        }
    }

    def test_patch_bump_is_not_drift(self):
        drift = upstream_watch.build_drift(self.baseline, {"claude_code": "2.1.399"})
        self.assertEqual(drift, [])

    def test_minor_bump_is_drift(self):
        drift = upstream_watch.build_drift(self.baseline, {"claude_code": "2.2.0"})
        self.assertEqual(len(drift), 1)
        self.assertEqual(drift[0]["component"], "claude_code")

    def test_t3_any_change_is_drift(self):
        drift = upstream_watch.build_drift(self.baseline, {"t3_code": "v0.1.11"})
        self.assertEqual(len(drift), 1)
        self.assertEqual(drift[0]["component"], "t3_code")

    def test_no_change_is_no_drift(self):
        drift = upstream_watch.build_drift(
            self.baseline, {"claude_code": "2.1.326", "t3_code": "v0.1.10"}
        )
        self.assertEqual(drift, [])

    def test_check_collects_current_versions(self):
        claude = mock.Mock(return_value={"version": "2.1.400"})
        t3 = mock.Mock(return_value={"tag_name": "v0.1.10"})
        result = upstream_watch.check(self.baseline, claude, t3)
        self.assertEqual(result["current"]["claude_code"], "2.1.400")
        self.assertEqual(result["current"]["t3_code"], "v0.1.10")
        self.assertEqual(result["drift"], [])


class BaselineFileTests(unittest.TestCase):
    def test_shipped_baseline_is_valid(self):
        data = upstream_watch.load_baseline()
        self.assertIn("claude_code", data["components"])
        self.assertIn("t3_code", data["components"])

    def test_update_baseline_rewrites_versions(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "baseline.json"
            upstream_watch.save_baseline(
                path,
                {"checked_at": "x", "components": {"claude_code": {"version": "2.1.326"}}},
            )
            data = upstream_watch.load_baseline(path)
            data["components"]["claude_code"]["version"] = "2.2.0"
            upstream_watch.save_baseline(path, data)
            again = upstream_watch.load_baseline(path)
            self.assertEqual(again["components"]["claude_code"]["version"], "2.2.0")

    def test_report_text_marks_drift(self):
        result = {"checked_at": "now", "current": {"claude_code": "2.2.0"}, "drift": [
            {"component": "claude_code", "baseline": "2.1.326", "current": "2.2.0",
             "compare": "major.minor", "why_it_matters": "registry"}
        ]}
        text = upstream_watch.report_text(result)
        self.assertIn("DRIFT", text)
        self.assertIn("registry", text)


if __name__ == "__main__":
    unittest.main()
