"""What a clone's ``origin`` remote says it is.

There used to be two near-identical copies of this parser - one in ``repos.py``
to key the fleet, one in ``localgit.py`` to confirm a mount point is the repo it
claims to be - and both threw the host away and kept only the last two path
segments. That was fine while every repo was on GitHub, where ``owner/name`` is
the whole of an identity. It is wrong twice over now:

- the host is what says which forge a checkout belongs to, and inferring it is
  free here where the URL is already in hand;
- GitLab namespaces nest. ``gitlab.com/acme/platform/infra/web`` is the project
  ``web`` in the namespace ``acme/platform/infra``, and keeping the last two
  segments gives ``infra/web`` - a repo that does not exist, and an API path
  that 404s.

So the namespace is the whole path when there is a host to hang it off. A path
remote - a clone of a clone, ``/srv/mirrors/o/r`` - has no host and no
namespace, only a filesystem trail, so there the last two segments remain the
best available guess.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass

log = logging.getLogger(__name__)

_TIMEOUT = 20

GITHUB = "github"
GITLAB = "gitlab"


@dataclass(frozen=True)
class Origin:
    """A parsed ``origin`` URL. ``host`` is empty for a path remote."""

    host: str
    namespace: str
    name: str

    @property
    def full_name(self) -> str:
        return f"{self.namespace}/{self.name}"

    @property
    def forge(self) -> str:
        return forge_for_host(self.host)


def forge_for_host(host: str) -> str:
    """Which forge a host is.

    GitHub is the default for anything unrecognised, including an empty host.
    That keeps a self-hosted GitHub Enterprise and every path remote behaving
    exactly as they did before this module existed; a fleet on some other forge
    says so with an explicit ``forge:`` in repos.yml rather than relying on a
    guess from a hostname.
    """
    host = host.lower()
    if host == "gitlab.com" or host.startswith("gitlab."):
        return GITLAB
    return GITHUB


def parse(url: str) -> Origin | None:
    """Split a git remote URL, or None if it cannot name a repo.

    Handles the four shapes a remote is written in: ``scheme://host/path``,
    the scp-like ``[user@]host:path``, and a bare absolute or relative path.
    """
    url = url.strip().removesuffix(".git")
    if not url:
        return None

    if "://" in url:
        authority, _, path = url.split("://", 1)[1].partition("/")
    elif ":" in url and "/" not in url.partition(":")[0]:
        # scp-like. The `/` test is what keeps a Windows-style or
        # colon-bearing path from being mistaken for host:path.
        authority, _, path = url.partition(":")
    else:
        authority, path = "", url

    # Strip any `user@` and any `:port` to leave the bare hostname.
    host = authority.rpartition("@")[2].partition(":")[0].lower()

    parts = [part for part in path.split("/") if part]
    if len(parts) < 2:
        # One segment cannot be split into a namespace and a name.
        return None
    if not host:
        # A filesystem trail is not a namespace; only its tail is meaningful.
        parts = parts[-2:]
    return Origin(host=host, namespace="/".join(parts[:-1]), name=parts[-1])


def read(path: str) -> Origin | None:
    """The parsed ``origin`` of the checkout at *path*.

    Read-only and lock-free, like every other git call the collector makes: the
    clones are somebody's live working copies and a monitor has no business
    taking a lock in one.
    """
    try:
        result = subprocess.run(
            ["git", "--no-optional-locks", "-C", path, "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            timeout=_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warning("reading origin of %s failed: %s", path, exc)
        return None
    if result.returncode != 0:
        return None
    return parse(result.stdout)
