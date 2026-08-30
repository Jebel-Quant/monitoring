"""Shared fixtures. Nothing here touches the network."""

from __future__ import annotations

import pytest

from jq_collector.config import Config
from jq_collector.github import GitHub


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


@pytest.fixture
def cfg() -> Config:
    return Config()


@pytest.fixture
def make_client(cfg):
    def _make(responses: dict[str, object], blobs: dict[str, bytes] | None = None) -> FakeGitHub:
        return FakeGitHub(cfg, responses, blobs)

    return _make


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
