"""Fireflies client, normalization, transcript chunking and meeting analysis.

The HTTP tests pin our request/response handling against the documented
GraphQL shapes (httpx MockTransport). They cannot prove Fireflies answers in
these shapes; the first real run does that.
"""

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from ci_core.settings import Settings, load_settings
from ci_fireflies.capture import meeting_participants, provider_summary, segments, source_revision
from ci_fireflies.client import (
    PAGE_LIMIT,
    FirefliesError,
    HttpFirefliesClient,
    TranscriptNotFound,
)
from ci_fireflies.fixture import FixtureFirefliesClient
from ci_processing.chunking import Segment, chunk_segments
from ci_processing.classification import KnownName
from ci_processing.meetings import (
    MAX_TRANSCRIPT_CHARS,
    MEETING_LABELS,
    AzureOpenAIMeetingAnalyzer,
    FakeMeetingAnalyzer,
    MeetingInput,
)

Handler = Callable[[httpx.Request], httpx.Response]
T0 = datetime(2026, 10, 1, tzinfo=UTC)


def client(handler: Handler, sleeps: list[float] | None = None) -> HttpFirefliesClient:
    return HttpFirefliesClient(
        "test-key",
        per_minute=10_000,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=(sleeps.append if sleeps is not None else lambda _: None),
    )


def _body(request: httpx.Request) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(request.content)
    return data


def test_a_full_page_splits_the_window_and_results_are_deduplicated() -> None:
    # 120 meetings, one every 6 hours; the API caps every reply at 50.
    meetings = [(f"m{i:03}", T0 + timedelta(hours=6 * i)) for i in range(120)]
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer test-key"
        v = _body(request)["variables"]
        calls.append((v["fromDate"], v["toDate"]))
        start = datetime.fromisoformat(v["fromDate"].replace("Z", "+00:00"))
        end = datetime.fromisoformat(v["toDate"].replace("Z", "+00:00"))
        rows = [
            {"id": mid, "title": None, "date": int(when.timestamp() * 1000)}
            for mid, when in meetings
            if start <= when < end
        ][:PAGE_LIMIT]
        return httpx.Response(200, json={"data": {"transcripts": rows}})

    stubs = client(handler).list_transcripts(T0, T0 + timedelta(days=31))
    assert [s.id for s in stubs] == [m for m, _ in meetings]
    assert len(calls) > 1  # the first, full page was split


def test_throttling_is_retried_after_the_given_delay() -> None:
    sleeps: list[float] = []
    replies = iter(
        [
            httpx.Response(429, headers={"Retry-After": "7"}),
            httpx.Response(200, json={"errors": [{"extensions": {"code": "too_many_requests"}}]}),
            httpx.Response(200, json={"data": {"users": [{"user_id": "u1", "email": "A@X.EX"}]}}),
        ]
    )
    users = client(lambda _: next(replies), sleeps).list_users()
    assert [(u.user_id, u.email) for u in users] == [("u1", "a@x.ex")]
    assert sleeps[0] == 7


def test_errors_carry_codes_not_content() -> None:
    def denied(_: httpx.Request) -> httpx.Response:
        body = {"errors": [{"message": "secret text", "extensions": {"code": "forbidden"}}]}
        return httpx.Response(200, json=body)

    with pytest.raises(FirefliesError) as exc:
        client(denied).list_users()
    assert "forbidden" in str(exc.value) and "secret" not in str(exc.value)

    def missing(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"errors": [{"extensions": {"code": "object_not_found"}}]})

    with pytest.raises(TranscriptNotFound):
        client(missing).get_transcript("nope")
    with pytest.raises(FirefliesError):
        HttpFirefliesClient("")


def test_fixture_default_listing_is_not_team_wide_but_per_user_listing_is() -> None:
    ff = FixtureFirefliesClient.from_directory()
    since, until = datetime(2026, 10, 20, tzinfo=UTC), datetime(2026, 10, 28, tzinfo=UTC)
    default = {s.id for s in ff.list_transcripts(since, until)}
    per_user = {s.id for u in ff.list_users() for s in ff.list_transcripts(since, until, u.user_id)}
    assert "ff-005-vendor" not in default and "ff-005-vendor" in per_user
    assert "ff-006-old" not in per_user  # outside the window


def test_normalization() -> None:
    t = FixtureFirefliesClient.from_directory().get_transcript("ff-001-oac")
    people = meeting_participants(t)
    assert ("organizer", "pat.example@finishessolutions.example", "Pat Example") in people
    assert ("attendee", "jordan@buildright.example", "Jordan Vendor") in people
    rows = segments(t)
    assert rows[0][0] == 0 and rows[0][1] == "Pat Example" and rows[0][2] == 0
    assert all(r[3] is not None and r[2] is not None and r[3] >= r[2] for r in rows)
    assert provider_summary(t).startswith("Overview:")
    before = source_revision(t)
    t["summary"]["overview"] = "Edited later by Fireflies."
    assert source_revision(t) != before  # a late or edited summary is a new version
    joined = {**t, "participants": ["a@x.example, b@y.example"], "meeting_attendees": []}
    assert {a for _, a, _ in meeting_participants(joined)} >= {"a@x.example", "b@y.example"}


