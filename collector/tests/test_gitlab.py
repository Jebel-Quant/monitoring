"""What the GitLab collector derives from a payload.

Faked at `_json`/`_text`, so this drives the derivation rather than the HTTP
layer - test_gitlab_http.py does that half. The interesting cases are the ones
where GitLab's answer differs in shape from GitHub's and the board's contract has
to be preserved anyway: a pipeline of jobs standing in for N workflows, a status
vocabulary that spells `failed` where the history says `failure`, and a
nested namespace that must survive being put in a URL.
"""

from __future__ import annotations

import pytest
from conftest import job, project

from jq_collector import gitlab
from jq_collector.config import Config
from jq_collector.forge import normalise_gitlab_status


def config(**overrides) -> Config:
    cfg = Config()
    for key, value in overrides.items():
        object.__setattr__(cfg, key, value)
    return cfg


# -- the project path, which becomes a URL segment --------------------------
@pytest.mark.parametrize(
    ("full_name", "encoded"),
    [
        ("o/r", "o%2Fr"),
        # The case the last-two-segments parser used to get wrong.
        ("acme/platform/infra/web", "acme%2Fplatform%2Finfra%2Fweb"),
        # A dot is legal in a project path and must not be left to be read as a
        # path traversal or an extension.
        ("o/r.js", "o%2Fr.js"),
    ],
)
def test_a_project_path_is_url_encoded_whole(full_name, encoded):
    assert gitlab._pid(full_name) == encoded


def test_a_nested_namespace_is_asked_for_by_its_whole_path(make_gitlab):
    client = make_gitlab(
        {"/projects/acme%2Fplatform%2Finfra%2Fweb": project("acme/platform/infra/web")}
    )

    found = client.list_projects(("acme/platform/infra/web",))

    assert [p["path_with_namespace"] for p in found] == ["acme/platform/infra/web"]


def test_a_project_that_cannot_be_read_is_dropped_with_a_warning(make_gitlab, caplog):
    """One bad line in repos.yml must not blank the whole board."""
    client = make_gitlab({"/projects/o%2Fgood": project("o/good")})

    with caplog.at_level("WARNING"):
        found = client.list_projects(("o/good", "o/missing"))

    assert [p["path_with_namespace"] for p in found] == ["o/good"]
    assert "o/missing is not readable on GitLab" in caplog.text


def test_only_this_forges_share_is_asked_for(make_gitlab):
    """A GitHub repo in a mixed fleet must not be asked of GitLab, which would
    answer 404 and log it as unreadable."""
    client = make_gitlab({"/projects/o%2Fr": project()})

    client.list_projects(("o/r",))

    assert client.calls == ["/projects/o%2Fr"]


# -- timestamps -------------------------------------------------------------
@pytest.mark.parametrize(
    ("value", "expected_year"),
    [("2026-08-31T06:05:36.000Z", 2026), ("2026-08-31T06:05:36Z", 2026)],
)
def test_gitlab_timestamps_with_a_z_and_milliseconds_parse(value, expected_year):
    import datetime

    assert datetime.datetime.fromtimestamp(gitlab._ts(value), datetime.UTC).year == expected_year


@pytest.mark.parametrize("value", [None, "", "not a date"])
def test_an_unusable_timestamp_is_zero_not_a_crash(value):
    assert gitlab._ts(value) == 0.0


# -- the status vocabulary --------------------------------------------------
@pytest.mark.parametrize(
    ("gitlab_status", "conclusion"),
    [
        ("success", "success"),
        # GitLab spells it `failed`; the stored history and every dashboard
        # value mapping say `failure`.
        ("failed", "failure"),
        # One l, unlike GitHub's `cancelled`.
        ("canceled", "cancelled"),
        ("skipped", "skipped"),
        # A held deploy gate is not a failure. Counting it as red would make a
        # correctly configured pipeline permanently angry.
        ("manual", "cancelled"),
        # In flight: the board reports the last *completed* state, so a running
        # pipeline must not overwrite the verdict of the one before it.
        ("running", "stale"),
        ("pending", "stale"),
        # GitLab has added states before and will again. A new one appearing as
        # a fleet-wide red is worse than it appearing as "no verdict yet".
        ("some_future_state", "stale"),
        ("", "stale"),
    ],
)
def test_gitlab_statuses_are_normalised_to_the_boards_vocabulary(gitlab_status, conclusion):
    assert normalise_gitlab_status(gitlab_status) == conclusion


