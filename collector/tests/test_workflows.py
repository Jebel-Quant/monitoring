"""Regression tests for how CI state is derived from GitHub.

Three shipped bugs came from treating the runs feed as the authority on which
workflows exist. Each has a test here.
"""

from __future__ import annotations

from conftest import run, workflow

RUNS = "/repos/o/r/actions/runs"
WFS = "/repos/o/r/actions/workflows"


def test_deleted_workflow_is_ignored(make_client):
    """A workflow deleted months ago keeps its runs; a failing last run must not
    make the repo red forever. Jebel-Quant/platform sat red for 12 weeks on a
    `latex.yml` removed in June."""
    c = make_client(
        {
            WFS: {"workflows": [workflow(1, "Build PDF")]},
            RUNS: {
                "workflow_runs": [
                    run(1, "Build PDF", "success", "2026-08-01T00:00:00Z"),
                    run(99, ".github/workflows/latex.yml", "failure", "2026-06-01T00:00:00Z"),
                ]
            },
        }
    )
    assert [r["_name"] for r in c.latest_runs("o/r", "main")] == ["Build PDF"]


def test_disabled_workflow_is_ignored(make_client):
    """A switched-off job is not a failing one."""
    c = make_client(
        {
            WFS: {"workflows": [workflow(1, "CI"), workflow(2, "Old", state="disabled_manually")]},
            RUNS: {
                "workflow_runs": [
                    run(1, "CI", "success", "2026-08-01T00:00:00Z"),
                    run(2, "Old", "failure", "2026-07-01T00:00:00Z"),
                ]
            },
        }
    )
    assert [r["_name"] for r in c.latest_runs("o/r", "main")] == ["CI"]


def test_renamed_workflow_yields_one_series_under_its_current_name(make_client):
    """One id producing two run names must not become two series - the old
    name's last run would linger exactly as a deleted workflow's does."""
    c = make_client(
        {
            WFS: {"workflows": [workflow(1, "Build PDF")]},
            RUNS: {
                "workflow_runs": [
                    run(1, "Build vision.pdf", "failure", "2026-06-01T00:00:00Z"),
                    run(1, "Build PDF", "success", "2026-08-01T00:00:00Z"),
                ]
            },
        }
    )
    got = c.latest_runs("o/r", "main")
    assert len(got) == 1
    assert got[0]["_name"] == "Build PDF"
    assert got[0]["conclusion"] == "success"


def test_latest_is_by_completion_not_feed_order(make_client):
    """GitHub orders by created_at, so a long or re-run job sorts below newer
    ones that finished earlier."""
    c = make_client(
        {
            WFS: {"workflows": [workflow(1, "CI")]},
            RUNS: {
                "workflow_runs": [
                    run(1, "CI", "success", "2026-08-01T04:09:00Z"),
                    run(1, "CI", "failure", "2026-08-01T05:39:00Z"),
                ]
            },
        }
    )
    assert c.latest_runs("o/r", "main")[0]["conclusion"] == "failure"


def test_workflow_missing_from_the_feed_is_fetched_directly(make_client):
    """The feed's first page is dominated by frequent workflows. cvxgrp/simulator
    has 2406 runs on main and only 6 of its 23 workflows appear in the first 100;
    a failing weekly job was invisible for 40 days."""
    c = make_client(
        {
            WFS: {"workflows": [workflow(1, "CI"), workflow(2, "Weekly")]},
            RUNS: {"workflow_runs": [run(1, "CI", "success", "2026-08-01T00:00:00Z")]},
            "/repos/o/r/actions/workflows/2/runs": {
                "workflow_runs": [run(2, "Weekly", "failure", "2026-07-01T00:00:00Z")]
            },
        }
    )
    got = {r["_name"]: r["conclusion"] for r in c.latest_runs("o/r", "main")}
    assert got == {"CI": "success", "Weekly": "failure"}


