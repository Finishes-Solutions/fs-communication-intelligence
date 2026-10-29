"""Rate the impact and likelihood of project-risk emails (migration 0017).

Runs after processing in each scheduled run, for published versions whose latest
classification says ``project_risk`` = yes and that have no rating yet, so new
mail and mail processed before ratings existed are both covered, a bounded batch
at a time. Identical copies of one email (several mailboxes) reuse the first
copy's rating through the input hash instead of a second model call. A rating
failure leaves the version unrated; the next run tries again.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import psycopg

from ci_core.db import Connection
from ci_processing.risk_rating import RATING_VERSION, RiskInput, RiskRater, RiskRating

log = logging.getLogger(__name__)

_PENDING = """
SELECT v.id::text AS version_id, c.title, r.risk_text,
       (SELECT t.content FROM version_texts t
        WHERE t.version_id = v.id AND t.text_kind = 'email_body' LIMIT 1) AS body
FROM communication_versions v
JOIN communications c ON c.active_version_id = v.id
JOIN LATERAL (SELECT r.id, r.risk_text FROM classification_runs r
              WHERE r.version_id = v.id ORDER BY r.id DESC LIMIT 1) r ON true
WHERE v.processing_state = 'published'
  AND EXISTS (SELECT 1 FROM classification_labels l
              WHERE l.run_id = r.id AND l.label = 'project_risk' AND l.decision = 'yes')
  AND NOT EXISTS (SELECT 1 FROM risk_ratings rr WHERE rr.version_id = v.id)
ORDER BY c.occurred_at DESC NULLS LAST
LIMIT %s
"""


def _reuse(conn: Connection, sha: str, model: str) -> RiskRating | None:
    row = conn.execute(
        "SELECT impact, likelihood, reason FROM risk_ratings"
        " WHERE input_sha256 = %s AND model = %s AND rating_version = %s LIMIT 1",
        (sha, model, RATING_VERSION),
    ).fetchone()
    if row is None:
        return None
    return RiskRating(row["impact"], row["likelihood"], row["reason"], model)


def _save(conn: Connection, version_id: str, sha: str, rating: RiskRating) -> None:
    conn.execute(
        "INSERT INTO risk_ratings (version_id, impact, likelihood, reason, input_sha256,"
        " model, rating_version) VALUES (%s, %s, %s, %s, %s, %s, %s)"
        " ON CONFLICT (version_id) DO NOTHING",
        (
            version_id,
            rating.impact,
            rating.likelihood,
            rating.reason,
            sha,
            rating.model,
            RATING_VERSION,
        ),
    )


def rate_one(conn: Connection, rater: RiskRater, row: dict[str, Any]) -> str:
    """Rate one pending version; returns the outcome counted by the run."""
    item = RiskInput(row["title"] or "", row["body"] or "", row["risk_text"])
    sha = item.sha256()
    rating = _reuse(conn, sha, rater.model)
    outcome = "risk_rating_reused"
    if rating is None:
        try:
            rating = rater.rate(item)
        except Exception as exc:  # content-free code; the next run tries again
            log.warning("risk rating failed: %s", type(exc).__name__)
            return "risk_rating_failed"
        outcome = "risks_rated"
    _save(conn, row["version_id"], sha, rating)
    conn.commit()
    return outcome


def rate_pending_risks(
    connect: Callable[[], Connection],
    rater: RiskRater,
    *,
    limit: int = 400,
    workers: int = 8,
    max_seconds: float | None = 240.0,
) -> Counter[str]:
    """Rate up to ``limit`` unrated risk emails, newest first, within ``max_seconds``.

    Returns nothing on a database without migration 0017."""
    counts: Counter[str] = Counter()
    try:
        with connect() as conn:
            rows = conn.execute(_PENDING, (limit,)).fetchall()
            conn.commit()
    except psycopg.Error as exc:
        log.warning("risk ratings skipped: %s", type(exc).__name__)
        return counts
    if not rows:
        return counts
    deadline = time.monotonic() + max_seconds if max_seconds is not None else None
    queue = list(rows)
    lock = threading.Lock()

    def take() -> dict[str, Any] | None:
        if deadline is not None and time.monotonic() >= deadline:
            return None
        with lock:
            return queue.pop(0) if queue else None

    def worker() -> Counter[str]:
        local: Counter[str] = Counter()
        with connect() as conn:
            while (row := take()) is not None:
                local[rate_one(conn, rater, row)] += 1
        return local

    n = max(1, min(workers, len(rows)))
    with ThreadPoolExecutor(max_workers=n) as pool:
        for result in pool.map(lambda _: worker(), range(n)):
            counts.update(result)
    return counts
