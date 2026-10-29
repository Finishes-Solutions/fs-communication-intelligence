"""The admin console: a static single-page app plus a JSON API, one Starlette app.

Every ``/api`` call needs an Entra delegated token for the search API
registration carrying the scope ``Communications.Console`` **and** the app role
``Communications.Admin`` (assigned to named people only). A missing or invalid
token gets 401; a valid token without the role gets 403. The browser signs in
with authorization code + PKCE (public SPA client, no secret) and sends the
token as a bearer header, so there are no cookies and no CSRF surface.

Operations (schedule, run history, scope, mode, requeue) run as database role
``ci_operator`` and are audited with the signed-in person as actor. Message
content goes through the same authorized retrieval code as the MCP tools
(``ci_search``, grants from the token's roles, audited per call), so the
console shows exactly what that person may read: restricted mail never.
Original attachment files (kept without malware scanning, CLAUDE.md section
3a) are served only for a message the caller may read, always as a download
with a sandboxing CSP, and typed from a short allow-list so a file can never
run as a page of this site.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote

import psycopg
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ci_admin import billing, calibration, costs, lab, models, reports
from ci_core import ops
from ci_core.db import Connection, connect
from ci_core.operator import (
    OperatorError,
    approve_all_enrolled,
    approve_mailbox,
    audit,
    exclude_mailbox,
    exclude_participant,
    hidden_knowledge,
    hide_knowledge,
    knowledge_entry,
    list_mailboxes,
    list_participant_exclusions,
    list_people,
    merge_knowledge,
    merge_knowledge_many,
    reclassify_knowledge,
    rename_knowledge,
    set_knowledge_detail,
    set_mode,
    set_person,
    unmerge_knowledge,
)
from ci_core.runtime import RuntimeConfigError, active_config, eligible_mailboxes
from ci_core.settings import Settings
from ci_email.process import known_names, record_usage
from ci_processing.classification import (
    LABELS,
    LABELS_BY_NAME,
    POLICY_VERSION,
    Classifier,
    KnownName,
)
from ci_processing.embedding import Embedder
from ci_processing.knowledge import name_key
from ci_processing.originals import OriginalStore
from ci_processing.routing import PersonProfile
from ci_retrieval import knowledge as kb
from ci_retrieval import reports as report_queries
from ci_retrieval import search as retrieval
from ci_retrieval.entra import EntraTokenVerifier
from ci_retrieval.identity import Principal

log = logging.getLogger("ci.admin")

ADMIN_SCOPE = "Communications.Console"  # Entra refuses a scope and a role with one name
ADMIN_ROLE = "Communications.Admin"
STATIC = Path(__file__).parent / "static"
# Console pages are plain paths (/reports, /messages?q=...); each returns the same
# single-page app, which reads the path. "check" is the old name of "review".
PAGES = (
    "overview",
    "costs",
    "schedule",
    "mailboxes",
    "messages",
    "actionable",
    "unpublished",
    "risks",
    "reports",
    "review",
    "check",
    "knowledge",
    "lab",
)

Connector = Callable[[str], Connection]
# Types the browser may preview; everything else is served as an opaque download.
PREVIEW_TYPES = {
    "application/pdf",
    "image/png",
    "image/jpeg",
    "image/gif",
    "image/webp",
    "text/plain",
    "text/csv",
}
MAX_FILE_BYTES = 30 * 1024 * 1024
# Why a listed action item was not really the person's (Label check).
ACTION_REASONS = (
    "not_for_person",
    "fyi_only",
    "automated",
    "group_email",
    "already_handled",
    "other",
)
Handler = Callable[[Request, Principal, dict[str, Any]], Any]

_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:; "
    "connect-src 'self' https://login.microsoftonline.com; frame-src blob:; "
    "object-src blob:; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
)
_HEADERS = [
    (b"content-security-policy", _CSP.encode()),
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"no-referrer"),
    (b"x-frame-options", b"DENY"),
    (b"strict-transport-security", b"max-age=31536000"),
    (b"cache-control", b"no-store"),
]


class SecurityHeaders:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                # A response may set its own (stricter) value, e.g. a file download.
                present = {k.lower() for k, _ in message.get("headers", [])}
                message["headers"] = list(message.get("headers", [])) + [
                    (k, v) for k, v in _HEADERS if k not in present
                ]
            await send(message)

        await self.app(scope, receive, with_headers)


class RequestTiming:
    """One log line per API call: method, path, status and milliseconds, so slow
    pages can be found in Application Insights. Never the query string (it can
    hold search text), headers or bodies."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not str(scope.get("path", "")).startswith("/api/"):
            await self.app(scope, receive, send)
            return
        status: list[int] = []
        started = time.monotonic()

        async def sending(message: Message) -> None:
            if message["type"] == "http.response.start":
                status.append(int(message["status"]))
            await send(message)

        try:
            await self.app(scope, receive, sending)
        finally:
            log.info(
                "admin request %s %s -> %s in %d ms",
                scope.get("method"),
                scope.get("path"),
                status[0] if status else "error",
                (time.monotonic() - started) * 1000,
            )


def _role_connect(role: str) -> Connection:
    return connect(role=role)  # type: ignore[arg-type]


