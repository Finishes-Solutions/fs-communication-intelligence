"""T0 Teams access probe against recorded-shape Graph responses (httpx MockTransport)."""

import base64
import json
from collections.abc import Callable
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx

from ci_teams.probe import REQUIRED_ROLES, TeamsProbe, explain

Handler = Callable[[httpx.Request], httpx.Response]


def token_with_roles(*roles: str) -> str:
    payload = base64.urlsafe_b64encode(json.dumps({"roles": list(roles)}).encode()).rstrip(b"=")
    return "h." + payload.decode() + ".s"


class FakeCredential:
    def __init__(self, token: str) -> None:
        self._token = token

    def get_token(self, *scopes: str) -> SimpleNamespace:
        return SimpleNamespace(token=self._token)


def probe(handler: Handler, token: str) -> TeamsProbe:
    return TeamsProbe(
        FakeCredential(token),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        now=lambda: datetime(2026, 11, 4, 12, tzinfo=UTC),
    )


def test_ok_path_reports_counts_and_roles_only() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        path = request.url.path
        if path.endswith("/users/someone@example.com"):
            return httpx.Response(200, json={"id": "u1"})
        if path.endswith("/chats"):
            return httpx.Response(200, json={"value": [{"id": "19:secret@unq.gbl.spaces"}]})
        if path.endswith("/getAllMessages"):
            return httpx.Response(
                200, json={"value": [{"id": "m1", "body": {"content": "private text"}}]}
            )
        return httpx.Response(404, json={"error": {"code": "x", "message": "no"}})

    report = probe(handler, token_with_roles(*REQUIRED_ROLES)).run("someone@example.com")
    assert report.verdict == "ok"
    assert report.missing_roles == ()
    assert [s.name for s in report.steps] == ["user", "chats", "messages"]
    assert report.steps[2].count == 1
    assert "lastModifiedDateTime gt 2026-10-28T12:00:00Z" in seen[2].url.params["$filter"]
    assert seen[2].url.params["$top"] == "1"
    dumped = json.dumps(report.to_dict()) + explain(report)
    assert "private text" not in dumped and "secret" not in dumped and "u1" not in dumped


def test_protected_api_refusal_is_named() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/users"):
            return httpx.Response(200, json={"value": [{"id": "u1"}]})
        if request.url.path.endswith("/chats"):
            return httpx.Response(200, json={"value": []})
        return httpx.Response(
            403,
            json={
                "error": {
                    "code": "Forbidden",
                    "message": "Invoked API requires Protected API access in application-only "
                    "context when Resource Specific Consent is not used. Visit ...",
                }
            },
        )

    report = probe(handler, token_with_roles("Chat.Read.All", "User.Read.All")).run()
    assert report.user_given is False
    assert report.verdict == "protected_api_approval_required"
    assert report.protected_api_refusal is True
    assert report.missing_roles == ("ChatMember.Read.All",)
    text = explain(report)
    assert "Protected APIs request" in text
    assert "ChatMember.Read.All" in text


def test_plain_403_and_unreadable_token() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/users"):
            return httpx.Response(200, json={"value": [{"id": "u1"}]})
        return httpx.Response(403, json={"error": {"code": "AclCheckFailed", "message": "no"}})

    report = probe(handler, "not-a-jwt").run()
    assert report.token_roles == ()
    assert report.missing_roles == REQUIRED_ROLES
    assert report.verdict == "forbidden"
    assert report.steps[1].error_code == "AclCheckFailed"


def test_directory_failure_stops_early() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"code": "InvalidAuthenticationToken"}})

    report = probe(handler, token_with_roles()).run()
    assert report.verdict == "directory_read_failed"
    assert len(report.steps) == 1
