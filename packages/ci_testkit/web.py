"""Testkit web UI (local only, 127.0.0.1). Server-rendered pages; no JavaScript framework.

Pages: Flow board, Send a test email, Message trace, Scope & rules, Search.
Every POST is checked against the Host and Origin headers so another website
open in the same browser cannot drive the testkit.
"""

from __future__ import annotations

import traceback
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from ci_core import control
from ci_core.control import ControlError
from ci_core.settings import AttachmentPolicy
from ci_testkit.harness import LANES, Overrides, RunReport, Testkit, TestkitError
from ci_testkit.routing import DEPARTMENTS

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
LANE_ORDER = (
    "searchable",
    "flagged",
    "restricted",
    "held",
    "not_indexed",
    "excluded",
    "withdrawn",
    "hidden",
    "queued",
    "failed",
    "not_read",
)


def create_app(testkit: Testkit, *, host: str, port: int) -> FastAPI:
    app = FastAPI(
        title="Communication Intelligence testkit", docs_url=None, redoc_url=None, openapi_url=None
    )
    allowed_hosts = {f"{host}:{port}", f"localhost:{port}", f"127.0.0.1:{port}"}
    allowed_origins = {f"http://{h}" for h in allowed_hosts}
    flash: list[RunReport] = []

    @app.middleware("http")
    async def local_only(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if request.headers.get("host") not in allowed_hosts:
            return PlainTextResponse("forbidden host", status_code=403)
        if request.method == "POST":
            origin = request.headers.get("origin")
            if origin is not None and origin not in allowed_origins:
                return PlainTextResponse("forbidden origin", status_code=403)
        try:
            return await call_next(request)
        except TestkitError as exc:
            return TEMPLATES.TemplateResponse(
                request, "error.html", {"message": str(exc)}, status_code=503
            )

    def page(request: Request, name: str, **ctx: Any) -> HTMLResponse:
        reports = list(flash)
        flash.clear()
        try:
            mode = testkit.mode()
        except TestkitError:
            mode = "unknown"
        base = {
            "mode": mode,
            "flash": reports,
            "settings": testkit.settings,
            "overrides": testkit.overrides(),
        }
        return TEMPLATES.TemplateResponse(request, name, {**base, **ctx})

    def back(url: str = "/", *reports: RunReport) -> RedirectResponse:
        flash.extend(reports)
        return RedirectResponse(url, status_code=303)

    def guarded(fn: Callable[[], list[RunReport]], url: str = "/") -> RedirectResponse:
        try:
            return back(url, *fn())
        except (ControlError, KeyError, ValueError) as exc:
            return back(url, RunReport("Not done", [str(exc)]))
        except TestkitError:
            raise
        except Exception as exc:  # show, don't hide, unexpected failures
            return back(
                url,
                RunReport(
                    "Error",
                    [f"{type(exc).__name__}: {exc}", *traceback.format_exc().splitlines()[-6:]],
                ),
            )

    # -- flow board ----------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    async def board(request: Request) -> HTMLResponse:
        lanes = testkit.board()
        return page(
            request,
            "board.html",
            lanes=lanes,
            lane_order=LANE_ORDER,
            lane_info=LANES,
            queue=testkit.queue(),
            mailboxes=testkit.mailboxes(),
        )

    @app.post("/run/{step}")
    async def run(step: str) -> RedirectResponse:
        steps: dict[str, Callable[[], list[RunReport]]] = {
            "all": testkit.run_all,
            "sync": lambda: [testkit.sync()],
            "reconcile": lambda: [testkit.reconcile()],
            "work": lambda: [testkit.work()],
            "reprocess": lambda: [testkit.reprocess_all()],
        }
        if step not in steps:
            return back("/", RunReport("Unknown step", [step]))
        return guarded(steps[step])

    @app.post("/reset")
    async def reset() -> RedirectResponse:
        return guarded(lambda: [testkit.reset()])

    # -- compose -------------------------------------------------------------

    @app.get("/compose", response_class=HTMLResponse)
    async def compose(request: Request, preset: str = "") -> HTMLResponse:
        presets = testkit.presets()
        chosen = next((p for p in presets if p["key"] == preset), None)
        form: dict[str, Any] = {
            "to_mailbox": "pat.morgan@finishes.example",
            "folder": "inbox",
            "from_name": "",
            "from_address": "",
            "cc": "",
            "subject": "",
            "body": "",
            "inline_logo": False,
            "pdf_attachment": False,
        }
        if chosen:
            m = chosen["message"]
            sender = m["from"]["emailAddress"]
            form.update(
                {
                    "to_mailbox": chosen["deliveries"][0]["mailbox"],
                    "folder": chosen["deliveries"][0].get("folder", "inbox"),
                    "from_name": sender.get("name", ""),
                    "from_address": sender["address"],
                    "cc": ", ".join(
                        r["emailAddress"]["address"] for r in m.get("ccRecipients", [])
                    ),
                    "subject": m["subject"],
                    "body": (m.get("uniqueBody") or m["body"])["content"],
                    "inline_logo": any(a.get("isInline") for a in m.get("attachments", [])),
                    "pdf_attachment": any(not a.get("isInline") for a in m.get("attachments", [])),
                }
            )
        mailboxes = [m["fixture"]["address"] for m in testkit.mailboxes()]
        return page(
            request, "compose.html", presets=presets, chosen=chosen, form=form, mailboxes=mailboxes
        )

    @app.post("/compose")
    async def compose_send(
        to_mailbox: str = Form(...),
        folder: str = Form("inbox"),
        from_name: str = Form(""),
        from_address: str = Form(...),
        cc: str = Form(""),
        subject: str = Form(""),
        body: str = Form(""),
        inline_logo: bool = Form(False),
        pdf_attachment: bool = Form(False),
        run_now: bool = Form(False),
    ) -> RedirectResponse:
        try:
            mid = testkit.deliver(
                to_mailbox=to_mailbox,
                folder=folder,
                from_name=from_name,
                from_address=from_address,
                cc=cc,
                subject=subject,
                body=body,
                inline_logo=inline_logo,
                pdf_attachment=pdf_attachment,
            )
        except KeyError as exc:
            return back("/compose", RunReport("Not delivered", [str(exc)]))
        delivered = RunReport("Delivered", [f"Delivered to {to_mailbox} ({folder})."])
        if run_now:
            return guarded(lambda: [delivered, *testkit.run_all()], f"/message/{mid}")
        return back(f"/message/{mid}", delivered)

    # -- message trace -------------------------------------------------------

    @app.get("/message/{message_id}", response_class=HTMLResponse)
    async def message(request: Request, message_id: str) -> HTMLResponse:
        try:
            t = testkit.trace(message_id)
        except KeyError:
            return TEMPLATES.TemplateResponse(
                request, "error.html", {"message": "No such message."}, status_code=404
            )
        return page(request, "message.html", t=t, lane_info=LANES, departments=DEPARTMENTS)

    @app.post("/message/{message_id}/move")
    async def move(message_id: str, folder: str = Form(...)) -> RedirectResponse:
        def go() -> list[RunReport]:
            testkit.store.move(message_id, folder)
            return [
                RunReport(
                    "Moved at source",
                    [f"Moved to {folder}. Run the flow to see what capture does with it."],
                )
            ]

        return guarded(go, f"/message/{message_id}")

    @app.post("/message/{message_id}/delete")
    async def delete(message_id: str) -> RedirectResponse:
        def go() -> list[RunReport]:
            testkit.store.hard_delete(message_id)
            return [RunReport("Deleted at source", ["Run the flow to see the withdrawal."])]

        return guarded(go, f"/message/{message_id}")

    # -- scope & rules -------------------------------------------------------

    @app.get("/scope", response_class=HTMLResponse)
    async def scope(request: Request) -> HTMLResponse:
        mailboxes = testkit.mailboxes()
        return page(
            request,
            "scope.html",
            mailboxes=mailboxes,
            addresses={m["fixture"]["id"]: m["fixture"]["address"] for m in mailboxes},
            exclusions=control.active_exclusions(testkit.conn),
            policies=[p.value for p in AttachmentPolicy],
        )

    @app.post("/scope/mailbox")
    async def scope_mailbox(
        scope_id: str = Form(...), native_scope_id: str = Form(...), action: str = Form(...)
    ) -> RedirectResponse:
        def go() -> list[RunReport]:
            conn = testkit.conn
            if action in ("approve", "unapprove"):
                control.set_scope_approval(
                    conn,
                    scope_id,
                    approved=action == "approve",
                    actor="testkit-ui",
                    backfill_since=testkit.settings.email_backfill_since,
                )
            elif action == "exclude":
                control.add_exclusion(
                    conn,
                    "scope",
                    actor="testkit-ui",
                    reason_code="ui",
                    scope_kind="mailbox",
                    native_scope_id=native_scope_id,
                )
            elif action == "unexclude":
                for e in control.active_exclusions(conn):
                    if e["exclusion_kind"] == "scope" and e["native_scope_id"] == native_scope_id:
                        control.revoke_exclusion(conn, str(e["id"]), actor="testkit-ui")
            else:
                raise ValueError(f"unknown action {action}")
            return [
                RunReport("Scope updated", [f"{action}: done and written to the audit log."]),
                testkit.sync(),
            ]

        return guarded(go, "/scope")

    @app.post("/scope/exclusion")
    async def add_exclusion(kind: str = Form(...), pattern: str = Form(...)) -> RedirectResponse:
        def go() -> list[RunReport]:
            if kind == "participant_address":
                control.add_exclusion(
                    testkit.conn,
                    "participant_address",
                    actor="testkit-ui",
                    reason_code="ui",
                    pattern=pattern,
                )
            elif kind == "participant_domain":
                control.add_exclusion(
                    testkit.conn,
                    "participant_domain",
                    actor="testkit-ui",
                    reason_code="ui",
                    pattern=pattern,
                )
            else:
                raise ValueError("choose an address or a domain")
            return [RunReport("Exclusion added", [f"{kind}: {pattern.strip().lower()}"])]

        return guarded(go, "/scope")

    @app.post("/scope/exclusion/{exclusion_id}/revoke")
    async def revoke(exclusion_id: str) -> RedirectResponse:
        def go() -> list[RunReport]:
            control.revoke_exclusion(testkit.conn, exclusion_id, actor="testkit-ui")
            return [
                RunReport(
                    "Exclusion revoked",
                    ["Already-indexed items reappear at once; skipped items need Reprocess."],
                )
            ]

        return guarded(go, "/scope")

    @app.post("/scope/mode")
    async def mode(mode: str = Form(...)) -> RedirectResponse:
        def go() -> list[RunReport]:
            if mode not in ("test", "paused"):
                raise ValueError("the testkit switches between test and paused only")
            control.set_mode(testkit.conn, mode, actor="testkit-ui")  # type: ignore[arg-type]
            return [RunReport("Mode changed", [f"Mode is now {mode}."])]

        return guarded(go, "/scope")

    @app.post("/scope/settings")
    async def settings(
        attachment_policy: str = Form(...),
        include_sent: bool = Form(False),
        screening_down: bool = Form(False),
    ) -> RedirectResponse:
        def go() -> list[RunReport]:
            AttachmentPolicy(attachment_policy)
            testkit.save_overrides(Overrides(attachment_policy, include_sent, screening_down))
            return [
                RunReport(
                    "Settings saved",
                    [
                        f"ATTACHMENT_UNSUPPORTED_POLICY={attachment_policy}, "
                        f"EMAIL_INCLUDE_SENT={str(include_sent).lower()}, "
                        f"screening service {'DOWN' if screening_down else 'up'}.",
                        "Use 'Reprocess everything' to apply a policy change to mail "
                        "already captured.",
                    ],
                )
            ]

        return guarded(go, "/scope")

    @app.post("/scope/simulate")
    async def simulate(
        throttle_calls: int = Form(0), expire_mailbox: str = Form("")
    ) -> RedirectResponse:
        def go() -> list[RunReport]:
            testkit.store.simulate(
                throttle_calls=throttle_calls, expire_delta_for=expire_mailbox or None
            )
            lines = []
            if throttle_calls:
                lines.append(f"The next {throttle_calls} Graph calls will return 429.")
            if expire_mailbox:
                lines.append(f"The next delta call for {expire_mailbox} will return 410.")
            return [RunReport("Simulation armed", lines or ["Nothing armed."])]

        return guarded(go, "/scope")

    # -- search --------------------------------------------------------------

    @app.get("/search", response_class=HTMLResponse)
    async def search_page(
        request: Request, q: str = "", persona: str = "executive", vector_only: bool = False
    ) -> HTMLResponse:
        result = testkit.search(q, persona) if q.strip() else None
        hidden = 0
        if result is not None and not vector_only:
            hidden = sum(1 for h in result.hits if not h.keyword_match)
            result.hits = [h for h in result.hits if h.keyword_match]
        return page(
            request,
            "search.html",
            q=q,
            persona=persona,
            result=result,
            vector_only=vector_only,
            hidden=hidden,
        )

    return app
