# Fleet monitoring

A Grafana board for the state of your repo fleet — template drift, CI on the
default branch, open pull requests, and the working copies on this machine —
with Prometheus keeping the history and six alert rules on top.

Two kinds of repo are in scope: **whole orgs** (`Jebel-Quant`, swept in full) and
**individually named repos** (`cvxgrp/cvxrisk`, `cvxgrp/simulator`,
`cvxgrp/cvxcla`, `cvxgrp/cvxmarkowitz`). The second exists because cvxgrp has
100+ repos and only four are yours.

**Archived repos are never monitored.** They are dropped from the GitHub sweep
*and* their local clones are skipped, so a checkout left on disk cannot keep a
dead repo on the board — that gap kept `rhiza-brainbug` showing as the fleet's
one red repo for a while after it was archived. Set `JQ_INCLUDE_ARCHIVED=true`
to opt back in.

Dropping a repo stops new samples but leaves its **history**, so it still
appears in time windows that reach back before the change. To erase that too:

```bash
./scripts/purge-repo.sh Jebel-Quant/rhiza-brainbug   # irreversible
```

The script enables Prometheus's admin API, deletes the series, cleans
tombstones, and turns the admin API straight back off. It is not left enabled:
Grafana's datasource proxy lets any viewer — and anonymous access is on — reach
arbitrary paths on the datasource, which would put "delete every metric" one
request away from a read-only visitor. Both label generations are purged, since
repos predating the `owner/name` rename have series under a bare name too.

Right after a purge, `/api/v1/series` may still list the series while queries
return nothing: that is stale head-block index metadata, cleared at the next
head compaction (~2h). No panel is affected, because panels run queries — and
neither is the repo picker, whose `label_values` resolves through the query
path. The script reports both numbers so the difference is visible rather than
alarming.

```bash
./scripts/up.sh     # builds, mints a token from `gh auth token`, starts everything
./scripts/down.sh   # stop; add --volumes to discard the history too
```

| | |
|---|---|
| Dashboard | <http://localhost:3000/d/jq-fleet> |
| Alert rules | <http://localhost:3000/alerting/list> |
| Prometheus | <http://localhost:9090> |
| Raw metrics | <http://localhost:9109/metrics> |

### The "Sign in" button

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

### "No data" usually means the Mac was asleep

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

### Clicking a tile

Every tile except *Repos monitored* links to a table listing exactly what is
behind that number — collapsed under *Drill-down* at the bottom, opened full
screen by the click. Each drill-down's row count is the tile's value by
construction, and each repo name links to GitHub.

The tiles are laid out in two rows because that is how the data splits. The top
row is what **GitHub** says about the repos — a headline count, the two workload
counts (open PRs, open issues), then the three problem counts. The bottom row is
what **this machine** says about the clones: not in sync, dirty, off default
branch. A green top row with an amber bottom row means the fleet is healthy and
your checkouts are stale — a different problem, and a much cheaper one.

*Open PRs* and *Open issues* are deliberately **not** status-coloured. They are
workload, not verdicts; the status palette is reserved for things that actually
mean good or bad, so colouring them would make a normal backlog read as a
failure.

## How it fits together

```
your clones ──(read-only bind mount, git --no-optional-locks)──┐
                                                               ├─► collector ─► Prometheus ─► Grafana
GitHub REST API ──(repos, runs, PRs, releases)─────────────────┘   :9109         :9090         :3000
```

The collector holds a cached snapshot and refreshes it on two independent
timers — GitHub every 5 minutes, the local clones every 60 seconds — so a scrape
never waits on the API, and an API outage does not blank out the local panels.
Prometheus scrapes every 30s and keeps 180 days.

## What it watches

**Template drift.** Each repo's `.rhiza/template.yml` `ref` against the newest
published `rhiza` release, as a count of releases missed. Repos pinned to a
branch or a sha show as *not a release* rather than being silently counted as
current.

