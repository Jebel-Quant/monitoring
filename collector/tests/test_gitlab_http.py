"""The real HTTP layer of the GitLab client, driven through a mock transport.

test_gitlab.py fakes the client at `_json`/`_text`, which is right for testing
what the collector *derives* from a payload but means the layer producing those
payloads - the auth header, status handling, pagination, URL encoding - never
runs. A MockTransport keeps the client, the request building and the response
handling real, and only replaces the socket.
"""

from __future__ import annotations

import httpx
import pytest

from jq_collector.config import Config
from jq_collector.gitlab import GitLab


def client(handler, **cfg_overrides) -> GitLab:
    """A GitLab whose transport is `handler`, everything else real."""
    cfg = Config()
    for key, value in cfg_overrides.items():
        object.__setattr__(cfg, key, value)
    api = GitLab(cfg)
    api._client = httpx.Client(
        base_url=cfg.gitlab_api,
        headers=dict(api._client.headers),
        transport=httpx.MockTransport(handler),
        follow_redirects=True,
    )
    return api


def route(table: dict[str, httpx.Response]):
    """Dispatch on the raw path, ignoring query parameters.

    Raw, not `url.path`: a project id is a percent-encoded path, and `url.path`
    decodes it back to slashes - which is exactly the collapse these tests exist
    to catch. Keys therefore carry the `/api/v4` prefix and the `%2F` intact.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.raw_path.decode().split("?", 1)[0]
        response = table.get(path)
        if response is None:  # pragma: no cover - a test asked for an unstubbed path
            raise AssertionError(f"unstubbed path {path}")
        return httpx.Response(
            response.status_code, content=response.content, headers=response.headers
        )

    return handler


def ok(payload) -> httpx.Response:
    return httpx.Response(200, json=payload)


# -- the token header -------------------------------------------------------
def test_a_token_is_sent_as_a_private_token_header():
    """PRIVATE-TOKEN, not Bearer: GITLAB_TOKEN holds a personal or project
    access token, and Bearer would be an OAuth one."""
    api = GitLab(_cfg(gitlab_token="sekrit"))

    assert api._client.headers["PRIVATE-TOKEN"] == "sekrit"
    assert "Authorization" not in api._client.headers


def test_no_token_sends_no_auth_header():
    """Public projects are readable unauthenticated, so an empty token must
    produce a valid anonymous request rather than an empty header."""
    api = GitLab(_cfg(gitlab_token=""))

    assert "PRIVATE-TOKEN" not in api._client.headers


def _cfg(**overrides) -> Config:
    cfg = Config()
    for key, value in overrides.items():
        object.__setattr__(cfg, key, value)
    return cfg


# -- the statuses a real fleet produces -------------------------------------
@pytest.mark.parametrize("status", [401, 403, 404])
def test_the_expected_empties_are_none_not_exceptions(status, caplog):
    """A project the token cannot see, a feature the tier excludes, a file that
    is not there. All states a real fleet contains; failing the refresh over one
    would blank the whole board."""
    api = client(route({"/api/v4/projects/o%2Fr": httpx.Response(status)}))

    with caplog.at_level("INFO"):
        assert api._json("/projects/o%2Fr") is None

    # Logged, not silent: a repo vanishing from the board because its token lost
    # a scope must leave a trace somebody can find.
    assert f"-> {status}" in caplog.text


@pytest.mark.parametrize("status", [500, 502, 429])
def test_a_server_error_is_raised_so_the_refresh_is_recorded_as_failed(status):
    """Unlike the empties, these are not a state of the fleet - they are the API
    being broken, and must show up as a failed refresh rather than as a repo
    that quietly vanished."""
    api = client(route({"/api/v4/projects/o%2Fr": httpx.Response(status)}))

    with pytest.raises(httpx.HTTPStatusError):
        api._json("/projects/o%2Fr")


# -- URL encoding, end to end ----------------------------------------------
def test_a_nested_namespace_reaches_the_wire_encoded():
    """The encoded path must survive httpx's own URL handling: an unescaped
    slash here would address a different endpoint entirely."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.raw_path.decode())
        return httpx.Response(200, json={"path_with_namespace": "acme/platform/infra/web"})

    api = client(handler)
    api.list_projects(("acme/platform/infra/web",))

    assert seen == ["/api/v4/projects/acme%2Fplatform%2Finfra%2Fweb"]


