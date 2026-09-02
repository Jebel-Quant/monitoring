"""The fleet is the list, and only the list.

The board used to be assembled by a whole-org GitHub sweep plus a directory
walk under one mounted root. Both decided membership on their own: a new repo
in the org appeared unasked, and any checkout that happened to sit under the
root joined the board because its origin looked right. These tests pin the
replacement - a repo is monitored because it is named in the config, and for no
other reason - and pin the two ways that can go quietly wrong: a listed repo
with no checkout must be a no-op, and a checkout of the wrong repo must be
refused rather than reported under the listed repo's name.
"""

from __future__ import annotations

import dataclasses
import os
import pathlib
import subprocess

import pytest

from jq_collector import localgit, repos
from jq_collector import origin as origin_module
from jq_collector.config import Config

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]


def make_checkout(
    root: pathlib.Path, owner: str, name: str, origin: str | None = None
) -> pathlib.Path:
    """A real git checkout - the code shells out to git, so fixtures must too."""
    path = root / owner / name
    path.mkdir(parents=True)
    git = ["git", "-C", str(path)]
    subprocess.run([*git[:1], "init", "-q", "-b", "main", str(path)], check=True)
    subprocess.run([*git, "config", "user.email", "t@example.com"], check=True)
    subprocess.run([*git, "config", "user.name", "T"], check=True)
    (path / "README.md").write_text("x\n")
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-qm", "init"], check=True)
    subprocess.run(
        [*git, "remote", "add", "origin", origin or f"git@github.com:{owner}/{name}.git"],
        check=True,
    )
    return path


def config(
    repos: tuple[str, ...],
    repo_root: pathlib.Path | str,
    repo_paths: dict[str, str] | None = None,
) -> Config:
    cfg = Config()
    object.__setattr__(cfg, "repos", repos)
    object.__setattr__(cfg, "repo_root", str(repo_root))
    object.__setattr__(cfg, "repo_paths", repo_paths or {})
    return cfg


# -- the local half ----------------------------------------------------------


def test_only_listed_repos_are_read(tmp_path):
    """An unlisted checkout sitting right next to a listed one is not the fleet."""
    make_checkout(tmp_path, "Jebel-Quant", "rhiza")
    make_checkout(tmp_path, "Jebel-Quant", "not-listed")

    found = localgit.scan(config(("Jebel-Quant/rhiza",), tmp_path), {})

    assert set(found) == {"Jebel-Quant/rhiza"}


def test_a_listed_repo_without_a_checkout_is_a_quiet_no_op(tmp_path):
    """You may monitor a repo you have never cloned; the GitHub half still works."""
    make_checkout(tmp_path, "Jebel-Quant", "rhiza")

    found = localgit.scan(config(("Jebel-Quant/rhiza", "Jebel-Quant/actions"), tmp_path), {})

    assert set(found) == {"Jebel-Quant/rhiza"}


def test_a_checkout_of_the_wrong_repo_is_refused(tmp_path, caplog):
    """A bad path in repos.yml must not file one repo's dirt under another's name."""
    # Mounted where Jebel-Quant/rhiza is expected, but it is really cvxgrp/cvxpy.
    make_checkout(tmp_path, "Jebel-Quant", "rhiza", origin="git@github.com:cvxgrp/cvxpy.git")

    found = localgit.scan(config(("Jebel-Quant/rhiza",), tmp_path), {})

    assert found == {}
    assert "check repos.yml" in caplog.text


def test_an_ignored_repo_is_skipped_even_when_checked_out(tmp_path):
    make_checkout(tmp_path, "Jebel-Quant", "rhiza")
    cfg = config(("Jebel-Quant/rhiza",), tmp_path)

    found = localgit.scan(cfg, {}, skip=frozenset({"Jebel-Quant/rhiza"}))

    assert found == {}


def test_the_checkout_is_read_at_the_canonical_path(tmp_path):
    """<repo_root>/<owner>/<name>, which is exactly where the override mounts it."""
    path = make_checkout(tmp_path, "cvxgrp", "cvxsimulator")

    found = localgit.scan(config(("cvxgrp/cvxsimulator",), tmp_path), {})

    assert found["cvxgrp/cvxsimulator"].path == str(path)
    assert found["cvxgrp/cvxsimulator"].branch == "main"


# -- where a checkout actually is --------------------------------------------


def test_a_checkout_outside_the_canonical_layout_is_read(tmp_path):
    """<root>/<owner>/<name> is a default, not a rule.

    The bind mounts normalise every checkout onto that shape inside the
    container, so the default is always right there. On the host it is not:
    ~/repos/tschm/rhiza_projects/cs is a checkout of tschm/cs, and no amount of
    joining owner to name will produce that path. Four repos in the real fleet
    are laid out this way, and they silently vanished from the working-copy
    panels while staying on the GitHub ones.
    """
    path = make_checkout(tmp_path / "nested" / "elsewhere", "tschm", "cs")
    cfg = config(("tschm/cs",), tmp_path, {"tschm/cs": str(path)})

    found = localgit.scan(cfg, {})

    assert set(found) == {"tschm/cs"}
    assert found["tschm/cs"].path == str(path)