The pointer is **always read from GitHub's default branch, never from your
clone.** Drift is a property of the repo, and a clone can be arbitrarily stale —
an early version read it from disk to save an API call, which made every
un-pulled checkout look like an out-of-date repo and understated fleet progress
badly. Your clone's own pinned ref is still reported, as the *Checkout ref*
column of the local table, so a stale checkout is visible instead of silently
overwriting the truth.

To keep that correctness free, the pointer read is cached against the default
branch's sha: the file cannot have changed if the branch head has not moved, so
a steady-state refresh spends no extra calls.

**CI on the default branch.** The latest completed run of **every** workflow,
not just the most recent run overall. Pull-request runs are excluded, so this is
the state of `main`, not of someone's branch. A repo is red if *any* workflow is
red; `jq_ci_workflow_success{repo,workflow}` carries the per-workflow detail and
`jq_ci_workflows_failing` counts them.

Two traps live here, and the first cut of this collector fell into both:

- Fetching `per_page=1` returns one run, so a repo whose docs build is failing
  reports green because a *different* workflow ran more recently. `homotopy` sat
  green with `Build and publish PDF` broken; correcting it turned up **11**
  failing workflows fleet-wide, one of them red for three months.
- GitHub orders runs by `created_at`, not by when they finished, so even "the
  newest run" can sort below an older one that took longer or was re-run. Runs
  are compared on `updated_at` instead.

Workflows are keyed by **name**, not `workflow_id`. A renamed or recreated
workflow file leaves two ids sharing one name, and since the metric is labelled
by name, keying on the id exported two series with identical labels — Prometheus
dropped 16 samples per scrape with *"samples with different value but same
timestamp"*.

**Open pull requests and issues.** Per PR: age, author, draft flag, and a
rolled-up check state. Check *runs* are used rather than the combined-status
endpoint, because GitHub Actions does not report as a commit status and every
repo would otherwise show as having no checks.

In the pull-request tables the repo, number and title cells all link out: the
number and title open the PR itself, the repo opens its pull-request list. The
URL is built from the `repo` label and the PR number rather than carrying
GitHub's `html_url` as a label — the label is already `owner/name`, so
`github.com/<repo>/pull/<number>` is exactly the canonical URL.

Issue counts cost no extra API call — they ride the repo listing. GitHub's
`open_issues_count` counts pull requests as issues, so the open PR total is
subtracted back out; that PR total is the unclipped one, so a repo with more
open PRs than `JQ_MAX_PRS_PER_REPO` still reports both numbers correctly.

**Local working copies.** Branch, dirty and untracked counts, stashes, ahead and
behind, and whether the clone sits on its default branch. `~/repos` is scanned
two levels deep, and a clone is kept only if its `origin` belongs to a monitored
org or is named explicitly — so an upstream `numpy` checkout, or the repos under
`~/repos/tschm`, are skipped without any extra configuration.

### The `repo` label is `owner/name`

Not the bare name. Two orgs are in scope and a bare name is only unique within
one owner, so the pair is the identity: the dashboard joins table frames on this
label and builds every GitHub link out of it, and both need it unique on its
own. It also makes alert annotations work unmodified — `{{ $labels.repo }}` is
already the full path.

## Two things worth knowing about the local numbers

*Ahead and behind are as stale as your last fetch.* The collector never fetches
— the mount is read-only and it must never be the reason a repo grows an
`index.lock` mid-rebase. So `jq_local_fetch_age_seconds` is exported next to
them; read the two together. Several clones here had not fetched in weeks.

*`In sync` does not have that problem.* It compares the local default-branch
commit with the sha GitHub reports, which needs no fetch at all. When the two
columns disagree, trust this one.

*Nothing on the local row describes the repo.* `Checkout ref` is what your clone
pins, not what the repo pins — when it differs from the *Template drift* row,
your checkout is behind, not the repo.

## Configuration

Everything lives in `.env` (see `.env.example`). The useful knobs:

