"""End-to-end email flow on the synthetic fixtures, against a real PostgreSQL + pgvector.

Runs the same code the testkit UI drives: inventory, delta capture, job queue,
fetch/normalize, screening, classification, publication and authorized search.
Skipped unless CI_TEST_DATABASE_URL points at a THROWAWAY server; the database
named in it is dropped and recreated for every test.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from ci_core import control
from ci_core.db import many, transaction
from ci_testkit.harness import TESTKIT_ENV, Overrides, Testkit

pytestmark = pytest.mark.db

DB_URL = os.environ.get("CI_TEST_DATABASE_URL")

EXPECTED = {
    "01_invoice_approval": ["searchable", "searchable"],
    "02_change_order": ["searchable"],
    "03_lease_renewal": ["searchable"],
    "04_legal_notice": ["searchable"],
    "05_lender_draw": ["searchable"],
    "06_vendor_complaint": ["searchable"],
    "07_newsletter": ["not_indexed"],
    "08_spam": ["not_indexed"],
    "09_phishing_lookalike": ["flagged"],
    "10_fake_ssn": ["restricted"],
    "11_fake_api_key": ["restricted"],
    "12_long_thread": ["searchable"],
    "13_signature_inline_image": ["searchable"],
    "14_same_message_three_copies": ["searchable"] * 3,
    "15_prompt_injection": ["held"],
    "16_sent_item": ["searchable"],
    "17_excluded_mailbox": ["not_read"],
    "18_excluded_participant_domain": ["excluded"],
    "19_before_backfill": ["not_read"],
    "20_draft": ["not_read"],
    "21_junk": ["not_read"],
    "22_unapproved_mailbox": ["not_read"],
    "23_uncertain_phi": ["flagged"],
}


@pytest.fixture
def tk(tmp_path: Path) -> Iterator[Testkit]:
    if not DB_URL:
        pytest.skip("set CI_TEST_DATABASE_URL to a throwaway PostgreSQL + pgvector server")
    saved = dict(os.environ)
    os.environ.update(
        {**TESTKIT_ENV, "DATABASE_URL": DB_URL, "LOCAL_BLOB_DIR": str(tmp_path / "blob")}
    )
    try:
        kit = Testkit(data_dir=tmp_path)
        kit.reset()
        kit.run_all()
        yield kit
    finally:
        os.environ.clear()
        os.environ.update(saved)


def lanes(tk: Testkit) -> dict[str, list[str]]:
    state = tk.store.read()
    out: dict[str, list[str]] = {}
    for lane, cards in tk.board().items():
        for card in cards:
            out.setdefault(state["messages"][card["id"]]["scenario"], []).append(lane)
    return out


def ids(tk: Testkit, scenario: str, mailbox: str | None = None) -> list[str]:
    state = tk.store.read()
    return [
        mid
        for mid, e in state["messages"].items()
        if e["scenario"] == scenario
        and (mailbox is None or state["mailboxes"][e["mailbox_id"]]["address"] == mailbox)
    ]


def deliver(tk: Testkit, subject: str = "Paint touch-up schedule for Building C") -> str:
    return tk.deliver(
        to_mailbox="pat.morgan@finishes.example",
        from_name="Test Vendor",
        from_address="crew@paint-co.example",
        subject=subject,
        body="Touch-up crew arrives Thursday for the Building C punch list.",
    )


def test_every_fixture_lands_in_its_expected_lane(tk: Testkit) -> None:
    got = {k: sorted(v) for k, v in lanes(tk).items()}
    assert got == {k: sorted(v) for k, v in EXPECTED.items()}


def test_second_run_changes_nothing(tk: Testkit) -> None:
    with transaction(tk.conn, "ci_ingest"):
        before = many(tk.conn, "SELECT count(*) AS n FROM communication_versions")[0]["n"]
    reports = tk.run_all()
    with transaction(tk.conn, "ci_ingest"):
        after = many(tk.conn, "SELECT count(*) AS n FROM communication_versions")[0]["n"]
    assert before == after
    assert "Nothing ready in the queue." in reports[-1].lines


def test_search_is_authorized_before_ranking_and_collapses_copies(tk: Testkit) -> None:
    res = tk.search("site walk notes", "executive")
    walks = [h for h in res.hits if (h.title or "").startswith("Site walk notes")]
    assert len(walks) == 1 and len(walks[0].collapsed_copies) == 2
    assert tk.search("site walk notes", "no_role").hits == []
    everything = tk.search("payroll paperwork api key portal ssn", "executive")
    titles = {h.title for h in everything.hits}
    assert not titles & {
        "New hire paperwork - Taylor Brooks",
        "Portal API key for the draw reporting integration",
    }
    flagged = tk.search("remittance", "executive").hits[0]
    assert flagged.review_reasons == ["phishing_yes"]


def test_revoked_grant_returns_nothing(tk: Testkit) -> None:
    with transaction(tk.conn, "ci_operator"):
        tk.conn.execute("UPDATE access_grants SET revoked_at = now()")
    assert tk.search("change order", "executive").hits == []


def test_paused_mode_reads_nothing_and_search_returns_nothing(tk: Testkit) -> None:
    control.set_mode(tk.conn, "paused", actor="test")
    assert tk.search("change order", "executive").hits == []
    mid = deliver(tk)
    reports = tk.run_all()
    assert "Paused: nothing is read." in reports[1].lines
    assert ids(tk, "composed") == [mid]
    assert lanes(tk)["composed"] == ["not_read"]
    control.set_mode(tk.conn, "test", actor="test")
    tk.run_all()
    assert lanes(tk)["composed"] == ["searchable"]


def test_participant_exclusion_hides_already_indexed_mail_at_once(tk: Testkit) -> None:
    ex = control.add_exclusion(
        tk.conn,
        "participant_domain",
        actor="test",
        reason_code="t",
        pattern="brightline-builders.example",
    )
    assert lanes(tk)["02_change_order"] == ["hidden"]
    assert all(
        h.sender != "dana.whitfield@brightline-builders.example"
        for h in tk.search("change order drainage", "executive").hits
    )
    control.revoke_exclusion(tk.conn, ex, actor="test")
    assert lanes(tk)["02_change_order"] == ["searchable"]


def test_delete_and_moves_at_source(tk: Testkit) -> None:
    (pat_copy,) = ids(tk, "14_same_message_three_copies", "pat.morgan@finishes.example")
    tk.store.hard_delete(pat_copy)
    (co,) = ids(tk, "02_change_order")
    tk.store.move(co, "deleteditems")
    (lease,) = ids(tk, "03_lease_renewal")
    tk.store.move(lease, "Projects")
    tk.run_all()
    got = lanes(tk)
    assert sorted(got["14_same_message_three_copies"]) == ["searchable", "searchable", "withdrawn"]
    assert got["02_change_order"] == ["withdrawn"]
    assert got["03_lease_renewal"] == ["searchable"]


def test_expired_delta_resyncs_without_duplicates(tk: Testkit) -> None:
    with transaction(tk.conn, "ci_ingest"):
        before = many(tk.conn, "SELECT count(*) AS n FROM communications")[0]["n"]
    tk.store.simulate(expire_delta_for="pat.morgan@finishes.example")
    report = tk.reconcile()
    tk.work()
    with transaction(tk.conn, "ci_ingest"):
        after = many(tk.conn, "SELECT count(*) AS n FROM communications")[0]["n"]
    assert any("410" in line for line in report.lines)
    assert before == after


def test_throttling_stops_the_round_and_the_next_run_recovers(tk: Testkit) -> None:
    mid = deliver(tk)
    tk.store.simulate(throttle_calls=3)
    first = tk.run_all()
    assert any("429" in line for r in first for line in r.lines)
    tk.run_all()
    assert lanes(tk)["composed"] == ["searchable"], mid


def test_screening_outage_is_pending_never_pass(tk: Testkit) -> None:
    tk.save_overrides(Overrides(screening_down=True))
    deliver(tk)
    tk.run_all()
    assert lanes(tk)["composed"] == ["queued"]
    assert all(
        h.title != "Paint touch-up schedule for Building C"
        for h in tk.search("touch-up crew", "executive").hits
    )
    tk.save_overrides(Overrides(screening_down=False))
    with transaction(tk.conn, "ci_ingest"):
        tk.conn.execute("UPDATE jobs SET run_after = now() WHERE state = 'queued'")
    tk.work()
    assert lanes(tk)["composed"] == ["searchable"]


def test_stricter_attachment_policy_reprocesses_and_unpublishes(tk: Testkit) -> None:
    tk.save_overrides(Overrides(attachment_policy="hold_message"))
    tk.reprocess_all()
    got = lanes(tk)
    assert got["13_signature_inline_image"] == ["held"]
    assert got["01_invoice_approval"] == ["held", "held"]
    assert got["02_change_order"] == ["searchable"]  # no attachments: unchanged
    tk.save_overrides(Overrides(attachment_policy="exempt_cid_inline"))
    tk.reprocess_all()
    got = lanes(tk)
    assert got["13_signature_inline_image"] == ["searchable"]  # inline logo exempt
    assert got["01_invoice_approval"] == ["held", "held"]  # PDF still unsupported in E1


def test_empty_scope_fails_closed(tk: Testkit) -> None:
    for m in tk.mailboxes():
        if m["scope"] and m["scope"]["test_approved"]:
            control.set_scope_approval(
                tk.conn, str(m["scope"]["id"]), approved=False, actor="test", backfill_since=None
            )
    report = tk.reconcile()
    assert report.lines == ["No mailbox is in scope: nothing is read (fails closed)."]
    assert tk.search("change order", "executive").hits == []


def test_approving_a_mailbox_captures_its_mail(tk: Testkit) -> None:
    leasing = next(
        m for m in tk.mailboxes() if m["fixture"]["address"] == "leasing@finishes.example"
    )
    control.set_scope_approval(
        tk.conn,
        str(leasing["scope"]["id"]),
        approved=True,
        actor="test",
        backfill_since=tk.settings.email_backfill_since,
    )
    tk.run_all()
    assert lanes(tk)["22_unapproved_mailbox"] == ["searchable"]
