"""Write the knowledge an email carries into the knowledge base (migration 0016).

Called by processing for every version it is about to publish, in the same
transaction, so knowledge follows publication like chunks and labels do. No
approval step (Matthew, 2026-10-28); what keeps it honest is in
``ci_processing.knowledge`` (checked against the email) and here:

* mail with phishing indicators (any answer but "no") adds nothing;
* an email may link a name to an existing entry only through the exact learned
  name it was shown (``known``); everything else is matched by its normalized
  key (``name_key``), and the administrator's registry (``ci names``) decides
  canonical names first;
* entities are created in key order, so concurrent workers never deadlock.

Names learned from earlier mail (``learned_names``) count only published
messages in the active dataset and need at least two of them.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from ci_core.db import Connection
from ci_processing.classification import (
    ClassificationResult,
    Decision,
    KnownName,
    normalize_names,
)
from ci_processing.knowledge import Knowledge, LearnedName, fold, from_names, name_key

log = logging.getLogger(__name__)

LEARNED_MIN_MESSAGES = 2
LEARNED_LIMIT = 150
LEARNED_DAYS = 365
LEARNED_SECONDS = 300.0
_ADMIN_KIND = {"property": "property", "organization": "entity"}

Ref = tuple[str, str] | int  # (kind, key) of an entity to find or create, or a known id


@dataclass
class LearnedCache:
    """The learned names, re-read at most every ``seconds`` (one per processing run)."""

    seconds: float = LEARNED_SECONDS
    _at: float = 0.0
    _names: tuple[LearnedName, ...] = ()
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def get(self, conn: Connection) -> tuple[LearnedName, ...]:
        with self._lock:
            if self._at and time.monotonic() - self._at < self.seconds:
                return self._names
        names = tuple(learned_names(conn))
        with self._lock:
            self._at, self._names = time.monotonic(), names
        return names


# Mentions in published messages of the active dataset, entities resolved.
_PUBLISHED_MENTIONS = """
    SELECT v.entity_id, r.kind, r.name_override, km.written, km.detail, c.id AS cid,
           c.occurred_at
    FROM kg_mentions km
    JOIN communications c ON c.active_version_id = km.version_id
    JOIN communication_versions cv ON cv.id = km.version_id
    JOIN kg_visible_entities v ON v.id = km.entity_id
    JOIN kg_entities r ON r.id = v.entity_id
    WHERE cv.processing_state = 'published' AND c.lifecycle_state = 'active'
      AND c.dataset_key = ci.active_dataset()"""


def learned_names(
    conn: Connection, *, minimum: int = LEARNED_MIN_MESSAGES, limit: int = LEARNED_LIMIT
) -> list[LearnedName]:
    """Properties, organizations, terms and meetings seen in at least ``minimum`` messages over
    the last year, most frequent first, as the classifier is shown them."""
    rows = conn.execute(
        f"""
        WITH m AS ({_PUBLISHED_MENTIONS}
                   AND r.kind IN ('property', 'organization', 'term', 'meeting')
                   AND c.occurred_at > now() - make_interval(days => %(days)s))
        SELECT entity_id, kind, max(name_override) AS override, count(DISTINCT cid) AS n
        FROM m GROUP BY entity_id, kind HAVING count(DISTINCT cid) >= %(minimum)s
        ORDER BY n DESC, entity_id LIMIT %(limit)s
        """,  # noqa: S608 - the fragment is a constant
        {"days": LEARNED_DAYS, "minimum": minimum, "limit": limit},
    ).fetchall()
    if not rows:
        return []
    ids = [r["entity_id"] for r in rows]
    forms: dict[int, list[str]] = {}
    for f in conn.execute(
        f"""
        WITH m AS ({_PUBLISHED_MENTIONS} AND v.entity_id = ANY(%(ids)s))
        SELECT entity_id, written, count(*) AS n FROM m GROUP BY entity_id, written
        ORDER BY entity_id, n DESC, written
        """,  # noqa: S608
        {"ids": ids},
    ).fetchall():
        forms.setdefault(f["entity_id"], []).append(f["written"])
    meanings = {
        m["entity_id"]: m["detail"]
        for m in conn.execute(
            f"""
            WITH m AS ({_PUBLISHED_MENTIONS} AND v.entity_id = ANY(%(ids)s)
                       AND r.kind = 'term' AND km.detail IS NOT NULL)
            SELECT DISTINCT ON (entity_id) entity_id, detail FROM m
            ORDER BY entity_id, occurred_at DESC
            """,  # noqa: S608
            {"ids": ids},
        ).fetchall()
    }
    out = []
    for r in rows:
        written = forms.get(r["entity_id"], [])
        name = r["override"] or (written[0] if written else "")
        if not name:
            continue
        also = tuple(w for w in written if w.casefold() != name.casefold())[:3]
        out.append(
            LearnedName(r["entity_id"], r["kind"], name, also, meanings.get(r["entity_id"]) or "")
        )
    return out


def knowledge_of(result: ClassificationResult) -> Knowledge:
    """The result's knowledge, or the per-email names for a run without it."""
    if result.knowledge is not None:
        return result.knowledge
    return from_names(result.properties, result.entities)


def contributes(result: ClassificationResult) -> bool:
    """Mail with phishing indicators must not teach the knowledge base anything."""
    return result.decisions()["phishing"] is Decision.NO


