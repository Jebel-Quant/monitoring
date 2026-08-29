#!/usr/bin/env bash
# Permanently delete a repo's history from Prometheus.
#
#   ./scripts/purge-repo.sh Jebel-Quant/rhiza-brainbug
#
# Use after archiving or deleting a repo, when you do not want its old rows
# lingering in long time windows. THIS IS IRREVERSIBLE - the samples are gone.
#
# Both label generations are purged: this board once used a bare repo name and
# now uses owner/name, so a repo that predates that change has series under both.
set -euo pipefail

cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ $# -eq 0 ]]; then
  echo "usage: $0 <owner/name> [more...]" >&2
  exit 64
fi

PROM=http://localhost:9090
selectors=()
for repo in "$@"; do
  # A bare name would match nothing dangerous, but an empty or wildcard
  # argument could match everything. Refuse anything that is not a literal.
  if [[ -z "$repo" || "$repo" == *'*'* || "$repo" == *'~'* ]]; then
    echo "refusing non-literal repo selector: '$repo'" >&2
    exit 64
  fi
  selectors+=("{repo=\"$repo\"}")
  [[ "$repo" == */* ]] && selectors+=("{repo=\"${repo##*/}\"}")
done

since() { date -u -v-3650d +%s 2>/dev/null || date -u -d '10 years ago' +%s; }

# Series metadata: what the index still lists.
count() {
  curl -sG "$PROM/api/v1/series" --data-urlencode "match[]=$1" \
    --data-urlencode "start=$(since)" --data-urlencode "end=$(date -u +%s)" |
    python3 -c 'import json,sys; print(len(json.load(sys.stdin)["data"]))'
}

# Datapoints an actual query returns. This is the number that matters: panels
# run queries, and a freshly purged series can still be listed by the index
# until the head block is compacted, while returning no data at all.
# A daily step keeps a decade-wide window under Prometheus's 11,000-point cap
# while still detecting any surviving sample.
points() {
  curl -sG "$PROM/api/v1/query_range" --data-urlencode "query=$1" \
    --data-urlencode "start=$(since)" --data-urlencode "end=$(date -u +%s)" \
    --data-urlencode "step=86400" |
    python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except ValueError:
    print("?"); raise SystemExit
if d.get("status") != "success":
    print("?"); raise SystemExit
print(sum(len(s["values"]) for s in d["data"]["result"]))'
}

echo "About to delete:"
total=0
for sel in "${selectors[@]}"; do
  n=$(count "$sel")
  total=$((total + n))
  printf '  %-46s %s series\n' "$sel" "$n"
done
if [[ "$total" -eq 0 ]]; then
  echo "nothing to do"
  exit 0
fi

echo "enabling the admin API..."
docker compose -f docker-compose.yml -f docker-compose.admin.yml up -d prometheus >/dev/null
until [[ "$(curl -s -o /dev/null -w '%{http_code}' "$PROM/-/ready")" == "200" ]]; do sleep 1; done

for sel in "${selectors[@]}"; do
  curl -s -X POST -G "$PROM/api/v1/admin/tsdb/delete_series" --data-urlencode "match[]=$sel" \
    -o /dev/null -w "  delete $sel -> HTTP %{http_code}\n"
done

# delete_series only tombstones; this reclaims the blocks on disk.
curl -s -X POST "$PROM/api/v1/admin/tsdb/clean_tombstones" -o /dev/null -w "  clean_tombstones -> HTTP %{http_code}\n"

echo "disabling the admin API again..."
docker compose up -d prometheus >/dev/null
until [[ "$(curl -s -o /dev/null -w '%{http_code}' "$PROM/-/ready")" == "200" ]]; do sleep 1; done

echo "remaining:"
stale=0
for sel in "${selectors[@]}"; do
  pts=$(points "$sel")
  idx=$(count "$sel")
  printf '  %-46s %s datapoints (index still lists %s)\n' "$sel" "$pts" "$idx"
  [[ "$pts" != "0" ]] && stale=1
  [[ "$idx" != "0" && "$pts" == "0" ]] && stale_index=1
done
if [[ "$stale" == "1" ]]; then
  echo "WARNING: data survived the purge" >&2
  exit 1
fi
if [[ "${stale_index:-0}" == "1" ]]; then
  echo "Purged. The index still lists the series until the next head-block"
  echo "compaction (~2h); queries already return nothing, so no panel shows it."
else
  echo "Purged."
fi
