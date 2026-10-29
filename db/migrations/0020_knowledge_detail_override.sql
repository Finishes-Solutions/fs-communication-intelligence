-- 0020_knowledge_detail_override.sql
-- Correct a knowledge entry's detail by hand (Matthew, 2026-10-29: the base
-- showed Chuck Cates with Chuck Jr.'s title). The detail is a person's title,
-- a term's meaning or an organization's domain. An operator-set value wins
-- over what emails said; without one, readers see the value stated in the most
-- messages (ci_retrieval.knowledge), so one wrong email no longer replaces it.
-- Audited like the other corrections (ci_core.operator.set_knowledge_detail).

SET search_path = ci, public;

ALTER TABLE kg_entities ADD COLUMN IF NOT EXISTS detail_override text
    CHECK (detail_override IS NULL OR length(detail_override) BETWEEN 1 AND 200);

-- Same view as 0016 with the root entry's detail_override appended.
CREATE OR REPLACE VIEW published_kg_mentions AS
SELECT pc.id AS communication_id, pc.corpus_id, pc.occurred_at,
       v.entity_id, r.kind, r.key, r.name_override, m.written, m.detail,
       r.detail_override
FROM published_communications pc
JOIN kg_mentions m ON m.version_id = pc.active_version_id
JOIN kg_visible_entities v ON v.id = m.entity_id
JOIN kg_entities r ON r.id = v.entity_id;
