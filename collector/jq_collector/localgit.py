"""Read the state of the local working copies.

Every git invocation goes through ``--no-optional-locks`` and the clones are
mounted read-only: the collector must never be the reason a repo grows an
``index.lock`` or a refreshed index while a session is mid-rebase.

Nothing here fetches. ``ahead``/``behind`` are therefore only as fresh as the
last fetch *you* ran, which is why ``fetch_age`` is reported alongside them; for
a fetch-independent answer the exporter compares the local default-branch sha
with the one GitHub reports.

Nothing here searches for checkouts either. Each monitored repo is expected at
``<repo_root>/<owner>/<name>``, which is exactly where the generated compose
override mounts it. The fleet is decided by ``repos.yml``, not by whatever
happened to be lying around under a scanned directory.
"""

from __future__ import annotations

import logging
import os
import subprocess
import time

from .config import Config
from .state import LocalRepo

log = logging.getLogger(__name__)

_TIMEOUT = 20


def _git(path: str, *args: str) -> str | None:
    """Run a read-only git command, returning None if it fails."""
    try:
        result = subprocess.run(
            ["git", "--no-optional-locks", "-C", path, *args],
            capture_output=True,
            text=True,
            timeout=_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warning("git %s in %s failed: %s", " ".join(args), path, exc)
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def origin_owner_name(path: str) -> tuple[str, str] | None:
    """The ``(owner, name)`` this clone points at, or None if there is no origin."""
    url = _git(path, "remote", "get-url", "origin")
    if not url:
        return None
    url = url.removesuffix(".git")
    if url.startswith("git@"):
        _, _, tail = url.partition(":")
    elif "://" in url:
        tail = url.split("://", 1)[1].split("/", 1)[-1]
    else:
        tail = url
    parts = [p for p in tail.split("/") if p]
    if len(parts) < 2:
        return None
    return parts[-2], parts[-1]


def _template_ref(path: str, pointer: str) -> str:
    """The ref pinned in the repo's template pointer, if it is managed."""
    full = os.path.join(path, pointer)
    if not os.path.isfile(full):
        return ""
    try:
        import yaml

        with open(full, encoding="utf-8") as handle:
            data = yaml.safe_load(handle) or {}
    except Exception as exc:  # noqa: BLE001 - a malformed pointer is data, not a crash
        log.warning("could not parse %s: %s", full, exc)
        return ""
    ref = data.get("ref") if isinstance(data, dict) else None
    return str(ref).strip() if ref else ""


def _fetch_age(path: str) -> float | None:
    """Seconds since the last fetch, from FETCH_HEAD's mtime."""
    git_dir = _git(path, "rev-parse", "--git-dir")
    if not git_dir:
        return None
    if not os.path.isabs(git_dir):
        git_dir = os.path.join(path, git_dir)
    fetch_head = os.path.join(git_dir, "FETCH_HEAD")
    try:
        return max(0.0, time.time() - os.path.getmtime(fetch_head))
    except OSError:
        return None


def _ahead_behind(path: str) -> tuple[int | None, int | None]:
    counts = _git(path, "rev-list", "--left-right", "--count", "@{upstream}...HEAD")
    if not counts:
        return None, None
    parts = counts.split()
    if len(parts) != 2:
        return None, None
    try:
        return int(parts[1]), int(parts[0])  # ahead, behind
    except ValueError:
        return None, None


def scan_repo(name: str, owner: str, path: str, cfg: Config) -> LocalRepo:
    status = _git(path, "status", "--porcelain=v1") or ""
    lines = [line for line in status.splitlines() if line.strip()]
    untracked = sum(1 for line in lines if line.startswith("??"))
    ahead, behind = _ahead_behind(path)

    last_commit = _git(path, "log", "-1", "--format=%ct") or ""
    stashes = _git(path, "stash", "list") or ""

    branch = _git(path, "rev-parse", "--abbrev-ref", "HEAD") or ""
    # A detached HEAD reports "HEAD"; name the commit instead so the dashboard
    # shows something actionable.
    if branch == "HEAD":
        short = _git(path, "rev-parse", "--short", "HEAD") or "unknown"
        branch = f"detached@{short}"

    return LocalRepo(
        name=name,
        owner=owner,
        path=path,
        branch=branch,
        dirty_files=len(lines) - untracked,
        untracked_files=untracked,
        ahead=ahead,
        behind=behind,
        stashes=len([s for s in stashes.splitlines() if s.strip()]),
        last_commit_at=float(last_commit) if last_commit.isdigit() else 0.0,
        fetch_age=_fetch_age(path),
        rhiza_ref=_template_ref(path, cfg.template_pointer),
    )


def scan(
    cfg: Config,
    default_branches: dict[str, str],
    skip: frozenset[str] = frozenset(),
) -> dict[str, LocalRepo]:
    """Read every listed repo's working copy, keyed by ``owner/name``.

    A repo with no checkout on this machine is not an error: the fleet is the
    list, and the local panels simply have nothing to say about that row.

    ``skip`` holds repos deliberately dropped - archived on GitHub, or named
    in JQ_IGNORE. Without it a checkout would keep a dropped repo on the board
    after the GitHub half had stopped reporting it.
    """
    found: dict[str, LocalRepo] = {}
    if not cfg.repo_root:
        # Deliberate: on a server there are no working copies to report on, and
        # an empty result is the honest answer rather than an error every minute.
        return found
    if not os.path.isdir(cfg.repo_root):
        log.error("repo root %s is not a directory", cfg.repo_root)
        return found

    for key in cfg.repos:
        if "/" not in key or key in skip:
            continue
        owner, repo_name = key.split("/", 1)
        path = os.path.join(cfg.repo_root, owner, repo_name)
        # `.git` is a directory in a plain checkout and a file in a worktree.
        if not os.path.exists(os.path.join(path, ".git")):
            # Not mounted, or mounted somewhere else. Normal on a server, and
            # normal for a repo you monitor but have not checked out.
            log.debug("no working copy for %s at %s", key, path)
            continue

        # The mount point claims to be this repo; the origin remote is the only
        # thing that can confirm it. A wrong path in repos.yml would otherwise
        # report one repo's dirty files under another repo's name.
        origin = origin_owner_name(path)
        if origin is not None and (origin[0].lower(), origin[1].lower()) != (
            owner.lower(),
            repo_name.lower(),
        ):
            log.warning(
                "%s is a checkout of %s/%s, not %s - check repos.yml",
                path,
                origin[0],
                origin[1],
                key,
            )
            continue

        local = scan_repo(repo_name, owner, path, cfg)
        default_branch = default_branches.get(key, "main")
        sha = _git(path, "rev-parse", "--verify", "--quiet", default_branch) or ""
        found[key] = LocalRepo(**{**local.__dict__, "default_branch_sha": sha})

    return found
