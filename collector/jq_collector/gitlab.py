"""Read the state of the repos as GitLab sees them.

The GitHub twin of this module (``github.py``) is the reference; this one answers
the same questions of a different API. Two differences are worth knowing before
reading it.

**The cost.** Roughly ``8 * repos + open_mrs`` calls per refresh (nine on the
first pass, before the template pointer is cached), against GitHub's
``6 * repos + workflows + open_pull_requests``. It is not the bargain it first
looked like:

- coverage genuinely is cheaper. It is a field on the pipeline, so there is no
  artifact to list and no zip to download (``github.GitHub.coverage_percent``).
  The coverage cache this module's ``collect`` still accepts is therefore
  unused - it is in the protocol because GitHub needs it, and honouring the
  shape keeps ``__main__`` from special-casing either forge.
- MR checks are not. GitLab documents ``head_pipeline`` on the merge-request
  response, but only the *single* MR endpoint returns it - the listing omits the
  key entirely, and reading that absence as "no pipeline" reported every GitLab
  MR as unchecked. So there is one follow-up call per open MR, exactly as on
  GitHub. See ``head_pipeline_status``.
- The pipeline itself costs two calls where GitHub's workflow runs cost one: the
  listing carries a status but not coverage, so the newest one is fetched again
  in full.

**Projects are addressed by URL-encoded path.** ``acme/platform/infra/web``
becomes ``acme%2Fplatform%2Finfra%2Fweb``. GitLab namespaces nest, which is why
``origin.parse`` keeps the whole path rather than the last two segments.

One panel cannot be filled. Dependabot's counterpart is GitLab's vulnerability
report, an Ultimate-tier feature whose REST API is being retired in favour of
GraphQL, so ``alerts_enabled`` is always False here. That is the honest answer
rather than a misleading one: ``state.RemoteRepo`` keeps "the feature is off"
apart from "zero open alerts" precisely so an unscanned repo does not render as
a green tile.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import quote

import httpx
import yaml

from .config import Config
from .forge import behind_count, cached_ref, ci_summary, fan_out, normalise_gitlab_status, ts
from .state import MergedPull, PullRequest, RemoteRepo, WorkflowRun

log = logging.getLogger(__name__)

_MAX_WORKERS = 8

# GitLab's own cap. Asking for more is not an error, it is silently clamped, so
# there is nothing to gain by trying.
_PER_PAGE = 100


def _pid(full_name: str) -> str:
    """A project path as GitLab's ``:id`` path segment."""
    return quote(full_name, safe="")


