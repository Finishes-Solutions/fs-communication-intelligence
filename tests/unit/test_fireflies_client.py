"""Fireflies clients: the fixture team and the GraphQL client against recorded shapes."""

import json
from collections.abc import Callable
from datetime import UTC, datetime

import httpx
import pytest

from ci_fireflies.api import AccessDenied, Throttled, TranscriptNotFound
from ci_fireflies.capture import meeting_people, source_revision, summary_text, transcript_text
from ci_fireflies.fixture import FixtureFirefliesClient, fixture_now
from ci_fireflies.graphql_http import HttpFirefliesClient

Handler = Callable[[httpx.Request], httpx.Response]


def http(handler: Handler, **kwargs: object) -> HttpFirefliesClient:
    waits: list[float] = []
    client = HttpFirefliesClient(
        "test-key",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=waits.append,
        clock=lambda: 1000.0,
        **kwargs,  # type: ignore[arg-type]
    )
    client.waits = waits  # type: ignore[attr-defined]
    return client


def test_fixture_team_lists_windows_newest_first_and_applies_later_rounds() -> None:
    client = FixtureFirefliesClient.from_directory()
    assert client.whoami().is_admin
    assert len(client.list_users()) == 4
    since = datetime(2026, 10, 1, tzinfo=UTC)
    refs = client.list_transcripts(since, fixture_now())
    assert [r.id for r in refs][:2] == ["fft-0007-deleted-later", "fft-0005-ssn-mention"]
    assert all(since <= r.occurred_at < fixture_now() for r in refs)
    assert "fft-0006-old" not in {r.id for r in refs}
    before = source_revision(client.get_transcript("fft-0003-vendor-dispute"))
    client.advance()
    after = client.get_transcript("fft-0003-vendor-dispute")
    assert source_revision(after) != before
    assert after["title"].endswith("(resolved)") and len(after["sentences"]) == 4
    with pytest.raises(TranscriptNotFound):
        client.get_transcript("fft-0007-deleted-later")
    client.fail_next_with_429()
    with pytest.raises(Throttled):
        client.list_transcripts(since, fixture_now())


def test_normalisation_is_content_shaped_and_stable() -> None:
    data = FixtureFirefliesClient.from_directory().get_transcript("fft-0001-weekly-ops")
    text = transcript_text(data)
    assert text.startswith("[00:00] Pat Example: Let's start with Evergreen Heights.")
    assert text.count("\n") == 5
    summary = summary_text(data)
    assert "Fireflies AI, unverified" in summary and "Keywords" in summary
    people = meeting_people(data)
    roles = {(role, email) for role, email, _ in people}
    assert ("organizer", "pat.example@finishessolutions.example") in roles
    assert ("attendee", "dana.vendor@northwind-paving.example") in roles
    assert source_revision(data) == source_revision(json.loads(json.dumps(data)))
    assert summary_text({"summary": None}) == ""


def test_graphql_client_sends_the_key_pages_and_filters_the_range() -> None:
    seen: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        assert request.headers["Authorization"] == "Bearer test-key"
        if "transcripts(" in body["query"]:
            return httpx.Response(
                200,
                json={
                    "data": {
                        "transcripts": [
                            {
                                "id": "a",
                                "date": 1793000000000,
                                "duration": 10.0,
                                "organizer_email": "A@x.example",
                            },
                            {"id": "old", "date": 1700000000000, "duration": 1.0},
                        ]
                    }
                },
            )
        if "transcript(" in body["query"]:
            return httpx.Response(200, json={"data": {"transcript": {"id": "a", "sentences": []}}})
        return httpx.Response(
            200,
            json={
                "data": {
                    "user": {"user_id": "u", "email": "K@x.example", "name": "K", "is_admin": True}
                }
            },
        )

    client = http(handler)
    since, until = datetime(2026, 10, 1, tzinfo=UTC), datetime(2026, 11, 1, tzinfo=UTC)
    refs = client.list_transcripts(since, until, skip=50)
    assert [r.id for r in refs] == ["a"] and refs[0].organizer_email == "a@x.example"
    assert seen[0]["variables"] == {
        "fromDate": "2026-10-01T00:00:00Z",
        "toDate": "2026-11-01T00:00:00Z",
        "limit": 50,
        "skip": 50,
    }
    assert client.whoami().email == "k@x.example"
    assert client.get_transcript("a")["id"] == "a"
    # Paced to the default 60 requests a minute after the first call.
    assert client.waits == [1.0, 1.0]  # type: ignore[attr-defined]


def test_graphql_client_retries_429_then_raises_and_maps_errors() -> None:
    calls = {"n": 0}

    def throttled(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(429, headers={"Retry-After": "7"})

    client = http(throttled, max_retries=1)
    with pytest.raises(Throttled) as exc:
        client.list_users()
    assert exc.value.retry_after == 7.0 and calls["n"] == 2 and 7.0 in client.waits  # type: ignore[attr-defined]

    def graphql_error(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"errors": [{"message": "x", "extensions": {"code": "object_not_found"}}]}
        )

    with pytest.raises(TranscriptNotFound):
        http(graphql_error).get_transcript("missing")

    def forbidden(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={})

    with pytest.raises(AccessDenied):
        http(forbidden).list_users()
