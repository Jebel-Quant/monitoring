"""The snapshot the exporter serves from.

Two background refreshers write here on their own cadences; ``/metrics`` reads a
consistent copy. Keeping the two sources in separate maps means a GitHub outage
cannot blank out the local-clone panels, and vice versa.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field, replace


@dataclass(frozen=True)
class PullRequest:
    number: int
    title: str
    author: str
    draft: bool
    created_at: float
    updated_at: float
    checks: str  # success | failure | pending | none
    url: str


@dataclass(frozen=True)
class WorkflowRun:
    """The latest completed run of one workflow on the default branch."""

    name: str
    conclusion: str
    finished_at: float
    duration: float
    url: str


@dataclass(frozen=True)
class RemoteRepo:
    """What GitHub says about a repo."""

    name: str
    owner: str = ""
    default_branch: str = "main"
    visibility: str = "unknown"
    archived: bool = False
    head_sha: str = ""
    pushed_at: float = 0.0

    rhiza_managed: bool = False
    rhiza_ref: str = ""
    # None when the pinned ref is not a published release (a branch or a sha),
    # so the dashboard can say "unknown" rather than imply "up to date".
    rhiza_behind: int | None = None

    # The representative run for the repo: the failing workflow when there is
    # one, otherwise the most recently finished. A repo usually has several
    # workflows on its default branch and any one of them being red makes the
    # branch red, so a single "latest run" cannot describe it.
    ci_conclusion: str = ""  # success | failure | cancelled | ... | "" if never run
    ci_workflow: str = ""
    ci_finished_at: float = 0.0
    ci_duration: float = 0.0
    ci_url: str = ""

    workflows: tuple[WorkflowRun, ...] = ()
    open_issues: int = 0
    # Open PRs as GitHub counts them, before max_prs_per_repo clipping. `pulls`
    # may hold fewer, so the tile stays right even on a repo with a PR flood.
    open_pulls_total: int = 0
    pulls: tuple[PullRequest, ...] = ()


@dataclass(frozen=True)
class LocalRepo:
    """What the working copy on this machine says about a repo."""

    name: str
    path: str
    owner: str = ""
    branch: str = ""
    dirty_files: int = 0
    untracked_files: int = 0
    # None when the branch has no upstream configured.
    ahead: int | None = None
    behind: int | None = None
    stashes: int = 0
    last_commit_at: float = 0.0
    fetch_age: float | None = None
    default_branch_sha: str = ""
    rhiza_ref: str = ""


@dataclass(frozen=True)
class SourceHealth:
    last_success: float = 0.0
    last_duration: float = 0.0
    errors: int = 0
    last_error: str = ""


@dataclass(frozen=True)
class Snapshot:
    """Both repo maps are keyed by ``owner/name``.

    A bare name is only unique within one owner; once more than one org is in
    scope, keying on it would silently merge two different repos into one set of
    series.
    """

    remote: dict[str, RemoteRepo] = field(default_factory=dict)
    local: dict[str, LocalRepo] = field(default_factory=dict)
    # Repos deliberately dropped - archived on GitHub, or named in JQ_IGNORE.
    # Held separately because the local scan cannot work it out: a clone of a
    # dropped repo looks like any other clone, and without this it would keep
    # the repo on the board after the GitHub half had correctly stopped
    # reporting it. Distinct from RemoteRepo.archived, which is one repo's flag.
    excluded: frozenset[str] = frozenset()
    health: dict[str, SourceHealth] = field(default_factory=dict)
    rate_limit_remaining: float = -1.0
    rate_limit_limit: float = -1.0
    rate_limit_reset: float = 0.0
    latest_template_ref: str = ""


class Store:
    """Thread-safe holder for the current snapshot."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._snapshot = Snapshot()

    def snapshot(self) -> Snapshot:
        with self._lock:
            return self._snapshot

    def update(self, **changes: object) -> None:
        with self._lock:
            self._snapshot = replace(self._snapshot, **changes)  # type: ignore[arg-type]

    def record_health(self, source: str, health: SourceHealth) -> None:
        with self._lock:
            health_map = dict(self._snapshot.health)
            health_map[source] = health
            self._snapshot = replace(self._snapshot, health=health_map)

    def health_for(self, source: str) -> SourceHealth:
        return self.snapshot().health.get(source, SourceHealth())
