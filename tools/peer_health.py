"""Peer channel health probe (for cron/patrol). Read-only by default.

It reports whether the **mechanism** is healthy, not who is online:

- ok       mechanism fine AND at least one live peer session
- offline  mechanism fine, nobody live right now (normal, not a fault). Also the state of a
           machine where the channel was never set up (no registry dir yet)
- degraded mechanism fine, no live peer, but the registry still holds entries (dead pids to
           clean up). A live peer plus some dead pids is still `ok` - dead pids are normal
- error    mechanism broken. That includes the shapes an upstream change would produce:
             - roles table unreadable / config dir unreadable
             - registry entries whose `pid` is not a number (field type changed)
             - a session that is alive but whose creation time does not match the registry
               (pid reuse, or the field semantics changed)
             - registry entries without `procStart` at all -> the pid-reuse guard is OFF
             - a session we may not open (access denied) -> we cannot verify anything

Exit code: 0 for ok/offline/degraded; 1 for error (only a broken mechanism should turn a
monitor red). Whether `SendMessage` actually delivers is NOT verified here - this script
cannot send; that check needs a real ping from an agent.

**Read-only by default**: it sends nothing and writes nothing. Mirroring (which copies
registry entries *including peer token files* into the other config dir) is opt-in with
`--mirror`; the default path never touches the other harness's registry.

Usage:
    python tools/peer_health.py          # one line
    python tools/peer_health.py --json   # machine readable
    python tools/peer_health.py --mirror # also sync the registries (writes!)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import peer_mirror  # noqa: E402
from peer_mirror import entries, load_roles, mirror  # noqa: E402
from peer_resolve import as_pid, find, proc_state  # noqa: E402


def probe(do_mirror: bool = False) -> dict:
    result: dict = {
        "status": "ok",
        "roles": {},
        "config_dirs": [],
        "missing_config_dirs": [],
        "stale_entries": [],
        "mismatched_entries": [],
        "malformed_entries": [],
        "unverifiable_entries": [],
        "errors": [],
        "notes": [],
    }

    roles = load_roles()
    if not roles:
        result["status"] = "error"
        result["errors"].append(f"roles table unreadable or empty: {peer_mirror.PEERS_JSON}")
        return result

    for config_dir in peer_mirror.CONFIG_DIRS:
        if not (config_dir / "sessions").is_dir():
            # not set up yet (or a wrong path) - not a broken mechanism
            result["missing_config_dirs"].append(str(config_dir))
        else:
            result["config_dirs"].append(str(config_dir))
    if result["missing_config_dirs"]:
        result["notes"].append(
            "no peer registry in: " + ", ".join(result["missing_config_dirs"])
        )

    if do_mirror:
        try:
            mirror(quiet=True)
        except Exception as exc:  # noqa: BLE001 - a failed mirror means the mechanism is broken
            result["status"] = "error"
            result["errors"].append(f"mirror failed: {type(exc).__name__}: {exc}")

    live_any = False
    for role, spec in roles.items():
        hits = find(role, spec)
        live_any = live_any or bool(hits)
        result["roles"][role] = {
            "display_name": spec.get("display_name"),
            "live": len(hits),
            "peer": (
                {
                    "name": hits[0].get("name"),
                    "pid": hits[0]["pid"],
                    "entrypoint": hits[0].get("entrypoint"),
                    "state": hits[0].get("state"),
                }
                if hits
                else None
            ),
            "other_sessions": max(0, len(hits) - 1),
        }

    # registry entries vs reality
    seen: set[int] = set()
    verifiable = 0
    for config_dir in peer_mirror.CONFIG_DIRS:
        for _js, _key, payload in entries(config_dir):
            pid = as_pid(payload)
            if pid is None:
                result["malformed_entries"].append(
                    {"pid": payload.get("pid"), "name": payload.get("name"), "cwd": payload.get("cwd")}
                )
                continue
            if pid in seen:
                continue
            seen.add(pid)
            state = proc_state(pid, payload.get("procStart"))
            if state == "alive":
                verifiable += 1
                continue
            if state == "unknown":
                # alive, but no procStart -> the pid-reuse guard cannot work for this entry
                result["unverifiable_entries"].append(
                    {"pid": pid, "name": payload.get("name"), "cwd": payload.get("cwd")}
                )
                continue
            if state == "mismatch":
                result["mismatched_entries"].append(
                    {"pid": pid, "name": payload.get("name"), "cwd": payload.get("cwd")}
                )
            elif state == "denied":
                result["unverifiable_entries"].append(
                    {"pid": pid, "name": payload.get("name"), "cwd": payload.get("cwd")}
                )
            elif state == "unsupported":
                result["status"] = "error"
                result["errors"].append(
                    "this platform has no Windows process API - the liveness probe cannot run"
                )
            else:  # dead / gone
                result["stale_entries"].append(
                    {"pid": pid, "name": payload.get("name"), "cwd": payload.get("cwd")}
                )

    if result["status"] != "error":
        if result["malformed_entries"]:
            result["status"] = "error"
            result["errors"].append(
                f"{len(result['malformed_entries'])} registry entr(y/ies) with a non-numeric pid - "
                "the registry field type changed"
            )
        elif result["mismatched_entries"]:
            result["status"] = "error"
            result["errors"].append(
                f"{len(result['mismatched_entries'])} live session(s) whose creation time does not "
                "match the registry - pid reuse, or the registry field semantics changed"
            )
        elif result["unverifiable_entries"]:
            result["status"] = "error"
            result["errors"].append(
                f"{len(result['unverifiable_entries'])} session(s) cannot be verified "
                "(no procStart in the registry, or access denied) - the pid-reuse guard is off"
            )
        elif live_any:
            # a live peer is enough; dead pids alongside it are normal registry litter
            result["status"] = "ok"
            if result["stale_entries"]:
                result["notes"].append(f"stale entries: {len(result['stale_entries'])}")
        elif result["stale_entries"]:
            result["status"] = "degraded"
        else:
            result["status"] = "offline"
    return result


def summarize(result: dict) -> str:
    status = result["status"]
    if status == "error":
        return "peer channel broken: " + "; ".join(result["errors"])
    parts = []
    for role, info in result["roles"].items():
        peer = info.get("peer")
        names = peer["name"] if peer else "no live session"
        extra = f" (+{info['other_sessions']} more)" if info.get("other_sessions") else ""
        parts.append(f"{role}({info['display_name']}): {names}{extra}")
    tail = f"; stale entries: {len(result['stale_entries'])}" if result["stale_entries"] else ""
    notes = ("; " + "; ".join(result["notes"])) if result.get("notes") else ""
    return f"peer channel {status}: " + " | ".join(parts) + tail + notes


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--mirror", action="store_true", help="also sync registries (writes to the peer dir)")
    ap.add_argument("--config-dir", action="append", help="config dir to scan (repeatable)")
    ap.add_argument("--roles", help="roles json path")
    args = ap.parse_args()
    if os.name != "nt":
        print(
            "peer_health needs Windows (NTFS junctions, named pipes, Windows process APIs).",
            file=sys.stderr,
        )
        return 2
    if args.config_dir:
        peer_mirror.set_config_dirs(args.config_dir)
    if args.roles:
        peer_mirror.set_peers_json(args.roles)
    result = probe(do_mirror=args.mirror)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=1))
    else:
        print(summarize(result))
    return 1 if result["status"] == "error" else 0


if __name__ == "__main__":
    sys.exit(main())
