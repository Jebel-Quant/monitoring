"""Read the state of the local working copies.

Every git invocation goes through ``--no-optional-locks`` and the clones are
mounted read-only: the collector must never be the reason a repo grows an
``index.lock`` or a refreshed index while a session is mid-rebase.

Nothing here fetches. ``ahead``/``behind`` are therefore only as fresh as the
last fetch *you* ran, which is why ``fetch_age`` is reported alongside them; for
a fetch-independent answer the exporter compares the local default-branch sha
with the one GitHub reports.

Nothing here measures twice, either. The line counts read every tracked file
and the commit counts spawn three git processes, which is cheap on a native
filesystem and much less so through a Docker bind mount. So both are taken only
when the clone has actually moved - see ``_fingerprint`` - and carried over from
the previous scan otherwise. The template pointer is deliberately outside that:
drift is the one thing that changes while the clone stands still, because it is
the upstream that moved.

Nothing here searches for checkouts either. A repo is read at the path
``repos.py`` resolved for it out of ``repos.yml``, and those paths are whatever
they are on disk: ``repos.yml`` may well say ``~/repos/tschm/rhiza_projects/cs``
for ``tschm/cs``, and no amount of joining owner to name will produce that. The
``<repo_root>/<owner>/<name>`` fallback is off by default and survives only for
a deployment laid out that way. Either way the fleet is decided by
``repos.yml``, not by whatever happened to be lying around under a scanned
directory.
"""

from __future__ import annotations

import logging
import os
import subprocess
import time

from .config import Config
from .state import LocalRepo

log = logging.getLogger(__name__)

_TIMEOUT = 20


