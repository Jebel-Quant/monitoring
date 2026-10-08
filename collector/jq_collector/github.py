"""Read the state of the repos as GitHub sees them.

Per refresh this costs roughly ``6 * repos + workflows + open_pull_requests``
REST calls (one branch, one protection, one alert listing, one workflow-run,
one artifact listing and one pull listing each, plus a check-run listing per
open PR). Measured on a fleet of 25: 370 calls in the steady state. There are
no conditional requests, so the cost scales with the fleet and does not fall
when nothing has changed - see JQ_GITHUB_INTERVAL before growing either.

The coverage artifact is the one response that is not JSON. Listing artifacts
happens every refresh so a newly published report is picked up, but the zip is
downloaded only when the artifact id has changed - measured 19 downloads on a
cold pass and 0 on the next.
``jq_github_rate_limit_remaining`` is exported so the headroom is visible
rather than assumed.

The REST client itself is in github_api.py; this module is the orchestration
on top of it - which repos, which calls, and how the answers become a
RemoteRepo.
"""

from __future__ import annotations

import logging
from typing import Any

from .config import Config
from .forge import (
    GOOD_CONCLUSIONS,
    INCONCLUSIVE_CONCLUSIONS,
    cached_ref,
    ci_summary,
    fan_out,
)
from .github_api import GitHub
from .github_payloads import ts as _ts
from .state import RemoteRepo, WorkflowRun

log = logging.getLogger(__name__)

# Both re-exported. They used to be defined here and again in metrics.py, and a
# second forge would have made three copies of one contract. They live in
# forge.py now, next to the table that translates GitLab's spelling into them.
# GitHub is re-exported too: collect() looks it up in this module, which is
# where callers and tests reach for it.
__all__ = ["GOOD_CONCLUSIONS", "INCONCLUSIVE_CONCLUSIONS", "GitHub", "collect"]

_MAX_WORKERS = 8


def _behind_count(tags: list[str], ref: str) -> int | None:
    """How many releases were published after ``ref``.

    Returns None when the pinned ref is not a published release tag - a branch
    name or a sha - so the dashboard can show "unknown" instead of "current".
    """
    if not ref or ref not in tags:
        return None
    return tags.index(ref)


def collect(
    cfg: Config,
    ref_cache: dict[str, tuple[str, str]],
    coverage_cache: dict[str, tuple[int, tuple[float, int] | None]] | None = None,
    fleet: tuple[str, ...] | None = None,
) -> tuple[dict[str, RemoteRepo], GitHub, list[str], frozenset[str]]:
    """Build the GitHub part of the snapshot, keyed by ``owner/name``.

    Returns the template repo's release tags along with the repos, because the
    GitLab collector needs the same list to measure its own repos' drift: the
    template lives on GitHub wherever the repo pinning it lives, so fetching the
    tags once here and passing them on keeps it to one call per refresh.

    ``fleet`` is this forge's share of the repos. It defaults to the whole
    fleet, which is right for a GitHub-only deployment and for every test.

    The template pointer is always read from GitHub's default branch, never from
    the local clone. Drift is a property of the *repo*, and a clone can be
    arbitrarily stale - reading it from disk made an un-pulled checkout look
    like an out-of-date repo.

    ``ref_cache`` maps ``full_name -> (default_branch_sha, ref)`` from the
    previous refresh. The pointer can only have changed if the branch head
    moved, so an unchanged sha skips the fetch and the steady-state cost of
    correctness is zero extra calls.

    ``coverage_cache`` maps ``full_name -> (artifact_id, (percent, lines))``. Listing
    the artifacts is one call per repo and always happens, so a report
    published between refreshes is picked up; *downloading* one only happens
    when the id has changed. That matters more than the call count - the
    download is a zip, by far the largest response this collector handles, and
    in the steady state it is never fetched at all.
    """
    coverage_cache = coverage_cache or {}
    api = GitHub(cfg)
    tags = api.release_tags(cfg.template_repo)

    # Dropped repos are reported back so the local scan can skip their clones
    # too - otherwise a checkout keeps a dead repo on the board after the GitHub
    # half has correctly stopped reporting it.
    listing = api.list_repos(fleet)
    excluded = frozenset(r["full_name"] for r in listing if _dropped(cfg, r))
    repos = [r for r in listing if r["full_name"] not in excluded]

    def one(raw: dict[str, Any]) -> RemoteRepo:
        return _remote_repo(api, cfg, raw, tags, ref_cache, coverage_cache)

    return fan_out(repos, "full_name", one, log, _MAX_WORKERS), api, tags, excluded


