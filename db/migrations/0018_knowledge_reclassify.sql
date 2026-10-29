-- 0018_knowledge_reclassify.sql
-- Fix the kind the AI gave a knowledge entry (Matthew, 2026-10-29: people
-- classified as projects should be reclassified or merged with the person).
-- Reclassifying finds or creates an entry of the right kind and merges the
-- wrong one into it (ci_core.operator.reclassify_knowledge), so later emails
-- that repeat the mistake land on the merged entry and still show under the
-- right kind; Unmerge undoes it. The operator role needs INSERT for the new
-- entry. Merges may now cross kinds (the merged entry takes the target's
-- kind), except that a person never becomes another kind: person entries are
-- keyed by address and checked against participant exclusions.

SET search_path = ci, public;

GRANT INSERT ON kg_entities TO ci_operator;