def test_an_explicit_path_wins_over_the_canonical_one(tmp_path):
    canonical = make_checkout(tmp_path, "tschm", "cs")
    elsewhere = make_checkout(tmp_path / "other", "tschm", "cs")
    cfg = config(("tschm/cs",), tmp_path, {"tschm/cs": str(elsewhere)})

    found = localgit.scan(cfg, {})

    assert found["tschm/cs"].path == str(elsewhere)
    assert found["tschm/cs"].path != str(canonical)


def test_repos_without_an_explicit_path_still_fall_back_to_the_root(tmp_path):
    """The container relies on this: mounts, no JQ_REPO_PATHS at all."""
    make_checkout(tmp_path, "Jebel-Quant", "rhiza")
    nested = make_checkout(tmp_path / "nested", "tschm", "cs")
    cfg = config(("Jebel-Quant/rhiza", "tschm/cs"), tmp_path, {"tschm/cs": str(nested)})

    found = localgit.scan(cfg, {})

    assert set(found) == {"Jebel-Quant/rhiza", "tschm/cs"}


def test_an_explicit_path_to_the_wrong_repo_is_still_refused(tmp_path, caplog):
    """The origin check matters more now, not less - the path is arbitrary."""
    path = make_checkout(
        tmp_path / "nested", "tschm", "cs", origin="git@github.com:cvxgrp/cvxpy.git"
    )
    cfg = config(("tschm/cs",), tmp_path, {"tschm/cs": str(path)})

    assert localgit.scan(cfg, {}) == {}
    assert "check repos.yml" in caplog.text


def test_explicit_paths_work_with_no_repo_root_at_all(tmp_path):
    """Running outside the container there is no /repos to fall back to."""
    path = make_checkout(tmp_path / "nested", "tschm", "cs")
    cfg = config(("tschm/cs",), "", {"tschm/cs": str(path)})

    assert set(localgit.scan(cfg, {})) == {"tschm/cs"}


def test_nothing_local_configured_is_a_quiet_no_op(tmp_path):
    assert localgit.scan(config(("tschm/cs",), "", {}), {}) == {}


def test_a_missing_root_does_not_abandon_the_explicit_paths(tmp_path, caplog):
    """One error for the bad root, and the repos that do not need it still work."""
    path = make_checkout(tmp_path / "nested", "tschm", "cs")
    cfg = config(("tschm/cs",), tmp_path / "does-not-exist", {"tschm/cs": str(path)})

    assert set(localgit.scan(cfg, {})) == {"tschm/cs"}
    assert "is not a directory" in caplog.text


def test_malformed_repo_paths_refuse_to_start(monkeypatch):
    """A dropped pair would take one repo off the board and say nothing."""
    monkeypatch.setenv("JQ_REPO_PATHS", "tschm/cs")
    with pytest.raises(ValueError, match="owner/name=path"):
        Config()


def test_repo_paths_are_parsed_and_expanded(monkeypatch):
    monkeypatch.setenv("JQ_REPO_PATHS", "tschm/cs=~/repos/tschm/rhiza_projects/cs, a/b=/tmp/b")
    paths = Config().repo_paths
    assert paths["a/b"] == "/tmp/b"
    assert paths["tschm/cs"] == os.path.expanduser("~/repos/tschm/rhiza_projects/cs")


# -- measurements and the activity cache -------------------------------------


