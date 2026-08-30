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
    checks: str  # success | failure | cancelled | pending | none
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
class MergedPull:
    """A pull request that landed. Immutable once merged, unlike an open one."""

    number: int
    title: str
    author: str
    merged_at: float
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

    # Default-branch protection. None means "GitHub would not tell us" - the
    # protection endpoint needs admin on the repo, and a token without it gets
    # the same 404 as a genuinely unprotected branch. Reporting that as
    # "unprotected" would invent a finding; the dashboard shows it as unknown.
    protected: bool | None = None
    required_reviews: int = 0
    allows_force_push: bool = False

    # Open Dependabot alerts by severity. `alerts_enabled` is False when the
    # feature is off for the repo, which GitHub reports as a 404 - the same
    # status as "no alerts". Kept apart because "0 open alerts" and "nobody is
    # looking" are opposite facts and must not render as the same green tile.
    alerts_enabled: bool = False
    alerts: tuple[tuple[str, int], ...] = ()

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

    # Line coverage from the newest coverage-report artifact CI built on the
    # default branch, and that artifact's id. None means the repo publishes no
    # such artifact, or the newest one could not be read - not that it has no
    # tests. The id is what lets a refresh skip re-downloading an unchanged
    # report; see github.collect.
    coverage: float | None = None
    # Lines CI actually measured. The percentage is not interpretable without
    # it - 100% of 176 lines and 100% of 3878 are different assurances.
    coverage_lines: int = 0
    coverage_artifact: int = 0

    workflows: tuple[WorkflowRun, ...] = ()
    open_issues: int = 0
    # Open PRs as GitHub counts them, before max_prs_per_repo clipping. `pulls`
    # may hold fewer, so the tile stays right even on a repo with a PR flood.
    open_pulls_total: int = 0
    pulls: tuple[PullRequest, ...] = ()
    merged: tuple[MergedPull, ...] = ()


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
    head_sha: str = ""
    default_branch_sha: str = ""
    rhiza_ref: str = ""

    # -- measurements ----------------------------------------------------
    # Everything below is carried over from the previous scan unless the clone
    # moved; `fingerprint` is what "moved" means and `measured_at` caps how
    # long a carried-over value may stand. See localgit._fingerprint.
    #
    # Line counts describe the working copy, commit counts describe the
    # default branch. That is deliberate rather than sloppy: lines are what is
    # on disk right now, including work you have not committed, while a clone
    # parked on a feature branch should still report what the *repo* has been
    # doing rather than what that branch has.
    code_lines: int = 0
    test_lines: int = 0
    commits_30d: int = 0
    # None when the clone has no tags at all. Zero would read as "everything is
    # released", which is the opposite of "nothing has ever been released".
    commits_since_release: int | None = None
    last_release: str = ""
    fingerprint: str = ""
    measured_at: float = 0.0


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