def _dropped(cfg: Config, raw: dict[str, Any]) -> bool:
    """Whether a listed repo is left off the board.

    JQ_IGNORE is applied here as well as in the local scan. It used to be
    honoured only by the scan, via Config.wants(), so ignoring a repo silently
    removed its working-copy rows while leaving every CI, drift and
    pull-request series in place.
    """
    if raw.get("archived") and not cfg.include_archived:
        return True
    if cfg.is_ignored(*raw["full_name"].split("/", 1)):
        return True
    # `visibility` covers private and internal; `private` is the older flag.
    return cfg.public_only and bool(raw.get("private") or raw.get("visibility") != "public")


def _remote_repo(
    api: GitHub,
    cfg: Config,
    raw: dict[str, Any],
    tags: list[str],
    ref_cache: dict[str, tuple[str, str]],
    coverage_cache: dict[str, tuple[int, tuple[float, int] | None]],
) -> RemoteRepo:
    """Everything the board shows about one repo, one call per question."""
    full_name = raw["full_name"]
    branch = raw.get("default_branch") or "main"
    head_sha = api.branch_sha(full_name, branch)
    ref = cached_ref(ref_cache, full_name, head_sha, lambda: api.template_ref(full_name))
    artifact, coverage, coverage_lines = _coverage(api, coverage_cache, full_name, branch)
    workflows = tuple(_workflow_run(r) for r in api.latest_runs(full_name, branch))
    protected, required_reviews, allows_force_push = _protection(api, full_name, branch)
    alerts = api.open_alerts(full_name)
    pulls_total, pulls = api.open_pulls(full_name)
    merged = api.recent_merges(full_name, cfg.recent_merges_per_repo)

    return RemoteRepo(
        name=raw["name"],
        owner=(raw.get("owner") or {}).get("login") or full_name.split("/", 1)[0],
        forge="github",
        url=raw.get("html_url") or "",
        pulls_url=f"{raw['html_url']}/pulls" if raw.get("html_url") else "",
        default_branch=branch,
        visibility=raw.get("visibility") or "unknown",
        archived=bool(raw.get("archived")),
        head_sha=head_sha,
        pushed_at=_ts(raw.get("pushed_at")),
        protected=protected,
        required_reviews=required_reviews,
        allows_force_push=allows_force_push,
        alerts_enabled=alerts is not None,
        alerts=tuple(sorted((alerts or {}).items())),
        rhiza_managed=bool(ref),
        rhiza_ref=ref,
        rhiza_behind=_behind_count(tags, ref),
        **ci_summary(workflows),
        workflows=workflows,
        coverage=coverage,
        coverage_lines=coverage_lines,
        coverage_artifact=artifact,
        # GitHub's open_issues_count includes pull requests; subtract them to
        # get the number a human means by "open issues". No extra API call.
        open_issues=max(0, int(raw.get("open_issues_count") or 0) - pulls_total),
        open_pulls_total=pulls_total,
        pulls=tuple(pulls),
        merged=tuple(merged),
    )


def _coverage(
    api: GitHub,
    coverage_cache: dict[str, tuple[int, tuple[float, int] | None]],
    full_name: str,
    branch: str,
) -> tuple[int, float | None, int]:
    """``(artifact id, percent, lines)``, downloading only a report not yet seen."""
    artifact = api.coverage_artifact(full_name, branch)
    if not artifact:
        return 0, None, 0
    cached = coverage_cache.get(full_name)
    if cached is not None and cached[0] == artifact:
        measured = cached[1]
    else:
        measured = api.coverage_percent(full_name, artifact)
    if measured is None:
        return artifact, None, 0
    return artifact, measured[0], measured[1]


def _workflow_run(run: dict[str, Any]) -> WorkflowRun:
    """One entry of ``latest_runs`` as the board's WorkflowRun."""
    finished = _ts(run.get("updated_at"))
    return WorkflowRun(
        name=run.get("_name") or run.get("name") or "unnamed",
        conclusion=run.get("conclusion") or "",
        finished_at=finished,
        duration=max(0.0, finished - _ts(run.get("run_started_at"))),
        url=run.get("html_url") or "",
    )


def _protection(api: GitHub, full_name: str, branch: str) -> tuple[bool | None, int, bool]:
    """``(protected, required reviews, force pushes allowed)`` for the branch.

    ``protected`` is None when GitHub would not say - see branch_protection.
    """
    protection, known = api.branch_protection(full_name, branch)
    reviews = (protection or {}).get("required_pull_request_reviews") or {}
    force = (protection or {}).get("allow_force_pushes") or {}
    return (
        (protection is not None) if known else None,
        int(reviews.get("required_approving_review_count") or 0),
        bool(force.get("enabled")),
    )
