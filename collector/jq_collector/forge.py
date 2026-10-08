"""The seam between "a forge" and the rest of the collector.

``RemoteRepo`` asks the same questions of every forge - what is the default
branch, is it protected, is CI green, what is open - so the only thing that
varies is which URLs answer them and in what words. This module holds the
protocol both collectors satisfy and the one translation that matters: CI
verdicts.

GitHub and GitLab disagree on the vocabulary. GitHub conclusions are ``success``,
``failure``, ``cancelled``, ``skipped``, ``neutral``, ``stale``; GitLab pipeline
and job statuses are ``success``, ``failed``, ``canceled`` (one l), ``skipped``,
``manual``, ``running``, plus a handful of pre-run states. ``metrics.py`` and the
dashboard's value mappings are written against GitHub's set, and that set is
also what the stored history says. So the GitLab collector normalises on the way
in and nothing downstream has to know there was ever a second spelling.
"""

from __future__ import annotations

import concurrent.futures
import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any, Protocol, TypedDict

from .state import RemoteRepo, WorkflowRun

# Conclusions that mean the run did its job.
GOOD_CONCLUSIONS = frozenset({"success", "neutral", "skipped"})

# Conclusions that are no verdict at all - cancelled by hand or superseded by a
# newer push, and `stale` for a run that never really happened. Neither is green
# or red, so they are left out of the exposition rather than counted as failures.
INCONCLUSIVE_CONCLUSIONS = frozenset({"cancelled", "stale"})


def ts(value: str | None) -> float:
    """An ISO timestamp as epoch seconds, or 0.0 when there is none to read.

    One function for both forges. GitLab writes ``2026-08-31T06:05:36.000Z``
    where GitHub has no milliseconds; both parse as they stand, because 3.11
    taught ``fromisoformat`` the whole of ISO 8601, trailing ``Z`` included, and
    3.11 is this package's floor.

    >>> ts("1970-01-01T00:01:00+00:00")
    60.0
    >>> ts("1970-01-01T00:01:00.000Z")
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


def behind_count(tags: list[str], ref: str) -> int | None:
    """How many template releases were published after ``ref``, newest first in ``tags``.

    None when the pinned ref is not a published release tag - a branch name or
    a sha - so the dashboard can show "unknown" instead of "current". The same
    for both forges: the template lives on GitHub whichever forge the repo
    pinning it lives on, so drift is measured against one tag list.

    >>> behind_count(["v2.0", "v1.9", "v1.8"], "v1.8")
    2
    >>> behind_count(["v2.0"], "main") is None
    True
    """
    if not ref or ref not in tags:
        return None
    return tags.index(ref)


# GitLab's spelling -> the vocabulary the board already speaks.
#
# `manual` is a job waiting for somebody to press the button - a deploy gate,
# typically. It is not a failure and not a success, so it maps onto the same
# inconclusive bucket as a cancelled run; counting held deploys as red would
# make a correctly-configured pipeline permanently angry.
#
# The pre-run states (`created`, `pending`, `preparing`, `waiting_for_resource`,
# `scheduled`, `running`) describe a pipeline still in flight. They are also
# inconclusive: the board reports the last *completed* state of the default
# branch, so a run in progress must not overwrite the verdict of the one before.
_GITLAB_STATUS = {
    "success": "success",
    "failed": "failure",
    "canceled": "cancelled",
    "canceling": "cancelled",
    "skipped": "skipped",
    "manual": "cancelled",
    "created": "stale",
    "pending": "stale",
    "preparing": "stale",
    "waiting_for_resource": "stale",
    "waiting_for_callback": "stale",
    "scheduled": "stale",
    "running": "stale",
}


def normalise_gitlab_status(status: str) -> str:
    """One GitLab status as a GitHub conclusion.

    An unrecognised status becomes ``stale`` rather than ``failure``: GitLab has
    added states before and will again, and a new one appearing as a fleet-wide
    red is a worse failure mode than it appearing as "no verdict yet".

    >>> normalise_gitlab_status("failed")
    'failure'
    >>> normalise_gitlab_status(" Canceled ")
    'cancelled'
    >>> normalise_gitlab_status("manual")
    'cancelled'
    >>> normalise_gitlab_status("some_future_state")
    'stale'
    """
    return _GITLAB_STATUS.get((status or "").strip().lower(), "stale")


class RemoteSource(Protocol):
    """What ``__main__`` needs of a forge client.

    ``collect`` returns the repos it was able to read, keyed by
    ``namespace/name``, plus the set it deliberately dropped (archived, or named
    in ``JQ_IGNORE``). Both caches are keyed the same way and may hold entries
    for repos on other forges; an implementation is expected to ignore those
    rather than trip over them.
    """

    def collect(
        self,
        ref_cache: dict[str, tuple[str, str]],
        coverage_cache: dict[str, tuple[int, tuple[float, int] | None]],
    ) -> tuple[dict[str, RemoteRepo], frozenset[str]]:
        """This forge's repos keyed by ``namespace/name``, and the ones it dropped."""

    def close(self) -> None:
        """Release the client's connections."""


