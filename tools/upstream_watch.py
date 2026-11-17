"""Upstream watch: did the two upstreams we actually depend on move?

This kit breaks in two ways, and only two:

1. **Claude Code** changes its peer registry format, its `CLAUDE_CODE_ENTRYPOINT`
   semantics, or how `SendMessage` addresses a session.
2. **T3 Code** changes provider instance ids, the model manifest, or thread/worktree
   behaviour.

T3 ships a nightly every few hours; Claude Code ships patches constantly. Flagging every
version bump would be noise, so each component declares its own comparison rule:

- Claude Code -> compare **major.minor** only (patch releases rarely change the registry).
- T3 Code   -> compare the **full release tag** (its stable line moves rarely).

Which components are watched is declared in `config/upstream_baseline.json` - not in this
code. Each component says how to fetch it:

    {"kind": "npm_version",   "package": "@anthropic-ai/claude-code", ...}
    {"kind": "github_release", "repo":    "pingdotgg/t3code", ...}

An unknown `kind`, a component that cannot be fetched, or a baseline component that this
run did not check are all treated as **"could not check"** (exit 2), never as "no drift".
That matters: a silently unwatched component would keep the alerting green forever.

Usage:
    python tools/upstream_watch.py --check                    # print a report
    python tools/upstream_watch.py --check --report drift.json # also write a JSON report
    python tools/upstream_watch.py --check --strict           # exit 1 on drift (gate a release)
    python tools/upstream_watch.py --update-baseline          # only AFTER a human reconciliation

Exit codes: 0 = no drift (or drift without --strict); 1 = drift and --strict;
2 = could not check (network / bad baseline / unknown component kind) - that is *not* drift,
do not report it as such, and never let it look like a green check.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_BASELINE = REPO_ROOT / "config" / "upstream_baseline.json"


def fetch_json(url: str, timeout: int = 15) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "t3-remote-kit-upstream-watch"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - fixed https URLs
        return json.loads(resp.read().decode("utf-8"))


def fetch_component(spec: dict, fetcher=None) -> str:
    """Fetch the current version of one baseline component.

    The component declares its own kind, so adding a component is a baseline change, not a
    code change.
    """
    fetcher = fetcher or fetch_json
    kind = spec.get("kind")
    if kind == "npm_version":
        url = spec.get("source") or f"https://registry.npmjs.org/{spec.get('package')}/latest"
        return str(fetcher(url)["version"])
    if kind == "github_release":
        url = spec.get("source") or f"https://api.github.com/repos/{spec.get('repo')}/releases/latest"
        return str(fetcher(url)["tag_name"])
    raise ValueError(f"unknown component kind: {kind!r}")


def major_minor(version: str) -> str:
    """'2.1.326' -> '2.1'. Patch bumps alone are not drift for us."""
    m = re.match(r"v?(\d+)\.(\d+)", version or "")
    return f"{m.group(1)}.{m.group(2)}" if m else (version or "")


def load_baseline(path: Path = DEFAULT_BASELINE) -> dict:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not data.get("components"):
        raise ValueError("baseline file has no 'components'")
    return data


def save_baseline(path: Path, data: dict) -> None:
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def build_drift(baseline: dict, current: dict) -> list[dict]:
    """Compare the baseline against the current upstream versions.

    `current` maps component -> version string. Each component in the baseline declares its
    own comparison rule ('major.minor' or 'exact'). A baseline component that is missing
    from `current` is an ERROR, not "nothing to compare": that would silently unwatch it.
    """
    drift: list[dict] = []
    for name, spec in (baseline.get("components") or {}).items():
        if name not in current:
            raise ValueError(f"baseline component {name!r} was not checked in this run")
        want = str(spec.get("version") or "")
        got = str(current[name])
        rule = spec.get("compare", "exact")
        same = major_minor(want) == major_minor(got) if rule == "major.minor" else want == got
        if not same:
            drift.append(
                {
                    "component": name,
                    "baseline": want,
                    "current": got,
                    "compare": rule,
                    "source": spec.get("source"),
                    "why_it_matters": spec.get("why_it_matters"),
                }
            )
    return drift


def check(baseline: dict, fetcher=None) -> dict:
    """Collect current versions for every baseline component and diff them."""
    fetcher = fetcher or fetch_json
    components = baseline.get("components") or {}
    if not components:
        raise ValueError("baseline has no components")
    current = {name: fetch_component(spec, fetcher) for name, spec in components.items()}
    return {
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "current": current,
        "drift": build_drift(baseline, current),
    }


def issue_body(result: dict) -> str:
    """Markdown body for a GitHub issue (used by CI)."""
    lines = ["An upstream release may have broken this kit.", ""]
    for d in result["drift"]:
        lines.append(
            f"- **{d['component']}**: baseline `{d['baseline']}` -> current `{d['current']}` "
            f"(compare: {d['compare']})"
        )
        if d.get("why_it_matters"):
            lines.append(f"  - why it matters: {d['why_it_matters']}")
    lines += [
        "",
        "Action: read the upstream diff, then either reconcile or update "
        "`config/upstream_baseline.json`.",
    ]
    return "\n".join(lines)


def report_text(result: dict) -> str:
    lines = [f"checked_at: {result['checked_at']}"]
    for name, version in result["current"].items():
        lines.append(f"  {name:<12} {version}")
    if not result["drift"]:
        lines.append("no drift vs baseline")
        return "\n".join(lines)
    lines.append("DRIFT:")
    for d in result["drift"]:
        lines.append(
            f"  {d['component']}: baseline {d['baseline']} -> current {d['current']} "
            f"(compare={d['compare']})"
        )
        if d.get("why_it_matters"):
            lines.append(f"      why: {d['why_it_matters']}")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="compare against the baseline")
    ap.add_argument("--report", help="write a JSON report here when drift is found")
    ap.add_argument("--strict", action="store_true", help="exit 1 on drift")
    ap.add_argument("--baseline", default=str(DEFAULT_BASELINE))
    ap.add_argument("--update-baseline", action="store_true", help="rewrite the baseline file")
    ap.add_argument("--issue-body", help="read a drift report JSON, print a markdown issue body")
    args = ap.parse_args()

    if args.issue_body:
        data = json.loads(Path(args.issue_body).read_text(encoding="utf-8"))
        print(issue_body(data))
        return 0

    try:
        baseline = load_baseline(Path(args.baseline))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"baseline unreadable: {exc}", file=sys.stderr)
        return 2

    try:
        result = check(baseline)
    except (urllib.error.URLError, OSError, KeyError, ValueError) as exc:
        print(f"could not check upstream: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2  # not drift: do not report a false alarm, do not look green

    if args.update_baseline:
        baseline["checked_at"] = result["checked_at"]
        for name, spec in (baseline.get("components") or {}).items():
            if name in result["current"]:
                spec["version"] = result["current"][name]
        save_baseline(Path(args.baseline), baseline)
        print(f"baseline updated -> {args.baseline}")
        return 0

    print(report_text(result))
    if result["drift"]:
        if args.report:
            Path(args.report).write_text(
                json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            print(f"report written -> {args.report}")
        return 1 if args.strict else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
