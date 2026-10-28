-- 0004_project_risk.sql
-- The classifier also describes the project risk an email signals (label
-- project_risk, decided by Matthew 2026-10-28: "risks" for executives are
-- risks to a project, not security items). AI-generated, a proposal only.

SET search_path = ci, public;

ALTER TABLE classification_runs ADD COLUMN risk_text text;

-- Same view as 0003 with risk_text appended (existing columns keep their order).
CREATE OR REPLACE VIEW published_classifications AS
SELECT DISTINCT ON (pc.id)
       pc.id AS communication_id, pc.corpus_id, r.disposition, r.summary, r.action_text,
       r.action_for, r.due_text, r.model, r.policy_version,
       ARRAY(SELECT l.label FROM classification_labels l
             WHERE l.run_id = r.id AND l.decision = 'yes' ORDER BY l.label) AS yes_labels,
       ARRAY(SELECT l.label FROM classification_labels l
             WHERE l.run_id = r.id AND l.decision IN ('review', 'unresolved')
             ORDER BY l.label) AS uncertain_labels,
       r.risk_text
FROM published_communications pc
JOIN classification_runs r ON r.version_id = pc.active_version_id
ORDER BY pc.id, r.id DESC;
