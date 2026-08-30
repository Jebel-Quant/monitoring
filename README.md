# Fleet monitoring

A Grafana board for the state of your repo fleet — template drift, CI on the
default branch, open pull requests, and the working copies on this machine —
with Prometheus keeping the history and six alert rules on top.

Everything runs in Docker on `localhost`. Nothing is discovered: a repo is on
the board because you listed it in `repos.yml`, and for no other reason.

## What you need

Docker, and the [`gh` CLI](https://cli.github.com) signed in (`up.sh` mints the
token from it — otherwise put a `GITHUB_TOKEN` in `.env` yourself).

## Recipe

```bash
git clone https://github.com/Jebel-Quant/monitoring.git && cd monitoring
./scripts/up.sh          # writes repos.yml + .env from the examples, then stops
```

Now **edit `repos.yml`** — one entry per repo you want on the board:

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
after the first API refresh. Both `repos.yml` and the generated
`docker-compose.repos.yml` are gitignored: they describe one machine's folders.

## Then what

| | |
|---|---|
| Add or drop a repo | edit `repos.yml`, `./scripts/up.sh` again — [details](docs/configuration.md) |
| Erase a dropped repo's history | [`./scripts/purge-repo.sh owner/name`](docs/configuration.md#dropping-a-repo) (irreversible) |
| Stop | `./scripts/down.sh` (add `--volumes` to discard the history too) |
| Edit the board | change `grafana/dashboards/fleet.json`; it reloads in 30s — [read the traps first](docs/dashboard.md#traps-worth-not-re-introducing) |
| Get notified | add a contact point under *Alerting → Contact points* — [why it is not provisioned](docs/dashboard.md#alerting) |
| Put it on the internet | [docs/serving.md](docs/serving.md) — do not skip the preflight |

| | |
|---|---|
| Dashboard | <http://localhost:3000/d/jq-fleet> |
| Alert rules | <http://localhost:3000/alerting/list> |
| Prometheus | <http://localhost:9090> |
| Raw metrics | <http://localhost:9109/metrics> |

All three ports bind to `127.0.0.1` only, because anonymous read access is on.
The board opens without signing in; `admin` / `admin` is only for settings.

## Docs

| | |
|---|---|
| [Configuration](docs/configuration.md) | `repos.yml`, `.env`, the API budget |
| [What it watches](docs/metrics.md) | the four subjects, the metrics, and why each is shaped that way |
| [The dashboard](docs/dashboard.md) | reading it, editing it, alerting, and the query traps |
| [Serving it publicly](docs/serving.md) | public dashboards, the server stack, a VPS, TLS |
| [Day to day](docs/operations.md) | why panels say *No data*, and what the sign-in button is |

**If every panel says "No data", the machine was probably asleep.** Docker
pauses with it. The *Data age* tile says how stale things are; the next scrape
lands a few seconds after waking. See [docs/operations.md](docs/operations.md).
