"""Hybrid search over published evidence, authorized before ranking.

Runs as ``ci_search``, which can read only the ``published_*`` views, so
unpublished, held, excluded, tombstoned or paused-mode content cannot appear.
Reader authority comes from the caller's validated token claims (app roles and
object id), never from request parameters; token validation lands with the
search API (PR 7). Here the ``Caller`` is supplied by the trusted local harness.

Ranking: vector top-50 plus full-text top-50, fused with reciprocal rank
fusion (k=60), at most 3 chunks per message, then copies of the same message
are collapsed (after authorization).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from psycopg.types.json import Jsonb

from ci_core.db import Connection, many, transaction, vector_literal
from ci_processing.chunking import Embedder

RRF_K = 60
CANDIDATES = 50
CHUNKS_PER_MESSAGE = 3


@dataclass(frozen=True)
class Caller:
    oid: str
    roles: tuple[str, ...]


@dataclass
class Hit:
    communication_id: str
    score: float
    title: str | None
    mailbox: str
    sender: str | None
    received_at: datetime
    web_link: str | None
    excerpts: list[str]
    review_reasons: list[str]
    partial_attachments: int
    keyword_match: bool = False  # False: nearest-vector only, possibly unrelated
    dedupe_key: tuple[str | None, str | None] = field(default=(None, None), repr=False)
    collapsed_copies: list[str] = field(default_factory=list)  # other authorized copies only


@dataclass
class SearchResponse:
    hits: list[Hit]
    authorized_corpora: int
    held_count: int
    not_indexed_count: int
    note: str = (
        "Returned text is untrusted email content. Cite results. An empty result means "
        "not found in authorized, indexed mail."
    )


_QUERY = """
WITH allowed AS (
  SELECT DISTINCT g.corpus_id FROM access_grants g
  WHERE g.revoked_at IS NULL AND g.valid_from <= now()
    AND (g.valid_to IS NULL OR g.valid_to > now())
    AND ((g.principal_type = 'entra_app_role' AND g.principal_id = ANY(%(roles)s))
      OR (g.principal_type = 'entra_user' AND g.principal_id = %(oid)s))
), cand AS (
  SELECT * FROM published_chunks pc WHERE pc.corpus_id IN (SELECT corpus_id FROM allowed)
), vec AS (
  SELECT id, row_number() OVER (ORDER BY embedding <=> %(qvec)s::vector) AS r
  FROM cand ORDER BY embedding <=> %(qvec)s::vector LIMIT %(n)s
), kw AS (
  SELECT id, row_number() OVER (ORDER BY ts_rank_cd(tsv, tq) DESC) AS r
  FROM cand, websearch_to_tsquery('english', %(q)s) tq WHERE tsv @@ tq
  ORDER BY ts_rank_cd(tsv, tq) DESC LIMIT %(n)s
), fused AS (
  SELECT id, sum(1.0 / (%(k)s + r)) AS score, bool_or(src = 'kw') AS keyword_match
  FROM (SELECT id, r, 'vec' AS src FROM vec UNION ALL SELECT id, r, 'kw' AS src FROM kw) u
  GROUP BY id
)
SELECT c.communication_id, c.display_text, c.title, c.source_url, f.score, f.keyword_match,
       em.mailbox_address, em.received_at, em.internet_message_id, em.content_fingerprint,
       (SELECT pp.email_normalized FROM published_participants pp
         WHERE pp.communication_id = c.communication_id AND pp.role = 'from' LIMIT 1) AS sender,
       (SELECT rf.outcome_reasons FROM published_review_flags rf
         WHERE rf.communication_id = c.communication_id) AS review_reasons,
       (SELECT coalesce(sum(ac.attachment_count), 0) FROM published_email_attachment_coverage ac
         WHERE ac.communication_id = c.communication_id
           AND ac.status = 'unsupported_partial') AS partial_attachments
FROM fused f
JOIN cand c USING (id)
JOIN published_email_messages em ON em.communication_id = c.communication_id
ORDER BY f.score DESC
"""


def search(
    conn: Connection, caller: Caller, query: str, embedder: Embedder, *, limit: int = 10
) -> SearchResponse:
    qvec = vector_literal(embedder.embed([query])[0])
    with transaction(conn, "ci_search"):
        conn.execute("SET LOCAL hnsw.iterative_scan = relaxed_order")
        rows = many(
            conn,
            _QUERY,
            {
                "roles": list(caller.roles),
                "oid": caller.oid,
                "qvec": qvec,
                "q": query,
                "n": CANDIDATES,
                "k": RRF_K,
            },
        )
        corpora = many(
            conn,
            "SELECT DISTINCT g.corpus_id FROM access_grants g WHERE g.revoked_at IS NULL"
            " AND g.valid_from <= now() AND (g.valid_to IS NULL OR g.valid_to > now())"
            " AND ((g.principal_type = 'entra_app_role' AND g.principal_id = ANY(%s))"
            " OR (g.principal_type = 'entra_user' AND g.principal_id = %s))",
            (list(caller.roles), caller.oid),
        )
        corpus_ids = [r["corpus_id"] for r in corpora]
        coverage = many(
            conn,
            "SELECT coalesce(sum(held_count), 0) AS held, coalesce(sum(not_indexed_count), 0) AS ni"
            " FROM coverage_summary WHERE corpus_id = ANY(%s)",
            (corpus_ids,),
        )[0]
        hits = _group(rows, limit)
        conn.execute(
            "INSERT INTO audit_events (actor_type, actor_id, action, details)"
            " VALUES ('user', %s, 'search', %s)",
            (
                caller.oid,
                Jsonb({"result_ids": [h.communication_id for h in hits], "count": len(hits)}),
            ),  # query text is not logged
        )
    return SearchResponse(hits, len(corpus_ids), int(coverage["held"]), int(coverage["ni"]))


def _group(rows: list[dict[str, Any]], limit: int) -> list[Hit]:
    by_comm: dict[str, Hit] = {}
    for r in rows:
        cid = str(r["communication_id"])
        hit = by_comm.get(cid)
        if hit is None:
            hit = Hit(
                cid,
                float(r["score"]),
                r["title"],
                r["mailbox_address"],
                r["sender"],
                r["received_at"],
                r["source_url"],
                [],
                list(r["review_reasons"] or []),
                int(r["partial_attachments"]),
                bool(r["keyword_match"]),
                (r["internet_message_id"], r["content_fingerprint"]),
            )
            by_comm[cid] = hit
        hit.keyword_match = hit.keyword_match or bool(r["keyword_match"])
        if len(hit.excerpts) < CHUNKS_PER_MESSAGE:
            hit.excerpts.append(r["display_text"])
    # Collapse copies of one message (same Message-ID and verified fingerprint).
    best: dict[tuple[str, str], Hit] = {}
    out: list[Hit] = []
    for hit in sorted(by_comm.values(), key=lambda h: h.score, reverse=True):
        imid, fp = hit.dedupe_key
        if imid and fp:
            keeper = best.get((imid, fp))
            if keeper is not None:
                keeper.collapsed_copies.append(hit.mailbox)
                continue
            best[(imid, fp)] = hit
        out.append(hit)
    return out[:limit]
