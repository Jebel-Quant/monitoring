"""The branches a healthy checkout never takes.

Every git call in this module is allowed to fail and must degrade rather than
raise: the collector reads working copies that are being actively worked in, so
a repo mid-rebase, a clone with no upstream, a symlink into nowhere and a
hand-mangled template pointer are all normal states, not exceptions. The tests
here drive each of those, because in the happy path none of this code runs.
"""

from __future__ import annotations

import os
import pathlib
import subprocess

import pytest
from test_fleet import config, make_checkout

from jq_collector import localgit
from jq_collector.config import Config
from jq_collector.localgit import (
    _ahead_behind,
    _fetch_age,
    _git,
    _git_dir,
    _line_counts,
    _tags_mtime,
    _template_ref,
    origin_owner_name,
)

# -- _git: the wrapper every other call goes through ------------------------


def test_a_git_binary_that_cannot_be_run_is_a_none_not_a_crash(monkeypatch, caplog):
    """OSError, e.g. git missing from PATH entirely inside a stripped image."""

    def explode(*_args, **_kwargs):
        raise OSError("no git here")

    monkeypatch.setattr(subprocess, "run", explode)

    assert _git("/anywhere", "status") is None
    assert "failed" in caplog.text


def test_a_git_call_that_times_out_is_a_none_not_a_crash(monkeypatch, caplog):
    """A 20s timeout exists because a repo on a stalled network mount can hang."""

    def stall(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(cmd="git", timeout=20)

    monkeypatch.setattr(subprocess, "run", stall)

    assert _git("/anywhere", "status") is None
    assert "failed" in caplog.text


def test_a_nonzero_exit_is_a_none(tmp_path):
    """`rev-parse` against a directory that is not a repo at all."""
    assert _git(str(tmp_path), "rev-parse", "HEAD") is None


# -- origin_owner_name: every URL shape a remote can carry ------------------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("git@github.com:o/r.git", ("o", "r")),
        ("git@github.com:o/r", ("o", "r")),
        ("https://github.com/o/r.git", ("o", "r")),
        ("ssh://git@github.com/o/r", ("o", "r")),
        # A path remote, which is what a local clone-of-a-clone has.
        ("/srv/mirrors/o/r", ("o", "r")),
        ("relative/o/r", ("o", "r")),
    ],
)
def test_owner_and_name_are_read_from_any_remote_shape(tmp_path, url, expected):
    path = make_checkout(tmp_path, "x", "y", origin=url)

    assert origin_owner_name(str(path)) == expected


def test_a_checkout_with_no_origin_has_no_identity(tmp_path):
    """`git init` with nothing added: a real state, and unusable as a repo id."""
    path = tmp_path / "bare"
    path.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)

    assert origin_owner_name(str(path)) is None


def test_a_remote_too_short_to_name_a_repo_is_refused(tmp_path):
    """`owner/name` needs two segments; one cannot be split into a repo id."""
    path = make_checkout(tmp_path, "x", "y", origin="https://example.com/lonely")

    assert origin_owner_name(str(path)) is None


# -- the template pointer ---------------------------------------------------


def test_a_malformed_pointer_is_data_not_a_crash(tmp_path, caplog):
    """Someone hand-edits .rhiza/template.yml and leaves it invalid YAML. The
    board should lose one label, not the whole local refresh."""
    pointer = tmp_path / ".rhiza" / "template.yml"
    pointer.parent.mkdir()
    pointer.write_text("ref: [unclosed\n")

    assert _template_ref(str(tmp_path), ".rhiza/template.yml") == ""
    assert "could not parse" in caplog.text


@pytest.mark.parametrize(
    "body",
    ["", "just a string\n", "- a\n- list\n", "ref:\n", "other: value\n"],
    ids=["empty", "scalar", "list", "null-ref", "no-ref"],
)
def test_a_pointer_without_a_usable_ref_yields_no_ref(tmp_path, body):
    pointer = tmp_path / ".rhiza" / "template.yml"
    pointer.parent.mkdir()
    pointer.write_text(body)

    assert _template_ref(str(tmp_path), ".rhiza/template.yml") == ""


def test_no_pointer_at_all_means_unmanaged(tmp_path):
    assert _template_ref(str(tmp_path), ".rhiza/template.yml") == ""


# -- git dir, fetch age, tag mtimes -----------------------------------------


def test_the_git_dir_is_absolute_even_when_git_reports_it_relative(tmp_path):
    path = make_checkout(tmp_path, "o", "r")

    git_dir = _git_dir(str(path))

    assert os.path.isabs(git_dir)
    assert pathlib.Path(git_dir).is_dir()


def test_a_directory_that_is_not_a_repo_has_no_git_dir(tmp_path):
    assert _git_dir(str(tmp_path)) == ""


def test_a_clone_that_has_never_fetched_has_no_fetch_age(tmp_path):
    """FETCH_HEAD only exists after a fetch; a fresh `git init` has none, and
    that is 'never', which the board must not render as 'just now'."""
    path = make_checkout(tmp_path, "o", "r")

    assert _fetch_age(_git_dir(str(path))) is None


def test_fetch_age_is_none_without_a_git_dir():
    assert _fetch_age("") is None


