"""The fleet, read from ``repos.yml``.

One file names every monitored repo and, where there is one, the checkout on
disk. It is read at startup and nothing is generated from it: there is no
second file, and no environment round-trip, that could fall out of step.

Paths are written the way you would write them on your own machine - ``~/repos/
jebel-quant/rhiza``. Inside the container that home directory is a single
read-only bind mount, so ``JQ_HOST_ROOT`` (``/host`` in the image, unset when
the collector runs natively) is what ``~`` expands to. One mount covers the
whole fleet, which is what makes a checkout at an arbitrary path expressible
here at all - the old per-repo bind mounts could only name
``<root>/<owner>/<name>``.

A path that is not reachable is not fatal. It means the home directory was not
mounted, or that repo is not checked out here: the GitHub panels still report
on it and the working-copy panels have nothing to say. That is the same shape
as an entry with no ``path`` at all.
"""

from __future__ import annotations

import logging
import os
import subprocess
from typing import Any

log = logging.getLogger(__name__)


class FleetError(Exception):
    """``repos.yml`` says something the collector cannot act on."""


def _origin_owner_name(path: str) -> tuple[str, str] | None:
    """The ``(owner, name)`` a checkout's origin points at."""
    try:
        url = subprocess.run(
            ["git", "--no-optional-locks", "-C", path, "remote", "get-url", "origin"],
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


def resolve_path(raw: str, host_root: str) -> str:
    """Where a ``repos.yml`` path lands on *this* filesystem.

    ``~/x`` and the relative ``x`` both hang off ``host_root`` when it is set,
    because that is the mount point of the home directory they were written
    against. An absolute path is tried as written first - it is right when the
    collector runs natively - and only then under the mount, which is what a
    whole-root mount (``-v /:/host:ro``) makes work.
    """
    raw = raw.strip()
    if not host_root:
        return os.path.abspath(os.path.expanduser(raw))

    if raw.startswith("~"):
        under = raw.removeprefix("~").lstrip("/")
    elif not os.path.isabs(raw):
        under = raw
    elif os.path.exists(raw):
        return raw
    else:
        under = raw.lstrip("/")
    # normpath so a bare `~` gives /host and not /host/ - a trailing slash is
    # harmless to open() but it reaches the dashboard as a repo's `path` label.
    return os.path.normpath(os.path.join(host_root, under))


def _is_checkout(path: str) -> bool:
    # `.git` is a directory in a plain checkout and a file in a worktree.
    return os.path.exists(os.path.join(path, ".git"))


def _entry(item: Any, index: int, host_root: str) -> tuple[str, str | None]:
    """One entry -> ``(owner/name, checkout path or None)``."""
    # `- ~/repos/foo` is accepted as shorthand for `- path: ~/repos/foo`.
    if isinstance(item, str):
        item = {"path": item}
    if not isinstance(item, dict):
        raise FleetError(f"entry {index} is neither a path nor a mapping: {item!r}")

    named = str(item.get("repo") or "").strip()
    raw_path = item.get("path")

    if raw_path is None:
        if "/" not in named:
            raise FleetError(f"entry {index} needs a `path`, or a `repo:` of the form owner/name")
        return named, None

    path = resolve_path(str(raw_path), host_root)

    if not _is_checkout(path):
        # Not an error: the home directory may not be mounted, or this repo may
        # simply not be checked out here. Either way the fleet keeps the repo
        # and only the working-copy panels go quiet - but say so once, because
        # a typo in repos.yml looks exactly like this from here.
        if named and "/" in named:
            log.warning("no checkout for %s at %s - GitHub panels only", named, path)
            return named, None
        raise FleetError(
            f"entry {index}: {raw_path} is not a git checkout (looked in {path}). "
            "Mount the home directory it lives under, or give the entry a "
            "`repo: owner/name` so it can be monitored without one."
        )

    if "/" in named:
        return named, path

    origin = _origin_owner_name(path)
    if origin is None:
        raise FleetError(
            f"entry {index}: {path} has no usable origin remote - add an explicit `repo: owner/name`"
        )
    return f"{origin[0]}/{origin[1]}", path


def load(source: str, host_root: str = "") -> tuple[tuple[str, ...], dict[str, str]]:
    """Read ``repos.yml`` into ``(fleet, checkout paths)``.

    The fleet is every listed repo as ``owner/name``; the paths map holds only
    those with a checkout this machine can actually read.
    """
    import yaml

    try:
        with open(source, encoding="utf-8") as handle:
            text = handle.read()
    except OSError as exc:
        raise FleetError(f"cannot read {source}: {exc}") from exc

    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise FleetError(f"{source} is not valid YAML: {exc}") from exc

    entries = data.get("repos") if isinstance(data, dict) else None
    if not isinstance(entries, list) or not entries:
        raise FleetError(f"{source} lists no repos under a top-level `repos:` key")

    fleet: list[str] = []
    paths: dict[str, str] = {}
    for index, item in enumerate(entries, start=1):
        full_name, path = _entry(item, index, host_root)
        if full_name in fleet:
            raise FleetError(f"{full_name} is listed twice")
        fleet.append(full_name)
        if path is not None:
            paths[full_name] = path

    return tuple(fleet), paths
