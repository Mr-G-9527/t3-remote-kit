import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import peer_health
import peer_mirror


ROLES = {"desktop": {"display_name": "desktop", "cwd_match": "my-repo", "entrypoints": ["cli"]}}


class PeerHealthTests(unittest.TestCase):
    def _patch_dirs(self, tmp: str):
        d = Path(tmp)
        (d / "sessions").mkdir(parents=True, exist_ok=True)
        return mock.patch.object(peer_mirror, "CONFIG_DIRS", [d])

    def _probe(self, tmp, *, live=(), entries_=(), state="alive", roles=ROLES, dirs=None,
               state_by_pid=None):
        def _state(pid, proc_start):
            if state_by_pid is not None:
                return state_by_pid.get(pid, state)
            return state

        patches = [
            mock.patch.object(peer_health, "load_roles", return_value=roles),
            mock.patch.object(peer_health, "find", return_value=list(live)),
            mock.patch.object(peer_health, "entries", return_value=list(entries_)),
            mock.patch.object(peer_health, "proc_state", side_effect=_state),
        ]
        if dirs is not None:
            patches.append(mock.patch.object(peer_mirror, "CONFIG_DIRS", dirs))
        with tempfile.TemporaryDirectory() as t:
            for p in patches:
                p.start()
            try:
                if dirs is None:
                    with self._patch_dirs(t):
                        return peer_health.probe()
                return peer_health.probe()
            finally:
                for p in patches:
                    p.stop()

    def test_missing_roles_table_is_an_error(self):
        with mock.patch.object(peer_health, "load_roles", return_value={}):
            result = peer_health.probe()
        self.assertEqual(result["status"], "error")
        self.assertTrue(result["errors"])

    def test_no_live_peer_is_offline(self):
        result = self._probe(None, live=[], entries_=[], state="dead")
        self.assertEqual(result["status"], "offline")

    def test_live_peer_is_ok(self):
        live = [{"name": "n", "pid": 1, "entrypoint": "cli", "state": "alive"}]
        result = self._probe(None, live=live, entries_=[], state="alive")
        self.assertEqual(result["status"], "ok")

    def test_live_peer_plus_dead_entries_is_still_ok(self):
        # dead pids are normal registry litter; they must not hide a healthy channel
        live = [{"name": "n", "pid": 1, "entrypoint": "cli", "state": "alive"}]
        entries_ = [(None, None, {"pid": 999, "name": "dead"})]
        result = self._probe(None, live=live, entries_=entries_, state="alive",
                             state_by_pid={1: "alive", 999: "dead"})
        self.assertEqual(result["status"], "ok")
        self.assertEqual(len(result["stale_entries"]), 1)

    def test_dead_entries_without_a_live_peer_is_degraded(self):
        entries_ = [(None, None, {"pid": 999, "name": "dead"})]
        result = self._probe(None, live=[], entries_=entries_, state="dead")
        self.assertEqual(result["status"], "degraded")

    def test_creation_time_mismatch_is_an_error_not_a_stale_entry(self):
        entries_ = [(None, None, {"pid": 7, "name": "x"})]
        result = self._probe(None, live=[], entries_=entries_, state="mismatch")
        self.assertEqual(result["status"], "error")
        self.assertEqual(len(result["mismatched_entries"]), 1)
        self.assertEqual(result["stale_entries"], [])

    def test_unparseable_pid_is_an_error(self):
        # upstream changed the pid field into something we cannot use -> entries would be
        # invisible everywhere, which must not read as "just offline"
        entries_ = [(None, None, {"pid": "not-a-pid", "name": "x"})]
        result = self._probe(None, live=[], entries_=entries_, state="alive")
        self.assertEqual(result["status"], "error")
        self.assertEqual(len(result["malformed_entries"]), 1)

    def test_numeric_string_pid_is_tolerated(self):
        # upstream changing pid to a string must not make the entry invisible
        entries_ = [(None, None, {"pid": "999", "name": "x"})]
        result = self._probe(None, live=[], entries_=entries_, state="dead")
        self.assertEqual(result["malformed_entries"], [])
        self.assertEqual(len(result["stale_entries"]), 1)

    def test_missing_proc_start_is_an_error_because_the_guard_is_off(self):
        entries_ = [(None, None, {"pid": 7, "name": "x"})]
        result = self._probe(None, live=[], entries_=entries_, state="unknown")
        self.assertEqual(result["status"], "error")
        self.assertEqual(len(result["unverifiable_entries"]), 1)

    def test_denied_session_is_an_error(self):
        entries_ = [(None, None, {"pid": 7, "name": "x"})]
        result = self._probe(None, live=[], entries_=entries_, state="denied")
        self.assertEqual(result["status"], "error")

    def test_missing_registry_dir_is_not_a_broken_mechanism(self):
        # a machine where the channel was never set up must not report a broken mechanism
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "nope"
            with mock.patch.object(peer_mirror, "CONFIG_DIRS", [missing]), \
                 mock.patch.object(peer_health, "load_roles", return_value=ROLES), \
                 mock.patch.object(peer_health, "find", return_value=[]), \
                 mock.patch.object(peer_health, "entries", return_value=[]):
                result = peer_health.probe()
        self.assertEqual(result["status"], "offline")
        self.assertTrue(result["missing_config_dirs"])

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
                 mock.patch.object(peer_health, "load_roles", return_value=ROLES), \
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
                 mock.patch.object(peer_health, "load_roles", return_value=ROLES), \
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
            "notes": [],
        }
        text = peer_health.summarize(result)
        self.assertNotIn("\n", text)
        self.assertIn("desktop", text)


if __name__ == "__main__":
    unittest.main()
