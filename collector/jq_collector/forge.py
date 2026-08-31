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

from typing import Protocol

from .state import RemoteRepo

# Conclusions that mean the run did its job.
GOOD_CONCLUSIONS = frozenset({"success", "neutral", "skipped"})

# Conclusions that are no verdict at all - cancelled by hand or superseded by a
# newer push, and `stale` for a run that never really happened. Neither is green
# or red, so they are left out of the exposition rather than counted as failures.
INCONCLUSIVE_CONCLUSIONS = frozenset({"cancelled", "stale"})

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
    ) -> tuple[dict[str, RemoteRepo], frozenset[str]]: ...

    def close(self) -> None: ...
