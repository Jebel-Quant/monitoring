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

    Repos arrive two ways: whole-org sweeps (``JQ_ORGS``) and individually named
    repos (``JQ_REPOS``). The second exists because you rarely want *all* of a
    large shared org - cvxgrp has 100+ repos and only a handful are yours.
    """

    orgs: tuple[str, ...] = field(default_factory=lambda: _csv("JQ_ORGS", ("Jebel-Quant",)))
    extra_repos: tuple[str, ...] = field(default_factory=lambda: _csv("JQ_REPOS"))

    token: str = os.environ.get("GITHUB_TOKEN", "")
    api: str = os.environ.get("GITHUB_API", "https://api.github.com")

    # Where the clones are mounted (read-only) inside the container. Scanned to
    # a depth of two, so both ~/repos/<repo> and ~/repos/<org>/<repo> are found.
    repo_root: str = os.environ.get("JQ_REPO_ROOT", "/repos")
    scan_depth: int = _int("JQ_SCAN_DEPTH", 2)

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

    # Repos to leave out, as bare names or as owner/name.
    ignore: tuple[str, ...] = field(default_factory=lambda: _csv("JQ_IGNORE"))

    include_archived: bool = os.environ.get("JQ_INCLUDE_ARCHIVED", "false").lower() == "true"

    # Drop private repos entirely - not just their details, but their existence.
    # For a board served world-readable, a private repo's name, its workflow
    # names, its PR titles and its local branch names are all disclosure.
    public_only: bool = os.environ.get("JQ_PUBLIC_ONLY", "false").lower() == "true"

    @property
    def owners(self) -> frozenset[str]:
        """Every owner we might accept a clone from, lowercased."""
        from_extras = (r.split("/", 1)[0] for r in self.extra_repos if "/" in r)
        return frozenset(o.lower() for o in (*self.orgs, *from_extras))

    def is_ignored(self, owner: str, name: str) -> bool:
        return name in self.ignore or f"{owner}/{name}" in self.ignore

    def wants(self, owner: str, name: str) -> bool:
        """Is this repo in scope - by its org, or by being named explicitly?"""
        if self.is_ignored(owner, name):
            return False
        if owner.lower() in {o.lower() for o in self.orgs}:
            return True
        return f"{owner}/{name}".lower() in {r.lower() for r in self.extra_repos}
