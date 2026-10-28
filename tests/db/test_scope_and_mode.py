"""Seed, inventory, mode and scope rules against a real migrated database."""

import pytest

from ci_cli.seed import seed
from ci_core.db import Connection
from ci_core.operator import (
    OperatorError,
    approve_mailbox,
    exclude_mailbox,
    exclude_participant,
    list_mailboxes,
    parse_participant,
    set_mode,
)
from ci_core.runtime import (
    EmptyScopeError,
    RuntimeConfigError,
    active_config,
    eligible_mailboxes,
    require_capture_scope,
)
from ci_email.fixture import FixtureGraphClient
from ci_email.inventory import sync_inventory

PAT = "pat.example@finishessolutions.example"
CASEY = "casey.private@finishessolutions.example"
ENROLLED = {PAT, "morgan.lee@finishessolutions.example", "ap@finishessolutions.example"}


def addresses(conn: Connection) -> set[str]:
    return {s.address for s in eligible_mailboxes(conn)}


def test_no_active_revision_fails_closed(conn: Connection) -> None:
    with pytest.raises(RuntimeConfigError):
        active_config(conn)


def test_seed_is_idempotent_and_states_match_graph(conn: Connection) -> None:
    first = seed(conn, "pytest")
    second = seed(conn, "pytest")
    assert second.approved == [] and second.excluded == []
    assert first.inventory.states == {
        "enrolled": 3,
        "excluded_by_rule": 1,
        "no_exchange_access": 1,
        "no_mailbox": 1,
    }
    states = {r.address: r.scope_state for r in list_mailboxes(conn)}
    assert states["riley.outside@finishessolutions.example"] == "no_exchange_access"
    assert states["no.mailbox@finishessolutions.example"] == "no_mailbox"
    assert addresses(conn) == ENROLLED
    assert active_config(conn).mode == "test"


def test_paused_and_production_without_approvals_select_nothing(conn: Connection) -> None:
    seed(conn, "pytest")
    set_mode(conn, "paused", "pytest")
    assert eligible_mailboxes(conn) == []
    with pytest.raises(EmptyScopeError):
        require_capture_scope(conn)
    set_mode(conn, "production", "pytest")
    with pytest.raises(EmptyScopeError):  # nothing is production-approved
        require_capture_scope(conn)
    approve_mailbox(conn, PAT, "production", "pytest")
    assert addresses(conn) == {PAT}
    assert conn.execute("SELECT count(*) AS n FROM runtime_config WHERE is_active").fetchone() == {
        "n": 1
    }


def test_exclusion_wins_over_approval(conn: Connection) -> None:
    seed(conn, "pytest")
    approve_mailbox(conn, CASEY, "test", "pytest")
    assert CASEY not in addresses(conn)
    exclude_mailbox(conn, PAT, "pytest", reason="personal_mailbox")
    assert PAT not in addresses(conn)  # immediately, before any inventory sync
    exclude_mailbox(conn, PAT, "pytest", revoke=True)
    assert PAT in addresses(conn)
    with pytest.raises(OperatorError, match="already excluded"):
        exclude_mailbox(conn, CASEY, "pytest", reason="again")


def test_revoked_approval_and_rerun_inventory(conn: Connection) -> None:
    result = seed(conn, "pytest")
    approve_mailbox(conn, PAT, "test", "pytest", revoke=True)
    assert PAT not in addresses(conn)
    exclude_mailbox(conn, CASEY, "pytest", revoke=True)
    sync_inventory(
        conn, FixtureGraphClient.from_directory(), result.connection_id, result.corpus_id, "pytest"
    )
    states = {r.address: r.scope_state for r in list_mailboxes(conn)}
    assert states[CASEY] == "enrolled"  # reachable again, still not approved
    assert CASEY not in addresses(conn)


def test_participant_exclusions(conn: Connection) -> None:
    assert parse_participant("@Example.COM") == ("participant_domain", "example.com")
    assert parse_participant("A.B@example.com") == ("participant_address", "a.b@example.com")
    with pytest.raises(OperatorError):
        parse_participant("not an address")
    exclude_participant(conn, "@vendor.example", "pytest", reason="legal_privilege")
    row = conn.execute(
        "SELECT exclusion_kind, pattern FROM exclusions WHERE revoked_at IS NULL"
    ).fetchone()
    assert row == {"exclusion_kind": "participant_domain", "pattern": "vendor.example"}
    exclude_participant(conn, "@vendor.example", "pytest", revoke=True)
    with pytest.raises(OperatorError):
        exclude_participant(conn, "@vendor.example", "pytest", revoke=True)


def test_every_operator_change_is_audited(conn: Connection) -> None:
    seed(conn, "pytest")
    before = conn.execute("SELECT count(*) AS n FROM audit_events").fetchone()
    set_mode(conn, "paused", "pytest")
    exclude_participant(conn, "x@vendor.example", "pytest", reason="test_reason")
    approve_mailbox(conn, PAT, "test", "pytest", revoke=True)
    rows = conn.execute(
        "SELECT action, actor_id FROM audit_events ORDER BY id DESC LIMIT 3"
    ).fetchall()
    assert [r["action"] for r in rows] == ["scope_unapprove", "participant_exclude", "mode_change"]
    assert {r["actor_id"] for r in rows} == {"pytest"}
    after = conn.execute("SELECT count(*) AS n FROM audit_events").fetchone()
    assert before is not None and after is not None and after["n"] == before["n"] + 3
    details = conn.execute("SELECT details::text AS d FROM audit_events").fetchall()
    assert not any("x@vendor.example" in r["d"] for r in details)  # ids and codes only


def test_reason_codes_are_validated(conn: Connection) -> None:
    seed(conn, "pytest")
    with pytest.raises(OperatorError, match="reason"):
        exclude_mailbox(conn, PAT, "pytest", reason="Free text with spaces")
