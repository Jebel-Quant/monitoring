"""Read the state of the repos as GitHub sees them.

Per refresh this costs roughly ``3 * repos + open_pull_requests`` REST calls
(one branch, one workflow-run and one pull listing each, plus a check-run
listing per open PR), which at the default five-minute cadence sits well inside
the authenticated 5000/hour budget. ``jq_github_rate_limit_remaining`` is
exported so the headroom is visible rather than assumed.
"""

from __future__ import annotations

import base64
import concurrent.futures
import logging
from datetime import datetime

import httpx

from .config import Config
from .state import MergedPull, PullRequest, RemoteRepo, WorkflowRun

log = logging.getLogger(__name__)

# Conclusions that mean the workflow did its job. Kept here as well as in
# metrics.py because the representative-run choice depends on it.
GOOD_CONCLUSIONS = frozenset({"success", "neutral", "skipped"})

_ACCEPT = "application/vnd.github+json"
_MAX_WORKERS = 8


def _ts(value: str | None) -> float:
    if not value:
        return 0.0
    try:
        return datetime.fromisoformat(value).timestamp()
    except ValueError:
        return 0.0


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

    def _get(self, path: str, **params: object) -> httpx.Response:
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

    def _json(self, path: str, **params: object) -> object | None:
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
        return response.json()

    def _paginate(self, path: str, **params: object) -> list[dict]:
        items: list[dict] = []
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

    def list_repos(self) -> list[dict]:
        """Every in-scope repo: whole-org sweeps plus individually named ones."""
        repos: list[dict] = []
        seen: set[str] = set()

        for org in self._cfg.orgs:
            found = self._paginate(f"/orgs/{org}/repos", type="all", sort="full_name")
            if not found:
                # A user account rather than an org, or no org read access.
                owned = self._paginate("/user/repos", affiliation="owner", sort="full_name")
                found = [r for r in owned if (r.get("owner") or {}).get("login") == org]
            for raw in found:
                if raw.get("full_name") not in seen:
                    seen.add(raw["full_name"])
                    repos.append(raw)

        for full_name in self._cfg.extra_repos:
            if full_name in seen or "/" not in full_name:
                continue
            raw = self._json(f"/repos/{full_name}")
            if isinstance(raw, dict):
                seen.add(raw["full_name"])
                repos.append(raw)
            else:
                log.warning(
                    "named repo %s is not readable - check the token's scopes",
                    full_name,
                )

        return repos

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
            import yaml

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
        labelled with the name it has now. Disabled workflows are left out - a
        switched-off job is not a failing one.

        Returns None if the listing cannot be read, which means "do not filter":
        better to over-report than to blank a repo's CI on a transient error.
        """
        data = self._json(f"/repos/{full_name}/actions/workflows", per_page=100)
        if not isinstance(data, dict):
            return None
        active = {
            w["id"]: (w.get("name") or w.get("path") or str(w["id"]))
            for w in data.get("workflows") or []
            if w.get("state") == "active" and "id" in w
        }
        # Two active workflows may legitimately share a display name; fall back
        # to the path for those so the metric labels stay unique.
        seen: dict[str, int] = {}
        for wid, name in list(active.items()):
            if name in seen:
                for other in (wid, seen[name]):
                    path = next(
                        (w.get("path") for w in data["workflows"] if w["id"] == other),
                        None,
                    )
                    if path:
                        active[other] = path
            else:
                seen[name] = wid
        return active

    def latest_runs(self, full_name: str, branch: str) -> list[dict]:
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
        """
        active = self.active_workflows(full_name)

        data = self._json(
            f"/repos/{full_name}/actions/runs",
            branch=branch,
            status="completed",
            exclude_pull_requests="true",
            per_page=100,
        )
        newest: dict[object, dict] = {}
        for run in (data or {}).get("workflow_runs") or []:
            wid = run.get("workflow_id")
            if active is not None and wid not in active:
                continue  # workflow deleted or disabled since this run
            key = wid if wid is not None else run.get("name")
            current = newest.get(key)
            if current is None or _ts(run.get("updated_at")) > _ts(current.get("updated_at")):
                newest[key] = run

        # One targeted call per workflow the feed missed. Quiet repos pay
        # nothing; only the busy ones do, and only for what was actually hidden.
        if active is not None:
            for wid in active:
                if wid in newest:
                    continue
                extra = self._json(
                    f"/repos/{full_name}/actions/workflows/{wid}/runs",
                    branch=branch,
                    status="completed",
                    exclude_pull_requests="true",
                    per_page=1,
                )
                runs = (extra or {}).get("workflow_runs") or []
                if runs:
                    newest[wid] = runs[0]

        return [
            {**run, "_name": (active or {}).get(wid) or run.get("name") or "unnamed"}
            for wid, run in newest.items()
        ]

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
                    created_at=_ts(item.get("created_at")),
                    updated_at=_ts(item.get("updated_at")),
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
                merged_at=_ts(item.get("merged_at")),
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
        runs = data.get("check_runs") or []
        if not runs:
            return "none"
        conclusions = {r.get("conclusion") for r in runs if r.get("status") == "completed"}
        if any(r.get("status") != "completed" for r in runs):
            return "pending"
        if conclusions & {"failure", "timed_out", "action_required"}:
            return "failure"
        if conclusions & {"cancelled"}:
            return "cancelled"
        return "success"