class GitLab:
    """A GitLab REST client for one refresh, authenticated with GITLAB_TOKEN when set."""

    def __init__(self, cfg: Config) -> None:
        self._cfg = cfg
        headers = {"Accept": "application/json"}
        if cfg.gitlab_token:
            # PRIVATE-TOKEN takes a personal or project access token; Bearer
            # would be an OAuth token, which is not what GITLAB_TOKEN holds.
            headers["PRIVATE-TOKEN"] = cfg.gitlab_token
        self._client = httpx.Client(
            base_url=cfg.gitlab_api,
            headers=headers,
            timeout=cfg.http_timeout,
            follow_redirects=True,
        )

    def close(self) -> None:
        """Release the client's connections."""
        self._client.close()

    def _json(self, path: str, **params: str | int) -> object | None:
        """GET returning parsed JSON, or None for the expected empty cases.

        The same three states the GitHub client tolerates, for the same reason:
        404 is a missing file or a project the token cannot see, 403 is a
        feature the tier does not include, and both are things a real fleet
        contains. Failing the refresh over one would blank the whole board.
        """
        response = self._get(path, **params)
        if response is None:
            return None
        payload: object = response.json()
        return payload

    def _get(self, path: str, **params: str | int) -> httpx.Response | None:
        """GET, or None for the statuses a real fleet legitimately produces."""
        response = self._client.get(path, params=params or None)
        if response.status_code in (401, 403, 404):
            log.info("%s -> %s", path, response.status_code)
            return None
        response.raise_for_status()
        return response

    def _text(self, path: str, **params: str | int) -> str | None:
        """GET returning the body as text, for the routes that serve a file.

        ``repository/files/.../raw`` answers with the file itself, so parsing it
        as JSON would fail on any ordinary YAML pointer.
        """
        response = self._get(path, **params)
        return response.text if response is not None else None

    def _paginate(self, path: str, **params: str | int) -> list[dict[str, Any]]:
        """Every item of a list endpoint, stopping at an empty or short page or page 10."""
        items: list[dict[str, Any]] = []
        page = 1
        while True:
            batch = self._json(path, per_page=_PER_PAGE, page=page, **params)
            if not isinstance(batch, list) or not batch:
                break
            items.extend(batch)
            if len(batch) < _PER_PAGE:
                break
            page += 1
            if page > 10:  # a fleet this size never needs more
                break
        return items

    # -- fleet-level -----------------------------------------------------

    def list_projects(self, fleet: tuple[str, ...]) -> list[dict[str, Any]]:
        """The projects named in the config, in the order they were listed.

        One call each, no group sweep - the same contract as
        ``github.GitHub.list_repos``, and for the same reason: the board's
        contents are decided by repos.yml and by nothing else.
        """
        projects: list[dict[str, Any]] = []
        seen: set[str] = set()

        for full_name in fleet:
            if "/" not in full_name or full_name in seen:
                continue
            raw = self._json(f"/projects/{_pid(full_name)}")
            if isinstance(raw, dict) and raw.get("path_with_namespace"):
                seen.add(raw["path_with_namespace"])
                projects.append(raw)
            else:
                log.warning(
                    "listed repo %s is not readable on GitLab - "
                    "check the path and the token's scopes",
                    full_name,
                )

        return projects

    def protected_branch(self, full_name: str, branch: str) -> dict[str, Any] | None:
        """The branch's protection entry, or None if it is not protected.

        Unlike GitHub's, this endpoint needs no admin rights, so there is no
        third "we were not allowed to look" state to model: a 404 here really
        does mean the branch is unprotected.
        """
        raw = self._json(f"/projects/{_pid(full_name)}/protected_branches/{quote(branch, safe='')}")
        return raw if isinstance(raw, dict) else None

    def branch_sha(self, full_name: str, branch: str) -> str:
        """The commit sha at the tip of ``branch``, or ``""`` if it cannot be read."""
        data = self._json(
            f"/projects/{_pid(full_name)}/repository/branches/{quote(branch, safe='')}"
        )
        if not isinstance(data, dict):
            return ""
        return ((data.get("commit") or {}).get("id")) or ""

    def template_ref(self, full_name: str, branch: str) -> str:
        """The ref pinned in the repo's template pointer, if it is managed."""
        pointer = quote(self._cfg.template_pointer, safe="")
        raw = self._text(
            f"/projects/{_pid(full_name)}/repository/files/{pointer}/raw",
            ref=branch,
        )
        if not raw:
            return ""
        try:
            data = yaml.safe_load(raw) or {}
        except yaml.YAMLError as exc:
            # Data, not a crash: a hand-edited pointer is one repo's problem and
            # must not take the rest of the refresh down with it.
            log.warning("%s has a malformed template pointer: %s", full_name, exc)
            return ""
        return str(data.get("ref") or "") if isinstance(data, dict) else ""

    def latest_pipeline(self, full_name: str, branch: str) -> dict[str, Any] | None:
        """The newest pipeline on *branch*, with its coverage.

        The listing carries a status but not coverage, so the one worth having
        is fetched in full. Two calls, and only for the newest pipeline.
        """
        listing = self._json(
            f"/projects/{_pid(full_name)}/pipelines",
            ref=branch,
            per_page=1,
            order_by="id",
            sort="desc",
        )
        if not isinstance(listing, list) or not listing:
            return None
        pipeline_id = listing[0].get("id")
        if pipeline_id is None:
            return None
        full = self._json(f"/projects/{_pid(full_name)}/pipelines/{pipeline_id}")
        return full if isinstance(full, dict) else listing[0]

    def pipeline_jobs(self, full_name: str, pipeline_id: int) -> list[dict[str, Any]]:
        """Every job in one pipeline.

        A GitLab pipeline is one run containing many jobs, where GitHub has many
        independent workflows. The job is the closest analogue to a workflow -
        it is the thing that is individually green or red and individually worth
        naming on the board - so one job becomes one `WorkflowRun`.
        """
        return self._paginate(
            f"/projects/{_pid(full_name)}/pipelines/{pipeline_id}/jobs",
            include_retried="false",
        )

    def head_pipeline_status(self, full_name: str, iid: int) -> str | None:
        """The status of the pipeline on one MR's head commit.

        A call per MR, which the listing looked like it would save: GitLab
        documents ``head_pipeline`` on the merge-request response, but only the
        *single* MR endpoint actually returns it - the listing omits it, and
        reading the absence as "no pipeline" reported every GitLab MR as having
        no checks at all. So this mirrors ``github.GitHub.checks_state``: one
        follow-up per MR, capped by ``max_prs_per_repo``.
        """
        raw = self._json(f"/projects/{_pid(full_name)}/merge_requests/{iid}")
        if not isinstance(raw, dict):
            return None
        pipeline = raw.get("head_pipeline") or raw.get("pipeline") or {}
        return pipeline.get("status") if isinstance(pipeline, dict) else None

    def open_merge_requests(self, full_name: str) -> tuple[int, list[PullRequest]]:
        """``(total open, the first N)`` as ``PullRequest`` records.

        The total is reported separately because the detail list is clipped -
        the tile stays right on a repo with an MR flood.
        """
        raw = self._paginate(
            f"/projects/{_pid(full_name)}/merge_requests",
            state="opened",
            order_by="updated_at",
            sort="desc",
        )
        total = len(raw)
        pulls: list[PullRequest] = []
        for item in raw[: self._cfg.max_prs_per_repo]:
            # iid, not id: iid is the number shown in the UI and in the MR's own
            # URL. id is globally unique and means nothing to anyone reading the
            # board.
            iid = int(item.get("iid") or 0)
            status = self.head_pipeline_status(full_name, iid) if iid else None
            pulls.append(
                PullRequest(
                    number=iid,
                    # Clipped like GitHub's, so one essay of a title cannot
                    # stretch the table's column past everything else.
                    title=(item.get("title") or "")[:120],
                    author=(item.get("author") or {}).get("username") or "unknown",
                    draft=bool(item.get("draft")),
                    created_at=ts(item.get("created_at")),
                    updated_at=ts(item.get("updated_at")),
                    checks=_checks_state(status),
                    url=item.get("web_url") or "",
                )
            )
        return total, pulls

    def recent_merges(self, full_name: str, limit: int) -> list[MergedPull]:
        """The ``limit`` most recently merged merge requests, newest first; any without a merge time are dropped."""
        raw = self._json(
            f"/projects/{_pid(full_name)}/merge_requests",
            state="merged",
            order_by="updated_at",
            sort="desc",
            per_page=limit,
        )
        if not isinstance(raw, list):
            return []
        merged = []
        for item in raw:
            at = ts(item.get("merged_at"))
            if not at:
                continue
            merged.append(
                MergedPull(
                    number=int(item.get("iid") or 0),
                    title=item.get("title") or "",
                    author=(item.get("author") or {}).get("username") or "",
                    merged_at=at,
                    url=item.get("web_url") or "",
                )
            )
        return merged