def commit(path: pathlib.Path, rel: str, body: str, message: str = "c") -> None:
    target = path / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body)
    subprocess.run(["git", "-C", str(path), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", message], check=True)


def test_lines_are_split_into_code_and_tests(tmp_path):
    """Both fleet conventions count as tests: a tests/ tree, and a test_ prefix."""
    path = make_checkout(tmp_path, "Jebel-Quant", "rhiza")
    commit(path, "src/thing.py", "a\nb\nc\n")
    commit(path, "tests/test_thing.py", "d\ne\n")
    commit(path, "src/test_inline.py", "f\n")
    # Neither of these is code: one is prose, one is configuration.
    commit(path, "README.md", "x\n" * 99)
    commit(path, "pyproject.toml", "y\n" * 99)

    found = localgit.scan(config(("Jebel-Quant/rhiza",), tmp_path), {})

    assert found["Jebel-Quant/rhiza"].code_lines == 3
    assert found["Jebel-Quant/rhiza"].test_lines == 3


def test_a_repo_with_no_tags_reports_no_release_rather_than_zero(tmp_path):
    """Zero unreleased commits means "all shipped"; never tagged is the opposite."""
    make_checkout(tmp_path, "Jebel-Quant", "rhiza")

    found = localgit.scan(config(("Jebel-Quant/rhiza",), tmp_path), {})

    assert found["Jebel-Quant/rhiza"].commits_since_release is None
    assert found["Jebel-Quant/rhiza"].last_release == ""


def test_commits_are_counted_from_the_newest_tag(tmp_path):
    path = make_checkout(tmp_path, "Jebel-Quant", "rhiza")
    subprocess.run(["git", "-C", str(path), "tag", "v1.0.0"], check=True)
    commit(path, "src/a.py", "a\n")
    commit(path, "src/b.py", "b\n")

    found = localgit.scan(config(("Jebel-Quant/rhiza",), tmp_path), {})

    assert found["Jebel-Quant/rhiza"].last_release == "v1.0.0"
    assert found["Jebel-Quant/rhiza"].commits_since_release == 2
    assert found["Jebel-Quant/rhiza"].commits_30d == 3


def test_a_quiet_repo_is_not_measured_again(tmp_path, monkeypatch):
    """The whole point of the fingerprint: no activity, no work.

    Proved by making measurement itself fail. A cached pass must not notice,
    an uncached one must - anything weaker only shows the numbers came out the
    same, which they would even if every file had been read again.
    """
    path = make_checkout(tmp_path, "Jebel-Quant", "rhiza")
    commit(path, "src/a.py", "a\n")
    cfg = config(("Jebel-Quant/rhiza",), tmp_path)
    first = localgit.scan(cfg, {})

    def explode(*_args):
        raise AssertionError("measured a repo that had not moved")

    monkeypatch.setattr(localgit, "_line_counts", explode)
    monkeypatch.setattr(localgit, "_commit_counts", explode)

    second = localgit.scan(cfg, {}, previous=first)
    assert second["Jebel-Quant/rhiza"].code_lines == 1

    with pytest.raises(AssertionError, match="had not moved"):
        localgit.scan(cfg, {})


def test_a_new_commit_invalidates_the_cache(tmp_path):
    path = make_checkout(tmp_path, "Jebel-Quant", "rhiza")
    commit(path, "src/a.py", "a\n")
    cfg = config(("Jebel-Quant/rhiza",), tmp_path)
    first = localgit.scan(cfg, {})

    unchanged = localgit.scan(cfg, {}, previous=first)
    assert unchanged["Jebel-Quant/rhiza"].measured_at == first["Jebel-Quant/rhiza"].measured_at

    commit(path, "src/b.py", "b\nc\n")
    after = localgit.scan(cfg, {}, previous=unchanged)
    assert after["Jebel-Quant/rhiza"].measured_at != first["Jebel-Quant/rhiza"].measured_at
    assert after["Jebel-Quant/rhiza"].code_lines == 3


def test_tagging_the_current_commit_invalidates_the_cache(tmp_path):
    """A release moves neither HEAD nor the tree, and must still be noticed."""
    path = make_checkout(tmp_path, "Jebel-Quant", "rhiza")
    commit(path, "src/a.py", "a\n")
    cfg = config(("Jebel-Quant/rhiza",), tmp_path)
    first = localgit.scan(cfg, {})
    assert first["Jebel-Quant/rhiza"].commits_since_release is None

    subprocess.run(["git", "-C", str(path), "tag", "v1.0.0"], check=True)

    after = localgit.scan(cfg, {}, previous=first)
    assert after["Jebel-Quant/rhiza"].last_release == "v1.0.0"
    assert after["Jebel-Quant/rhiza"].commits_since_release == 0


def test_template_drift_is_re_read_even_when_the_measurements_are_cached(tmp_path):
    """Drift is the one thing that changes while the clone stands still.

    The upstream publishes a release, or someone edits the pointer in place;
    neither moves HEAD. Caching the measurements must not freeze the pointer
    along with them, or the board would go on reporting the old ref.
    """
    path = make_checkout(tmp_path, "Jebel-Quant", "rhiza")
    pointer = path / ".rhiza" / "template.yml"
    pointer.parent.mkdir()
    pointer.write_text("ref: v1.0.0\n")
    commit(path, "src/a.py", "a\n")
    cfg = config(("Jebel-Quant/rhiza",), tmp_path)

    first = localgit.scan(cfg, {})
    assert first["Jebel-Quant/rhiza"].rhiza_ref == "v1.0.0"

    # Edited in place and left uncommitted: HEAD does not move, so the
    # measurements are entitled to be cached. The ref is not.
    pointer.write_text("ref: v2.0.0\n")

    second = localgit.scan(cfg, {}, previous=first)
    assert second["Jebel-Quant/rhiza"].rhiza_ref == "v2.0.0"


def test_a_measurement_older_than_the_cap_is_retaken(tmp_path):
    """The 30-day window slides even when nobody commits."""
    path = make_checkout(tmp_path, "Jebel-Quant", "rhiza")
    commit(path, "src/a.py", "a\n")
    cfg = config(("Jebel-Quant/rhiza",), tmp_path)
    first = localgit.scan(cfg, {})

    row = first["Jebel-Quant/rhiza"]
    aged = dataclasses.replace(row, measured_at=row.measured_at - cfg.measure_max_age - 1)

    after = localgit.scan(cfg, {}, previous={"Jebel-Quant/rhiza": aged})
    assert after["Jebel-Quant/rhiza"].measured_at > aged.measured_at


# -- the GitHub half ---------------------------------------------------------


def test_list_repos_asks_only_for_listed_repos(make_client, cfg):
    """No /orgs/<org>/repos call: GitHub is never asked what the fleet contains."""
    object.__setattr__(cfg, "repos", ("Jebel-Quant/rhiza", "cvxgrp/cvxsimulator"))
    client = make_client(
        {
            "/repos/Jebel-Quant/rhiza": {"full_name": "Jebel-Quant/rhiza"},
            "/repos/cvxgrp/cvxsimulator": {"full_name": "cvxgrp/cvxsimulator"},
        }
    )

    repos = client.list_repos()

    assert [r["full_name"] for r in repos] == ["Jebel-Quant/rhiza", "cvxgrp/cvxsimulator"]
    assert not [c for c in client.calls if c.startswith("/orgs/")]


def test_an_unreadable_repo_does_not_lose_the_others(make_client, cfg, caplog):
    """One typo in the list must cost one row, not the whole board."""
    object.__setattr__(cfg, "repos", ("Jebel-Quant/typo", "Jebel-Quant/rhiza"))
    client = make_client({"/repos/Jebel-Quant/rhiza": {"full_name": "Jebel-Quant/rhiza"}})

    repos = client.list_repos()

    assert [r["full_name"] for r in repos] == ["Jebel-Quant/rhiza"]
    assert "Jebel-Quant/typo" in caplog.text


# -- reading repos.yml -------------------------------------------------------
#
# One file, read at startup by the collector itself. It used to be turned into
# two environment lines by a script the launcher ran, which meant three places
# a repo could be lost between the file and the board.


def write_fleet(tmp_path: pathlib.Path, body: str) -> str:
    source = tmp_path / "repos.yml"
    source.write_text(f"repos:\n{body}")
    return str(source)


def test_a_checkout_is_named_by_its_origin(tmp_path):
    path = make_checkout(tmp_path, "cvxgrp", "cvxsimulator")

    fleet, paths, forges = repos.load(write_fleet(tmp_path, f"  - path: {path}\n"))

    assert fleet == ("cvxgrp/cvxsimulator",)
    assert paths == {"cvxgrp/cvxsimulator": str(path)}
    # make_checkout gives the clone a github.com origin, so the forge follows
    # from the host without the entry having to say.
    assert forges == {"cvxgrp/cvxsimulator": "github"}


def test_an_explicit_repo_overrides_the_origin(tmp_path):
    """For a fork you want the board to follow upstream, not your copy."""
    path = make_checkout(tmp_path, "me", "cvxpy")
    body = f"  - path: {path}\n    repo: cvxpy/cvxpy\n"

    assert repos.load(write_fleet(tmp_path, body)) == (
        ("cvxpy/cvxpy",),
        {"cvxpy/cvxpy": str(path)},
        {"cvxpy/cvxpy": "github"},
    )


def test_an_entry_may_name_a_repo_with_no_checkout(tmp_path):
    fleet, paths, forges = repos.load(write_fleet(tmp_path, "  - repo: Jebel-Quant/actions\n"))

    assert fleet == ("Jebel-Quant/actions",)
    assert paths == {}
    # No checkout means no origin to infer from, so it falls to the default.
    assert forges == {"Jebel-Quant/actions": "github"}


def test_a_bare_string_entry_is_a_path(tmp_path):
    path = make_checkout(tmp_path, "Jebel-Quant", "rhiza")

    assert repos.load(write_fleet(tmp_path, f"  - {path}\n"))[0] == ("Jebel-Quant/rhiza",)


def test_a_named_repo_whose_checkout_is_missing_keeps_its_github_panels(tmp_path, caplog):
    """The supported way to run without mounting a home directory.

    An unreachable path is not an error - the mount may simply not be there -
    but it must be said out loud, because a typo in repos.yml looks identical
    from in here and would otherwise silently empty half a repo's row.
    """
    body = "  - path: ~/nowhere/rhiza\n    repo: Jebel-Quant/rhiza\n"

    with caplog.at_level("WARNING"):
        fleet, paths, _forges = repos.load(write_fleet(tmp_path, body))

    assert fleet == ("Jebel-Quant/rhiza",)
    assert paths == {}
    assert "no checkout for Jebel-Quant/rhiza" in caplog.text


@pytest.mark.parametrize(
    "body",
    [
        "  - repo: no-slash\n",  # not owner/name, and no path to derive it from
        "  - {}\n",  # neither
        "  - path: /definitely/not/here\n",  # unreachable, and nothing to fall back on
        "  - 42\n",  # not a path and not a mapping
    ],
)
def test_a_broken_entry_refuses_to_start(tmp_path, body):
    """Better a refusal at startup than a board that is quietly short a repo."""
    with pytest.raises(repos.FleetError):
        repos.load(write_fleet(tmp_path, body))


def test_the_refusal_names_the_offending_value(tmp_path):
    """`entry 7` in a twenty-five entry file means counting lines. The value is
    what you can search the file for."""
    with pytest.raises(repos.FleetError, match="no-slash"):
        repos.load(write_fleet(tmp_path, "  - repo: no-slash\n"))


def test_a_directory_that_is_not_a_checkout_is_refused(tmp_path):
    (tmp_path / "empty").mkdir()
    with pytest.raises(repos.FleetError):
        repos.load(write_fleet(tmp_path, f"  - path: {tmp_path / 'empty'}\n"))


def test_the_same_repo_twice_is_refused(tmp_path):
    """Two entries, one row on the board: the second would silently win."""
    body = "  - repo: a/b\n  - repo: a/b\n"
    with pytest.raises(repos.FleetError):
        repos.load(write_fleet(tmp_path, body))


@pytest.mark.parametrize(
    "body",
    ["  - repo: [\n", ""],  # unparseable, and no `repos:` list at all
)
def test_an_unusable_file_refuses_to_start(tmp_path, body):
    source = tmp_path / "repos.yml"
    source.write_text(body)
    with pytest.raises(repos.FleetError):
        repos.load(str(source))


def test_a_missing_file_refuses_to_start(tmp_path):
    with pytest.raises(repos.FleetError):
        repos.load(str(tmp_path / "absent.yml"))


# -- host paths --------------------------------------------------------------
#
# In the container the home directory is one read-only mount, and repos.yml is
# written against the host's view of it. This is the whole translation, and it
# is the reason a checkout at any path can be named at all - the per-repo bind
# mounts it replaced could only express <root>/<owner>/<name>.


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("~/repos/rhiza", "/host/repos/rhiza"),
        ("~", "/host"),
        ("repos/rhiza", "/host/repos/rhiza"),  # relative: also relative to home
        ("/nowhere/rhiza", "/host/nowhere/rhiza"),  # absolute, and not on this fs
    ],
)
def test_a_path_is_read_through_the_host_mount(raw, expected):
    assert repos.resolve_path(raw, "/host") == expected


