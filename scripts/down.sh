#!/usr/bin/env bash
# Stop the stack. Add --volumes to also throw away the Prometheus history.
set -euo pipefail
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
docker compose down "$@"
