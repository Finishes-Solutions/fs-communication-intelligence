-- 0016_knowledge.sql
-- The knowledge base (Matthew, 2026-10-28: "start understanding relationships,
-- projects, properties, terms over time"; no seeded list, no approval step).
--
-- The classifier returns, per email, the properties, organizations, people and
-- terms it names and the facts it states about them (ci_processing.knowledge);
-- the answer is kept on classification_runs.knowledge. Processing records it
-- per published version:
--   kg_entities   one row per distinct thing (kind + normalized key; a person's
--                 key is their address). Holds no counts: those are computed per
--                 caller from the messages they may read.
--   kg_mentions   a version names an entity (as written; person: title; term:
--                 meaning; organization: domain).
--   kg_relations  a version states a fact: subject, relation, role, object.
-- Like chunks and labels, these rows follow publication: a superseded,
-- withdrawn, restricted or excluded message drops out of the published_kg_*
-- views, which are all ci_search can read. Operators correct the base without
-- approving it: merge two entities (merged_into, reversible), hide one, or set
-- its display name; every change is audited.

SET search_path = ci, public;

ALTER TABLE classification_runs ADD COLUMN knowledge jsonb;

CREATE TABLE kg_entities (
    id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    kind          text NOT NULL CHECK (kind IN ('property', 'organization', 'person', 'term')),
    key           text NOT NULL CHECK (length(key) BETWEEN 1 AND 160),
    name          text NOT NULL,          -- first form seen; shown to operators only
    name_override text,                   -- operator-set display name
    merged_into   bigint REFERENCES kg_entities(id),
    hidden        boolean NOT NULL DEFAULT false,
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_by    text,
    updated_at    timestamptz,
    UNIQUE (kind, key),
    CHECK (merged_into IS DISTINCT FROM id)
);
CREATE INDEX kg_entities_merged ON kg_entities (merged_into) WHERE merged_into IS NOT NULL;

CREATE TABLE kg_mentions (
    version_id uuid NOT NULL REFERENCES communication_versions(id) ON DELETE CASCADE,
    entity_id  bigint NOT NULL REFERENCES kg_entities(id),
    written    text NOT NULL,
    detail     text,
    PRIMARY KEY (version_id, entity_id)
);
CREATE INDEX kg_mentions_entity ON kg_mentions (entity_id);

CREATE TABLE kg_relations (
    version_id uuid NOT NULL REFERENCES communication_versions(id) ON DELETE CASCADE,
    subject_id bigint NOT NULL REFERENCES kg_entities(id),
    relation   text NOT NULL CHECK (relation IN ('works_for', 'role_on', 'owns', 'affiliate_of')),
    role       text NOT NULL DEFAULT '',
    object_id  bigint NOT NULL REFERENCES kg_entities(id),
    evidence   text,
    PRIMARY KEY (version_id, subject_id, relation, role, object_id)
);
CREATE INDEX kg_relations_subject ON kg_relations (subject_id);
CREATE INDEX kg_relations_object ON kg_relations (object_id);

-- An entity as callers see it: merges followed, hidden ones gone, and a person
-- whose address or domain is excluded as a participant never shown.
CREATE VIEW kg_visible_entities AS
SELECT e.id, coalesce(e.merged_into, e.id) AS entity_id
FROM kg_entities e
JOIN kg_entities r ON r.id = coalesce(e.merged_into, e.id)
WHERE NOT e.hidden AND NOT r.hidden
  AND NOT (r.kind = 'person' AND EXISTS (
      SELECT 1 FROM exclusions x
      WHERE x.revoked_at IS NULL
        AND ((x.exclusion_kind = 'participant_address' AND x.pattern = r.key)
          OR (x.exclusion_kind = 'participant_domain'
              AND x.pattern = split_part(r.key, '@', 2)))));

CREATE VIEW published_kg_mentions AS
SELECT pc.id AS communication_id, pc.corpus_id, pc.occurred_at,
       v.entity_id, r.kind, r.key, r.name_override, m.written, m.detail
FROM published_communications pc
JOIN kg_mentions m ON m.version_id = pc.active_version_id
JOIN kg_visible_entities v ON v.id = m.entity_id
JOIN kg_entities r ON r.id = v.entity_id;

CREATE VIEW published_kg_relations AS
SELECT pc.id AS communication_id, pc.corpus_id, pc.occurred_at,
       s.entity_id AS subject_id, k.relation, k.role, o.entity_id AS object_id, k.evidence
FROM published_communications pc
JOIN kg_relations k ON k.version_id = pc.active_version_id
JOIN kg_visible_entities s ON s.id = k.subject_id
JOIN kg_visible_entities o ON o.id = k.object_id
WHERE s.entity_id <> o.entity_id;  -- two merged entities say nothing about each other

GRANT SELECT, INSERT ON kg_entities TO ci_ingest;
GRANT SELECT, INSERT, DELETE ON kg_mentions, kg_relations TO ci_ingest;
GRANT SELECT, UPDATE ON kg_entities TO ci_operator;
GRANT SELECT ON kg_mentions, kg_relations, kg_visible_entities TO ci_operator;
GRANT SELECT ON kg_visible_entities TO ci_ingest;
GRANT SELECT ON published_kg_mentions, published_kg_relations TO ci_search;
