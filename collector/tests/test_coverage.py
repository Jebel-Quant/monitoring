"""Reading test coverage out of what CI already publishes.

The collector never runs anyone's tests. It reads the `coverage-report`
artifact CI uploads, which means the number is tied to a commit and a branch
rather than to whenever somebody last ran pytest in a checkout.

The trap these pin is the branch. Artifacts come back newest-first across every
ref, and in a repo that tags releases the newest one is usually a tag build -
rhiza's most recent coverage artifact is from `v1.7.1`, not `main`. Taking the
latest would report a release build's coverage as the repo's.
"""

from __future__ import annotations

import io
import zipfile

import pytest

from jq_collector.github import _coverage

ARTIFACTS = "/repos/o/r/actions/artifacts"


def artifact(aid: int, branch: str, created: str, name: str = "coverage-report", expired=False):
    return {
        "id": aid,
        "name": name,
        "expired": expired,
        "created_at": created,
        "workflow_run": {"head_branch": branch},
    }


def zipped(xml: str, filename: str = "coverage.xml") -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        bundle.writestr(filename, xml)
    return buffer.getvalue()


REPORT = '<coverage line-rate="0.873" branch-rate="0" lines-covered="412" lines-valid="472"/>'


def test_the_newest_default_branch_artifact_wins_not_the_newest_overall(make_client):
    """A tag build is usually the most recent artifact. It is not the answer."""
    client = make_client(
        {
            ARTIFACTS: {
                "artifacts": [
                    artifact(3, "v1.7.1", "2026-08-27T04:09:22Z"),  # newest, wrong branch
                    artifact(2, "main", "2026-08-27T04:07:41Z"),  # what we want
                    artifact(1, "release-prep", "2026-08-27T04:03:09Z"),
                ]
            }
        }
    )
    assert client.coverage_artifact("o/r", "main") == 2


def test_expired_and_unrelated_artifacts_are_ignored(make_client):
    client = make_client(
        {
            ARTIFACTS: {
                "artifacts": [
                    artifact(9, "main", "2026-08-28T00:00:00Z", expired=True),
                    artifact(8, "main", "2026-08-27T00:00:00Z", name="book"),
                    artifact(7, "main", "2026-08-26T00:00:00Z"),
                ]
            }
        }
    )
    assert client.coverage_artifact("o/r", "main") == 7


def test_a_repo_publishing_no_coverage_report_is_not_an_error(make_client):
    """Most of a mixed fleet does not publish one; six of this one do not."""
    client = make_client({ARTIFACTS: {"artifacts": [artifact(1, "main", "x", name="book")]}})
    assert client.coverage_artifact("o/r", "main") == 0


def test_the_percentage_and_its_denominator_are_both_read(make_client):
    """100% of 176 lines and 100% of 3878 are different assurances."""
    client = make_client({}, {f"{ARTIFACTS}/5/zip": zipped(REPORT)})
    assert client.coverage_percent("o/r", 5) == (87.3, 472)


def test_a_nested_coverage_xml_is_found(make_client):
    """CI uploads it as _tests/coverage.xml, so it is not at the archive root."""
    client = make_client({}, {f"{ARTIFACTS}/5/zip": zipped(REPORT, "_tests/coverage.xml")})
    assert client.coverage_percent("o/r", 5) == (87.3, 472)


def test_an_expired_artifact_download_is_survivable(make_client):
    """410 Gone is normal - artifacts expire on a schedule nobody here sets."""
    client = make_client({}, {})
    assert client.coverage_percent("o/r", 5) is None


@pytest.mark.parametrize(
    "blob",
    [
        b"not a zip at all",
        zipped("<coverage/>"),  # no line-rate
        zipped("not xml", "coverage.xml"),  # unparseable
        zipped(REPORT, "something-else.txt"),  # no coverage.xml inside
    ],
    ids=["not-a-zip", "no-line-rate", "bad-xml", "no-coverage-xml"],
)
def test_a_malformed_report_is_ci_s_problem_not_a_failed_refresh(make_client, blob):
    """Everything else in the refresh has already been gathered by this point."""
    client = make_client({}, {f"{ARTIFACTS}/5/zip": blob})
    assert client.coverage_percent("o/r", 5) is None


