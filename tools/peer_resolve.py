"""Resolve a live peer session by role and print its current addressable name.

`SendMessage` addresses peers by name, but names drift (auto-generated on first message,
changed by `/rename`, gone after archive) and pids get reused. Stable identity is the
**role**: `cwd_match` (a path fragment) + `entrypoints` (the entry type), see
`config/peers.json`. The `repo` field there is informational - it cannot be a hard
constraint, because a T3 thread runs in a worktree outside the repo path. The name is
only the handle valid at the moment you send.

This script:
1. mirrors the registries first (unless `--no-mirror`) — the other harness rewrites its own
   registry, so a copied entry is only short-lived;
2. scans the config dirs, matches cwd + entrypoint against the role table, and checks the
   process is still alive (pid alive AND creation time matches `procStart` -> guards pid reuse);
3. prints the peer's name **in the caller's own registry** — that is the string `SendMessage`
   accepts. When several sessions share a role, the one whose name equals the role's
   `display_name` wins, otherwise the newest.

Usage:
    python tools/peer_resolve.py --role mobile       # print the name to SendMessage to
    python tools/peer_resolve.py --role all --json   # list every live peer
    python tools/peer_resolve.py --pid 12345         # resolve a pid (e.g. your own $CLAUDE_PID)
    python tools/peer_resolve.py --role mobile --no-mirror   # read-only, no writes

Exit codes: 0 = found; 1 = no live session for that role; 2 = config/role problem.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import peer_mirror  # noqa: E402
from peer_mirror import entries, load_roles, mirror  # noqa: E402

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
STILL_ACTIVE = 259
ERROR_ACCESS_DENIED = 5


class FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", ctypes.c_ulong), ("dwHighDateTime", ctypes.c_ulong)]

    def as_int(self) -> int:
        return (self.dwHighDateTime << 32) | self.dwLowDateTime


def proc_alive(pid: int, proc_start) -> bool:
    """pid alive AND creation time matches the registry record (guards pid reuse).

    Never use `os.kill(pid, 0)` to test liveness on Windows: that calls TerminateProcess
    and kills the peer.
    """
    return proc_state(pid, proc_start) in ("alive", "unknown")


def as_pid(payload: dict):
    """The registry's pid, tolerating a numeric string.

    If upstream changes the field type, `int(pid)` keeps working instead of making the
    entry invisible (an invisible entry would read as "peer offline" and stay green).
    """
    pid = payload.get("pid")
    if isinstance(pid, bool):
        return None
    if isinstance(pid, int):
        return pid
    if isinstance(pid, str) and pid.strip().isdigit():
        return int(pid.strip())
    return None


def proc_state(pid: int, proc_start) -> str:
    """Liveness with a reason, so the health probe can tell problems apart.

    - alive     : the process runs and its creation time matches the registry
    - unknown   : the process runs but the registry has no `procStart` to compare
                  (the pid-reuse guard is OFF for this entry - that is not "healthy")
    - mismatch  : the process runs but its creation time differs -> either the pid was
                  reused, or the registry field semantics changed (that is a mechanism
                  break, not a stale entry)
    - denied    : the process exists but we may not open it (another user / session)
    - dead      : the process is gone
    - gone      : no such process
    - unsupported: this platform has no Windows process API (see `windows_only`)
    """
    if os.name != "nt":
        return "unsupported"
    kernel32 = ctypes.windll.kernel32
    kernel32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.GetProcessTimes.argtypes = [ctypes.c_void_p] + [ctypes.POINTER(FILETIME)] * 4
    kernel32.GetProcessTimes.restype = ctypes.c_int
    kernel32.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
    kernel32.GetExitCodeProcess.restype = ctypes.c_int

    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        # NULL means "denied" as well as "absent" - check the error code before giving up
        if kernel32.GetLastError() == ERROR_ACCESS_DENIED:
            return "denied"
        return "gone"
    try:
        creation, exit_t, kernel_t, user_t = (FILETIME() for _ in range(4))
        exit_code = ctypes.c_ulong()
        if not kernel32.GetProcessTimes(
            handle,
            ctypes.byref(creation),
            ctypes.byref(exit_t),
            ctypes.byref(kernel_t),
            ctypes.byref(user_t),
        ):
            return "gone"
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return "gone"
        if exit_code.value != STILL_ACTIVE:
            return "dead"
        if not proc_start:
            return "unknown"
        if creation.as_int() != int(proc_start):
            return "mismatch"  # pid reused, or the registry field semantics changed
        return "alive"
    finally:
        kernel32.CloseHandle(handle)


def caller_dir() -> Path:
    """The caller's own Claude config dir — `SendMessage` looks the peer up in its names."""
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(env) if env else peer_mirror.CONFIG_DIRS[0]


def read_entry(path: Path, retries: int = 10) -> dict | None:
    """The registry file has a ~100ms hole while the harness rewrites it; retry.

    Retries on a partially written file too (JSONDecodeError is a ValueError): a half-written
    entry must not be mistaken for "no entry".
    """
    for _ in range(retries):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            time.sleep(0.05)
        except ValueError:
            time.sleep(0.05)
        except OSError:
            return None
    return None


def path_parts(path: str) -> list[str]:
    return [p for p in re.split(r"[\\/]+", (path or "").lower()) if p]


def contains_parts(haystack: list[str], needle: list[str]) -> bool:
    """True if `needle` appears as a contiguous run of path components.

    Component-wise, not substring: `my-repo` must not match `my-repo-2`.
    """
    if not needle or len(needle) > len(haystack):
        return False
    for i in range(len(haystack) - len(needle) + 1):
        if haystack[i : i + len(needle)] == needle:
            return True
    return False


