import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import peer_mirror
import peer_resolve


class PeerChannelTests(unittest.TestCase):
    def test_roles_table_has_roles_with_display_name_and_cwd_match(self):
        roles = peer_mirror.load_roles()
        self.assertTrue(roles, "config/peers.json must ship with at least one role")
        for role, spec in roles.items():
            self.assertTrue(spec.get("display_name"), role)
            self.assertTrue(spec.get("cwd_match"), role)

    def test_match_requires_cwd_and_entrypoint(self):
        spec = {"cwd_match": "my-repo", "entrypoints": ["cli"]}
        self.assertTrue(peer_resolve.match({"cwd": "C:\\ws\\my-repo", "entrypoint": "cli"}, spec))
        # right dir, wrong entrypoint (a T3 thread in the same repo) does not match
        self.assertFalse(peer_resolve.match({"cwd": "C:\\ws\\my-repo", "entrypoint": "t3-code"}, spec))
        # right entrypoint, wrong repo does not match
        self.assertFalse(peer_resolve.match({"cwd": "C:\\other", "entrypoint": "cli"}, spec))

    def test_match_is_component_wise_not_substring(self):
        spec = {"cwd_match": "my-repo", "entrypoints": ["cli"]}
        # a different repo whose name merely starts with the marker must not match
        self.assertFalse(peer_resolve.match({"cwd": "C:\\ws\\my-repo-2", "entrypoint": "cli"}, spec))
        self.assertFalse(peer_resolve.match({"cwd": "C:\\ws\\my-repo-old", "entrypoint": "cli"}, spec))
        # the marker may sit anywhere in the path, as whole components
        self.assertTrue(
            peer_resolve.match({"cwd": "C:\\ws\\archive\\my-repo", "entrypoint": "cli"}, spec)
        )

    def test_match_handles_t3_worktree_paths(self):
        spec = {"cwd_match": ".t3\\worktrees\\myrepo", "entrypoints": ["t3-code"]}
        self.assertTrue(
            peer_resolve.match(
                {"cwd": "C:\\ws\\.t3\\worktrees\\myrepo\\abc123", "entrypoint": "t3-code"}, spec
            )
        )
        self.assertFalse(
            peer_resolve.match(
                {"cwd": "C:\\ws\\.t3\\worktrees\\otherrepo\\abc123", "entrypoint": "t3-code"}, spec
            )
        )

    def test_find_prefers_named_peer_then_newest(self):
        spec = {"cwd_match": "my-repo", "entrypoints": ["cli"], "display_name": "desktop"}
        peers = [
            {"pid": 1, "name": "old", "cwd": "C:\\ws\\my-repo", "entrypoint": "cli", "procStart": 10},
            {"pid": 2, "name": "desktop", "cwd": "C:\\ws\\my-repo", "entrypoint": "cli", "procStart": 5},
            {"pid": 3, "name": "newer", "cwd": "C:\\ws\\my-repo", "entrypoint": "cli", "procStart": 99},
        ]
        with mock.patch.object(peer_resolve, "entries", return_value=[(None, None, p) for p in peers]), \
             mock.patch.object(peer_resolve, "proc_state", return_value="alive"), \
             mock.patch.object(peer_resolve, "read_entry", return_value=None):
            hits = peer_resolve.find("desktop", spec)
        self.assertEqual(hits[0]["pid"], 2)  # the named one wins even if it is not the newest

    def test_find_by_pid_ignores_role_table(self):
        peers = [{"pid": 7, "name": "me", "cwd": "C:\\ws\\my-repo", "entrypoint": "cli", "procStart": 3}]
        with mock.patch.object(peer_resolve, "entries", return_value=[(None, None, p) for p in peers]), \
             mock.patch.object(peer_resolve, "proc_state", return_value="alive"), \
             mock.patch.object(peer_resolve, "read_entry", return_value=None):
            hits = peer_resolve.find_by_pid(7)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["name"], "me")

    def test_find_never_falls_back_to_the_role_label(self):
        # a session with no name in any registry is not addressable -> name must stay None
        spec = {"cwd_match": "my-repo", "entrypoints": ["cli"], "display_name": "desktop"}
        peers = [{"pid": 8, "cwd": "C:\\ws\\my-repo", "entrypoint": "cli", "procStart": 3}]
        with mock.patch.object(peer_resolve, "entries", return_value=[(None, None, p) for p in peers]), \
             mock.patch.object(peer_resolve, "proc_state", return_value="alive"), \
             mock.patch.object(peer_resolve, "read_entry", return_value=None):
            hits = peer_resolve.find("desktop", spec)
        self.assertIsNone(hits[0]["name"])
        self.assertNotEqual(hits[0]["name"], "desktop")

    def test_read_entry_retries_a_partially_written_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "1.json"
            path.write_text('{"pid": 1, "na', encoding="utf-8")  # half written
            with mock.patch.object(peer_resolve.time, "sleep"):
                self.assertIsNone(peer_resolve.read_entry(path, retries=3))

    def test_mirror_copies_json_and_key_both_ways(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = Path(tmp) / "a"
            b = Path(tmp) / "b"
            (a / "sessions").mkdir(parents=True)
            (b / "sessions").mkdir(parents=True)
            payload = {"pid": 42, "name": "x", "cwd": "C:\\ws", "entrypoint": "cli", "updatedAt": 1}
            (a / "sessions" / "42.json").write_text(json.dumps(payload), encoding="utf-8")
            (a / "sessions" / "42.tok.key").write_text("peerToken", encoding="utf-8")
            with mock.patch.object(peer_mirror, "CONFIG_DIRS", [a, b]):
                copied, _skipped = peer_mirror.mirror(quiet=True)
            self.assertGreater(copied, 0)
            self.assertTrue((b / "sessions" / "42.json").exists())
            self.assertTrue((b / "sessions" / "42.tok.key").exists())
            self.assertEqual(json.loads((b / "sessions" / "42.json").read_text(encoding="utf-8")), payload)

    def test_mirror_writes_atomically_and_leaves_no_temp_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "src.json"
            dst = Path(tmp) / "dst.json"
            src.write_text(json.dumps({"pid": 1}), encoding="utf-8")
            peer_mirror.atomic_copy(src, dst)
            self.assertEqual(json.loads(dst.read_text(encoding="utf-8")), {"pid": 1})
            self.assertEqual([p.name for p in Path(tmp).glob("*.tmp")], [])

    def test_mirror_skips_when_dirs_share_one_registry(self):
        # junction case: dst resolves to the same file -> nothing copied
        with tempfile.TemporaryDirectory() as tmp:
            a = Path(tmp) / "a"
            (a / "sessions").mkdir(parents=True)
            (a / "sessions" / "42.json").write_text(json.dumps({"pid": 42, "updatedAt": 1}), encoding="utf-8")
            with mock.patch.object(peer_mirror, "CONFIG_DIRS", [a, a]):
                copied, _skipped = peer_mirror.mirror(quiet=True)
            self.assertEqual(copied, 0)

    def test_no_mirror_flag_writes_nothing(self):
        with mock.patch.object(sys, "argv", ["peer_resolve.py", "--role", "desktop", "--no-mirror"]), \
             mock.patch.object(peer_resolve, "mirror") as m, \
             mock.patch.object(peer_resolve, "find", return_value=[]):
            rc = peer_resolve.main()
        m.assert_not_called()
        self.assertEqual(rc, 1)  # no live peer -> rc 1, but nothing was written

    @unittest.skipUnless(os.name == "nt", "Windows process APIs only")
    def test_proc_alive_rejects_mismatched_proc_start(self):
        # a short-lived child: alive, but a wrong procStart must read as dead (pid reuse guard)
        import subprocess
        import time
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)"])
        try:
            time.sleep(0.3)
            self.assertTrue(peer_resolve.proc_alive(proc.pid, None))
            self.assertFalse(peer_resolve.proc_alive(proc.pid, "12345"))
            self.assertEqual(peer_resolve.proc_state(proc.pid, "12345"), "mismatch")
        finally:
            proc.terminate()


class ConfigDirResolutionTests(unittest.TestCase):
    def test_env_overrides_default_dirs(self):
        raw = str(Path("/tmp/a")) + os.pathsep + str(Path("/tmp/b"))
        with mock.patch.dict("os.environ", {"PEER_CONFIG_DIRS": raw}):
            dirs = peer_mirror.default_config_dirs()
        self.assertEqual([str(Path("/tmp/a")), str(Path("/tmp/b"))], [str(d) for d in dirs])

    def test_default_is_user_claude_dir(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            dirs = peer_mirror.default_config_dirs()
        self.assertEqual(len(dirs), 1)
        self.assertEqual(dirs[0].name, ".claude")


if __name__ == "__main__":
    unittest.main()