def test_an_absurdly_large_report_is_refused():
    """A zip bomb would be the collector's problem, not CI's."""
    from jq_collector import github

    blob = zipped("<coverage line-rate='1'/>" + " " * 1000)
    original = github._MAX_UNPACKED_BYTES
    github._MAX_UNPACKED_BYTES = 10
    try:
        with pytest.raises(ValueError, match="unpacks to"):
            _coverage(blob)
    finally:
        github._MAX_UNPACKED_BYTES = original


# -- the cache, which is what keeps this affordable --------------------------


class StubAPI:
    """Just enough of GitHub for collect(), counting the expensive call.

    Listing artifacts is one cheap call per repo and always happens, so a
    report published between refreshes is picked up. Downloading one is a zip -
    by far the largest response this collector handles - and must not happen
    again while the artifact id is unchanged.
    """

    rate_remaining = rate_limit = rate_reset = 0.0

    def __init__(self, artifact_id: int = 42) -> None:
        self.artifact_id = artifact_id
        self.downloads: list[str] = []

    def list_repos(self):
        return [
            {
                "full_name": "o/r",
                "name": "r",
                "default_branch": "main",
                "owner": {"login": "o"},
                "visibility": "public",
                "open_issues_count": 0,
            }
        ]

    def release_tags(self, _full_name):
        return []

    def branch_sha(self, *_):
        return "sha"

    def template_ref(self, *_):
        return ""

    def branch_protection(self, *_):
        return None, False

    def open_alerts(self, *_):
        return None

    def latest_runs(self, *_):
        return []

    def open_pulls(self, *_):
        return 0, []

    def recent_merges(self, *_):
        return []

    def coverage_artifact(self, *_):
        return self.artifact_id

    def coverage_percent(self, full_name, _artifact_id):
        self.downloads.append(full_name)
        return (87.3, 472)

    def close(self):
        pass


@pytest.fixture
def stubbed(monkeypatch):
    from jq_collector import github

    stub = StubAPI()
    monkeypatch.setattr(github, "GitHub", lambda _cfg: stub)
    return github, stub


def test_a_cold_refresh_downloads_the_report(stubbed, cfg):
    github, stub = stubbed
    remote, _api, _latest, _excluded = github.collect(cfg, {}, {})

    assert stub.downloads == ["o/r"]
    assert remote["o/r"].coverage == 87.3
    assert remote["o/r"].coverage_lines == 472
    assert remote["o/r"].coverage_artifact == 42


def test_an_unchanged_artifact_is_not_downloaded_again(stubbed, cfg):
    github, stub = stubbed
    cache = {"o/r": (42, (87.3, 472))}

    remote, *_ = github.collect(cfg, {}, cache)

    assert stub.downloads == [], "the zip was pulled again for an unchanged artifact"
    assert remote["o/r"].coverage == 87.3
    assert remote["o/r"].coverage_lines == 472


def test_a_new_artifact_is_downloaded(stubbed, cfg):
    github, stub = stubbed
    remote, *_ = github.collect(cfg, {}, {"o/r": (41, (10.0, 100))})

    assert stub.downloads == ["o/r"]
    assert remote["o/r"].coverage == 87.3


def test_a_repo_that_stops_publishing_loses_its_coverage(stubbed, cfg):
    """Absent, not the last value it happened to have."""
    github, stub = stubbed
    stub.artifact_id = 0

    remote, *_ = github.collect(cfg, {}, {"o/r": (42, (87.3, 472))})

    assert remote["o/r"].coverage is None
    assert stub.downloads == []
