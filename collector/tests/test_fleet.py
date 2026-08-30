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
import importlib.util
import pathlib
import subprocess
import sys

import pytest

from jq_collector import localgit
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


def config(repos: tuple[str, ...], repo_root: pathlib.Path) -> Config:
    cfg = Config()
    object.__setattr__(cfg, "repos", repos)
    object.__setattr__(cfg, "repo_root", str(repo_root))
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


# -- the generator -----------------------------------------------------------


@pytest.fixture
def gen_repos():
    """scripts/gen-repos.py, loaded by path - it is a script, not a package."""
    pytest.importorskip("yaml")
    spec = importlib.util.spec_from_file_location(
        "gen_repos", REPO_ROOT / "scripts" / "gen-repos.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_generator_names_a_checkout_from_its_origin(gen_repos, tmp_path):
    path = make_checkout(tmp_path, "cvxgrp", "cvxsimulator")

    assert gen_repos.resolve({"path": str(path)}, 1) == ("cvxgrp/cvxsimulator", path)


def test_an_explicit_repo_overrides_the_origin(gen_repos, tmp_path):
    """For a fork you want the board to follow upstream, not your copy."""
    path = make_checkout(tmp_path, "me", "cvxpy")

    assert gen_repos.resolve({"path": str(path), "repo": "cvxpy/cvxpy"}, 1) == (
        "cvxpy/cvxpy",
        path,
    )


def test_an_entry_may_name_a_repo_with_no_checkout(gen_repos):
    assert gen_repos.resolve({"repo": "Jebel-Quant/actions"}, 1) == ("Jebel-Quant/actions", None)


def test_a_bare_string_entry_is_a_path(gen_repos, tmp_path):
    path = make_checkout(tmp_path, "Jebel-Quant", "rhiza")

    assert gen_repos.resolve(str(path), 1) == ("Jebel-Quant/rhiza", path)


@pytest.mark.parametrize(
    "entry",
    [
        {"repo": "no-slash"},  # not owner/name, and no path to derive it from
        {},  # neither
        {"path": "/definitely/not/here"},
    ],
)
def test_a_broken_entry_fails_loudly(gen_repos, entry):
    """Better a refusal at generate time than a board that is quietly short a repo."""
    with pytest.raises(SystemExit):
        gen_repos.resolve(entry, 1)


def test_a_directory_that_is_not_a_checkout_fails(gen_repos, tmp_path):
    (tmp_path / "empty").mkdir()
    with pytest.raises(SystemExit):
        gen_repos.resolve({"path": str(tmp_path / "empty")}, 1)


def test_env_mode_prints_the_list_and_writes_nothing(gen_repos, tmp_path, monkeypatch, capsys):
    """A server names its fleet in .env, and that line must come from repos.yml.

    Retyping it is how the two drift: a repo added here never reaches the board
    and nothing reports the difference. --env must also leave the compose
    override alone, so it is safe to run on a machine that has no stack.
    """
    path = make_checkout(tmp_path, "Jebel-Quant", "rhiza")
    source = tmp_path / "repos.yml"
    source.write_text(f"repos:\n  - path: {path}\n  - repo: cvxgrp/cvxsimulator\n")
    target = tmp_path / "docker-compose.repos.yml"
    monkeypatch.setattr(gen_repos, "SOURCE", source)
    monkeypatch.setattr(gen_repos, "TARGET", target)
    monkeypatch.setattr(sys, "argv", ["gen-repos.py", "--env"])

    gen_repos.main()

    assert capsys.readouterr().out.strip() == "JQ_REPOS=Jebel-Quant/rhiza,cvxgrp/cvxsimulator"
    assert not target.exists()


def test_an_unknown_flag_is_refused(gen_repos, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["gen-repos.py", "--all"])
    with pytest.raises(SystemExit):
        gen_repos.main()
