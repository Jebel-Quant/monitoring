#!/usr/bin/env bash
# Supervise the three processes that make up the board.
#
# Not an init system, on purpose: there is nothing here to restart. The three
# are one board - a dead collector means empty panels, a dead Prometheus means
# no history, a dead Grafana means no board at all - so any of them exiting
# takes the container down and Docker's own restart policy handles it. That
# keeps `docker ps` honest about whether the board is up, which a supervisor
# quietly restarting one process in a still-"healthy" container would not.
set -uo pipefail

die() { printf '\033[31mfleet:\033[0m %s\n' "$1" >&2; exit 1; }
say() { printf '\033[36mfleet:\033[0m %s\n' "$1"; }

# -- what you have to have provided ------------------------------------------
if [[ ! -f "${JQ_REPOS_FILE:-/config/repos.yml}" ]]; then
  die "no repos.yml at ${JQ_REPOS_FILE:-/config/repos.yml} - mount one:
       -v \"\$PWD/repos.yml:/config/repos.yml:ro\"
       It lists the repos to monitor; nothing is discovered."
fi
if [[ -z "${GITHUB_TOKEN:-}" ]]; then
  say "no GITHUB_TOKEN - GitHub allows 60 unauthenticated calls an hour, which
       is not enough for a fleet. Pass -e GITHUB_TOKEN=\"\$(gh auth token)\"."
fi
# Only if the fleet actually has a GitLab repo in it. A GitHub-only board must
# not start warning about credentials it has no use for, so this greps the file
# rather than just testing the variable.
if [[ -z "${GITLAB_TOKEN:-}" ]] \
  && grep -qE '^[[:space:]]*forge:[[:space:]]*.?gitlab' "${JQ_REPOS_FILE:-/config/repos.yml}"; then
  say "repos.yml lists a GitLab repo but no GITLAB_TOKEN is set - only public
       projects will be readable. Pass -e GITLAB_TOKEN=\"...\"."
fi
# Not fatal: a board with no working copies mounted is a supported way to run
# this. Saying so once beats a user wondering why half the panels are empty.
if [[ ! -d "${JQ_HOST_ROOT:-/host}" ]]; then
  say "${JQ_HOST_ROOT:-/host} is not mounted - the working-copy panels will stay
       empty. Add -v \"\$HOME:/host:ro\" to fill them in."
fi

mkdir -p /data/prometheus /data/grafana

# -- shut down together ------------------------------------------------------
pids=()
stop() {
  trap - TERM INT
  # Signal the group rather than each pid: promtool-style children and
  # Grafana's own subprocesses are otherwise left behind holding /data.
  kill -TERM "${pids[@]}" 2>/dev/null
  wait
  exit 0
}
trap stop TERM INT

# -- prometheus --------------------------------------------------------------
prom_args=(
  --config.file=/etc/prometheus/prometheus.yml
  --storage.tsdb.path=/data/prometheus
  --storage.tsdb.retention.time="${PROM_RETENTION:-180d}"
  --web.enable-lifecycle
)
# Deleting series is irreversible and the API needs no authentication, so it is
# off unless you asked for it - `purge-repo` says so when it is not there.
if [[ "${JQ_PROM_ADMIN_API:-false}" == "true" ]]; then
  prom_args+=(--web.enable-admin-api)
  say "Prometheus admin API is ENABLED - series can be deleted without a password"
fi
# Process substitution, not a pipe: a pipeline's $! is the *last* command in
# it, so `kill $!` would reap the sed and leave Prometheus running.
# `sed -u` because a block-buffered log is a log you cannot tail.
prometheus "${prom_args[@]}" > >(sed -u 's/^/prometheus | /') 2>&1 &
pids+=($!)

# -- grafana -----------------------------------------------------------------
/usr/share/grafana/bin/grafana server \
  --homepath=/usr/share/grafana \
  --config="${GF_PATHS_CONFIG}" > >(sed -u 's/^/grafana    | /') 2>&1 &
pids+=($!)

# -- the collector -----------------------------------------------------------
# Started last because it is the one that refuses to start on a repos.yml it
# cannot act on, and that error is what you want at the bottom of
# `docker logs` rather than buried above Grafana's startup banner.
# -u because this log is the only window into the container: block-buffered
# output would hold a startup error until enough of it accumulated to flush.
python -u -m jq_collector > >(sed -u 's/^/collector  | /') 2>&1 &
pids+=($!)

say "Grafana http://localhost:3000/d/jq-fleet  ·  Prometheus :9090  ·  metrics :9109"

# First exit wins: report which, then bring the rest down with it.
wait -n
code=$?
say "a process exited ($code) - stopping the others"
kill -TERM "${pids[@]}" 2>/dev/null
wait
exit "$code"
