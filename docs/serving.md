# Serving it world-readable

## From this machine

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

## On a server

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
| the fleet is `JQ_REPOS` | no checkouts to derive it from, so the list is named directly rather than in `repos.yml` |
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
git clone https://github.com/Jebel-Quant/monitoring.git && cd monitoring

cat > .env <<'SETTINGS'
GITHUB_TOKEN=github_pat_...        # public_repo scope is enough
GF_ADMIN_PASSWORD=...              # 16+ chars; openssl rand -base64 24
FLEET_DOMAIN=fleet.example.com
ACME_EMAIL=you@example.com
# The fleet. Required - there are no checkouts here to derive it from, and the
# stack refuses to start without it rather than serving an empty board.
JQ_REPOS=Jebel-Quant/rhiza,Jebel-Quant/actions,cvxgrp/cvxsimulator
SETTINGS
chmod 600 .env

./scripts/bootstrap-server.sh      # checks everything, then starts the stack
```

A `.env` rather than `export`, so the settings survive a reboot - compose reads
it automatically, and it is gitignored.

Afterwards, every routine change - a new repo in `JQ_REPOS`, a `git pull` - is
`./scripts/restart.sh`. It skips the one-time DNS and token checks, waits for
the collector's first GitHub refresh, and re-runs the preflight, because the
setting you change most often is the fleet:

```bash
git pull && ./scripts/restart.sh
```

**Do not retype that `JQ_REPOS` line.** A fleet transcribed by hand is a fleet
that quietly diverges from the one in `repos.yml` - a repo added on the laptop
never reaches the board, and nothing reports the difference. Generate it on a
machine that has the checkouts and copy the single line across:

```bash
python3 scripts/gen-repos.py --env
# JQ_REPOS=Jebel-Quant/monitoring,cvxgrp/cvxrisk,tschm/pyhrp
```

`--env` writes nothing and prints only that line, so it pipes:

```bash
python3 scripts/gen-repos.py --env | ssh fleet-host 'cat >> monitoring/.env'
```

Re-run it after editing `repos.yml` and replace the line on the server. The
server keeps naming its fleet in `.env` rather than reading `repos.yml`, because
every path in that file describes a machine the server is not.

Note the token has to be able to read every repo named in `JQ_REPOS`. A
fine-grained token scoped to one org cannot see another's, and the collector
logs `listed repo ... is not readable` when that happens — one unreadable entry
costs one row, not the whole board.

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

