"""Mail rules (migration 0028, Matthew 2026-11-13): recurring emails that read like
risks or requests stay searchable but are left out of risks and action items
("quiet"), or are not processed at all ("skip"). Synthetic tenant, fake backends."""

from datetime import UTC, datetime
from typing import Any

import pytest
from test_pipeline import EMBEDDER, pipeline  # noqa: F401

from ci_core.operator import (
    OperatorError,
    add_mail_rule,
    list_mail_rules,
    remove_mail_rule,
)
from ci_email.process import _prefilter
from ci_processing.prefilter import OPERATOR_RULE
from ci_retrieval import reports
from ci_retrieval import search as retrieval
from ci_retrieval.identity import READER_ALL, Principal

READER = Principal("reader-oid", None, (READER_ALL,))
NOW = datetime(2026, 10, 28, tzinfo=UTC)
LIEN = "Notice of mechanic's lien - Example Ridge Phase 2"
DAYS = 366


@pytest.fixture(autouse=True)
def fixture_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(retrieval, "utcnow", lambda: NOW)


def risk_subjects(p: dict[str, Any]) -> set[str]:
    with p["connect"]("ci_search") as conn:
        overview = reports.risk_overview(conn, READER, days=DAYS)
        listed = retrieval.list_actionable(conn, READER, hours=744, limit=100, status="all")
    from_overview = {i["subject"] for i in overview["items"]}
    assert from_overview == {r["subject"] for r in listed["risks"]}
    return from_overview


def action_subjects(p: dict[str, Any]) -> set[str]:
    with p["connect"]("ci_search") as conn:
        listed = retrieval.list_actionable(conn, READER, hours=744, limit=100, status="all")
    return {a["subject"] for a in listed["actions"] + listed["possible"]}


def searchable(p: dict[str, Any], subject: str) -> bool:
    with p["connect"]("ci_search") as conn:
        found = retrieval.search(conn, READER, subject, EMBEDDER, retrieval.NO_FILTERS, 25)
    return subject in {r["subject"] for r in found["results"]}


def test_a_quiet_rule_keeps_mail_searchable_but_out_of_risks_and_actions(
    pipeline: dict[str, Any],  # noqa: F811
) -> None:
    before_risks, before_actions = risk_subjects(pipeline), action_subjects(pipeline)
    assert LIEN in before_risks
    target = next(iter(sorted(before_actions)))
    with pipeline["connect"]("ci_operator") as conn:
        first = add_mail_rule(conn, "  Notice of Mechanic's LIEN ", "quiet", "pytest")
        second = add_mail_rule(conn, target, "quiet", "pytest")
        conn.commit()
        rules = list_mail_rules(conn)
    assert first["muted_emails"] >= 1 and second["muted_emails"] >= 1
    assert next(r["subject_contains"] for r in rules) == "notice of mechanic's lien"
    assert all(r["matching_emails"] >= 1 for r in rules)

    assert risk_subjects(pipeline) == before_risks - {LIEN, target}
    assert action_subjects(pipeline) == before_actions - {LIEN, target}
    assert searchable(pipeline, LIEN) and searchable(pipeline, target)
    with pipeline["connect"]("ci_search") as conn:
        stats = reports.report_groups(conn, READER, "risks", "label")["groups"]
        assert sum(g["messages"] for g in stats if g["key"] == "project_risk") == len(
            before_risks - {LIEN, target}
        )

    # Removing the rules brings the AI's original answers back.
    with pipeline["connect"]("ci_operator") as conn:
        restored = 0
        for r in rules:
            restored += remove_mail_rule(conn, r["id"], "pytest")["restored_emails"]
        conn.commit()
        assert list_mail_rules(conn) == []
    with pipeline["connect"]() as conn:
        actions = {r["action"] for r in conn.execute("SELECT action FROM audit_events").fetchall()}
    assert restored >= 2 and {"mail_rule_add", "mail_rule_remove"} <= actions
    assert risk_subjects(pipeline) == before_risks
    assert action_subjects(pipeline) == before_actions


