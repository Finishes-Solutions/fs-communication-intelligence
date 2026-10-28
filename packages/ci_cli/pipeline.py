"""``ci`` commands for the real-mail run: tenant, grants, capture, process, stats,
search, actionable and the MCP server. Each opens its own connections with the
least-privileged application role it needs.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections import Counter
from typing import Any

from ci_cli import backends
from ci_cli.seed import CORPUS_KEY, READER_ALL_ROLE
from ci_core.db import AppRole, Connection, connect, required
from ci_core.operator import OperatorError, approve_mailbox, audit, set_mode
from ci_core.settings import GraphBackend, Settings, load_settings
from ci_email.capture import NORMALIZER_VERSION, capture_window
from ci_email.fixture import FixtureGraphClient
from ci_email.inventory import sync_inventory
from ci_email.process import process_all, retry_failed
from ci_processing.chunking import CHUNKER_VERSION
from ci_processing.classification import POLICY_VERSION
from ci_retrieval import search as retrieval
from ci_retrieval.identity import Principal, resolve_principal

log = logging.getLogger("ci")


def _tenant_id(settings: Settings) -> str:
    if settings.graph_backend is GraphBackend.FIXTURE:
        return FixtureGraphClient.from_directory().tenant.tenant_id
    settings.require("graph_tenant_id")
    return settings.graph_tenant_id or ""


def connection_and_corpus(conn: Connection, settings: Settings) -> tuple[str, str]:
    row = conn.execute(
        "SELECT c.id::text AS connection_id, k.id::text AS corpus_id"
        " FROM source_connections c, corpora k"
        " WHERE c.provider = 'email' AND c.account_ref = %s AND k.corpus_key = %s",
        (_tenant_id(settings), CORPUS_KEY),
    ).fetchone()
    if row is None:
        raise OperatorError("tenant not registered; run `ci tenant init` (or `ci seed` locally)")
    return row["connection_id"], row["corpus_id"]


def tenant_init(conn: Connection, actor: str) -> None:
    settings = load_settings()
    tenant = _tenant_id(settings)
    connection = required(
        conn.execute(
            "INSERT INTO source_connections (provider, account_ref, display_name, enabled)"
            " VALUES ('email', %s, 'Finishes Solutions Microsoft 365', true)"
            " ON CONFLICT (provider, account_ref) DO UPDATE SET enabled = true RETURNING id::text",
            (tenant,),
        ).fetchone()
    )["id"]
    corpus = required(
        conn.execute(
            "INSERT INTO corpora (corpus_key, description)"
            " VALUES (%s, 'Mail from enrolled mailboxes')"
            " ON CONFLICT (corpus_key) DO UPDATE SET corpus_key = EXCLUDED.corpus_key"
            " RETURNING id::text",
            (CORPUS_KEY,),
        ).fetchone()
    )["id"]
    if not conn.execute(
        "SELECT 1 FROM access_grants WHERE corpus_id = %s AND principal_type = 'entra_app_role'"
        " AND principal_id = %s AND revoked_at IS NULL",
        (corpus, READER_ALL_ROLE),
    ).fetchone():
        conn.execute(
            "INSERT INTO access_grants (corpus_id, principal_type, principal_id, granted_by)"
            " VALUES (%s, 'entra_app_role', %s, %s)",
            (corpus, READER_ALL_ROLE, actor),
        )
    if not conn.execute("SELECT 1 FROM runtime_config WHERE is_active").fetchone():
        set_mode(conn, "test", actor, note="ci tenant init")
    audit(conn, actor, "tenant_init", "source_connection", connection)
    print(f"tenant registered (connection {connection}); mode: test")


def inventory_sync(conn: Connection, actor: str) -> None:
    settings = load_settings()
    connection, corpus = connection_and_corpus(conn, settings)
    summary = sync_inventory(conn, backends.graph_client(settings), connection, corpus, actor)
    print(f"inventory: {dict(sorted(summary.states.items()))}")


def include_all_enrolled(conn: Connection, actor: str) -> None:
    rows = conn.execute(
        "SELECT m.primary_smtp_address AS address FROM source_scopes s"
        " JOIN email_mailboxes m ON m.scope_id = s.id"
        " WHERE s.scope_state = 'enrolled' AND NOT s.test_approved ORDER BY 1"
    ).fetchall()
    for r in rows:
        approve_mailbox(conn, r["address"], "test", actor)
    print(f"approved {len(rows)} enrolled mailbox(es) for test; exclusions still win")


def grant(conn: Connection, args: argparse.Namespace, actor: str) -> None:
    corpus = required(
        conn.execute("SELECT id::text FROM corpora WHERE corpus_key = %s", (CORPUS_KEY,)).fetchone()
    )["id"]
    if args.action == "list":
        for r in conn.execute(
            "SELECT principal_type, principal_id, granted_by, valid_from FROM access_grants"
            " WHERE corpus_id = %s AND revoked_at IS NULL ORDER BY valid_from",
            (corpus,),
        ).fetchall():
            print(f"{r['principal_type']:<15} {r['principal_id']:<40} by {r['granted_by']}")
        return
    oid = args.user
    if getattr(args, "me", False):
        oid = resolve_principal(load_settings()).oid
    if not oid:
        raise OperatorError("give --me or --user OBJECT_ID")
    if args.action == "revoke":
        conn.execute(
            "UPDATE access_grants SET revoked_at = now() WHERE corpus_id = %s"
            " AND principal_type = 'entra_user' AND principal_id = %s AND revoked_at IS NULL",
            (corpus, oid),
        )
        audit(conn, actor, "grant_revoke", "corpus", corpus, {"principal": oid})
        print(f"revoked read access for {oid}")
        return
    conn.execute(
        "INSERT INTO access_grants (corpus_id, principal_type, principal_id, granted_by)"
        " SELECT %s, 'entra_user', %s, %s WHERE NOT EXISTS (SELECT 1 FROM access_grants"
        " WHERE corpus_id = %s AND principal_type = 'entra_user' AND principal_id = %s"
        " AND revoked_at IS NULL)",
        (corpus, oid, actor, corpus, oid),
    )
    audit(conn, actor, "grant_add", "corpus", corpus, {"principal": oid})
    print(f"granted read access on {CORPUS_KEY} to {oid}")


def _role_connect(role: AppRole | None) -> Connection:
    return connect(role=role)


def capture_profile(settings: Settings) -> dict[str, Any]:
    """Processing profile stamped on new versions (changing it reprocesses)."""
    return {
        "normalizer": NORMALIZER_VERSION,
        "chunker": CHUNKER_VERSION,
        "classifier_policy": POLICY_VERSION,
        "embedding_dimensions": settings.embedding_dimensions,
        "embedding_model": settings.azure_openai_embedding_deployment
        if settings.embedder.value != "fake"
        else "fake-hashing-v1",
    }


def capture(args: argparse.Namespace) -> None:
    settings = load_settings()
    results = capture_window(
        _role_connect,
        backends.graph_client(settings),
        hours=args.hours,
        include_sent=settings.email_include_sent,
        profile=capture_profile(settings),
        workers=args.workers,
    )
    total: Counter[str] = Counter()
    for r in results:
        total.update(r.counts)
        status = (
            f"error={r.error}"
            if r.error
            else ", ".join(f"{k}={v}" for k, v in sorted(r.counts.items()))
        )
        print(f"{r.address:<45} {status}")
    failed = sum(1 for r in results if r.error)
    print(f"\n{len(results)} mailboxes, {failed} failed: {dict(sorted(total.items()))}")


def process(args: argparse.Namespace) -> None:
    settings = load_settings()
    if args.retry_failed:
        with connect(role="ci_ingest") as conn:
            print(f"requeued {retry_failed(conn)} failed version(s)")
    if args.limit == 0:  # requeue only; the Function App's process timer picks them up
        return
    counts = process_all(
        lambda: connect(role="ci_ingest"),
        backends.processing_backends(settings),
        limit=args.limit,
        workers=args.workers,
    )
    print(f"processed: {dict(sorted(counts.items()))}")


def stats() -> None:
    with connect(role="ci_ingest") as conn:
        sections = {
            "versions by state": "SELECT processing_state AS k, count(*) AS n"
            " FROM communication_versions GROUP BY 1 ORDER BY 2 DESC",
            "held/rejected reasons": "SELECT hold_reason_code AS k, count(*) AS n"
            " FROM communication_versions WHERE hold_reason_code IS NOT NULL GROUP BY 1"
            " ORDER BY 2 DESC",
            "failed error codes": "SELECT last_error_code AS k, count(*) AS n"
            " FROM communication_versions WHERE processing_state = 'failed' GROUP BY 1",
            "dispositions": "SELECT disposition AS k, count(*) AS n FROM classification_runs"
            " GROUP BY 1 ORDER BY 2 DESC",
            "label = yes": "SELECT label AS k, count(*) AS n FROM classification_labels"
            " WHERE decision = 'yes' GROUP BY 1 ORDER BY 2 DESC",
            "label = review/unresolved": "SELECT label AS k, count(*) AS n"
            " FROM classification_labels WHERE decision IN ('review', 'unresolved')"
            " GROUP BY 1 ORDER BY 2 DESC",
            "attachment extraction": "SELECT extraction_status AS k, count(*) AS n"
            " FROM email_attachments GROUP BY 1 ORDER BY 2 DESC",
            "classification reused across copies": "SELECT (reused_from IS NOT NULL)::text AS k,"
            " count(*) AS n FROM classification_runs GROUP BY 1",
        }
        for title, query in sections.items():
            print(f"\n{title}:")
            for r in conn.execute(query).fetchall():
                print(f"  {r['k'] or '-':<45} {r['n']}")


def _principal() -> tuple[Settings, Principal]:
    settings = load_settings()
    return settings, resolve_principal(settings)


def search(args: argparse.Namespace) -> None:
    settings, principal = _principal()
    with connect(role="ci_search") as conn:
        result = retrieval.search(
            conn,
            principal,
            args.query,
            backends.embedder(settings),
            retrieval.Filters(mailbox=args.mailbox),
            args.limit,
        )
    if not result["authorized"]:
        print("You have no read grant. Run: ci grant add --me")
    for r in result["results"]:
        print(f"\n{r['received_at'][:16]}  {r['sender']}  ->  {r['mailbox']}")
        print(
            f"  {r['subject']}  [{', '.join(r['labels'])}]{'  REVIEW' if r['review_flag'] else ''}"
        )
        for e in r["excerpts"][:1]:
            print(f"  > {' '.join((e['text'] or '').split())[:240]}")
        print(f"  {r['web_link']}")
    print(f"\ncoverage: {result['coverage']}")


def actionable(args: argparse.Namespace) -> None:
    settings, principal = _principal()
    del settings
    with connect(role="ci_search") as conn:
        report = retrieval.list_actionable(
            conn, principal, person=args.person, hours=args.hours, limit=args.limit
        )
    text = render_actionable(report)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text)
        print(f"wrote {args.out}")
    else:
        print(text)


def render_actionable(report: dict[str, Any]) -> str:
    who = report["person"] or "anyone (all authorized mail)"
    lines = [
        f"# Actionable email for {who}",
        "",
        f"Since {report['since'][:16]} UTC. AI-generated summaries and actions are "
        "proposals: open the email before acting on it.",
        "",
    ]

    def item(i: dict[str, Any]) -> list[str]:
        out = [
            f"- **{i['subject'] or '(no subject)'}**: from {i['sender']}, "
            f"{i['received_at'][:16]}, mailbox {i['mailbox']}"
        ]
        if i["action_ai"]:
            due = f" (due: {i['due_ai']})" if i["due_ai"] else ""
            out.append(f"  - Ask: {i['action_ai']}{due}")
        if i.get("risk_ai"):
            out.append(f"  - Risk: {i['risk_ai']}")
        if i["summary_ai"]:
            out.append(f"  - Summary: {i['summary_ai']}")
        flags = ", ".join(i["labels"]) + ("; flagged for review" if i["review_flag"] else "")
        out.append(f"  - Labels: {flags or '-'}")
        if i["web_link"]:
            out.append(f"  - [Open in Outlook]({i['web_link']})")
        return out

    lines.append(f"## Needs action ({len(report['actions'])})")
    for i in report["actions"]:
        lines.extend(item(i))
    lines += ["", f"## Project risks ({len(report['risks'])})"]
    for i in report["risks"]:
        lines.extend(item(i))
    lines += ["", f"Coverage (messages by state): {json.dumps(report['coverage'])}", ""]
    return "\n".join(lines)


def mcp() -> None:
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING)
    settings, principal = _principal()
    from ci_retrieval.mcp_server import run_stdio

    run_stdio(principal, backends.embedder(settings))
