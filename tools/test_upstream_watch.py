import json
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import upstream_watch


BASELINE = {
    "checked_at": "2026-11-17T13:00:00+00:00",
    "components": {
        "claude_code": {
            "kind": "npm_version",
            "package": "@anthropic-ai/claude-code",
            "version": "2.1.326",
            "compare": "major.minor",
        },
        "t3_code": {
            "kind": "github_release",
            "repo": "pingdotgg/t3code",
            "version": "v0.1.10",
            "compare": "exact",
        },
    },
}


def fake_fetcher(versions):
    """Map a URL fragment to a payload, like the real endpoints."""

    def _fetch(url, timeout=15):
        if "npmjs" in url:
            return {"version": versions["claude_code"]}
        if "github" in url:
            return {"tag_name": versions["t3_code"]}
        raise urllib.error.URLError(f"unexpected url {url}")

    return _fetch


class VersionCompareTests(unittest.TestCase):
    def test_major_minor_parsing(self):
        self.assertEqual(upstream_watch.major_minor("2.1.326"), "2.1")
        self.assertEqual(upstream_watch.major_minor("v0.1.10"), "0.1")
        self.assertEqual(upstream_watch.major_minor(""), "")
        self.assertEqual(upstream_watch.major_minor("weird"), "weird")


class FetchTests(unittest.TestCase):
    def test_each_component_declares_its_own_kind(self):
        self.assertEqual(
            upstream_watch.fetch_component(BASELINE["components"]["claude_code"], fake_fetcher(
                {"claude_code": "2.1.400", "t3_code": "v0.1.10"})),
            "2.1.400",
        )
        self.assertEqual(
            upstream_watch.fetch_component(BASELINE["components"]["t3_code"], fake_fetcher(
                {"claude_code": "2.1.400", "t3_code": "v0.1.11"})),
            "v0.1.11",
        )

    def test_unknown_kind_is_an_error_not_a_silent_skip(self):
        with self.assertRaises(ValueError):
            upstream_watch.fetch_component({"kind": "carrier_pigeon"}, fake_fetcher({}))


class DriftTests(unittest.TestCase):
    def test_patch_bump_is_not_drift(self):
        drift = upstream_watch.build_drift(BASELINE, {"claude_code": "2.1.399", "t3_code": "v0.1.10"})
        self.assertEqual(drift, [])

    def test_minor_bump_is_drift(self):
        drift = upstream_watch.build_drift(BASELINE, {"claude_code": "2.2.0", "t3_code": "v0.1.10"})
        self.assertEqual(len(drift), 1)
        self.assertEqual(drift[0]["component"], "claude_code")

    def test_t3_any_change_is_drift(self):
        drift = upstream_watch.build_drift(BASELINE, {"claude_code": "2.1.326", "t3_code": "v0.1.11"})
        self.assertEqual(len(drift), 1)
        self.assertEqual(drift[0]["component"], "t3_code")

    def test_no_change_is_no_drift(self):
        drift = upstream_watch.build_drift(
            BASELINE, {"claude_code": "2.1.326", "t3_code": "v0.1.10"}
        )
        self.assertEqual(drift, [])

    def test_an_unchecked_baseline_component_is_an_error(self):
        # a component nobody checked must never read as "no drift"
        with self.assertRaises(ValueError):
            upstream_watch.build_drift(BASELINE, {"claude_code": "2.1.326"})

    def test_check_collects_current_versions(self):
        result = upstream_watch.check(
            BASELINE, fake_fetcher({"claude_code": "2.1.400", "t3_code": "v0.1.10"})
        )
        self.assertEqual(result["current"]["claude_code"], "2.1.400")
        self.assertEqual(result["current"]["t3_code"], "v0.1.10")
        self.assertEqual(result["drift"], [])