def test_workflow_that_never_ran_is_absent_not_green(make_client):
    """Absent means "nothing to judge"; assuming green would hide a repo."""
    c = make_client(
        {
            WFS: {"workflows": [workflow(1, "CI"), workflow(2, "Release")]},
            RUNS: {"workflow_runs": [run(1, "CI", "success", "2026-08-01T00:00:00Z")]},
            "/repos/o/r/actions/workflows/2/runs": {"workflow_runs": []},
        }
    )
    assert [r["_name"] for r in c.latest_runs("o/r", "main")] == ["CI"]


def test_unreadable_workflow_listing_falls_back_to_the_feed(make_client):
    """A transient API error must over-report, never blank a repo's CI."""
    c = make_client(
        {
            WFS: None,
            RUNS: {"workflow_runs": [run(1, "CI", "failure", "2026-08-01T00:00:00Z")]},
        }
    )
    assert [r["conclusion"] for r in c.latest_runs("o/r", "main")] == ["failure"]


def test_two_active_workflows_sharing_a_name_get_distinct_labels(make_client):
    """Duplicate label sets made Prometheus drop 16 samples a scrape."""
    c = make_client(
        {
            WFS: {
                "workflows": [
                    workflow(1, "CI", path=".github/workflows/a.yml"),
                    workflow(2, "CI", path=".github/workflows/b.yml"),
                ]
            },
            RUNS: {
                "workflow_runs": [
                    run(1, "CI", "success", "2026-08-01T00:00:00Z"),
                    run(2, "CI", "failure", "2026-08-01T00:00:00Z"),
                ]
            },
        }
    )
    names = [r["_name"] for r in c.latest_runs("o/r", "main")]
    assert len(names) == len(set(names)), f"labels collide: {names}"


def test_cancelled_run_falls_back_to_the_last_real_verdict(make_client):
    """Stopping a job by hand, or a concurrency group killing it when the next
    push lands, is not a failure. The workflow keeps its last conclusive run."""
    c = make_client(
        {
            WFS: {"workflows": [workflow(1, "CI")]},
            RUNS: {
                "workflow_runs": [
                    run(1, "CI", "cancelled", "2026-08-02T00:00:00Z"),
                    run(1, "CI", "success", "2026-08-01T00:00:00Z"),
                ]
            },
        }
    )
    got = c.latest_runs("o/r", "main")
    assert [(r["_name"], r["conclusion"]) for r in got] == [("CI", "success")]


def test_workflow_with_only_cancelled_runs_is_absent_not_red(make_client):
    """No verdict at all reads like a workflow that never ran."""
    c = make_client(
        {
            WFS: {"workflows": [workflow(1, "CI"), workflow(2, "Weekly")]},
            RUNS: {"workflow_runs": [run(1, "CI", "success", "2026-08-01T00:00:00Z")]},
            "/repos/o/r/actions/workflows/2/runs": {
                "workflow_runs": [run(2, "Weekly", "cancelled", "2026-07-01T00:00:00Z")]
            },
        }
    )
    assert [r["_name"] for r in c.latest_runs("o/r", "main")] == ["CI"]


def test_direct_fetch_skips_cancelled_runs(make_client):
    """The targeted call asks for several runs precisely because the newest one
    may be cancelled."""
    c = make_client(
        {
            WFS: {"workflows": [workflow(1, "CI"), workflow(2, "Weekly")]},
            RUNS: {"workflow_runs": [run(1, "CI", "success", "2026-08-01T00:00:00Z")]},
            "/repos/o/r/actions/workflows/2/runs": {
                "workflow_runs": [
                    run(2, "Weekly", "cancelled", "2026-07-02T00:00:00Z"),
                    run(2, "Weekly", "failure", "2026-07-01T00:00:00Z"),
                ]
            },
        }
    )
    got = {r["_name"]: r["conclusion"] for r in c.latest_runs("o/r", "main")}
    assert got == {"CI": "success", "Weekly": "failure"}
