---
title: Start here
description: Get a Grafana board for your repo fleet running on localhost in about two minutes.
keywords: grafana, prometheus, github, repo fleet, ci monitoring, template drift
---

# Fleet monitoring

A Grafana board for the state of your repo fleet — template drift, CI on the
default branch, open pull requests, and the working copies on your machine —
with Prometheus keeping the history and six alert rules on top.

One container, one file. **Nothing is discovered:** a repo is on the board
because you listed it in `repos.yml`, and for no other reason.

## What you need

Docker, and the [`gh` CLI](https://cli.github.com) signed in — or any GitHub
token that can read the repos you list.

## The recipe

Write a `repos.yml` — one entry per repo you want on the board:

```yaml
repos:
  - path: ~/repos/jebel-quant/rhiza     # a checkout on this machine
  - path: ~/repos/cvxgrp/cvxsimulator
  - repo: Jebel-Quant/actions           # monitored, but not cloned here
```

`owner/name` comes from each checkout's `origin`, so the path is all you write.
Then:

```bash
docker run -d --name jq-fleet \
  -p 127.0.0.1:3000:3000 \
  -v "$PWD/repos.yml:/config/repos.yml:ro" \
  -v "$HOME:/host:ro" \
  -v jq-fleet-data:/data \
  -e GITHUB_TOKEN="$(gh auth token)" \
  ghcr.io/jebel-quant/monitoring:latest

open http://localhost:3000/d/jq-fleet
```

That is the whole install. The dashboard, the datasource, the alert rules and
the scrape config are baked into the image, so there is nothing to clone and
nothing on your disk but `repos.yml`.

The board fills in within a minute or two — the GitHub panels first, the
working-copy panels once the first scan of the mount completes.

### The four flags

| | |
|---|---|
| `-v .../repos.yml:/config/repos.yml:ro` | Required. The fleet — see [Configuration](configuration.md). |
| `-v "$HOME:/host:ro"` | Your home directory, read-only, so `~/...` in `repos.yml` resolves. Leave it out and the working-copy panels stay empty; everything the GitHub half reports still works. |
| `-v jq-fleet-data:/data` | Prometheus history and Grafana's database. Leave it out and both start empty at every run. |
| `-e GITHUB_TOKEN=...` | Needs `repo` and `read:org`. Without one GitHub allows 60 calls an hour, which is not a fleet. |
| `-e GITLAB_TOKEN=...` | Only if the fleet has a GitLab repo in it. Needs `read_api`. |

!!! warning "Publish the ports to `127.0.0.1` only"

    Anonymous read access is on, so the board opens without signing in and
    `admin` / `admin` is only for settings. Published on `0.0.0.0` the
    container would serve private repo names, PR titles and local branch names
    to anyone on the network with no password. This is built to run on one
    machine; putting it on the internet is not a supported path.

## Where to go next

<div class="grid cards" markdown>

- :material-tune: **[Configuration](configuration.md)**

    `repos.yml` and the environment, dropping and purging a repo, and the
    API budget.

- :material-eye-outline: **[What it watches](metrics.md)**

    The four subjects, the metrics behind them, and why each is shaped the way
    it is — including two bugs that made repos look green while they were red.

- :material-view-dashboard-outline: **[The dashboard](dashboard.md)**

    Reading the tiles, editing the JSON, alerting, and four PromQL traps worth
    not re-introducing.

- :material-calendar-check: **[Day to day](operations.md)**

    Why panels say *No data*, and what the sign-in button actually is.

</div>

!!! tip "If every panel says \"No data\", the machine was probably asleep"

    Docker pauses with it. The *Data age* tile says how stale things are, and
    the next scrape lands a few seconds after waking. See
    [Day to day](operations.md).
