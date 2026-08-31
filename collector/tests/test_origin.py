"""The origin-URL grammar, which decides both a repo's key and its forge.

This used to be two copies of one parser, tested only through a checkout on
disk. It is one pure function now, so the shapes can be driven directly - which
matters because getting it wrong is not a crash but a wrong answer: a repo keyed
under a name that does not exist, or read through the wrong API.
"""

from __future__ import annotations

import subprocess

import pytest

from jq_collector import origin


# -- the URL shapes a remote is written in ----------------------------------
@pytest.mark.parametrize(
    ("url", "host", "namespace", "name"),
    [
        # scp-like, with and without the .git suffix.
        ("git@github.com:o/r.git", "github.com", "o", "r"),
        ("git@github.com:o/r", "github.com", "o", "r"),
        # https, and ssh:// which is the same grammar with a user in it.
        ("https://github.com/o/r.git", "github.com", "o", "r"),
        ("ssh://git@github.com/o/r", "github.com", "o", "r"),
        # A port must not be mistaken for part of the host.
        ("ssh://git@gitlab.com:2222/o/r", "gitlab.com", "o", "r"),
        # GitLab namespaces nest, and the whole path is the namespace. Keeping
        # the last two segments here would key this repo as `infra/web` and ask
        # the API for a project that does not exist.
        (
            "https://gitlab.com/acme/platform/infra/web.git",
            "gitlab.com",
            "acme/platform/infra",
            "web",
        ),
        ("git@gitlab.com:acme/platform/infra/web", "gitlab.com", "acme/platform/infra", "web"),
        # Case is not significant in a hostname, and the forge lookup depends on
        # it matching.
        ("https://GitLab.com/o/r", "gitlab.com", "o", "r"),
        # A path remote - a clone of a clone. No host, so the filesystem trail
        # is not a namespace and only its tail is meaningful.
        ("/srv/mirrors/o/r", "", "o", "r"),
        ("relative/o/r", "", "o", "r"),
        ("/deeply/nested/mirror/tree/o/r", "", "o", "r"),
        # Whitespace, because this comes off `git remote get-url` stdout.
        ("  https://github.com/o/r\n", "github.com", "o", "r"),
    ],
)
def test_every_remote_shape_is_split_into_host_namespace_and_name(url, host, namespace, name):
    parsed = origin.parse(url)

    assert parsed is not None
    assert (parsed.host, parsed.namespace, parsed.name) == (host, namespace, name)
    assert parsed.full_name == f"{namespace}/{name}"


@pytest.mark.parametrize("url", ["", "   ", "https://example.com/lonely", "lonely", "git@host:x"])
def test_a_url_that_cannot_name_a_repo_is_none(url):
    """One path segment cannot be split into a namespace and a name."""
    assert origin.parse(url) is None


# -- which forge a host is --------------------------------------------------
@pytest.mark.parametrize(
    ("host", "forge"),
    [
        ("gitlab.com", "gitlab"),
        ("GITLAB.COM", "gitlab"),
        # A self-hosted GitLab conventionally lives on a gitlab.* hostname.
        ("gitlab.internal.acme.com", "gitlab"),
        ("github.com", "github"),
        # Anything unrecognised stays GitHub, which is what keeps a GitHub
        # Enterprise host and every path remote behaving as they did before
        # this module existed.
        ("github.acme.com", "github"),
        ("git.example.com", "github"),
        ("", "github"),
    ],
)
def test_the_forge_is_inferred_from_the_host(host, forge):
    assert origin.forge_for_host(host) == forge


def test_the_parsed_origin_carries_its_own_forge():
    """The whole point of keeping the host: inference costs nothing here."""
    assert origin.parse("git@gitlab.com:acme/web").forge == "gitlab"
    assert origin.parse("git@github.com:acme/web").forge == "github"
    # A path remote has no host to infer from, so it falls to the default.
    assert origin.parse("/srv/mirrors/acme/web").forge == "github"


# -- reading it off a checkout ----------------------------------------------
def test_a_directory_that_is_not_a_repo_is_none(tmp_path):
    assert origin.read(str(tmp_path)) is None


def test_a_git_binary_that_cannot_be_run_is_a_none_not_a_crash(monkeypatch, caplog):
    """OSError, e.g. git missing from PATH entirely inside a stripped image."""

    def explode(*_args, **_kwargs):
        raise OSError("no git here")

    monkeypatch.setattr(subprocess, "run", explode)

    assert origin.read("/anywhere") is None
    assert "failed" in caplog.text


def test_a_git_call_that_times_out_is_a_none_not_a_crash(monkeypatch, caplog):
    """A repo on a stalled network mount can hang; the monitor must not."""

    def stall(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(cmd="git", timeout=20)

    monkeypatch.setattr(subprocess, "run", stall)

    assert origin.read("/anywhere") is None
    assert "failed" in caplog.text