| Variable | Default | |
|---|---|---|
| `JQ_ORGS` | `Jebel-Quant` | Orgs swept in full, comma-separated. |
| `JQ_REPOS` | the four cvxgrp repos | Individually named repos, as `owner/name`. Use this for orgs where only some repos are yours. |
| `JQ_TEMPLATE_REPO` | `Jebel-Quant/rhiza` | Whose releases define "up to date", as `owner/name`. |
| `JQ_IGNORE` | — | Repos to leave out, as bare names or `owner/name`. Applies to both the GitHub sweep and the local scan. |
| `JQ_INCLUDE_ARCHIVED` | `false` | Archived repos are dropped from both the GitHub sweep and the local scan. |
| `JQ_REPO_ROOT_HOST` | `../..` | Which host folder to mount. Defaults to all of `~/repos`. |
| `JQ_SCAN_DEPTH` | `2` | How deep to look for clones, so `~/repos/<org>/<repo>` is found. |
| `JQ_GITHUB_INTERVAL` | `300` | Seconds between GitHub refreshes. |
| `PROM_RETENTION` | `180d` | How much history to keep. |

### API budget

A refresh costs roughly `3 × repos + open PRs` REST calls. Measured on this
fleet of 32: **119 calls** for a steady-state refresh, or ~1430/hour at the
default cadence, against an authenticated budget of 5000. A cold start adds one
pointer read per repo; after that the sha-cache skips them — measured 32 pointer
reads on the first refresh and **1** on the second, that one being the single
repo whose default branch had moved. `jq_github_rate_limit_remaining` is on the *Collector health* row so the
headroom is visible rather than assumed. Lower `JQ_GITHUB_INTERVAL` only if that
number stays comfortable.

## Serving it world-readable

The default stack is a private, loopback-only tool. Making it public needs two
separate things — clean **data** and a locked-down **access mode** — and getting
only the first right leaks.

**1. Data.** `JQ_PUBLIC_ONLY=true` (already set in `.env`) drops private repos
completely: not just their details, but their existence. A private repo's name,
its workflow names, its PR titles and its local branch names are all disclosure.
Purge whatever was collected before you set it:

```bash
./scripts/purge-repo.sh Jebel-Quant/some-private-repo ...
```

**2. Access mode.** Purging is not enough on its own. With anonymous access on,
a visitor can POST arbitrary PromQL to `/api/ds/query` and read the raw label
index through `/api/datasources/proxy` — and **purged names linger in that index
for hours**, until head compaction. Measured on this stack: after deleting all
seven private repos, the index still returned every one of their names, and a
restart did not clear them.

So serve it through Grafana's public-dashboard link, which runs only that one
dashboard's queries and exposes no datasource:

```bash
docker compose -f docker-compose.yml -f docker-compose.public.yml up -d
./scripts/check-public-safe.sh          # must pass before you expose anything
```

The overlay turns anonymous access off and public dashboards on. The preflight
verifies every exported repo really is public *on GitHub* (not merely labelled
so here), that anonymous queries and the datasource proxy are both refused, and
that Prometheus and the collector are still loopback-only. It exits non-zero if
not.

Then open the board as `admin`, *Share → Public dashboard*, and share only that
link. Verified behaviour: the public link and its own panel queries return 200;
`/api/search`, `/api/ds/query` and the datasource proxy all return 401.

**Public dashboards do not resolve template variables**, so the `$repo` picker
would leave every panel showing "No data". `scripts/make-public-dashboard.py`
generates `fleet-public.json` from `fleet.json` with the variable and its
selectors stripped — equivalent queries, because the collector is already
restricted to public repos. Re-run it after changing `fleet.json`.

Nothing is on the internet until you put it there. The public URL is still only
reachable on `localhost` — a tunnel (Cloudflare, Tailscale Funnel) or a host with
a public address is a separate, deliberate step.

To go back to the convenient local setup, drop the overlay:
`docker compose up -d`.

## Running it world-readable on a server

The laptop stack is the wrong shape for this: Docker pauses when the Mac sleeps,
so any tunnel to it is dead most of the time, and there are no working copies on
a server anyway. `docker-compose.server.yml` is a standalone stack for an
always-on host — use it *instead of* `docker-compose.yml`, not as an overlay.