def match(payload: dict, spec: dict) -> bool:
    """Role match = the repo path contains `cwd_match` as whole path components AND the
    entrypoint is listed."""
    if not contains_parts(path_parts(payload.get("cwd") or ""), path_parts(spec.get("cwd_match") or "")):
        return False
    entrypoints = spec.get("entrypoints")
    if entrypoints and payload.get("entrypoint") not in entrypoints:
        return False
    return True


def find(role: str, spec: dict) -> list[dict]:
    """All live sessions for a role, each with the name to use in *our* registry."""
    cdir = caller_dir()
    hits: list[dict] = []
    seen: set[int] = set()
    for config_dir in peer_mirror.CONFIG_DIRS:
        for _js, _key, payload in entries(config_dir):
            pid = as_pid(payload)
            if pid is None or pid in seen:
                continue
            if not match(payload, spec):
                continue
            state = proc_state(pid, payload.get("procStart"))
            if state in ("dead", "gone", "denied", "unsupported"):
                continue
            seen.add(pid)
            local = read_entry(cdir / "sessions" / f"{pid}.json")
            # a session name only — never fall back to the role label: that is not an address
            name = (local or {}).get("name") or payload.get("name")
            hits.append(
                {
                    "role": role,
                    "pid": pid,
                    "name": name,
                    "state": state,
                    "named": name == spec.get("display_name"),
                    "cwd": payload.get("cwd"),
                    "entrypoint": payload.get("entrypoint"),
                    "started": int(payload.get("procStart") or 0),
                }
            )
    # name matching the role wins, then a healthy process, then the newest
    hits.sort(key=lambda h: (h["named"], h["state"] == "alive", h["started"]), reverse=True)
    return hits


def find_by_pid(pid: int) -> list[dict]:
    """Resolve by pid: no role table needed, works in any repo (e.g. your own $CLAUDE_PID)."""
    cdir = caller_dir()
    hits: list[dict] = []
    seen: set[int] = set()
    for config_dir in peer_mirror.CONFIG_DIRS:
        for _js, _key, payload in entries(config_dir):
            if as_pid(payload) != pid or pid in seen:
                continue
            state = proc_state(pid, payload.get("procStart"))
            if state in ("dead", "gone", "denied", "unsupported"):
                continue
            seen.add(pid)
            local = read_entry(cdir / "sessions" / f"{pid}.json")
            name = (local or {}).get("name") or payload.get("name")
            hits.append(
                {
                    "role": None,
                    "pid": pid,
                    "name": name,
                    "state": state,
                    "named": False,
                    "cwd": payload.get("cwd"),
                    "entrypoint": payload.get("entrypoint"),
                    "started": int(payload.get("procStart") or 0),
                }
            )
    return hits


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--role", help="role name (see config/peers.json), or 'all'")
    ap.add_argument(
        "--pid",
        type=int,
        help="resolve by pid (e.g. your own $CLAUDE_PID); no role table needed",
    )
    ap.add_argument("--json", action="store_true")
    ap.add_argument(
        "--no-mirror",
        action="store_true",
        help="skip registry mirroring (read-only use; writes nothing, may see stale entries)",
    )
    ap.add_argument("--config-dir", action="append", help="config dir to scan (repeatable)")
    ap.add_argument("--roles", help="roles json path")
    args = ap.parse_args()

    if args.role is None and args.pid is None:
        ap.error("give --role or --pid")
    if os.name != "nt":
        print(
            "this kit's liveness probe needs Windows (NTFS junctions, named pipes, "
            "Windows process APIs). On this platform the peer registry cannot be checked.",
            file=sys.stderr,
        )
        return 2
    if args.config_dir:
        peer_mirror.set_config_dirs(args.config_dir)
    if args.roles:
        peer_mirror.set_peers_json(args.roles)

    if not args.no_mirror:
        mirror(quiet=True)  # sync first, else the peer's entry may have just been rewritten

    if args.pid is not None:
        hits = find_by_pid(args.pid)
        if not hits:
            print(f"pid {args.pid}: no live session in registry", file=sys.stderr)
            return 1
        if not hits[0].get("name"):
            print(f"pid {args.pid}: session is alive but has no addressable name", file=sys.stderr)
            return 2
        print(json.dumps(hits, ensure_ascii=False, indent=1) if args.json else hits[0]["name"])
        return 0

    roles = load_roles()
    if not roles:
        print(f"roles table unreadable or empty: {peer_mirror.PEERS_JSON}", file=sys.stderr)
        return 2

    if args.role == "all":
        rows: list[dict] = []
        for role, spec in roles.items():
            rows.extend(find(role, spec))
        if args.json:
            print(json.dumps(rows, ensure_ascii=False, indent=1))
        else:
            print(
                "\n".join(
                    f"{r['role']:<9} {r['name']:<14} pid={r['pid']} {r['entrypoint']}" for r in rows
                )
            )
        return 0 if rows else 1

    spec = roles.get(args.role)
    if not spec:
        print(f"unknown role {args.role}; known: {', '.join(roles)}", file=sys.stderr)
        return 2

    hits = find(args.role, spec)
    if not hits:
        print(
            f"{args.role}: no live session right now "
            f"(role table matched nothing — check cwd_match vs this machine's paths and "
            f"entrypoints vs the entry type)",
            file=sys.stderr,
        )
        return 1
    if not hits[0].get("name"):
        print(f"{args.role}: session is alive but has no addressable name", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(hits, ensure_ascii=False, indent=1))
    else:
        print(hits[0]["name"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