@pytest.mark.parametrize(
    ("status", "checks"),
    [
        ("success", "success"),
        ("failed", "failure"),
        ("running", "pending"),
        ("created", "pending"),
        ("canceled", "cancelled"),
        ("manual", "cancelled"),
        (None, "none"),
    ],
)
def test_an_mrs_pipeline_status_becomes_the_boards_check_state(status, checks):
    """The PR table's vocabulary is GitHub's check-run summary, where in-flight
    is `pending` - not the `stale` a *completed* run would normalise to."""
    assert gitlab._checks_state(status) == checks


# -- branch protection ------------------------------------------------------
def test_an_unprotected_branch_is_false_not_unknown():
    """GitHub needs a third state because its endpoint 404s both for an
    unprotected branch and for a token without admin. GitLab's needs no special
    rights, so a 404 here is unambiguous and `None` would understate it."""
    protected, force, reviews = gitlab._protection(None)

    assert protected is False
    assert force is False
    assert reviews == 0


def test_a_protected_branch_reports_its_force_push_setting():
    protected, force, reviews = gitlab._protection({"allow_force_push": True})

    assert protected is True
    assert force is True
    # MR approval rules are a Premium feature, so there is nothing to report.
    assert reviews == 0


# -- template drift ---------------------------------------------------------
def test_drift_is_measured_against_the_github_template_tags():
    """The template lives on GitHub whichever forge pins it, so a GitLab repo's
    drift is the same count against the same list."""
    tags = ["v2.0.0", "v1.9.0", "v1.8.0"]

    assert gitlab._behind_count(tags, "v2.0.0") == 0
    assert gitlab._behind_count(tags, "v1.8.0") == 2


@pytest.mark.parametrize("ref", ["", "main", "deadbeef", "v9.9.9"])
def test_a_ref_that_is_not_a_release_is_unknown_not_zero(ref):
    """Zero would read as "up to date", which is the opposite of "we cannot
    tell" for a repo pinned to a branch or a sha."""
    assert gitlab._behind_count(["v2.0.0", "v1.9.0"], ref) is None


def test_the_template_pointer_is_read_as_yaml_from_the_raw_route(make_gitlab):
    client = make_gitlab(
        {},
        texts={"/projects/o%2Fr/repository/files/.rhiza%2Ftemplate.yml/raw": "ref: v1.7.1\n"},
    )

    assert client.template_ref("o/r", "main") == "v1.7.1"


def test_a_malformed_pointer_is_data_not_a_crash(make_gitlab, caplog):
    """A hand-edited pointer is one repo's problem and must not take the rest of
    the refresh down with it."""
    client = make_gitlab(
        {},
        texts={"/projects/o%2Fr/repository/files/.rhiza%2Ftemplate.yml/raw": "ref: [unclosed"},
    )

    with caplog.at_level("WARNING"):
        assert client.template_ref("o/r", "main") == ""

    assert "malformed template pointer" in caplog.text


def test_a_repo_with_no_pointer_is_simply_unmanaged(make_gitlab):
    client = make_gitlab({}, texts={})

    assert client.template_ref("o/r", "main") == ""


# -- merge requests ---------------------------------------------------------
def test_an_mrs_checks_cost_a_follow_up_call(make_gitlab):
    """GitLab documents `head_pipeline` on the merge-request response, but only
    the *single* MR endpoint returns it - the listing omits the key entirely.
    Reading that absence as "no pipeline" reported every GitLab MR as
    unchecked, which is why this looks like GitHub's per-PR call."""
    client = make_gitlab(
        {
            "/projects/o%2Fr/merge_requests": [
                {
                    "iid": 7,
                    "title": "Fix the thing",
                    "author": {"username": "someone"},
                    "draft": False,
                    "created_at": "2026-08-30T10:00:00.000Z",
                    "updated_at": "2026-08-31T10:00:00.000Z",
                    "web_url": "https://gitlab.com/o/r/-/merge_requests/7",
                    # Deliberately present here and ignored: the real listing
                    # does not carry it, so trusting it would hide the bug.
                    "head_pipeline": {"status": "success"},
                }
            ],
            "/projects/o%2Fr/merge_requests/7": {"head_pipeline": {"status": "failed"}},
        }
    )

    total, pulls = client.open_merge_requests("o/r")

    assert total == 1
    assert pulls[0].number == 7, "iid is the number the UI and the URL use, not id"
    assert pulls[0].checks == "failure", "the listing's stale copy was trusted"
    assert pulls[0].url == "https://gitlab.com/o/r/-/merge_requests/7"
    assert client.calls == [
        "/projects/o%2Fr/merge_requests",
        "/projects/o%2Fr/merge_requests/7",
    ]