def _json(data: Any, status: int = 200) -> Response:
    return Response(
        json.dumps(data, default=_default, ensure_ascii=False),
        status_code=status,
        media_type="application/json",
    )


def _default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if hasattr(value, "is_finite"):  # Decimal
        return float(value)
    if hasattr(value, "__dict__"):
        return value.__dict__
    return str(value)


def _actor(principal: Principal) -> str:
    return principal.upn or principal.oid


def _int(request: Request, name: str, default: int, low: int, high: int) -> int:
    try:
        value = int(request.query_params.get(name, default))
    except ValueError as exc:
        raise HTTPException(400, f"{name} must be a number") from exc
    return max(low, min(value, high))


def _int_path(request: Request, name: str) -> int:
    try:
        return int(request.path_params[name])
    except ValueError as exc:
        raise HTTPException(404, "no such entry") from exc


def _float(value: Any, name: str) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise HTTPException(400, f"{name} must be a number") from exc


def build_admin_app(
    settings: Settings,
    embedder: Embedder,
    *,
    verifier: EntraTokenVerifier | None = None,
    connector: Connector = _role_connect,
    lab_backends: Callable[[], lab.LabBackends] | None = None,
    originals: OriginalStore | None = None,
    cost_client: billing.CostClient | None = None,
    deployment_lister: models.DeploymentLister | None = None,
    model_classifier: Callable[[str | None], Classifier] | None = None,
) -> Starlette:
    tenant = settings.entra_tenant_id or settings.graph_tenant_id or ""
    settings.require("search_api_client_id", "search_api_app_id_uri", "admin_spa_client_id")
    if not tenant:
        settings.require("entra_tenant_id")
    verifier = verifier or EntraTokenVerifier(
        tenant,
        settings.search_api_client_id or "",
        settings.search_api_app_id_uri or "",
        required_scope=ADMIN_SCOPE,
    )
    if verifier.required_scope != ADMIN_SCOPE:
        raise ValueError("the admin console needs a verifier for Communications.Console")
    _lab_cache: dict[str, lab.LabBackends] = {}  # by chat deployment
    if cost_client is None and settings.azure_subscription_id:
        cost_client = billing.CostClient(
            settings.azure_subscription_id, settings.azure_resource_group
        )
    openai_account = settings.azure_openai_account or models.account_name(
        settings.azure_openai_endpoint
    )
    if deployment_lister is None and settings.azure_subscription_id and openai_account:
        deployment_lister = models.DeploymentLister(
            settings.azure_subscription_id, settings.azure_resource_group, openai_account
        )

    def classifier_for(deployment: str | None) -> Classifier:
        """A classifier on that chat deployment (None: the app setting), for a test call."""
        if model_classifier is not None:
            return model_classifier(deployment)
        from ci_cli import backends

        chosen = (
            settings.model_copy(update={"azure_openai_chat_deployment": deployment})
            if deployment
            else settings
        )
        return backends.classifier(chosen, lab_ready(deployment).meter)

    if originals is None:
        from ci_cli.backends import original_store

        originals = original_store(settings)

    def lab_ready(chat_deployment: str | None = None) -> lab.LabBackends:
        """Lab backends for the console's chosen chat model (built once per model)."""
        key = chat_deployment or ""
        if key not in _lab_cache:
            if lab_backends is not None:
                _lab_cache[key] = lab_backends()
            else:
                from ci_cli import backends
                from ci_processing.prefilter import allow_domains
                from ci_processing.usage import Meter

                meter = Meter()
                chosen = (
                    settings.model_copy(update={"azure_openai_chat_deployment": chat_deployment})
                    if chat_deployment
                    else settings
                )
                _lab_cache[key] = lab.LabBackends(
                    backends.screener(settings, meter),
                    backends.classifier(chosen, meter),
                    backends.redactor(settings, meter),
                    backends.person_router(chosen, meter),
                    meter=meter,
                    prefilter=settings.bulk_prefilter_enabled,
                    prefilter_allow_domains=allow_domains(settings.bulk_prefilter_allow_domains),
                )
        return _lab_cache[key]

    def lab_now() -> lab.LabBackends:
        """The lab's backends with the console's switches for the paid checks applied,
        as processing applies them (ci_cli.backends.with_switches)."""
        with connector("ci_operator") as conn:
            schedule = ops.read_schedule(conn)
        return lab.with_switches(lab_ready(schedule.chat_deployment), settings, schedule)

    async def authenticate(request: Request) -> Principal | Response:
        header = request.headers.get("authorization", "")
        scheme, _, token = header.partition(" ")
        challenge = {"WWW-Authenticate": 'Bearer error="invalid_token"'}
        if scheme.lower() != "bearer" or not token:
            return JSONResponse({"error": "sign in"}, 401, headers=challenge)
        verified = await asyncio.to_thread(verifier.verify, token.strip())
        if verified is None:
            return JSONResponse({"error": "sign in again"}, 401, headers=challenge)
        principal = verified.principal()
        if ADMIN_ROLE not in principal.roles:
            log.info("admin refused: no %s role", ADMIN_ROLE)
            return JSONResponse({"error": "you are not an admin of this console"}, 403)
        return principal

    def endpoint(handler: Handler) -> Callable[[Request], Awaitable[Response]]:
        async def run(request: Request) -> Response:
            principal = await authenticate(request)
            if isinstance(principal, Response):
                return principal
            body: dict[str, Any] = {}
            if request.method in ("POST", "PUT", "DELETE"):
                raw = await request.body()
                if raw:
                    try:
                        parsed = json.loads(raw)
                    except ValueError:
                        return JSONResponse({"error": "body must be JSON"}, 400)
                    if not isinstance(parsed, dict):
                        return JSONResponse({"error": "body must be a JSON object"}, 400)
                    body = parsed
            try:
                data = await run_in_threadpool(handler, request, principal, body)
            except HTTPException as exc:
                return JSONResponse({"error": exc.detail}, exc.status_code)
            except (OperatorError, ValueError, RuntimeConfigError) as exc:
                return JSONResponse({"error": str(exc)}, 400)
            return data if isinstance(data, Response) else _json(data)

        return run

    # ----- operations (ci_operator) ------------------------------------------

    def overview(request: Request, principal: Principal, body: dict[str, Any]) -> Any:
        with connector("ci_operator") as conn:
            data = ops.overview(conn)
            data["schedule"] = ops.read_schedule(conn).public()
            data["runs"] = ops.recent_runs(conn, 10)
            data["mode"] = _mode(conn)
        with connector("ci_search") as conn:
            data["coverage"] = retrieval.coverage(conn)
        return data

    def _mode(conn: Connection) -> dict[str, Any]:
        try:
            config = active_config(conn)
        except RuntimeConfigError:
            return {"mode": None, "revision": None, "eligible_mailboxes": 0}
        return {
            "mode": config.mode,
            "revision": config.revision,
            "eligible_mailboxes": len(eligible_mailboxes(conn)),
        }

    def runs(request: Request, principal: Principal, body: dict[str, Any]) -> Any:
        with connector("ci_operator") as conn:
            return {"runs": ops.recent_runs(conn, _int(request, "limit", 100, 1, 500))}

    def schedule(request: Request, principal: Principal, body: dict[str, Any]) -> Any:
        with connector("ci_operator") as conn:
            if request.method == "PUT":
                ops.update_schedule(conn, _actor(principal), **body)
                conn.commit()
            current = ops.read_schedule(conn)
            return {
                "schedule": current.public(),
                "intervals": list(ops.INTERVALS),
                "next": ops.capture_decision(current, datetime.now(UTC)).reason,
            }

    def run_now(request: Request, principal: Principal, body: dict[str, Any]) -> Any:
        hours = _float(body.get("hours"), "hours")
        with connector("ci_operator") as conn:
            ops.request_run(conn, _actor(principal), hours)
            conn.commit()
        return {"requested": True, "hours": hours, "note": "Starts within about 5 minutes."}

    def mode(request: Request, principal: Principal, body: dict[str, Any]) -> Any:
        with connector("ci_operator") as conn:
            if request.method == "PUT":
                wanted = body.get("mode")
                if wanted not in ("test", "production", "paused"):
                    raise OperatorError("mode must be test, production or paused")
                if wanted == "production" and body.get("confirm") != "production":
                    raise OperatorError("switching to production needs confirm = 'production'")
                set_mode(conn, wanted, _actor(principal), note=body.get("note"))
                conn.commit()
            return _mode(conn)

    def mailboxes(request: Request, principal: Principal, body: dict[str, Any]) -> Any:
        with connector("ci_operator") as conn:
            return {
                "mailboxes": [m.__dict__ for m in list_mailboxes(conn)],
                "participant_exclusions": list_participant_exclusions(conn),
            }

    def mailbox_action(request: Request, principal: Principal, body: dict[str, Any]) -> Any:
        action = request.path_params["action"]
        actor = _actor(principal)
        address = str(body.get("address") or "")
        dataset = body.get("dataset", "test")
        if dataset not in ("test", "production"):
            raise OperatorError("dataset must be test or production")
        if dataset == "production" and body.get("confirm") != "production":
            raise OperatorError("production approvals need confirm = 'production'")
        with connector("ci_operator") as conn:
            result: dict[str, Any] = {"ok": True}
            if action == "include-all":
                result["approved"] = approve_all_enrolled(conn, dataset, actor)
            elif action == "include":
                approve_mailbox(conn, address, dataset, actor)
            elif action == "unapprove":
                approve_mailbox(conn, address, dataset, actor, revoke=True)
            elif action == "exclude":
                exclude_mailbox(conn, address, actor, reason=body.get("reason"))
            elif action == "unexclude":
                exclude_mailbox(conn, address, actor, revoke=True)
            elif action == "exclude-participant":
                exclude_participant(
                    conn, str(body.get("pattern") or ""), actor, reason=body.get("reason")
                )
            elif action == "unexclude-participant":
                exclude_participant(conn, str(body.get("pattern") or ""), actor, revoke=True)
            else:
                raise HTTPException(404, "unknown action")
            conn.commit()
            return result

    def unpublished(request: Request, principal: Principal, body: dict[str, Any]) -> Any:
        state = request.query_params.get("state") or None
        with connector("ci_operator") as conn:
            rows = conn.execute(
                "SELECT communication_id::text, version_id::text, processing_state,"
                " hold_reason_code, last_error_code, occurred_at, mailbox, sender"
                " FROM ops_unpublished WHERE (%(state)s::text IS NULL"
                "  OR processing_state = %(state)s) ORDER BY occurred_at DESC LIMIT %(n)s",
                {"state": state, "n": _int(request, "limit", 200, 1, 1000)},
            ).fetchall()
        return {
            "note": "Metadata only: unpublished mail (held, restricted, bulk or failed) is never"
            " shown, including its subject.",
            "items": rows,
        }

    def requeue(request: Request, principal: Principal, body: dict[str, Any]) -> Any:
        with connector("ci_operator") as conn:
            rows = conn.execute(
                "UPDATE communication_versions SET processing_state = 'fetched',"
                " last_error_code = NULL WHERE processing_state = 'failed' RETURNING id"
            ).fetchall()
            audit(
                conn,
                _actor(principal),
                "requeue_failed",
                "communication_versions",
                None,
                {"versions": len(rows)},
            )
            conn.commit()
        return {"requeued": len(rows), "note": "The process timer picks them up within 5 minutes."}

    # ----- content (ci_search, the caller's own grants) ----------------------

    def _filters(request: Request) -> retrieval.Filters:
        q = request.query_params
        days = _float(q.get("since_days"), "since_days")
        return retrieval.Filters(
            mailbox=q.get("mailbox") or None,
            sender=q.get("sender") or None,
            since=datetime.now(UTC) - timedelta(days=days) if days else None,
            label=q.get("label") or None,
            property=q.get("property") or None,
            entity=q.get("entity") or None,
        )

    def messages(request: Request, principal: Principal, body: dict[str, Any]) -> Any:
        query = (request.query_params.get("q") or "").strip()
        filters = _filters(request)
        with connector("ci_search") as conn:
            if query:
                limit = _int(request, "limit", 25, 1, 25)
                return retrieval.search(conn, principal, query, embedder, filters, limit)
            limit = _int(request, "limit", 100, 1, 100)
            return retrieval.list_email(conn, principal, filters, limit)

    def message(request: Request, principal: Principal, body: dict[str, Any]) -> Any:
        with connector("ci_search") as conn:
            found = retrieval.get_email(conn, principal, request.path_params["id"], 30_000)
        if found is None:
            raise HTTPException(404, "not found in mail you are authorized to see")
        return found

    def attachment(request: Request, principal: Principal, body: dict[str, Any]) -> Any:
        file_id = request.path_params["file_id"].lower()
        if not re.fullmatch(r"[0-9a-f]{64}", file_id):
            raise HTTPException(404, "no such file")
        with connector("ci_search") as conn:
            meta = retrieval.attachment_file(conn, principal, request.path_params["id"], file_id)
        if meta is None:
            raise HTTPException(404, "not found in mail you are authorized to see")
        if originals is None:
            raise HTTPException(404, "original files are not configured")
        data = originals.get(file_id)
        if data is None:
            raise HTTPException(404, "the original file is not stored")
        if len(data) > MAX_FILE_BYTES:
            raise HTTPException(413, "file too large to serve")
        declared = (meta["content_type"] or "").split(";")[0].strip().lower()
        media = declared if declared in PREVIEW_TYPES else "application/octet-stream"
        name = (meta["name"] or "attachment").replace("\r", " ").replace("\n", " ")
        return Response(
            data,
            media_type=media,
            headers={
                "Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}",
                "Content-Security-Policy": "sandbox; default-src 'none'",
                "X-Content-Type-Options": "nosniff",
                "Cache-Control": "no-store",
            },
        )

    def actionable(request: Request, principal: Principal, body: dict[str, Any]) -> Any:
        hours = _float(request.query_params.get("hours"), "hours") or 24
        with connector("ci_search") as conn:
            return retrieval.list_actionable(
                conn,
                principal,
                person=request.query_params.get("person") or None,
                hours=min(hours, 24 * 31),
                include_risks=True,
                limit=_int(request, "limit", 100, 1, 200),
            )

    # ----- knowledge base (content via ci_search; corrections as ci_operator) --

    def knowledge_list(request: Request, principal: Principal, body: dict[str, Any]) -> Any:
        q = request.query_params
        with connector("ci_search") as conn:
            found = kb.find_knowledge(
                conn,
                principal,
                q.get("q") or None,
                kind=q.get("kind") or None,
                limit=_int(request, "limit", 50, 1, 100),
            )
        with connector("ci_operator") as conn:
            found["hidden"] = hidden_knowledge(conn)
        return found

    def knowledge_graph(request: Request, principal: Principal, body: dict[str, Any]) -> Any:
        q = request.query_params
        days = q.get("days") or ""
        focus = q.get("focus") or ""
        if focus and not focus.isdigit():
            raise HTTPException(400, "focus must be an entry number")
        with connector("ci_search") as conn:
            return kb.knowledge_graph(
                conn,
                principal,
                kinds=[k for k in (q.get("kinds") or "").split(",") if k] or None,
                focus=int(focus) if focus else None,
                since=datetime.now(UTC) - timedelta(days=_int(request, "days", 365, 1, 3660))
                if days
                else None,
                limit=_int(request, "limit", 80, 2, kb.GRAPH_MAX_NODES),
                min_shared=_int(request, "min_shared", 2, 1, 50),
            )

    def knowledge_merge_many(request: Request, principal: Principal, body: dict[str, Any]) -> Any:
        ids = body.get("ids")
        if not isinstance(ids, list) or not all(isinstance(i, int) for i in ids):
            raise HTTPException(400, "ids must be a list of entry numbers")
        into = body.get("into")
        if not isinstance(into, int):
            raise HTTPException(400, "into must be an entry number")
        with connector("ci_operator") as conn:
            merged = merge_knowledge_many(conn, _actor(principal), ids, into)
            conn.commit()
            return {"merged": merged} | knowledge_entry(conn, into)

    def knowledge_entry_page(request: Request, principal: Principal, body: dict[str, Any]) -> Any:
        entity_id = _int_path(request, "id")
        with connector("ci_search") as conn:
            profile = kb.get_profile(conn, principal, entity_id)
        if profile is None:
            raise HTTPException(404, "not found in mail you are authorized to see")
        with connector("ci_operator") as conn:
            profile["correction"] = knowledge_entry(conn, entity_id)
        return profile

    def knowledge_action(request: Request, principal: Principal, body: dict[str, Any]) -> Any:
        entity_id = _int_path(request, "id")
        action = request.path_params["action"]
        actor = _actor(principal)
        with connector("ci_operator") as conn:
            if action == "merge":
                merge_knowledge(conn, actor, entity_id, int(body.get("into") or 0))
            elif action == "unmerge":
                unmerge_knowledge(conn, actor, entity_id)
            elif action in ("hide", "unhide"):
                hide_knowledge(conn, actor, entity_id, hidden=action == "hide")
            elif action == "rename":
                rename_knowledge(conn, actor, entity_id, str(body.get("name") or "") or None)
            elif action == "detail":
                set_knowledge_detail(conn, actor, entity_id, str(body.get("detail") or "") or None)
            elif action == "reclassify":
                into = reclassify_knowledge(
                    conn, actor, entity_id, str(body.get("kind") or ""), name_key
                )
                conn.commit()
                return knowledge_entry(conn, entity_id) | {"now_counts_as": into}
            else:
                raise HTTPException(404, "unknown action")
            conn.commit()
            return knowledge_entry(conn, entity_id)

    # ----- label check (reviewer ground truth) ------------------------------

    def _authorized_email(principal: Principal, communication_id: str) -> dict[str, Any]:
        with connector("ci_search") as conn:
            email = retrieval.get_email(conn, principal, communication_id, 6_000)
            if email is None:
                raise HTTPException(404, "not found in mail you are authorized to see")
            email["scores"] = conn.execute(
                "SELECT label, probability, decision FROM published_label_scores"
                " WHERE communication_id = %s ORDER BY label",
                (communication_id,),
            ).fetchall()
        return email

    def feedback(request: Request, principal: Principal, body: dict[str, Any]) -> Any:
        communication_id = request.path_params["id"]
        email = _authorized_email(principal, communication_id)
        actor = _actor(principal)
        with connector("ci_operator") as conn:
            if request.method == "PUT":
                _save_feedback(conn, communication_id, actor, body)
                conn.commit()
            rows = conn.execute(
                "SELECT subject_kind, subject, applies, reason_code FROM label_feedback"
                " WHERE communication_id = %s AND reviewer = %s",
                (communication_id, actor),
            ).fetchall()
        return {
            "email": email,
            "labels": {r["subject"]: r["applies"] for r in rows if r["subject_kind"] == "label"},
            "action_for": {
                r["subject"]: {"applies": r["applies"], "reason": r["reason_code"]}
                for r in rows
                if r["subject_kind"] == "action_for"
            },
            "reasons": list(ACTION_REASONS),
        }

    def _save_feedback(
        conn: Connection, communication_id: str, actor: str, body: dict[str, Any]
    ) -> None:
        kind = body.get("kind")
        subject = str(body.get("subject") or "").strip().lower()
        if kind == "label" and subject not in LABELS_BY_NAME:
            raise OperatorError("unknown label")
        if kind == "action_for" and not lab.addresses(subject):
            raise OperatorError("give the person's email address")
        if kind not in ("label", "action_for"):
            raise OperatorError("kind must be label or action_for")
        applies = body.get("applies")
        if applies is None:  # clear the judgement
            conn.execute(
                "DELETE FROM label_feedback WHERE communication_id = %s AND subject_kind = %s"
                " AND subject = %s AND reviewer = %s",
                (communication_id, kind, subject, actor),
            )
            return
        reason = body.get("reason") or None
        if reason is not None and reason not in ACTION_REASONS:
            raise OperatorError("unknown reason")
        conn.execute(
            "INSERT INTO label_feedback (communication_id, subject_kind, subject, applies,"
            " reason_code, reviewer) VALUES (%s, %s, %s, %s, %s, %s)"
            " ON CONFLICT (communication_id, subject_kind, subject, reviewer) DO UPDATE"
            " SET applies = EXCLUDED.applies, reason_code = EXCLUDED.reason_code,"
            " updated_at = now()",
            (communication_id, kind, subject, bool(applies), None if applies else reason, actor),
        )

    def review_next(request: Request, principal: Principal, body: dict[str, Any]) -> Any:
        """A not-yet-judged email from the chosen source, picked at random."""
        q = request.query_params
        source = q.get("source") or "recent"
        with connector("ci_search") as conn:
            if source == "actionable":
                items = retrieval.list_actionable(
                    conn,
                    principal,
                    person=q.get("person") or None,
                    hours=24 * 31,
                    include_risks=False,
                    limit=200,
                )["actions"]
            elif source == "risks":
                items = retrieval.list_actionable(
                    conn, principal, hours=24 * 31, include_risks=True, limit=200
                )["risks"]
            elif source == "label":
                items = retrieval.list_email(
                    conn, principal, retrieval.Filters(label=q.get("label") or None), 100
                )["results"]
            else:
                items = retrieval.list_email(conn, principal, retrieval.NO_FILTERS, 100)["results"]
        with connector("ci_operator") as conn:
            done = {
                str(r["communication_id"])
                for r in conn.execute(
                    "SELECT DISTINCT communication_id FROM label_feedback WHERE reviewer = %s",
                    (_actor(principal),),
                ).fetchall()
            }
        left = [i["communication_id"] for i in items if i["communication_id"] not in done]
        return {
            "communication_id": secrets.choice(left) if left else None,
            "remaining": len(left),
            "in_source": len(items),
        }

    def calibration_report(request: Request, principal: Principal, body: dict[str, Any]) -> Any:
        with connector("ci_operator") as conn:
            label_rows = conn.execute(
                "SELECT label, applies, probability, decision FROM ops_label_calibration"
            ).fetchall()
            action_rows = conn.execute(
                "SELECT person, applies, reason_code FROM ops_action_feedback"
            ).fetchall()
        return {
            "labels": calibration.label_report(label_rows),
            "actions": calibration.action_report(action_rows),
            "min_sample": calibration.MIN_SAMPLE,
            "policy_version": POLICY_VERSION,
        }

    # ----- lab ---------------------------------------------------------------

    def _known() -> list[KnownName]:
        with connector("ci_operator") as conn:
            return known_names(conn)

    def _lab_profile(person: list[str]) -> PersonProfile | None:
        if not person:
            return None
        with connector("ci_operator") as conn:
            row = conn.execute(
                "SELECT key, display_name, addresses, role, not_to_confuse FROM action_people"
                " WHERE enabled AND (key = ANY(%s) OR addresses && %s::text[]) ORDER BY key"
                " LIMIT 1",
                (person, person),
            ).fetchone()
        if row is None:
            return None
        return PersonProfile(
            row["key"],
            row["display_name"],
            tuple(row["addresses"]),
            row["role"],
            row["not_to_confuse"],
        )

    def people(request: Request, principal: Principal, body: dict[str, Any]) -> Any:
        with connector("ci_operator") as conn:
            if request.method == "PUT":
                set_person(
                    conn,
                    _actor(principal),
                    str(body.get("key") or ""),
                    display_name=str(body.get("display_name") or ""),
                    addresses=lab.addresses(body.get("addresses")),
                    role=str(body.get("role") or ""),
                    not_to_confuse=str(body.get("not_to_confuse") or ""),
                    enabled=bool(body.get("enabled", True)),
                )
                conn.commit()
            return {"people": list_people(conn)}

    def lab_classify(request: Request, principal: Principal, body: dict[str, Any]) -> Any:
        item = lab.pasted_input(body)
        person = lab.addresses(body.get("person"))
        received_by = lab.addresses(body.get("received_by")) or person
        result = lab.run(
            lab_now(),
            item,
            person,
            received_by=received_by,
            profile=_lab_profile(person),
            known=_known(),
            full_body=str(body.get("body") or ""),
        )
        _audit_lab(principal, "lab_classify", {"input_sha256": result["input_sha256"]})
        return result

    def lab_reclassify(request: Request, principal: Principal, body: dict[str, Any]) -> Any:
        communication_id = str(body.get("communication_id") or "")
        with connector("ci_search") as conn:
            email = retrieval.get_email(conn, principal, communication_id, 30_000)
        if email is None:
            raise HTTPException(404, "not found in mail you are authorized to see")
        person = lab.addresses(body.get("person"))
        sender = next(
            (p["email_normalized"] for p in email["participants"] if p["role"] == "from"), ""
        )
        result = lab.run(
            lab_now(),
            lab.stored_input(email),
            person,
            received_by=[email["mailbox"]],
            direction="out" if sender == email["mailbox"] else "in",
            profile=_lab_profile(person),
            known=_known(),
            full_body=next((t["text"] for t in email["text"] if t["source"] == "body"), ""),
            has_real_attachment=bool(email["attachments"]),
        )
        result["stored"] = {
            "labels": email["labels"],
            "uncertain_labels": email["uncertain_labels"],
            "summary_ai": email["summary_ai"],
            "action_ai": email["action_ai"],
            "action_for_ai": email["action_for_ai"],
            "risk_ai": email["risk_ai"],
            "properties_ai": email["properties_ai"],
            "entities_ai": email["entities_ai"],
        }
        result["changed_labels"] = sorted(set(email["labels"]) ^ set(result["yes_labels"]))
        _audit_lab(principal, "lab_reclassify", {"result_ids": [communication_id]})
        return result

    def _audit_lab(principal: Principal, action: str, details: dict[str, Any]) -> None:
        with connector("ci_operator") as conn:
            audit(conn, principal.oid, action, None, None, details, actor_type="user")
            conn.commit()
            # The lab's paid calls count on the cost page too (every model used).
            for built in list(_lab_cache.values()):
                if built.meter is None:
                    continue
                try:
                    with conn.transaction():
                        record_usage(conn, built.meter.drain(), source="lab")
                except psycopg.Error as exc:  # e.g. before migration 0013
                    log.warning("lab usage not recorded: %s", type(exc).__name__)

    # ----- costs --------------------------------------------------------------

    def cost_page(request: Request, principal: Principal, body: dict[str, Any]) -> Any:
        refresh = request.query_params.get("refresh") == "1"
        billed = (
            cost_client.costs(refresh=refresh)
            if cost_client is not None
            else {"available": False, "error": "not_configured"}
        )
        with connector("ci_operator") as conn:
            time_zone = ops.read_schedule(conn).time_zone
            try:
                with conn.transaction():
                    metered = costs.metered(conn, time_zone)
                    price_list = costs.prices(conn)
            except psycopg.errors.UndefinedTable:
                metered, price_list = {"available": False, "error": "migration_0013"}, []
        return {
            "billed": billed,
            "metered": metered,
            "prices": price_list,
            "time_zone": time_zone,
            "resource_group": settings.azure_resource_group,
        }

    def model_page(request: Request, principal: Principal, body: dict[str, Any]) -> Any:
        with connector("ci_operator") as conn:
            if request.method == "PUT":
                try:
                    _switch_model(conn, principal, body)
                except psycopg.errors.UndefinedColumn as exc:
                    raise OperatorError("apply database migration 0014 first") from exc
            schedule = ops.read_schedule(conn)
            try:
                with conn.transaction():
                    prices = models.model_prices(conn)
                    usage = models.usage_by_model(conn, schedule.time_zone)
                    defaults = {p["key"]: p for p in costs.prices(conn)}
            except psycopg.errors.UndefinedTable:
                return {"available": False, "error": "migration_0014"}
        listed = (
            deployment_lister.deployments(refresh=request.query_params.get("refresh") == "1")
            if deployment_lister is not None
            else {"available": False, "error": "not_configured"}
        )
        chat_in, chat_out = defaults.get("openai_chat_input"), defaults.get("openai_chat_output")
        return {
            "available": True,
            "current": schedule.chat_deployment or settings.azure_openai_chat_deployment,
            "chosen_in_console": schedule.chat_deployment is not None,
            "app_setting": settings.azure_openai_chat_deployment,
            "deployments": listed,
            "prices": prices,
            "default_price": {
                "input_usd_per_1m": chat_in["usd"] * 1_000_000 / chat_in["per_units"]
                if chat_in
                else None,
                "output_usd_per_1m": chat_out["usd"] * 1_000_000 / chat_out["per_units"]
                if chat_out
                else None,
            },
            "suggested_prices": {k: list(v) for k, v in models.SUGGESTED_PRICES.items()},
            "usage": usage,
        }

    def _switch_model(conn: Connection, principal: Principal, body: dict[str, Any]) -> None:
        """Switch only to a listed chat deployment that classifies a synthetic test email."""
        name = str(body.get("deployment") or "").strip() or None
        if name is not None and not ops.DEPLOYMENT_NAME.match(name):
            raise OperatorError("a deployment name is letters, digits, '.', '_' or '-'")
        listed = deployment_lister.deployments() if deployment_lister is not None else None
        if name is not None and listed and listed.get("available"):
            known = {d["deployment"] for d in listed["deployments"]}
            if name not in known:
                raise OperatorError(f"{name} is not a chat deployment on the Azure OpenAI account")
        models.try_model(classifier_for(name))
        ops.update_schedule(conn, _actor(principal), chat_deployment=name)
        conn.commit()

    def model_price(request: Request, principal: Principal, body: dict[str, Any]) -> Any:
        with connector("ci_operator") as conn:
            prices = models.set_model_price(
                conn, _actor(principal), request.path_params["deployment"], body
            )
            conn.commit()
            return {"prices": prices}

    def cost_prices(request: Request, principal: Principal, body: dict[str, Any]) -> Any:
        with connector("ci_operator") as conn:
            updated = costs.update_prices(conn, _actor(principal), body)
            conn.commit()
            return {"prices": updated}

    # ----- risk analysis, label review and reports ---------------------------

    def _time_zone() -> str:
        with connector("ci_operator") as conn:
            return ops.read_schedule(conn).time_zone

    def risks(request: Request, principal: Principal, body: dict[str, Any]) -> Any:
        q = request.query_params
        with connector("ci_search") as conn:
            return report_queries.risk_overview(
                conn,
                principal,
                days=_int(request, "days", 30, 1, 366),
                mailbox=(q.get("mailbox") or "").strip().lower() or None,
                category=q.get("category") or None,
                property=(q.get("property") or "").strip() or None,
                impact=q.get("impact") or None,
                likelihood=q.get("likelihood") or None,
                tz=_time_zone(),
            )

    def _report(principal: Principal, body: dict[str, Any], limit: int, action: str) -> Any:
        req = reports.parse(body)
        tz = _time_zone()
        data = reports.run(
            req,
            principal,
            search_conn=lambda: connector("ci_search"),
            operator_conn=lambda: connector("ci_operator"),
            tz_name=tz,
            limit=limit,
            action=action,
        )
        return req, tz, data

    def report_preview(request: Request, principal: Principal, body: dict[str, Any]) -> Any:
        _, tz, data = _report(principal, body, reports.PREVIEW_ROWS, "report_preview")
        return data | {"time_zone": tz}

    def report_export(request: Request, principal: Principal, body: dict[str, Any]) -> Any:
        fmt = body.get("format")
        if fmt not in reports.FORMATS:
            raise OperatorError("format must be csv or xlsx")
        req, tz, data = _report(principal, body, report_queries.MAX_ROWS, "report_export")
        header, rows = reports.table(data, tz)
        name = reports.file_name(req, fmt, datetime.now(UTC).date())
        content = (
            reports.to_csv(header, rows)
            if fmt == "csv"
            else reports.to_xlsx(header, rows, req.dataset.replace("_", " ").title())
        )
        media = (
            "text/csv; charset=utf-8"
            if fmt == "csv"
            else "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
        return Response(
            content,
            media_type=media,
            headers={
                "Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}",
                "Content-Security-Policy": "sandbox; default-src 'none'",
                "X-Content-Type-Options": "nosniff",
                "Cache-Control": "no-store",
                "X-Report-Rows": str(len(rows)),
                "X-Report-Total": str(data["total"]),
            },
        )

    # ----- public ------------------------------------------------------------

    async def config(request: Request) -> Response:
        """What the SPA needs to sign in. Public on purpose: ids, not secrets."""
        return _json(
            {
                "tenant_id": tenant,
                "client_id": settings.admin_spa_client_id,
                "scope": verifier.scope_uri,
                "environment": settings.deployment_environment.value,
            }
        )

    def labels(request: Request, principal: Principal, body: dict[str, Any]) -> Any:
        return {
            "labels": [lb.__dict__ | {"title": reports.label_title(lb.name)} for lb in LABELS],
            "policy_version": POLICY_VERSION,
        }

    async def page(request: Request) -> Response:
        return FileResponse(STATIC / "index.html", media_type="text/html")

    async def health(request: Request) -> Response:
        return _json({"ok": True})

    routes = [
        Route("/config.json", config),
        Route("/healthz", health),
        Route("/api/overview", endpoint(overview)),
        Route("/api/labels", endpoint(labels)),
        Route("/api/runs", endpoint(runs)),
        Route("/api/runs/capture", endpoint(run_now), methods=["POST"]),
        Route("/api/schedule", endpoint(schedule), methods=["GET", "PUT"]),
        Route("/api/mode", endpoint(mode), methods=["GET", "PUT"]),
        Route("/api/mailboxes", endpoint(mailboxes)),
        Route("/api/mailboxes/{action}", endpoint(mailbox_action), methods=["POST"]),
        Route("/api/unpublished", endpoint(unpublished)),
        Route("/api/requeue-failed", endpoint(requeue), methods=["POST"]),
        Route("/api/messages", endpoint(messages)),
        Route("/api/messages/{id}", endpoint(message)),
        Route("/api/messages/{id}/files/{file_id}", endpoint(attachment)),
        Route("/api/actionable", endpoint(actionable)),
        Route("/api/feedback/{id}", endpoint(feedback), methods=["GET", "PUT"]),
        Route("/api/knowledge", endpoint(knowledge_list)),
        Route("/api/knowledge/graph", endpoint(knowledge_graph)),
        Route("/api/knowledge/merge", endpoint(knowledge_merge_many), methods=["POST"]),
        Route("/api/knowledge/{id}", endpoint(knowledge_entry_page)),
        Route("/api/knowledge/{id}/{action}", endpoint(knowledge_action), methods=["POST"]),
        Route("/api/review/next", endpoint(review_next)),
        Route("/api/people", endpoint(people), methods=["GET", "PUT"]),
        Route("/api/calibration", endpoint(calibration_report)),
        Route("/api/lab/classify", endpoint(lab_classify), methods=["POST"]),
        Route("/api/lab/reclassify", endpoint(lab_reclassify), methods=["POST"]),
        Route("/api/risks", endpoint(risks)),
        Route("/api/costs", endpoint(cost_page)),
        Route("/api/costs/prices", endpoint(cost_prices), methods=["PUT"]),
        Route("/api/models", endpoint(model_page), methods=["GET", "PUT"]),
        Route("/api/models/{deployment}/price", endpoint(model_price), methods=["PUT"]),
        Route("/api/reports/preview", endpoint(report_preview), methods=["POST"]),
        Route("/api/reports/export", endpoint(report_export), methods=["POST"]),
        *(Route(f"/{name}", page) for name in PAGES),
        Route("/read/{id}", page),  # the read-only email page MCP results link to
        Mount("/", StaticFiles(directory=STATIC, html=True)),
    ]
    return Starlette(
        routes=routes, middleware=[Middleware(RequestTiming), Middleware(SecurityHeaders)]
    )