def test_transcript_chunks_keep_whole_sentences_with_ranges_and_speakers() -> None:
    segs = [
        Segment(
            i, "Pat" if i % 2 else "Morgan", f"Sentence number {i} " * 6, i * 1000, i * 1000 + 900
        )
        for i in range(60)
    ]
    chunks = chunk_segments(segs)
    assert len(chunks) > 1
    assert chunks[0].segment_from == 0 and chunks[-1].segment_to == 59
    assert all(c.start_ms is not None and c.end_ms is not None for c in chunks)
    assert chunks[1].segment_from <= chunks[0].segment_to  # overlap
    assert set(chunks[0].speakers) == {"Pat", "Morgan"}
    assert all(len(c.text) <= 2000 for c in chunks)
    assert chunks[0].text.startswith("Morgan: Sentence number 0")


def _item(transcript: str = "Pat: hello", attendees: tuple[str, ...] = ()) -> MeetingInput:
    return MeetingInput(
        "Weekly", "2026-10-27T15:00:00+00:00", "pat@x.example", attendees, transcript
    )


def test_fake_analyzer_finds_actions_decisions_and_risks() -> None:
    result = FakeMeetingAnalyzer().analyze(
        _item(
            "Pat: Framing is behind, a schedule delay at Example Ridge.\n"
            "Morgan: We agreed to approve change order 7.\n"
            "Jordan: I will send the schedule Friday."
        )
    )
    assert "project_risk" in result.yes_labels()
    assert result.decisions and result.risks and result.action_items
    assert result.action_items[0].owner == "Jordan"
    assert result.properties == ("Example Ridge",)


class _FakeHttp:
    def __init__(self, content: dict[str, Any]) -> None:
        self.content = content
        self.bodies: list[dict[str, Any]] = []

    def post(self, url: str, body: dict[str, Any]) -> dict[str, Any]:
        self.bodies.append(body)
        return {"choices": [{"message": {"content": json.dumps(self.content)}}]}


def test_azure_analyzer_validates_the_model_output() -> None:
    labels = {lb.name: 0.5 for lb in MEETING_LABELS} | {"project_risk": 1.7}
    http = _FakeHttp(
        {
            "labels": labels,
            "summary": "  A   summary. ",
            "decisions": ["Approve CO 7", "Approve CO 7", ""],
            "action_items": [
                {
                    "task": "Send schedule",
                    "owner": "Jordan",
                    "owner_email": "JORDAN@B.EXAMPLE",
                    "due": "Friday",
                },
                {
                    "task": "Pay",
                    "owner": "Someone",
                    "owner_email": "outsider@evil.example",
                    "due": None,
                },
                {"task": "", "owner": None, "owner_email": None, "due": None},
            ],
            "risks": ["Framing delay"],
            "properties": ["Willow Creek"],
            "entities": [],
        }
    )
    analyzer = AzureOpenAIMeetingAnalyzer("https://aoai.example/", "gpt", "2024-10-21", http=http)
    long_transcript = "Pat: " + "x" * (MAX_TRANSCRIPT_CHARS + 10)
    result = analyzer.analyze(
        _item(long_transcript, ("jordan@b.example",)),
        [KnownName("property", "Willow Creek Retail", ("Willow Creek",))],
    )
    assert result.probabilities["project_risk"] == 1.0
    assert result.summary == "A summary."
    assert result.decisions == ("Approve CO 7",)
    assert [(a.task, a.owner_email) for a in result.action_items] == [
        ("Send schedule", "jordan@b.example"),
        ("Pay", None),  # an address that is not an attendee is never kept
    ]
    assert result.input_truncated
    sent = json.loads(http.bodies[0]["messages"][1]["content"])
    assert len(sent["transcript"]) == MAX_TRANSCRIPT_CHARS and sent["transcript_truncated"]
    system = http.bodies[0]["messages"][0]["content"]
    assert "untrusted" in system and "Willow Creek Retail" in system
    assert http.bodies[0]["response_format"]["json_schema"]["strict"] is True


def test_settings_default_to_the_fixture_locally_and_refuse_it_in_production(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEPLOYMENT_ENVIRONMENT", "local")
    assert load_settings().fireflies_backend.value == "fixture"
    assert load_settings().fireflies_enabled is False
    monkeypatch.setenv("DEPLOYMENT_ENVIRONMENT", "development")
    assert load_settings().fireflies_backend.value == "api"
    monkeypatch.setenv("FIREFLIES_API_KEY", "abc")
    assert "abc" not in repr(load_settings())  # never printed
    with pytest.raises(ValueError, match="not allowed in production: fireflies_backend"):
        Settings(
            deployment_environment="production",
            attachment_unsupported_policy="publish_partial",
            fireflies_backend="fixture",
        )