def _behind_count(tags: list[str], ref: str) -> int | None:
    """How many releases were published after ``ref``.

    Returns None when the pinned ref is not a published release tag - a branch
    name or a sha - so the dashboard can show "unknown" instead of "current".
    """
    if not ref or ref not in tags:
        return None
    return tags.index(ref)


def collect(
    cfg: Config, ref_cache: dict[str, tuple[str, str]]
) -> tuple[dict[str, RemoteRepo], GitHub, str, frozenset[str]]:
    """Build the remote half of the snapshot, keyed by ``owner/name``.

    The template pointer is always read from GitHub's default branch, never from
    the local clone. Drift is a property of the *repo*, and a clone can be
    arbitrarily stale - reading it from disk made an un-pulled checkout look
    like an out-of-date repo.

    ``ref_cache`` maps ``full_name -> (default_branch_sha, ref)`` from the
    previous refresh. The pointer can only have changed if the branch head
    moved, so an unchanged sha skips the fetch and the steady-state cost of
    correctness is zero extra calls.
    """
    api = GitHub(cfg)
    tags = api.release_tags(cfg.template_repo)
    latest = tags[0] if tags else ""

    # Dropped repos are reported back so the local scan can skip their clones
    # too - otherwise a checkout keeps a dead repo on the board after the GitHub
    # half has correctly stopped reporting it.
    #
    # JQ_IGNORE is applied here as well as in the local scan. It used to be
    # honoured only by the scan, via Config.wants(), so ignoring a repo silently
    # removed its working-copy rows while leaving every CI, drift and
    # pull-request series in place.
    listing = api.list_repos()
    excluded = frozenset(
        r["full_name"]
        for r in listing
        if (r.get("archived") and not cfg.include_archived)
        or cfg.is_ignored(*r["full_name"].split("/", 1))
        # `visibility` covers private and internal; `private` is the older flag.
        or (cfg.public_only and (r.get("private") or r.get("visibility") != "public"))
    )
    repos = [r for r in listing if r["full_name"] not in excluded]

    def one(raw: dict) -> RemoteRepo:
        full_name = raw["full_name"]
        owner = (raw.get("owner") or {}).get("login") or full_name.split("/", 1)[0]
        branch = raw.get("default_branch") or "main"
        head_sha = api.branch_sha(full_name, branch)

        cached = ref_cache.get(full_name)
        if cached is not None and head_sha and cached[0] == head_sha:
            ref = cached[1]
        else:
            ref = api.template_ref(full_name)

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

        pulls_total, pulls = api.open_pulls(full_name)
        merged = api.recent_merges(full_name, cfg.recent_merges_per_repo)
        # GitHub's open_issues_count includes pull requests; subtract them to
        # get the number a human means by "open issues". No extra API call.
        open_issues = max(0, int(raw.get("open_issues_count") or 0) - pulls_total)

        return RemoteRepo(
            name=raw["name"],
            owner=owner,
            default_branch=branch,
            visibility=raw.get("visibility") or "unknown",
            archived=bool(raw.get("archived")),
            head_sha=head_sha,
            pushed_at=_ts(raw.get("pushed_at")),
            rhiza_managed=bool(ref),
            rhiza_ref=ref,
            rhiza_behind=_behind_count(tags, ref),
            ci_conclusion=representative.conclusion if representative else "",
            ci_workflow=representative.name if representative else "",
            ci_finished_at=representative.finished_at if representative else 0.0,
            ci_duration=representative.duration if representative else 0.0,
            ci_url=representative.url if representative else "",
            workflows=workflows,
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

    return result, api, latest, excluded