def test_sender_must_match_and_replies_are_never_matched(
    pipeline: dict[str, Any],  # noqa: F811
) -> None:
    before = risk_subjects(pipeline)
    with pipeline["connect"]("ci_operator") as conn:
        other = add_mail_rule(
            conn, "notice of mechanic's lien", "quiet", "pytest", sender="nobody@example.com"
        )
        assert other == {"muted_emails": 0, "restored_emails": 0}
        row = conn.execute(
            "SELECT mail_rule_for(%s, %s, 'quiet') AS direct,"
            " mail_rule_for(%s, %s, 'quiet') AS reply, mail_rule_for(%s, %s, 'quiet') AS else_",
            (
                "Notice of mechanic's lien",
                "NOBODY@example.com",
                "RE: Notice of mechanic's lien",
                "nobody@example.com",
                "Notice of mechanic's lien",
                "someone@example.com",
            ),
        ).fetchone()
        assert row and row["direct"] is not None
        assert row["reply"] is None and row["else_"] is None
        conn.rollback()
    assert risk_subjects(pipeline) == before


def test_rules_are_validated(pipeline: dict[str, Any]) -> None:  # noqa: F811
    with pipeline["connect"]("ci_operator") as conn:
        for subject, effect, sender in (
            ("re:", "quiet", None),  # too short: would match far too much
            ("unresolved governance violations", "hide", None),
            ("unresolved governance violations", "quiet", "not-an-address"),
        ):
            with pytest.raises(OperatorError):
                add_mail_rule(conn, subject, effect, "pytest", sender=sender)
        add_mail_rule(conn, "unresolved governance violations", "quiet", "pytest")
        with pytest.raises(OperatorError, match="already exists"):
            add_mail_rule(conn, "Unresolved  Governance Violations", "quiet", "pytest")
        with pytest.raises(OperatorError):
            remove_mail_rule(conn, "999999", "pytest")
        conn.rollback()


def test_new_mail_is_quieted_before_it_is_published(pipeline: dict[str, Any]) -> None:  # noqa: F811
    """Processing applies the rule to the message itself (apply_mail_rules(version))."""
    with pipeline["connect"]() as conn:
        add_mail_rule(conn, "notice of mechanic's lien", "quiet", "pytest")
        conn.execute("SELECT * FROM apply_mail_rules()")  # all quiet
        version = conn.execute(
            "SELECT v.id::text AS id FROM communication_versions v"
            " JOIN communications c ON c.active_version_id = v.id WHERE c.title = %s LIMIT 1",
            (LIEN,),
        ).fetchone()
        assert version
        # As if just classified: the AI's answers are back in place ...
        conn.execute(
            "UPDATE classification_labels l SET decision = muted_from, muted_from = NULL"
            " FROM classification_runs r WHERE r.id = l.run_id AND r.version_id = %s"
            " AND l.muted_from IS NOT NULL",
            (version["id"],),
        )
        # ... and the per-message call processing makes mutes them again.
        one = conn.execute("SELECT * FROM apply_mail_rules(%s::uuid)", (version["id"],)).fetchone()
        assert one == {"muted_emails": 1, "restored_emails": 0}
        risk = conn.execute(
            "SELECT l.decision, l.muted_from FROM classification_labels l"
            " JOIN classification_runs r ON r.id = l.run_id"
            " WHERE r.version_id = %s AND l.label = 'project_risk'",
            (version["id"],),
        ).fetchone()
        assert risk == {"decision": "no", "muted_from": "yes"}
        conn.rollback()


def test_a_skip_rule_stops_processing_of_matching_mail(pipeline: dict[str, Any]) -> None:  # noqa: F811
    with pipeline["connect"]() as conn:
        job = conn.execute(
            "SELECT v.id AS version_id, c.title FROM communication_versions v"
            " JOIN communications c ON c.active_version_id = v.id WHERE c.title = %s LIMIT 1",
            (LIEN,),
        ).fetchone()
        assert job
        backends: Any = type("B", (), {"prefilter_allow_domains": ()})()
        sender = "counsel@example-law.example"
        assert _prefilter(conn, job, backends, sender, "body") is None
        add_mail_rule(conn, "notice of mechanic's lien", "skip", "pytest")
        outcome = _prefilter(conn, job, backends, sender, "body")
        assert outcome is not None and outcome.reason_codes == (OPERATOR_RULE,)
        reply = dict(job) | {"title": "RE: " + job["title"]}
        assert _prefilter(conn, reply, backends, sender, "body") is None
        conn.rollback()


def test_the_search_role_cannot_change_rules_or_labels(pipeline: dict[str, Any]) -> None:  # noqa: F811
    with pipeline["connect"]("ci_search") as conn:
        for sql in ("SELECT * FROM apply_mail_rules()", "SELECT * FROM mail_rules"):
            with pytest.raises(Exception, match="permission denied"):
                conn.execute(sql).fetchall()
            conn.rollback()