# -- shared by both collectors -----------------------------------------------
#
# Each of these was written twice, once per forge, inside a per-repo closure
# that radon does not show by default - github's scored E (38) and gitlab's
# D (28) while their enclosing collect() read as a mild C. One copy each, here,
# is both the smaller code and the guarantee that the two forges cannot drift.


class CiSummary(TypedDict):
    """The ``ci_*`` fields of a RemoteRepo. Unpacked into it with ``**``."""

    ci_conclusion: str
    ci_workflow: str
    ci_finished_at: float
    ci_duration: float
    ci_url: str


def ci_summary(workflows: tuple[WorkflowRun, ...]) -> CiSummary:
    """The run that represents the branch, as RemoteRepo's ``ci_*`` fields.

    The branch is red if ANY workflow's latest run is red, and the run worth
    showing is that failure - not whichever workflow happens to have run most
    recently. With nothing red, it is the most recently finished run.

    >>> old_red = WorkflowRun("lint", "failure", 10.0, 1.0, "u1")
    >>> new_green = WorkflowRun("test", "success", 20.0, 2.0, "u2")
    >>> ci_summary((new_green, old_red))["ci_workflow"]
    'lint'
    >>> ci_summary(())["ci_conclusion"]
    ''
    """
    newest_first = sorted(workflows, key=lambda w: w.finished_at, reverse=True)
    failing = [w for w in newest_first if w.conclusion and w.conclusion not in GOOD_CONCLUSIONS]
    shown = failing[0] if failing else (newest_first[0] if newest_first else None)
    if shown is None:
        return CiSummary(
            ci_conclusion="", ci_workflow="", ci_finished_at=0.0, ci_duration=0.0, ci_url=""
        )
    return CiSummary(
        ci_conclusion=shown.conclusion,
        ci_workflow=shown.name,
        ci_finished_at=shown.finished_at,
        ci_duration=shown.duration,
        ci_url=shown.url,
    )


def cached_ref(
    ref_cache: dict[str, tuple[str, str]],
    full_name: str,
    head_sha: str,
    fetch: Callable[[], str],
) -> str:
    """The template ref, refetched only when the default branch has moved.

    ``ref_cache`` maps ``full_name -> (default_branch_sha, ref)`` from the last
    refresh. The pointer can only have changed if the branch head moved, so an
    unchanged sha costs no call at all.

    >>> cached_ref({"o/r": ("abc", "v1.2")}, "o/r", "abc", lambda: "fetched")
    'v1.2'
    >>> cached_ref({"o/r": ("abc", "v1.2")}, "o/r", "def", lambda: "fetched")
    'fetched'
    """
    cached = ref_cache.get(full_name)
    if cached is not None and head_sha and cached[0] == head_sha:
        return cached[1]
    return fetch()


def fan_out(
    raws: list[dict[str, Any]],
    key: str,
    build: Callable[[dict[str, Any]], RemoteRepo],
    log: logging.Logger,
    workers: int = 8,
) -> dict[str, RemoteRepo]:
    """``build`` every raw listing concurrently, keyed by its ``key`` field.

    One bad repo must not sink the refresh: a repo whose build raises is logged
    on the caller's logger, so the line still names the forge, and left out.
    """
    result: dict[str, RemoteRepo] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(build, raw): raw[key] for raw in raws}
        for future in concurrent.futures.as_completed(futures):
            full_name = futures[future]
            try:
                result[full_name] = future.result()
            except Exception as exc:  # noqa: BLE001 - one bad repo must not sink the refresh
                log.warning("repo %s failed: %s", full_name, exc)
    return result
