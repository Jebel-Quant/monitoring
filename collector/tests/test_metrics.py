"""The exposition must be valid and must not overstate health."""

from __future__ import annotations

import collections

from prometheus_client import CollectorRegistry, generate_latest

from jq_collector.metrics import FleetCollector
from jq_collector.state import (
    LocalRepo,
    PullRequest,
    RemoteRepo,
    Snapshot,
    SourceHealth,
    Store,
    WorkflowRun,
)


def expose(snapshot: Snapshot) -> list[str]:
    store = Store()
    store.update(
        **{f.name: getattr(snapshot, f.name) for f in snapshot.__dataclass_fields__.values()}
    )
    reg = CollectorRegistry()
    reg.register(FleetCollector(store))
    return [
        ln for ln in generate_latest(reg).decode().splitlines() if ln and not ln.startswith("#")
    ]


def repo(name: str, *workflows: WorkflowRun, conclusion: str = "success") -> RemoteRepo:
    return RemoteRepo(
        name=name.split("/")[-1],
        owner=name.split("/")[0],
        ci_conclusion=conclusion,
        ci_workflow="x",
        workflows=tuple(workflows),
    )


def pull(number: int, checks: str) -> PullRequest:
    return PullRequest(
        number=number,
        title=f"pr {number}",
        author="u",
        draft=False,
        created_at=1.0,
        updated_at=1.0,
        checks=checks,
        url="",
    )


def wf(name: str, conclusion: str) -> WorkflowRun:
    return WorkflowRun(name=name, conclusion=conclusion, finished_at=1.0, duration=1.0, url="")


def test_repo_is_green_only_when_every_workflow_is():
    """Deriving health from one run reported green while another job was red."""
    snap = Snapshot(remote={"o/r": repo("o/r", wf("CI", "success"), wf("Docs", "failure"))})
    line = next(x for x in expose(snap) if x.startswith("jq_ci_last_run_success"))
    assert line.endswith(" 0.0"), line


def test_repo_green_when_all_pass():
    snap = Snapshot(remote={"o/r": repo("o/r", wf("CI", "success"), wf("Docs", "skipped"))})
    line = next(x for x in expose(snap) if x.startswith("jq_ci_last_run_success"))
    assert line.endswith(" 1.0"), line


def test_failing_workflow_count_matches_the_red_workflows():
    snap = Snapshot(
        remote={"o/r": repo("o/r", wf("A", "failure"), wf("B", "failure"), wf("C", "success"))}
    )
    lines = expose(snap)
    count = next(x for x in lines if x.startswith("jq_ci_workflows_failing"))
    reds = [x for x in lines if x.startswith("jq_ci_workflow_success") and x.endswith(" 0.0")]
    assert count.endswith(" 2.0")
    assert len(reds) == 2


def test_no_duplicate_label_sets_anywhere():
    """Prometheus drops samples that share a timestamp and label set, silently."""
    snap = Snapshot(
        remote={"o/r": repo("o/r", wf("CI", "success"), wf("CI", "failure"))},
        local={"o/r": LocalRepo(name="r", owner="o", path="/tmp/r", branch="main")},
    )
    lines = expose(snap)
    keyed = [ln.rsplit(" ", 1)[0] for ln in lines]
    dupes = [k for k, n in collections.Counter(keyed).items() if n > 1]
    assert not dupes, f"duplicate label sets exported: {dupes}"


def test_excluded_repos_produce_no_series():
    """A clone of an archived, ignored or private repo must not resurrect it."""
    snap = Snapshot(
        remote={"o/keep": repo("o/keep", wf("CI", "success"))},
        local={"o/drop": LocalRepo(name="drop", owner="o", path="/tmp/d", branch="main")},
        excluded=frozenset({"o/drop"}),
    )
    lines = expose(snap)
    assert not [ln for ln in lines if "o/drop" in ln], "excluded repo leaked into the exposition"
    assert [ln for ln in lines if "o/keep" in ln]


def test_merged_pull_requests_are_one_series_each_keyed_on_merge_time():
    """The board takes topk() over the value, so it must be the merge time."""
    from jq_collector.state import MergedPull

    snap = Snapshot(
        remote={
            "o/r": RemoteRepo(
                name="r",
                owner="o",
                ci_conclusion="success",
                workflows=(wf("CI", "success"),),
                merged=(
                    MergedPull(
                        number=7, title="add thing", author="tschm", merged_at=100.0, url=""
                    ),
                    MergedPull(number=8, title="fix thing", author="bot", merged_at=200.0, url=""),
                ),
            )
        }
    )
    lines = [x for x in expose(snap) if x.startswith("jq_merged_pull_request_timestamp_seconds")]
    assert len(lines) == 2
    assert any('number="8"' in x and x.endswith(" 200.0") for x in lines)


