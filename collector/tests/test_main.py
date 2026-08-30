"""The process: two refresh loops, one endpoint, and shutdown.

Almost none of this ran under test before, which is the wrong way round - it is
the layer that decides whether a failure degrades or takes the board down. The
three properties that matter here are all about *not* losing things: a raising
refresh must be recorded rather than propagated, a GitHub outage must not blank
the local half, and the port must not open until both halves have been seeded.
"""

from __future__ import annotations

import logging
import threading

import pytest

from jq_collector import __main__ as entry
from jq_collector.config import Config
from jq_collector.state import LocalRepo, RemoteRepo, SourceHealth, Store

# -- _run_once: health bookkeeping either way ------------------------------


def test_a_successful_refresh_records_a_success():
    store = Store()

    entry._run_once("github", store, lambda: None)

    health = store.health_for("github")
    assert health.last_success > 0
    assert health.errors == 0
    assert health.last_error == ""


def test_a_raising_refresh_is_recorded_not_propagated(caplog):
    """A refresh that raises must not kill its loop - the loop is the only thing
    that will ever try again."""
    store = Store()

    def boom():
        raise RuntimeError("github is down")

    entry._run_once("github", store, boom)

    health = store.health_for("github")
    assert health.errors == 1
    assert health.last_error == "github is down"
    assert "github refresh failed" in caplog.text


def test_a_failure_preserves_the_last_success_time():
    """`last_success` is what the Data age tile reads. Zeroing it on failure
    would say "never refreshed" about a board that refreshed a minute ago."""
    store = Store()
    entry._run_once("github", store, lambda: None)
    was = store.health_for("github").last_success

    entry._run_once("github", store, lambda: (_ for _ in ()).throw(RuntimeError("x")))

    assert store.health_for("github").last_success == was


def test_errors_accumulate_across_failures():
    store = Store()

    for _ in range(3):
        entry._run_once("local", store, lambda: (_ for _ in ()).throw(RuntimeError("x")))

    assert store.health_for("local").errors == 3


def test_a_success_after_failures_keeps_the_error_count():
    """The counter is a Prometheus counter; it must never go backwards."""
    store = Store()
    entry._run_once("local", store, lambda: (_ for _ in ()).throw(RuntimeError("x")))

    entry._run_once("local", store, lambda: None)

    assert store.health_for("local").errors == 1
    assert store.health_for("local").last_error == ""


# -- _loop: waits first, and stops promptly --------------------------------


def test_the_loop_waits_before_its_first_run():
    """main() has already seeded both sources, so running immediately would
    spend a second full GitHub pass for nothing."""
    stop = threading.Event()
    stop.set()
    ran: list[str] = []

    entry._loop("github", 60, Store(), lambda: ran.append("x"), stop)

    assert ran == []


def test_the_loop_runs_the_work_and_then_notices_the_stop():
    stop = threading.Event()
    store = Store()
    runs: list[str] = []

    def work():
        runs.append("x")
        stop.set()

    entry._loop("local", 0, store, work, stop)

    assert runs == ["x"]
    assert store.health_for("local").last_success > 0


def test_a_stop_during_the_wait_returns_without_working():
    """Shutdown must not wait out a ten-minute interval before exiting."""
    stop = threading.Event()
    ran: list[str] = []
    threading.Timer(0.01, stop.set).start()

    entry._loop("github", 0.05, Store(), lambda: ran.append("x"), stop)

    assert ran == []


# -- the two refreshers, and the caches they thread through ---------------


def test_the_local_refresh_passes_the_previous_scan_as_the_cache(monkeypatch):
    """Without this the line counts are re-measured every minute forever."""
    seen: dict[str, object] = {}

    def fake_scan(_cfg, branches, skip, previous):
        seen.update(branches=branches, skip=skip, previous=previous)
        return {"o/r": LocalRepo(name="r", path="/p")}

    monkeypatch.setattr(entry.localgit, "scan", fake_scan)
    store = Store()
    store.update(
        remote={"o/r": RemoteRepo(name="r", default_branch="trunk")},
        local={"o/r": LocalRepo(name="r", path="/p", code_lines=42)},
        excluded=frozenset({"o/dropped"}),
    )

    entry._refresh_local(Config(), store)

    assert seen["branches"] == {"o/r": "trunk"}, "the local scan needs GitHub's branch names"
    assert seen["skip"] == frozenset({"o/dropped"})
    assert seen["previous"]["o/r"].code_lines == 42
    assert set(store.snapshot().local) == {"o/r"}


