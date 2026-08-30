"""The real HTTP layer, driven through a mock transport.

Everywhere else the GitHub client is faked at `_json`, which is right for
testing what the collector *derives* from a payload but means the layer that
produces those payloads - status handling, pagination, rate-limit bookkeeping -
never runs. That layer is where a fleet's odder states live: a repo whose
protection endpoint 404s for two different reasons, an artifact GitHub has
already deleted, a paginated feed longer than anyone expected.

A MockTransport keeps the client, the request building and the response
handling real, and only replaces the socket.
"""

from __future__ import annotations

import base64
import logging

import httpx
import pytest
from test_coverage import StubAPI

from jq_collector.config import Config
from jq_collector.github import GitHub, _behind_count, _ts


def client(handler, **cfg_overrides) -> GitHub:
    """A GitHub whose transport is `handler`, everything else real."""
    cfg = Config()
    for key, value in cfg_overrides.items():
        object.__setattr__(cfg, key, value)
    api = GitHub(cfg)
    api._client = httpx.Client(
        base_url=cfg.api,
        transport=httpx.MockTransport(handler),
        follow_redirects=True,
    )
    return api


def route(table: dict[str, httpx.Response]):
    """Dispatch on path, ignoring query parameters."""

    def handler(request: httpx.Request) -> httpx.Response:
        response = table.get(request.url.path)
        if response is None:  # pragma: no cover - a test asked for a path it did not stub
            raise AssertionError(f"unstubbed path {request.url.path}")
        return httpx.Response(
            response.status_code, content=response.content, headers=response.headers
        )

    return handler


def ok(payload) -> httpx.Response:
    return httpx.Response(200, json=payload)


# -- the token header -------------------------------------------------------


def _cfg_with_token(token: str) -> Config:
    cfg = Config()
    object.__setattr__(cfg, "token", token)
    return cfg


def test_a_token_is_sent_as_a_bearer_header():
    api = GitHub(_cfg_with_token("sekrit"))

    assert api._client.headers["Authorization"] == "Bearer sekrit"
    api.close()


def test_no_token_means_no_authorization_header():
    api = GitHub(_cfg_with_token(""))

    assert "Authorization" not in api._client.headers
    api.close()


# -- rate-limit bookkeeping -------------------------------------------------


def test_rate_limit_headers_are_recorded():
    api = client(
        lambda _r: httpx.Response(
            200,
            json={},
            headers={
                "x-ratelimit-remaining": "4321",
                "x-ratelimit-limit": "5000",
                "x-ratelimit-reset": "1800000000",
            },
        )
    )

    api._json("/anything")

    assert (api.rate_remaining, api.rate_limit, api.rate_reset) == (4321.0, 5000.0, 1800000000.0)


def test_a_malformed_rate_limit_header_is_ignored_not_fatal():
    """GitHub has been known to send an empty header value behind a proxy."""
    api = client(
        lambda _r: httpx.Response(200, json={}, headers={"x-ratelimit-remaining": "not-a-number"})
    )
    before = api.rate_remaining

    api._json("/anything")

    assert api.rate_remaining == before


def test_absent_rate_limit_headers_leave_the_previous_reading():
    api = client(lambda _r: httpx.Response(200, json={}))

    api._json("/anything")

    assert api.rate_remaining == -1.0


# -- _json / _bytes status handling ----------------------------------------


@pytest.mark.parametrize("status", [403, 404, 409, 451])
def test_the_expected_empties_are_none_not_exceptions(status, caplog):
    """Each is a state the fleet legitimately contains - a private repo, a
    missing pointer, an empty repository, a blocked one."""
    caplog.set_level(logging.INFO, logger="jq_collector.github")
    api = client(lambda _r: httpx.Response(status, json={"message": "nope"}))

    assert api._json("/repos/o/r") is None
    assert f"-> {status}" in caplog.text


def test_an_unexpected_status_fails_the_refresh():
    """A 500 is not a fleet state; swallowing it would publish a snapshot that
    silently lost a repo, which Prometheus then records as fact."""
    api = client(lambda _r: httpx.Response(500, text="boom"))

    with pytest.raises(httpx.HTTPStatusError):
        api._json("/repos/o/r")


