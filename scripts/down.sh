#!/usr/bin/env bash
# Stop both halves. Add --volumes to also throw away the Prometheus history.
set -euo pipefail
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

label="com.jebel-quant.jq-collector"
if [[ "$(uname -s)" == "Darwin" ]]; then
  # KeepAlive means the agent restarts itself if merely killed, so unload it.
  # Fails when it was never loaded, which is fine.
  launchctl bootout "gui/$UID/$label" 2>/dev/null && echo "collector: launchd agent unloaded" || true
else
  pkill -f "python -m jq_collector" 2>/dev/null && echo "collector: stopped" || true
fi

docker compose down "$@"