def test_the_github_refresh_threads_both_caches(monkeypatch):
    seen: dict[str, object] = {}

    class FakeAPI:
        rate_remaining = 10.0
        rate_limit = 5000.0
        rate_reset = 1.0
        closed = False

        def close(self):
            type(self).closed = True

    def fake_collect(_cfg, ref_cache, coverage_cache):
        seen.update(ref_cache=ref_cache, coverage_cache=coverage_cache)
        return {"o/r": RemoteRepo(name="r")}, FakeAPI(), "v9", frozenset({"o/gone"})

    monkeypatch.setattr(entry.gh, "collect", fake_collect)
    store = Store()
    store.update(
        remote={
            "o/r": RemoteRepo(
                name="r",
                head_sha="abc",
                rhiza_ref="v1",
                coverage=87.3,
                coverage_lines=472,
                coverage_artifact=99,
            ),
            # No head_sha: nothing to key a pointer cache on, so it is left out.
            "o/fresh": RemoteRepo(name="fresh"),
        }
    )

    entry._refresh_github(Config(), store)

    assert seen["ref_cache"] == {"o/r": ("abc", "v1")}
    assert seen["coverage_cache"] == {"o/r": (99, (87.3, 472))}
    snap = store.snapshot()
    assert snap.latest_template_ref == "v9"
    assert snap.excluded == frozenset({"o/gone"})
    assert snap.rate_limit_remaining == 10.0
    assert FakeAPI.closed, "the HTTP client was left open"


def test_a_repo_with_no_coverage_still_caches_its_artifact_id(monkeypatch):
    """So a repo whose report could not be read is retried, rather than being
    remembered as having none."""
    seen: dict[str, object] = {}

    class FakeAPI:
        rate_remaining = rate_limit = rate_reset = 0.0

        def close(self):
            pass

    def fake_collect(_cfg, _ref_cache, coverage_cache):
        seen["coverage_cache"] = coverage_cache
        return {}, FakeAPI(), "", frozenset()

    monkeypatch.setattr(entry.gh, "collect", fake_collect)
    store = Store()
    store.update(remote={"o/r": RemoteRepo(name="r", coverage=None, coverage_artifact=7)})

    entry._refresh_github(Config(), store)

    assert seen["coverage_cache"] == {"o/r": (7, None)}


def test_the_client_is_closed_even_when_the_store_update_raises(monkeypatch):
    """The `finally` exists so a bad snapshot cannot leak a connection pool."""
    closed: list[bool] = []

    class FakeAPI:
        rate_remaining = rate_limit = rate_reset = 0.0

        def close(self):
            closed.append(True)

    monkeypatch.setattr(
        entry.gh,
        "collect",
        lambda *_a, **_k: ({}, FakeAPI(), "", frozenset()),
    )
    store = Store()
    monkeypatch.setattr(store, "update", lambda **_: (_ for _ in ()).throw(RuntimeError("bad")))

    with pytest.raises(RuntimeError):
        entry._refresh_github(Config(), store)

    assert closed == [True]


# -- main(): seeding order, then shutdown ---------------------------------


def test_main_seeds_both_sources_before_opening_the_port(monkeypatch):
    """Serving in between would publish a half-snapshot, which Prometheus keeps
    forever as a real observation of a fleet that was never in that state."""
    order: list[str] = []

    monkeypatch.setattr(entry, "_refresh_github", lambda *_: order.append("github"))
    monkeypatch.setattr(entry, "_refresh_local", lambda *_: order.append("local"))
    monkeypatch.setattr(entry, "start_http_server", lambda _port: order.append("serve"))
    monkeypatch.setattr(entry.REGISTRY, "register", lambda _c: order.append("register"))
    # Acting as if the signal arrived the moment it is registered: main() then
    # falls straight through its wait loop instead of blocking for ever.
    monkeypatch.setattr(entry.signal, "signal", lambda _sig, handler: handler(_sig, None))

    entry.main()

    assert order[:2] == ["github", "local"], "the port opened before both halves were seeded"
    assert order.index("serve") > order.index("local")


def test_main_warns_when_there_is_no_token(monkeypatch, caplog):
    """60 calls an hour will not refresh this fleet once."""
    monkeypatch.setattr(entry, "_refresh_github", lambda *_: None)
    monkeypatch.setattr(entry, "_refresh_local", lambda *_: None)
    monkeypatch.setattr(entry, "start_http_server", lambda _port: None)
    monkeypatch.setattr(entry.REGISTRY, "register", lambda _c: None)
    monkeypatch.setattr(entry.signal, "signal", lambda _sig, handler: handler(_sig, None))

    cfg = Config()
    object.__setattr__(cfg, "token", "")
    monkeypatch.setattr(entry, "Config", lambda: cfg)

    entry.main()

    assert "no GITHUB_TOKEN" in caplog.text


