#!/usr/bin/env bash
# Stop the stack. Add --volumes to also throw away the Prometheus history.
set -euo pipefail
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# The generated override may be missing on a fresh clone; the project name in
# docker-compose.yml is enough to find the running containers either way.
files=(-f docker-compose.yml)
[[ -f docker-compose.repos.yml ]] && files+=(-f docker-compose.repos.yml)
docker compose "${files[@]}" down "$@"
