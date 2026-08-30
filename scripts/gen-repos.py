#!/usr/bin/env python3
"""Turn repos.yml into the two environment lines the collector reads.

The collector never searches for checkouts. This script resolves every listed
path, asks its origin remote who it is, and prints:

    JQ_REPOS        the fleet, as owner/name - read by both halves
    JQ_REPO_PATHS   where each checkout actually is - read by the local half

    python3 scripts/gen-repos.py

Nothing is written. scripts/collector.sh runs this at every launch and exports
the result, so repos.yml stays the only place the fleet and the layout are
written down - there is no generated file in between to go stale.

The paths line exists because a repos.yml entry like

    - path: ~/repos/tschm/rhiza_projects/cs     # a checkout of tschm/cs

cannot be recovered by joining owner to name. This used to be papered over by
bind-mounting every checkout onto /repos/<owner>/<name>, back when the collector
ran in a container; it does not, so the real path has to be carried.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "repos.yml"


def fail(message: str) -> None:
    print(f"gen-repos: {message}", file=sys.stderr)
    sys.exit(1)


def origin_owner_name(path: Path) -> tuple[str, str] | None:
    """The ``(owner, name)`` a checkout's origin points at."""
    try:
        url = subprocess.run(
            ["git", "--no-optional-locks", "-C", str(path), "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        ).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return None
    if not url:
        return None
    url = url.removesuffix(".git")
    if url.startswith("git@"):
        tail = url.partition(":")[2]
    elif "://" in url:
        tail = url.split("://", 1)[1].split("/", 1)[-1]
    else:
        tail = url
    parts = [p for p in tail.split("/") if p]
    return (parts[-2], parts[-1]) if len(parts) >= 2 else None


def load_entries() -> list[dict]:
    if not SOURCE.exists():
        fail(f"{SOURCE.name} not found - copy repos.example.yml to repos.yml and list your repos")
    try:
        import yaml
    except ModuleNotFoundError:
        fail(
            "PyYAML is not installed - `uv run --with pyyaml scripts/gen-repos.py`, or pip install pyyaml"
        )
    data = yaml.safe_load(SOURCE.read_text(encoding="utf-8")) or {}
    entries = data.get("repos") if isinstance(data, dict) else None
    if not isinstance(entries, list) or not entries:
        fail(f"{SOURCE.name} lists no repos under a top-level `repos:` key")
    return entries


def resolve(entry: object, index: int) -> tuple[str, Path | None]:
    """One entry -> ``(owner/name, checkout path or None)``."""
    # `- ~/repos/foo` is accepted as shorthand for `- path: ~/repos/foo`.
    if isinstance(entry, str):
        entry = {"path": entry}
    if not isinstance(entry, dict):
        fail(f"entry {index} is neither a path nor a mapping: {entry!r}")

    named = str(entry.get("repo") or "").strip()
    raw_path = entry.get("path")

    if raw_path is None:
        if "/" not in named:
            fail(f"entry {index} needs a `path`, or a `repo:` of the form owner/name")
        return named, None

    # Relative paths are relative to this repo, so a checked-in repos.yml on a
    # colleague's machine means the same thing as it does here.
    path = Path(os.path.expanduser(str(raw_path)))
    path = (path if path.is_absolute() else ROOT / path).resolve()

    if not path.is_dir():
        fail(f"{raw_path} is not a directory")
    # `.git` is a directory in a plain checkout and a file in a worktree.
    if not (path / ".git").exists():
        fail(f"{path} is not a git checkout")

    if "/" in named:
        return named, path

    origin = origin_owner_name(path)
    if origin is None:
        fail(f"{path} has no usable origin remote - add an explicit `repo: owner/name`")
    return f"{origin[0]}/{origin[1]}", path


def main() -> None:
    unknown = sys.argv[1:]
    if unknown:
        fail(f"unknown argument {unknown[0]} - this script takes none")

    resolved: dict[str, Path | None] = {}
    for index, entry in enumerate(load_entries(), start=1):
        full_name, path = resolve(entry, index)
        if full_name in resolved:
            fail(f"{full_name} is listed twice")
        resolved[full_name] = path

    # Nothing but the lines themselves on stdout, so they can be exported,
    # piped, or appended to a .env.
    print(f'JQ_REPOS={",".join(resolved)}')

    paths = {name: path for name, path in resolved.items() if path is not None}
    # A comma is the separator, so a path containing one cannot be expressed.
    # Refuse rather than print a line the collector would reject or, worse,
    # silently misread as two repos.
    for name, path in paths.items():
        if "," in str(path):
            fail(f"{name}: path contains a comma, which JQ_REPO_PATHS cannot express: {path}")
    if paths:
        joined = ",".join(f"{name}={path}" for name, path in paths.items())
        print(f"JQ_REPO_PATHS={joined}")


if __name__ == "__main__":
    main()
