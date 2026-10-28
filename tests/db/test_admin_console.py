"""The admin console API: who gets in, what each page returns, and that content
comes only through the caller's own grants. Tokens are signed with a throwaway
RSA key standing in for Entra's; everything else is the production code path.
"""

import time
from collections.abc import Iterator
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from starlette.testclient import TestClient

from ci_admin.api import ADMIN_ROLE, ADMIN_SCOPE, build_admin_app
from ci_admin.lab import LabBackends
from ci_cli.seed import seed
from ci_core.db import AppRole, Connection, connect
from ci_core.settings import Settings
from ci_email.capture import capture_window
from ci_email.fixture import FixtureGraphClient
from ci_email.process import (
    Backends,
    process_all,
    requeue_failed_attachments,
    store_missing_originals,
)
from ci_processing.classification import FakeClassifier
from ci_processing.embedding import FakeEmbedder
from ci_processing.originals import DirectoryStore
from ci_processing.routing import FakePersonRouter
from ci_processing.screening import FakeScreener
from ci_retrieval.entra import EntraTokenVerifier
from ci_retrieval.identity import READER_ALL

TENANT = "11111111-1111-1111-1111-111111111111"
API_ID = "22222222-2222-2222-2222-222222222222"
API_URI = f"api://{API_ID}"
BASE = "https://func-fs-ci-adm-dev.azurewebsites.net"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PAT = "pat.example@finishessolutions.example"


class StaticKeys:
    def get_signing_key_from_jwt(self, token: str) -> SimpleNamespace:
        return SimpleNamespace(key=KEY.public_key())


def token(**overrides: Any) -> str:
    now = int(time.time())
    claims = {
        "iss": f"https://login.microsoftonline.com/{TENANT}/v2.0",
        "aud": API_ID,
        "tid": TENANT,
        "oid": "matthew-oid",
        "preferred_username": "Matthew@FinishesSolutions.example",
        "scp": ADMIN_SCOPE,
        "roles": [READER_ALL, ADMIN_ROLE],
        "azp": "admin-spa",
        "iat": now,
        "exp": now + 3600,
    } | overrides
    return jwt.encode({k: v for k, v in claims.items() if v is not None}, KEY, algorithm="RS256")


def auth(**overrides: Any) -> dict[str, str]:
    return {"Authorization": f"Bearer {token(**overrides)}"}


@pytest.fixture(scope="module")
def files(tmp_path_factory: pytest.TempPathFactory) -> DirectoryStore:
    return DirectoryStore(tmp_path_factory.mktemp("originals"))


@pytest.fixture(scope="module")
def database_ready(database: str, files: DirectoryStore) -> Iterator[str]:
    def connector(role: AppRole | None = None) -> Connection:
        return connect(dbname=database, role=role)

    with connector() as conn:
        seed(conn, "pytest")
        conn.commit()
    graph = FixtureGraphClient.from_directory()
    capture_window(
        connector,
        graph,
        hours=744,
        include_sent=True,
        profile={"p": 1},
        now=datetime(2026, 10, 28, tzinfo=UTC),
    )
    process_all(
        lambda: connector("ci_ingest"),
        Backends(
            FakeScreener(),
            FakeClassifier(),
            FakeEmbedder(),
            graph=graph,
            extraction_enabled=True,
            originals=files,
        ),
        workers=1,
    )
    yield database


@pytest.fixture
def client(database_ready: str, files: DirectoryStore) -> Iterator[TestClient]:
    settings = Settings(
        deployment_environment="development",
        attachment_unsupported_policy="publish_partial",
        entra_tenant_id=TENANT,
        search_api_client_id=API_ID,
        search_api_app_id_uri=API_URI,
        admin_spa_client_id="admin-spa",
    )
    app = build_admin_app(
        settings,
        FakeEmbedder(),
        verifier=EntraTokenVerifier(
            TENANT, API_ID, API_URI, keys=StaticKeys(), required_scope=ADMIN_SCOPE
        ),
        connector=lambda role: connect(dbname=database_ready, role=role),  # type: ignore[arg-type]
        lab_backends=lambda: LabBackends(
            FakeScreener(), FakeClassifier(), router=FakePersonRouter()
        ),
        originals=files,
    )
    with TestClient(app, base_url=BASE) as c:
        yield c


