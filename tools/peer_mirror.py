"""Mutual registry mirroring: make Claude Code sessions in two config dirs visible to each other.

Why: Claude Code keeps a peer registry under `CLAUDE_CONFIG_DIR`. A mobile T3 Code thread
usually runs with its own config dir (e.g. `~/.claude_minimax`), the desktop CLI uses `~/.claude`.
Neither sees the other. Registry entries carry `messagingSocketPath` (a machine-global named
pipe on Windows), so copying entries across is enough for `SendMessage` to deliver — no
provider config change needed.

Two pitfalls (measured, Windows):
- The other harness rewrites its own registry periodically (delete-then-write, ~100ms gap),
  so copied entries are only valid for a short time -> always mirror right before resolving.
- Session names change (auto-generated on first message, or `/rename`). Identity is the
  *role* (see `config/peers.json`), not the name. This script copies verbatim.

Side effects: only adds/updates `*.json` and `*.key` files under `<config-dir>/sessions/`.

Config dirs, in priority order:
  1. `--config-dir` (repeatable)
  2. `PEER_CONFIG_DIRS` env var (os.pathsep separated)
  3. default: `~/.claude`, plus `$CLAUDE_CONFIG_DIR` when set and different

Usage:
    python tools/peer_mirror.py                     # sync
    python tools/peer_mirror.py --dry-run           # report only
    python tools/peer_mirror.py --config-dir <dir>  # add a dir
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

HOME = Path(os.environ.get("USERPROFILE") or Path.home())
REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PEERS_JSON = REPO_ROOT / "config" / "peers.json"


def default_config_dirs() -> list[Path]:
    """Config dirs to scan. See module docstring for the priority order."""
    env = os.environ.get("PEER_CONFIG_DIRS", "")
    if env.strip():
        return [Path(p.strip()).expanduser() for p in env.split(os.pathsep) if p.strip()]
    dirs = [HOME / ".claude"]
    cur = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    if cur:
        d = Path(cur).expanduser()
        if d not in dirs:
            dirs.append(d)
    return dirs


CONFIG_DIRS: list[Path] = default_config_dirs()
PEERS_JSON: Path = DEFAULT_PEERS_JSON


def set_config_dirs(dirs: list[str | Path]) -> None:
    global CONFIG_DIRS
    CONFIG_DIRS = [Path(d).expanduser() for d in dirs]


def set_peers_json(path: str | Path) -> None:
    global PEERS_JSON
    PEERS_JSON = Path(path).expanduser()


def load_roles(path: Path | None = None) -> dict[str, dict]:
    """Read the role table (repo + role + entrypoint). Empty dict if unreadable."""
    target = Path(path) if path else PEERS_JSON
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data.get("peers") or {}


def entries(config_dir: Path) -> list[tuple[Path, Path | None, dict]]:
    """One session = `<pid>.json` + `<pid>.<token>.key`. Returns (json, key, payload)."""
    sessions = config_dir / "sessions"
    out: list[tuple[Path, Path | None, dict]] = []
    if not sessions.is_dir():
        return out
    for js in sessions.glob("*.json"):
        key = next(iter(sessions.glob(f"{js.stem}.*.key")), None)
        try:
            payload = json.loads(js.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        out.append((js, key, payload))
    return out


def updated_at(payload: dict) -> int:
    try:
        return int(payload.get("updatedAt") or 0)
    except (TypeError, ValueError):
        return 0


def mirror(
    dry_run: bool = False,
    quiet: bool = False,
    dirs: list[Path] | None = None,
) -> tuple[int, int]:
    """Copy registry entries between config dirs. Returns (copied, skipped)."""
    dirs = dirs or CONFIG_DIRS
    copied = skipped = 0
    for src_dir, dst_dir in zip(dirs, dirs[1:] + dirs[:1]):
        if not src_dir.is_dir() or not dst_dir.is_dir():
            continue
        for js, key, payload in entries(src_dir):
            for f in [p for p in (js, key) if p is not None]:
                dst = dst_dir / "sessions" / f.name
                try:
                    if dst.resolve() == f.resolve():
                        continue  # dirs already share one registry (junction) -> nothing to copy
                except OSError:
                    pass
                if dst.exists():
                    try:
                        cur = json.loads(dst.read_text(encoding="utf-8"))
                    except (OSError, ValueError):
                        cur = {}
                    if updated_at(cur) >= updated_at(payload):
                        skipped += 1
                        continue
                if not dry_run:
                    dst_dir.joinpath("sessions").mkdir(parents=True, exist_ok=True)
                    shutil.copy2(f, dst)
                copied += 1
                if not quiet:
                    print(f"{'would copy' if dry_run else 'copied'}: {f.name} -> {dst_dir.name}")
    return copied, skipped


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--config-dir", action="append", help="config dir to scan (repeatable)")
    ap.add_argument("--roles", help="roles json path")
    args = ap.parse_args()
    if args.config_dir:
        set_config_dirs(args.config_dir)
    if args.roles:
        set_peers_json(args.roles)
    copied, skipped = mirror(args.dry_run)
    print(f"copied={copied} skipped(fresher dst)={skipped}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
