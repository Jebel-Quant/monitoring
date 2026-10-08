"""The GitHub REST client: one call per question, and the answers as plain data.

Split out of github.py, which turns these answers into a snapshot. Everything
that knows about GitHub's endpoints, paging, rate-limit headers and the
quirks of its responses lives here; nothing here knows what a RemoteRepo is
beyond the PullRequest and MergedPull rows it hands back.

The coverage artifact is the one response that is not JSON. Its bytes are
handed to coverage_xml, which owns every guard on an archive we did not build.
``rate_remaining`` and friends are read off every response, so the headroom
is visible on the board rather than assumed.
"""

from __future__ import annotations

import base64
import logging
from typing import Any

import httpx
import yaml

from . import coverage_xml
from .config import Config
from .forge import ts
from .github_payloads import (
    active_workflow_names,
    alert_counts,
    checks_rollup,
    inconclusive,
    newest_coverage_artifact,
    newest_per_workflow,
)
from .state import MergedPull, PullRequest

# Under github.py's name, not this module's: the client was split out of it, and the
# logger name is printed on every line of `docker logs`, where a refactor
# should not move anything somebody greps for.
log = logging.getLogger("jq_collector.github")

_ACCEPT = "application/vnd.github+json"


class GitHub:
    def __init__(self, cfg: Config) -> None:
        self._cfg = cfg
        headers = {"Accept": _ACCEPT, "X-GitHub-Api-Version": "2022-11-28"}
        if cfg.token:
            headers["Authorization"] = f"Bearer {cfg.token}"
        self._client = httpx.Client(
            base_url=cfg.api,
            headers=headers,
            timeout=cfg.http_timeout,
            follow_redirects=True,
        )
        self.rate_remaining = -1.0
        self.rate_limit = -1.0
        self.rate_reset = 0.0

    def close(self) -> None:
        self._client.close()

    def _get(self, path: str, **params: str | int) -> httpx.Response:
        response = self._client.get(path, params=params or None)
        self._note_rate_limit(response)
        return response

    def _note_rate_limit(self, response: httpx.Response) -> None:
        for header, attr in (
            ("x-ratelimit-remaining", "rate_remaining"),
            ("x-ratelimit-limit", "rate_limit"),
            ("x-ratelimit-reset", "rate_reset"),
        ):
            raw = response.headers.get(header)
            if raw is not None:
                try:
                    setattr(self, attr, float(raw))
                except ValueError:
                    pass

    def _json(self, path: str, **params: str | int) -> object | None:
        """GET returning parsed JSON, or None for the expected empty cases.

        404 (no such file), 403 (rate limited or forbidden) and 409 (empty
        repository) are all states the fleet legitimately contains; they are
        logged and skipped rather than failing the whole refresh.
        """
        response = self._get(path, **params)
        if response.status_code in (403, 404, 409, 451):
            log.info("%s -> %s", path, response.status_code)
            return None
        response.raise_for_status()
        payload: object = response.json()
        return payload

    def _bytes(self, path: str) -> bytes | None:
        """GET returning raw bytes. 410 is added to the expected empties: that
        is what an expired artifact returns, and artifacts expire on a schedule
        nobody here controls."""
        response = self._get(path)
        if response.status_code in (403, 404, 409, 410, 451):
            log.info("%s -> %s", path, response.status_code)
            return None
        response.raise_for_status()
        return response.content

    def _paginate(self, path: str, **params: str | int) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        page = 1
        while True:
            batch = self._json(path, per_page=100, page=page, **params)
            if not isinstance(batch, list) or not batch:
                break
            items.extend(batch)
            if len(batch) < 100:
                break
            page += 1
            if page > 10:  # a fleet this size never needs more
                break
        return items

    # -- fleet-level -----------------------------------------------------

    def list_repos(self, fleet: tuple[str, ...] | None = None) -> list[dict[str, Any]]:
        """The repos named in the config, in the order they were listed.

        One call each, and no org sweep: the fleet is whatever you wrote down.
        A repo that cannot be read is dropped with a warning rather than
        failing the refresh, so one bad line does not blank the whole board.

        ``fleet`` narrows that list to this forge's share. Without it every
        GitLab repo in a mixed fleet would be asked of GitHub, which answers 404
        and logs each one as unreadable.
        """
        repos: list[dict[str, Any]] = []
        seen: set[str] = set()

        for full_name in fleet if fleet is not None else self._cfg.repos:
            if "/" not in full_name or full_name in seen:
                continue
            raw = self._json(f"/repos/{full_name}")
            if isinstance(raw, dict) and raw.get("full_name"):
                seen.add(raw["full_name"])
                repos.append(raw)
            else:
                log.warning(
                    "listed repo %s is not readable - check the name and the token's scopes",
                    full_name,
                )

        return repos

    def branch_protection(self, full_name: str, branch: str) -> tuple[dict[str, Any] | None, bool]:
        """``(protection, known)`` for one branch.

        The endpoint 404s both for an unprotected branch and for a token
        without admin, but the bodies differ: an unprotected branch says
        "Branch not protected", where a permission gap does not. Reading the
        message is what lets an unprotected branch be reported as a fact
        rather than as a gap - and a fleet where nothing is protected is
        exactly the case this metric exists to show.

        ``known`` is False only when GitHub genuinely would not say, so the
        caller never has to turn "we cannot see" into "it is unprotected".
        """
        response = self._get(f"/repos/{full_name}/branches/{branch}/protection")
        if response.status_code == 200:
            return response.json(), True
        if response.status_code == 404:
            try:
                message = str((response.json() or {}).get("message", ""))
            except ValueError:
                message = ""
            if "not protected" in message.lower():
                return None, True
            log.info("%s protection unreadable: %s", full_name, message or "404")
        return None, False

    def open_alerts(self, full_name: str) -> dict[str, int] | None:
        """Open Dependabot alerts by severity, or None when the feature is off.

        A repo with alerts disabled 404s exactly like one with none open, so
        None and {} are kept distinct all the way to the exposition.
        """
        raw = self._json(f"/repos/{full_name}/dependabot/alerts", state="open", per_page=100)
        if not isinstance(raw, list):
            return None
        return alert_counts(raw)

    def release_tags(self, full_name: str) -> list[str]:
        """Published release tags for a repo, newest first."""
        releases = self._paginate(f"/repos/{full_name}/releases")
        return [r["tag_name"] for r in releases if not r.get("draft") and r.get("tag_name")]

    # -- per repo --------------------------------------------------------

    def branch_sha(self, full_name: str, branch: str) -> str:
        data = self._json(f"/repos/{full_name}/branches/{branch}")
        if not isinstance(data, dict):
            return ""
        return ((data.get("commit") or {}).get("sha")) or ""

    def template_ref(self, full_name: str) -> str:
        """Read the template pointer over the API (for repos not cloned locally)."""
        data = self._json(f"/repos/{full_name}/contents/{self._cfg.template_pointer}")
        if not isinstance(data, dict) or "content" not in data:
            return ""
        try:
            raw = base64.b64decode(data["content"]).decode("utf-8")
            parsed = yaml.safe_load(raw) or {}
        except Exception as exc:  # noqa: BLE001 - malformed pointer is data
            log.warning("%s: could not parse template pointer: %s", full_name, exc)
            return ""
        ref = parsed.get("ref") if isinstance(parsed, dict) else None
        return str(ref).strip() if ref else ""

    def active_workflows(self, full_name: str) -> dict[int, str] | None:
        """Active workflow id -> its current name, from the authoritative listing.

        Run history is not a reliable source for either fact. It outlives the
        workflow file, so a workflow deleted months ago keeps its old runs and a
        failing last run makes the repo look permanently red for something that
        no longer exists - Jebel-Quant/platform sat red for 12 weeks on a deleted
        `latex.yml`. And a renamed workflow appears under BOTH names, so the old
        name's last run lingers the same way: one id on that repo produced runs
        called "Build PDF" and "Build vision.pdf".

        Taking the name from here instead means one series per real workflow,
        labelled with the name it has now - see active_workflow_names.

        Returns None if the listing cannot be read, which means "do not filter":
        better to over-report than to blank a repo's CI on a transient error.
        """
        data = self._json(f"/repos/{full_name}/actions/workflows", per_page=100)
        if not isinstance(data, dict):
            return None
        return active_workflow_names(data.get("workflows") or [])

    def latest_runs(self, full_name: str, branch: str) -> list[dict[str, Any]]:
        """The newest completed run of each active workflow on ``branch``.

        The runs feed is ordered by ``created_at`` and is not a per-workflow
        view, so on a busy repo it is dominated by whatever runs most often: of
        cvxgrp/simulator's 2406 completed runs on main, the first hundred cover
        only 6 of its 23 active workflows. A quiet workflow - a weekly job, say -
        falls off the page entirely and its failure becomes invisible. So
        anything the feed does not account for is asked for directly.

        Runs are also compared on ``updated_at`` rather than trusting feed
        order, because ``created_at`` ordering puts a long or re-run job below
        newer ones that finished earlier.

        Inconclusive runs are skipped, so a workflow keeps showing its last real
        verdict instead of being reported on a run that never reached one.
        """
        active = self.active_workflows(full_name)

        data = self._json(
            f"/repos/{full_name}/actions/runs",
            branch=branch,
            status="completed",
            exclude_pull_requests="true",
            per_page=100,
        )
        feed = data.get("workflow_runs") if isinstance(data, dict) else None
        newest = newest_per_workflow(feed or [], active)
        names = active or {}
        self._backfill(full_name, branch, names, newest)
        return [
            {**run, "_name": names.get(wid) or run.get("name") or "unnamed"}
            for wid, run in newest.items()
        ]

    def _backfill(
        self, full_name: str, branch: str, active: dict[int, str], newest: dict[Any, dict[str, Any]]
    ) -> None:
        """Add to ``newest`` each active workflow the runs feed missed.

        One targeted call per missing workflow, in listing order. Quiet repos
        pay nothing; only the busy ones do, and only for what was actually hidden.
        """
        for wid in active:
            if wid in newest:
                continue
            conclusive = self._newest_conclusive(full_name, branch, wid)
            if conclusive is not None:
                newest[wid] = conclusive

    def _newest_conclusive(self, full_name: str, branch: str, wid: int) -> dict[str, Any] | None:
        """The newest run of one workflow that reached a verdict, if any."""
        extra = self._json(
            f"/repos/{full_name}/actions/workflows/{wid}/runs",
            branch=branch,
            status="completed",
            exclude_pull_requests="true",
            # More than one, because the newest run may be a cancelled
            # one; the API has no "conclusive only" filter.
            per_page=5,
        )
        runs = (extra.get("workflow_runs") if isinstance(extra, dict) else None) or []
        return next((r for r in runs if not inconclusive(r)), None)

    def coverage_artifact(self, full_name: str, branch: str) -> int:
        """Id of the newest ``coverage-report`` artifact built on ``branch``.

        Filtering on the branch is not optional. Artifacts are returned newest
        first across *every* ref, and a tag build is usually the most recent
        one - rhiza's newest coverage artifact is from ``v1.7.1``, not ``main``.
        Taking the latest would quietly report a release build's coverage as
        the repo's, which is a different number measured at a different commit.

        Zero when the repo publishes no such artifact, which most of a mixed
        fleet does not.
        """
        raw = self._json(f"/repos/{full_name}/actions/artifacts", per_page=100)
        if not isinstance(raw, dict):
            return 0
        return newest_coverage_artifact(raw.get("artifacts") or [], branch)

    def coverage_percent(self, full_name: str, artifact_id: int) -> tuple[float, int] | None:
        """``(line coverage as a percentage, lines measured)`` from that artifact.

        The line count is carried because the percentage alone is not
        interpretable. CI measures whatever it pointed ``--cov`` at, which is
        the package rather than everything tracked: rhiza reports 100% of 176
        lines, while the board's own LOC column counts 1477. Both are right and
        they are answering different questions, so the denominator is exported
        alongside rather than left to be guessed at.

        ``branch-rate`` is deliberately not read. Branch coverage is not
        enabled in this fleet's CI, so it is a constant zero and putting it on
        the board would invent a finding.
        """
        blob = self._bytes(f"/repos/{full_name}/actions/artifacts/{artifact_id}/zip")
        if blob is None:
            return None
        return coverage_xml.read(blob, full_name)

    def open_pulls(self, full_name: str) -> tuple[int, list[PullRequest]]:
        """(total open PRs, detail for the first ``max_prs_per_repo``).

        The total is reported separately because the detail list is clipped, and
        because it is what turns GitHub's ``open_issues_count`` - which counts
        pull requests as issues - into a real issue count.
        """
        raw = self._paginate(f"/repos/{full_name}/pulls", state="open")
        total = len(raw)
        raw = raw[: self._cfg.max_prs_per_repo]
        pulls: list[PullRequest] = []
        for item in raw:
            sha = ((item.get("head") or {}).get("sha")) or ""
            pulls.append(
                PullRequest(
                    number=int(item.get("number", 0)),
                    title=(item.get("title") or "")[:120],
                    author=((item.get("user") or {}).get("login")) or "unknown",
                    draft=bool(item.get("draft")),
                    created_at=ts(item.get("created_at")),
                    updated_at=ts(item.get("updated_at")),
                    checks=self.checks_state(full_name, sha) if sha else "none",
                    url=item.get("html_url") or "",
                )
            )
        return total, pulls

    def recent_merges(self, full_name: str, limit: int) -> list[MergedPull]:
        """The most recently merged pull requests, newest first.

        Closed and merged are not the same thing - a closed PR may simply have
        been abandoned - so anything without a merged_at is dropped. GitHub
        sorts by update time rather than merge time, which are usually but not
        always the same order, so they are re-sorted here.
        """
        raw = self._json(
            f"/repos/{full_name}/pulls",
            state="closed",
            sort="updated",
            direction="desc",
            per_page=limit * 2,  # closed-but-unmerged ones get filtered out
        )
        if not isinstance(raw, list):
            return []
        merged = [
            MergedPull(
                number=int(item.get("number", 0)),
                title=(item.get("title") or "")[:120],
                author=((item.get("user") or {}).get("login")) or "unknown",
                merged_at=ts(item.get("merged_at")),
                url=item.get("html_url") or "",
            )
            for item in raw
            if item.get("merged_at")
        ]
        merged.sort(key=lambda m: m.merged_at, reverse=True)
        return merged[:limit]

    def checks_state(self, full_name: str, sha: str) -> str:
        """Roll a commit's check runs up to one word.

        GitHub Actions report as *check runs*, not as legacy commit statuses, so
        the combined-status endpoint would show every Actions-only repo as
        having no checks at all.
        """
        data = self._json(f"/repos/{full_name}/commits/{sha}/check-runs", per_page=100)
        if not isinstance(data, dict):
            return "unknown"
        return checks_rollup(data.get("check_runs") or [])