def test_an_absolute_path_that_exists_is_taken_as_written(tmp_path):
    """The collector running natively, where there is no mount to look under."""
    assert repos.resolve_path(str(tmp_path), "/host") == str(tmp_path)


def test_without_a_mount_a_path_is_just_a_path(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))

    assert repos.resolve_path("~/repos/rhiza", "") == str(tmp_path / "repos" / "rhiza")


# -- and how Config folds it in ----------------------------------------------


def test_config_reads_the_fleet_out_of_the_file(tmp_path, monkeypatch):
    path = make_checkout(tmp_path, "Jebel-Quant", "rhiza")
    source = write_fleet(tmp_path, f"  - path: {path}\n  - repo: cvxgrp/cvxsimulator\n")
    monkeypatch.setenv("JQ_REPOS_FILE", source)

    cfg = Config(repos_file=source)

    assert cfg.repos == ("Jebel-Quant/rhiza", "cvxgrp/cvxsimulator")
    assert cfg.repo_paths == {"Jebel-Quant/rhiza": str(path)}


def test_an_explicit_path_in_the_environment_beats_the_file(tmp_path):
    """The escape hatch: one awkward path corrected without editing the file."""
    path = make_checkout(tmp_path, "Jebel-Quant", "rhiza")
    source = write_fleet(tmp_path, f"  - path: {path}\n")

    cfg = Config(repos_file=source, repo_paths={"Jebel-Quant/rhiza": "/elsewhere"})

    assert cfg.repo_paths == {"Jebel-Quant/rhiza": "/elsewhere"}