def latest_audit(database: str, action: str) -> dict[str, Any] | None:
    with connect(dbname=database) as conn:
        return conn.execute(
            "SELECT actor_id, details FROM audit_events WHERE action = %s ORDER BY id DESC LIMIT 1",
            (action,),
        ).fetchone()


@pytest.mark.parametrize(
    ("headers", "status"),
    [
        ({}, 401),
        ({"Authorization": "Basic abc"}, 401),
        ({"Authorization": "Bearer not-a-token"}, 401),
        (auth(scp="Communications.Search"), 401),  # an MCP client token is not an admin token
        (auth(aud="some-other-api"), 401),
        (auth(roles=[READER_ALL]), 403),  # signed in, but not an admin
        (auth(), 200),
    ],
)
def test_only_admins_get_in(client: TestClient, headers: dict[str, str], status: int) -> None:
    assert client.get("/api/overview", headers=headers).status_code == status


def test_page_and_sign_in_config_are_public_and_locked_down(client: TestClient) -> None:
    page = client.get("/")
    assert page.status_code == 200 and "app.js" in page.text
    csp = page.headers["content-security-policy"]
    assert "script-src 'self'" in csp and "frame-ancestors 'none'" in csp
    assert page.headers["x-content-type-options"] == "nosniff"
    # Every console page is a plain path serving the same locked-down page.
    for path in ("/overview", "/reports", "/messages?q=invoice", "/review", "/check"):
        same = client.get(path)
        assert same.status_code == 200 and same.text == page.text, path
        assert same.headers["content-security-policy"] == csp
    assert client.get("/not-a-page").status_code == 404
    config = client.get("/config.json").json()
    assert config == {
        "tenant_id": TENANT,
        "client_id": "admin-spa",
        "scope": f"{API_URI}/{ADMIN_SCOPE}",
        "environment": "development",
    }
    assert client.get("/api/overview").headers["www-authenticate"].startswith("Bearer")


def test_overview_counts(client: TestClient) -> None:
    data = client.get("/api/overview", headers=auth()).json()
    states = {v["processing_state"] for v in data["versions"]}
    assert "published" in states and "rejected" in states
    assert data["mode"]["mode"] == "test" and data["mode"]["eligible_mailboxes"] > 0
    assert data["schedule"]["capture_enabled"] is False
    assert any(lb["label"] == "invoice_receipt" for lb in data["labels"])


