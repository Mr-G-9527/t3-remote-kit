import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import peer_health
import peer_mirror


class PeerHealthTests(unittest.TestCase):
    def _patch_dirs(self, tmp: str):
        d = Path(tmp)
        (d / "sessions").mkdir(parents=True, exist_ok=True)
        return mock.patch.object(peer_mirror, "CONFIG_DIRS", [d])

    def test_missing_roles_table_is_an_error(self):
        with mock.patch.object(peer_health, "load_roles", return_value={}):
            result = peer_health.probe()
        self.assertEqual(result["status"], "error")
        self.assertTrue(result["errors"])

    def test_no_live_peer_is_offline_not_error(self):
        with tempfile.TemporaryDirectory() as tmp, self._patch_dirs(tmp), \
             mock.patch.object(peer_health, "load_roles", return_value={"desktop": {"display_name": "desktop"}}), \
             mock.patch.object(peer_health, "find", return_value=[]), \
             mock.patch.object(peer_health, "entries", return_value=[]), \
             mock.patch.object(peer_health, "proc_state", return_value="dead"):
            result = peer_health.probe()
        self.assertEqual(result["status"], "offline")

    def test_live_peer_is_ok(self):
        with tempfile.TemporaryDirectory() as tmp, self._patch_dirs(tmp), \
             mock.patch.object(peer_health, "load_roles", return_value={"desktop": {"display_name": "desktop"}}), \
             mock.patch.object(peer_health, "find", return_value=[{"name": "n", "pid": 1, "entrypoint": "cli", "state": "alive"}]), \
             mock.patch.object(peer_health, "entries", return_value=[]), \
             mock.patch.object(peer_health, "proc_state", return_value="alive"):
            result = peer_health.probe()
        self.assertEqual(result["status"], "ok")

    def test_stale_entries_make_it_degraded(self):
        with tempfile.TemporaryDirectory() as tmp, self._patch_dirs(tmp), \
             mock.patch.object(peer_health, "load_roles", return_value={"desktop": {"display_name": "desktop"}}), \
             mock.patch.object(peer_health, "find", return_value=[]), \
             mock.patch.object(peer_health, "entries", return_value=[(None, None, {"pid": 999, "name": "dead"})]), \
             mock.patch.object(peer_health, "proc_state", return_value="dead"):
            result = peer_health.probe()
        self.assertEqual(result["status"], "degraded")
        self.assertEqual(len(result["stale_entries"]), 1)

    def test_creation_time_mismatch_is_an_error_not_a_stale_entry(self):
        # the registry field semantics changed -> the kit's central guard stopped working
        with tempfile.TemporaryDirectory() as tmp, self._patch_dirs(tmp), \
             mock.patch.object(peer_health, "load_roles", return_value={"desktop": {"display_name": "desktop"}}), \
             mock.patch.object(peer_health, "find", return_value=[]), \
             mock.patch.object(peer_health, "entries", return_value=[(None, None, {"pid": 7, "name": "x"})]), \
             mock.patch.object(peer_health, "proc_state", return_value="mismatch"):
            result = peer_health.probe()
        self.assertEqual(result["status"], "error")
        self.assertEqual(len(result["mismatched_entries"]), 1)
        self.assertEqual(result["stale_entries"], [])

    def test_probe_does_not_write_to_the_peer_registry_by_default(self):
        # the probe is advertised as read-only: it must not copy entries (or tokens) anywhere
        with tempfile.TemporaryDirectory() as tmp:
            a = Path(tmp) / "a"
            b = Path(tmp) / "b"
            (a / "sessions").mkdir(parents=True)
            (b / "sessions").mkdir(parents=True)
            (a / "sessions" / "42.json").write_text('{"pid": 42, "updatedAt": 1}', encoding="utf-8")
            (a / "sessions" / "42.tok.key").write_text("peerToken", encoding="utf-8")
            with mock.patch.object(peer_mirror, "CONFIG_DIRS", [a, b]), \
                 mock.patch.object(peer_health, "load_roles", return_value={"desktop": {"display_name": "desktop"}}), \
                 mock.patch.object(peer_health, "find", return_value=[]), \
                 mock.patch.object(peer_health, "proc_state", return_value="dead"):
                peer_health.probe()
            self.assertEqual(list((b / "sessions").iterdir()), [])

    def test_probe_mirrors_when_asked(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = Path(tmp) / "a"
            b = Path(tmp) / "b"
            (a / "sessions").mkdir(parents=True)
            (b / "sessions").mkdir(parents=True)
            (a / "sessions" / "42.json").write_text('{"pid": 42, "updatedAt": 1}', encoding="utf-8")
            with mock.patch.object(peer_mirror, "CONFIG_DIRS", [a, b]), \
                 mock.patch.object(peer_health, "load_roles", return_value={"desktop": {"display_name": "desktop"}}), \
                 mock.patch.object(peer_health, "find", return_value=[]), \
                 mock.patch.object(peer_health, "proc_state", return_value="dead"):
                peer_health.probe(do_mirror=True)
            self.assertTrue((b / "sessions" / "42.json").exists())

    def test_summary_is_one_line(self):
        result = {
            "status": "ok",
            "roles": {"desktop": {"display_name": "desktop", "peer": {"name": "n"}, "other_sessions": 0}},
            "errors": [],
            "stale_entries": [],
        }
        text = peer_health.summarize(result)
        self.assertNotIn("\n", text)
        self.assertIn("desktop", text)


if __name__ == "__main__":
    unittest.main()
