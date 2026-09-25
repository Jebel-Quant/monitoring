"""The whole exposition, byte for byte, for one fleet that takes every branch.

The other metric tests assert one property each. This one pins everything at
once - family order, HELP text, label order, which series are absent - so that
restructuring ``render`` cannot change what Prometheus scrapes without saying
so here. A deliberate change to the exposition regenerates the golden file:

    uv run python -c "import test_metrics_golden as t; t.write_golden()"

run from ``collector/tests``, and the diff is then the review.
"""

from __future__ import annotations

import dataclasses
import pathlib

from prometheus_client import CollectorRegistry, generate_latest
from test_metrics import full_local, full_remote, pull, wf

from jq_collector.metrics import FleetCollector
from jq_collector.state import (
    LocalRepo,
    MergedPull,
    RemoteRepo,
    Snapshot,
    SourceHealth,
    Store,
    WorkflowRun,
)

GOLDEN = pathlib.Path(__file__).parent / "golden" / "metrics.prom"


def fleet() -> Snapshot:
    full = dataclasses.replace(
        full_remote(),
        workflows=(
            WorkflowRun(name="ci", conclusion="success", finished_at=900.0, duration=1.0, url="u1"),
            # Older run of the same name: collapsed away, newest wins.
            WorkflowRun(name="ci", conclusion="failure", finished_at=100.0, duration=1.0, url="u0"),
            WorkflowRun(
                name="docs", conclusion="failure", finished_at=800.0, duration=1.0, url="u2"
            ),
            WorkflowRun(
                name="nightly", conclusion="cancelled", finished_at=950.0, duration=1.0, url=""
            ),
            WorkflowRun(name="empty", conclusion="", finished_at=10.0, duration=1.0, url=""),
        ),
        pulls=(
            pull(7, "success"),
            pull(8, "failure"),
            dataclasses.replace(pull(9, "cancelled"), draft=True),
        ),
        open_pulls_total=3,
        merged=(
            MergedPull(number=5, title="five", author="a", merged_at=500.0, url="m5"),
            MergedPull(number=5, title="five", author="a", merged_at=500.0, url="m5"),
            MergedPull(number=4, title="four", author="b", merged_at=400.0, url="m4"),
        ),
    )
    gitlab = RemoteRepo(
        name="web",
        owner="acme/platform",
        forge="gitlab",
        default_branch="trunk",
        visibility="private",
        url="https://gitlab.com/acme/platform/web",
        pulls_url="https://gitlab.com/acme/platform/web/-/merge_requests",
        protected=None,
        alerts_enabled=False,
        ci_conclusion="cancelled",
        workflows=(wf("build", "cancelled"),),
    )
    unprotected = RemoteRepo(
        name="loose",
        owner="o",
        head_sha="new",
        protected=False,
        ci_conclusion="failure",
        ci_workflow="ci",
        workflows=(wf("ci", "failure"),),
        rhiza_managed=False,
    )
    detached = LocalRepo(
        name="solo",
        path="/repos/o/solo",
        owner="o",
        branch="",
        ahead=None,
        behind=None,
        fetch_age=None,
        commits_since_release=None,
    )
    stale_clone = dataclasses.replace(full_local(), branch="feature", default_branch_sha="old")
    return Snapshot(
        remote={
            "Jebel-Quant/rhiza": full,
            "acme/platform/web": gitlab,
            "o/loose": unprotected,
            "o/gone": full_remote(),
        },
        local={
            "Jebel-Quant/rhiza": full_local(),
            "o/solo": detached,
            "o/loose": stale_clone,
        },
        excluded=frozenset({"o/gone"}),
        latest_template_ref="v1.7.1",
        rate_limit_remaining=4000.0,
        rate_limit_limit=5000.0,
        rate_limit_reset=1234.0,
        health={
            "local": SourceHealth(last_success=3.0, last_duration=0.5, errors=0),
            "github": SourceHealth(last_success=1.0, last_duration=2.0, errors=3),
        },
    )


def exposition(snapshot: Snapshot) -> str:
    store = Store()
    store.update(**{f.name: getattr(snapshot, f.name) for f in dataclasses.fields(snapshot)})
    reg = CollectorRegistry()
    reg.register(FleetCollector(store))
    return generate_latest(reg).decode()


def write_golden() -> None:
    GOLDEN.parent.mkdir(exist_ok=True)
    GOLDEN.write_text(exposition(fleet()))


def test_the_exposition_is_unchanged():
    assert exposition(fleet()) == GOLDEN.read_text()