def test_main_starts_a_loop_per_source_and_shuts_them_down(monkeypatch):
    started: list[str] = []
    real_thread = threading.Thread

    class RecordingThread(real_thread):
        def start(self):
            started.append(self.name)
            super().start()

    monkeypatch.setattr(entry, "_refresh_github", lambda *_: None)
    monkeypatch.setattr(entry, "_refresh_local", lambda *_: None)
    monkeypatch.setattr(entry, "start_http_server", lambda _port: None)
    monkeypatch.setattr(entry.REGISTRY, "register", lambda _c: None)
    monkeypatch.setattr(entry.threading, "Thread", RecordingThread)
    monkeypatch.setattr(entry.signal, "signal", lambda _sig, handler: handler(_sig, None))

    entry.main()

    assert sorted(started) == ["github", "local"]


def test_main_logs_that_it_is_shutting_down(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="jq_collector")
    monkeypatch.setattr(entry, "_refresh_github", lambda *_: None)
    monkeypatch.setattr(entry, "_refresh_local", lambda *_: None)
    monkeypatch.setattr(entry, "start_http_server", lambda _port: None)
    monkeypatch.setattr(entry.REGISTRY, "register", lambda _c: None)
    monkeypatch.setattr(entry.signal, "signal", lambda _sig, handler: handler(_sig, None))

    entry.main()

    assert "shutting down" in caplog.text


def test_the_seed_records_health_for_both_sources(monkeypatch):
    """The Data age tile reads these, and it must not be blank on first scrape."""
    monkeypatch.setattr(entry, "_refresh_github", lambda *_: None)
    monkeypatch.setattr(entry, "_refresh_local", lambda *_: None)
    monkeypatch.setattr(entry, "start_http_server", lambda _port: None)
    seen: list[Store] = []
    monkeypatch.setattr(entry.REGISTRY, "register", lambda c: seen.append(c._store))
    monkeypatch.setattr(entry.signal, "signal", lambda _sig, handler: handler(_sig, None))

    entry.main()

    health = seen[0].snapshot().health
    assert set(health) == {"github", "local"}
    assert all(h.last_success > 0 for h in health.values())
    assert isinstance(health["github"], SourceHealth)


def test_main_holds_the_process_open_until_the_signal_arrives(monkeypatch):
    """The wait loop is what keeps the exporter alive; the refresh threads are
    daemons and would not hold the process on their own. Registering the handler
    schedules the signal rather than firing it, so main() actually enters the
    loop."""
    monkeypatch.setattr(entry, "_refresh_github", lambda *_: None)
    monkeypatch.setattr(entry, "_refresh_local", lambda *_: None)
    monkeypatch.setattr(entry, "start_http_server", lambda _port: None)
    monkeypatch.setattr(entry.REGISTRY, "register", lambda _c: None)
    monkeypatch.setattr(
        entry.signal,
        "signal",
        lambda sig, handler: threading.Timer(0.05, lambda: handler(sig, None)).start(),
    )

    entry.main()  # returns only because the scheduled handler set the event


def test_running_the_package_as_a_module_calls_main(monkeypatch):
    """`python -m jq_collector` is how the process is actually started - the
    Dockerfile used it and scripts/collector.sh still does. Patching the shared
    dependency modules rather than the entry point, because runpy re-executes
    __main__ in a fresh namespace where a patched `main` would not be seen.
    """
    import runpy
    import sys

    # Dropped from sys.modules first: runpy warns about re-executing a module
    # that is already imported, and the warning is fair - it is exactly what
    # would happen here. The monkeypatches below are on the *shared* dependency
    # modules, so they still apply to the fresh execution.
    monkeypatch.delitem(sys.modules, "jq_collector.__main__", raising=False)

    monkeypatch.setattr(entry.gh, "collect", lambda *_a, **_k: ({}, _NullAPI(), "", frozenset()))
    monkeypatch.setattr(entry.localgit, "scan", lambda *_a, **_k: {})
    monkeypatch.setattr("prometheus_client.start_http_server", lambda *_a, **_k: None)
    monkeypatch.setattr(entry.REGISTRY, "register", lambda _c: None)
    monkeypatch.setattr(entry.signal, "signal", lambda sig, handler: handler(sig, None))

    runpy.run_module("jq_collector", run_name="__main__")


class _NullAPI:
    rate_remaining = rate_limit = rate_reset = 0.0

    def close(self):
        pass