class MainContractTests(unittest.TestCase):
    """The contract ci.yml branches on: exit code + report file."""

    def _baseline_file(self, tmp: str) -> Path:
        path = Path(tmp) / "baseline.json"
        upstream_watch.save_baseline(path, BASELINE)
        return path

    def _run(self, argv, fetcher=None):
        with mock.patch.object(sys, "argv", argv), \
             mock.patch.object(upstream_watch, "fetch_json", fetcher or fake_fetcher(
                 {"claude_code": "2.1.326", "t3_code": "v0.1.10"})):
            return upstream_watch.main()

    def test_no_drift_exits_zero_and_writes_no_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            baseline = self._baseline_file(tmp)
            report = Path(tmp) / "drift.json"
            rc = self._run(["upstream_watch.py", "--check", "--baseline", str(baseline),
                            "--report", str(report)])
            self.assertEqual(rc, 0)
            self.assertFalse(report.exists())

    def test_drift_exits_zero_and_writes_the_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            baseline = self._baseline_file(tmp)
            report = Path(tmp) / "drift.json"
            fetcher = fake_fetcher({"claude_code": "2.2.0", "t3_code": "v0.1.10"})
            rc = self._run(["upstream_watch.py", "--check", "--baseline", str(baseline),
                            "--report", str(report)], fetcher)
            self.assertEqual(rc, 0)
            self.assertTrue(report.exists())
            data = json.loads(report.read_text(encoding="utf-8"))
            self.assertEqual(data["drift"][0]["component"], "claude_code")

    def test_drift_with_strict_exits_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            baseline = self._baseline_file(tmp)
            fetcher = fake_fetcher({"claude_code": "2.2.0", "t3_code": "v0.1.10"})
            rc = self._run(["upstream_watch.py", "--check", "--strict", "--baseline", str(baseline)],
                            fetcher)
            self.assertEqual(rc, 1)

    def test_cannot_check_exits_two_and_writes_no_report(self):
        def boom(url, timeout=15):
            raise urllib.error.URLError("no network")

        with tempfile.TemporaryDirectory() as tmp:
            baseline = self._baseline_file(tmp)
            report = Path(tmp) / "drift.json"
            rc = self._run(["upstream_watch.py", "--check", "--baseline", str(baseline),
                            "--report", str(report)], boom)
            self.assertEqual(rc, 2)
            self.assertFalse(report.exists())

    def test_unknown_component_kind_exits_two(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "baseline.json"
            bad = json.loads(json.dumps(BASELINE))
            bad["components"]["claude_code"]["kind"] = "carrier_pigeon"
            upstream_watch.save_baseline(path, bad)
            rc = self._run(["upstream_watch.py", "--check", "--baseline", str(path)])
            self.assertEqual(rc, 2)

    def test_update_baseline_rewrites_the_versions(self):
        with tempfile.TemporaryDirectory() as tmp:
            baseline = self._baseline_file(tmp)
            fetcher = fake_fetcher({"claude_code": "2.2.0", "t3_code": "v0.1.11"})
            rc = self._run(["upstream_watch.py", "--update-baseline", "--baseline", str(baseline)],
                            fetcher)
            self.assertEqual(rc, 0)
            data = json.loads(baseline.read_text(encoding="utf-8"))
            self.assertEqual(data["components"]["claude_code"]["version"], "2.2.0")
            self.assertEqual(data["components"]["t3_code"]["version"], "v0.1.11")

    def test_issue_body_mentions_the_drift(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp) / "drift.json"
            report.write_text(json.dumps({
                "checked_at": "now",
                "current": {"claude_code": "2.2.0"},
                "drift": [{"component": "claude_code", "baseline": "2.1.326", "current": "2.2.0",
                            "compare": "major.minor", "why_it_matters": "registry format"}],
            }), encoding="utf-8")
            rc = self._run(["upstream_watch.py", "--issue-body", str(report)])
            self.assertEqual(rc, 0)


class ShippedBaselineTests(unittest.TestCase):
    def test_shipped_baseline_is_valid_and_declares_kinds(self):
        data = upstream_watch.load_baseline()
        for name, spec in data["components"].items():
            self.assertIn("kind", spec, name)
            self.assertIn(spec["kind"], ("npm_version", "github_release"), name)
            self.assertTrue(spec.get("version"), name)

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