def _checks_state(status: str | None) -> str:
    """One MR's pipeline status in the vocabulary the board's PR table uses.

    That vocabulary is `success | failure | cancelled | pending | none`, which is
    GitHub's check-run summary rather than GitLab's pipeline status, so the
    in-flight states collapse to `pending` here rather than to the `stale` that
    `normalise_gitlab_status` gives a *completed* run.
    """
    if not status:
        return "none"
    status = status.strip().lower()
    if status in (
        "running",
        "pending",
        "created",
        "preparing",
        "waiting_for_resource",
        "scheduled",
    ):
        return "pending"
    if status == "success":
        return "success"
    if status in ("canceled", "canceling", "manual", "skipped"):
        return "cancelled"
    if status == "failed":
        return "failure"
    return "none"


def _visibility(raw: dict[str, Any]) -> str:
    """The project's visibility: ``public``, ``internal`` or ``private``.

    Named rather than inlined because ``public_only`` tests it too, and
    ``internal`` must fall on the private side of that test - it means "every
    signed-in user on the instance", which is not public but reads like it.
    """
    return raw.get("visibility") or "unknown"


def _protection(entry: dict[str, Any] | None) -> tuple[bool | None, bool, int]:
    """``(protected, allows_force_push, required_reviews)`` from one entry.

    ``protected`` is never None on GitLab. GitHub's third state exists because
    its protection endpoint 404s both for an unprotected branch and for a token
    without admin; GitLab's needs no special rights, so a 404 is unambiguous.

    ``required_reviews`` is always 0. MR approval rules are a Premium feature
    and the free tier has no equivalent to report, so `protected` carries the
    signal and the review count stays honestly empty.
    """
    if entry is None:
        return False, False, 0
    return True, bool(entry.get("allow_force_push")), 0


def collect(
    cfg: Config,
    ref_cache: dict[str, tuple[str, str]],
    coverage_cache: dict[str, tuple[int, tuple[float, int] | None]] | None = None,
    fleet: tuple[str, ...] = (),
    tags: list[str] | None = None,
) -> tuple[dict[str, RemoteRepo], GitLab, frozenset[str]]:
    """Build the GitLab part of the snapshot, keyed by ``namespace/name``.

    ``tags`` is the template repo's releases, fetched once by the GitHub
    collector and passed in - see this module's docstring. Without them drift is
    reported as unknown rather than as zero, which is the same thing an
    unmanaged repo reports and is the honest answer when the comparison could
    not be made.

    ``ref_cache`` maps ``full_name -> (default_branch_sha, ref)`` from the
    previous refresh, exactly as in ``github.collect``: the pointer cannot have
    changed if the branch head has not moved. ``coverage_cache`` is accepted and
    ignored, because coverage here is a field rather than a download.
    """
    tags = tags or []
    api = GitLab(cfg)

    listing = api.list_projects(fleet)
    excluded = frozenset(r["path_with_namespace"] for r in listing if _dropped(cfg, r))
    projects = [r for r in listing if r["path_with_namespace"] not in excluded]

    def one(raw: dict[str, Any]) -> RemoteRepo:
        return _remote_repo(api, cfg, raw, tags, ref_cache)

    return fan_out(projects, "path_with_namespace", one, log, _MAX_WORKERS), api, excluded


