---
title: Configuration
description: repos.yml and the environment, dropping and purging a repo, and the GitHub API budget.
keywords: repos.yml, docker run, github token, prometheus retention, api rate limit, purge series
---

# Configuration

**Which repos** is `repos.yml`, the one file you own. **Everything else** is
environment variables on the `docker run`.

## The fleet is an explicit list

`repos.yml` names every monitored repo, one
entry per checkout on this machine:

```yaml
repos:
  - path: ~/repos/jebel-quant/rhiza
  - path: ~/repos/cvxgrp/cvxsimulator
  - repo: Jebel-Quant/actions      # monitored, but not checked out here
```

`owner/name` is read from each checkout's `origin` remote, so the path is all
you write. Nothing is discovered: a repo is on the board because it is in this
file, and for no other reason. The collector reads the file itself, at startup,
from `/config/repos.yml` — nothing is generated from it, so there is no second
file to fall out of step.

This replaced a whole-org GitHub sweep plus a directory walk under one mounted
root. Both decided membership on their own: a new repo in the org arrived
unasked, a shared org like cvxgrp dragged in 100+ repos that were not yours, and
any checkout that happened to sit under the root joined the board because its
origin looked right.

Edit `repos.yml`, `docker restart jq-fleet`, and the fleet is whatever you just
wrote. Both halves of the collector read the same list, so the GitHub panels
and the working-copy panels can never disagree about who is in scope.

**Archived repos are never monitored.** They are dropped from the GitHub half
*and* their local checkouts are skipped, so a checkout left on disk cannot keep a
dead repo on the board — that gap kept `rhiza-brainbug` showing as the fleet's
one red repo for a while after it was archived. Set `JQ_INCLUDE_ARCHIVED=true`
to opt back in.

## Dropping a repo

Dropping a repo stops new samples but leaves its **history**, so it still
appears in time windows that reach back before the change. To erase that too:

```bash
docker exec jq-fleet purge-repo Jebel-Quant/rhiza-brainbug   # irreversible
```

This needs Prometheus's admin API, which is off unless the container was started
with `-e JQ_PROM_ADMIN_API=true` — so a purge is: recreate the container with
that flag, purge, recreate it without. It is deliberately not left on: Grafana's
datasource proxy lets any viewer — and anonymous access is on — reach arbitrary
paths on the datasource, which would put "delete every metric" one request away
from a read-only visitor. `purge-repo` says exactly this if you run it with the
API disabled. Both label generations are purged, since repos predating the
`owner/name` rename have series under a bare name too.

Right after a purge, `/api/v1/series` may still list the series while queries
return nothing: that is stale head-block index metadata, cleared at the next
head compaction (~2h). No panel is affected, because panels run queries — and
neither is the repo picker, whose `label_values` resolves through the query
path. The script reports both numbers so the difference is visible rather than
alarming.

## repos.yml

| Key | | |
|---|---|---|
| `path` | | A checkout on this machine, written as you would write it yourself. `~` is your home directory — which the container sees as the single `-v "$HOME:/host:ro"` mount — and a relative path is relative to it too. |
| `repo` | | `owner/name`. Optional next to a `path` — it overrides the origin, which is what you want for a fork whose board should follow upstream. On its own it monitors a repo you have not cloned: GitHub panels are gathered, the working-copy panels stay empty for that row. |

A bare string is shorthand for `path`. Duplicate entries, a path that is not a
checkout, and an entry with neither key all stop the collector at startup —
better a refusal than a board that is quietly one repo short.

The one thing that is *not* fatal is a `path` that cannot be reached alongside
an explicit `repo:`. That is what running without the `$HOME` mount looks like,
and it is a supported way to use this: the repo keeps its GitHub panels and the
working-copy panels stay empty. It is logged as a warning, because a typo looks
identical from inside the container.

## Environment

Passed as `-e NAME=value` on the `docker run`, or in `.env` if you use
`docker compose`.

