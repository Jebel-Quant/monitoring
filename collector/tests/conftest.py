"""Shared fixtures. Nothing here touches the network."""

from __future__ import annotations

import pytest

from jq_collector.config import Config
from jq_collector.github import GitHub
from jq_collector.gitlab import GitLab


class FakeGitHub(GitHub):
    """A GitHub client whose HTTP layer is a dict of canned responses.

    Keyed by the request path; params are ignored except where a test needs
    them, which keeps the fixtures readable.
    """

    def __init__(
        self,
        cfg: Config,
        responses: dict[str, object],
        blobs: dict[str, bytes] | None = None,
    ) -> None:
        super().__init__(cfg)
        self.responses = responses
        # Raw-bytes routes, for the endpoints that return an archive rather
        # than JSON. Kept apart so a test that does not care never has to
        # mention them.
        self.blobs = blobs or {}
        self.calls: list[str] = []

    def _json(self, path: str, **params: object) -> object | None:
        self.calls.append(path)
        return self.responses.get(path)

    def _bytes(self, path: str) -> bytes | None:
        self.calls.append(path)
        return self.blobs.get(path)


class FakeGitLab(GitLab):
    """A GitLab client whose HTTP layer is a dict of canned responses.

    The GitHub twin above is faked at ``_json``; this one has to fake ``_text``
    as well, because the template pointer is served by a raw-file route that
    answers with YAML rather than JSON.
    """

    def __init__(
        self,
        cfg: Config,
        responses: dict[str, object],
        texts: dict[str, str] | None = None,
    ) -> None:
        super().__init__(cfg)
        self.responses = responses
        self.texts = texts or {}
        self.calls: list[str] = []

    def _json(self, path: str, **params: object) -> object | None:
        self.calls.append(path)
        return self.responses.get(path)

    def _text(self, path: str, **params: object) -> str | None:
        self.calls.append(path)
        return self.texts.get(path)


@pytest.fixture
def cfg() -> Config:
    return Config()


@pytest.fixture
def make_client(cfg):
    def _make(responses: dict[str, object], blobs: dict[str, bytes] | None = None) -> FakeGitHub:
        return FakeGitHub(cfg, responses, blobs)

    return _make


@pytest.fixture
def make_gitlab(cfg):
    def _make(responses: dict[str, object], texts: dict[str, str] | None = None) -> FakeGitLab:
        return FakeGitLab(cfg, responses, texts)

    return _make


def job(name: str, status: str, finished: str | None = None, duration: float = 1.0):
    """A minimal GitLab pipeline-job payload."""
    return {
        "name": name,
        "status": status,
        "finished_at": finished or "2026-08-31T06:05:36.000Z",
        "duration": duration,
        "web_url": f"https://gitlab.com/o/r/-/jobs/{name}",
    }


def project(full_name: str = "o/r", **overrides):
    """A minimal GitLab project payload."""
    namespace, _, name = full_name.rpartition("/")
    base = {
        "path_with_namespace": full_name,
        "path": name,
        "name": name,
        "default_branch": "main",
        "visibility": "public",
        "archived": False,
        "open_issues_count": 0,
        "web_url": f"https://gitlab.com/{full_name}",
        "last_activity_at": "2026-08-31T06:05:36.000Z",
        "namespace": {"full_path": namespace},
    }
    base.update(overrides)
    return base


def run(workflow_id: int, name: str, conclusion: str, updated: str, started: str | None = None):
    """A minimal workflow_run payload."""
    return {
        "workflow_id": workflow_id,
        "name": name,
        "conclusion": conclusion,
        "updated_at": updated,
        "run_started_at": started or updated,
        "html_url": f"https://github.com/x/y/actions/runs/{workflow_id}",
    }


def workflow(wid: int, name: str, state: str = "active", path: str | None = None):
    return {
        "id": wid,
        "name": name,
        "state": state,
        "path": path or f".github/workflows/{name}.yml",
    }