def test_an_mr_the_api_will_not_describe_has_no_checks(make_gitlab):
    """The follow-up 404s - a token that can list but not read one MR."""
    client = make_gitlab(
        {"/projects/o%2Fr/merge_requests": [{"iid": 1, "author": {}, "title": "x"}]}
    )

    _total, pulls = client.open_merge_requests("o/r")

    assert pulls[0].checks == "none"


def test_an_mr_with_no_pipeline_at_all_has_no_checks(make_gitlab):
    """A brand-new MR on a project with no CI, or before the first run."""
    client = make_gitlab(
        {
            "/projects/o%2Fr/merge_requests": [{"iid": 1, "author": {}, "title": "x"}],
            "/projects/o%2Fr/merge_requests/1": {"iid": 1, "head_pipeline": None},
        }
    )

    _total, pulls = client.open_merge_requests("o/r")

    assert pulls[0].checks == "none"


def test_an_mr_with_no_iid_is_not_looked_up(make_gitlab):
    """A malformed entry must not send a request to `/merge_requests/0`."""
    client = make_gitlab({"/projects/o%2Fr/merge_requests": [{"author": {}, "title": "x"}]})

    _total, pulls = client.open_merge_requests("o/r")

    assert pulls[0].checks == "none"
    assert client.calls == ["/projects/o%2Fr/merge_requests"]


def test_a_long_mr_title_is_clipped_like_githubs(make_gitlab):
    """One essay of a title must not stretch the table's column past the rest."""
    client = make_gitlab(
        {"/projects/o%2Fr/merge_requests": [{"iid": 1, "author": {}, "title": "x" * 300}]}
    )

    _total, pulls = client.open_merge_requests("o/r")

    assert len(pulls[0].title) == 120


def test_an_mr_with_no_author_is_unknown_not_blank(make_gitlab):
    client = make_gitlab({"/projects/o%2Fr/merge_requests": [{"iid": 1, "title": "x"}]})

    _total, pulls = client.open_merge_requests("o/r")

    assert pulls[0].author == "unknown"


def test_open_mrs_are_capped_but_the_total_is_not(make_gitlab, cfg):
    """The tile must stay right even on a repo with an MR flood."""
    object.__setattr__(cfg, "max_prs_per_repo", 2)
    client = make_gitlab(
        {
            "/projects/o%2Fr/merge_requests": [
                {"iid": n, "author": {}, "title": f"mr {n}"} for n in range(5)
            ]
        }
    )

    total, pulls = client.open_merge_requests("o/r")

    assert total == 5
    assert len(pulls) == 2


def test_an_unmerged_entry_is_left_out_of_the_merged_list(make_gitlab):
    """`merged_at` is what makes a MergedPull immutable; without one there is
    nothing to place it on the timeline with."""
    client = make_gitlab(
        {
            "/projects/o%2Fr/merge_requests": [
                {"iid": 1, "author": {}, "title": "landed", "merged_at": "2026-08-31T06:00:00Z"},
                {"iid": 2, "author": {}, "title": "closed unmerged", "merged_at": None},
            ]
        }
    )

    merged = client.recent_merges("o/r", 10)

    assert [m.number for m in merged] == [1]


