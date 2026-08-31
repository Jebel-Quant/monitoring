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
from typing import Any

from . import origin

log = logging.getLogger(__name__)

KNOWN_FORGES = (origin.GITHUB, origin.GITLAB)


class FleetError(Exception):
    """``repos.yml`` says something the collector cannot act on."""


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


def _declared_forge(item: dict, index: int) -> str | None:
    """The entry's explicit ``forge:``, validated, or None if it has none.

    An unknown value is refused rather than defaulted. Silently reading
    ``forge: gitbucket`` as GitHub would put a repo on the board with every
    remote panel wrong and nothing saying why.
    """
    declared = str(item.get("forge") or "").strip().lower()
    if not declared:
        return None
    if declared not in KNOWN_FORGES:
        raise FleetError(
            f"entry {index}: forge {declared!r} is not one of {', '.join(KNOWN_FORGES)}"
        )
    return declared


def _entry(item: Any, index: int, host_root: str) -> tuple[str, str | None, str]:
    """One entry -> ``(namespace/name, checkout path or None, forge)``."""
    # `- ~/repos/foo` is accepted as shorthand for `- path: ~/repos/foo`.
    if isinstance(item, str):
        item = {"path": item}
    if not isinstance(item, dict):
        raise FleetError(f"entry {index} is neither a path nor a mapping: {item!r}")

    named = str(item.get("repo") or "").strip()
    raw_path = item.get("path")
    declared = _declared_forge(item, index)

    if raw_path is None:
        # Name the offending value, not just the rule. "entry 7" in a
        # twenty-five entry file means counting; the value is searchable.
        if not named:
            raise FleetError(
                f"entry {index} ({item!r}) has neither a `path` nor a `repo: owner/name`"
            )
        if "/" not in named:
            raise FleetError(f"entry {index}: repo {named!r} is not of the form owner/name")
        # No checkout means no origin to infer from, so an entry on any forge
        # but the default has to say so itself.
        return named, None, declared or origin.GITHUB

    path = resolve_path(str(raw_path), host_root)

    if not _is_checkout(path):
        # Not an error: the home directory may not be mounted, or this repo may
        # simply not be checked out here. Either way the fleet keeps the repo
        # and only the working-copy panels go quiet - but say so once, because
        # a typo in repos.yml looks exactly like this from here.
        if named and "/" in named:
            log.warning("no checkout for %s at %s - remote panels only", named, path)
            return named, None, declared or origin.GITHUB
        raise FleetError(
            f"entry {index}: {raw_path} is not a git checkout (looked in {path}). "
            "Mount the home directory it lives under, or give the entry a "
            "`repo: owner/name` so it can be monitored without one."
        )

    parsed = origin.read(path)

    if "/" in named:
        # An explicit `repo:` overrides the origin - that is what it is for, on
        # a fork - but the origin's host is still the best evidence of which
        # forge the repo lives on when the entry did not say.
        return named, path, declared or (parsed.forge if parsed else origin.GITHUB)

    if parsed is None:
        raise FleetError(
            f"entry {index}: {path} has no usable origin remote - add an explicit `repo: owner/name`"
        )
    return parsed.full_name, path, declared or parsed.forge


def load(
    source: str, host_root: str = ""
) -> tuple[tuple[str, ...], dict[str, str], dict[str, str]]:
    """Read ``repos.yml`` into ``(fleet, checkout paths, forge per repo)``.

    The fleet is every listed repo as ``namespace/name``; the paths map holds
    only those with a checkout this machine can actually read; the forge map
    says which API each one is read through.
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
    forges: dict[str, str] = {}
    for index, item in enumerate(entries, start=1):
        full_name, path, forge = _entry(item, index, host_root)
        if full_name in fleet:
            # Two forges can host the same `namespace/name`, and the board's
            # whole label scheme is that one `repo` value is one repo. Refusing
            # is the same choice the duplicate case has always made: a merged
            # pair would report one repo's CI under the other's name, and read
            # as a working board while doing it.
            clash = forges[full_name]
            detail = f" - on {clash} and on {forge}" if clash != forge else ""
            raise FleetError(f"{full_name} is listed twice{detail}")
        fleet.append(full_name)
        forges[full_name] = forge
        if path is not None:
            paths[full_name] = path

    return tuple(fleet), paths, forges
