-- 0019_knowledge_meetings.sql
-- A fifth knowledge kind, "meeting" (Matthew, 2026-10-29): recurring or named
-- meetings and events, e.g. a weekly ops meeting or an OAC meeting. Without it
-- the model filed a meeting under a person. Two new facts: a meeting is
-- "about" a property, and a person "attends" a meeting.
--
-- Apply before or right after the code deploys. Until it is applied, an email
-- whose knowledge names a meeting is still published, but its knowledge is
-- not recorded (logged); `ci knowledge backfill` adds it afterwards.

SET search_path = ci, public;

ALTER TABLE kg_entities DROP CONSTRAINT IF EXISTS kg_entities_kind_check;
ALTER TABLE kg_entities ADD CONSTRAINT kg_entities_kind_check
    CHECK (kind IN ('property', 'organization', 'person', 'term', 'meeting'));

ALTER TABLE kg_relations DROP CONSTRAINT IF EXISTS kg_relations_relation_check;
ALTER TABLE kg_relations ADD CONSTRAINT kg_relations_relation_check
    CHECK (relation IN ('works_for', 'role_on', 'owns', 'affiliate_of', 'about', 'attends'));
