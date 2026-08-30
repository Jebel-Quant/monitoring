#!/usr/bin/env bash
# Run the collector on this machine, in the foreground.
#
# Not in a container, on purpose. It reads your working copies, and a container
# can only reach those through bind mounts - which cannot name a checkout that
# sits somewhere other than <root>/<owner>/<name>, and which are slow enough on
# macOS that the line counts needed a caching layer to stay affordable.
#
# Prometheus, which is still in Docker, scrapes this at host.docker.internal:9109.
#
# Run it under launchd (./scripts/up.sh sets that up on macOS), in a terminal,
# or under tmux - it is an ordinary foreground process either way, and Ctrl-C
# stops it.
#
# The trade this makes: inside the container the collector could only see the
# checkouts that were mounted into it, so an unlisted repo was not merely
# filtered out, it was invisible. Here it runs as you and could read anything
# you can. It still never writes - every git call is read-only and passes
# --no-optional-locks - but that is now a property of the code rather than
# something the sandbox enforces.
set -euo pipefail
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

die() { printf '\033[31merror\033[0m  %s\n' "$1" >&2; exit 1; }

[[ -f .env ]] || die ".env not found - run ./scripts/up.sh first"
[[ -f repos.yml ]] || die "repos.yml not found - run ./scripts/up.sh first"
# Says "on PATH", not "installed", because under launchd it is almost always
# the former: an agent inherits /usr/bin:/bin:/usr/sbin:/sbin and nothing else,
# so a perfectly good uv under /opt/homebrew or ~/.local is invisible. up.sh
# pins uv's directory into the plist for exactly this reason.
command -v uv >/dev/null 2>&1 || die "uv is not on PATH ($PATH) - see https://docs.astral.sh/uv/"

set -a; . ./.env; set +a

# repos.yml is the fleet and the layout, and it stays the only place either is
# written down: both lines are computed at every launch rather than generated
# into a file that can fall out of step with it.
#
# `export "$line"` and not `export $line` - a path may contain spaces, and
# unquoted word splitting would turn one repo into two broken ones.
while IFS= read -r line; do
  [[ -n "$line" ]] && export "$line"
done < <(uv run --quiet --with pyyaml python scripts/gen-repos.py)

# No container means no bind mounts and no /repos to fall back on. Every path
# is real and absolute, and JQ_REPO_PATHS carries all of them.
export JQ_REPO_ROOT=""

cd collector
exec uv run --quiet python -m jq_collector
