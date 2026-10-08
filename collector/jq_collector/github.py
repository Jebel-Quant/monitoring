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

import concurrent.futures
import logging
from typing import Any

from .config import Config
from .forge import GOOD_CONCLUSIONS, INCONCLUSIVE_CONCLUSIONS
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
    #
    # JQ_IGNORE is applied here as well as in the local scan. It used to be
    # honoured only by the scan, via Config.wants(), so ignoring a repo silently
    # removed its working-copy rows while leaving every CI, drift and
    # pull-request series in place.
    listing = api.list_repos(fleet)
    excluded = frozenset(
        r["full_name"]
        for r in listing
        if (r.get("archived") and not cfg.include_archived)
        or cfg.is_ignored(*r["full_name"].split("/", 1))
        # `visibility` covers private and internal; `private` is the older flag.
        or (cfg.public_only and (r.get("private") or r.get("visibility") != "public"))
    )
    repos = [r for r in listing if r["full_name"] not in excluded]

    def one(raw: dict[str, Any]) -> RemoteRepo:
        full_name = raw["full_name"]
        owner = (raw.get("owner") or {}).get("login") or full_name.split("/", 1)[0]
        branch = raw.get("default_branch") or "main"
        head_sha = api.branch_sha(full_name, branch)

        cached = ref_cache.get(full_name)
        if cached is not None and head_sha and cached[0] == head_sha:
            ref = cached[1]
        else:
            ref = api.template_ref(full_name)

        artifact = api.coverage_artifact(full_name, branch)
        cached_coverage = coverage_cache.get(full_name)
        if artifact and cached_coverage is not None and cached_coverage[0] == artifact:
            measured = cached_coverage[1]
        elif artifact:
            measured = api.coverage_percent(full_name, artifact)
        else:
            measured = None
        coverage, coverage_lines = measured if measured else (None, 0)

        workflows = tuple(
            WorkflowRun(
                name=r.get("_name") or r.get("name") or "unnamed",
                conclusion=r.get("conclusion") or "",
                finished_at=_ts(r.get("updated_at")),
                duration=max(0.0, _ts(r.get("updated_at")) - _ts(r.get("run_started_at"))),
                url=r.get("html_url") or "",
            )
            for r in api.latest_runs(full_name, branch)
        )
        # The branch is red if ANY workflow's latest run is red, and the run
        # worth showing is that failure - not whichever workflow happens to have
        # run most recently.
        failing = sorted(
            (w for w in workflows if w.conclusion and w.conclusion not in GOOD_CONCLUSIONS),
            key=lambda w: w.finished_at,
            reverse=True,
        )
        rest = sorted(workflows, key=lambda w: w.finished_at, reverse=True)
        representative = failing[0] if failing else (rest[0] if rest else None)

        protection, protection_known = api.branch_protection(full_name, branch)
        reviews = (protection or {}).get("required_pull_request_reviews") or {}
        force = (protection or {}).get("allow_force_pushes") or {}

        alerts = api.open_alerts(full_name)

        pulls_total, pulls = api.open_pulls(full_name)
        merged = api.recent_merges(full_name, cfg.recent_merges_per_repo)
        # GitHub's open_issues_count includes pull requests; subtract them to
        # get the number a human means by "open issues". No extra API call.
        open_issues = max(0, int(raw.get("open_issues_count") or 0) - pulls_total)

        return RemoteRepo(
            name=raw["name"],
            owner=owner,
            forge="github",
            url=raw.get("html_url") or "",
            pulls_url=f"{raw['html_url']}/pulls" if raw.get("html_url") else "",
            default_branch=branch,
            visibility=raw.get("visibility") or "unknown",
            archived=bool(raw.get("archived")),
            head_sha=head_sha,
            pushed_at=_ts(raw.get("pushed_at")),
            protected=(protection is not None) if protection_known else None,
            required_reviews=int(reviews.get("required_approving_review_count") or 0),
            allows_force_push=bool(force.get("enabled")),
            alerts_enabled=alerts is not None,
            alerts=tuple(sorted((alerts or {}).items())),
            rhiza_managed=bool(ref),
            rhiza_ref=ref,
            rhiza_behind=_behind_count(tags, ref),
            ci_conclusion=representative.conclusion if representative else "",
            ci_workflow=representative.name if representative else "",
            ci_finished_at=representative.finished_at if representative else 0.0,
            ci_duration=representative.duration if representative else 0.0,
            ci_url=representative.url if representative else "",
            workflows=workflows,
            coverage=coverage,
            coverage_lines=coverage_lines,
            coverage_artifact=artifact,
            open_issues=open_issues,
            open_pulls_total=pulls_total,
            pulls=tuple(pulls),
            merged=tuple(merged),
        )

    result: dict[str, RemoteRepo] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=_MAX_WORKERS) as pool:
        futures = {pool.submit(one, raw): raw["full_name"] for raw in repos}
        for future in concurrent.futures.as_completed(futures):
            full_name = futures[future]
            try:
                result[full_name] = future.result()
            except Exception as exc:  # noqa: BLE001 - one bad repo must not sink the refresh
                log.warning("repo %s failed: %s", full_name, exc)

    return result, api, tags, excluded