def test_a_merged_pr_reported_twice_yields_one_series():
    """Duplicate label sets are dropped silently by Prometheus."""
    from jq_collector.state import MergedPull

    dupe = MergedPull(number=7, title="add thing", author="tschm", merged_at=100.0, url="")
    snap = Snapshot(
        remote={
            "o/r": RemoteRepo(
                name="r",
                owner="o",
                ci_conclusion="success",
                workflows=(wf("CI", "success"),),
                merged=(dupe, dupe),
            )
        }
    )
    lines = [x for x in expose(snap) if x.startswith("jq_merged_pull_request_timestamp_seconds")]
    assert len(lines) == 1


def test_protection_is_absent_rather_than_zero_when_unknown():
    """A token without admin 404s exactly like an unprotected branch.

    Exporting 0 there would put every repo on the "unprotected" list on the
    strength of a permission gap, which is a finding the board invented.
    """
    lines = expose(Snapshot(remote={"o/r": RemoteRepo(name="r", owner="o", protected=None)}))
    assert not [ln for ln in lines if ln.startswith("jq_branch_protected")]


def test_protection_details_are_exported_when_known():
    lines = expose(
        Snapshot(
            remote={
                "o/r": RemoteRepo(
                    name="r",
                    owner="o",
                    protected=True,
                    required_reviews=1,
                    allows_force_push=True,
                )
            }
        )
    )
    assert 'jq_branch_protected{repo="o/r"} 1.0' in lines
    assert 'jq_branch_required_reviews{repo="o/r"} 1.0' in lines
    # Protected but still force-pushable is the interesting case: the tile
    # would otherwise read as green on a branch anyone can rewrite.
    assert 'jq_branch_allows_force_push{repo="o/r"} 1.0' in lines


def test_disabled_dependabot_is_not_reported_as_zero_alerts():
    """ "No alerts" and "nobody is looking" must not render as the same tile."""
    lines = expose(Snapshot(remote={"o/r": RemoteRepo(name="r", owner="o", alerts_enabled=False)}))
    assert 'jq_dependabot_alerts_enabled{repo="o/r"} 0.0' in lines
    assert not [ln for ln in lines if ln.startswith("jq_dependabot_open_alerts")]


def test_open_alerts_zero_fill_the_known_severities():
    """A cleared severity must report 0, not vanish and strand its last value."""
    lines = expose(
        Snapshot(
            remote={
                "o/r": RemoteRepo(name="r", owner="o", alerts_enabled=True, alerts=(("high", 3),))
            }
        )
    )
    assert 'jq_dependabot_open_alerts{repo="o/r",severity="high"} 3.0' in lines
    assert 'jq_dependabot_open_alerts{repo="o/r",severity="critical"} 0.0' in lines


def test_unprotected_is_a_fact_not_a_gap():
    """GitHub says "Branch not protected" outright; that must reach the board.

    Reporting it as unknown would leave the unprotected repos - here, nearly
    the whole fleet - invisible on the one metric that exists to show them.
    """
    lines = expose(Snapshot(remote={"o/r": RemoteRepo(name="r", owner="o", protected=False)}))
    assert 'jq_branch_protected{repo="o/r"} 0.0' in lines


def test_cancelled_workflow_is_not_counted_as_failing():
    """A cancelled run is no verdict: it must not turn the repo red, and it must
    not appear in the failing-workflows table."""
    snap = Snapshot(remote={"o/r": repo("o/r", wf("CI", "success"), wf("Docs", "cancelled"))})
    lines = expose(snap)
    ok = next(x for x in lines if x.startswith("jq_ci_last_run_success"))
    count = next(x for x in lines if x.startswith("jq_ci_workflows_failing"))
    assert ok.endswith(" 1.0"), ok
    assert count.endswith(" 0.0"), count
    assert not [x for x in lines if x.startswith("jq_ci_workflow_success") and "Docs" in x]


def test_repo_whose_only_run_was_cancelled_reports_no_ci_state():
    """Absent means "nothing to judge" - neither green nor red."""
    snap = Snapshot(remote={"o/r": repo("o/r", wf("CI", "cancelled"), conclusion="cancelled")})
    lines = expose(snap)
    assert not [x for x in lines if x.startswith("jq_ci_last_run")]


