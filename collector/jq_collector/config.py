"""Runtime configuration, read once from the environment."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return int(raw)


def _csv(name: str, default: tuple[str, ...] = ()) -> tuple[str, ...]:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return tuple(part.strip() for part in raw.split(",") if part.strip())


@dataclass(frozen=True)
class Config:
    """Everything the collector needs to know about its environment.

    The fleet is an explicit list: ``JQ_REPOS`` names every monitored repo as
    ``owner/name``, and nothing else is ever gathered. There used to be a
    whole-org sweep as well, which meant the board's contents were decided by
    GitHub rather than by you - a new repo in the org appeared unasked, and a
    shared org like cvxgrp dragged in a hundred repos that were not yours.

    On a laptop the list is generated from ``repos.yml`` by
    ``scripts/gen-repos.py``, which also mounts each checkout at
    ``$JQ_REPO_ROOT/<owner>/<name>``. Setting it by hand works too, and then
    nothing mounts the checkouts. Both halves read the same list, so the GitHub
    panels and the working-copy panels can never disagree about who is in the
    fleet.
    """

    repos: tuple[str, ...] = field(default_factory=lambda: _csv("JQ_REPOS"))

    token: str = os.environ.get("GITHUB_TOKEN", "")
    api: str = os.environ.get("GITHUB_API", "https://api.github.com")

    # Where the checkouts are mounted (read-only) inside the container, one per
    # repo at <repo_root>/<owner>/<name>. Set empty to skip local scanning
    # entirely, which is the right setting anywhere there are no working copies
    # to report on - the local panels then simply have nothing to say.
    repo_root: str = os.environ.get("JQ_REPO_ROOT", "/repos")

    # owner/name of the repo whose releases define "up to date".
    template_repo: str = os.environ.get("JQ_TEMPLATE_REPO", "Jebel-Quant/rhiza")
    template_pointer: str = os.environ.get("JQ_TEMPLATE_POINTER", ".rhiza/template.yml")

    listen_port: int = _int("JQ_LISTEN_PORT", 9109)

    # GitHub is rate limited and slow; the local filesystem is neither.
    github_interval: int = _int("JQ_GITHUB_INTERVAL", 300)
    local_interval: int = _int("JQ_LOCAL_INTERVAL", 60)

    http_timeout: int = _int("JQ_HTTP_TIMEOUT", 20)

    # Open PRs are enumerated per repo; each one costs a further call for its
    # check runs. Cap it so a runaway repo cannot exhaust the hourly budget.
    max_prs_per_repo: int = _int("JQ_MAX_PRS_PER_REPO", 20)

    # Merged pull requests kept per repo. The board shows the newest N across
    # the whole fleet, so this must be at least that N or a burst in one repo
    # could crowd out entries the fleet-wide list should have shown.
    recent_merges_per_repo: int = _int("JQ_RECENT_MERGES_PER_REPO", 10)

    # Repos to leave out, as bare names or as owner/name. Redundant now that
    # the fleet is an explicit list - deleting the line from repos.yml is the
    # obvious move - but it stays for the server, where the list is an env var
    # and commenting one entry out is not possible.
    ignore: tuple[str, ...] = field(default_factory=lambda: _csv("JQ_IGNORE"))

    include_archived: bool = os.environ.get("JQ_INCLUDE_ARCHIVED", "false").lower() == "true"

    # Drop private repos entirely - not just their details, but their existence.
    # The board binds to loopback, so this is not what keeps a private repo off
    # the network; it is for when you would rather it were never fetched, since
    # its name, workflow names, PR titles and branch names are all disclosure.
    public_only: bool = os.environ.get("JQ_PUBLIC_ONLY", "false").lower() == "true"

    def is_ignored(self, owner: str, name: str) -> bool:
        return name in self.ignore or f"{owner}/{name}" in self.ignore
