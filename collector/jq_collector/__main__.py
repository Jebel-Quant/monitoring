"""Entry point: two refresh loops feeding one /metrics endpoint.

The loops run on their own cadences and write into a shared store, so a scrape
never waits on a forge's API. Prometheus can poll every 15s while the forges are
only asked every five minutes.

There are two loops and not three, though there are now two forges. The remote
loop collects both and publishes once, because `Store.update(remote=...)`
replaces the whole map: a loop per forge would have each thread publish only its
own share, and the board would show half the fleet flickering against the other
half. Health and errors are still recorded per forge inside that one pass.
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
from . import gitlab as gl
from . import localgit
from .config import Config
from .metrics import FleetCollector
from .state import RemoteRepo, SourceHealth, Store

log = logging.getLogger("jq_collector")


def _refresh_local(cfg: Config, store: Store) -> None:
    snap = store.snapshot()
    branches = {name: repo.default_branch for name, repo in snap.remote.items()}
    # The previous scan is the measurement cache: a repo that has not moved
    # since it keeps its line and commit counts instead of paying for them
    # again, every minute, forever.
    store.update(
        local=localgit.scan(cfg, branches, skip=snap.excluded, previous=snap.local),
    )


def _refresh_remote(cfg: Config, store: Store) -> None:
    """Collect every forge in the fleet and publish the result as one snapshot.

    One pass over both forges rather than a loop each, because
    ``Store.update(remote=...)`` replaces the whole map: two threads writing it
    would each publish only their own share, and the board would show half the
    fleet flickering against the other half.

    GitHub is collected even when no repo lives there, because it is where the
    template's releases are published and therefore where drift is measured
    from, whichever forge the repo pinning it is on.
    """
    snap = store.snapshot()
    # (default_branch_sha, ref) per repo: the pointer can only have changed if
    # the branch head moved, so this keeps the drift read correct without
    # spending a contents call per repo per refresh.
    ref_cache = {
        name: (repo.head_sha, repo.rhiza_ref) for name, repo in snap.remote.items() if repo.head_sha
    }
    # (artifact id, percent) per repo: an unchanged artifact means the report
    # behind it is byte-identical, so there is nothing to gain from pulling the
    # zip down again. GitLab reports coverage as a field and ignores this.
    coverage_cache = {
        name: (repo.coverage_artifact, (repo.coverage, repo.coverage_lines))
        if repo.coverage is not None
        else (repo.coverage_artifact, None)
        for name, repo in snap.remote.items()
        if repo.coverage_artifact
    }

    by_forge = cfg.repos_by_forge()
    remote: dict[str, RemoteRepo] = {}
    excluded: set[str] = set()
    # The template's release tags, fetched by the GitHub pass and needed by the
    # GitLab one to measure drift against the same list.
    template_tags: list[str] = []

    def gather_github() -> None:
        result, api, tags, dropped = gh.collect(
            cfg, ref_cache, coverage_cache, fleet=by_forge.get("github", ())
        )
        try:
            remote.update(result)
            excluded.update(dropped)
            template_tags[:] = tags
            store.update(
                latest_template_ref=tags[0] if tags else "",
                rate_limit_remaining=api.rate_remaining,
                rate_limit_limit=api.rate_limit,
                rate_limit_reset=api.rate_reset,
            )
        finally:
            api.close()

    def gather_gitlab() -> None:
        result, api, dropped = gl.collect(
            cfg,
            ref_cache,
            coverage_cache,
            fleet=by_forge["gitlab"],
            tags=template_tags,
        )
        try:
            remote.update(result)
            excluded.update(dropped)
        finally:
            api.close()

    # Health is recorded per forge, so a GitLab outage shows up as
    # jq_collector_errors{source="gitlab"} and leaves the GitHub repos standing
    # rather than shortening the fleet silently.
    _run_once("github", store, gather_github)
    if by_forge.get("gitlab"):
        _run_once("gitlab", store, gather_gitlab)

    store.update(remote=remote, excluded=frozenset(excluded))


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


def _tick_local(cfg: Config, store: Store) -> None:
    """One local refresh, health recorded."""
    _run_once("local", store, lambda: _refresh_local(cfg, store))


def _loop(
    interval: int,
    tick: Callable[[], None],
    stop: threading.Event,
) -> None:
    """Refresh forever. Waits first, because main() has already seeded once.

    The tick owns its own health bookkeeping. That moved out of here when the
    remote pass grew from one source to one per forge: a single `source` name
    per loop could no longer describe what the tick had actually talked to.

    Which leaves this loop holding the last resort. A tick records what each
    forge did, but the work between forges - merging their results and
    publishing the snapshot - belongs to no single source and would otherwise
    escape. An exception reaching a thread's target kills the thread silently,
    and the loop is the only thing that will ever try again, so nothing may get
    out of here.
    """
    while not stop.is_set():
        stop.wait(interval)
        if stop.is_set():
            return
        try:
            tick()
        except Exception:
            log.exception("refresh tick failed")


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("JQ_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
    )
    cfg = Config()
    if not cfg.token:
        log.warning("no GITHUB_TOKEN set - unauthenticated GitHub calls are limited to 60/hour")
    # Only warn about the token a fleet actually needs. A GitHub-only deployment
    # must not start complaining about GitLab credentials it has no use for.
    if cfg.repos_by_forge().get("gitlab") and not cfg.gitlab_token:
        log.warning("no GITLAB_TOKEN set - only public GitLab projects will be readable")

    store = Store()

    # Seed both sources BEFORE opening the port. The local pass takes ~2s and
    # the remote pass ~11s, and serving in between would publish a snapshot with
    # working copies but no CI, drift or pull requests - which Prometheus stores
    # as a real observation, leaving a permanent dip in the history that reads
    # as "nothing was behind the template" rather than "we had not looked yet".
    # A refused connection for those few seconds is an honest target-down.
    # The forges run first: the local pass needs their default-branch names to
    # know which local ref to compare against, and nothing flows the other way.
    _refresh_remote(cfg, store)
    _tick_local(cfg, store)

    # The default registry already carries the process and GC collectors, so
    # adding ours to it keeps exporter self-monitoring on the same endpoint.
    REGISTRY.register(FleetCollector(store))
    start_http_server(cfg.listen_port)
    log.info("serving metrics on :%d", cfg.listen_port)

    stop = threading.Event()
    threads = [
        threading.Thread(
            target=_loop,
            args=(cfg.local_interval, lambda: _tick_local(cfg, store), stop),
            daemon=True,
            name="local",
        ),
        threading.Thread(
            target=_loop,
            args=(cfg.github_interval, lambda: _refresh_remote(cfg, store), stop),
            daemon=True,
            name="remote",
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