| Variable | Default | |
|---|---|---|
| `GITHUB_TOKEN` | — | Must be able to read every repo in `repos.yml`. `gh auth token` prints a usable one. |
| `JQ_TEMPLATE_REPO` | `Jebel-Quant/rhiza` | Whose releases define "up to date", as `owner/name`. |
| `JQ_IGNORE` | — | Repos to drop without editing `repos.yml`, as bare names or `owner/name`. Applies to both halves. |
| `JQ_INCLUDE_ARCHIVED` | `false` | Archived repos are dropped from both halves. |
| `JQ_PUBLIC_ONLY` | `false` | Drop private repos entirely — not just their details, their existence. |
| `JQ_REPO_PATHS` | — | Where each checkout really is, as `owner/name=path` pairs. Filled from `repos.yml`; set it to override one entry — see [Checkout paths](#checkout-paths). |
| `JQ_HOST_ROOT` | `/host` | Where your home directory is mounted, and therefore what `~` in `repos.yml` means. |
| `JQ_PROM_ADMIN_API` | `false` | Enables the API [`purge-repo`](#dropping-a-repo) needs. Leave it off. |
| `JQ_GITHUB_INTERVAL` | `300` | Seconds between GitHub refreshes. |
| `JQ_MEASURE_MAX_AGE` | `86400` | Seconds an unchanged line/commit count may stand before it is retaken. See [Size and cadence](dashboard.md#size-and-cadence). |
| `PROM_RETENTION` | `180d` | How much history to keep. |

`repos.yml` is read at startup and turned into the fleet the collector actually
uses, so both halves see the same list. Setting `JQ_REPOS` by hand works too and
is what a deployment with no file to mount — a server, or CI — does instead.

## Checkout paths

The collector reads a repo at the path `repos.yml` gave for it, and nowhere
else. Paths are whatever they are on disk, so an entry like

```yaml
  - path: ~/repos/tschm/rhiza_projects/cs     # this is tschm/cs
```

means exactly that. Joining owner to name would produce `tschm/cs` and find
nothing, and that repo would drop off the working-copy panels while staying on
the GitHub ones — present on the board, and quietly missing half its columns.
Four repos in the fleet this was built against are laid out that way, which is
the whole reason `repos.yml` carries paths rather than deriving them.

Inside the container `~` is `$JQ_HOST_ROOT`, the mount point of your home
directory. That single mount is what makes an arbitrary layout expressible at
all: the per-repo bind mounts it replaced could only ever name
`<root>/<owner>/<name>`. To see what a path resolved to, read `jq_local_*`
series' `path` label at <http://localhost:9109/metrics>, or the startup line:

```
collector | INFO jq_collector.config fleet: 25 repos, 24 with a checkout
```

`JQ_REPO_PATHS` overrides the file, one repo at a time, for the case where a
path is right everywhere except in the container. A path containing a comma
cannot be expressed there — comma is the separator — which is one more reason
`repos.yml` is where the layout is normally written down.

## API budget

A refresh costs roughly `6 × repos + workflows + open PRs` REST calls. Measured
on this fleet of 25: **370 calls** for a steady-state refresh, or ~2220/hour at
the default 600s cadence, against an authenticated budget of 5000.

> An earlier version of this page said 119 calls and ~1430/hour. That was
> understated; the numbers above were measured by counting requests through a
> full refresh rather than derived from the formula.

Two caches keep it there. A cold start adds one pointer read per repo; after
that the sha-cache skips them — measured 32 pointer reads on the first refresh
and **1** on the second, that one being the single repo whose default branch
had moved. Coverage adds one artifact listing per repo (25 calls, ~150/hour),
but the report itself — a zip, the largest response here — is downloaded only
when the artifact id changes: **19 downloads cold, 0 on the next refresh.** `jq_github_rate_limit_remaining` is on the *Collector health* row so the
headroom is visible rather than assumed. Lower `JQ_GITHUB_INTERVAL` only if that
number stays comfortable.

