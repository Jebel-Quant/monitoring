"""GitHub's JSON answers, reduced to the facts the board shows.

Split out of github_api.py so the client is left with transport alone - which
endpoint, which parameters, which status codes count as an expected empty.
Everything here is a pure function of a payload already fetched: no client,
no network, and so nothing that needs a fake HTTP layer to test.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .forge import INCONCLUSIVE_CONCLUSIONS

# The artifact CI uploads its coverage report as, and the file to read inside
# it. Repos that publish nothing by this name simply have no coverage on the
# board - which is the honest answer, and not the same as zero.
COVERAGE_ARTIFACT = "coverage-report"


def ts(value: str | None) -> float:
    """An ISO timestamp as epoch seconds, or 0.0 when there is none to read.

    >>> ts("1970-01-01T00:01:00+00:00")
    60.0
    >>> ts(None), ts("not a date")
    (0.0, 0.0)
    """
    if not value:
        return 0.0
    try:
        return datetime.fromisoformat(value).timestamp()
    except ValueError:
        return 0.0


def active_workflow_names(workflows: list[dict[str, Any]]) -> dict[int, str]:
    """Active workflow id -> a display name unique among them.

    Disabled workflows are left out - a switched-off job is not a failing one.
    Two active workflows may legitimately share a display name; those fall back
    to their path so the metric labels stay unique.

    >>> active_workflow_names([
    ...     {"id": 1, "name": "CI", "path": "a.yml", "state": "active"},
    ...     {"id": 2, "name": "CI", "path": "b.yml", "state": "active"},
    ...     {"id": 3, "name": "Old", "path": "c.yml", "state": "disabled_manually"},
    ... ])
    {1: 'a.yml', 2: 'b.yml'}
    """
    active = {
        w["id"]: (w.get("name") or w.get("path") or str(w["id"]))
        for w in workflows
        if w.get("state") == "active" and "id" in w
    }
    paths = {w["id"]: w.get("path") for w in workflows if "id" in w}
    seen: dict[str, int] = {}
    for wid, name in list(active.items()):
        if name not in seen:
            seen[name] = wid
            continue
        for other in (wid, seen[name]):
            if paths.get(other):
                active[other] = paths[other]
    return active


def inconclusive(run: dict[str, Any]) -> bool:
    """Whether a run ended without a verdict worth reporting (cancelled, stale)."""
    return (run.get("conclusion") or "") in INCONCLUSIVE_CONCLUSIONS


def newest_per_workflow(
    feed: list[dict[str, Any]], active: dict[int, str] | None
) -> dict[Any, dict[str, Any]]:
    """The runs feed reduced to the newest conclusive run per active workflow.

    Keyed on workflow id, or on the run's name for a run that carries none.
    """
    newest: dict[Any, dict[str, Any]] = {}
    for run in feed:
        wid = run.get("workflow_id")
        if active is not None and wid not in active:
            continue  # workflow deleted or disabled since this run
        if inconclusive(run):
            continue  # cancelled or stale: no verdict to report
        key = wid if wid is not None else run.get("name")
        current = newest.get(key)
        if current is None or ts(run.get("updated_at")) > ts(current.get("updated_at")):
            newest[key] = run
    return newest


def newest_coverage_artifact(artifacts: list[dict[str, Any]], branch: str) -> int:
    """Id of the newest unexpired ``coverage-report`` artifact built on ``branch``.

    Zero when there is none.

    >>> newest_coverage_artifact([
    ...     {"id": 1, "name": "coverage-report", "created_at": "2026-01-02",
    ...      "workflow_run": {"head_branch": "v1.0"}},
    ...     {"id": 2, "name": "coverage-report", "created_at": "2026-01-01",
    ...      "workflow_run": {"head_branch": "main"}},
    ... ], "main")
    2
    """
    best_id, best_at = 0, ""
    for artifact in artifacts:
        if artifact.get("name") != COVERAGE_ARTIFACT or artifact.get("expired"):
            continue
        if ((artifact.get("workflow_run") or {}).get("head_branch")) != branch:
            continue
        created = artifact.get("created_at") or ""
        if created >= best_at:
            best_id, best_at = int(artifact.get("id") or 0), created
    return best_id


def alert_counts(alerts: list[dict[str, Any]]) -> dict[str, int]:
    """Open Dependabot alerts counted by severity.

    >>> alert_counts([{"security_advisory": {"severity": "HIGH"}}, {}])
    {'high': 1, 'unknown': 1}
    """
    counts: dict[str, int] = {}
    for alert in alerts:
        advisory = alert.get("security_advisory") or {}
        severity = str(advisory.get("severity") or "unknown").lower()
        counts[severity] = counts.get(severity, 0) + 1
    return counts


def checks_rollup(runs: list[dict[str, Any]]) -> str:
    """A commit's check runs rolled up to one word.

    >>> checks_rollup([])
    'none'
    >>> checks_rollup([{"status": "completed", "conclusion": "success"},
    ...                {"status": "completed", "conclusion": "failure"}])
    'failure'
    """
    if not runs:
        return "none"
    if any(r.get("status") != "completed" for r in runs):
        return "pending"
    conclusions = {r.get("conclusion") for r in runs}
    if conclusions & {"failure", "timed_out", "action_required"}:
        return "failure"
    if conclusions & {"cancelled"}:
        return "cancelled"
    return "success"