@pytest.mark.parametrize("status", [403, 404, 409, 410, 451])
def test_bytes_adds_410_to_the_expected_empties(status):
    """410 Gone is what an expired artifact returns, and artifacts expire on a
    schedule nobody here controls."""
    api = client(lambda _r: httpx.Response(status, text=""))

    assert api._bytes("/artifact/zip") is None


def test_bytes_returns_the_body_on_success():
    api = client(lambda _r: httpx.Response(200, content=b"PK\x03\x04zip"))

    assert api._bytes("/artifact/zip") == b"PK\x03\x04zip"


def test_bytes_raises_on_an_unexpected_status():
    api = client(lambda _r: httpx.Response(500, text="boom"))

    with pytest.raises(httpx.HTTPStatusError):
        api._bytes("/artifact/zip")


# -- pagination -------------------------------------------------------------


def test_a_short_page_ends_the_walk():
    calls: list[int] = []

    def handler(request):
        calls.append(int(request.url.params["page"]))
        return ok([{"n": 1}])

    assert len(client(handler)._paginate("/x")) == 1
    assert calls == [1], "a page shorter than the limit means there is no next page"


def test_a_full_page_is_followed_by_the_next():
    def handler(request):
        page = int(request.url.params["page"])
        return ok([{"n": i} for i in range(100)] if page == 1 else [{"n": 100}])

    assert len(client(handler)._paginate("/x")) == 101


def test_pagination_stops_at_the_page_cap():
    """A runaway feed must not walk forever; ten pages is far past this fleet."""
    pages: list[int] = []

    def handler(request):
        pages.append(int(request.url.params["page"]))
        return ok([{"n": i} for i in range(100)])

    items = client(handler)._paginate("/x")

    assert max(pages) == 10
    assert len(items) == 1000


def test_a_non_list_payload_ends_the_walk():
    """404 becomes None, and a dict is not a feed - neither is a page of items."""
    assert client(lambda _r: httpx.Response(404, json={}))._paginate("/x") == []
    assert client(lambda _r: ok({"not": "a list"}))._paginate("/x") == []


# -- branch protection: two different 404s ---------------------------------


def test_a_protected_branch_returns_its_settings():
    api = client(route({"/repos/o/r/branches/main/protection": ok({"enabled": True})}))

    protection, known = api.branch_protection("o/r", "main")

    assert protection == {"enabled": True} and known is True


def test_an_unprotected_branch_is_a_fact_not_a_gap():
    """GitHub says "Branch not protected" - that is an answer, so `known` is True
    and the board can report the repo as unprotected."""
    api = client(
        route(
            {
                "/repos/o/r/branches/main/protection": httpx.Response(
                    404, json={"message": "Branch not protected"}
                )
            }
        )
    )

    protection, known = api.branch_protection("o/r", "main")

    assert protection is None and known is True


def test_a_token_without_admin_cannot_see_and_says_so(caplog):
    """The same 404, a different body. Reporting this as "unprotected" would
    invent a finding, so `known` is False and the board shows unknown."""
    caplog.set_level(logging.INFO, logger="jq_collector.github")
    api = client(
        route(
            {
                "/repos/o/r/branches/main/protection": httpx.Response(
                    404, json={"message": "Not Found"}
                )
            }
        )
    )

    protection, known = api.branch_protection("o/r", "main")

    assert protection is None and known is False
    assert "protection unreadable" in caplog.text


def test_a_404_with_an_unreadable_body_is_unknown(caplog):
    caplog.set_level(logging.INFO, logger="jq_collector.github")
    api = client(
        route({"/repos/o/r/branches/main/protection": httpx.Response(404, text="<html>nope")})
    )

    assert api.branch_protection("o/r", "main") == (None, False)
    assert "protection unreadable" in caplog.text


def test_any_other_status_is_unknown():
    api = client(route({"/repos/o/r/branches/main/protection": httpx.Response(500, text="")}))

    assert api.branch_protection("o/r", "main") == (None, False)


# -- dependabot -------------------------------------------------------------


def test_alerts_are_counted_by_severity():
    api = client(
        route(
            {
                "/repos/o/r/dependabot/alerts": ok(
                    [
                        {"security_advisory": {"severity": "HIGH"}},
                        {"security_advisory": {"severity": "high"}},
                        {"security_advisory": {}},
                        {},
                    ]
                )
            }
        )
    )

    assert api.open_alerts("o/r") == {"high": 2, "unknown": 2}


