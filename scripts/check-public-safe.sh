#!/usr/bin/env bash
# Preflight before serving the board world-readable.
#
# Checks the two things that actually leak: the DATA (does any private repo
# appear?) and the ACCESS MODE (can a visitor ask for something other than this
# dashboard?). Run it against the stack exactly as it will be exposed.
#
# Works against either stack. The laptop stack publishes Grafana on localhost
# and the collector on 9109; the server stack publishes neither - Grafana sits
# behind Caddy on a public hostname and the collector is reachable only inside
# the compose network. An earlier version assumed the laptop shape and, on a
# server, reported two false FAILs (it read "connection refused" as "a visitor
# can run any PromQL") and one false PASS (it looked for a container named
# jq-prometheus, found nothing, and concluded nothing was exposed).
set -uo pipefail
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

PROJECT=jq-monitoring
fails=0
ok()   { printf '  \033[32mPASS\033[0m  %s\n' "$1"; }
bad()  { printf '  \033[31mFAIL\033[0m  %s\n' "$1"; fails=$((fails + 1)); }
warn() { printf '  \033[33mWARN\033[0m  %s\n' "$1"; }

[[ -f .env ]] && { set -a; . ./.env; set +a; }

# Where Grafana is actually reachable. Behind Caddy that is the public URL, and
# probing localhost:3000 would prove nothing.
if [[ -n "${FLEET_DOMAIN:-}" ]] && curl -sk --max-time 10 "https://$FLEET_DOMAIN/api/health" >/dev/null 2>&1; then
  GRAFANA="https://$FLEET_DOMAIN"
elif curl -s --max-time 5 http://localhost:3000/api/health >/dev/null 2>&1; then
  GRAFANA=http://localhost:3000
else
  echo "cannot reach Grafana - is the stack up?" >&2
  exit 1
fi
echo "  probing $GRAFANA"

container() { docker ps -q --filter "label=com.docker.compose.project=$PROJECT" --filter "name=$1" | head -1; }

# 1. Every repo the collector exports must be public ON GITHUB - checked against
#    GitHub, not against our own visibility label, so a mislabelled snapshot
#    cannot pass. The collector may not publish a port, so ask it from inside.
cid=$(container collector)
if [[ -z "$cid" ]]; then
  bad "collector container not running - cannot verify which repos are exported"
else
  metrics=$(docker exec "$cid" python -c "
import urllib.request
print(urllib.request.urlopen('http://localhost:9109/metrics').read().decode())" 2>/dev/null)
  repos=$(grep '^jq_repo_info' <<< "$metrics" | sed 's/.*repo="\([^"]*\)".*/\1/' | sort -u)
  count=$(grep -c . <<< "$repos")
  if [[ "$count" -eq 0 ]]; then
    bad "collector exported no repos - it may still be starting, or failing"
  else
    private=0
    while IFS= read -r repo; do
      [[ -z "$repo" ]] && continue
      vis=$(curl -s --max-time 15 -H "Authorization: Bearer ${GITHUB_TOKEN:-}" \
              "https://api.github.com/repos/$repo" |
            python3 -c 'import json,sys
try: print(json.load(sys.stdin).get("visibility",""))
except Exception: print("")' 2>/dev/null)
      if [[ "$vis" != "public" ]]; then
        bad "exported repo is not public on GitHub: $repo (${vis:-unknown})"
        private=$((private + 1))
      fi
    done <<< "$repos"
    [[ "$private" -eq 0 ]] && ok "all $count exported repos are public on GitHub"
  fi
fi

# 2/3. A visitor must not be able to ask for anything but this dashboard.
#      A refused connection counts as safe; only a 2xx is a failure.
probe() {
  local label=$1 code=$2
  case "$code" in
    401|403|404) ok "$label refused (HTTP $code)" ;;
    000)         ok "$label unreachable" ;;
    2*)          bad "$label returned HTTP $code - a visitor can reach it" ;;
    *)           warn "$label returned HTTP $code - check manually" ;;
  esac
}
probe "anonymous /api/ds/query" "$(curl -sk -o /dev/null -w '%{http_code}' --max-time 15 \
  -X POST "$GRAFANA/api/ds/query" -H 'Content-Type: application/json' \
  -d '{"queries":[{"refId":"A","datasource":{"type":"prometheus","uid":"jq-prometheus"},"expr":"up","instant":true}]}')"
probe "anonymous datasource proxy" "$(curl -sk -o /dev/null -w '%{http_code}' --max-time 15 \
  "$GRAFANA/api/datasources/proxy/uid/jq-prometheus/api/v1/label/repo/values")"
probe "anonymous dashboard listing" "$(curl -sk -o /dev/null -w '%{http_code}' --max-time 15 \
  "$GRAFANA/api/search?type=dash-db")"

# 4. Prometheus and the collector must never be published themselves. Resolve
#    the containers by compose label - hardcoded names silently pass when the
#    project names them differently.
for svc in prometheus collector; do
  cid=$(container "$svc")
  if [[ -z "$cid" ]]; then
    bad "$svc container not found - cannot verify its port bindings"
    continue
  fi
  binding=$(docker inspect "$cid" --format '{{json .NetworkSettings.Ports}}')
  if grep -q '"HostIp":"0.0.0.0"' <<< "$binding"; then
    bad "$svc publishes a port on 0.0.0.0"
  elif grep -q 'HostPort' <<< "$binding"; then
    warn "$svc publishes a port on loopback - fine locally, remove it on a server"
  else
    ok "$svc publishes no port"
  fi
done

# 5. A live public link while private repos are being gathered is the loaded
#    gun. Read the setting from the running container, not from .env - the
#    server stack sets it in the compose file.
cid=$(container collector)
public_only=$(docker inspect "$cid" --format '{{range .Config.Env}}{{println .}}{{end}}' 2>/dev/null |
  sed -n 's/^JQ_PUBLIC_ONLY=//p' | tr '[:upper:]' '[:lower:]')
for uid in jq-fleet jq-fleet-public; do
  enabled=$(curl -sk --max-time 10 "$GRAFANA/api/dashboards/uid/$uid/public-dashboards" 2>/dev/null |
    python3 -c 'import json,sys
try: print(str(json.load(sys.stdin).get("isEnabled", False)).lower())
except Exception: print("unknown")')
  [[ "$enabled" == "true" && "$public_only" != "true" ]] &&
    bad "$uid has a LIVE public link while JQ_PUBLIC_ONLY is '${public_only:-unset}'"
done
if [[ "$public_only" == "true" ]]; then
  ok "collector is restricted to public repos"
else
  warn "JQ_PUBLIC_ONLY is '${public_only:-unset}' - fine for local use, but no public link may be live"
fi

echo
if [[ "$fails" -gt 0 ]]; then
  echo "NOT SAFE TO EXPOSE - $fails check(s) failed" >&2
  exit 1
fi
echo "Safe to expose: publish via Share -> Public dashboard, and share only that link."