def test_no_file_leaves_the_environment_in_charge(tmp_path, monkeypatch):
    """A deployment with nothing to mount - a server, or CI."""
    monkeypatch.setenv("JQ_REPOS", "a/b,c/d")

    cfg = Config(repos_file=str(tmp_path / "absent.yml"))

    assert cfg.repos == ("a/b", "c/d")


def test_a_broken_file_stops_the_collector(tmp_path):
    """SystemExit, not a short fleet: a board that came up missing repos and
    said nothing is worse than one that did not come up."""
    source = write_fleet(tmp_path, "  - repo: no-slash\n")

    with pytest.raises(SystemExit):
        Config(repos_file=source)


# -- who a checkout says it is -----------------------------------------------
#
# The origin URL is the only thing that can name a checkout, and git writes it
# in several shapes. Getting one wrong puts a repo on the board under the wrong
# name, or drops it - neither of which the URL itself would ever hint at.


@pytest.mark.parametrize(
    "origin",
    [
        "git@github.com:Jebel-Quant/rhiza.git",
        "https://github.com/Jebel-Quant/rhiza.git",
        "https://github.com/Jebel-Quant/rhiza",
        "ssh://git@github.com/Jebel-Quant/rhiza.git",
        "/srv/mirrors/Jebel-Quant/rhiza",  # a local clone of a local clone
    ],
)
def test_every_shape_of_origin_url_names_the_same_repo(tmp_path, origin):
    path = make_checkout(tmp_path, "somewhere", "else", origin=origin)

    assert repos.load(write_fleet(tmp_path, f"  - path: {path}\n"))[0] == ("Jebel-Quant/rhiza",)