def test_fetch_age_is_read_from_fetch_head(tmp_path):
    path = make_checkout(tmp_path, "o", "r")
    git_dir = _git_dir(str(path))
    (pathlib.Path(git_dir) / "FETCH_HEAD").write_text("")

    age = _fetch_age(git_dir)

    assert age is not None and age >= 0.0


def test_tags_mtime_is_zero_without_a_git_dir():
    assert _tags_mtime("") == 0.0


def test_tags_mtime_is_zero_when_neither_place_exists(tmp_path):
    """No packed-refs and no refs/tags directory: nothing has ever been tagged."""
    assert _tags_mtime(str(tmp_path)) == 0.0


# -- line counting ----------------------------------------------------------


def test_line_counts_are_zero_when_git_cannot_list_files(tmp_path):
    assert _line_counts(str(tmp_path)) == (0, 0)


def test_a_tracked_file_that_is_not_on_disk_is_skipped(tmp_path):
    """Tracked-but-absent is normal mid-edit: `rm` without `git rm`. Reading it
    raises OSError, and one deleted file must not zero the whole count."""
    path = make_checkout(tmp_path, "o", "r")
    (path / "kept.py").write_text("a\nb\n")
    (path / "gone.py").write_text("x\ny\nz\n")
    subprocess.run(["git", "-C", str(path), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "two files"], check=True)
    (path / "gone.py").unlink()

    assert _line_counts(str(path)) == (2, 0)


def test_a_broken_symlink_is_skipped(tmp_path):
    path = make_checkout(tmp_path, "o", "r")
    (path / "real.py").write_text("a\n")
    (path / "dangling.py").symlink_to(path / "nowhere.py")
    subprocess.run(["git", "-C", str(path), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "symlink"], check=True)

    assert _line_counts(str(path)) == (1, 0)


# -- ahead/behind -----------------------------------------------------------


def test_a_branch_with_no_upstream_reports_neither_ahead_nor_behind(tmp_path):
    """None, not zero: 'no upstream configured' and 'in step with upstream' are
    different facts and the board labels them differently."""
    path = make_checkout(tmp_path, "o", "r")

    assert _ahead_behind(str(path)) == (None, None)


def test_ahead_and_behind_are_read_from_the_upstream(tmp_path):
    upstream = make_checkout(tmp_path / "up", "o", "r")
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(upstream), str(clone)], check=True)
    # An identity has to be set on the clone as well: make_checkout sets it
    # per-repo, and repo-local config is not cloned. A CI runner has no global
    # identity, so without this the commit below fails there and passes on any
    # laptop that happens to have one.
    for key, value in (("user.email", "t@example.com"), ("user.name", "T")):
        subprocess.run(["git", "-C", str(clone), "config", key, value], check=True)
    (clone / "new.py").write_text("a\n")
    subprocess.run(["git", "-C", str(clone), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(clone), "commit", "-qm", "ahead by one"], check=True)

    assert _ahead_behind(str(clone)) == (1, 0)


@pytest.mark.parametrize(
    "output", ["one", "a b", "1 2 3"], ids=["one-field", "non-numeric", "three"]
)
def test_an_unparseable_rev_list_count_is_unknown(monkeypatch, output):
    """The two counts come from one string; anything else is not an answer."""
    monkeypatch.setattr(localgit, "_git", lambda *_args: output)

    assert _ahead_behind("/anywhere") == (None, None)


# -- detached HEAD ----------------------------------------------------------


def test_a_detached_head_is_named_by_its_commit(tmp_path):
    """`rev-parse --abbrev-ref HEAD` says only "HEAD", which tells nobody
    anything; the short sha is actionable."""
    path = make_checkout(tmp_path, "o", "r")
    sha = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    subprocess.run(["git", "-C", str(path), "checkout", "-q", "--detach", sha], check=True)

    found = localgit.scan(config(("o/r",), tmp_path), {})

    assert found["o/r"].branch == f"detached@{sha[:7]}"


def test_a_repo_with_no_commits_at_all_is_survivable(tmp_path):
    """`git log` fails on an unborn branch, so there is no sha and no timestamp."""
    path = tmp_path / "o" / "r"
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    subprocess.run(
        ["git", "-C", str(path), "remote", "add", "origin", "git@github.com:o/r.git"], check=True
    )

    found = localgit.scan(config(("o/r",), tmp_path), {})

    assert found["o/r"].head_sha == ""
    assert found["o/r"].last_commit_at == 0.0


# -- the fleet walk ---------------------------------------------------------


def test_an_entry_without_a_slash_is_skipped(tmp_path):
    """JQ_REPOS is owner/name; a bare name cannot be located or joined on."""
    cfg = Config()
    object.__setattr__(cfg, "repos", ("bare-name",))
    object.__setattr__(cfg, "repo_root", str(tmp_path))
    object.__setattr__(cfg, "repo_paths", {})

    assert localgit.scan(cfg, {}) == {}


def test_with_no_root_a_repo_absent_from_the_paths_is_simply_skipped(tmp_path):
    """The shape a host-run collector has: explicit paths, no /repos to fall
    back on. A repo the paths do not name has nowhere to be looked for, and
    that is a quiet no-op rather than a scan of the current directory.
    """
    named = make_checkout(tmp_path / "somewhere", "o", "named")
    cfg = config(("o/named", "o/unnamed"), "", {"o/named": str(named)})

    found = localgit.scan(cfg, {})

    assert set(found) == {"o/named"}