def test_alerts_disabled_is_none_not_an_empty_count():
    """404 covers both "disabled" and "none open"; only None can mean the first,
    and rendering them alike would show a green tile for an unwatched repo."""
    api = client(route({"/repos/o/r/dependabot/alerts": httpx.Response(404, json={})}))

    assert api.open_alerts("o/r") is None


def test_no_open_alerts_is_an_empty_count():
    api = client(route({"/repos/o/r/dependabot/alerts": ok([])}))

    assert api.open_alerts("o/r") == {}


# -- releases, branch sha, template pointer --------------------------------


def test_draft_releases_are_not_tags():
    api = client(
        route(
            {
                "/repos/o/r/releases": ok(
                    [
                        {"tag_name": "v2", "draft": False},
                        {"tag_name": "v1.9", "draft": True},
                        {"draft": False},
                    ]
                )
            }
        )
    )

    assert api.release_tags("o/r") == ["v2"]


def test_a_branch_sha_is_read_from_the_commit():
    api = client(route({"/repos/o/r/branches/main": ok({"commit": {"sha": "abc"}})}))

    assert api.branch_sha("o/r", "main") == "abc"


@pytest.mark.parametrize(
    "payload",
    [httpx.Response(404, json={}), ok({}), ok({"commit": {}}), ok([])],
    ids=["missing", "no-commit", "no-sha", "not-a-dict"],
)
def test_an_unreadable_branch_has_no_sha(payload):
    api = client(route({"/repos/o/r/branches/main": payload}))

    assert api.branch_sha("o/r", "main") == ""


def contents(body: str) -> httpx.Response:
    return ok({"content": base64.b64encode(body.encode()).decode()})


def test_the_template_ref_is_decoded_from_the_contents_api():
    api = client(route({"/repos/o/r/contents/.rhiza/template.yml": contents("ref: v1.7.1\n")}))

    assert api.template_ref("o/r") == "v1.7.1"


@pytest.mark.parametrize(
    "payload",
    [httpx.Response(404, json={}), ok({}), ok([]), contents("ref:\n"), contents("- a\n")],
    ids=["missing", "no-content", "not-a-dict", "null-ref", "not-a-mapping"],
)
def test_a_pointer_without_a_ref_yields_nothing(payload):
    api = client(route({"/repos/o/r/contents/.rhiza/template.yml": payload}))

    assert api.template_ref("o/r") == ""


def test_a_malformed_pointer_is_data_not_a_crash(caplog):
    api = client(
        route({"/repos/o/r/contents/.rhiza/template.yml": ok({"content": "not base64!!"})})
    )

    assert api.template_ref("o/r") == ""
    assert "could not parse template pointer" in caplog.text


# -- pull requests ----------------------------------------------------------


def pr(number: int, sha: str = "s", **extra) -> dict:
    return {
        "number": number,
        "title": "t",
        "user": {"login": "u"},
        "draft": False,
        "created_at": "2026-08-01T00:00:00Z",
        "updated_at": "2026-08-02T00:00:00Z",
        "head": {"sha": sha},
        "html_url": "u",
        **extra,
    }


def test_open_pulls_reports_the_total_and_the_clipped_detail():
    """The total is what the tile shows, so it must survive the clipping that
    keeps a PR flood from exhausting the API budget."""
    api = client(
        route(
            {
                "/repos/o/r/pulls": ok([pr(n) for n in range(5)]),
                "/repos/o/r/commits/s/check-runs": ok({"check_runs": []}),
            }
        ),
        max_prs_per_repo=2,
    )

    total, pulls = api.open_pulls("o/r")

    assert total == 5
    assert [p.number for p in pulls] == [0, 1]


def test_a_pull_request_with_no_head_sha_has_no_checks():
    """Nothing to ask about, and asking with an empty sha would 404 per PR."""
    api = client(route({"/repos/o/r/pulls": ok([pr(1, sha="")])}))

    _total, pulls = api.open_pulls("o/r")

    assert pulls[0].checks == "none"