```bash
export GITHUB_TOKEN=ghp_...          # needs only public_repo
export GF_ADMIN_PASSWORD=...         # refuses to start without one
docker compose -f docker-compose.server.yml up -d
./scripts/check-public-safe.sh       # must pass before you share anything
```

Then sign in as admin, open *Jebel-Quant Fleet (public)* → *Share* → *Public
dashboard*, and share only that link.

It differs from the laptop stack in four ways, all following from "no working
copies here, and strangers can reach it":

| | |
|---|---|
| `JQ_REPO_ROOT` empty | local scanning skipped entirely — a clean no-op, not an error every minute |
| `JQ_PUBLIC_ONLY` forced on | private repos are never gathered, so they cannot leak |
| only Grafana publishes a port | Prometheus and the collector talk over the compose network and are unreachable from outside |
| anonymous off, public dashboards on | a public link serves one dashboard's queries with no datasource behind it |

Measured on a test run of this exact file: 719 series, **0** local series, 23
repos, 0 private, and the anonymous reach is `200` for the public dashboard and
`401` for `/api/search`, `/api/ds/query` and the datasource proxy.

### On a VPS

Any host with Docker runs `docker-compose.server.yml` unmodified — that is what
it was written for, and it was verified end to end before being committed.
A €4/month Hetzner CX22, a DigitalOcean droplet or a Lightsail instance are all
comfortably big enough; the whole stack idles under 500 MB.

```bash
# on the server, as a non-root user in the docker group
git clone https://github.com/Jebel-Quant/monitoring.git && cd monitoring

export GITHUB_TOKEN=github_pat_...     # public_repo scope is enough
export GF_ADMIN_PASSWORD="$(openssl rand -base64 24)"   # keep this somewhere
docker compose -f docker-compose.server.yml up -d

./scripts/check-public-safe.sh          # must pass before sharing anything
```

Then reach Grafana, sign in as `admin`, open **Jebel-Quant Fleet (public)** →
*Share* → *Public dashboard*, and share only that link.

**Add TLS with the overlay.** On its own the server stack publishes plain HTTP
on 3000, and the admin login should not cross the internet in clear.
`docker-compose.tls.yml` puts Caddy in front, stops publishing Grafana at all,
and obtains and renews a Let's Encrypt certificate by itself:

```bash
export FLEET_DOMAIN=fleet.example.com
export ACME_EMAIL=you@example.com
docker compose -f docker-compose.server.yml -f docker-compose.tls.yml up -d
```

With the overlay, only Caddy publishes anything (80, 443, 443/udp) — Grafana,
Prometheus and the collector are reachable only on the compose network. The
overlay also sets `GF_SERVER_ROOT_URL` so Grafana builds correct absolute links.

Point an A record at the box first: the ACME HTTP-01 challenge needs the name to
resolve and port 80 to be open before Caddy can get a certificate. Keep the
`caddy-data` volume — the certificates live there.

Keeping it current is `git pull && docker compose -f docker-compose.server.yml up -d --build`.
Only the *public* dashboard is provisioned to that host, so a change to
`fleet.json` reaches it via `scripts/make-public-dashboard.py`, which CI
enforces is not stale.

### Why not Fly.io

Tried and abandoned, recorded so nobody repeats it. Fly's docs describe
`[build.compose]` in detail, but **flyctl v0.4.95 does not implement it**:
`fly deploy` answers *"app does not have a Dockerfile or buildpacks
configured"*, `fly launch` writes a generic config ignoring the compose file
entirely, and neither command has a `--compose` flag. Two further gotchas found
before hitting that wall, in case the feature lands later:

- bind-mounted **directories** are rejected outright (*"is a directory"*), so
  the Grafana provisioning tree has to be injected file-by-file via `[[files]]`
- Fly Volumes mount at the **same path in every container** — per-container
  mounts are unsupported — so both stateful services must be relocated under one
  mount (`--storage.tsdb.path=/data/prometheus`, `GF_PATHS_DATA=/data/grafana`).
  That layout was verified locally, including that the public dashboard token
  survives a restart, which is what keeps the public URL stable.