def test_pull_request_with_cancelled_checks_is_not_red():
    """Same rule as the default branch: a stopped check is no verdict."""
    snap = Snapshot(
        remote={
            "o/r": RemoteRepo(
                name="r",
                owner="o",
                ci_conclusion="success",
                ci_workflow="x",
                pulls=(pull(1, "cancelled"), pull(2, "failure")),
            )
        }
    )
    line = next(x for x in expose(snap) if x.startswith("jq_open_pull_requests_failing"))
    assert line.endswith(" 1.0"), line


# -- every optional field, present ------------------------------------------
#
# Most of the exposition is guarded: a field that GitHub or git would not say is
# left out rather than exported as zero, and the tests above pin the absent
# side of each of those guards. This pins the other side. It is one fixture
# rather than a test per field because the risk being covered is uniform - a
# guard whose body was never executed - and twelve near-identical tests would
# obscure that rather than sharpen it.


def full_remote() -> RemoteRepo:
    return RemoteRepo(
        name="rhiza",
        owner="Jebel-Quant",
        default_branch="main",
        visibility="public",
        head_sha="abc123",
        pushed_at=1000.0,
        protected=True,
        required_reviews=2,
        allows_force_push=False,
        alerts_enabled=True,
        # `moderate` is not one of the four zero-filled severities, so it takes
        # the sorted-remainder branch.
        alerts=(("critical", 1), ("moderate", 3)),
        rhiza_managed=True,
        rhiza_ref="v1.7.1",
        rhiza_behind=2,
        coverage=87.3,
        coverage_lines=472,
        coverage_artifact=99,
        ci_conclusion="success",
        ci_workflow="ci",
        ci_finished_at=900.0,
        ci_duration=42.0,
        workflows=(wf("ci", "success"),),
        open_issues=4,
        open_pulls_total=1,
        pulls=(pull(7, "success"),),
    )


def full_local() -> LocalRepo:
    return LocalRepo(
        name="rhiza",
        path="/repos/Jebel-Quant/rhiza",
        owner="Jebel-Quant",
        branch="main",
        dirty_files=1,
        untracked_files=2,
        ahead=3,
        behind=4,
        stashes=5,
        last_commit_at=800.0,
        fetch_age=60.0,
        head_sha="abc123",
        default_branch_sha="abc123",
        rhiza_ref="v1.7.0",
        code_lines=1477,
        test_lines=15186,
        commits_30d=124,
        commits_since_release=0,
        last_release="v1.7.1",
    )


def test_every_optional_field_is_exported_when_present():
    lines = expose(
        Snapshot(
            remote={"Jebel-Quant/rhiza": full_remote()},
            local={"Jebel-Quant/rhiza": full_local()},
            latest_template_ref="v1.7.1",
            health={"github": SourceHealth(last_success=1.0, last_duration=2.0, errors=3)},
        )
    )
    body = "\n".join(lines)

    for metric in (
        "jq_template_latest_release_info",
        "jq_collector_last_success_timestamp_seconds",
        "jq_collector_refresh_duration_seconds",
        "jq_rhiza_template_ref_info",
        "jq_rhiza_releases_behind",
        "jq_ci_coverage_percent",
        "jq_ci_coverage_lines",
        "jq_local_ahead_commits",
        "jq_local_behind_commits",
        "jq_local_template_ref_info",
        "jq_local_fetch_age_seconds",
        "jq_local_default_branch_synced",
        "jq_local_commits_since_release",
        "jq_local_last_release_info",
    ):
        assert any(ln.startswith(metric) for ln in lines), f"{metric} missing:\n{body}"

    # The remainder severity is exported after the four zero-filled ones.
    assert 'jq_dependabot_open_alerts{repo="Jebel-Quant/rhiza",severity="moderate"} 3.0' in body
    # In sync: the clone's default-branch sha matches what GitHub reports.
    assert 'jq_local_default_branch_synced{repo="Jebel-Quant/rhiza"} 1.0' in body


def test_a_source_that_has_never_refreshed_reports_no_health():
    """`health` is keyed per source; the absent one must not export a zero."""
    lines = expose(Snapshot(health={"github": SourceHealth(last_success=1.0)}))

    assert any('source="github"' in ln for ln in lines)
    assert not any('source="local"' in ln for ln in lines)


# -- the forge and the URLs, which live on the identity metrics only ----------
#
# Deliberately only there. Adding a label to a metric family starts a new series
# and orphans the old one, and the thirty families keyed on `repo` alone are the
# history behind every trend panel and alert rule. The `*_info` metrics are
# `Always 1` join targets whose label sets churn anyway, so they are where a new
# dimension costs nothing.


