#!/usr/bin/env bash
# Restart the public board after editing .env or pulling new code.
#
#   ./scripts/restart.sh
#
# The everyday counterpart to bootstrap-server.sh, which is for a host that has
# never run the stack: this one assumes DNS, TLS and the token are already good
# and only re-applies what changed. It still runs the preflight at the end,
# because the thing most likely to have changed is the fleet, and a fleet is
# exactly what can turn a safe board unsafe.
#
# Both compose files, always. Naming only the server file would leave Caddy
# unmanaged, and a later `down` would strand it holding 80 and 443.
set -euo pipefail
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

die() { printf '\033[31merror\033[0m  %s\n' "$1" >&2; exit 1; }

[[ -f .env ]] || die ".env not found - run ./scripts/bootstrap-server.sh first"
set -a; . ./.env; set +a
[[ -n "${FLEET_DOMAIN:-}" ]] || die "FLEET_DOMAIN is not set in .env"

# --build because a pull may have changed the collector; compose recreates only
# the containers whose image or environment actually differs, so an unchanged
# Grafana keeps serving while the collector comes back.
docker compose -f docker-compose.server.yml -f docker-compose.tls.yml up -d --build

echo
echo "Waiting for Grafana..."
for _ in $(seq 1 60); do
  curl -sk "https://$FLEET_DOMAIN/api/health" >/dev/null 2>&1 && break
  sleep 5
done

# The collector serves :9109 only once its first GitHub refresh has finished,
# and the preflight reads that endpoint to see which repos are exported. Run it
# too early and it reports an empty fleet as a failure.
echo "Waiting for the first GitHub refresh..."
for _ in $(seq 1 60); do
  docker compose -f docker-compose.server.yml -f docker-compose.tls.yml exec -T collector \
    python -c "import urllib.request; urllib.request.urlopen('http://localhost:9109/metrics')" \
    >/dev/null 2>&1 && break
  sleep 5
done

echo
./scripts/check-public-safe.sh || die "preflight failed - do NOT share the link yet"
