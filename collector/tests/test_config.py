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
