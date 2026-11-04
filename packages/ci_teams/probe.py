"""T0, the Teams access spike (docs/TEAMS_PLAN.md section 8).

One question: can the Teams identity read chat messages without a user, or does
Microsoft Graph answer with the "protected API" refusal that needs Microsoft's
access request form? The probe makes three application-only calls with the
Teams identity and reports status codes, Graph error codes, counts and the
roles in its own token. It never returns, stores or logs message content, chat
ids, names or addresses: the report is safe to paste into a chat or an issue.

Calls (all in docs/TEAMS_PLAN.md section 2.1):
1. ``GET /users/{upn}``: directory read (``User.Read.All``).
2. ``GET /users/{id}/chats?$top=1``: the chat list (``Chat.Read.All``).
3. ``GET /users/{id}/chats/getAllMessages?$top=1&$filter=lastModifiedDateTime gt
   <7 days ago>``: the export API that capture would use (``Chat.Read.All``, the
   protected call).
"""

from __future__ import annotations

import base64
import json
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

GRAPH = "https://graph.microsoft.com/v1.0"
GRAPH_SCOPE = "https://graph.microsoft.com/.default"
# The permissions the identity needs for chats-only capture (D-T2, channels off).
REQUIRED_ROLES = ("User.Read.All", "Chat.Read.All", "ChatMember.Read.All")
PROTECTED_API_HINT = "Protected API"
MAX_MESSAGE_CHARS = 300


@dataclass(frozen=True)
class ProbeStep:
    name: str
    status: int | None  # HTTP status, or None when the request itself failed
    ok: bool
    error_code: str | None = None
    error_message: str | None = None  # Graph's own error text, truncated; never content
    count: int | None = None  # items in the first page
    note: str | None = None


@dataclass
class ProbeReport:
    checked_at: str
    user_given: bool
    token_roles: tuple[str, ...] = ()
    missing_roles: tuple[str, ...] = ()
    steps: list[ProbeStep] = field(default_factory=list)
    verdict: str = "unknown"
    protected_api_refusal: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _token_roles(token: str) -> tuple[str, ...]:
    """The ``roles`` claim of an app-only token, decoded without verification.

    Only the role names are read; nothing else from the token is kept.
    """
    try:
        payload = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        roles = claims.get("roles") or ()
        return tuple(sorted(str(r) for r in roles))
    except IndexError, ValueError, AttributeError:
        return ()


def _error(response: httpx.Response) -> tuple[str | None, str | None]:
    try:
        err = response.json().get("error") or {}
    except ValueError:
        return None, None
    raw = err.get("message")
    message = raw[:MAX_MESSAGE_CHARS] if isinstance(raw, str) else None
    code = err.get("code")
    return (str(code) if code is not None else None), message


class TeamsProbe:
    def __init__(
        self,
        credential: Any,
        *,
        client: httpx.Client | None = None,
        timeout: float = 60.0,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._credential = credential
        self._http = client or httpx.Client(timeout=timeout)
        self._now = now

    def close(self) -> None:
        self._http.close()

    def _get(self, url: str, params: dict[str, str] | None = None) -> httpx.Response:
        token = self._credential.get_token(GRAPH_SCOPE).token
        for attempt in range(3):
            response = self._http.get(
                url, params=params, headers={"Authorization": f"Bearer {token}"}
            )
            if response.status_code == 429 and attempt < 2:
                time.sleep(min(float(response.headers.get("Retry-After", 5)), 30.0))
                continue
            return response
        return response

    def _step(
        self, name: str, url: str, params: dict[str, str] | None = None
    ) -> tuple[ProbeStep, dict[str, Any]]:
        try:
            response = self._get(url, params)
        except httpx.HTTPError as exc:
            return ProbeStep(
                name, None, False, note=f"request failed: {exc.__class__.__name__}"
            ), {}
        if response.status_code < 400:
            body = response.json() if response.content else {}
            count = len(body["value"]) if isinstance(body.get("value"), list) else None
            return ProbeStep(name, response.status_code, True, count=count), body
        code, message = _error(response)
        return ProbeStep(name, response.status_code, False, code, message), {}

    def run(self, user: str | None = None) -> ProbeReport:
        """Probe with the given user (UPN or object id), or the first directory user."""
        now = self._now()
        report = ProbeReport(checked_at=now.isoformat(), user_given=user is not None)
        try:
            report.token_roles = _token_roles(self._credential.get_token(GRAPH_SCOPE).token)
        except Exception as exc:  # the report must still be produced
            report.steps.append(
                ProbeStep("token", None, False, note=f"no token: {exc.__class__.__name__}")
            )
            report.verdict = "no_token"
            return report
        report.missing_roles = tuple(r for r in REQUIRED_ROLES if r not in report.token_roles)

        if user:
            step, body = self._step("user", f"{GRAPH}/users/{user}", {"$select": "id"})
            user_id = body.get("id")
        else:
            step, body = self._step(
                "user",
                f"{GRAPH}/users",
                {"$select": "id", "$top": "1", "$filter": "accountEnabled eq true"},
            )
            first = (body.get("value") or [{}])[0]
            user_id = first.get("id")
        report.steps.append(step)
        if not user_id:
            report.verdict = "directory_read_failed"
            return report

        step, _ = self._step("chats", f"{GRAPH}/users/{user_id}/chats", {"$top": "1"})
        report.steps.append(step)

        since = (now - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
        step, _ = self._step(
            "messages",
            f"{GRAPH}/users/{user_id}/chats/getAllMessages",
            {"$top": "1", "$filter": f"lastModifiedDateTime gt {since}"},
        )
        report.steps.append(step)

        refusal = any(
            s.status == 403 and PROTECTED_API_HINT.lower() in (s.error_message or "").lower()
            for s in report.steps
        )
        report.protected_api_refusal = refusal
        if refusal:
            report.verdict = "protected_api_approval_required"
        elif all(s.ok for s in report.steps):
            report.verdict = "ok"
        elif any(s.status == 403 for s in report.steps):
            report.verdict = "forbidden"
        elif any(s.status == 401 for s in report.steps):
            report.verdict = "unauthorized"
        else:
            report.verdict = "failed"
        return report


def explain(report: ProbeReport) -> str:
    """Plain-language lines for the console or a chat, counts and codes only."""
    lines = [f"Teams access probe at {report.checked_at}: {report.verdict}"]
    lines.append(f"  token roles: {', '.join(report.token_roles) or 'none'}")
    if report.missing_roles:
        lines.append(
            f"  missing roles: {', '.join(report.missing_roles)} "
            "(grant them with scripts/azure/10-teams-access.sh; Entra can take a few minutes)"
        )
    for s in report.steps:
        detail = f"HTTP {s.status}" if s.status is not None else "no response"
        if s.count is not None:
            detail += f", {s.count} item(s) in the first page"
        if s.error_code:
            detail += f", code {s.error_code}"
        if s.error_message:
            detail += f": {s.error_message}"
        if s.note:
            detail += f" ({s.note})"
        lines.append(f"  {s.name}: {detail}")
    if report.verdict == "ok":
        lines.append("  Chat export works for this identity. T1 can start.")
    elif report.protected_api_refusal:
        lines.append(
            "  Microsoft refuses app-only chat reads until the Protected APIs request is "
            "approved: submit the form (search 'Protected APIs in Microsoft Teams' on "
            "learn.microsoft.com) and re-run the probe after approval."
        )
    elif report.verdict == "forbidden":
        lines.append(
            "  403 without the protected-API text: check the roles above and wait a few "
            "minutes after granting; a cross-tenant chat also answers 403."
        )
    return "\n".join(lines)