@pytest.mark.parametrize("origin", ["rhiza", ""])
def test_an_origin_that_names_no_owner_is_refused(tmp_path, origin):
    """No owner means no `owner/name`, and guessing one would file the repo
    under a name that does not exist on GitHub."""
    path = make_checkout(tmp_path, "somewhere", "else")
    if origin:
        subprocess.run(["git", "-C", str(path), "remote", "set-url", "origin", origin], check=True)
    else:
        subprocess.run(["git", "-C", str(path), "remote", "remove", "origin"], check=True)

    with pytest.raises(repos.FleetError, match="origin"):
        repos.load(write_fleet(tmp_path, f"  - path: {path}\n"))


def test_git_being_unrunnable_is_refused_not_guessed_at(tmp_path, monkeypatch):
    """Same answer as a missing remote: the collector will not invent a name."""
    path = make_checkout(tmp_path, "Jebel-Quant", "rhiza")
    # The git call lives in `origin` now, which is what both repos.py and
    # localgit.py read a remote through.
    monkeypatch.setattr(
        origin_module.subprocess, "run", lambda *_a, **_k: (_ for _ in ()).throw(OSError("no git"))
    )

    with pytest.raises(repos.FleetError, match="origin"):
        repos.load(write_fleet(tmp_path, f"  - path: {path}\n"))


# -- which forge each entry belongs to ---------------------------------------
#
# The forge is what decides which API a repo is read through, so getting it
# wrong is not a crash but a repo on the board with every remote panel blank.
# It is inferred from the origin host where there is a checkout, and stated
# outright where there is not.


def test_the_forge_is_inferred_from_a_checkouts_origin_host(tmp_path):
    """Free, because the origin URL is parsed for the repo's name anyway."""
    path = make_checkout(tmp_path, "acme", "web", origin="git@gitlab.com:acme/web.git")

    _fleet, _paths, forges = repos.load(write_fleet(tmp_path, f"  - path: {path}\n"))

    assert forges == {"acme/web": "gitlab"}


def test_a_gitlab_subgroup_keeps_its_whole_namespace(tmp_path):
    """The old parser kept the last two segments, which named a project that
    does not exist and an API path that 404s."""
    path = make_checkout(
        tmp_path, "x", "y", origin="https://gitlab.com/acme/platform/infra/web.git"
    )

    fleet, paths, forges = repos.load(write_fleet(tmp_path, f"  - path: {path}\n"))

    assert fleet == ("acme/platform/infra/web",)
    assert paths == {"acme/platform/infra/web": str(path)}
    assert forges == {"acme/platform/infra/web": "gitlab"}


def test_an_explicit_forge_wins_over_the_origin(tmp_path):
    """For a checkout whose origin is a mirror on the other forge."""
    path = make_checkout(tmp_path, "acme", "web", origin="git@github.com:acme/web.git")
    body = f"  - path: {path}\n    forge: gitlab\n"

    _fleet, _paths, forges = repos.load(write_fleet(tmp_path, body))

    assert forges == {"acme/web": "gitlab"}


def test_an_entry_with_no_checkout_states_its_forge(tmp_path):
    """No origin to infer from, so anything but the default has to say."""
    body = "  - repo: acme/platform/infra/web\n    forge: gitlab\n"

    fleet, paths, forges = repos.load(write_fleet(tmp_path, body))

    assert fleet == ("acme/platform/infra/web",)
    assert paths == {}
    assert forges == {"acme/platform/infra/web": "gitlab"}


def test_an_explicit_repo_still_takes_its_forge_from_the_origin(tmp_path):
    """`repo:` overrides the name - that is what it is for, on a fork - but the
    origin's host is still the best evidence of where the repo lives."""
    path = make_checkout(tmp_path, "me", "web", origin="git@gitlab.com:me/web.git")
    body = f"  - path: {path}\n    repo: acme/web\n"

    _fleet, _paths, forges = repos.load(write_fleet(tmp_path, body))

    assert forges == {"acme/web": "gitlab"}


@pytest.mark.parametrize("declared", ["gitbucket", "GitHub Enterprise", "gitea"])
def test_a_forge_nobody_implements_is_refused(tmp_path, declared):
    """Reading an unknown name as GitHub would put the repo on the board with
    every remote panel wrong and nothing saying why."""
    body = f"  - repo: acme/web\n    forge: {declared}\n"

    with pytest.raises(repos.FleetError, match="forge"):
        repos.load(write_fleet(tmp_path, body))