# -- collect(): the whole derivation ---------------------------------------
def _stub(monkeypatch, **methods):
    """A GitLab whose methods are canned, installed over the constructor.

    A callable value is used as the method itself, so a test can count calls or
    raise; anything else becomes a method returning it. The default argument is
    what binds each value at definition time - a bare closure over the loop
    variable would give every method the last one.
    """

    class Stub:
        def close(self):
            pass

    for name, value in methods.items():
        if callable(value):
            setattr(Stub, name, value)
        else:
            setattr(Stub, name, lambda _s, *_a, _v=value, **_k: _v)
    monkeypatch.setattr(gitlab, "GitLab", lambda _cfg: Stub())
    return Stub


def test_one_job_becomes_one_workflow_row(monkeypatch):
    """A GitLab pipeline is one run of many jobs where GitHub has many
    independent workflows. The job is what is individually red and individually
    worth naming, so it takes the workflow's place on the board."""
    _stub(
        monkeypatch,
        list_projects=[project()],
        branch_sha="sha",
        template_ref="",
        latest_pipeline={"id": 5, "status": "failed", "coverage": None},
        pipeline_jobs=[
            job("lint", "failed", finished="2026-08-31T06:00:00Z"),
            job("test", "success", finished="2026-08-31T06:01:00Z"),
        ],
        protected_branch=None,
        open_merge_requests=(0, []),
        recent_merges=[],
    )

    remote, _api, _excluded = gitlab.collect(config(), {}, {}, fleet=("o/r",))

    repo = remote["o/r"]
    assert {w.name for w in repo.workflows} == {"lint", "test"}
    # The representative run is the failure, not whichever job finished last -
    # the same rule the GitHub collector applies.
    assert repo.ci_workflow == "lint"
    assert repo.ci_conclusion == "failure"


def test_a_green_pipeline_shows_its_most_recent_job(monkeypatch):
    _stub(
        monkeypatch,
        list_projects=[project()],
        branch_sha="sha",
        template_ref="",
        latest_pipeline={"id": 5, "status": "success", "coverage": "87.3"},
        pipeline_jobs=[
            job("build", "success", finished="2026-08-31T06:00:00Z"),
            job("test", "success", finished="2026-08-31T06:02:00Z"),
        ],
        protected_branch=None,
        open_merge_requests=(0, []),
        recent_merges=[],
    )

    remote, _api, _excluded = gitlab.collect(config(), {}, {}, fleet=("o/r",))

    assert remote["o/r"].ci_workflow == "test"
    assert remote["o/r"].ci_conclusion == "success"


def test_coverage_is_a_field_not_a_download(monkeypatch):
    """GitLab reports it as a string percentage on the pipeline, so there is no
    artifact to list and no zip to fetch."""
    _stub(
        monkeypatch,
        list_projects=[project()],
        branch_sha="sha",
        template_ref="",
        latest_pipeline={"id": 5, "status": "success", "coverage": "87.34"},
        pipeline_jobs=[],
        protected_branch=None,
        open_merge_requests=(0, []),
        recent_merges=[],
    )

    remote, _api, _excluded = gitlab.collect(config(), {}, {}, fleet=("o/r",))

    assert remote["o/r"].coverage == pytest.approx(87.34)
    # Lines are not reported, and zero says so rather than inventing a count.
    assert remote["o/r"].coverage_lines == 0
    assert remote["o/r"].coverage_artifact == 0


@pytest.mark.parametrize("raw", [None, "", "not a number"])
def test_a_project_with_no_coverage_regex_reports_none(monkeypatch, raw):
    """None means "publishes no coverage", which is not the same as zero."""
    _stub(
        monkeypatch,
        list_projects=[project()],
        branch_sha="sha",
        template_ref="",
        latest_pipeline={"id": 5, "status": "success", "coverage": raw},
        pipeline_jobs=[],
        protected_branch=None,
        open_merge_requests=(0, []),
        recent_merges=[],
    )

    remote, _api, _excluded = gitlab.collect(config(), {}, {}, fleet=("o/r",))

    assert remote["o/r"].coverage is None


def test_a_repo_with_no_pipeline_at_all_has_no_ci(monkeypatch):
    _stub(
        monkeypatch,
        list_projects=[project()],
        branch_sha="sha",
        template_ref="",
        latest_pipeline=None,
        pipeline_jobs=[],
        protected_branch=None,
        open_merge_requests=(0, []),
        recent_merges=[],
    )

    remote, _api, _excluded = gitlab.collect(config(), {}, {}, fleet=("o/r",))

    assert remote["o/r"].workflows == ()
    assert remote["o/r"].ci_conclusion == ""


