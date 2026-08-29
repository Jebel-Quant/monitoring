#!/usr/bin/env bash
# Preflight before serving the board world-readable.
#
# Checks the two things that actually leak: the DATA (does any private repo
# appear?) and the ACCESS MODE (can a visitor ask for something other than this
# dashboard?). Run it against the stack exactly as it will be exposed.
set -uo pipefail
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

GRAFANA=http://localhost:3000
COLLECTOR=http://localhost:9109
fails=0
ok()   { printf '  \033[32mPASS\033[0m  %s\n' "$1"; }
bad()  { printf '  \033[31mFAIL\033[0m  %s\n' "$1"; fails=$((fails + 1)); }
warn() { printf '  \033[33mWARN\033[0m  %s\n' "$1"; }

metrics=$(curl -s --max-time 10 "$COLLECTOR/metrics")
repos=$(echo "$metrics" | grep '^jq_repo_info' | sed 's/.*repo="\([^"]*\)".*/\1/' | sort -u)

# 1. Every repo the collector exports must actually be public on GitHub. This is
#    checked against GitHub rather than against our own visibility label, so a
#    stale or mislabelled snapshot cannot pass.
private=0
while IFS= read -r repo; do
  [[ -z "$repo" ]] && continue
  vis=$(gh api "repos/$repo" --jq '.visibility' 2>/dev/null)
  if [[ "$vis" != "public" ]]; then
    bad "exported repo is not public on GitHub: $repo ($vis)"
    private=$((private + 1))
  fi
done <<< "$repos"
[[ "$private" -eq 0 ]] && ok "all $(wc -l <<< "$repos" | tr -d ' ') exported repos are public on GitHub"

# 2. No unauthenticated arbitrary query.
code=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$GRAFANA/api/ds/query" \
  -H 'Content-Type: application/json' \
  -d '{"queries":[{"refId":"A","datasource":{"type":"prometheus","uid":"jq-prometheus"},"expr":"up","instant":true}]}')
if [[ "$code" == "401" || "$code" == "403" ]]; then
  ok "anonymous /api/ds/query refused (HTTP $code)"
else
  bad "anonymous /api/ds/query returned HTTP $code - a visitor can run any PromQL"
fi

# 3. No unauthenticated datasource proxy. This is the one that exposes the raw
#    label index, where purged private repo names linger until head compaction.
code=$(curl -s -o /dev/null -w '%{http_code}' \
  "$GRAFANA/api/datasources/proxy/uid/jq-prometheus/api/v1/label/repo/values")
if [[ "$code" == "401" || "$code" == "403" ]]; then
  ok "anonymous datasource proxy refused (HTTP $code)"
else
  bad "anonymous datasource proxy returned HTTP $code - the label index is readable"
fi

# 4. Prometheus and the collector must never be published themselves.
for name in jq-prometheus jq-collector; do
  binding=$(docker inspect "$name" --format '{{json .NetworkSettings.Ports}}' 2>/dev/null)
  if grep -q '"0.0.0.0"' <<< "$binding"; then
    bad "$name publishes a port on 0.0.0.0 - bind it to 127.0.0.1"
  else
    ok "$name is not published beyond loopback"
  fi
done

# 5. The loaded gun: a live public link while the collector is gathering private
#    repos. Each is fine alone; together the public link serves private data the
#    moment anyone reaches this Grafana.
public_only=$(grep -E '^JQ_PUBLIC_ONLY=' .env 2>/dev/null | cut -d= -f2 | tr '[:upper:]' '[:lower:]')
for uid in jq-fleet jq-fleet-public; do
  enabled=$(curl -s "http://admin:admin@localhost:3000/api/dashboards/uid/$uid/public-dashboards" 2>/dev/null |
    python3 -c 'import json,sys
try: print(str(json.load(sys.stdin).get("isEnabled", False)).lower())
except Exception: print("false")')
  if [[ "$enabled" == "true" && "$public_only" != "true" ]]; then
    bad "$uid has a LIVE public link while JQ_PUBLIC_ONLY is '$public_only' - it would serve private repos"
  elif [[ "$enabled" == "true" ]]; then
    ok "$uid public link is live, and JQ_PUBLIC_ONLY=true"
  fi
done
[[ "$public_only" == "true" ]] && ok "collector is restricted to public repos" \
  || warn "JQ_PUBLIC_ONLY is '$public_only' - fine for local use, but no public link may be live"

# 6. Informational: purged names can still sit in the index for a couple of
#    hours. Harmless behind a public dashboard, fatal behind an open Grafana.
idx=$(curl -s "http://localhost:9090/api/v1/label/repo/values" 2>/dev/null |
  python3 -c 'import json,sys;print(" ".join(json.load(sys.stdin)["data"]))' 2>/dev/null)
stale=0
while IFS= read -r repo; do
  [[ -z "$repo" ]] && continue
  grep -qw -- "$repo" <<< "$idx" || true
done <<< "$repos"
for name in $idx; do
  grep -qx -- "$name" <<< "$repos" || stale=$((stale + 1))
done
[[ "$stale" -gt 0 ]] && warn "$stale name(s) linger in Prometheus's label index (purged data, clears at head compaction). Safe behind a public dashboard; NOT safe behind an open Grafana."

echo
if [[ "$fails" -gt 0 ]]; then
  echo "NOT SAFE TO EXPOSE - $fails check(s) failed" >&2
  exit 1
fi
echo "Safe to expose: publish via Share -> Public dashboard, and share only that link."