@pytest.mark.parametrize("declared", ["gitlab", "GitLab", "  GITLAB  "])
def test_a_declared_forge_is_case_and_space_insensitive(tmp_path, declared):
    body = f"  - repo: acme/web\n    forge: '{declared}'\n"

    assert repos.load(write_fleet(tmp_path, body))[2] == {"acme/web": "gitlab"}


def test_the_same_path_on_two_forges_is_refused(tmp_path):
    """One `repo` label is one repo, and the board's whole label scheme rests on
    that. A merged pair would report one repo's CI under the other's name and
    read as a working board while doing it."""
    body = "  - repo: acme/web\n  - repo: acme/web\n    forge: gitlab\n"

    with pytest.raises(repos.FleetError, match="on github and on gitlab"):
        repos.load(write_fleet(tmp_path, body))


def test_the_duplicate_message_stays_plain_when_the_forge_matches(tmp_path):
    """Naming a forge here would suggest the forge was the problem."""
    body = "  - repo: acme/web\n  - repo: acme/web\n"

    with pytest.raises(repos.FleetError, match="acme/web is listed twice$"):
        repos.load(write_fleet(tmp_path, body))


# -- a folder of repos -------------------------------------------------------
#
# One entry, however many checkouts are in it. Membership is still decided by
# the file - the folder is named there, and only its own children are looked at
# - which is what keeps this from being the whole-root walk it replaced.


def test_every_checkout_in_a_folder_joins_the_fleet(tmp_path):
    make_checkout(tmp_path, "org", "alpha")
    make_checkout(tmp_path, "org", "beta")

    fleet, paths, forges = repos.load(write_fleet(tmp_path, f"  - folder: {tmp_path / 'org'}\n"))

    assert fleet == ("org/alpha", "org/beta")
    assert paths == {
        "org/alpha": str(tmp_path / "org" / "alpha"),
        "org/beta": str(tmp_path / "org" / "beta"),
    }
    assert forges == {"org/alpha": "github", "org/beta": "github"}


def test_a_folder_is_scanned_one_level_and_no_deeper(tmp_path):
    """`folder: ~` would otherwise be a whole-disk walk.

    A folder says where you keep a set of repos, and one level is what that
    means. Anything more is the discovery this file exists to have got rid of.
    """
    make_checkout(tmp_path, "org", "alpha")
    make_checkout(tmp_path / "org" / "deeper", "org", "buried")
    (tmp_path / "org" / "not-a-repo").mkdir()
    (tmp_path / "org" / "notes.md").write_text("x\n")

    fleet, _paths, _forges = repos.load(write_fleet(tmp_path, f"  - folder: {tmp_path / 'org'}\n"))

    assert fleet == ("org/alpha",)


def test_a_folder_that_is_not_there_is_refused(tmp_path):
    """The same call as an unreachable `path`: better a refusal than a board
    quietly short a folder's worth of repos."""
    with pytest.raises(repos.FleetError, match="cannot read folder"):
        repos.load(write_fleet(tmp_path, f"  - folder: {tmp_path / 'absent'}\n"))


def test_a_folder_with_no_checkouts_in_it_is_refused(tmp_path):
    """Naming the folder was a request for the repos in it; there are none."""
    (tmp_path / "org").mkdir()
    (tmp_path / "org" / "not-a-repo").mkdir()

    with pytest.raises(repos.FleetError, match="holds no git checkouts"):
        repos.load(write_fleet(tmp_path, f"  - folder: {tmp_path / 'org'}\n"))


@pytest.mark.parametrize("key", ["path", "repo"])
def test_a_folder_cannot_also_be_a_path_or_a_repo(tmp_path, key):
    """A folder stands for many repos, so there is no one path or name for it."""
    make_checkout(tmp_path, "org", "alpha")
    body = f"  - folder: {tmp_path / 'org'}\n    {key}: whatever/it-is\n"

    with pytest.raises(repos.FleetError, match="cannot also carry"):
        repos.load(write_fleet(tmp_path, body))


@pytest.mark.parametrize("explicit_first", [True, False])
def test_an_entry_naming_a_repo_outright_wins_over_the_folder(tmp_path, explicit_first):
    """How one repo in a listed folder gets a `repo:` override.

    The checkout is a fork and the board should follow upstream. Colliding
    would mean the folder could not be used at all; the deliberate entry wins,
    whichever order the two are written in.
    """
    make_checkout(tmp_path, "org", "alpha")
    fork = make_checkout(tmp_path, "org", "beta")
    folder = f"  - folder: {tmp_path / 'org'}\n"
    override = f"  - path: {fork}\n    repo: upstream/beta\n"

    fleet, paths, _forges = repos.load(
        write_fleet(tmp_path, override + folder if explicit_first else folder + override)
    )

    assert set(fleet) == {"org/alpha", "upstream/beta"}
    assert "org/beta" not in paths
    assert paths["upstream/beta"] == str(fork)


