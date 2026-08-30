"""The snapshot store, which two refreshers write and every scrape reads.

The thread safety is not incidental. Two background loops replace the snapshot
on their own cadences while `/metrics` renders it, and the reason `update`
replaces rather than mutates is that a half-applied snapshot served to
Prometheus becomes a permanent, wrong observation in the history.
"""

from __future__ import annotations

import threading

from jq_collector.state import LocalRepo, RemoteRepo, Snapshot, SourceHealth, Store


def test_a_fresh_store_serves_an_empty_snapshot():
    snap = Store().snapshot()

    assert snap.remote == {} and snap.local == {}
    assert snap.excluded == frozenset()
    # -1 rather than 0: nothing has been observed yet, and a zero here would
    # read as "no rate limit left".
    assert snap.rate_limit_remaining == -1.0


def test_update_replaces_only_the_named_fields():
    store = Store()
    store.update(remote={"o/r": RemoteRepo(name="r")})
    store.update(local={"o/r": LocalRepo(name="r", path="/p")})

    snap = store.snapshot()
    assert set(snap.remote) == {"o/r"}, "the GitHub half was lost by a local update"
    assert set(snap.local) == {"o/r"}


def test_health_is_recorded_per_source_without_disturbing_the_other():
    store = Store()
    store.record_health("github", SourceHealth(last_success=1.0, errors=2))
    store.record_health("local", SourceHealth(last_success=3.0))

    assert store.health_for("github").errors == 2
    assert store.health_for("local").last_success == 3.0


def test_health_for_an_unknown_source_is_a_default_not_a_crash():
    """A scrape can arrive before either loop has finished its first pass."""
    assert store_health_default() == SourceHealth()


def store_health_default() -> SourceHealth:
    return Store().health_for("github")


def test_recording_health_does_not_drop_the_repos():
    store = Store()
    store.update(remote={"o/r": RemoteRepo(name="r")})
    store.record_health("github", SourceHealth(last_success=1.0))

    assert set(store.snapshot().remote) == {"o/r"}


def test_a_snapshot_handed_out_is_not_mutated_by_a_later_update():
    """The renderer walks the snapshot it was given; a swap under it would
    yield a frame that is half one refresh and half the next."""
    store = Store()
    store.update(remote={"o/r": RemoteRepo(name="r")})
    held = store.snapshot()

    store.update(remote={})

    assert set(held.remote) == {"o/r"}
    assert store.snapshot().remote == {}


def test_concurrent_writers_leave_a_consistent_snapshot():
    """Both loops write on their own cadence; neither may lose the other's half."""
    store = Store()
    done = threading.Barrier(3)

    def write_remote():
        for i in range(200):
            store.update(remote={f"o/r{i}": RemoteRepo(name=f"r{i}")})
        done.wait()

    def write_health():
        for i in range(200):
            store.record_health("github", SourceHealth(last_success=float(i)))
        done.wait()

    threads = [threading.Thread(target=write_remote), threading.Thread(target=write_health)]
    for t in threads:
        t.start()
    done.wait(timeout=10)
    for t in threads:
        t.join(timeout=10)

    snap = store.snapshot()
    assert len(snap.remote) == 1
    assert snap.health["github"].last_success == 199.0


def test_snapshot_is_a_dataclass_of_two_independent_halves():
    """A GitHub outage must not blank the local panels, which is why the maps
    are separate rather than one merged view."""
    snap = Snapshot(remote={"o/r": RemoteRepo(name="r")})

    assert snap.local == {}
    assert snap.remote["o/r"].name == "r"