def test_pull_request_fields_are_defaulted_rather_than_missing():
    api = client(
        route(
            {
                "/repos/o/r/pulls": ok([{"head": {"sha": "s"}}]),
                "/repos/o/r/commits/s/check-runs": ok({"check_runs": []}),
            }
        )
    )

    _total, pulls = api.open_pulls("o/r")

    assert (pulls[0].number, pulls[0].author, pulls[0].title) == (0, "unknown", "")


def test_a_very_long_title_is_truncated():
    api = client(
        route(
            {
                "/repos/o/r/pulls": ok([pr(1, title="x" * 500)]),
                "/repos/o/r/commits/s/check-runs": ok({"check_runs": []}),
            }
        )
    )

    _total, pulls = api.open_pulls("o/r")

    assert len(pulls[0].title) == 120


# -- check runs -------------------------------------------------------------


@pytest.mark.parametrize(
    ("runs", "expected"),
    [
        ([], "none"),
        ([{"status": "in_progress"}], "pending"),
        ([{"status": "completed", "conclusion": "success"}], "success"),
        ([{"status": "completed", "conclusion": "failure"}], "failure"),
        ([{"status": "completed", "conclusion": "timed_out"}], "failure"),
        ([{"status": "completed", "conclusion": "action_required"}], "failure"),
        ([{"status": "completed", "conclusion": "cancelled"}], "cancelled"),
        ([{"status": "completed", "conclusion": "skipped"}], "success"),
        # One still running outranks anything already concluded.
        (
            [{"status": "completed", "conclusion": "failure"}, {"status": "queued"}],
            "pending",
        ),
        # Failure outranks a cancellation.
        (
            [
                {"status": "completed", "conclusion": "cancelled"},
                {"status": "completed", "conclusion": "failure"},
            ],
            "failure",
        ),
    ],
)
def test_check_runs_roll_up_to_one_word(runs, expected):
    api = client(route({"/repos/o/r/commits/s/check-runs": ok({"check_runs": runs})}))

    assert api.checks_state("o/r", "s") == expected


def test_an_unreadable_check_feed_is_unknown():
    """Distinct from "none": nobody has told us, rather than nothing ran."""
    api = client(route({"/repos/o/r/commits/s/check-runs": httpx.Response(404, json={})}))

    assert api.checks_state("o/r", "s") == "unknown"


# -- merged pull requests --------------------------------------------------


def test_closed_but_unmerged_pull_requests_are_dropped():
    """Abandoned is not landed, and the board's list is of things that shipped."""
    api = client(
        route(
            {
                "/repos/o/r/pulls": ok(
                    [
                        {"number": 1, "merged_at": "2026-08-01T00:00:00Z", "user": {"login": "u"}},
                        {"number": 2, "merged_at": None, "user": {"login": "u"}},
                    ]
                )
            }
        )
    )

    merged = api.recent_merges("o/r", 10)

    assert [m.number for m in merged] == [1]


def test_merges_are_sorted_by_merge_time_not_update_time():
    """GitHub sorts by update time, which is usually but not always the same."""
    api = client(
        route(
            {
                "/repos/o/r/pulls": ok(
                    [
                        {"number": 1, "merged_at": "2026-08-01T00:00:00Z"},
                        {"number": 2, "merged_at": "2026-08-03T00:00:00Z"},
                        {"number": 3, "merged_at": "2026-08-02T00:00:00Z"},
                    ]
                )
            }
        )
    )

    assert [m.number for m in api.recent_merges("o/r", 10)] == [2, 3, 1]


def test_merges_are_limited():
    api = client(
        route(
            {
                "/repos/o/r/pulls": ok(
                    [{"number": n, "merged_at": f"2026-08-{n:02d}T00:00:00Z"} for n in range(1, 6)]
                )
            }
        )
    )

    assert len(api.recent_merges("o/r", 2)) == 2


def test_an_unreadable_closed_feed_yields_no_merges():
    api = client(route({"/repos/o/r/pulls": httpx.Response(404, json={})}))

    assert api.recent_merges("o/r", 10) == []


# -- small helpers ---------------------------------------------------------


@pytest.mark.parametrize("raw", [None, "", "not a date", "2026-13-45T99:99:99Z"])
def test_an_unparseable_timestamp_is_zero(raw):
    """Zero sorts to the end rather than raising mid-refresh."""
    assert _ts(raw) == 0.0


