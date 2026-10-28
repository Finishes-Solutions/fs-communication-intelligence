-- 0010_properties_entities.sql
-- AI fields (Matthew, 2026-10-28): the properties/projects an email is about
-- and the legal entities it names, exactly as written in the email (never
-- inferred). An optional administrator registry of canonical names
-- (`ci names`) is shown to the classifier and used to map aliases such as
-- "WC Retail" to one canonical name; names with no match are kept as written.

SET search_path = ci, public;

ALTER TABLE classification_runs
    ADD COLUMN properties text[] NOT NULL DEFAULT '{}',
    ADD COLUMN entities   text[] NOT NULL DEFAULT '{}';

-- Same definition as 0004 with the two new columns appended at the end.
CREATE OR REPLACE VIEW published_classifications AS
SELECT DISTINCT ON (pc.id)
       pc.id AS communication_id, pc.corpus_id, r.disposition, r.summary, r.action_text,
       r.action_for, r.due_text, r.model, r.policy_version,
       ARRAY(SELECT l.label FROM classification_labels l
             WHERE l.run_id = r.id AND l.decision = 'yes' ORDER BY l.label) AS yes_labels,
       ARRAY(SELECT l.label FROM classification_labels l
             WHERE l.run_id = r.id AND l.decision IN ('review', 'unresolved')
             ORDER BY l.label) AS uncertain_labels,
       r.risk_text, r.properties, r.entities
FROM published_communications pc
JOIN classification_runs r ON r.version_id = pc.active_version_id
ORDER BY pc.id, r.id DESC;

CREATE TABLE known_names (
    id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    kind       text NOT NULL CHECK (kind IN ('property', 'entity')),
    canonical  text NOT NULL CHECK (length(canonical) BETWEEN 1 AND 120),
    aliases    text[] NOT NULL DEFAULT '{}',
    created_by text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX known_names_unique ON known_names (kind, lower(canonical));

GRANT SELECT ON known_names TO ci_ingest;
GRANT SELECT, INSERT, UPDATE, DELETE ON known_names TO ci_operator;