def test_the_forge_and_url_come_from_the_project(monkeypatch):
    """The dashboard reads these instead of pasting the repo label onto a fixed
    github.com prefix, which is what makes a link correct on either forge."""
    _stub(
        monkeypatch,
        list_projects=[project("acme/platform/infra/web")],
        branch_sha="sha",
        template_ref="",
        latest_pipeline=None,
        pipeline_jobs=[],
        protected_branch=None,
        open_merge_requests=(0, []),
        recent_merges=[],
    )

    remote, _api, _excluded = gitlab.collect(config(), {}, {}, fleet=("acme/platform/infra/web",))

    repo = remote["acme/platform/infra/web"]
    assert repo.forge == "gitlab"
    assert repo.url == "https://gitlab.com/acme/platform/infra/web"
    # The owner is the whole nested namespace, not its first segment.
    assert repo.owner == "acme/platform/infra"
    assert repo.name == "web"


def test_dependabot_has_no_counterpart_and_says_so(monkeypatch):
    """GitLab's equivalent is Ultimate-tier. `alerts_enabled=False` renders as
    unknown rather than as a green "zero open alerts" tile - the distinction
    RemoteRepo keeps precisely so an unscanned repo is not mistaken for a clean
    one."""
    _stub(
        monkeypatch,
        list_projects=[project()],
        branch_sha="sha",
        template_ref="",
        latest_pipeline=None,
        pipeline_jobs=[],
        protected_branch=None,
        open_merge_requests=(0, []),
        recent_merges=[],
    )

    remote, _api, _excluded = gitlab.collect(config(), {}, {}, fleet=("o/r",))

    assert remote["o/r"].alerts_enabled is False
    assert remote["o/r"].alerts == ()


def test_open_issues_are_not_double_counted(monkeypatch):
    """GitLab's open_issues_count already excludes merge requests, unlike
    GitHub's, so there is nothing to subtract."""
    _stub(
        monkeypatch,
        list_projects=[project(open_issues_count=4)],
        branch_sha="sha",
        template_ref="",
        latest_pipeline=None,
        pipeline_jobs=[],
        protected_branch=None,
        open_merge_requests=(3, []),
        recent_merges=[],
    )

    remote, _api, _excluded = gitlab.collect(config(), {}, {}, fleet=("o/r",))

    assert remote["o/r"].open_issues == 4


def test_an_unchanged_branch_head_skips_the_pointer_read(monkeypatch):
    """The steady-state cost of drift correctness is zero extra calls, exactly
    as on the GitHub side."""
    reads: list[str] = []

    def counting_template_ref(_self, full_name, _branch):
        reads.append(full_name)
        return "v1.7.1"

    _stub(
        monkeypatch,
        list_projects=[project()],
        branch_sha="sha",
        template_ref=counting_template_ref,
        latest_pipeline=None,
        pipeline_jobs=[],
        protected_branch=None,
        open_merge_requests=(0, []),
        recent_merges=[],
    )

    cold, *_ = gitlab.collect(config(), {}, {}, fleet=("o/r",))
    assert reads == ["o/r"]
    assert cold["o/r"].rhiza_ref == "v1.7.1"

    warm, *_ = gitlab.collect(config(), {"o/r": ("sha", "v1.7.1")}, {}, fleet=("o/r",))
    assert reads == ["o/r"], "an unchanged head still cost a pointer read"
    assert warm["o/r"].rhiza_ref == "v1.7.1"


def test_one_failing_repo_does_not_sink_the_refresh(monkeypatch, caplog):
    def cursed_sha(_self, full_name, *_a):
        if full_name == "o/bad":
            raise RuntimeError("this repo is cursed")
        return "sha"

    _stub(
        monkeypatch,
        list_projects=[project("o/good"), project("o/bad")],
        branch_sha=cursed_sha,
        template_ref="",
        latest_pipeline=None,
        pipeline_jobs=[],
        protected_branch=None,
        open_merge_requests=(0, []),
        recent_merges=[],
    )

    remote, _api, _excluded = gitlab.collect(config(), {}, {}, fleet=("o/good", "o/bad"))

    assert set(remote) == {"o/good"}
    assert "o/bad failed" in caplog.text