def _git(path: str, *args: str) -> str | None:
    """Run a read-only git command, returning None if it fails."""
    try:
        result = subprocess.run(
            ["git", "--no-optional-locks", "-C", path, *args],
            capture_output=True,
            text=True,
            timeout=_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warning("git %s in %s failed: %s", " ".join(args), path, exc)
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def origin_owner_name(path: str) -> tuple[str, str] | None:
    """The ``(owner, name)`` this clone points at, or None if there is no origin."""
    url = _git(path, "remote", "get-url", "origin")
    if not url:
        return None
    url = url.removesuffix(".git")
    if url.startswith("git@"):
        _, _, tail = url.partition(":")
    elif "://" in url:
        tail = url.split("://", 1)[1].split("/", 1)[-1]
    else:
        tail = url
    parts = [p for p in tail.split("/") if p]
    if len(parts) < 2:
        return None
    return parts[-2], parts[-1]


def _template_ref(path: str, pointer: str) -> str:
    """The ref pinned in the repo's template pointer, if it is managed."""
    full = os.path.join(path, pointer)
    if not os.path.isfile(full):
        return ""
    try:
        import yaml

        with open(full, encoding="utf-8") as handle:
            data = yaml.safe_load(handle) or {}
    except Exception as exc:  # noqa: BLE001 - a malformed pointer is data, not a crash
        log.warning("could not parse %s: %s", full, exc)
        return ""
    ref = data.get("ref") if isinstance(data, dict) else None
    return str(ref).strip() if ref else ""


def _git_dir(path: str) -> str:
    """The clone's git directory, absolute. Empty when git will not say."""
    git_dir = _git(path, "rev-parse", "--git-dir")
    if not git_dir:
        return ""
    return git_dir if os.path.isabs(git_dir) else os.path.join(path, git_dir)


def _fetch_age(git_dir: str) -> float | None:
    """Seconds since the last fetch, from FETCH_HEAD's mtime."""
    if not git_dir:
        return None
    try:
        return max(0.0, time.time() - os.path.getmtime(os.path.join(git_dir, "FETCH_HEAD")))
    except OSError:
        return None


def _tags_mtime(git_dir: str) -> float:
    """Newest mtime across the two places a tag can live.

    Tagging the current commit moves neither HEAD nor the working copy, so
    without this the release counter would keep its old value until the max-age
    refresh - on precisely the day someone cut the release and would look.
    """
    if not git_dir:
        return 0.0
    newest = 0.0
    for candidate in ("packed-refs", os.path.join("refs", "tags")):
        try:
            newest = max(newest, os.path.getmtime(os.path.join(git_dir, candidate)))
        except OSError:
            continue
    return newest


def _fingerprint(head_sha: str, branch_sha: str, dirty: int, untracked: int, tags: float) -> str:
    """What "this clone has moved" means for the cached measurements.

    Every input is something the scan already knows or can stat, so deciding to
    skip a measurement costs no git process of its own. The measurements it
    guards each depend on one of them: the line counts on the working copy
    (head, dirty, untracked), the commit counts on the default branch, the
    release counter on the tags.
    """
    # Sub-second precision on the mtime is not fussiness: cutting a release and
    # the scan that follows it land in the same second often enough, and a
    # whole-second fingerprint would call that "nothing moved" and go on
    # reporting the pre-release count.
    return f"{head_sha}:{branch_sha}:{dirty}:{untracked}:{tags:.6f}"


# Extensions that count as source. The fleet is Python with a little shell and
# config, but the template supports Rust and Go too. Everything else a repo
# tracks - the markdown, the lockfiles, the CSV fixtures, the images - is real
# work and still not what "lines of code" is asking about.
_SOURCE_EXTENSIONS = frozenset(
    ["py", "pyi", "pyx"]
    + ["rs", "go", "c", "h", "cpp", "hpp", "java", "kt", "swift"]
    + ["js", "jsx", "ts", "tsx"]
    + ["rb", "jl", "r", "sql", "sh", "bash", "zsh"]
)

# Both conventions are in the fleet: a `tests/` tree, and `test_*.py` sitting
# next to the module it covers.
_TEST_DIRS = frozenset({"test", "tests", "testing"})


def _is_test(rel: str) -> bool:
    *parents, base = rel.split("/")
    if any(part in _TEST_DIRS for part in parents):
        return True
    return base.startswith("test_") or "_test." in base or ".test." in base


def _line_counts(path: str) -> tuple[int, int]:
    """Lines of source in the working copy, as ``(code, tests)``.

    Read off the files on disk rather than out of a commit, so uncommitted work
    counts - this is the same tree every other local panel describes. Only
    tracked files are considered, which is what keeps a stray virtualenv or a
    build directory from dwarfing the repo it sits in.
    """
    listing = _git(path, "ls-files", "-z")
    if listing is None:
        return 0, 0

    code = tests = 0
    for rel in listing.split("\0"):
        if not rel or rel.rpartition(".")[2].lower() not in _SOURCE_EXTENSIONS:
            continue
        try:
            with open(os.path.join(path, rel), "rb") as handle:
                lines = handle.read().count(b"\n")
        except OSError:
            # A symlink into nowhere, or a file deleted but not yet staged.
            # Tracked-but-absent is normal in a working copy mid-edit.
            continue
        if _is_test(rel):
            tests += lines
        else:
            code += lines
    return code, tests


def _commit_counts(path: str, ref: str) -> tuple[int, int | None, str]:
    """``(commits in the last 30 days, commits since the newest tag, that tag)``.

    Both counts are taken on ``ref`` - the default branch - rather than on
    HEAD, so a clone parked on a feature branch still reports what the repo has
    been doing instead of what that branch has.

    The tag comes from the clone, so a release published since the last fetch
    is not here yet. Read the release counter next to the fetch age, the same
    way ahead/behind are read.
    """
    recent = _git(path, "rev-list", "--count", "--since=30.days", ref)
    tag = _git(path, "describe", "--tags", "--abbrev=0", ref) or ""
    since = _git(path, "rev-list", "--count", f"{tag}..{ref}") if tag else None
    return (
        int(recent) if recent and recent.isdigit() else 0,
        int(since) if since and since.isdigit() else None,
        tag,
    )


def _ahead_behind(path: str) -> tuple[int | None, int | None]:
    counts = _git(path, "rev-list", "--left-right", "--count", "@{upstream}...HEAD")
    if not counts:
        return None, None
    parts = counts.split()
    if len(parts) != 2:
        return None, None
    try:
        return int(parts[1]), int(parts[0])  # ahead, behind
    except ValueError:
        return None, None


def scan_repo(
    name: str,
    owner: str,
    path: str,
    cfg: Config,
    default_branch: str = "main",
    previous: LocalRepo | None = None,
) -> LocalRepo:
    """Read one working copy.

    ``previous`` is this repo's row from the last scan. When the clone has not
    moved since then the measurements are carried over rather than re-taken:
    counting lines means reading every tracked file, and the clones are bind
    mounts. Everything else here is re-read every pass, the template pointer
    included - drift is the one thing that can change without the clone moving
    at all, because it is the *upstream* that moved.
    """
    status = _git(path, "status", "--porcelain=v1") or ""
    lines = [line for line in status.splitlines() if line.strip()]
    untracked = sum(1 for line in lines if line.startswith("??"))
    dirty = len(lines) - untracked
    ahead, behind = _ahead_behind(path)

    # One call for both: the commit time and the sha the fingerprint needs.
    head = (_git(path, "log", "-1", "--format=%ct %H") or "").split()
    last_commit = head[0] if head else ""
    head_sha = head[1] if len(head) > 1 else ""
    stashes = _git(path, "stash", "list") or ""

    branch = _git(path, "rev-parse", "--abbrev-ref", "HEAD") or ""
    # A detached HEAD reports "HEAD"; name the commit instead so the dashboard
    # shows something actionable.
    if branch == "HEAD":
        branch = f"detached@{head_sha[:7]}" if head_sha else "detached@unknown"

    git_dir = _git_dir(path)
    branch_sha = _git(path, "rev-parse", "--verify", "--quiet", default_branch) or ""
    fingerprint = _fingerprint(head_sha, branch_sha, dirty, untracked, _tags_mtime(git_dir))

    measured = _measurements(path, branch_sha or "HEAD", cfg, fingerprint, previous)

    return LocalRepo(
        name=name,
        owner=owner,
        path=path,
        branch=branch,
        dirty_files=dirty,
        untracked_files=untracked,
        ahead=ahead,
        behind=behind,
        stashes=len([s for s in stashes.splitlines() if s.strip()]),
        last_commit_at=float(last_commit) if last_commit.isdigit() else 0.0,
        fetch_age=_fetch_age(git_dir),
        head_sha=head_sha,
        default_branch_sha=branch_sha,
        rhiza_ref=_template_ref(path, cfg.template_pointer),
        **measured,
    )


def _measurements(
    path: str,
    ref: str,
    cfg: Config,
    fingerprint: str,
    previous: LocalRepo | None,
) -> dict[str, object]:
    """The measured fields, taken fresh or carried over from ``previous``.

    Carried over when the clone has not moved *and* the old reading is younger
    than ``measure_max_age``. The age cap is not belt-and-braces: the 30-day
    window slides on its own, so a repo that has gone quiet must still watch
    its old commits fall out of the count.
    """
    if (
        previous is not None
        and previous.fingerprint == fingerprint
        and time.time() - previous.measured_at < cfg.measure_max_age
    ):
        return {
            "code_lines": previous.code_lines,
            "test_lines": previous.test_lines,
            "commits_30d": previous.commits_30d,
            "commits_since_release": previous.commits_since_release,
            "last_release": previous.last_release,
            "fingerprint": fingerprint,
            "measured_at": previous.measured_at,
        }

    code, tests = _line_counts(path)
    recent, since_release, tag = _commit_counts(path, ref)
    return {
        "code_lines": code,
        "test_lines": tests,
        "commits_30d": recent,
        "commits_since_release": since_release,
        "last_release": tag,
        "fingerprint": fingerprint,
        "measured_at": time.time(),
    }


def scan(
    cfg: Config,
    default_branches: dict[str, str],
    skip: frozenset[str] = frozenset(),
    previous: dict[str, LocalRepo] | None = None,
) -> dict[str, LocalRepo]:
    """Read every listed repo's working copy, keyed by ``owner/name``.

    A repo with no checkout on this machine is not an error: the fleet is the
    list, and the local panels simply have nothing to say about that row.

    ``skip`` holds repos deliberately dropped - archived on GitHub, or named
    in JQ_IGNORE. Without it a checkout would keep a dropped repo on the board
    after the GitHub half had stopped reporting it.

    ``previous`` is the last scan's result, which lets each repo skip the
    expensive measurements when nothing about the clone has moved. Passing
    nothing simply means everything is measured, which is what a cold start
    wants anyway.
    """
    prior = previous or {}
    found: dict[str, LocalRepo] = {}

    # A configured root that does not exist is worth one error, not one per
    # repo per minute. Explicit paths are unaffected by it, so this only
    # withdraws the fallback rather than abandoning the whole scan.
    root = cfg.repo_root
    if root and not os.path.isdir(root):
        log.error("repo root %s is not a directory", root)
        root = ""
    if not root and not cfg.repo_paths:
        # Deliberate: nothing points at a working copy, so there are none to
        # report on, and an empty result is the honest answer rather than an
        # error every minute.
        return found

    for key in cfg.repos:
        if "/" not in key or key in skip:
            continue
        owner, repo_name = key.split("/", 1)
        path = cfg.repo_paths.get(key)
        if path is None:
            if not root:
                continue
            path = os.path.join(root, owner, repo_name)
        # `.git` is a directory in a plain checkout and a file in a worktree.
        if not os.path.exists(os.path.join(path, ".git")):
            # Not mounted, or mounted somewhere else. Normal for a repo you
            # monitor but have not checked out.
            log.debug("no working copy for %s at %s", key, path)
            continue

        # The mount point claims to be this repo; the origin remote is the only
        # thing that can confirm it. A wrong path in repos.yml would otherwise
        # report one repo's dirty files under another repo's name.
        origin = origin_owner_name(path)
        if origin is not None and (origin[0].lower(), origin[1].lower()) != (
            owner.lower(),
            repo_name.lower(),
        ):
            log.warning(
                "%s is a checkout of %s/%s, not %s - check repos.yml",
                path,
                origin[0],
                origin[1],
                key,
            )
            continue

        found[key] = scan_repo(
            repo_name,
            owner,
            path,
            cfg,
            default_branch=default_branches.get(key, "main"),
            previous=prior.get(key),
        )

    return found
