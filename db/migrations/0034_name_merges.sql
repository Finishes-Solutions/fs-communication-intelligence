-- 0034_name_merges.sql
-- Knowledge merges reach every page that groups or filters by name (Matthew,
-- 2026-11-18: "if I merge Stokes Rd into Gracious Gardens, the system should know
-- moving forward that Stokes Rd means Gracious Gardens").
--
-- Emails keep the names the AI found as written (classification_runs.properties
-- and .entities). Until now only the Knowledge pages followed an operator's merge
-- or rename; the Files folders, the property and entity filters, the groupings of
-- reports and email_stats, and the Risk page's property charts used the written
-- names. They now map each written name through kg_name_map:
--   kg_name_key(kind, name)   the knowledge base's key for a name, the same as
--                             ci_processing.knowledge.name_key (checked by a test)
--   kg_name_map               one row per property or organization entry that is
--                             merged into another entry of the same kind, or whose
--                             root has members or an operator-set name: its key and
--                             the name it is shown as (the operator's name, else the
--                             form written most often across the merged entries).
--                             Rebuilt by a trigger on every change to kg_entities,
--                             so a merge, unmerge, rename or hide applies at once,
--                             to earlier mail too; nothing is reprocessed.
--   canonical_names(kind, names)  the names as shown: mapped, duplicates dropped,
--                             order kept; a name with no merged entry stays as written.
-- Hidden entries are not mapped (their emails keep the written name). An entry
-- merged into another kind (Fix Kind) is not mapped either. published_window's
-- name filters match the written or the mapped name, so filtering by "gracious"
-- also finds emails that say "Stokes Rd".
--
-- Names in kg_name_map come from email the reader may not be able to read (the
-- name of the entry merged into). That is the only thing it reveals, and only for
-- names written in an email the reader can read.

SET search_path = ci, public;

CREATE OR REPLACE FUNCTION kg_name_key(kind text, name text) RETURNS text
LANGUAGE sql IMMUTABLE PARALLEL SAFE
SET search_path = ci, public
AS $$
    WITH base AS (
        SELECT btrim(regexp_replace(regexp_replace(
                   replace(lower(normalize(coalesce(name, ''), NFKC)), '&', ' and '),
                   '[^[:alnum:]_[:space:]]', ' ', 'g'), '[[:space:]]+', ' ', 'g')) AS s),
    org AS (
        SELECT CASE WHEN kind = 'organization' THEN
            regexp_replace(regexp_replace(regexp_replace(regexp_replace(regexp_replace(
            regexp_replace(regexp_replace(regexp_replace(s, '^the ', ''),
                '\ml l c\M', 'llc', 'g'), '\ml l p\M', 'llp', 'g'), '\ml p\M', 'lp', 'g'),
                '\mincorporated\M', 'inc', 'g'), '\mcorporation\M', 'corp', 'g'),
                '\mcompany\M', 'co', 'g'), '\mlimited\M', 'ltd', 'g')
            ELSE s END AS s
        FROM base)
    SELECT left(s, 120) FROM org
$$;

CREATE TABLE IF NOT EXISTS kg_name_map (
    kind   text NOT NULL CHECK (kind IN ('property', 'organization')),
    key    text NOT NULL,
    label  text NOT NULL,
    PRIMARY KEY (kind, key)
);

CREATE OR REPLACE FUNCTION refresh_kg_name_map() RETURNS void
LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path = ci, public
AS $$
    DELETE FROM kg_name_map;
    WITH members AS (  -- each visible entry with its root, same kind only
        SELECT e.id, e.kind, e.key, r.id AS root_id, r.name_override, r.name
        FROM kg_entities e
        JOIN kg_entities r ON r.id = coalesce(e.merged_into, e.id)
        WHERE e.kind IN ('property', 'organization') AND r.kind = e.kind
          AND NOT e.hidden AND NOT r.hidden),
    roots AS (  -- roots with something to map: members, or an operator-set name
        SELECT root_id FROM members
        GROUP BY root_id, name_override
        HAVING count(*) > 1 OR name_override IS NOT NULL),
    written AS (  -- the form written most often across a root's entries
        SELECT DISTINCT ON (m.root_id) m.root_id, km.written
        FROM members m JOIN kg_mentions km ON km.entity_id = m.id
        WHERE m.root_id IN (SELECT root_id FROM roots)
        GROUP BY m.root_id, km.written
        ORDER BY m.root_id, count(*) DESC, km.written)
    INSERT INTO kg_name_map (kind, key, label)
    SELECT m.kind, m.key, coalesce(m.name_override, w.written, m.name)
    FROM members m
    JOIN roots USING (root_id)
    LEFT JOIN written w USING (root_id)
    ON CONFLICT DO NOTHING;
$$;

CREATE OR REPLACE FUNCTION kg_name_map_changed() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = ci, public
AS $$
BEGIN
    PERFORM refresh_kg_name_map();
    RETURN NULL;
END
$$;

DROP TRIGGER IF EXISTS kg_entities_name_map ON kg_entities;
CREATE TRIGGER kg_entities_name_map
AFTER UPDATE OR DELETE ON kg_entities
FOR EACH STATEMENT EXECUTE FUNCTION kg_name_map_changed();

CREATE OR REPLACE FUNCTION canonical_names(kind text, names text[]) RETURNS text[]
LANGUAGE sql STABLE PARALLEL SAFE
SET search_path = ci, public
AS $$
    SELECT CASE WHEN names IS NULL OR cardinality(names) = 0 OR NOT EXISTS (
                    SELECT 1 FROM kg_name_map WHERE kg_name_map.kind = canonical_names.kind)
           THEN coalesce(names, '{}')
           ELSE (SELECT coalesce(array_agg(d.n ORDER BY d.o), '{}')
                 FROM (SELECT DISTINCT ON (lower(s.n)) s.n, s.o
                       FROM (SELECT coalesce(m.label, btrim(x.v)) AS n, x.o
                             FROM unnest(names) WITH ORDINALITY AS x(v, o)
                             LEFT JOIN kg_name_map m ON m.kind = canonical_names.kind
                                 AND m.key = kg_name_key(canonical_names.kind, x.v)) s
                       ORDER BY lower(s.n), s.o) d)
           END
$$;

SELECT refresh_kg_name_map();

REVOKE ALL ON kg_name_map FROM PUBLIC;
GRANT SELECT ON kg_name_map TO ci_search, ci_operator, ci_ingest;
REVOKE ALL ON FUNCTION refresh_kg_name_map() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION refresh_kg_name_map() TO ci_operator;
GRANT EXECUTE ON FUNCTION kg_name_key(text, text), canonical_names(text, text[])
    TO ci_search, ci_operator, ci_ingest;

-- The window's name filters also match the mapped name (otherwise as in 0031).
CREATE OR REPLACE FUNCTION published_window(
    since timestamptz DEFAULT NULL, until timestamptz DEFAULT NULL,
    ids uuid[] DEFAULT NULL, conversation text DEFAULT NULL,
    in_mailbox text DEFAULT NULL, from_address text DEFAULT NULL,
    property_like text DEFAULT NULL, entity_like text DEFAULT NULL)
RETURNS TABLE (communication_id uuid, corpus_id uuid, occurred_at timestamptz, title text,
               source_url text, mailbox_address text, direction text,
               internet_message_id text, content_fingerprint text, sender text,
               disposition text, summary text, action_text text, action_for text[],
               due_text text, risk_text text, properties text[], entities text[],
               yes_labels text[], uncertain_labels text[], attachments bigint,
               risk_impact text, risk_likelihood text, risk_reason text,
               conversation_id text)
LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path = ci, public
-- Planned for the values given: a generic plan could not use the date index, the
-- id list, the conversation index or the sender's index.
SET plan_cache_mode = force_custom_plan
AS $$
#variable_conflict use_column
BEGIN
    IF conversation IS NOT NULL THEN
        -- The conversation's emails by id (within `ids` when both are given).
        ids := ARRAY(SELECT em.communication_id FROM email_messages em
                     WHERE em.conversation_id = conversation
                       AND (ids IS NULL OR em.communication_id = ANY(ids)));
    END IF;
    -- Every column is what the published_* views show for the same email, as in
    -- published_labelled; an email without a classification has NULLs and empty
    -- lists there, as a LEFT JOIN to published_classifications would give.
    -- The optional filters are the caller's own (ci_retrieval.search._FILTERS:
    -- mailbox address, a 'from' address, text within a property or entity name,
    -- already escaped for LIKE); they are applied here, before the label lists,
    -- attachment counts and senders are built, so a narrow filter over a long
    -- period stays cheap. They only narrow.
    RETURN QUERY
    WITH dated AS MATERIALIZED (
        SELECT pc.id, pc.corpus_id, pc.occurred_at, pc.title, pc.source_url,
               pc.active_version_id, mb.primary_smtp_address, em.direction,
               em.internet_message_id, em.content_fingerprint, em.conversation_id
        FROM published_communications pc
        JOIN email_messages em ON em.communication_id = pc.id
        JOIN email_mailboxes mb ON mb.scope_id = em.mailbox_scope_id
        WHERE (since IS NULL OR pc.occurred_at >= since)
          AND (until IS NULL OR pc.occurred_at < until)
          AND (ids IS NULL OR pc.id = ANY(ids))
          AND (in_mailbox IS NULL OR mb.primary_smtp_address = in_mailbox)
          AND (from_address IS NULL OR pc.id IN (
                SELECT cp.communication_id
                FROM participants p
                JOIN communication_participants cp ON cp.participant_id = p.id
                WHERE p.email_normalized = from_address AND cp.role = 'from'))),
    runs AS MATERIALIZED (  -- the latest run of each version
        SELECT DISTINCT ON (r.version_id) r.id, r.version_id, r.disposition, r.summary,
               r.action_text, r.action_for, r.due_text, r.risk_text, r.properties, r.entities
        FROM classification_runs r
        WHERE r.version_id IN (SELECT dated.active_version_id FROM dated)
        ORDER BY r.version_id, r.id DESC),
    pub AS MATERIALIZED (
        SELECT d.*, r.id AS run_id, r.disposition, r.summary, r.action_text, r.action_for,
               r.due_text, r.risk_text, r.properties, r.entities
        FROM dated d
        LEFT JOIN runs r ON r.version_id = d.active_version_id
        WHERE (property_like IS NULL OR EXISTS (
                SELECT 1 FROM unnest(r.properties || canonical_names('property', r.properties))
                    AS x(name)
                WHERE x.name ILIKE '%' || property_like || '%'))
          AND (entity_like IS NULL OR EXISTS (
                SELECT 1 FROM unnest(r.entities || canonical_names('organization', r.entities))
                    AS x(name)
                WHERE x.name ILIKE '%' || entity_like || '%'))),
    lab AS (
        SELECT l.run_id,
               array_agg(l.label ORDER BY l.label) FILTER (WHERE l.decision = 'yes') AS yes,
               array_agg(l.label ORDER BY l.label)
                   FILTER (WHERE l.decision IN ('review', 'unresolved')) AS uncertain
        FROM classification_labels l
        WHERE l.decision <> 'no' AND l.run_id IN (SELECT pub.run_id FROM pub)
        GROUP BY l.run_id),
    att AS (
        SELECT a.version_id, count(*) AS n
        FROM email_attachments a
        WHERE a.extraction_status <> 'skipped'
          AND a.version_id IN (SELECT pub.active_version_id FROM pub)
        GROUP BY a.version_id),
    snd AS (
        SELECT DISTINCT ON (cp.communication_id) cp.communication_id, p.email_normalized
        FROM communication_participants cp JOIN participants p ON p.id = cp.participant_id
        WHERE cp.role = 'from' AND cp.communication_id IN (SELECT pub.id FROM pub)
        ORDER BY cp.communication_id)
    SELECT pc.id, pc.corpus_id, pc.occurred_at, pc.title, pc.source_url,
           pc.primary_smtp_address, pc.direction, pc.internet_message_id,
           pc.content_fingerprint, snd.email_normalized,
           pc.disposition, pc.summary, pc.action_text, pc.action_for, pc.due_text,
           pc.risk_text, coalesce(pc.properties, '{}'), coalesce(pc.entities, '{}'),
           coalesce(lab.yes, '{}'), coalesce(lab.uncertain, '{}'), coalesce(att.n, 0),
           rr.impact, rr.likelihood, rr.reason, pc.conversation_id
    FROM pub pc
    LEFT JOIN lab ON lab.run_id = pc.run_id
    LEFT JOIN att ON att.version_id = pc.active_version_id
    LEFT JOIN snd ON snd.communication_id = pc.id
    LEFT JOIN risk_ratings rr ON rr.version_id = pc.active_version_id;
END
$$;