@pytest.mark.parametrize(
    ("overrides", "cfg_overrides"),
    [
        ({"archived": True}, {}),
        # `internal` means every signed-in user on the instance - not public,
        # though it reads like it.
        ({"visibility": "internal"}, {"public_only": True}),
        ({"visibility": "private"}, {"public_only": True}),
    ],
)
def test_a_dropped_repo_is_reported_back_as_excluded(monkeypatch, overrides, cfg_overrides):
    """Reported back so the local scan skips its clone too - otherwise a
    checkout keeps a dropped repo on the board after the remote half has
    correctly stopped reporting it."""
    _stub(
        monkeypatch,
        list_projects=[project(**overrides)],
        branch_sha="sha",
        template_ref="",
        latest_pipeline=None,
        pipeline_jobs=[],
        protected_branch=None,
        open_merge_requests=(0, []),
        recent_merges=[],
    )

    remote, _api, excluded = gitlab.collect(config(**cfg_overrides), {}, {}, fleet=("o/r",))

    assert excluded == frozenset({"o/r"})
    assert remote == {}


def test_an_ignored_repo_is_excluded(monkeypatch):
    _stub(
        monkeypatch,
        list_projects=[project()],
        branch_sha="sha",
        template_ref="",
        latest_pipeline=None,
        pipeline_jobs=[],
        protected_branch=None,
        open_merge_requests=(0, []),
        recent_merges=[],
    )

    _remote, _api, excluded = gitlab.collect(config(ignore=("o/r",)), {}, {}, fleet=("o/r",))

    assert excluded == frozenset({"o/r"})


# -- the branches a healthy fleet never takes -------------------------------
def test_the_client_can_be_closed(make_gitlab):
    """`__main__` closes it in a `finally`, so a bad snapshot cannot leak a
    connection pool."""
    client = make_gitlab({})

    client.close()

    assert client._client.is_closed


@pytest.mark.parametrize("entry", ["no-slash", "o/r"])
def test_an_unusable_or_repeated_fleet_entry_is_skipped(make_gitlab, entry):
    """A bare name cannot address a project, and a repeat would cost a second
    call for a row that is already there."""
    client = make_gitlab({"/projects/o%2Fr": project()})

    found = client.list_projects(("o/r", entry))

    assert [p["path_with_namespace"] for p in found] == ["o/r"]
    assert client.calls.count("/projects/o%2Fr") == 1


def test_a_branch_the_api_will_not_describe_has_no_sha(make_gitlab):
    """An empty repository, or a default_branch that no longer exists."""
    client = make_gitlab({})

    assert client.branch_sha("o/r", "main") == ""


def test_a_branch_payload_with_no_commit_has_no_sha(make_gitlab):
    client = make_gitlab({"/projects/o%2Fr/repository/branches/main": {"name": "main"}})

    assert client.branch_sha("o/r", "main") == ""


def test_a_pipeline_listing_entry_with_no_id_is_no_pipeline(make_gitlab):
    """Without an id there is nothing to fetch in full, and a half-read pipeline
    would report a CI state nobody can click through to."""
    client = make_gitlab({"/projects/o%2Fr/pipelines": [{"status": "success"}]})

    assert client.latest_pipeline("o/r", "main") is None


def test_the_jobs_of_a_pipeline_are_paginated(make_gitlab):
    client = make_gitlab(
        {"/projects/o%2Fr/pipelines/77/jobs": [job("test", "success"), job("lint", "failed")]}
    )

    jobs = client.pipeline_jobs("o/r", 77)

    assert [j["name"] for j in jobs] == ["test", "lint"]
    assert client.calls == ["/projects/o%2Fr/pipelines/77/jobs"]


def test_a_merged_listing_the_api_will_not_give_is_empty(make_gitlab):
    client = make_gitlab({})

    assert client.recent_merges("o/r", 10) == []


def test_an_unknown_mr_pipeline_status_is_no_checks():
    """GitLab has added states before and will again; an unrecognised one must
    not render as a red check."""
    assert gitlab._checks_state("some_future_state") == "none"