def test_a_real_timestamp_is_parsed():
    assert _ts("2026-08-01T00:00:00+00:00") > 0


@pytest.mark.parametrize(
    ("tags", "ref", "expected"),
    [
        (["v3", "v2", "v1"], "v1", 2),
        (["v3", "v2", "v1"], "v3", 0),
        (["v3", "v2", "v1"], "main", None),
        (["v3"], "", None),
        ([], "v1", None),
    ],
)
def test_releases_behind_is_none_when_the_ref_is_not_a_release(tags, ref, expected):
    """A branch or a sha is not "up to date" and not "behind" - it is unknown,
    and the board says so rather than implying either."""
    assert _behind_count(tags, ref) == expected


def test_close_releases_the_client():
    api = client(lambda _r: ok({}))

    api.close()

    assert api._client.is_closed


# -- the fleet walk and its failure isolation ------------------------------


def test_a_duplicate_or_malformed_fleet_entry_is_skipped():
    """JQ_REPOS is a list someone edits; the same repo twice, or a bare name,
    must not cost a call or produce a second set of series."""
    asked: list[str] = []

    def handler(request):
        asked.append(request.url.path)
        return ok({"full_name": "o/r", "name": "r"})

    api = client(handler, repos=("o/r", "o/r", "bare"))

    repos = api.list_repos()

    assert [r["full_name"] for r in repos] == ["o/r"]
    assert asked == ["/repos/o/r"], "a duplicate or bare entry still cost a request"


def test_an_oversized_coverage_artifact_is_refused(caplog):
    """A guard on an archive we did not build. Coverage reports here are tens of
    kilobytes; anything near the cap is a bug or a bomb."""
    from jq_collector import github as gh

    api = client(lambda _r: httpx.Response(200, content=b"x" * 64))
    original = gh._MAX_ARTIFACT_BYTES
    gh._MAX_ARTIFACT_BYTES = 8
    try:
        assert api.coverage_percent("o/r", 5) is None
    finally:
        gh._MAX_ARTIFACT_BYTES = original
    assert "skipping" in caplog.text


def test_an_artifact_listing_that_is_not_a_dict_yields_no_artifact():
    api = client(lambda _r: ok([]))

    assert api.coverage_artifact("o/r", "main") == 0


def test_one_failing_repo_does_not_sink_the_refresh(monkeypatch, caplog):
    """Eight repos are gathered in parallel; a single one raising must cost that
    repo's row and nothing else. Losing the whole refresh would blank the board."""
    from jq_collector import github as gh

    class HalfBrokenAPI(StubAPI):
        def branch_sha(self, full_name, *_):
            if full_name == "o/bad":
                raise RuntimeError("this repo is cursed")
            return "sha"

        def list_repos(self):
            return [
                {
                    "full_name": f"o/{n}",
                    "name": n,
                    "default_branch": "main",
                    "owner": {"login": "o"},
                    "visibility": "public",
                    "open_issues_count": 0,
                }
                for n in ("good", "bad")
            ]

    stub = HalfBrokenAPI()
    monkeypatch.setattr(gh, "GitHub", lambda _cfg: stub)

    remote, _api, _latest, _excluded = gh.collect(Config(), {}, {})

    assert set(remote) == {"o/good"}, "a cursed repo took the healthy one with it"
    assert "o/bad failed" in caplog.text


def test_an_unchanged_branch_head_skips_the_pointer_read(monkeypatch):
    """The steady-state cost of drift correctness is zero extra calls."""
    from jq_collector import github as gh

    class CountingAPI(StubAPI):
        pointer_reads = 0

        def template_ref(self, *_):
            type(self).pointer_reads += 1
            return "v1.7.1"

    stub = CountingAPI()
    monkeypatch.setattr(gh, "GitHub", lambda _cfg: stub)

    cold, *_ = gh.collect(Config(), {}, {})
    assert CountingAPI.pointer_reads == 1
    assert cold["o/r"].rhiza_ref == "v1.7.1"

    warm, *_ = gh.collect(Config(), {"o/r": ("sha", "v1.7.1")}, {})
    assert CountingAPI.pointer_reads == 1, "an unchanged head still cost a pointer read"
    assert warm["o/r"].rhiza_ref == "v1.7.1"
