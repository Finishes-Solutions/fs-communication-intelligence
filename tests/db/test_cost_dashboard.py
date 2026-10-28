"""Metered usage on the cost page: usage rows priced by ci.ops_usage_cost, totals
per run and per 1,000 emails, and audited price edits by the operator role."""

from collections.abc import Iterator

import pytest

from ci_admin import costs
from ci_cli.seed import seed
from ci_core import ops
from ci_core.db import connect
from ci_core.operator import OperatorError
from ci_email.process import record_usage
from ci_processing.usage import (
    LANGUAGE_PII,
    OPENAI_CHAT,
    OPENAI_EMBEDDING,
    PROMPT_SHIELDS,
    UsageRow,
)

TZ = "America/Chicago"


@pytest.fixture(scope="module")
def db(database: str) -> Iterator[str]:
    with connect(dbname=database) as conn:
        seed(conn, "pytest")
        conn.commit()
    with connect(dbname=database, role="ci_ingest") as conn:
        run_id = ops.start_run(conn, "process", "schedule")
        ops.finish_run(conn, run_id, "succeeded", {"index": 900, "review": 50, "not_indexed": 50})
        record_usage(
            conn,
            [
                UsageRow(OPENAI_CHAT, "azure-openai:gpt", 1000, 1_000_000, 1_000_000),
                UsageRow(OPENAI_EMBEDDING, "azure-openai:emb", 10, 1_000_000),
                UsageRow(LANGUAGE_PII, "azure-language-pii", 100, characters=999_000, records=1000),
                UsageRow(PROMPT_SHIELDS, "content-safety-prompt-shields", 0, records=1000),
            ],
            source="processing",
            run_id=run_id,
        )
        conn.commit()
    yield database


def test_usage_is_priced_with_the_default_unit_prices(db: str) -> None:
    with connect(dbname=db, role="ci_operator") as conn:
        rows = {
            r["meter"]: float(r["estimated_usd"])
            for r in conn.execute("SELECT meter, estimated_usd FROM ops_usage_cost").fetchall()
        }
    # Zero-call rows are not stored; the rest at 0.44 + 1.76 per 1M chat tokens,
    # 0.13 per 1M embedding tokens and 1.00 per 1,000 PII records.
    assert rows == {OPENAI_CHAT: 2.20, OPENAI_EMBEDDING: 0.13, LANGUAGE_PII: 1.00}


def test_metered_totals_per_run_and_per_thousand_emails(db: str) -> None:
    with connect(dbname=db, role="ci_operator") as conn:
        data = costs.metered(conn, TZ)
    assert data["month_usd"] == pytest.approx(3.33)
    assert data["today_usd"] == pytest.approx(3.33)
    assert data["month_messages"] == 1000
    assert data["per_1000_messages_usd"] == pytest.approx(3.33)
    (run,) = data["runs"]
    assert run["messages"] == 1000 and run["usd"] == pytest.approx(3.33)
    assert run["by_meter"][OPENAI_CHAT] == pytest.approx(2.20)
    assert {r["meter"] for r in data["by_day"]} == {OPENAI_CHAT, OPENAI_EMBEDDING, LANGUAGE_PII}


def test_price_edits_are_validated_audited_and_repriced(db: str) -> None:
    with connect(dbname=db, role="ci_operator") as conn:
        with pytest.raises(OperatorError, match="unknown price keys"):
            costs.update_prices(conn, "pytest", {"gold": {"usd": 1}})
        with pytest.raises(OperatorError, match="between 0"):
            costs.update_prices(conn, "pytest", {"language_pii": {"usd": -1}})
        with pytest.raises(OperatorError, match="must be a number"):
            costs.update_prices(conn, "pytest", {"language_pii": {"usd": "cheap"}})
        conn.rollback()
        updated = costs.update_prices(
            conn, "matthew", {"language_pii": {"usd": 0.5, "verified": True}}
        )
        conn.commit()
        pii = next(p for p in updated if p["key"] == "language_pii")
        assert (pii["usd"], pii["verified"], pii["updated_by"]) == (0.5, True, "matthew")
        assert costs.metered(conn, TZ)["month_usd"] == pytest.approx(2.83)
    with connect(dbname=db) as conn:
        audit = conn.execute(
            "SELECT actor_id, details FROM audit_events WHERE action = 'cost_prices_change'"
        ).fetchone()
        assert audit == {
            "actor_id": "matthew",
            "details": {"language_pii": {"usd": "0.5", "verified": True}},
        }


def test_only_prices_are_editable(db: str) -> None:
    with connect(dbname=db, role="ci_operator") as conn:
        with pytest.raises(Exception, match="permission denied"):
            conn.execute("UPDATE cost_prices SET per_units = 1")
        conn.rollback()
    with connect(dbname=db, role="ci_search") as conn:
        with pytest.raises(Exception, match="permission denied"):
            conn.execute("SELECT * FROM ai_usage")
        conn.rollback()
