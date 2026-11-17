"""Peer channel health probe (for cron/patrol). Read-only by default.

It reports whether the **mechanism** is healthy, not who is online:

- ok       mechanism fine and at least one live peer session
- offline  mechanism fine, but no live peer right now (normal, not a fault)
- degraded mechanism fine, but the registry holds stale entries (dead pids; harmless)
- error    mechanism broken: roles table unreadable / config dir missing / mirroring failed /
           **a session whose creation time does not match the registry** (that means either the
           pid was reused or the registry field semantics changed upstream — either way this
           kit's central guard stopped working, so it must not be reported as "just stale")

Exit code: 0 for ok/offline/degraded; 1 for error (only a broken mechanism should turn a
monitor red). Whether `SendMessage` actually delivers is NOT verified here — this script
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
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import peer_mirror  # noqa: E402
from peer_mirror import entries, load_roles, mirror  # noqa: E402
from peer_resolve import find, proc_state  # noqa: E402


def probe(do_mirror: bool = False) -> dict:
    result: dict = {
        "status": "ok",
        "roles": {},
        "config_dirs": [],
        "stale_entries": [],
        "mismatched_entries": [],
        "errors": [],
    }

    roles = load_roles()
    if not roles:
        result["status"] = "error"
        result["errors"].append(f"roles table unreadable or empty: {peer_mirror.PEERS_JSON}")
        return result

    for config_dir in peer_mirror.CONFIG_DIRS:
        if not (config_dir / "sessions").is_dir():
            result["status"] = "error"
            result["errors"].append(f"peer registry dir missing: {config_dir / 'sessions'}")
        else:
            result["config_dirs"].append(str(config_dir))

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

    # registry entries vs reality: dead pid = stale; alive pid with a different creation
    # time = the field semantics changed (or the pid was reused) -> mechanism break
    seen: set[int] = set()
    for config_dir in peer_mirror.CONFIG_DIRS:
        for _js, _key, payload in entries(config_dir):
            pid = payload.get("pid")
            if not isinstance(pid, int) or pid in seen:
                continue
            seen.add(pid)
            state = proc_state(pid, payload.get("procStart"))
            if state in ("alive", "unknown"):
                continue
            if state == "mismatch":
                result["mismatched_entries"].append(
                    {"pid": pid, "name": payload.get("name"), "cwd": payload.get("cwd")}
                )
            else:
                result["stale_entries"].append(
                    {"pid": pid, "name": payload.get("name"), "cwd": payload.get("cwd")}
                )

    if result["status"] != "error":
        if result["mismatched_entries"]:
            result["status"] = "error"
            result["errors"].append(
                f"{len(result['mismatched_entries'])} live session(s) whose creation time does not "
                "match the registry - pid reuse, or the registry field semantics changed"
            )
        elif result["stale_entries"]:
            result["status"] = "degraded"
        elif not live_any:
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
    return f"peer channel {status}: " + " | ".join(parts) + tail


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--mirror", action="store_true", help="also sync registries (writes to the peer dir)")
    ap.add_argument("--config-dir", action="append", help="config dir to scan (repeatable)")
    ap.add_argument("--roles", help="roles json path")
    args = ap.parse_args()
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
