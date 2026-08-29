"""The exposition must be valid and must not overstate health."""

from __future__ import annotations

import collections

from prometheus_client import CollectorRegistry, generate_latest

from jq_collector.metrics import FleetCollector
from jq_collector.state import LocalRepo, RemoteRepo, Snapshot, Store, WorkflowRun


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
