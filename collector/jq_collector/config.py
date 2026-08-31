"""Runtime configuration, read once from the environment."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

from .origin import GITHUB
from .repos import KNOWN_FORGES, FleetError, load

log = logging.getLogger(__name__)


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return int(raw)


def _csv(name: str, default: tuple[str, ...] = ()) -> tuple[str, ...]:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def _pairs(name: str) -> dict[str, str]:
    """``owner/name=path`` pairs, comma separated, into a mapping.

    Raises rather than skipping a malformed entry. A dropped pair would take
    one repo's working-copy panels off the board and say nothing about why,
    which is the failure mode this whole module is shaped to avoid; refusing to
    start is louder and cheaper to diagnose. A path containing a comma cannot
    be expressed here at all, which is one more reason repos.yml is the place
    the layout is normally written down.
    """
    mapping: dict[str, str] = {}
    for item in _csv(name):
        key, separator, value = item.partition("=")
        key, value = key.strip(), value.strip()
        if not separator or not key or not value:
            raise ValueError(f"{name}: expected owner/name=path, got {item!r}")
        mapping[key] = os.path.expanduser(value)
    return mapping


def _forges(name: str) -> dict[str, str]:
    """``owner/name=forge`` pairs, comma separated, into a mapping.

    Raises on an unknown forge for the same reason `repos._declared_forge`
    does: reading a name nobody implements as GitHub would put the repo on the
    board with every remote panel wrong and nothing saying why.
    """
    mapping: dict[str, str] = {}
    for item in _csv(name):
        key, separator, value = item.partition("=")
        key, value = key.strip(), value.strip().lower()
        if not separator or not key or not value:
            raise ValueError(f"{name}: expected owner/name=forge, got {item!r}")
        if value not in KNOWN_FORGES:
            raise ValueError(f"{name}: forge {value!r} is not one of {', '.join(KNOWN_FORGES)}")
        mapping[key] = value
    return mapping


@dataclass(frozen=True)
class Config:
    """Everything the collector needs to know about its environment.

    The fleet is an explicit list, and nothing else is ever gathered. There
    used to be a whole-org sweep as well, which meant the board's contents were
    decided by GitHub rather than by you - a new repo in the org appeared
    unasked, and a shared org like cvxgrp dragged in a hundred repos that were
    not yours.

    ``repos.yml`` is where that list lives, and it is read here directly: both
    the fleet and each checkout's path come out of the one file, at startup,
    with nothing generated in between to go stale. ``JQ_REPOS`` and
    ``JQ_REPO_PATHS`` remain for a deployment that has no file to mount - a
    server, or CI - and ``JQ_REPO_PATHS`` still wins over the file, so one
    awkward path can be corrected without editing it.

    Both halves read the same list, so the GitHub panels and the working-copy
    panels can never disagree about who is in the fleet.
    """

    repos: tuple[str, ...] = field(default_factory=lambda: _csv("JQ_REPOS"))

    token: str = os.environ.get("GITHUB_TOKEN", "")
    api: str = os.environ.get("GITHUB_API", "https://api.github.com")

    # The second forge. Kept as its own pair rather than a per-forge mapping
    # because gitlab.com is the only GitLab this supports: a fleet spanning a
    # self-hosted instance as well would need the base URL per entry, and there
    # is no point paying for that generality until somebody has one.
    gitlab_token: str = os.environ.get("GITLAB_TOKEN", "")
    gitlab_api: str = os.environ.get("GITLAB_API", "https://gitlab.com/api/v4")

    # The fleet file. Read when it is there; when it is not, JQ_REPOS and
    # JQ_REPO_PATHS are the whole story. /config/repos.yml is where the image
    # expects it to be mounted.
    repos_file: str = os.environ.get("JQ_REPOS_FILE", "/config/repos.yml")

    # Where the host's home directory is mounted, and therefore what `~` in
    # repos.yml means. Set to /host in the image; empty when the collector runs
    # natively, where `~` is just `~`. One mount for the whole fleet is what
    # lets repos.yml name a checkout at any path at all - the per-repo bind
    # mounts it replaced could only express <root>/<owner>/<name>.
    host_root: str = os.environ.get("JQ_HOST_ROOT", "")

    # Last-resort fallback for a repo no path is known for: <repo_root>/<owner>/
    # <name>. Off by default, because repos.yml states every path outright.
    # Setting it is for a deployment that checks its fleet out in that shape.
    repo_root: str = os.environ.get("JQ_REPO_ROOT", "")

    # Explicit path per repo, as owner/name=path. Filled from repos.yml, and
    # overridable from the environment for a deployment that has no file.
    repo_paths: dict[str, str] = field(default_factory=lambda: _pairs("JQ_REPO_PATHS"))

    # Which forge each repo is read through, as owner/name=forge. Filled from
    # repos.yml; anything unlisted is GitHub, which is what makes a fleet that
    # predates GitLab support keep working untouched.
    forges: dict[str, str] = field(default_factory=lambda: _forges("JQ_REPO_FORGES"))

    # owner/name of the repo whose releases define "up to date".
    template_repo: str = os.environ.get("JQ_TEMPLATE_REPO", "Jebel-Quant/rhiza")
    template_pointer: str = os.environ.get("JQ_TEMPLATE_POINTER", ".rhiza/template.yml")

    listen_port: int = _int("JQ_LISTEN_PORT", 9109)

    # GitHub is rate limited and slow; the local filesystem is neither.
    github_interval: int = _int("JQ_GITHUB_INTERVAL", 300)
    local_interval: int = _int("JQ_LOCAL_INTERVAL", 60)

    # Line counts and commit counts are re-measured only when the clone has
    # actually moved (see localgit._fingerprint), because counting lines means
    # reading every tracked file and the clones are bind mounts - cheap on a
    # native filesystem, not cheap through Docker Desktop. This is the ceiling
    # on how long a cached measurement may stand anyway: the 30-day commit
    # window slides whether or not anyone commits, so a repo that has gone
    # quiet still needs its count to decay. A day is well inside the resolution
    # of a 30-day figure.
    measure_max_age: int = _int("JQ_MEASURE_MAX_AGE", 86400)

    http_timeout: int = _int("JQ_HTTP_TIMEOUT", 20)

    # Open PRs are enumerated per repo; each one costs a further call for its
    # check runs. Cap it so a runaway repo cannot exhaust the hourly budget.
    max_prs_per_repo: int = _int("JQ_MAX_PRS_PER_REPO", 20)

    # Merged pull requests kept per repo. The board shows the newest N across
    # the whole fleet, so this must be at least that N or a burst in one repo
    # could crowd out entries the fleet-wide list should have shown.
    recent_merges_per_repo: int = _int("JQ_RECENT_MERGES_PER_REPO", 10)

    # Repos to leave out, as bare names or as owner/name. Redundant now that
    # the fleet is an explicit list - deleting the line from repos.yml is the
    # obvious move - but it stays for the server, where the list is an env var
    # and commenting one entry out is not possible.
    ignore: tuple[str, ...] = field(default_factory=lambda: _csv("JQ_IGNORE"))

    include_archived: bool = os.environ.get("JQ_INCLUDE_ARCHIVED", "false").lower() == "true"

    # Drop private repos entirely - not just their details, but their existence.
    # The board binds to loopback, so this is not what keeps a private repo off
    # the network; it is for when you would rather it were never fetched, since
    # its name, workflow names, PR titles and branch names are all disclosure.
    public_only: bool = os.environ.get("JQ_PUBLIC_ONLY", "false").lower() == "true"

    def __post_init__(self) -> None:
        """Fold repos.yml in, when there is one.

        Refuses to start on a file it cannot act on. The alternative - carrying
        on with a short fleet - takes repos off the board and says nothing about
        why, and a board that is quietly incomplete is worse than one that did
        not come up.
        """
        if not self.repos_file or not os.path.isfile(self.repos_file):
            return
        try:
            fleet, paths, forges = load(self.repos_file, self.host_root)
        except FleetError as exc:
            raise SystemExit(f"repos.yml: {exc}") from exc
        object.__setattr__(self, "repos", fleet)
        # The environment wins: it is the narrower, more deliberate statement,
        # and it is how one path can be corrected without touching the file.
        object.__setattr__(self, "repo_paths", {**paths, **self.repo_paths})
        object.__setattr__(self, "forges", {**forges, **self.forges})
        log.info("fleet: %d repos, %d with a checkout", len(fleet), len(self.repo_paths))
        # A second line rather than a clause on the first, and only when there
        # is a split worth reporting: a GitHub-only fleet logs exactly what it
        # logged before GitLab support existed, which is what CI asserts on and
        # what anybody reading these logs already recognises.
        by_forge = self.repos_by_forge()
        if len(by_forge) > 1:
            log.info(
                "forges: %s",
                ", ".join(f"{len(names)} on {forge}" for forge, names in sorted(by_forge.items())),
            )

    def is_ignored(self, owner: str, name: str) -> bool:
        return name in self.ignore or f"{owner}/{name}" in self.ignore

    def forge_for(self, full_name: str) -> str:
        """Which forge a repo is read through. Unlisted means GitHub."""
        return self.forges.get(full_name, GITHUB)

    def repos_by_forge(self) -> dict[str, tuple[str, ...]]:
        """The fleet grouped by forge, so each API is asked once for its share.

        Only forges that actually have repos appear, which is what keeps a
        GitHub-only fleet from ever building a GitLab client or warning about a
        token it has no use for.
        """
        grouped: dict[str, list[str]] = {}
        for full_name in self.repos:
            grouped.setdefault(self.forge_for(full_name), []).append(full_name)
        return {forge: tuple(names) for forge, names in grouped.items()}