Deploying there today needs the lower-level Machines API: three images in a
registry and a hand-written machine config with a `containers` array, giving up
`fly deploy` for every subsequent update.

### The public dashboard is a stripped copy

`scripts/make-public-dashboard.py` drops every panel sourced from `jq_local_*`
as well as the `$repo` variable. Those panels describe *one particular machine* —
its dirty files, its checked-out branch names, how long since it fetched. None
of that is secret when the repos are public, but it is someone's working state,
it is meaningless to anyone else, and on a server it would be empty regardless.
Re-run the generator after changing `fleet.json`; CI fails if the copy is stale.

## Alerting

Six rules are provisioned into the `Jebel-Quant` folder, each multi-dimensional
so you get one alert instance per repo:

| Rule | Fires when |
|---|---|
| CI red on the default branch | last completed run failed, for 24h |
| Repo behind the template | one or more releases behind, for 7d |
| Open pull request with red checks | a PR's checks are red, for 24h |
| Working copy left dirty | uncommitted changes, for 3d |
| Clone parked off its default branch | non-default branch checked out, for 3d |
| Collector is not refreshing | no successful refresh in 30m |

The long `for` windows are deliberate: this is a fleet board, not a pager, and a
repo that goes red for an hour during a normal push-fix cycle is not worth a
notification.

**Notifications need one more step.** The rules fire and show up in the Grafana
UI as provisioned, but they route to Grafana's built-in email contact point,
which has no SMTP configured — so nothing is delivered anywhere yet. To actually
get notified, add a contact point (Slack webhook, email, ntfy) under
*Alerting → Contact points* and point the default notification policy at it.
Provisioning it here would mean committing a webhook URL to the repo.

## Editing the dashboard

The board is provisioned from `grafana/dashboards/fleet.json` and is read-only in
the UI on purpose — edits there would be silently reverted. Change the JSON and
Grafana reloads it within 30 seconds. To iterate visually instead: copy the
panel, tweak it in the UI, then *Panel JSON* → paste back into the file.

Colours follow a validated palette: sequential blue where the number is a
*magnitude* (releases behind), and the reserved status colours only where a
colour actually means good or bad. Status cells always carry a glyph and a word
as well, so nothing depends on colour alone.

### Traps worth not re-introducing

JSON cannot carry comments, so these are recorded here instead. The first two
were live in the first cut of this board.

**Never write `count(...) or vector(0)` in a panel with a time range.** The
fallback fires at *every* timestamp where the left side has no series, so the
chart paints a confident flat zero across all the time before the collector
existed — it asserts "nothing was wrong last week" about a week it knows nothing
about. Use the `bool` modifier instead: `sum(jq_ci_last_run_success == bool 0)`
evaluates per existing series, so it is 0 when the repos really are all green
and simply absent when there is no data. `or vector(0)` is only safe on a stat
tile reading a single instant.

**Renaming a label starts a new series.** The `repo` label changed from a bare
name to `owner/name` when the cvxgrp repos were added, so history from before
that point lives under the old names. Windows spanning the change show both
generations; they age out. This is inherent to Prometheus, not a misconfiguration.

**Never `group_left` onto an `_info` metric without a `topk` guard.** The
`_info` metrics carry a label that changes value — `ref`, `branch`,
`conclusion`, `checks`. When one changes, Prometheus returns the old *and* the
new series until the old is marked stale, and `group_left` then aborts the whole
query with `found duplicate series for the match group`. The panel goes blank
exactly while someone is doing the work the board exists to show — which is how
this was found, mid-`rhiza` bump. Wrap the right-hand side:
`group_left(ref) topk by (repo) (1, jq_rhiza_template_ref_info)`. During the
overlap it may show the older label value; that self-corrects on the next
scrape, which is far better than an error. The same applies to `and on(...)`,
which does not error but does double-count.

**A `state-timeline` colours from thresholds, not from value mappings.** With
`color.mode: thresholds` and no `thresholds` block, Grafana silently falls back
to its default steps (green up to 80), so `0` and `1` both render green and a
failing repo looks fine. Define the steps explicitly and let the mappings supply
only the words.
