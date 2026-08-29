"""Entry point: two refresh loops feeding one /metrics endpoint.

The loops run on their own cadences and write into a shared store, so a scrape
never waits on the GitHub API. Prometheus can poll every 15s while GitHub is
only asked every five minutes.
"""

from __future__ import annotations

import logging
import os
import signal
import threading
import time
from collections.abc import Callable

from prometheus_client import REGISTRY, start_http_server

from . import github as gh
from . import localgit
from .config import Config
from .metrics import FleetCollector
from .state import SourceHealth, Store

log = logging.getLogger("jq_collector")


def _refresh_local(cfg: Config, store: Store) -> None:
    snap = store.snapshot()
    branches = {name: repo.default_branch for name, repo in snap.remote.items()}
    store.update(local=localgit.scan(cfg, branches, skip=snap.excluded))


def _refresh_github(cfg: Config, store: Store) -> None:
    snap = store.snapshot()
    # (default_branch_sha, ref) per repo: the pointer can only have changed if
    # the branch head moved, so this keeps the drift read correct without
    # spending a contents call per repo per refresh.
    ref_cache = {
        name: (repo.head_sha, repo.rhiza_ref) for name, repo in snap.remote.items() if repo.head_sha
    }
    remote, api, latest, excluded = gh.collect(cfg, ref_cache)
    try:
        store.update(
            remote=remote,
            excluded=excluded,
            latest_template_ref=latest,
            rate_limit_remaining=api.rate_remaining,
            rate_limit_limit=api.rate_limit,
            rate_limit_reset=api.rate_reset,
        )
    finally:
        api.close()


def _run_once(source: str, store: Store, work: Callable[[], None]) -> None:
    """Run one refresh, recording duration and errors either way."""
    previous = store.health_for(source)
    started = time.monotonic()
    try:
        work()
    except Exception as exc:
        log.exception("%s refresh failed", source)
        store.record_health(
            source,
            SourceHealth(
                last_success=previous.last_success,
                last_duration=time.monotonic() - started,
                errors=previous.errors + 1,
                last_error=str(exc),
            ),
        )
        return
    elapsed = time.monotonic() - started
    store.record_health(
        source,
        SourceHealth(
            last_success=time.time(),
            last_duration=elapsed,
            errors=previous.errors,
            last_error="",
        ),
    )
    log.info("%s refresh finished in %.1fs", source, elapsed)


def _loop(
    source: str,
    interval: int,
    store: Store,
    work: Callable[[], None],
    stop: threading.Event,
) -> None:
    """Refresh forever. Waits first, because main() has already seeded once."""
    while not stop.is_set():
        stop.wait(interval)
        if stop.is_set():
            return
        _run_once(source, store, work)


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("JQ_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
    )
    cfg = Config()
    if not cfg.token:
        log.warning("no GITHUB_TOKEN set - unauthenticated GitHub calls are limited to 60/hour")

    store = Store()

    # Seed both sources BEFORE opening the port. The local pass takes ~2s and
    # the GitHub pass ~11s, and serving in between would publish a snapshot with
    # working copies but no CI, drift or pull requests - which Prometheus stores
    # as a real observation, leaving a permanent dip in the history that reads
    # as "nothing was behind the template" rather than "we had not looked yet".
    # A refused connection for those few seconds is an honest target-down.
    # GitHub runs first: the local pass needs its default-branch names to know
    # which local ref to compare against, and nothing now flows the other way.
    _run_once("github", store, lambda: _refresh_github(cfg, store))
    _run_once("local", store, lambda: _refresh_local(cfg, store))

    # The default registry already carries the process and GC collectors, so
    # adding ours to it keeps exporter self-monitoring on the same endpoint.
    REGISTRY.register(FleetCollector(store))
    start_http_server(cfg.listen_port)
    log.info("serving metrics on :%d", cfg.listen_port)

    stop = threading.Event()
    threads = [
        threading.Thread(
            target=_loop,
            args=(
                "local",
                cfg.local_interval,
                store,
                lambda: _refresh_local(cfg, store),
                stop,
            ),
            daemon=True,
            name="local",
        ),
        threading.Thread(
            target=_loop,
            args=(
                "github",
                cfg.github_interval,
                store,
                lambda: _refresh_github(cfg, store),
                stop,
            ),
            daemon=True,
            name="github",
        ),
    ]
    # The seed above already ran both, so hold each loop off for one interval.
    for thread in threads:
        thread.start()

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())

    while not stop.is_set():
        stop.wait(1)
    log.info("shutting down")


if __name__ == "__main__":
    main()
