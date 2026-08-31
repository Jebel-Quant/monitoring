---
title: Day to day
description: Why panels say No data, and what the Grafana sign-in button actually is.
keywords: grafana login, no data, prometheus staleness, macos sleep, caffeinate
---

# Running it day to day

## One container, three processes

Prometheus, Grafana and the collector share a container, and `docker logs -f
jq-fleet` shows all three with a prefix telling you which is talking.

| | |
|---|---|
| What it is doing | `docker logs -f jq-fleet` |
| Restart it | `docker restart jq-fleet` — also how you pick up an edited `repos.yml` |
| Is Prometheus reaching the collector? | <http://localhost:9090/targets> — the `jq-collector` job (needs `-p 127.0.0.1:9090:9090`) |
| Raw metrics | <http://localhost:9109/metrics> (needs `-p 127.0.0.1:9109:9109`) |

Nothing here restarts a dead process. The three are one board — a dead
collector means empty panels, a dead Prometheus means no history — so any of
them exiting takes the container down and Docker's own `--restart` policy
handles it. That keeps `docker ps` honest about whether the board is up, which
a supervisor quietly restarting one process inside a still-healthy container
would not.

### How the working copies get in

They used to keep the collector out of Docker entirely. A bind mount had to
place every checkout at `/repos/<owner>/<name>`, which silently lost any repo
living somewhere else — and four repos in the fleet this was built against are
laid out that way. So the collector ran on the host as a launchd agent, and
Prometheus scraped it at `host.docker.internal:9109`.

The fix was to stop mounting repos one at a time. `-v "$HOME:/host:ro"` mounts
the home directory once, whole, and `~/...` in `repos.yml` is read relative to
that mount — so `~/repos/tschm/rhiza_projects/cs` means exactly what it says.
One mount expresses any layout, and the collector came back inside.

**The trade.** The collector can now see everything under your home directory,
not only the checkouts you listed — the mount is the same width either way.
It still never writes: every git call is read-only and passes
`--no-optional-locks`, and the mount is `:ro` so the kernel enforces it too.
Leave the mount off entirely and the GitHub half still works; the working-copy
panels simply stay empty.

**The first scan is slow.** Reading through a Docker Desktop bind mount is cold
the first time — on a 24-repo fleet the opening pass took about two minutes,
and about three milliseconds per git call once the mount was warm. This is also
why line counts are cached against a fingerprint of the clone rather than
retaken every minute (see [Size and cadence](dashboard.md#size-and-cadence)).

## Coming from the two-container stack

The board used to be `jq-prometheus` and `jq-grafana` plus a launchd agent, with
history in the `jq-monitoring_prometheus-data` and `jq-monitoring_grafana-data`
volumes. Both carry over — Prometheus's `instance` label is pinned to
`jq-collector` by a relabel rule (see `prometheus/prometheus.yml`) precisely so
that moving the collector does not fork every series in two.

Stop the old stack **cleanly** first. `docker stop` sends SIGTERM and Prometheus
flushes its WAL on it; killing it instead loses whatever had not been compacted
into a block yet.

```bash
launchctl bootout "gui/$UID/com.jebel-quant.jq-collector"   # macOS
docker stop jq-prometheus jq-grafana

docker volume create jq-fleet-data
docker run --rm \
  -v jq-monitoring_prometheus-data:/old-prom:ro \
  -v jq-monitoring_grafana-data:/old-graf:ro \
  -v jq-fleet-data:/data \
  alpine sh -euc '
    mkdir -p /data/prometheus /data/grafana
    cd /old-prom; for f in *; do case "$f" in lock|queries.active) continue;; esac
      cp -a "$f" /data/prometheus/; done
    cd /old-graf; for f in *; do case "$f" in dashboards) continue;; esac
      cp -a "$f" /data/grafana/; done'
```

Two paths change: the TSDB moves from `/prometheus` to `/data/prometheus` and
Grafana's database from `/var/lib/grafana` to `/data/grafana`, which is why the
copy is into subdirectories rather than into the volume root. `lock` and
`queries.active` are runtime files Prometheus rebuilds, and `dashboards/` is an
empty leftover from the old compose file bind-mounting over that path — the
dashboards are in the image now.

Then start the container [as in the recipe](index.md#the-recipe) with
`-v jq-fleet-data:/data`. This is a copy, so the old volumes are untouched and
rolling back is `docker start jq-prometheus jq-grafana`. Once you are satisfied:

```bash
docker rm jq-prometheus jq-grafana
docker volume rm jq-monitoring_prometheus-data jq-monitoring_grafana-data
rm -rf .collector-logs      # the launchd agent's log; nothing writes here now
```

## The "Sign in" button

Grafana's own local login, against a SQLite file under `/data` on this
machine. There is one account, `admin` / `admin` (override with
`-e GF_SECURITY_ADMIN_PASSWORD=...`). No
Grafana Cloud, no external account, nothing leaves the box. Anonymous access is
enabled with the `Viewer` role, so the board opens without signing in; the
dashboard is provisioned and read-only anyway, so you would only sign in to add
a contact point or poke at settings.

Because anonymous access is on, **publish the ports to `127.0.0.1` only** —
`-p 127.0.0.1:3000:3000`, as every example here does. A bare `-p 3000:3000`
would serve private repo names, PR titles and local branch names to anyone on
the same network with no password.

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
caffeinate -s -w "$(docker inspect -f '{{.State.Pid}}' jq-fleet)"    # or
                                     # Energy Saver -> prevent sleeping
```