def test_a_branch_name_with_a_slash_is_encoded():
    """`release/2.0` is a legal branch name and a broken URL path."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.raw_path.decode())
        return httpx.Response(200, json={"commit": {"id": "abc123"}})

    api = client(handler)
    assert api.branch_sha("o/r", "release/2.0") == "abc123"
    assert seen == ["/api/v4/projects/o%2Fr/repository/branches/release%2F2.0"]


# -- pagination -------------------------------------------------------------
def test_a_full_page_is_followed_and_a_short_one_ends_it():
    pages: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params.get("page", 1))
        pages.append(page)
        if page == 1:
            return httpx.Response(200, json=[{"iid": n} for n in range(100)])
        return httpx.Response(200, json=[{"iid": 100}])

    api = client(handler)
    items = api._paginate("/projects/o%2Fr/merge_requests")

    assert pages == [1, 2]
    assert len(items) == 101


def test_pagination_stops_at_a_sane_ceiling():
    """A runaway feed must not spend the whole refresh. Ten pages of 100 is far
    beyond anything a monitored repo produces."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{"iid": n} for n in range(100)])

    api = client(handler)
    items = api._paginate("/projects/o%2Fr/merge_requests")

    assert len(items) == 1000


def test_an_empty_first_page_is_no_items_not_an_error():
    api = client(route({"/api/v4/projects/o%2Fr/merge_requests": ok([])}))

    assert api._paginate("/projects/o%2Fr/merge_requests") == []


# -- the raw-file route, which is not JSON ---------------------------------
def test_the_pointer_is_read_as_text_not_parsed_as_json():
    """`repository/files/.../raw` answers with the file itself. Parsing that as
    JSON would fail on any ordinary YAML pointer."""
    api = client(
        route(
            {
                "/api/v4/projects/o%2Fr/repository/files/.rhiza%2Ftemplate.yml/raw": httpx.Response(
                    200, content=b"ref: v1.7.1\nrepo: Jebel-Quant/rhiza\n"
                )
            }
        )
    )

    assert api.template_ref("o/r", "main") == "v1.7.1"


def test_a_missing_pointer_is_an_unmanaged_repo():
    api = client(
        route(
            {
                "/api/v4/projects/o%2Fr/repository/files/.rhiza%2Ftemplate.yml/raw": httpx.Response(
                    404
                )
            }
        )
    )

    assert api.template_ref("o/r", "main") == ""


# -- the pipeline pair ------------------------------------------------------
def test_the_newest_pipeline_is_fetched_in_full_for_its_coverage():
    """The listing carries a status but not coverage, so the one worth having is
    fetched in full - two calls, and only for the newest."""
    api = client(
        route(
            {
                "/api/v4/projects/o%2Fr/pipelines": ok([{"id": 77, "status": "success"}]),
                "/api/v4/projects/o%2Fr/pipelines/77": ok(
                    {"id": 77, "status": "success", "coverage": "91.2"}
                ),
            }
        )
    )

    pipeline = api.latest_pipeline("o/r", "main")

    assert pipeline["coverage"] == "91.2"


def test_a_repo_that_has_never_run_a_pipeline_is_none():
    api = client(route({"/api/v4/projects/o%2Fr/pipelines": ok([])}))

    assert api.latest_pipeline("o/r", "main") is None


def test_the_listing_stands_in_when_the_full_fetch_fails():
    """Losing coverage is worth it to keep the status; dropping the pipeline
    entirely would report a repo with CI as having none."""
    api = client(
        route(
            {
                "/api/v4/projects/o%2Fr/pipelines": ok([{"id": 77, "status": "failed"}]),
                "/api/v4/projects/o%2Fr/pipelines/77": httpx.Response(404),
            }
        )
    )

    pipeline = api.latest_pipeline("o/r", "main")

    assert pipeline["status"] == "failed"


def test_a_protected_branch_is_read_and_an_unprotected_one_is_none():
    api = client(
        route(
            {
                "/api/v4/projects/o%2Fr/protected_branches/main": ok(
                    {"name": "main", "allow_force_push": False}
                ),
                "/api/v4/projects/o%2Fr/protected_branches/dev": httpx.Response(404),
            }
        )
    )

    assert api.protected_branch("o/r", "main")["allow_force_push"] is False
    assert api.protected_branch("o/r", "dev") is None