def test_a_repo_listed_outright_and_swept_up_by_a_folder_is_one_repo(tmp_path):
    """Belt and braces in repos.yml is not a duplicate to refuse over."""
    path = make_checkout(tmp_path, "org", "alpha")
    body = f"  - folder: {tmp_path / 'org'}\n  - path: {path}\n"

    fleet, paths, _forges = repos.load(write_fleet(tmp_path, body))

    assert fleet == ("org/alpha",)
    assert paths == {"org/alpha": str(path)}


def test_two_folders_holding_the_same_repo_are_refused(tmp_path):
    """Two checkouts, one row: there is no saying which one the board means."""
    make_checkout(tmp_path / "one", "org", "alpha")
    make_checkout(tmp_path / "two", "org", "alpha")
    body = f"  - folder: {tmp_path / 'one' / 'org'}\n  - folder: {tmp_path / 'two' / 'org'}\n"

    with pytest.raises(repos.FleetError, match="checked out at .* and at "):
        repos.load(write_fleet(tmp_path, body))


def test_a_declared_forge_covers_every_checkout_in_the_folder(tmp_path):
    """A whole folder on the other forge is one line, not one line per repo."""
    make_checkout(tmp_path, "acme", "web", origin="git@gitlab.com:acme/web.git")
    make_checkout(tmp_path, "acme", "api", origin="git@gitlab.com:acme/api.git")
    body = f"  - folder: {tmp_path / 'acme'}\n    forge: gitlab\n"

    assert repos.load(write_fleet(tmp_path, body))[2] == {
        "acme/api": "gitlab",
        "acme/web": "gitlab",
    }


def test_a_bad_forge_on_a_folder_is_refused_once(tmp_path):
    make_checkout(tmp_path, "org", "alpha")
    body = f"  - folder: {tmp_path / 'org'}\n    forge: gitbucket\n"

    with pytest.raises(repos.FleetError, match="forge 'gitbucket'"):
        repos.load(write_fleet(tmp_path, body))


def test_a_scratch_clone_in_the_folder_is_skipped_not_fatal(tmp_path, caplog):
    """A clone with no origin cannot be named, and nobody asked to monitor it.

    A listed `path` to the same clone is still refused - that path was a
    deliberate statement. A folder is not, so one stray clone must not take the
    whole board down.
    """
    make_checkout(tmp_path, "org", "alpha")
    scratch = make_checkout(tmp_path, "org", "scratch")
    subprocess.run(["git", "-C", str(scratch), "remote", "remove", "origin"], check=True)

    with caplog.at_level("WARNING"):
        fleet, paths, _forges = repos.load(
            write_fleet(tmp_path, f"  - folder: {tmp_path / 'org'}\n")
        )

    assert fleet == ("org/alpha",)
    assert str(scratch) not in paths.values()
    assert "skipping" in caplog.text


def test_a_folder_of_nothing_but_scratch_clones_refuses_to_start(tmp_path):
    """The file said something and none of it survived - that is not a fleet."""
    scratch = make_checkout(tmp_path, "org", "scratch")
    subprocess.run(["git", "-C", str(scratch), "remote", "remove", "origin"], check=True)

    with pytest.raises(repos.FleetError, match="could identify"):
        repos.load(write_fleet(tmp_path, f"  - folder: {tmp_path / 'org'}\n"))


def test_a_folder_is_read_through_the_host_mount(tmp_path):
    """`~/repos/org` in repos.yml is /host/repos/org inside the container."""
    make_checkout(tmp_path / "repos", "org", "alpha")

    fleet, paths, _forges = repos.load(
        write_fleet(tmp_path, "  - folder: ~/repos/org\n"), host_root=str(tmp_path)
    )

    assert fleet == ("org/alpha",)
    assert paths == {"org/alpha": str(tmp_path / "repos" / "org" / "alpha")}


@pytest.mark.parametrize("outright_first", [True, False])
def test_naming_a_repo_the_folder_also_holds_is_one_entry(tmp_path, outright_first):
    """`- repo: org/alpha` beside the folder org/alpha is checked out in.

    The entry written for the repo itself is the deliberate one, so it decides
    the name and the forge either way round - but the folder still says where
    the checkout is, which the other entry never claimed to know.
    """
    path = make_checkout(tmp_path, "org", "alpha")
    outright = "  - repo: org/alpha\n"
    folder = f"  - folder: {tmp_path / 'org'}\n"

    fleet, paths, forges = repos.load(
        write_fleet(tmp_path, outright + folder if outright_first else folder + outright)
    )

    assert fleet == ("org/alpha",)
    assert paths == {"org/alpha": str(path)}
    assert forges == {"org/alpha": "github"}
