"""Turn a snapshot into Prometheus metric families.

This is a *custom collector*: it renders the cached snapshot at scrape time
rather than mutating long-lived gauges. That matters because the label sets here
churn - a merged PR, a deleted repo, a branch that goes away - and a registry of
persistent gauges would keep exporting those series forever.

The ``repo`` label is the full ``owner/name``. A bare name is only unique
within one owner, and the dashboard joins frames on this label and builds GitHub
links out of it - both of which need it to be unique on its own. ``owner`` is
carried on ``jq_repo_info`` alone, purely so the dashboard can offer it as a
filter, rather than as a duplicate column on every joined table.
"""

from __future__ import annotations

from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily

from .state import Snapshot

# Conclusions that mean "this pipeline did its job".
_GOOD_CONCLUSIONS = {"success", "neutral", "skipped"}


def _gauge(name: str, doc: str, labels: list[str] | None = None) -> GaugeMetricFamily:
    return GaugeMetricFamily(name, doc, labels=labels or [])


def render(snap: Snapshot):
    keys = sorted((set(snap.remote) | set(snap.local)) - snap.excluded)

    # -- collector health ------------------------------------------------
    last_success = _gauge(
        "jq_collector_last_success_timestamp_seconds",
        "Unix time of the last successful refresh, per source.",
        ["source"],
    )
    duration = _gauge(
        "jq_collector_refresh_duration_seconds",
        "Wall-clock seconds the last refresh took, per source.",
        ["source"],
    )
    errors = CounterMetricFamily(
        "jq_collector_errors",
        "Refreshes that raised, per source.",
        labels=["source"],
    )
    for source in ("github", "local"):
        health = snap.health.get(source)
        if health is None:
            continue
        last_success.add_metric([source], health.last_success)
        duration.add_metric([source], health.last_duration)
        errors.add_metric([source], health.errors)
    yield last_success
    yield duration
    yield errors

    rate_remaining = _gauge(
        "jq_github_rate_limit_remaining",
        "GitHub REST calls left in the current window.",
    )
    rate_limit = _gauge("jq_github_rate_limit_limit", "GitHub REST calls allowed per window.")
    rate_reset = _gauge(
        "jq_github_rate_limit_reset_timestamp_seconds",
        "Unix time the rate window resets.",
    )
    rate_remaining.add_metric([], snap.rate_limit_remaining)
    rate_limit.add_metric([], snap.rate_limit_limit)
    rate_reset.add_metric([], snap.rate_limit_reset)
    yield rate_remaining
    yield rate_limit
    yield rate_reset

    latest = _gauge(
        "jq_template_latest_release_info",
        "Always 1; the label carries the newest published template release.",
        ["ref"],
    )
    if snap.latest_template_ref:
        latest.add_metric([snap.latest_template_ref], 1)
    yield latest

    # -- identity --------------------------------------------------------
    repo_info = _gauge(
        "jq_repo_info",
        "Always 1; labels carry the repo's identity for joining.",
        ["repo", "owner", "default_branch", "visibility"],
    )
    cloned = _gauge(
        "jq_repo_cloned",
        "1 if the repo has a working copy on this machine.",
        ["repo"],
    )
    pushed = _gauge(
        "jq_repo_last_push_timestamp_seconds",
        "Unix time of the last push to GitHub.",
        ["repo"],
    )

    # -- template drift --------------------------------------------------
    managed = _gauge(
        "jq_rhiza_managed",
        "1 if the repo carries a template pointer.",
        ["repo"],
    )
    ref_info = _gauge(
        "jq_rhiza_template_ref_info",
        "Always 1; the label carries the pinned template ref.",
        ["repo", "ref"],
    )
    behind = _gauge(
        "jq_rhiza_releases_behind",
        "Template releases published after the pinned ref. Absent when the ref is not a release.",
        ["repo"],
    )

    # -- CI on the default branch ----------------------------------------
    ci_info = _gauge(
        "jq_ci_last_run_info",
        "Always 1; labels carry the last completed default-branch run.",
        ["repo", "conclusion", "workflow"],
    )
    ci_ok = _gauge(
        "jq_ci_last_run_success",
        "1 if the last completed default-branch run passed.",
        ["repo"],
    )
    ci_at = _gauge(
        "jq_ci_last_run_timestamp_seconds",
        "Unix time that run finished.",
        ["repo"],
    )
    ci_dur = _gauge("jq_ci_last_run_duration_seconds", "How long that run took.", ["repo"])
    wf_ok = _gauge(
        "jq_ci_workflow_success",
        "Per workflow: 1 if its latest completed default-branch run passed.",
        ["repo", "workflow"],
    )
    wf_at = _gauge(
        "jq_ci_workflow_timestamp_seconds",
        "Per workflow: when that run finished.",
        ["repo", "workflow"],
    )
    wf_failing = _gauge(
        "jq_ci_workflows_failing",
        "How many of the repo's workflows are red on the default branch.",
        ["repo"],
    )

    # -- pull requests ---------------------------------------------------
    pr_count = _gauge("jq_open_pull_requests", "Open pull requests.", ["repo"])
    issue_count = _gauge(
        "jq_open_issues",
        "Open issues, excluding pull requests.",
        ["repo"],
    )
    pr_failing = _gauge(
        "jq_open_pull_requests_failing",
        "Open pull requests whose checks are red.",
        ["repo"],
    )
    pr_info = _gauge(
        "jq_pull_request_info",
        "Always 1; one series per open pull request.",
        ["repo", "number", "title", "author", "checks", "draft"],
    )
    merged_at = _gauge(
        "jq_merged_pull_request_timestamp_seconds",
        "Unix time a pull request was merged. topk() over this gives the newest.",
        ["repo", "number", "title", "author"],
    )
    pr_created = _gauge(
        "jq_pull_request_created_timestamp_seconds",
        "Unix time the pull request was opened.",
        ["repo", "number"],
    )

    # -- local working copies --------------------------------------------
    local_branch = _gauge(
        "jq_local_branch_info",
        "Always 1; the label carries the checked-out branch.",
        ["repo", "branch"],
    )
    on_default = _gauge(
        "jq_local_on_default_branch",
        "1 if the clone sits on its default branch.",
        ["repo"],
    )
    dirty = _gauge(
        "jq_local_dirty_files",
        "Tracked files with uncommitted changes.",
        ["repo"],
    )
    untracked = _gauge(
        "jq_local_untracked_files",
        "Untracked files in the working copy.",
        ["repo"],
    )
    ahead = _gauge(
        "jq_local_ahead_commits",
        "Commits ahead of upstream, as of the last fetch.",
        ["repo"],
    )
    behind_local = _gauge(
        "jq_local_behind_commits",
        "Commits behind upstream, as of the last fetch.",
        ["repo"],
    )
    stashes = _gauge("jq_local_stash_entries", "Stash entries.", ["repo"])
    last_commit = _gauge(
        "jq_local_last_commit_timestamp_seconds",
        "Unix time of HEAD's commit.",
        ["repo"],
    )
    fetch_age = _gauge(
        "jq_local_fetch_age_seconds",
        "Seconds since this clone last fetched. Read the ahead/behind counts against this.",
        ["repo"],
    )
    local_ref = _gauge(
        "jq_local_template_ref_info",
        "Always 1; the ref pinned in the *clone's* pointer. May lag the repo's.",
        ["repo", "ref"],
    )
    synced = _gauge(
        "jq_local_default_branch_synced",
        "1 if the local default branch is the same commit GitHub reports. Fetch-independent.",
        ["repo"],
    )

    for key in keys:
        remote = snap.remote.get(key)
        local = snap.local.get(key)
        owner = key.partition("/")[0]
        ident = [key]

        default_branch = remote.default_branch if remote else "main"
        repo_info.add_metric(
            [key, owner, default_branch, remote.visibility if remote else "unknown"], 1
        )
        cloned.add_metric(ident, 1 if local else 0)

        if remote is not None:
            pushed.add_metric(ident, remote.pushed_at)
            managed.add_metric(ident, 1 if remote.rhiza_managed else 0)
            if remote.rhiza_ref:
                ref_info.add_metric([*ident, remote.rhiza_ref], 1)
            if remote.rhiza_behind is not None:
                behind.add_metric(ident, remote.rhiza_behind)

            # Collapse workflows sharing a name, newest run winning. github.py
            # already guarantees one per name, but this layer owns the exposition
            # contract: duplicate label sets are silently dropped by Prometheus
            # ("samples with different value but same timestamp"), which cost 16
            # samples a scrape when the invariant was last broken upstream.
            unique: dict[str, object] = {}
            for wf in sorted(remote.workflows, key=lambda w: w.finished_at, reverse=True):
                if wf.conclusion and wf.name not in unique:
                    unique[wf.name] = wf

            bad = 0
            for wf in unique.values():
                good = wf.conclusion in _GOOD_CONCLUSIONS
                bad += 0 if good else 1
                wf_ok.add_metric([*ident, wf.name], 1 if good else 0)
                wf_at.add_metric([*ident, wf.name], wf.finished_at)

            if remote.ci_conclusion:
                ci_info.add_metric([*ident, remote.ci_conclusion, remote.ci_workflow], 1)
                # Green only when no workflow is red. Deriving this from a single
                # run made a repo look green whenever some other workflow had run
                # more recently than the failing one.
                ci_ok.add_metric(ident, 1 if bad == 0 else 0)
                ci_at.add_metric(ident, remote.ci_finished_at)
                ci_dur.add_metric(ident, remote.ci_duration)
                wf_failing.add_metric(ident, bad)

            pr_count.add_metric(ident, remote.open_pulls_total)
            issue_count.add_metric(ident, remote.open_issues)
            pr_failing.add_metric(
                ident,
                sum(1 for p in remote.pulls if p.checks in ("failure", "cancelled")),
            )
            for pull in remote.pulls:
                number = str(pull.number)
                pr_info.add_metric(
                    [
                        *ident,
                        number,
                        pull.title,
                        pull.author,
                        pull.checks,
                        str(pull.draft).lower(),
                    ],
                    1,
                )
                pr_created.add_metric([*ident, number], pull.created_at)

            # One series per recently merged PR. The value is the merge time, so
            # the board can take topk() across the fleet rather than needing a
            # per-repo view. Deduped on number because a repo occasionally
            # reports the same PR twice across a page boundary.
            seen: set[int] = set()
            for m in remote.merged:
                if m.number in seen:
                    continue
                seen.add(m.number)
                merged_at.add_metric([*ident, str(m.number), m.title, m.author], m.merged_at)

        if local is not None:
            local_branch.add_metric([*ident, local.branch or "unknown"], 1)
            on_default.add_metric(ident, 1 if local.branch == default_branch else 0)
            dirty.add_metric(ident, local.dirty_files)
            untracked.add_metric(ident, local.untracked_files)
            if local.ahead is not None:
                ahead.add_metric(ident, local.ahead)
            if local.behind is not None:
                behind_local.add_metric(ident, local.behind)
            if local.rhiza_ref:
                local_ref.add_metric([*ident, local.rhiza_ref], 1)
            stashes.add_metric(ident, local.stashes)
            last_commit.add_metric(ident, local.last_commit_at)
            if local.fetch_age is not None:
                fetch_age.add_metric(ident, local.fetch_age)
            if local.default_branch_sha and remote and remote.head_sha:
                synced.add_metric(ident, 1 if local.default_branch_sha == remote.head_sha else 0)

    yield from (
        repo_info,
        cloned,
        pushed,
        managed,
        ref_info,
        behind,
        ci_info,
        ci_ok,
        ci_at,
        ci_dur,
        wf_ok,
        wf_at,
        wf_failing,
        pr_count,
        issue_count,
        pr_failing,
        pr_info,
        pr_created,
        merged_at,
        local_branch,
        on_default,
        dirty,
        untracked,
        ahead,
        behind_local,
        stashes,
        last_commit,
        fetch_age,
        local_ref,
        synced,
    )


class FleetCollector:
    """Adapts the store to prometheus_client's collector interface."""

    def __init__(self, store) -> None:
        self._store = store

    def collect(self):
        yield from render(self._store.snapshot())