def test_the_forge_and_url_ride_on_the_identity_metric():
    snap = Snapshot(
        remote={
            "acme/platform/web": RemoteRepo(
                name="web",
                owner="acme/platform",
                forge="gitlab",
                url="https://gitlab.com/acme/platform/web",
                visibility="private",
            )
        }
    )

    line = next(x for x in expose(snap) if x.startswith("jq_repo_info"))

    assert 'forge="gitlab"' in line
    assert 'repo_url="https://gitlab.com/acme/platform/web"' in line
    # The owner is the whole nested namespace, not its first segment.
    assert 'owner="acme/platform"' in line


def test_a_repo_with_no_remote_half_still_gets_an_identity_row():
    """A clone of a repo the forge could not be read for. The row must exist -
    the dashboard joins on it - and must not claim a forge it does not know."""
    snap = Snapshot(local={"o/r": LocalRepo(name="r", path="/p")})

    line = next(x for x in expose(snap) if x.startswith("jq_repo_info"))

    assert 'forge="github"' in line
    assert 'repo_url=""' in line


def test_no_other_family_gained_a_forge_label():
    """The history-preserving half of that decision, pinned. A `forge` label
    leaking onto a `["repo"]` family would silently orphan 180 days of it."""
    snap = Snapshot(
        remote={"o/r": RemoteRepo(name="r", owner="o", forge="gitlab", pushed_at=1.0)},
        local={"o/r": LocalRepo(name="r", path="/p", dirty_files=2)},
    )

    carrying = [x.split("{")[0] for x in expose(snap) if 'forge="' in x]

    assert carrying == ["jq_repo_info"]


def test_a_pull_requests_url_is_exposed_rather_than_rebuilt():
    """The dashboard used to paste the repo label onto `https://github.com/` and
    append the number. That cannot be right for both forges, and never had to be
    - the API hands the URL over."""
    snap = Snapshot(
        remote={
            "acme/web": RemoteRepo(
                name="web",
                owner="acme",
                forge="gitlab",
                pulls=(
                    PullRequest(
                        number=7,
                        title="Fix it",
                        author="someone",
                        draft=False,
                        created_at=1.0,
                        updated_at=2.0,
                        checks="success",
                        url="https://gitlab.com/acme/web/-/merge_requests/7",
                    ),
                ),
            )
        }
    )

    line = next(x for x in expose(snap) if x.startswith("jq_pull_request_info"))

    assert 'url="https://gitlab.com/acme/web/-/merge_requests/7"' in line


def test_the_merged_timeline_itself_gains_no_label():
    """That timestamp *is* the merged-PR history the panel reads, so the URL
    goes on a sibling family. A label here would have orphaned every existing
    series and, until they went stale, shown each merged PR twice."""
    from jq_collector.state import MergedPull

    snap = Snapshot(
        remote={
            "o/r": RemoteRepo(
                name="r",
                owner="o",
                merged=(
                    MergedPull(
                        number=4,
                        title="Landed",
                        author="someone",
                        merged_at=100.0,
                        url="https://github.com/o/r/pull/4",
                    ),
                ),
            )
        }
    )

    line = next(x for x in expose(snap) if x.startswith("jq_merged_pull_request_timestamp_seconds"))

    assert "url=" not in line


def test_a_merged_pull_requests_url_is_exposed_too():
    from jq_collector.state import MergedPull

    snap = Snapshot(
        remote={
            "acme/web": RemoteRepo(
                name="web",
                owner="acme",
                merged=(
                    MergedPull(
                        number=4,
                        title="Landed",
                        author="someone",
                        merged_at=100.0,
                        url="https://gitlab.com/acme/web/-/merge_requests/4",
                    ),
                ),
            )
        }
    )

    line = next(x for x in expose(snap) if x.startswith("jq_merged_pull_request_info"))

    assert 'url="https://gitlab.com/acme/web/-/merge_requests/4"' in line


def test_health_is_exposed_for_whatever_reported():
    """The source list used to be a hardcoded ("github", "local") tuple, which
    would have dropped gitlab's health silently."""
    snap = Snapshot(
        health={
            "github": SourceHealth(last_success=1.0),
            "gitlab": SourceHealth(last_success=2.0, errors=3),
            "local": SourceHealth(last_success=3.0),
        }
    )

    lines = [x for x in expose(snap) if x.startswith("jq_collector_last_success")]

    assert {'source="github"', 'source="gitlab"', 'source="local"'} == {
        x.split("{")[1].split("}")[0] for x in lines
    }