def test_schedule_changes_are_validated_and_audited(
    client: TestClient, database_ready: str
) -> None:
    bad = client.put("/api/schedule", headers=auth(), json={"capture_interval_min": 7})
    assert bad.status_code == 400
    narrow = client.put(
        "/api/schedule",
        headers=auth(),
        json={"capture_interval_min": 240, "capture_window_hours": 1},
    )
    assert narrow.status_code == 400 and "window must cover" in narrow.json()["error"]
    unknown = client.put("/api/schedule", headers=auth(), json={"last_capture_started_at": None})
    assert unknown.status_code == 400
    ok = client.put(
        "/api/schedule",
        headers=auth(),
        json={"capture_enabled": True, "capture_interval_min": 60, "capture_window_hours": 2},
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["schedule"]["capture_enabled"] is True
    row = latest_audit(database_ready, "schedule_change")
    assert row is not None and row["actor_id"] == "matthew@finishessolutions.example"
    assert row["details"]["capture_enabled"] is True
    run = client.post("/api/runs/capture", headers=auth(), json={"hours": 24})
    assert run.status_code == 200
    schedule = client.get("/api/schedule", headers=auth()).json()
    assert schedule["schedule"]["run_requested_hours"] == 24
    assert schedule["next"] == "run requested"
    client.put("/api/schedule", headers=auth(), json={"capture_enabled": False})


def test_mailbox_scope_actions(client: TestClient, database_ready: str) -> None:
    listed = client.get("/api/mailboxes", headers=auth()).json()
    address = next(m["address"] for m in listed["mailboxes"] if not m["excluded"])
    bad = client.post(
        "/api/mailboxes/exclude", headers=auth(), json={"address": address, "reason": "No Way!"}
    )
    assert bad.status_code == 400
    done = client.post(
        "/api/mailboxes/exclude", headers=auth(), json={"address": address, "reason": "privacy"}
    )
    assert done.status_code == 200
    after = client.get("/api/mailboxes", headers=auth()).json()
    assert next(m for m in after["mailboxes"] if m["address"] == address)["excluded"] is True
    assert latest_audit(database_ready, "scope_exclude") is not None
    client.post("/api/mailboxes/unexclude", headers=auth(), json={"address": address})
    production = client.post(
        "/api/mailboxes/include", headers=auth(), json={"address": address, "dataset": "production"}
    )
    assert production.status_code == 400  # needs confirm = 'production'
    assert client.post("/api/mailboxes/explode", headers=auth(), json={}).status_code == 404


def test_messages_detail_and_attachments(client: TestClient) -> None:
    listed = client.get("/api/messages?since_days=0", headers=auth()).json()
    assert listed["results"], listed
    with_attachment = client.get("/api/messages?q=invoice&since_days=0", headers=auth()).json()[
        "results"
    ]
    assert with_attachment
    detail = client.get(
        f"/api/messages/{with_attachment[0]['communication_id']}", headers=auth()
    ).json()
    assert {"participants", "attachments", "text", "web_link"} <= detail.keys()
    missing = client.get("/api/messages/00000000-0000-0000-0000-000000000000", headers=auth())
    assert missing.status_code == 404
    # An admin without a reader role sees no mail at all: the console uses the caller's grants.
    blind = client.get("/api/messages?since_days=0", headers=auth(roles=[ADMIN_ROLE])).json()
    assert blind["results"] == []


def test_unpublished_is_metadata_only(client: TestClient) -> None:
    data = client.get("/api/unpublished", headers=auth()).json()
    assert data["items"]
    assert any(i["hold_reason_code"].startswith("restricted") for i in data["items"])
    for item in data["items"]:
        assert "title" not in item and "subject" not in item


def test_actionable_matches_the_mcp_tool(client: TestClient) -> None:
    data = client.get(f"/api/actionable?person={PAT}&hours=1440", headers=auth()).json()
    assert "Invoice 4471 for Lot 12 grading - payment due Friday" in {
        a["subject"] for a in data["actions"]
    }


def test_lab_routes_without_storing(client: TestClient, database_ready: str) -> None:
    invoice = client.post(
        "/api/lab/classify",
        headers=auth(),
        json={
            "subject": "Invoice 9001 for Lot 4 framing",
            "sender": "billing@vendor.example",
            "to": PAT,
            "body": "Please review the attached invoice and confirm approval by Friday.",
            "person": PAT,
        },
    ).json()
    assert invoice["routing"]["searchable"] is True
    assert invoice["routing"]["action_item"] is True
    assert "invoice_receipt" in invoice["yes_labels"]
    restricted = client.post(
        "/api/lab/classify",
        headers=auth(),
        json={"subject": "Payroll", "body": "Employee SSN 123-45-6789", "person": PAT},
    ).json()
    assert restricted["routing"]["searchable"] is False
    assert restricted["routing"]["disposition"] == "restricted"
    assert client.post("/api/lab/classify", headers=auth(), json={}).status_code == 400
    row = latest_audit(database_ready, "lab_classify")
    assert row is not None and set(row["details"]) == {"input_sha256"}


def test_lab_reruns_a_stored_message(client: TestClient) -> None:
    target = client.get(f"/api/actionable?person={PAT}&hours=1440", headers=auth()).json()
    communication_id = target["actions"][0]["communication_id"]
    result = client.post(
        "/api/lab/reclassify",
        headers=auth(),
        json={"communication_id": communication_id, "person": PAT},
    ).json()
    assert result["changed_labels"] == []  # same fake classifier, same answer
    assert result["routing"]["action_item"] is True
    missing = client.post(
        "/api/lab/reclassify",
        headers=auth(),
        json={"communication_id": "00000000-0000-0000-0000-000000000000"},
    )
    assert missing.status_code == 404


def test_requeue_failed(client: TestClient, database_ready: str) -> None:
    response = client.post("/api/requeue-failed", headers=auth())
    assert response.status_code == 200 and response.json()["requeued"] == 0
    assert latest_audit(database_ready, "requeue_failed") is not None


def invoice_detail(client: TestClient) -> dict[str, Any]:
    found = client.get("/api/messages?q=invoice%204471&since_days=0", headers=auth()).json()
    first = found["results"][0]["communication_id"]
    detail: dict[str, Any] = client.get(f"/api/messages/{first}", headers=auth()).json()
    return detail


def test_original_files_are_served_only_for_authorized_messages(client: TestClient) -> None:
    detail = invoice_detail(client)
    pdf = next(a for a in detail["attachments"] if a["name"] == "Invoice-4471.pdf")
    assert pdf["file_id"]
    # The signature image is neither kept nor listed.
    assert [a["name"] for a in detail["attachments"]] == ["Invoice-4471.pdf"]
    url = f"/api/messages/{detail['communication_id']}/files/{pdf['file_id']}"
    response = client.get(url, headers=auth())
    assert response.status_code == 200 and response.content.startswith(b"%PDF")
    assert response.headers["content-type"] == "application/pdf"
    assert response.headers["content-disposition"].startswith("attachment;")
    assert response.headers["content-security-policy"] == "sandbox; default-src 'none'"
    assert client.get(url).status_code == 401
    assert client.get(url, headers=auth(roles=[READER_ALL])).status_code == 403
    # An admin with no reader role, a hash from another message and a malformed id: 404.
    assert client.get(url, headers=auth(roles=[ADMIN_ROLE])).status_code == 404
    other = client.get("/api/messages?since_days=0", headers=auth()).json()["results"]
    elsewhere = next(r for r in other if r["communication_id"] != detail["communication_id"])
    wrong = f"/api/messages/{elsewhere['communication_id']}/files/{pdf['file_id']}"
    assert client.get(wrong, headers=auth()).status_code == 404
    bad = f"/api/messages/{detail['communication_id']}/files/..%2F..%2Fetc"
    assert client.get(bad, headers=auth()).status_code == 404


def test_missing_originals_are_backfilled(database_ready: str, files: DirectoryStore) -> None:
    def ingest() -> Connection:
        return connect(dbname=database_ready, role="ci_ingest")

    with connect(dbname=database_ready) as conn:
        cleared = conn.execute(
            "UPDATE email_attachments SET original_stored_at = NULL"
            " WHERE original_stored_at IS NOT NULL RETURNING sha256"
        ).fetchall()
        conn.commit()
    assert cleared
    for path in files.root.rglob("*"):
        if path.is_file():
            path.unlink()
    counts = store_missing_originals(
        ingest,
        Backends(
            FakeScreener(),
            FakeClassifier(),
            FakeEmbedder(),
            graph=FixtureGraphClient.from_directory(),
            extraction_enabled=True,
            originals=files,
        ),
        limit=500,
    )
    assert counts["originals_backfilled"] >= 1 and not counts["original_download_failed"]
    assert all(files.get(r["sha256"]) is not None for r in cleared if r["sha256"])


def test_failed_attachments_are_reread_and_messages_republished(
    database_ready: str, files: DirectoryStore
) -> None:
    def ingest() -> Connection:
        return connect(dbname=database_ready, role="ci_ingest")

    with connect(dbname=database_ready) as conn:
        conn.execute(
            "UPDATE email_attachments SET extraction_status = 'failed'"
            " WHERE name = 'Invoice-4471.pdf'"
        )
        conn.commit()
    with ingest() as conn:
        requeued = requeue_failed_attachments(conn)
        conn.commit()
    assert requeued >= 1
    counts = process_all(
        ingest,
        Backends(
            FakeScreener(),
            FakeClassifier(),
            FakeEmbedder(),
            graph=FixtureGraphClient.from_directory(),
            extraction_enabled=True,
            originals=files,
        ),
        workers=1,
    )
    assert counts.get("failed", 0) == 0 and sum(counts.values()) == requeued
    with connect(dbname=database_ready) as conn:
        rows = conn.execute(
            "SELECT a.extraction_status, v.processing_state,"
            " (SELECT count(*) FROM chunks c WHERE c.version_id = v.id"
            "   AND c.chunk_kind = 'attachment_text') AS attachment_chunks"
            " FROM email_attachments a JOIN communication_versions v ON v.id = a.version_id"
            " WHERE a.name = 'Invoice-4471.pdf'"
        ).fetchall()
    assert rows and all(
        r["extraction_status"] == "extracted"
        and r["processing_state"] == "published"
        and r["attachment_chunks"] > 0
        for r in rows
    )


# ----- reports, risk analysis and label review --------------------------------


def report(client: TestClient, **body: Any) -> Any:
    return client.post("/api/reports/preview", headers=auth(), json=body)


def test_report_rows_carry_no_email_text(client: TestClient) -> None:
    data = report(client, dataset="messages", columns=["received_at", "subject", "labels"]).json()
    assert data["total"] >= len(data["rows"]) > 0
    assert [c["key"] for c in data["columns"]] == ["received_at", "subject", "labels"]
    for row in data["rows"]:
        assert not {"text", "body", "excerpts"} & row.keys()
    # The admin role alone reads no mail: reports use the caller's own grants.
    blind = client.post(
        "/api/reports/preview", headers=auth(roles=[ADMIN_ROLE]), json={"dataset": "messages"}
    ).json()
    assert blind["total"] == 0 and blind["rows"] == []


def test_report_filters_and_groups(client: TestClient) -> None:
    invoices = report(client, dataset="messages", label="invoice_receipt").json()
    assert invoices["rows"] and all("invoice_receipt" in r["labels"] for r in invoices["rows"])
    by_label = report(client, dataset="messages", group_by="label").json()
    keys = {r["key"] for r in by_label["rows"]}
    assert "invoice_receipt" in keys and by_label["columns"][0]["title"] == "Label"
    by_week = report(client, dataset="messages", group_by="week").json()
    assert (
        by_week["rows"]
        and sum(r["messages"] for r in by_week["rows"])
        == report(client, dataset="messages").json()["total"]
    )
    future = report(client, dataset="messages", since="2030-01-01").json()
    assert future["total"] == 0
    for bad in (
        {"dataset": "everything"},
        {"dataset": "runs", "group_by": "week"},
        {"dataset": "messages", "group_by": "subject"},
        {"dataset": "messages", "since": "yesterday"},
        {"dataset": "messages", "since": "2026-10-28", "until": "2026-10-01"},
    ):
        assert report(client, **bad).status_code == 400, bad


def test_report_exports_are_files_and_audited(client: TestClient, database_ready: str) -> None:
    csv_file = client.post(
        "/api/reports/export",
        headers=auth(),
        json={"dataset": "risks", "format": "csv", "label_mode": "names"},
    )
    assert csv_file.status_code == 200
    assert csv_file.headers["content-disposition"].startswith("attachment;")
    assert csv_file.headers["content-security-policy"] == "sandbox; default-src 'none'"
    text = csv_file.content.decode("utf-8-sig")
    assert text.splitlines()[0].startswith("Received,Mailbox,Sender,Subject,Labels")
    assert "Project Risk" in text and "project_risk" not in text
    row = latest_audit(database_ready, "report_export")
    assert row is not None and row["details"]["dataset"] == "risks"
    assert len(row["details"]["result_ids"]) == int(csv_file.headers["x-report-rows"]) > 0
    xlsx = client.post(
        "/api/reports/export",
        headers=auth(),
        json={"dataset": "messages", "group_by": "sender_domain", "format": "xlsx"},
    )
    assert xlsx.status_code == 200 and xlsx.content.startswith(b"PK")
    for dataset in ("runs", "mailboxes", "review_metrics"):
        ops_file = client.post(
            "/api/reports/export", headers=auth(), json={"dataset": dataset, "format": "csv"}
        )
        assert ops_file.status_code == 200, dataset
    bad = client.post("/api/reports/export", headers=auth(), json={"format": "pdf"})
    assert bad.status_code == 400
    assert client.post("/api/reports/export", json={"format": "csv"}).status_code == 401


def test_risk_overview(client: TestClient, database_ready: str) -> None:
    data = client.get("/api/risks?days=366", headers=auth()).json()
    assert data["total"] == len(data["items"]) > 0
    assert all("project_risk" in i["labels"] for i in data["items"])
    assert sum(s["risks"] for s in data["series"]) == data["total"]
    assert all(c["label"].startswith("cat_") for c in data["by_category"])
    assert data["by_property"] and all(p["risks"] > 0 for p in data["by_property"])
    row = latest_audit(database_ready, "risk_overview")
    assert row is not None and len(row["details"]["result_ids"]) == data["total"]
    blind = client.get("/api/risks?days=366", headers=auth(roles=[ADMIN_ROLE])).json()
    assert blind["total"] == 0 and blind["items"] == []


def test_reports_by_property_and_action_items_match_the_page(client: TestClient) -> None:
    by_property = report(client, dataset="messages", group_by="property").json()
    assert by_property["rows"] and by_property["columns"][0]["title"] == "Property / Project"
    named = report(client, dataset="messages", columns=["subject", "properties_ai"]).json()
    assert any(r["properties_ai"] for r in named["rows"])
    # The Action Items report is the same list as the Action Items page.
    page = client.get(f"/api/actionable?person={PAT}&hours=744", headers=auth()).json()
    rows = report(client, dataset="actions", person=PAT).json()["rows"]
    assert page["actions"]
    assert sorted(r["subject"] for r in rows) == sorted(a["subject"] for a in page["actions"])


def test_labels_have_titles(client: TestClient) -> None:
    labels = client.get("/api/labels", headers=auth()).json()["labels"]
    titles = {lb["name"]: lb["title"] for lb in labels}
    assert titles["cat_payment_billing"] == "Payment"
    assert all(titles.values())


def test_label_feedback_round_trip(client: TestClient, database_ready: str) -> None:
    target = invoice_detail(client)["communication_id"]
    url = f"/api/feedback/{target}"
    first = client.get(url, headers=auth()).json()
    assert first["email"]["subject"] and first["labels"] == {} and first["action_for"] == {}
    assert "fyi_only" in first["reasons"]
    assert {s["label"] for s in first["email"]["scores"]} >= {"spam", "action_required"}
    saved = client.put(
        url, headers=auth(), json={"kind": "label", "subject": "spam", "applies": False}
    ).json()
    assert saved["labels"] == {"spam": False}
    saved = client.put(
        url,
        headers=auth(),
        json={"kind": "action_for", "subject": PAT, "applies": False, "reason": "fyi_only"},
    ).json()
    assert saved["action_for"] == {PAT: {"applies": False, "reason": "fyi_only"}}
    cleared = client.put(
        url, headers=auth(), json={"kind": "label", "subject": "spam", "applies": None}
    ).json()
    assert cleared["labels"] == {}
    for bad in (
        {"kind": "label", "subject": "made_up", "applies": True},
        {"kind": "action_for", "subject": PAT, "applies": False, "reason": "because"},
        {"kind": "other", "subject": "spam", "applies": True},
    ):
        assert client.put(url, headers=auth(), json=bad).status_code == 400
    missing = "/api/feedback/00000000-0000-0000-0000-000000000000"
    assert client.get(missing, headers=auth()).status_code == 404
    # An admin without a reader role can't judge (or see) mail.
    assert client.get(url, headers=auth(roles=[ADMIN_ROLE])).status_code == 404
    with connect(dbname=database_ready) as conn:
        rows = conn.execute(
            "SELECT subject_kind, reviewer FROM label_feedback WHERE communication_id = %s",
            (target,),
        ).fetchall()
    assert [(r["subject_kind"], r["reviewer"]) for r in rows] == [
        ("action_for", "matthew@finishessolutions.example")
    ]


def test_review_queue_skips_judged_emails(client: TestClient) -> None:
    query = f"/api/review/next?source=actionable&person={PAT}"
    before = client.get(query, headers=auth()).json()
    assert before["communication_id"] and before["in_source"] >= 1
    client.put(
        f"/api/feedback/{before['communication_id']}",
        headers=auth(),
        json={"kind": "label", "subject": "urgent", "applies": True},
    )
    after = client.get(query, headers=auth()).json()
    assert after["remaining"] == before["remaining"] - 1
    assert after["communication_id"] != before["communication_id"]
    for source in ("recent", "risks", "label&label=invoice_receipt"):
        assert client.get(f"/api/review/next?source={source}", headers=auth()).status_code == 200


def test_calibration_report_shape(client: TestClient) -> None:
    report = client.get("/api/calibration", headers=auth()).json()
    assert {lb["label"] for lb in report["labels"]} >= {"spam", "project_risk"}
    assert report["min_sample"] == 20 and isinstance(report["actions"], list)
    urgent = next(lb for lb in report["labels"] if lb["label"] == "urgent")
    assert urgent["judged"] >= 1 and urgent["suggestion"] is None


def test_people_profiles_and_lab_person_check(client: TestClient, database_ready: str) -> None:
    sam = "sam.new@finishessolutions.example"  # not in the fixture lists used above
    bad = client.put(
        "/api/people", headers=auth(), json={"key": "Sam!", "display_name": "Sam", "addresses": sam}
    )
    assert bad.status_code == 400
    saved = client.put(
        "/api/people",
        headers=auth(),
        json={
            "key": "sam",
            "display_name": "Sam New",
            "addresses": sam,
            "role": "Approves change orders",
            "not_to_confuse": "Sam Old (sam.old@finishessolutions.example)",
        },
    ).json()
    assert any(p["key"] == "sam" and p["addresses"] == [sam] for p in saved["people"])
    row = latest_audit(database_ready, "person_profile_set")
    assert row is not None and row["actor_id"] == "matthew@finishessolutions.example"
    result = client.post(
        "/api/lab/classify",
        headers=auth(),
        json={
            "subject": "CO 9 for Building C",
            "sender": "pm@builder.example",
            "to": sam,
            "body": "Sam, please approve change order 9 by Friday.",
            "person": sam,
        },
    ).json()
    check = result["person_check"]
    assert check["person"] == "Sam New" and check["decision"] == "yes"
    assert result["routing"]["action_item"] is True
    assert result["routing"]["why"][0].startswith("Step 2 for Sam New")
    no_profile = client.post(
        "/api/lab/classify",
        headers=auth(),
        json={"subject": "Hello", "body": "Please confirm by Friday.", "person": PAT},
    ).json()
    assert no_profile["person_check"] is None


def test_messages_filter_by_property_and_entity(client: TestClient) -> None:
    acme = client.get("/api/messages?since_days=0&entity=acme", headers=auth()).json()
    assert acme["results"]
    assert all(any("acme" in e.lower() for e in r["entities_ai"]) for r in acme["results"])
    lots = client.get("/api/messages?since_days=0&property=lot", headers=auth()).json()
    everything = client.get("/api/messages?since_days=0", headers=auth()).json()
    assert 0 < len(lots["results"]) < len(everything["results"])
    nothing = client.get("/api/messages?since_days=0&entity=zzz-no-such", headers=auth()).json()
    assert nothing["results"] == []


def test_lab_returns_property_and_entity_names(client: TestClient) -> None:
    result = client.post(
        "/api/lab/classify",
        headers=auth(),
        json={
            "subject": "Draw request for Example Commons",
            "body": "Capital Company LLC asks you to approve the draw for Building B by Friday.",
            "person": PAT,
        },
    ).json()
    assert "Capital Company LLC" in result["entities_ai"]
    assert {"Example Commons", "Building B"} <= set(result["properties_ai"])
