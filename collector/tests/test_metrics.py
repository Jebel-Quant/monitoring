"""The exposition must be valid and must not overstate health."""

from __future__ import annotations

import collections

from prometheus_client import CollectorRegistry, generate_latest

from jq_collector.metrics import FleetCollector
from jq_collector.state import LocalRepo, PullRequest, RemoteRepo, Snapshot, Store, WorkflowRun


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
