---
title: Day to day
description: Why panels say No data, and what the Grafana sign-in button actually is.
keywords: grafana login, no data, prometheus staleness, macos sleep, caffeinate
---

# Running it day to day

## The collector runs on your machine

Two halves, not three containers. Prometheus and Grafana are in Docker;
**the collector is an ordinary process on your Mac**, started by `up.sh` as a
launchd agent and stopped by `down.sh`.

It is not containerised because it reads your working copies, and a container
can only reach those through bind mounts. Those mounts have to place every
checkout at `<root>/<owner>/<name>`, which silently loses any repo that lives
somewhere else, and reading thousands of small files back through them on macOS
is slow enough that the line counts needed a cache to stay affordable. On the
host both problems disappear.

| | |
|---|---|
| Its log | `.collector-logs/collector.log` |
| Restart it | `launchctl kickstart -k gui/$UID/com.jebel-quant.jq-collector` |
| Run it in the foreground instead | `./scripts/collector.sh` (Ctrl-C stops it) |
| Is Prometheus reaching it? | <http://localhost:9090/targets> — the `jq-collector` job |

Prometheus scrapes it at `host.docker.internal:9109`, which is how a container
reaches the machine it runs on.

**The trade.** Inside the container the collector could only see the checkouts
mounted into it, so an unlisted repo was not merely filtered out — it was
invisible. It now runs as you and could read anything you can. It still never
writes: every git call is read-only and passes `--no-optional-locks`. But that
is now a property of the code rather than something the sandbox enforces.

**If it will not start**, the usual cause is `PATH`. A launchd agent inherits
`/usr/bin:/bin:/usr/sbin:/sbin` and nothing else, so a `uv` under
`/opt/homebrew` or `~/.local` is invisible to it. `up.sh` pins `uv`'s directory
into the plist when it installs the agent, so re-running `./scripts/up.sh`
after moving or reinstalling `uv` is the fix.


## The "Sign in" button

Grafana's own local login, against a SQLite file in the `grafana-data` volume on
this machine. There is one account, `admin` / `admin` (override in `.env`). No
Grafana Cloud, no external account, nothing leaves the box. Anonymous access is
enabled with the `Viewer` role, so the board opens without signing in; the
dashboard is provisioned and read-only anyway, so you would only sign in to add
a contact point or poke at settings.

Because anonymous access is on, **all three ports bind to `127.0.0.1` only**.
Published on `0.0.0.0` they would serve private repo names, PR titles and local
branch names to anyone on the same network with no password. If you genuinely
want that, drop the `127.0.0.1:` prefix in `docker-compose.yml`.

## "No data" usually means the Mac was asleep

Docker pauses with the machine, so while this laptop sleeps nothing is scraped
and nothing is collected. On waking, Prometheus has no sample inside its
five-minute staleness window, and every instant-query panel renders **No data**
until the next scrape lands — a few seconds later.

The **Data age** tile exists to make that unambiguous: it shows how long since
the collector last refreshed, worst of its two sources, and turns amber past ten
minutes and red past thirty. If it is red, everything above it predates the last
sleep. Cross-check against `pmset -g log | grep -E 'Entering Sleep|Wake'` — the
container's log gaps line up with it exactly.

Gaps in the time series mean the same thing, and they are left honest rather
than bridged. Raising Prometheus's `--query.lookback-delta` above the default
`5m` would keep the tiles populated across short sleeps, but it would also carry
the last value forward through every gap in the range panels — asserting that CI
was green during hours nobody observed. That is the same failure as the
`or vector(0)` trap below, so the default stands. To avoid the gaps instead of
papering over them, keep the machine awake while the stack matters:

```bash
caffeinate -s docker compose up -d    # or Energy Saver -> prevent sleeping
```

