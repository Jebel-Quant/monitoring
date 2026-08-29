#!/usr/bin/env bash
# Bring the stack up, minting a GitHub token from the gh CLI if there isn't one.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$here"

if [[ ! -f .env ]]; then
  cp .env.example .env
  echo "created monitoring/.env from the example"
fi

# Only fill the token if it is still blank - never clobber one you set by hand.
if ! grep -qE '^GITHUB_TOKEN=.+' .env; then
  if ! command -v gh >/dev/null 2>&1; then
    echo "no GITHUB_TOKEN in .env and no gh CLI to mint one; set it by hand" >&2
    exit 1
  fi
  token="$(gh auth token)"
  # Portable in-place edit: BSD sed and GNU sed disagree about -i.
  tmp="$(mktemp)"
  sed "s|^GITHUB_TOKEN=.*|GITHUB_TOKEN=${token}|" .env > "$tmp" && mv "$tmp" .env
  chmod 600 .env
  echo "wrote a token from 'gh auth token' into monitoring/.env"
fi

docker compose up -d --build

echo
echo "Grafana     http://localhost:3000/d/jq-fleet   (anonymous read-only; admin/admin to edit)"
echo "Prometheus  http://localhost:9090"
echo "Collector   http://localhost:9109/metrics"
