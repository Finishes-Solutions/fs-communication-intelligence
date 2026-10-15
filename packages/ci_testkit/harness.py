"""The local testkit: wires the fixture mailboxes, database, worker and search together.

Everything the UI shows about an item is read back from the database (as the
roles the real apps use) or from the fixture mailbox store; nothing is
simulated in between. Only the backends are fakes.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import psycopg

from ci_core import control, jobs
from ci_core.blob import LocalBlobStore
from ci_core.db import Connection, connect, many, one, one_required, transaction
from ci_core.settings import (
    AttachmentPolicy,
    ClassifierBackend,
    DeploymentEnvironment,
    EmbedderBackend,
    GraphBackend,
    ScreenerBackend,
    Settings,
    SettingsError,
    load_settings,
)
from ci_email import capture
from ci_email.fixture_graph import FixtureGraphClient, FixtureMailStore
from ci_email.graph import SKIPPED_FOLDERS, GraphThrottled
from ci_email.worker import EmailWorker, WorkerReport, run_until_idle
from ci_processing.chunking import make_embedder
from ci_processing.classifier import make_classifier
from ci_processing.labels import LABELS_BY_NAME
from ci_processing.pipeline import ProcessingDeps
from ci_processing.screening import make_screener
from ci_retrieval.search import Caller, SearchResponse, search
from ci_testkit.routing import preview

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "email"
DATA_DIR = REPO_ROOT / "local" / ".data"
LOCAL_DB = "ci_local"
LOCAL_SERVER = "postgresql://postgres:postgres@127.0.0.1:5433"
BACKFILL_SINCE = "2026-09-01T00:00:00+00:00"
CONNECTION_ACCOUNT = "tenant-local-synthetic"
CORPUS_KEY = "email-enrolled"
READER_ALL = "Communications.Reader.All"
ACTOR = "testkit"
APPROVED_AT_SEED = (
    "pat.morgan@finishes.example",
    "sam.lee@finishes.example",
    "ap@finishes.example",
)
EXCLUDED_AT_SEED = "exec.private@finishes.example"
EXCLUDED_DOMAIN_AT_SEED = "hr-benefits.example"

TESTKIT_ENV = {
    "DEPLOYMENT_ENVIRONMENT": "local",
    "GRAPH_BACKEND": "fixture",
    "EMBEDDER": "fake",
    "SCREENER": "fake",
    "CLASSIFIER_BACKEND": "fake",
    "ATTACHMENT_UNSUPPORTED_POLICY": "publish_partial",
    "EMAIL_INCLUDE_SENT": "true",
    "EMAIL_BACKFILL_SINCE": BACKFILL_SINCE,
    "DATABASE_URL": f"{LOCAL_SERVER}/{LOCAL_DB}",
    "LOCAL_BLOB_DIR": str(DATA_DIR / "blob"),
}

LANES: dict[str, tuple[str, str]] = {
    # lane -> (title, explanation)
    "searchable": ("Delivered: searchable", "Published; executives' agents can find and cite it."),
    "flagged": ("Delivered with a review flag", "Published and searchable, marked for review."),
    "restricted": ("Restricted", "Stored, never embedded and never returned by search."),
    "held": ("Held", "Not indexed until a policy decision or a fix releases it."),
    "not_indexed": ("Recorded, not indexed", "Spam and newsletters are kept out of search."),
    "excluded": ("Excluded by rule", "A participant exclusion stopped capture before staging."),
    "withdrawn": ("Withdrawn", "Deleted at source; retrieval is blocked."),
    "hidden": ("Published but hidden", "Published, but scope, exclusion or mode hides it now."),
    "queued": ("In the queue", "Waiting for the worker, or retrying."),
    "failed": ("Failed (dead letter)", "Retries exhausted; needs attention."),
    "not_read": ("Not read", "Outside scope: never fetched from the mailbox."),
}


class TestkitError(RuntimeError):
    __test__ = False  # not a pytest test class


def apply_testkit_env() -> None:
    """Defaults for the local testkit; explicit environment variables still win."""
    for key, value in TESTKIT_ENV.items():
        os.environ.setdefault(key, value)


def load_testkit_settings() -> Settings:
    apply_testkit_env()
    try:
        settings = load_settings()
    except SettingsError as exc:
        raise TestkitError(f"settings refused: {exc}") from exc
    if settings.deployment_environment is not DeploymentEnvironment.LOCAL:
        raise TestkitError("the testkit only runs with DEPLOYMENT_ENVIRONMENT=local")
    if settings.graph_backend is not GraphBackend.FIXTURE:
        raise TestkitError("the testkit only runs with GRAPH_BACKEND=fixture (synthetic mail)")
    fakes = (
        settings.screener is ScreenerBackend.FAKE,
        settings.embedder is EmbedderBackend.FAKE,
        settings.classifier_backend is ClassifierBackend.FAKE,
    )
    if not all(fakes):
        raise TestkitError(
            "the testkit currently supports only the fake screener, classifier and embedder"
        )
    return settings


@dataclass
class Overrides:
    """What-if switches the UI can flip without restarting (stored in local/.data)."""

    attachment_policy: str | None = None
    include_sent: bool | None = None
    screening_down: bool = False


@dataclass
class RunReport:
    title: str
    lines: list[str] = field(default_factory=list)


class Testkit:
    __test__ = False  # not a pytest test class

    def __init__(self, data_dir: Path = DATA_DIR) -> None:
        self.base_settings = load_testkit_settings()
        self.data_dir = data_dir
        self.store = FixtureMailStore(data_dir / "fixture_mailboxes.json")
        self.graph = FixtureGraphClient(self.store)
        self.blob = LocalBlobStore(self.base_settings.local_blob_dir or data_dir / "blob")
        self._conn: Connection | None = None

    # -- configuration -----------------------------------------------------

    @property
    def database_url(self) -> str:
        url = self.base_settings.database_url
        if not url:
            raise TestkitError("DATABASE_URL is not set")
        return url

    def overrides(self) -> Overrides:
        path = self.data_dir / "testkit_overrides.json"
        if not path.exists():
            return Overrides()
        return Overrides(**json.loads(path.read_text()))

    def save_overrides(self, o: Overrides) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        (self.data_dir / "testkit_overrides.json").write_text(json.dumps(o.__dict__))

    @property
    def settings(self) -> Settings:
        o = self.overrides()
        update: dict[str, Any] = {}
        if o.attachment_policy:
            update["attachment_unsupported_policy"] = AttachmentPolicy(o.attachment_policy)
        if o.include_sent is not None:
            update["email_include_sent"] = o.include_sent
        return self.base_settings.model_copy(update=update)

    @property
    def conn(self) -> Connection:
        if self._conn is None or self._conn.closed:
            try:
                self._conn = connect(self.database_url)
            except psycopg.OperationalError as exc:
                raise TestkitError(
                    "cannot reach the local database. Is Docker running? Try: uv run ci up"
                ) from exc
        return self._conn

    def deps(self) -> ProcessingDeps:
        s = self.settings
        return ProcessingDeps(
            screener=make_screener(s.screener, unavailable=self.overrides().screening_down),
            classifier=make_classifier(s.classifier_backend),
            embedder=make_embedder(s.embedder, s.embedding_dimensions),
        )

    def connection(self) -> tuple[str, str]:
        with transaction(self.conn, "ci_ingest"):
            row = one(
                self.conn,
                "SELECT id, account_ref FROM source_connections"
                " WHERE provider = 'email' AND account_ref = %s",
                (CONNECTION_ACCOUNT,),
            )
        if row is None:
            raise TestkitError("the testkit is not seeded; run: uv run ci reset")
        return str(row["id"]), str(row["account_ref"])

    def corpus_id(self) -> str:
        with transaction(self.conn, "ci_ingest"):
            row = one_required(
                self.conn, "SELECT id FROM corpora WHERE corpus_key = %s", (CORPUS_KEY,)
            )
        return str(row["id"])

    def worker(self) -> EmailWorker:
        cid, ref = self.connection()
        return EmailWorker(self.conn, self.graph, self.blob, self.settings, self.deps(), cid, ref)

    # -- database lifecycle --------------------------------------------------

    def _pg_env(self) -> dict[str, str]:
        u = urlparse(self.database_url)
        env = dict(os.environ)
        env.update(
            {
                "PGHOST": u.hostname or "127.0.0.1",
                "PGPORT": str(u.port or 5432),
                "PGUSER": u.username or "postgres",
                "PGPASSWORD": u.password or "",
                "PGDATABASE": u.path.lstrip("/"),
            }
        )
        for candidate in ("/opt/homebrew/opt/libpq/bin", "/usr/local/opt/libpq/bin"):
            if shutil.which("psql", path=env.get("PATH")) is None and Path(candidate).exists():
                env["PATH"] = f"{candidate}:{env.get('PATH', '')}"
        if shutil.which("psql", path=env.get("PATH")) is None:
            raise TestkitError(
                "psql was not found. On a Mac: brew install libpq && brew link --force libpq"
            )
        return env

    def migrate(self) -> str:
        result = subprocess.run(  # noqa: S603
            [sys.executable, str(REPO_ROOT / "db" / "migrate.py"), "apply"],
            env=self._pg_env(),
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise TestkitError(f"migration failed:\n{result.stdout}{result.stderr}")
        return result.stdout.strip()

    def reset(self) -> RunReport:
        """Drop and recreate the local database and fixture mailboxes, then seed."""
        report = RunReport("Reset")
        if self._conn is not None:
            self._conn.close()
            self._conn = None
        u = urlparse(self.database_url)
        dbname = u.path.lstrip("/")
        if dbname in ("", "postgres") or u.hostname not in ("127.0.0.1", "localhost"):
            raise TestkitError("reset only works on a local, non-default database")
        admin_url = self.database_url.rsplit("/", 1)[0] + "/postgres"
        with psycopg.connect(admin_url, autocommit=True) as admin:
            admin.execute(f'DROP DATABASE IF EXISTS "{dbname}" WITH (FORCE)')
            admin.execute(f'CREATE DATABASE "{dbname}"')
        report.lines.append(self.migrate())
        for path in (self.data_dir / "blob", self.data_dir / "testkit_overrides.json"):
            if path.is_dir():
                shutil.rmtree(path)
            elif path.exists():
                path.unlink()
        report.lines.extend(self.seed().lines)
        return report

    def seed(self) -> RunReport:
        report = RunReport("Seed")
        delivered = self.store.reset_from_fixtures(FIXTURE_DIR)
        report.lines.append(f"Fixture mailboxes loaded: {delivered} synthetic message copies.")
        conn = self.conn
        control.set_mode(conn, "test", actor=ACTOR, notes="testkit seed")
        with transaction(conn, "ci_operator"):
            src = one_required(
                conn,
                "INSERT INTO source_connections (provider, account_ref, display_name,"
                " enabled) VALUES ('email', %s, 'Synthetic M365 tenant', true)"
                " RETURNING id",
                (CONNECTION_ACCOUNT,),
            )
            corpus = one_required(
                conn,
                "INSERT INTO corpora (corpus_key, description) VALUES (%s,"
                " 'Enrolled mailboxes') RETURNING id",
                (CORPUS_KEY,),
            )
            conn.execute(
                "INSERT INTO access_grants (corpus_id, principal_type, principal_id,"
                " granted_by) VALUES (%s, 'entra_app_role', %s, %s)",
                (corpus["id"], READER_ALL, ACTOR),
            )
        excluded = next(m for m in self.graph.list_mailboxes() if m.address == EXCLUDED_AT_SEED)
        control.add_exclusion(
            conn,
            "scope",
            actor=ACTOR,
            reason_code="fs_ci_exclude_attribute",
            scope_kind="mailbox",
            native_scope_id=excluded.id,
        )
        control.add_exclusion(
            conn,
            "participant_domain",
            actor=ACTOR,
            reason_code="testkit_demo",
            pattern=EXCLUDED_DOMAIN_AT_SEED,
        )
        inv = capture.sync_inventory(
            conn, self.graph, connection_id=str(src["id"]), corpus_id=str(corpus["id"])
        )
        boundary = self.settings.email_backfill_since
        for r in inv:
            if r.address in APPROVED_AT_SEED:
                control.set_scope_approval(
                    conn, r.scope_id, approved=True, actor=ACTOR, backfill_since=boundary
                )
        report.lines.append("Mode: test. Approved: " + ", ".join(APPROVED_AT_SEED) + ".")
        report.lines.append(
            f"Kept out: {EXCLUDED_AT_SEED} (scope exclusion + Exchange 403); "
            f"participant domain {EXCLUDED_DOMAIN_AT_SEED}."
        )
        report.lines.append("Not approved yet: leasing@finishes.example.")
        return report

    # -- pipeline steps --------------------------------------------------------

    def sync(self) -> RunReport:
        report = RunReport("1. Mailbox inventory")
        cid, _ = self.connection()
        try:
            results = capture.sync_inventory(
                self.conn, self.graph, connection_id=cid, corpus_id=self.corpus_id()
            )
        except GraphThrottled as exc:
            report.lines.append(
                f"Graph throttled the inventory (429); retry after {exc.retry_after_seconds}s."
            )
            return report
        for r in results:
            report.lines.append(
                f"{r.address}: {r.state}" + (f" ({r.reason_code})" if r.reason_code else "")
            )
        return report

    def reconcile(self) -> RunReport:
        report = RunReport("2. Check for new mail (per-folder delta)")
        cid, _ = self.connection()
        result = capture.reconcile(self.conn, self.graph, self.settings, connection_id=cid)
        if result.skipped:
            report.lines.append(
                {
                    "mode_paused": "Paused: nothing is read.",
                    "empty_scope": "No mailbox is in scope: nothing is read (fails closed).",
                }.get(result.skipped, result.skipped)
            )
        for m in result.mailboxes:
            line = (
                f"{m.address}: {m.folders_read} folders, {m.pages} pages, "
                f"{m.fetch_jobs} new/changed, {m.withdraw_jobs} removed"
            )
            if m.duplicates_collapsed:
                line += f", {m.duplicates_collapsed} duplicate admissions collapsed"
            if m.resynced_after_410:
                line += ", delta link expired (410): resynced from the backfill boundary"
            if m.stopped:
                line += f" - stopped: {m.stopped}"
            report.lines.append(line)
        return report

    def work(self) -> RunReport:
        report = RunReport("3. Process the queue")
        result: WorkerReport = run_until_idle(self.worker())
        if result.skipped:
            report.lines.append("Paused: jobs stay queued until the mode is back to test.")
        counts: dict[str, int] = {}
        for o in result.outcomes:
            key = f"{o.job_type} -> {o.result}"
            counts[key] = counts.get(key, 0) + 1
        report.lines.extend(f"{n} x {k}" for k, n in sorted(counts.items()))
        if not result.outcomes and not result.skipped:
            report.lines.append("Nothing ready in the queue.")
        return report

    def run_all(self) -> list[RunReport]:
        return [self.sync(), self.reconcile(), self.work()]

    def reprocess_all(self) -> RunReport:
        """Re-fetch every captured message, e.g. after changing the attachment policy."""
        report = RunReport("Reprocess everything")
        runtime = control.runtime_state(self.conn)
        if runtime is None or runtime.dataset_key is None:
            report.lines.append("Paused: nothing to do.")
            return report
        n = 0
        with transaction(self.conn, "ci_ingest"):
            for r in many(
                self.conn,
                "SELECT c.native_id, cs.scope_id, s.native_scope_id FROM communications c"
                " JOIN communication_scopes cs ON cs.communication_id = c.id"
                " JOIN source_scopes s ON s.id = cs.scope_id"
                " WHERE c.dataset_key = %s AND c.lifecycle_state = 'active'",
                (runtime.dataset_key,),
            ):
                if jobs.admit(
                    self.conn,
                    dataset_key=runtime.dataset_key,
                    config_revision=runtime.revision,
                    job_type=capture.FETCH,
                    dedupe_key=f"{r['scope_id']}:{r['native_id']}",
                    payload={
                        "scope_id": str(r["scope_id"]),
                        "mailbox_id": r["native_scope_id"],
                        "message_id": r["native_id"],
                    },
                ):
                    n += 1
        report.lines.append(f"{n} messages queued for re-fetch under the current settings.")
        report.lines.extend(self.work().lines)
        return report

    # -- mailbox actions (the fake Exchange) -------------------------------------

    def deliver(
        self,
        *,
        to_mailbox: str,
        from_name: str,
        from_address: str,
        subject: str,
        body: str,
        folder: str = "inbox",
        cc: str = "",
        inline_logo: bool = False,
        pdf_attachment: bool = False,
        received_at: datetime | None = None,
    ) -> str:
        when = (received_at or datetime.now(UTC)).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        state = self.store.read()
        names = {m["address"]: m["displayName"] for m in state["mailboxes"].values()}

        def recip(address: str) -> dict[str, Any]:
            address = address.strip().lower()
            return {"emailAddress": {"name": names.get(address, address), "address": address}}

        attachments: list[dict[str, Any]] = []
        if pdf_attachment:
            attachments.append(
                {
                    "id": "att-pdf",
                    "name": "Document.pdf",
                    "contentType": "application/pdf",
                    "size": 50210,
                    "isInline": False,
                }
            )
        if inline_logo:
            attachments.append(
                {
                    "id": "att-logo",
                    "name": "image001.png",
                    "contentType": "image/png",
                    "size": 6144,
                    "isInline": True,
                    "contentId": "image001.png@01DB1A2B.3C4D5E60",
                }
            )
        sender = {
            "emailAddress": {
                "name": from_name or from_address,
                "address": from_address.strip().lower(),
            }
        }
        message = {
            "subject": subject,
            "from": sender,
            "sender": sender,
            "toRecipients": [recip(to_mailbox)],
            "ccRecipients": [recip(a) for a in cc.split(",") if a.strip()],
            "bccRecipients": [],
            "receivedDateTime": when,
            "sentDateTime": when,
            "internetMessageId": f"<composed-{datetime.now().timestamp():.0f}@"
            f"{from_address.split('@')[-1]}>",
            "conversationId": f"AAQkAG-composed-{datetime.now().timestamp():.0f}",
            "body": {"contentType": "text", "content": body},
            "attachments": attachments,
        }
        return self.store.deliver(to_mailbox, folder, message)

    def presets(self) -> list[dict[str, Any]]:
        out = []
        for path in sorted((FIXTURE_DIR / "messages").glob("*.json")):
            doc = json.loads(path.read_text())
            out.append({"key": path.stem, **doc})
        return out

    # -- read models for the UI ------------------------------------------------

    def mailboxes(self) -> list[dict[str, Any]]:
        state = self.store.read()
        with transaction(self.conn, "ci_ingest"):
            scopes = {
                r["native_scope_id"]: r
                for r in many(
                    self.conn,
                    "SELECT s.id, s.native_scope_id, s.scope_state, s.state_reason_code,"
                    " s.test_approved, s.backfill_since, m.primary_smtp_address, m.recipient_type,"
                    " EXISTS (SELECT 1 FROM exclusions e WHERE e.exclusion_kind = 'scope'"
                    "  AND e.revoked_at IS NULL"
                    "  AND e.native_scope_id = s.native_scope_id) AS excluded"
                    " FROM source_scopes s JOIN email_mailboxes m ON m.scope_id = s.id",
                )
            }
        out = []
        for mbx in state["mailboxes"].values():
            s = scopes.get(mbx["id"])
            capturable = bool(
                s
                and s["scope_state"] == "enrolled"
                and s["test_approved"]
                and not s["excluded"]
                and s["backfill_since"]
            )
            out.append({"fixture": mbx, "scope": s, "capturable": capturable})
        return out

    def board(self) -> dict[str, list[dict[str, Any]]]:
        state = self.store.read()
        with transaction(self.conn, "ci_ingest"):
            comms = {
                r["native_id"]: r
                for r in many(
                    self.conn,
                    """
                SELECT c.id, c.native_id, c.lifecycle_state, v.processing_state, v.hold_reason_code,
                       r.outcome, r.outcome_reasons
                FROM communications c
                LEFT JOIN LATERAL (SELECT * FROM communication_versions cv
                                   WHERE cv.communication_id = c.id
                                   ORDER BY cv.version_no DESC LIMIT 1) v ON true
                LEFT JOIN classification_runs r ON r.version_id = v.id
                """,
                )
            }
            job_rows = many(
                self.conn,
                "SELECT payload->>'message_id' AS mid, job_type, state, last_error_code"
                " FROM jobs WHERE payload ? 'message_id' ORDER BY id",
            )
            process_jobs = {
                r["mid"]: r
                for r in many(
                    self.conn,
                    "SELECT c.native_id AS mid, j.state, j.last_error_code FROM jobs j"
                    " JOIN communication_versions v ON v.id = (j.payload->>'version_id')::uuid"
                    " JOIN communications c ON c.id = v.communication_id"
                    " WHERE j.job_type = 'process_version' ORDER BY j.id",
                )
            }
        jobs_by_mid: dict[str, list[dict[str, Any]]] = {}
        for j in job_rows:
            jobs_by_mid.setdefault(j["mid"], []).append(j)
        visible = self._visible_ids()
        mbx_status = {m["fixture"]["id"]: m for m in self.mailboxes()}
        lanes: dict[str, list[dict[str, Any]]] = {k: [] for k in LANES}
        for mid, entry in state["messages"].items():
            comm = comms.get(mid)
            if entry["deleted"] and comm is None:
                continue
            msg = entry["message"]
            card = {
                "id": mid,
                "subject": msg.get("subject") or "(no subject)",
                "from": (msg.get("from") or {}).get("emailAddress", {}).get("address"),
                "mailbox": state["mailboxes"][entry["mailbox_id"]]["address"],
                "folder": self.store.folder_name(state, entry["mailbox_id"], msg["parentFolderId"]),
                "received": msg.get("receivedDateTime", "")[:16].replace("T", " "),
                "reason": "",
            }
            lane, reason = self._lane(
                entry,
                comm,
                jobs_by_mid.get(mid, []),
                process_jobs.get(mid),
                visible,
                mbx_status,
                state,
            )
            card["reason"] = reason
            lanes[lane].append(card)
        for cards in lanes.values():
            cards.sort(key=lambda c: c["received"], reverse=True)
        return lanes

    def _visible_ids(self) -> set[str]:
        with transaction(self.conn, "ci_search"):
            return {
                r["native_id"]
                for r in many(self.conn, "SELECT native_id FROM published_communications")
            }

    def _lane(
        self,
        entry: dict[str, Any],
        comm: dict[str, Any] | None,
        fetch_jobs: list[dict[str, Any]],
        process_job: dict[str, Any] | None,
        visible: set[str],
        mbx_status: dict[str, dict[str, Any]],
        state: dict[str, Any],
    ) -> tuple[str, str]:
        last = fetch_jobs[-1] if fetch_jobs else None
        if comm is not None and comm["lifecycle_state"] == "deleted_at_source":
            return "withdrawn", "deleted or moved to a skipped folder"
        for j in (last, process_job):
            if j and j["state"] == "dead":
                return "failed", j["last_error_code"] or ""
            if j and j["state"] in ("queued", "leased"):
                return "queued", (
                    f"retrying: {j['last_error_code']}"
                    if j["last_error_code"]
                    else "waiting for the worker"
                )
        if comm is None:
            if last and last["state"] == "cancelled":
                code = last["last_error_code"] or ""
                if code.startswith("participant"):
                    return "excluded", code
                return "not_read", code
            return "not_read", self._why_not_read(entry, mbx_status, state)
        vstate, outcome = comm["processing_state"], comm["outcome"]
        reason = comm["hold_reason_code"] or ", ".join(comm["outcome_reasons"] or [])
        if vstate == "published":
            if comm["native_id"] not in visible:
                return "hidden", "mailbox un-approved/excluded, participant excluded, or paused"
            return ("flagged", reason) if outcome == "review" else ("searchable", "")
        if vstate == "held":
            return ("restricted" if (reason or "").startswith("restricted") else "held"), reason
        if vstate == "rejected":
            return "not_indexed", reason
        return "queued", f"version is {vstate}"

    def _why_not_read(
        self, entry: dict[str, Any], mbx_status: dict[str, dict[str, Any]], state: dict[str, Any]
    ) -> str:
        m = mbx_status.get(entry["mailbox_id"])
        scope = m["scope"] if m else None
        if scope is None:
            return "mailbox not in inventory yet"
        if scope["excluded"]:
            return "mailbox excluded (kept out)"
        if scope["scope_state"] != "enrolled":
            return f"mailbox state: {scope['scope_state']}"
        if not scope["test_approved"]:
            return "mailbox not approved"
        msg = entry["message"]
        folder = state["mailboxes"][entry["mailbox_id"]]["folders"].get(msg["parentFolderId"], {})
        wkn = folder.get("wellKnownName")
        if wkn in SKIPPED_FOLDERS or (wkn == "sentitems" and not self.settings.email_include_sent):
            return f"skipped folder: {folder.get('displayName')}"
        since = scope["backfill_since"]
        received = datetime.fromisoformat(msg["receivedDateTime"].replace("Z", "+00:00"))
        if since is None:
            return "no backfill boundary set"
        if received < since:
            return f"before the backfill boundary ({since:%Y-%m-%d})"
        return "not read yet: press Run everything"

    def trace(self, message_id: str) -> dict[str, Any]:
        state = self.store.read()
        entry = state["messages"].get(message_id)
        if entry is None:
            raise KeyError(message_id)
        mailbox = state["mailboxes"][entry["mailbox_id"]]
        msg = entry["message"]
        t: dict[str, Any] = {
            "id": message_id,
            "entry": entry,
            "mailbox": mailbox,
            "message": msg,
            "folder": self.store.folder_name(state, entry["mailbox_id"], msg["parentFolderId"]),
            "folders": [f["displayName"] for f in mailbox["folders"].values()],
        }
        scope = next((m for m in self.mailboxes() if m["fixture"]["id"] == mailbox["id"]), None)
        t["scope"] = scope
        conn = self.conn
        with transaction(conn, "ci_ingest"):
            t["jobs"] = many(
                conn,
                "SELECT id, job_type, state, attempts, last_error_code, priority,"
                " created_at, updated_at FROM jobs WHERE payload->>'message_id' = %s"
                " ORDER BY id",
                (message_id,),
            )
            t["audit"] = many(
                conn,
                "SELECT occurred_at, action, details FROM audit_events"
                " WHERE target_id = %s ORDER BY id",
                (message_id,),
            )
            comm = one(conn, "SELECT * FROM communications WHERE native_id = %s", (message_id,))
            t["communication"] = comm
            t["versions"] = []
            if comm is not None:
                t["email"] = one(
                    conn, "SELECT * FROM email_messages WHERE communication_id = %s", (comm["id"],)
                )
                t["participants"] = many(
                    conn,
                    "SELECT cp.role, p.email_normalized, p.display_name"
                    " FROM communication_participants cp JOIN participants p"
                    " ON p.id = cp.participant_id WHERE cp.communication_id = %s"
                    " ORDER BY cp.role",
                    (comm["id"],),
                )
                t["duplicates"] = many(
                    conn,
                    "SELECT m.primary_smtp_address, r.provenance FROM relationships r"
                    " JOIN email_messages em ON em.communication_id ="
                    "  CASE WHEN r.from_communication_id = %(c)s THEN r.to_communication_id"
                    "  ELSE r.from_communication_id END"
                    " JOIN email_mailboxes m ON m.scope_id = em.mailbox_scope_id"
                    " WHERE r.kind = 'possible_duplicate' AND %(c)s IN"
                    " (r.from_communication_id, r.to_communication_id)",
                    {"c": comm["id"]},
                )
                for v in many(
                    conn,
                    "SELECT * FROM communication_versions WHERE communication_id = %s"
                    " ORDER BY version_no DESC",
                    (comm["id"],),
                ):
                    v["decisions"] = many(
                        conn,
                        "SELECT check_name, decision, reason_code,"
                        " service_ref, decided_at FROM policy_decisions"
                        " WHERE version_id = %s ORDER BY id",
                        (v["id"],),
                    )
                    run = one(
                        conn,
                        "SELECT * FROM classification_runs WHERE version_id = %s"
                        " ORDER BY created_at DESC LIMIT 1",
                        (v["id"],),
                    )
                    v["classification"] = run
                    v["labels"] = (
                        []
                        if run is None
                        else many(
                            conn,
                            "SELECT label, probability, no_threshold, yes_threshold, decision"
                            " FROM classification_labels WHERE run_id = %s",
                            (run["id"],),
                        )
                    )
                    order = list(LABELS_BY_NAME)
                    v["labels"].sort(key=lambda r: order.index(r["label"]))
                    v["attachments"] = many(
                        conn,
                        "SELECT name, content_type, size_bytes, is_inline,"
                        " content_id, status FROM email_attachments"
                        " WHERE version_id = %s",
                        (v["id"],),
                    )
                    v["chunks"] = many(
                        conn,
                        "SELECT seq, display_text, embedding_model,"
                        " embedding_input_sha256 FROM chunks"
                        " WHERE version_id = %s ORDER BY seq",
                        (v["id"],),
                    )
                    t["versions"].append(v)
        t["visible"] = comm is not None and message_id in self._visible_ids()
        latest = t["versions"][0] if t["versions"] else None
        decisions = {r["label"]: r["decision"] for r in (latest["labels"] if latest else [])}
        outcome = (
            latest["classification"]["outcome"] if latest and latest["classification"] else None
        )
        if latest and latest["processing_state"] != "published":
            outcome = None
        t["routing"] = preview(outcome, decisions)
        board = self.board()
        t["lane"] = next(
            (
                (lane, card["reason"])
                for lane, cards in board.items()
                for card in cards
                if card["id"] == message_id
            ),
            ("not_read", ""),
        )
        return t

    def search(self, query: str, persona: str) -> SearchResponse:
        roles = {"executive": (READER_ALL,), "no_role": ()}.get(persona, ())
        caller = Caller(oid=f"local-{persona}", roles=roles)
        return search(self.conn, caller, query, self.deps().embedder)

    def queue(self) -> list[dict[str, Any]]:
        with transaction(self.conn, "ci_ingest"):
            return many(
                self.conn,
                "SELECT job_type, state, count(*) AS n FROM jobs"
                " GROUP BY job_type, state ORDER BY job_type, state",
            )

    def mode(self) -> str:
        rt = control.runtime_state(self.conn)
        return "none" if rt is None else rt.mode
