"""AI impact x likelihood ratings of project-risk emails (migration 0017) and the
Risk page's matrix, trend and property views built on them. Fake backends only."""

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest

from ci_cli.seed import seed
from ci_core.db import AppRole, Connection, connect
from ci_email.capture import capture_window
from ci_email.fixture import FixtureGraphClient
from ci_email.process import Backends, _requeue, process_all
from ci_email.risk_ratings import _PENDING, rate_one, rate_pending_risks
from ci_processing.classification import FakeClassifier
from ci_processing.embedding import FakeEmbedder
from ci_processing.risk_rating import FakeRiskRater, RiskInput, RiskRating
from ci_processing.screening import FakeScreener
from ci_retrieval import reports
from ci_retrieval.identity import READER_ALL, Principal

NOW = datetime(2026, 10, 28, tzinfo=UTC)
READER = Principal("reader-oid", None, (READER_ALL,))
NOBODY = Principal("nobody-oid", None, ())


class CountingRater(FakeRiskRater):
    def __init__(self, fail: bool = False) -> None:
        self.calls: list[RiskInput] = []
        self.fail = fail

    def rate(self, item: RiskInput) -> RiskRating:
        self.calls.append(item)
        if self.fail:
            raise TimeoutError("slow")
        return super().rate(item)


@pytest.fixture(scope="module")
def stack(database: str) -> Iterator[dict[str, Any]]:
    def connector(role: AppRole | None = None) -> Connection:
        return connect(dbname=database, role=role)

    with connector() as conn:
        seed(conn, "pytest")
        conn.commit()
    graph = FixtureGraphClient.from_directory()
    capture_window(connector, graph, hours=744, include_sent=True, profile={"p": 1}, now=NOW)
    process_all(
        lambda: connector("ci_ingest"),
        Backends(FakeScreener(), FakeClassifier(), FakeEmbedder(1536), graph=graph),
        workers=1,
    )
    yield {"connect": connector}


def risk_versions(conn: Connection) -> set[str]:
    """Active published versions whose latest classification says project_risk = yes."""
    return {
        r["id"]
        for r in conn.execute(
            "SELECT v.id::text AS id FROM communication_versions v"
            " JOIN communications c ON c.active_version_id = v.id"
            " JOIN LATERAL (SELECT id FROM classification_runs r WHERE r.version_id = v.id"
            "   ORDER BY id DESC LIMIT 1) r ON true"
            " JOIN classification_labels l ON l.run_id = r.id"
            " WHERE v.processing_state = 'published' AND l.label = 'project_risk'"
            "   AND l.decision = 'yes'"
        ).fetchall()
    }


def test_risk_emails_are_rated_once_and_copies_share_a_call(stack: dict[str, Any]) -> None:
    connector = stack["connect"]
    failing = CountingRater(fail=True)
    counts = rate_pending_risks(lambda: connector("ci_ingest"), failing, workers=2)
    with connector() as conn:
        expected = risk_versions(conn)
        assert expected, "the fixture tenant has project-risk mail"
        assert conn.execute("SELECT count(*) AS n FROM risk_ratings").fetchone()["n"] == 0
    assert counts == {"risk_rating_failed": len(expected)}  # left unrated, retried next run

    rater = CountingRater()
    counts = rate_pending_risks(lambda: connector("ci_ingest"), rater, workers=1)
    with connector() as conn:
        rated = conn.execute(
            "SELECT version_id::text AS v, impact, likelihood, input_sha256, model, rating_version"
            " FROM risk_ratings"
        ).fetchall()
    assert {r["v"] for r in rated} == expected  # only risk emails, all of them
    assert all(r["model"] == "fake-risk-rater" for r in rated)
    assert all(r["rating_version"] == "risk-rating-v1" for r in rated)
    # One model call per distinct input; identical copies reuse it.
    assert len(rater.calls) == len({r["input_sha256"] for r in rated})
    assert counts["risks_rated"] == len(rater.calls)
    assert counts["risks_rated"] + counts.get("risk_rating_reused", 0) == len(expected)
    # Nothing left to rate.
    again = CountingRater()
    assert rate_pending_risks(lambda: connector("ci_ingest"), again) == {}
    assert again.calls == []

    # An identical copy (same input hash) reuses a stored rating without a call.
    source = sorted(expected)[0]
    with connector("ci_ingest") as conn:
        other = conn.execute(
            "SELECT id::text AS id FROM communication_versions WHERE NOT (id::text = ANY(%s))"
            " LIMIT 1",
            (sorted(expected),),
        ).fetchone()["id"]
        conn.execute(
            "INSERT INTO risk_ratings (version_id, impact, likelihood, reason, input_sha256,"
            " model, rating_version) SELECT %s, impact, likelihood, reason, input_sha256, model,"
            " rating_version FROM risk_ratings WHERE version_id = %s",
            (other, source),
        )
        conn.execute("DELETE FROM risk_ratings WHERE version_id = %s", (source,))
        pending = conn.execute(_PENDING, (10,)).fetchall()
        assert [r["version_id"] for r in pending] == [source]
        copy = CountingRater()
        assert rate_one(conn, copy, pending[0]) == "risk_rating_reused"
        assert copy.calls == []
        conn.execute("DELETE FROM risk_ratings WHERE version_id = %s", (other,))
        conn.commit()

    # Reprocessing a message drops its rating with its other derived data.
    victim = sorted(expected)[0]
    with connector("ci_ingest") as conn:
        _requeue(conn, [victim])
        left = conn.execute(
            "SELECT count(*) AS n FROM risk_ratings WHERE version_id = %s", (victim,)
        ).fetchone()
        conn.rollback()
    assert left["n"] == 0