def record(
    conn: Connection,
    version_id: str,
    knowledge: Knowledge,
    *,
    learned: Sequence[LearnedName] = (),
    known: Sequence[KnownName] = (),
) -> Counter[str]:
    """Store the version's mentions and facts; returns counts by kind of row."""
    listed: dict[tuple[str, str], int] = {(n.kind, n.name): n.entity_id for n in learned}
    wanted: dict[tuple[str, str], str] = {}  # (kind, key) -> first written name
    refs: dict[str, Ref] = {}  # how this email refers to something -> what it is
    mentions: list[tuple[Ref, str, str | None]] = []
    facts: list[tuple[Ref, str, str, Ref, str | None]] = []

    def by_name(kind: str, written: str, known_as: str | None = None) -> Ref:
        if known_as and (kind, known_as) in listed:
            return listed[(kind, known_as)]
        found = refs.get(fold(written))
        if found is not None and (not isinstance(found, tuple) or found[0] == kind):
            return found
        admin = _ADMIN_KIND.get(kind)
        canonical = normalize_names([written], admin, known, 1)[0] if admin else written
        ref = (kind, name_key(kind, canonical))
        wanted.setdefault(ref, canonical)
        return ref

    for n in knowledge.names:
        ref = by_name(n.kind, n.written, n.known)
        mentions.append((ref, n.written, n.domain))
        refs.setdefault(fold(n.written), ref)
        if n.known:
            refs.setdefault(fold(n.known), ref)
    for p in knowledge.people:
        person: tuple[str, str] = ("person", name_key("person", p.email))
        wanted.setdefault(person, p.name)
        mentions.append((person, p.name, p.title))
        refs.setdefault(p.email, person)
        refs.setdefault(fold(p.name), person)
        if p.organization:
            org = by_name("organization", p.organization)
            mentions.append((org, p.organization, None))
            refs.setdefault(fold(p.organization), org)
            facts.append((person, "works_for", "", org, p.title))
    for r in knowledge.relations:
        subject = refs.get(r.subject.lower()) or refs.get(fold(r.subject))
        obj = refs.get(r.object.lower()) or refs.get(fold(r.object))
        if subject is not None and obj is not None:
            facts.append((subject, r.relation, r.role, obj, r.evidence))
    for t in knowledge.terms:
        term: tuple[str, str] = ("term", name_key("term", t.term))
        wanted.setdefault(term, t.term)
        mentions.append((term, t.term, t.meaning))

    ids = _entity_ids(conn, wanted)

    def resolve(ref: Ref) -> int | None:
        return ref if isinstance(ref, int) else ids.get(ref)

    mention_rows: dict[int, tuple[str, int, str, str | None]] = {}
    for ref, written, detail in mentions:
        entity = resolve(ref)
        if entity is not None:
            mention_rows.setdefault(entity, (version_id, entity, written, detail))
    fact_rows: dict[tuple[int, str, str, int], tuple[Any, ...]] = {}
    for s_ref, relation, role, o_ref, evidence in facts:
        s, o = resolve(s_ref), resolve(o_ref)
        if s is not None and o is not None and s != o:
            fact_rows.setdefault(
                (s, relation, role, o), (version_id, s, relation, role, o, evidence)
            )
    with conn.cursor() as cur:
        if mention_rows:
            cur.executemany(
                "INSERT INTO kg_mentions (version_id, entity_id, written, detail)"
                " VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
                list(mention_rows.values()),
            )
        if fact_rows:
            cur.executemany(
                "INSERT INTO kg_relations (version_id, subject_id, relation, role, object_id,"
                " evidence) VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
                list(fact_rows.values()),
            )
    return Counter({"mentions": len(mention_rows), "relations": len(fact_rows)})


def _entity_ids(conn: Connection, wanted: dict[tuple[str, str], str]) -> dict[tuple[str, str], int]:
    """Find or create each (kind, key), in key order (no deadlocks between workers)."""
    if not wanted:
        return {}
    ordered = sorted(wanted)
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO kg_entities (kind, key, name) VALUES (%s, %s, %s)"
            " ON CONFLICT (kind, key) DO NOTHING",
            [(kind, key, wanted[(kind, key)]) for kind, key in ordered],
        )
    rows = conn.execute(
        "SELECT e.id, e.kind, e.key FROM kg_entities e"
        " JOIN unnest(%s::text[], %s::text[]) AS w(kind, key) USING (kind, key)",
        ([k for k, _ in ordered], [k for _, k in ordered]),
    ).fetchall()
    return {(r["kind"], r["key"]): int(r["id"]) for r in rows}


def backfill(
    conn: Connection, *, known: Sequence[KnownName] = (), limit: int | None = None
) -> Counter[str]:
    """Knowledge for published mail that has none yet, from what its classification
    already holds (no AI call): the knowledge answer when there is one, else the
    property and entity names. Mail with phishing indicators is skipped."""
    rows = conn.execute(
        """
        SELECT DISTINCT ON (cv.id) cv.id::text AS version_id, r.knowledge, r.properties,
               r.entities,
               (SELECT l.decision FROM classification_labels l
                 WHERE l.run_id = r.id AND l.label = 'phishing') AS phishing
        FROM communications c
        JOIN communication_versions cv ON cv.id = c.active_version_id
        JOIN classification_runs r ON r.version_id = cv.id
        WHERE cv.processing_state = 'published'
          AND NOT EXISTS (SELECT 1 FROM kg_mentions m WHERE m.version_id = cv.id)
        ORDER BY cv.id, r.id DESC
        LIMIT %s
        """,
        (limit if limit else None,),
    ).fetchall()
    counts: Counter[str] = Counter()
    for row in rows:
        if row["phishing"] != Decision.NO.value:
            counts["skipped_phishing"] += 1
            continue
        stored = Knowledge.from_json(row["knowledge"])
        item = stored or from_names(row["properties"] or (), row["entities"] or ())
        if item.empty:
            counts["nothing_to_add"] += 1
            continue
        added = record(conn, row["version_id"], item, known=known)
        counts["messages"] += 1
        counts.update(added)
    return counts
