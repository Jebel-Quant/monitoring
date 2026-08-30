#!/usr/bin/env bash
# Bring both halves up: Prometheus and Grafana in Docker, the collector on this
# machine. Mints a GitHub token from the gh CLI if there isn't one.
#
# The collector is not containerised - see scripts/collector.sh for why. On
# macOS it is installed as a launchd agent so it comes back after a reboot;
# elsewhere this script says how to run it and leaves supervision to you.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$here"

if [[ ! -f .env ]]; then
  cp .env.example .env
  echo "created monitoring/.env from the example"
fi

if [[ ! -f repos.yml ]]; then
  cp repos.example.yml repos.yml
  echo "created monitoring/repos.yml from the example - EDIT IT, then run this again"
  echo "it lists the checkouts to monitor; the example points at paths you may not have"
  exit 1
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

command -v uv >/dev/null 2>&1 || {
  echo "uv is not installed - the collector needs it; see https://docs.astral.sh/uv/" >&2
  exit 1
}

# Fail on a broken repos.yml here, while there is still a human watching, rather
# than inside a launchd agent whose output nobody is reading. The collector
# recomputes this at every launch; this run is only a check.
uv run --quiet --with pyyaml python scripts/gen-repos.py >/dev/null

docker compose up -d

# -- the collector -----------------------------------------------------------
label="com.jebel-quant.jq-collector"
if [[ "$(uname -s)" == "Darwin" ]]; then
  plist="$HOME/Library/LaunchAgents/$label.plist"
  # A launchd agent inherits almost no PATH - /usr/bin:/bin:/usr/sbin:/sbin and
  # nothing else - so uv, which lives under /opt/homebrew or ~/.local, is simply
  # not found. Pin the directory it is actually in rather than guessing at
  # either location.
  uv_dir="$(dirname "$(command -v uv)")"
  logs="$here/.collector-logs"
  mkdir -p "$HOME/Library/LaunchAgents" "$logs"
  # Regenerated every time: the paths are absolute, so a moved checkout of this
  # repo would otherwise leave a plist pointing at where it used to be.
  cat > "$plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$label</string>
  <key>ProgramArguments</key>
  <array><string>$here/scripts/collector.sh</string></array>
  <key>WorkingDirectory</key><string>$here</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>$uv_dir:/usr/bin:/bin:/usr/sbin:/sbin</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <!-- KeepAlive on something that exits immediately is a spin. Ten seconds
       between attempts turns a misconfiguration into a slow retry with a
       readable log instead of thousands of lines a minute. -->
  <key>ThrottleInterval</key><integer>10</integer>
  <key>StandardOutPath</key><string>$logs/collector.log</string>
  <key>StandardErrorPath</key><string>$logs/collector.log</string>
</dict>
</plist>
PLIST
  # bootout first so an edited plist is actually re-read; it fails when nothing
  # is loaded, which is the normal first run.
  launchctl bootout "gui/$UID/$label" 2>/dev/null || true
  launchctl bootstrap "gui/$UID" "$plist"
  echo "collector: loaded as a launchd agent - logs in .collector-logs/collector.log"
else
  echo "collector: not started. Run it yourself, e.g."
  echo "    ./scripts/collector.sh            # foreground"
  echo "  or install it as a systemd --user service running that script."
fi

echo
echo "Grafana     http://localhost:3000/d/jq-fleet   (anonymous read-only; admin/admin to edit)"
echo "Prometheus  http://localhost:9090"
echo "Collector   http://localhost:9109/metrics"