def test_matrix_trend_heatmap_and_filters(stack: dict[str, Any]) -> None:
    connector = stack["connect"]
    rate_pending_risks(lambda: connector("ci_ingest"), FakeRiskRater())
    with connector("ci_search") as conn:
        data = reports.risk_overview(conn, READER, days=366, now=NOW)
    assert data["total"] > 0
    matrix = data["matrix"]
    assert len(matrix["cells"]) == 9
    # Rows run from high impact to low, columns from low likelihood to high.
    assert [c["impact"] for c in matrix["cells"][:3]] == ["high"] * 3
    assert [c["likelihood"] for c in matrix["cells"][:3]] == ["low", "medium", "high"]
    assert sum(c["risks"] for c in matrix["cells"]) == matrix["rated"] == data["total"]
    assert matrix["unrated"] == 0
    assert all("risk_rating_ai" in i for i in data["items"])

    periods = data["category_series"]["periods"]
    assert periods == [s["period"] for s in data["series"]]
    for cat in data["category_series"]["categories"]:
        assert cat["label"].startswith("cat_") and len(cat["counts"]) == len(periods)
    assert {c["label"]: c["risks"] for c in data["by_category"]} == {
        c["label"]: sum(c["counts"]) for c in data["category_series"]["categories"]
    }
    assert data["top_properties"], "fixture risk mail names properties"
    top = data["top_properties"][0]
    assert top["risks"] >= top["high_impact"] and top["previous"] == 0
    assert 1 <= top["worst_score"] <= 9
    heat = {r["property"]: sum(r["counts"]) for r in data["heatmap"]["rows"]}
    assert heat == {p["property"]: p["risks"] for p in data["top_properties"]}

    # A matrix cell narrows everything but the matrix itself.
    busiest = max(matrix["cells"], key=lambda c: c["risks"])
    with connector("ci_search") as conn:
        cell = reports.risk_overview(
            conn,
            READER,
            days=366,
            now=NOW,
            impact=busiest["impact"],
            likelihood=busiest["likelihood"],
        )
        by_property = reports.risk_overview(
            conn, READER, days=366, now=NOW, property=top["property"]
        )
        nobody = reports.risk_overview(conn, NOBODY, days=366, now=NOW)
        with pytest.raises(ValueError):
            reports.risk_overview(conn, READER, impact="severe")
    assert cell["total"] == busiest["risks"]
    assert cell["matrix"] == matrix
    assert all(
        i["risk_rating_ai"]["impact"] == busiest["impact"]
        and i["risk_rating_ai"]["likelihood"] == busiest["likelihood"]
        for i in cell["items"]
    )
    assert 0 < by_property["total"] <= data["total"]
    assert nobody["total"] == 0 and nobody["matrix"]["cells"] == []


def test_unrated_risks_count_as_unrated(stack: dict[str, Any]) -> None:
    connector = stack["connect"]
    with connector() as conn:
        conn.execute("DELETE FROM risk_ratings")
        conn.commit()
    with connector("ci_search") as conn:
        data = reports.risk_overview(conn, READER, days=366, now=NOW)
        only = reports.risk_overview(conn, READER, days=366, now=NOW, impact="unrated")
    assert data["matrix"]["rated"] == 0 and data["matrix"]["unrated"] == data["total"] > 0
    assert only["total"] == data["total"]
    assert not any("risk_rating_ai" in i for i in data["items"])


@pytest.mark.parametrize(
    "filters",
    [
        {},
        {"impact": "high"},
        {"likelihood": "unrated"},
        {"category": "cat_payment_billing"},
        {"property": "ridge"},
        {"mailbox": "pat.example@finishessolutions.example"},
        {"days": 7},
    ],
)
def test_fast_and_view_paths_agree(
    stack: dict[str, Any], monkeypatch: pytest.MonkeyPatch, filters: dict[str, Any]
) -> None:
    """The label-index path (migration 0021) and the old view path give the same
    overview, so the page is right before and after the migration is applied."""
    connector = stack["connect"]
    rate_pending_risks(lambda: connector("ci_ingest"), FakeRiskRater())
    args: dict[str, Any] = {"days": 366, "now": NOW} | filters
    with connector("ci_search") as conn:
        assert reports._has_label_lookup(conn)
        fast = reports.risk_overview(conn, READER, **args)
    monkeypatch.setattr(reports, "_has_label_lookup", lambda conn: False)
    with connector("ci_search") as conn:
        slow = reports.risk_overview(conn, READER, **args)
    assert fast == slow
    if not filters:
        assert fast["total"] > 0 and fast["items"] and fast["top_properties"]
