---
title: Configuration
description: repos.yml and .env, dropping and purging a repo, and the GitHub API budget.
keywords: repos.yml, dotenv, github token, prometheus retention, api rate limit, purge series
---

# Configuration

**Which repos** is `repos.yml`. **Everything else** is `.env` (see
`.env.example`).

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
file, and for no other reason. `scripts/up.sh` turns the file into
`docker-compose.repos.yml`, which mounts each checkout **read-only** at
`/repos/<owner>/<name>` — so an unlisted repo is not merely filtered out, it is
never visible to the container at all.

This replaced a whole-org GitHub sweep plus a directory walk under one mounted
root. Both decided membership on their own: a new repo in the org arrived
unasked, a shared org like cvxgrp dragged in 100+ repos that were not yours, and
any checkout that happened to sit under the root joined the board because its
origin looked right.

Edit `repos.yml`, run `./scripts/up.sh` again, and the fleet is whatever you
just wrote. Both halves of the collector read the same list, so the GitHub
panels and the working-copy panels can never disagree about who is in scope.

**Archived repos are never monitored.** They are dropped from the GitHub half
*and* their local checkouts are skipped, so a checkout left on disk cannot keep a
dead repo on the board — that gap kept `rhiza-brainbug` showing as the fleet's
one red repo for a while after it was archived. Set `JQ_INCLUDE_ARCHIVED=true`
to opt back in.

## Dropping a repo

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

## repos.yml

| Key | | |
|---|---|---|
| `path` | | A checkout on this machine. `~` and paths relative to the repo both work. |
| `repo` | | `owner/name`. Optional next to a `path` — it overrides the origin, which is what you want for a fork whose board should follow upstream. On its own it monitors a repo you have not cloned: GitHub panels are gathered, the working-copy panels stay empty for that row. |

A bare string is shorthand for `path`. Duplicate entries, a path that is not a
checkout, and an entry with neither key are all refused at generate time —
better a refusal than a board that is quietly one repo short.

## .env

| Variable | Default | |
|---|---|---|
| `GITHUB_TOKEN` | — | Must be able to read every repo in `repos.yml`. `up.sh` mints one from `gh auth token`. |
| `JQ_TEMPLATE_REPO` | `Jebel-Quant/rhiza` | Whose releases define "up to date", as `owner/name`. |
| `JQ_IGNORE` | — | Repos to drop without editing `repos.yml`, as bare names or `owner/name`. Applies to both halves. |
| `JQ_INCLUDE_ARCHIVED` | `false` | Archived repos are dropped from both halves. |
| `JQ_PUBLIC_ONLY` | `false` | Drop private repos entirely — not just their details, their existence. |
| `JQ_GITHUB_INTERVAL` | `300` | Seconds between GitHub refreshes. |
| `JQ_MEASURE_MAX_AGE` | `86400` | Seconds an unchanged line/commit count may stand before it is retaken. See [Size and cadence](dashboard.md#size-and-cadence). |
| `PROM_RETENTION` | `180d` | How much history to keep. |

`scripts/gen-repos.py` turns `repos.yml` into the `JQ_REPOS` list the
collector actually reads, so both halves see the same fleet. Setting `JQ_REPOS`
by hand works too, and skips `repos.yml` entirely — but then nothing mounts the
checkouts, so only the GitHub panels have anything to say.

## API budget

A refresh costs roughly `3 × repos + open PRs` REST calls. Measured on this
fleet of 32: **119 calls** for a steady-state refresh, or ~1430/hour at the
default cadence, against an authenticated budget of 5000. A cold start adds one
pointer read per repo; after that the sha-cache skips them — measured 32 pointer
reads on the first refresh and **1** on the second, that one being the single
repo whose default branch had moved. `jq_github_rate_limit_remaining` is on the *Collector health* row so the
headroom is visible rather than assumed. Lower `JQ_GITHUB_INTERVAL` only if that
number stays comfortable.

