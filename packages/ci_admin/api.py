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
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ci_admin import lab
from ci_core import ops
from ci_core.db import Connection, connect
from ci_core.operator import (
    OperatorError,
    approve_all_enrolled,
    approve_mailbox,
    audit,
    exclude_mailbox,
    exclude_participant,
    list_mailboxes,
    list_participant_exclusions,
    set_mode,
)
from ci_core.runtime import RuntimeConfigError, active_config, eligible_mailboxes
from ci_core.settings import Settings
from ci_processing.classification import LABELS, POLICY_VERSION
from ci_processing.embedding import Embedder
from ci_processing.originals import OriginalStore
from ci_retrieval import search as retrieval
from ci_retrieval.entra import EntraTokenVerifier
from ci_retrieval.identity import Principal

log = logging.getLogger("ci.admin")

ADMIN_SCOPE = "Communications.Console"  # Entra refuses a scope and a role with one name
ADMIN_ROLE = "Communications.Admin"
STATIC = Path(__file__).parent / "static"

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
    _lab_cache: list[lab.LabBackends] = []
    if originals is None:
        from ci_cli.backends import original_store

        originals = original_store(settings)

    def lab_ready() -> lab.LabBackends:
        if not _lab_cache:
            if lab_backends is not None:
                _lab_cache.append(lab_backends())
            else:
                from ci_cli import backends

                _lab_cache.append(
                    lab.LabBackends(
                        backends.screener(settings),
                        backends.classifier(settings),
                        backends.redactor(settings),
                    )
                )
        return _lab_cache[0]

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

    # ----- lab ---------------------------------------------------------------

    def lab_classify(request: Request, principal: Principal, body: dict[str, Any]) -> Any:
        item = lab.pasted_input(body)
        person = lab.addresses(body.get("person"))
        received_by = lab.addresses(body.get("received_by")) or person
        result = lab.run(lab_ready(), item, person, received_by=received_by)
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
            lab_ready(),
            lab.stored_input(email),
            person,
            received_by=[email["mailbox"]],
            direction="out" if sender == email["mailbox"] else "in",
        )
        result["stored"] = {
            "labels": email["labels"],
            "uncertain_labels": email["uncertain_labels"],
            "summary_ai": email["summary_ai"],
            "action_ai": email["action_ai"],
            "action_for_ai": email["action_for_ai"],
            "risk_ai": email["risk_ai"],
        }
        result["changed_labels"] = sorted(set(email["labels"]) ^ set(result["yes_labels"]))
        _audit_lab(principal, "lab_reclassify", {"result_ids": [communication_id]})
        return result

    def _audit_lab(principal: Principal, action: str, details: dict[str, Any]) -> None:
        with connector("ci_operator") as conn:
            audit(conn, principal.oid, action, None, None, details, actor_type="user")
            conn.commit()

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
        return {"labels": [lb.__dict__ for lb in LABELS], "policy_version": POLICY_VERSION}

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
        Route("/api/lab/classify", endpoint(lab_classify), methods=["POST"]),
        Route("/api/lab/reclassify", endpoint(lab_reclassify), methods=["POST"]),
        Mount("/", StaticFiles(directory=STATIC, html=True)),
    ]
    return Starlette(routes=routes, middleware=[Middleware(SecurityHeaders)])
