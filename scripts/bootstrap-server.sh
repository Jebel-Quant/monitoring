#!/usr/bin/env bash
# Bring the public board up on a fresh server, with TLS.
#
#   ./scripts/bootstrap-server.sh
#
# Expects a .env beside docker-compose.server.yml holding GITHUB_TOKEN,
# GF_ADMIN_PASSWORD, FLEET_DOMAIN and ACME_EMAIL. Refuses to start rather than
# come up half-configured, because a half-configured public board is worse than
# none - it can be reachable without TLS, or serving the wrong repos.
set -euo pipefail
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

die() { printf '\033[31merror\033[0m  %s\n' "$1" >&2; exit 1; }
ok()  { printf '\033[32mok\033[0m     %s\n' "$1"; }

command -v docker >/dev/null || die "docker not installed - curl -fsSL https://get.docker.com | sh"
docker compose version >/dev/null 2>&1 || die "docker compose v2 not available"
ok "docker $(docker --version | awk '{print $3}' | tr -d ,)"

[[ -f .env ]] || die ".env not found - see the README for the four required values"
set -a; . ./.env; set +a

for v in GITHUB_TOKEN GF_ADMIN_PASSWORD FLEET_DOMAIN ACME_EMAIL; do
  [[ -n "${!v:-}" ]] || die "$v is not set in .env"
done
[[ "${#GF_ADMIN_PASSWORD}" -ge 16 ]] || die "GF_ADMIN_PASSWORD is shorter than 16 characters - this host is on the internet"
ok "all four settings present"

# The ACME HTTP-01 challenge needs the name to resolve here and port 80 open,
# so check before Caddy burns a Let's Encrypt failure on it.
# getent is always present but only consults DNS where nsswitch says so (it
# does on Debian/Ubuntu, not on macOS), so fall back rather than trust one.
resolved=$(getent hosts "$FLEET_DOMAIN" 2>/dev/null | awk '{print $1}' | head -1 || true)
[[ -z "$resolved" ]] && resolved=$(dig +short A "$FLEET_DOMAIN" 2>/dev/null | tail -1 || true)
[[ -z "$resolved" ]] && resolved=$(python3 -c "import socket,sys
try: print(socket.gethostbyname(sys.argv[1]))
except Exception: pass" "$FLEET_DOMAIN" 2>/dev/null || true)
[[ -n "$resolved" ]] || die "$FLEET_DOMAIN does not resolve - add the A record first"
ok "$FLEET_DOMAIN resolves to $resolved"

public=$(curl -s --max-time 10 https://api.ipify.org || true)
if [[ -n "$public" && "$resolved" != "$public" ]]; then
  printf '\033[33mwarn\033[0m   %s points at %s but this host is %s - certificate issuance will fail\n' \
    "$FLEET_DOMAIN" "$resolved" "$public"
fi

# The GitHub token must be able to read; fail here rather than in a container log.
code=$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $GITHUB_TOKEN" https://api.github.com/user)
[[ "$code" == "200" ]] || die "GITHUB_TOKEN rejected by the GitHub API (HTTP $code)"
ok "GITHUB_TOKEN accepted by GitHub"

echo
docker compose -f docker-compose.server.yml -f docker-compose.tls.yml up -d --build
echo
echo "Waiting for Grafana..."
for _ in $(seq 1 60); do
  curl -sk "https://$FLEET_DOMAIN/api/health" >/dev/null 2>&1 && break
  sleep 5
done

echo
./scripts/check-public-safe.sh || die "preflight failed - do NOT share the link yet"
echo
echo "Now sign in at https://$FLEET_DOMAIN as admin, open 'Jebel-Quant Fleet (public)'"
echo "-> Share -> Public dashboard, and share only that link."
