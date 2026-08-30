"""Config must expose every field the rest of the package reads.

`github.py` once referenced `cfg.public_only` while `Config` never defined it.
Nothing failed at import; every GitHub refresh raised AttributeError at runtime
while the collector kept serving, so the target stayed "up" and the board
silently lost two thirds of its panels.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from jq_collector.config import Config

PACKAGE = pathlib.Path(__file__).resolve().parent.parent / "jq_collector"


def test_every_cfg_attribute_referenced_in_the_package_exists():
    """Catches the whole class of bug, not just the one that bit us."""
    referenced: set[str] = set()
    for module in PACKAGE.glob("*.py"):
        tree = ast.parse(module.read_text())
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id == "cfg"
            ):
                referenced.add(node.attr)

    assert referenced, "found no cfg.* references - has the parameter been renamed?"
    missing = sorted(a for a in referenced if not hasattr(Config(), a))
    assert not missing, f"Config is missing attributes the package reads: {missing}"


def test_the_package_imports_cleanly():
    import jq_collector.__main__
    import jq_collector.github
    import jq_collector.localgit
    import jq_collector.metrics

    assert jq_collector.github and jq_collector.metrics
    assert jq_collector.localgit and jq_collector.__main__


@pytest.mark.parametrize(
    ("value", "expected"),
    [("true", True), ("TRUE", True), ("false", False), ("", False), (None, False)],
)
def test_public_only_reads_the_environment(monkeypatch, value, expected):
    if value is None:
        monkeypatch.delenv("JQ_PUBLIC_ONLY", raising=False)
    else:
        monkeypatch.setenv("JQ_PUBLIC_ONLY", value)
    # Config field defaults bind at import, so re-evaluate the expression.
    import importlib

    import jq_collector.config as mod

    importlib.reload(mod)
    assert mod.Config().public_only is expected


def test_empty_repo_root_disables_local_scanning(tmp_path):
    """On a server there are no clones; that must be a clean no-op, not an error
    logged every minute."""
    from jq_collector import localgit
    from jq_collector.config import Config

    cfg = Config()
    object.__setattr__(cfg, "repo_root", "")
    assert localgit.scan(cfg, {}) == {}


@pytest.fixture
def reloaded_config(monkeypatch):
    """Build a Config with a given environment.

    Most field defaults bind at *import*, not per instance - only the three
    using `default_factory` re-read the environment when Config() is called. So
    varying one of the others means re-importing the module, which is what
    `test_public_only_reads_the_environment` does by hand.

    Teardown restores it. A reload leaves the class holding whatever the
    environment said at that moment, so without this a later test reading a
    default would see this test's value instead - including after a reload that
    raised part-way through.
    """
    import importlib

    import jq_collector.config as mod

    def _load(**env):
        for key, value in env.items():
            if value is None:
                monkeypatch.delenv(key, raising=False)
            else:
                monkeypatch.setenv(key, value)
        importlib.reload(mod)
        return mod.Config()

    yield _load

    monkeypatch.undo()
    importlib.reload(mod)


def test_an_interval_is_read_from_the_environment(reloaded_config):
    assert reloaded_config(JQ_GITHUB_INTERVAL="900").github_interval == 900


@pytest.mark.parametrize("raw", ["", "   "])
def test_a_blank_interval_falls_back_to_the_default(reloaded_config, raw):
    """Compose writes `JQ_X: ${JQ_X:-}` for an unset variable, so blank and
    unset both reach here and must mean the same thing."""
    assert reloaded_config(JQ_LOCAL_INTERVAL=raw).local_interval == 60


def test_a_non_numeric_interval_refuses_to_start(reloaded_config):
    """Louder than silently running at the default: a cadence someone meant to
    change and mistyped would otherwise be invisible."""
    with pytest.raises(ValueError):
        reloaded_config(JQ_GITHUB_INTERVAL="ten minutes")


def test_a_blank_csv_is_an_empty_fleet_not_a_one_repo_fleet(monkeypatch):
    """`"".split(",")` is `[""]`, which would put a nameless repo on the board."""
    monkeypatch.setenv("JQ_REPOS", " , ,")

    assert Config().repos == ()


def test_csv_entries_are_stripped(monkeypatch):
    monkeypatch.setenv("JQ_IGNORE", " a/b , c/d ")

    assert Config().ignore == ("a/b", "c/d")


@pytest.mark.parametrize(
    ("owner", "name", "ignored"),
    [
        ("o", "bare", True),
        ("o", "full", True),
        ("other", "bare", True),
        ("other", "full", False),
    ],
)
def test_is_ignored_accepts_a_bare_name_or_owner_slash_name(monkeypatch, owner, name, ignored):
    """Both forms are documented, and a bare name matches in any owner."""
    monkeypatch.setenv("JQ_IGNORE", "bare,o/full")

    assert Config().is_ignored(owner, name) is ignored