def _dropped(cfg: Config, raw: dict[str, Any]) -> bool:
    """Whether a listed project is left off the board - the same rules as GitHub's."""
    if raw.get("archived") and not cfg.include_archived:
        return True
    if cfg.is_ignored(*raw["path_with_namespace"].rsplit("/", 1)):
        return True
    return cfg.public_only and _visibility(raw) != "public"


def _remote_repo(
    api: GitLab,
    cfg: Config,
    raw: dict[str, Any],
    tags: list[str],
    ref_cache: dict[str, tuple[str, str]],
) -> RemoteRepo:
    """Everything the board shows about one project, one call per question."""
    full_name = raw["path_with_namespace"]
    branch = raw.get("default_branch") or "main"
    head_sha = api.branch_sha(full_name, branch)
    ref = cached_ref(ref_cache, full_name, head_sha, lambda: api.template_ref(full_name, branch))
    pipeline = api.latest_pipeline(full_name, branch) or {}
    workflows = _pipeline_runs(api, full_name, pipeline)
    protected, force_push, reviews = _protection(api.protected_branch(full_name, branch))
    pulls_total, pulls = api.open_merge_requests(full_name)
    merged = api.recent_merges(full_name, cfg.recent_merges_per_repo)

    return RemoteRepo(
        name=raw.get("path") or full_name.rsplit("/", 1)[-1],
        owner=full_name.rsplit("/", 1)[0],
        forge="gitlab",
        url=raw.get("web_url") or "",
        pulls_url=f"{raw['web_url']}/-/merge_requests" if raw.get("web_url") else "",
        default_branch=branch,
        visibility=_visibility(raw),
        archived=bool(raw.get("archived")),
        head_sha=head_sha,
        pushed_at=ts(raw.get("last_activity_at")),
        protected=protected,
        required_reviews=reviews,
        allows_force_push=force_push,
        # No counterpart at most tiers - see the module docstring.
        alerts_enabled=False,
        alerts=(),
        rhiza_managed=bool(ref),
        rhiza_ref=ref,
        rhiza_behind=behind_count(tags, ref),
        **ci_summary(workflows),
        workflows=workflows,
        coverage=_pipeline_coverage(pipeline),
        # Lines are not reported by GitLab's coverage field. Zero here means
        # the dashboard shows a percentage without a line count rather than
        # inventing one.
        coverage_lines=0,
        coverage_artifact=0,
        # Unlike GitHub's, GitLab's open_issues_count already excludes merge
        # requests, so there is nothing to subtract.
        open_issues=max(0, int(raw.get("open_issues_count") or 0)),
        open_pulls_total=pulls_total,
        pulls=tuple(pulls),
        merged=tuple(merged),
    )


def _pipeline_coverage(pipeline: dict[str, Any]) -> float | None:
    """The pipeline's coverage as a percentage, or None.

    GitLab reports coverage as a string percentage, and as null when the
    project has no coverage regex configured.

    >>> _pipeline_coverage({"coverage": "87.5"}), _pipeline_coverage({"coverage": None})
    (87.5, None)
    >>> _pipeline_coverage({"coverage": "n/a"}) is None
    True
    """
    coverage = pipeline.get("coverage")
    try:
        return float(coverage) if coverage is not None else None
    except (TypeError, ValueError):
        return None


def _pipeline_runs(
    api: GitLab, full_name: str, pipeline: dict[str, Any]
) -> tuple[WorkflowRun, ...]:
    """The newest pipeline's jobs as WorkflowRuns - none when there is no pipeline."""
    if pipeline.get("id") is None:
        return ()
    return tuple(
        WorkflowRun(
            name=job.get("name") or "unnamed",
            conclusion=normalise_gitlab_status(job.get("status") or ""),
            finished_at=ts(job.get("finished_at")),
            duration=float(job.get("duration") or 0.0),
            url=job.get("web_url") or "",
        )
        for job in api.pipeline_jobs(full_name, pipeline["id"])
    )