def test_the_pull_request_listing_url_is_carried_per_forge():
    """No API reports this one, so each collector builds it - GitHub spells it
    /pulls and GitLab /-/merge_requests. The dashboard reads the label rather
    than learning either convention."""
    snap = Snapshot(
        remote={
            "acme/web": RemoteRepo(
                name="web",
                owner="acme",
                forge="gitlab",
                url="https://gitlab.com/acme/web",
                pulls_url="https://gitlab.com/acme/web/-/merge_requests",
            )
        }
    )

    line = next(x for x in expose(snap) if x.startswith("jq_repo_info"))

    assert 'pulls_url="https://gitlab.com/acme/web/-/merge_requests"' in line


def test_the_ci_run_url_is_exposed_on_the_info_metric():
    """Linking to the run itself, not to a listing page whose path differs per
    forge - and strictly more useful than the /actions link it replaces."""
    snap = Snapshot(
        remote={
            "acme/web": RemoteRepo(
                name="web",
                owner="acme",
                ci_conclusion="failure",
                ci_workflow="test",
                ci_url="https://gitlab.com/acme/web/-/jobs/9",
            )
        }
    )

    line = next(x for x in expose(snap) if x.startswith("jq_ci_last_run_info"))

    assert 'url="https://gitlab.com/acme/web/-/jobs/9"' in line


def test_each_workflow_carries_its_own_run_url():
    """A separate family from jq_ci_workflow_success, which is a real gauge with
    history - a label there would have orphaned all of it."""
    snap = Snapshot(
        remote={
            "acme/web": RemoteRepo(
                name="web",
                owner="acme",
                workflows=(
                    WorkflowRun(
                        name="lint",
                        conclusion="failure",
                        finished_at=10.0,
                        duration=1.0,
                        url="https://gitlab.com/acme/web/-/jobs/1",
                    ),
                ),
            )
        }
    )

    lines = [x for x in expose(snap) if x.startswith("jq_ci_workflow_info")]

    assert len(lines) == 1
    assert 'workflow="lint"' in lines[0]
    assert 'url="https://gitlab.com/acme/web/-/jobs/1"' in lines[0]


def test_the_gauge_families_behind_the_history_carry_no_url():
    """The other half of the label decision. A `url` label leaking onto a
    `["repo"]` gauge would silently orphan 180 days of its series."""
    snap = Snapshot(
        remote={
            "o/r": RemoteRepo(
                name="r",
                owner="o",
                url="https://github.com/o/r",
                pulls_url="https://github.com/o/r/pulls",
                ci_conclusion="success",
                ci_workflow="test",
                ci_url="https://github.com/o/r/actions/runs/1",
                workflows=(
                    WorkflowRun(
                        name="test",
                        conclusion="success",
                        finished_at=1.0,
                        duration=1.0,
                        url="https://github.com/o/r/actions/runs/1",
                    ),
                ),
            )
        }
    )

    carrying = sorted({x.split("{")[0] for x in expose(snap) if 'url="http' in x})

    assert carrying == [
        "jq_ci_last_run_info",
        "jq_ci_workflow_info",
        "jq_repo_info",
    ], "a url label reached a family that carries history"


def test_only_the_join_targets_carry_a_url():
    """The census, pinned. Every family here is an `Always 1` join target whose
    label set already churns as PRs open and close; a `url` reaching anything
    else would start a new series and orphan its history."""
    from jq_collector.state import MergedPull

    snap = Snapshot(
        remote={
            "o/r": RemoteRepo(
                name="r",
                owner="o",
                url="https://github.com/o/r",
                pulls_url="https://github.com/o/r/pulls",
                ci_conclusion="success",
                ci_workflow="test",
                ci_url="https://github.com/o/r/actions/runs/1",
                workflows=(wf("test", "success"),),
                pulls=(pull(7, "success"),),
                merged=(
                    MergedPull(
                        number=4,
                        title="Landed",
                        author="someone",
                        merged_at=100.0,
                        url="https://github.com/o/r/pull/4",
                    ),
                ),
            )
        }
    )

    families = sorted({x.split("{")[0] for x in expose(snap) if "url=" in x})

    assert families == [
        "jq_ci_last_run_info",
        "jq_ci_workflow_info",
        "jq_merged_pull_request_info",
        "jq_pull_request_info",
        "jq_repo_info",
    ]
