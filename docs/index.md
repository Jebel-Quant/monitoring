---
title: Start here
description: Get a Grafana board for your repo fleet running on localhost in about two minutes.
keywords: grafana, prometheus, github, repo fleet, ci monitoring, template drift
---

# Fleet monitoring

A Grafana board for the state of your repo fleet — template drift, CI on the
default branch, open pull requests, and the working copies on your machine —
with Prometheus keeping the history and six alert rules on top.

Everything runs in Docker on `localhost`. **Nothing is discovered:** a repo is
on the board because you listed it in `repos.yml`, and for no other reason.

## What you need

Docker, and the [`gh` CLI](https://cli.github.com) signed in — `up.sh` mints the
token from it. Otherwise put a `GITHUB_TOKEN` in `.env` yourself.

## The recipe

```bash
git clone https://github.com/Jebel-Quant/monitoring.git && cd monitoring
./scripts/up.sh          # writes repos.yml + .env from the examples, then stops
```

Now edit `repos.yml` — one entry per repo you want on the board:

```yaml
repos:
  - path: ~/repos/jebel-quant/rhiza     # a checkout on this machine
  - path: ~/repos/cvxgrp/cvxsimulator
  - repo: Jebel-Quant/actions           # monitored, but not cloned here
```

`owner/name` comes from each checkout's `origin`, so the path is all you write.
Then:

```bash
./scripts/up.sh          # builds and starts everything
open http://localhost:3000/d/jq-fleet
```

The board fills in within a minute — the local panels first, the GitHub panels
after the first API refresh.

!!! warning "All three ports bind to `127.0.0.1` only"

    Anonymous read access is on, so the board opens without signing in and
    `admin` / `admin` is only for settings. Published on `0.0.0.0` the stack
    would serve private repo names, PR titles and local branch names to anyone
    on the network with no password. Putting it on the internet is a separate,
    deliberate procedure — see [Serving it publicly](serving.md).

## Where to go next

<div class="grid cards" markdown>

- :material-tune: **[Configuration](configuration.md)**

    `repos.yml` and `.env`, dropping and purging a repo, and the API budget.

- :material-eye-outline: **[What it watches](metrics.md)**

    The four subjects, the metrics behind them, and why each is shaped the way
    it is — including two bugs that made repos look green while they were red.

- :material-view-dashboard-outline: **[The dashboard](dashboard.md)**

    Reading the tiles, editing the JSON, alerting, and four PromQL traps worth
    not re-introducing.

- :material-web: **[Serving it publicly](serving.md)**

    Public dashboards, the server stack, a VPS, TLS — and the preflight that
    must pass before any of it is exposed.

- :material-calendar-check: **[Day to day](operations.md)**

    Why panels say *No data*, and what the sign-in button actually is.

</div>

!!! tip "If every panel says \"No data\", the machine was probably asleep"

    Docker pauses with it. The *Data age* tile says how stale things are, and
    the next scrape lands a few seconds after waking. See
    [Day to day](operations.md).
