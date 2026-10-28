"""Hand review of classifier labels, and the precision/recall they give.

Reviews are written as ``ci_operator`` and hold label decisions only. The API
first reads the message's labels through the caller's own grants
(``ci_retrieval.reports.message_labels``), so nobody can review, or learn the
labels of, a message they may not read.

Metrics per label, from the latest review of each (message, label):

- precision = AI yes and person yes / AI yes (how often a "yes" is right);
- recall = AI yes and person yes / person yes (how much is found; a label the
  AI left uncertain counts as missed, because lists act on "yes" only);
- uncertain = how the person resolved labels the AI left in the review band.

Only reviews of randomly sampled messages estimate real-world precision and
recall; the other sampling modes over-represent hard cases.
"""

from __future__ import annotations

from typing import Any

from ci_core.db import Connection
from ci_core.operator import OperatorError, audit

SAMPLES = ("random", "uncertain", "label", "browse")
MIN_SAMPLE = 20  # below this, a rate is shown but flagged as too few to judge


def reviewed_ids(conn: Connection, reviewer: str) -> list[str]:
    rows = conn.execute(
        "SELECT DISTINCT communication_id::text AS id FROM label_reviews WHERE reviewer_id = %s",
        (reviewer,),
    ).fetchall()
    return [r["id"] for r in rows]


def my_reviews(conn: Connection, reviewer: str, run_id: int) -> dict[str, str]:
    rows = conn.execute(
        "SELECT label, human_decision FROM label_reviews WHERE reviewer_id = %s AND run_id = %s",
        (reviewer, run_id),
    ).fetchall()
    return {r["label"]: r["human_decision"] for r in rows}


def record(
    conn: Connection,
    reviewer: str,
    labels: dict[str, Any],
    label: str,
    human: str | None,
    sample: str,
) -> None:
    """Save (or with ``human=None`` clear) one reviewer's answer for one label.

    ``labels`` is the authorized ``message_labels`` result; the AI decision and
    probability are copied from it, never taken from the request.
    """
    if sample not in SAMPLES:
        raise OperatorError(f"sample must be one of {', '.join(SAMPLES)}")
    if human not in ("yes", "no", None):
        raise OperatorError("answer must be yes, no or empty")
    ai = labels["labels"].get(label)
    if ai is None:
        raise OperatorError(f"this message has no classifier output for {label!r}")
    if human is None:
        conn.execute(
            "DELETE FROM label_reviews WHERE run_id = %s AND label = %s AND reviewer_id = %s",
            (labels["run_id"], label, reviewer),
        )
    else:
        conn.execute(
            """
            INSERT INTO label_reviews (communication_id, run_id, label, ai_decision,
                ai_probability, human_decision, sample, model, policy_version, reviewer_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (run_id, label, reviewer_id) DO UPDATE
            SET human_decision = excluded.human_decision, reviewed_at = now()
            """,
            (
                labels["communication_id"],
                labels["run_id"],
                label,
                ai["decision"],
                ai["probability"],
                human,
                sample,
                labels["model"],
                labels["policy_version"],
                reviewer,
            ),
        )
    audit(
        conn,
        reviewer,
        "label_review",
        "communications",
        labels["communication_id"],
        {"label": label, "answer": human, "sample": sample},
        actor_type="user",
    )


def _rate(hits: int, total: int) -> float | None:
    return round(hits / total, 3) if total else None


def metrics(conn: Connection, sample: str = "random") -> dict[str, Any]:
    """Per-label agreement between the classifier and reviewers.

    ``sample='random'`` uses random-sample reviews only (honest estimates);
    ``'all'`` uses every review.
    """
    if sample not in ("random", "all"):
        raise OperatorError("sample must be random or all")
    rows = conn.execute(
        """
        WITH latest AS (
            SELECT DISTINCT ON (communication_id, label) *
            FROM label_reviews WHERE (%(sample)s = 'all' OR sample = 'random')
            ORDER BY communication_id, label, reviewed_at DESC
        )
        SELECT label,
               count(*) AS reviewed,
               count(*) FILTER (WHERE ai_decision = 'yes' AND human_decision = 'yes') AS tp,
               count(*) FILTER (WHERE ai_decision = 'yes' AND human_decision = 'no') AS fp,
               count(*) FILTER (WHERE ai_decision = 'no' AND human_decision = 'yes') AS fn,
               count(*) FILTER (WHERE ai_decision = 'no' AND human_decision = 'no') AS tn,
               count(*) FILTER (WHERE ai_decision IN ('review', 'unresolved')
                                  AND human_decision = 'yes') AS uncertain_yes,
               count(*) FILTER (WHERE ai_decision IN ('review', 'unresolved')
                                  AND human_decision = 'no') AS uncertain_no,
               max(policy_version) AS policy_version
        FROM latest GROUP BY label ORDER BY label
        """,
        {"sample": sample},
    ).fetchall()
    totals = conn.execute(
        "SELECT count(DISTINCT communication_id) AS messages, count(*) AS reviews,"
        " count(DISTINCT reviewer_id) AS reviewers FROM label_reviews"
        " WHERE (%(sample)s = 'all' OR sample = 'random')",
        {"sample": sample},
    ).fetchone()
    out = []
    for r in rows:
        tp, fp, fn = int(r["tp"]), int(r["fp"]), int(r["fn"])
        unc_yes = int(r["uncertain_yes"])
        ai_yes, human_yes = tp + fp, tp + fn + unc_yes
        out.append(
            {
                "label": r["label"],
                "reviewed": int(r["reviewed"]),
                "true_positive": tp,
                "false_positive": fp,
                "false_negative": fn,
                "true_negative": int(r["tn"]),
                "uncertain_yes": unc_yes,
                "uncertain_no": int(r["uncertain_no"]),
                "precision": _rate(tp, ai_yes),
                "precision_n": ai_yes,
                "recall": _rate(tp, human_yes),
                "recall_n": human_yes,
                "enough": ai_yes >= MIN_SAMPLE and human_yes >= MIN_SAMPLE,
                "policy_version": r["policy_version"],
            }
        )
    return {
        "sample": sample,
        "min_sample": MIN_SAMPLE,
        "messages": int(totals["messages"]) if totals else 0,
        "reviews": int(totals["reviews"]) if totals else 0,
        "reviewers": int(totals["reviewers"]) if totals else 0,
        "labels": out,
    }
