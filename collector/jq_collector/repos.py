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

One entry may also name a ``folder:``, and then every checkout sitting directly
inside it is on the board. That is still the list deciding membership - the
folder is named here, one level is scanned and no deeper, and a repo joins
because a folder you wrote down holds it. It is not the old whole-root walk,
which took any checkout anywhere under one mount whose origin looked plausible.
Cloning a repo into a listed folder does add it to the board at the next
restart, which is the point: an org you keep whole is one line instead of
twenty, and the twenty cannot drift out of step with the disk.
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


def _folder(item: dict, index: int, host_root: str, claimed: frozenset[str]) -> list[dict]:
    """A ``folder:`` entry -> one ``path:`` entry per checkout inside it.

    Only the folder's own children are looked at, never their children in turn.
    A folder is a statement about where you keep a set of repos, and one level
    is what that means; recursing would make ``folder: ~`` a whole-disk walk,
    which is the discovery this file exists to have got rid of.

    A checkout some other entry names by ``path`` is left to that entry -
    ``claimed`` is every such path in the file. That is how one repo inside a
    listed folder gets an override: the folder covers the rest, and the entry
    written out for that one checkout is the only entry for it. Matching on the
    path rather than on ``namespace/name`` is what makes a ``repo:`` override
    work, since the whole point of one is that the name comes out different.

    A folder that is not there is refused, exactly as an unreachable ``path``
    is, and so is one holding no checkouts at all. Both look identical to a
    fleet that is quietly short a folder's worth of repos, and the point of
    naming the folder was to get those repos on the board.
    """
    raw = str(item.get("folder") or "").strip()
    for key in ("path", "repo"):
        if item.get(key):
            raise FleetError(
                f"entry {index}: `folder: {raw}` cannot also carry a `{key}` - "
                "a folder stands for however many repos are in it, so there is "
                "no one path or name to give it. List the repo on its own entry."
            )
    # Validated here so a bad `forge:` on the folder is refused once, against
    # the line that was actually written, rather than per checkout found.
    forge = _declared_forge(item, index)

    folder = resolve_path(raw, host_root)
    try:
        children = sorted(entry.path for entry in os.scandir(folder) if entry.is_dir())
    except OSError as exc:
        raise FleetError(
            f"entry {index}: cannot read folder {raw} (looked in {folder}): {exc}. "
            "Mount the home directory it lives under, or list the repos in it "
            "one by one."
        ) from exc

    checkouts = [child for child in children if _is_checkout(child)]
    if not checkouts:
        raise FleetError(
            f"entry {index}: folder {raw} holds no git checkouts (looked in {folder}). "
            "A folder is scanned one level deep, so name the folder the "
            "checkouts are directly in."
        )
    # The emptiness check is on what is in the folder, not on what is left
    # after the claimed ones go: a folder whose every checkout has an entry of
    # its own is a redundant line, not a mistake worth refusing to start over.
    taken = [child for child in checkouts if child not in claimed]
    if len(taken) == len(checkouts):
        log.info("folder %s: %d checkouts", raw, len(taken))
    else:
        log.info(
            "folder %s: %d checkouts, %d left to an entry of their own",
            raw,
            len(taken),
            len(checkouts) - len(taken),
        )
    return [{"path": child, "forge": forge} for child in taken]


def _expand(
    item: Any, index: int, host_root: str, claimed: frozenset[str]
) -> list[tuple[Any, bool]]:
    """One config entry -> the entries to read, and whether each was swept up.

    Everything is one entry, written outright, except a ``folder:`` - which is
    however many checkouts are in it. The flag is what lets an entry named
    outright override one a folder found, instead of colliding with it.
    """
    if isinstance(item, dict) and item.get("folder"):
        return [(found, True) for found in _folder(item, index, host_root, claimed)]
    return [(item, False)]


def _claimed_paths(entries: list, host_root: str) -> frozenset[str]:
    """Every checkout the file names by ``path``, resolved for this filesystem.

    A folder skips these, so an entry written out for one checkout inside a
    listed folder is that checkout's only entry.
    """
    claimed = set()
    for item in entries:
        if isinstance(item, str):
            claimed.add(resolve_path(item, host_root))
        elif isinstance(item, dict) and item.get("path"):
            claimed.add(resolve_path(str(item["path"]), host_root))
    return frozenset(claimed)


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
    # Named outright rather than swept up by a folder, which is what decides
    # who wins when both describe the same repo.
    named: set[str] = set()
    claimed = _claimed_paths(entries, host_root)
    for index, item in enumerate(entries, start=1):
        for entry, swept in _expand(item, index, host_root, claimed):
            try:
                full_name, path, forge = _entry(entry, index, host_root)
            except FleetError as exc:
                if not swept:
                    raise
                # A checkout with no usable origin is somebody's scratch clone
                # sitting in the folder. Refusing would take the whole board
                # down over a repo nobody asked to monitor; a folder entry is
                # not the deliberate statement that a listed path is.
                log.warning("skipping %s: %s", entry["path"], exc)
                continue

            # Exactly one of the two entries names the repo outright: a folder
            # overlapping an entry, which is not the duplicate case below.
            overlap = (not swept) != (full_name in named)
            if full_name in forges and overlap:
                # One entry names this repo outright and the other is a folder
                # that swept it up - `- repo: org/x` next to the folder org/x
                # is checked out in. The entry written for the repo itself is
                # the deliberate statement, so it decides the name and the
                # forge whichever order the two were written in; the folder can
                # still supply the checkout path, since that is where the repo
                # is on disk and the other entry may not have said.
                #
                # (A folder never reaches here for a checkout some entry names
                # by `path` - it skips those outright, which is what lets a
                # `repo:` override rename one repo inside a listed folder.)
                log.info("%s: named outright, so the folder does not list it too", full_name)
                if not swept:
                    named.add(full_name)
                    forges[full_name] = forge
                if path is not None:
                    paths[full_name] = path if not swept else paths.get(full_name, path)
                continue

            if full_name in forges:
                # Two forges can host the same `namespace/name`, and the board's
                # whole label scheme is that one `repo` value is one repo. Refusing
                # is the same choice the duplicate case has always made: a merged
                # pair would report one repo's CI under the other's name, and read
                # as a working board while doing it.
                clash, first = forges[full_name], paths.get(full_name)
                if clash != forge:
                    detail = f" - on {clash} and on {forge}"
                elif first and path and first != path:
                    detail = f" - checked out at {first} and at {path}"
                else:
                    detail = ""
                raise FleetError(f"{full_name} is listed twice{detail}")

            fleet.append(full_name)
            forges[full_name] = forge
            if not swept:
                named.add(full_name)
            if path is not None:
                paths[full_name] = path

    if not fleet:
        # Reachable only when every checkout a folder turned up was skipped for
        # want of an origin: the file said something, and none of it survived.
        raise FleetError(f"{source} named no repo the collector could identify")

    return tuple(fleet), paths, forges
