-- 0008_label_feedback.sql
-- Ground truth from reviewers in the admin console ("Label check"): does a
-- label apply to this email, and is it really an action item for a person?
-- Used to measure precision and recall per label and to calibrate thresholds
-- (CLAUDE.md sections 7 and 11). Holds judgements and reason codes only, never
-- content. The calibration view pairs each judgement with the model's current
-- probability and decision for that email.

SET search_path = ci, public;

CREATE TABLE label_feedback (
    id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    communication_id uuid NOT NULL REFERENCES communications(id) ON DELETE CASCADE,
    subject_kind     text NOT NULL CHECK (subject_kind IN ('label', 'action_for')),
    subject          text NOT NULL,          -- a label name, or a person's address
    applies          boolean NOT NULL,
    reason_code      text CHECK (reason_code IS NULL OR reason_code ~ '^[a-z0-9_]{2,40}$'),
    reviewer         text NOT NULL,
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now(),
    UNIQUE (communication_id, subject_kind, subject, reviewer)
);

CREATE VIEW ops_label_calibration AS
SELECT f.subject AS label, f.applies, f.reviewer, f.communication_id,
       l.probability, l.decision
FROM label_feedback f
JOIN communications c ON c.id = f.communication_id
JOIN LATERAL (SELECT r.id FROM classification_runs r
              WHERE r.version_id = c.active_version_id ORDER BY r.id DESC LIMIT 1) run ON true
JOIN classification_labels l ON l.run_id = run.id AND l.label = f.subject
WHERE f.subject_kind = 'label';

CREATE VIEW ops_action_feedback AS
SELECT f.subject AS person, f.applies, f.reason_code, f.reviewer, f.communication_id
FROM label_feedback f WHERE f.subject_kind = 'action_for';

-- Per-label scores of published mail, for showing the model's answer next to
-- the reviewer's. Built on published_communications; callers still apply
-- the caller's grants (corpus_id) like every other published_* view.
CREATE VIEW published_label_scores AS
SELECT pc.id AS communication_id, pc.corpus_id, l.label, l.probability, l.decision
FROM published_communications pc
JOIN LATERAL (SELECT r.id FROM classification_runs r
              WHERE r.version_id = pc.active_version_id ORDER BY r.id DESC LIMIT 1) run ON true
JOIN classification_labels l ON l.run_id = run.id;

GRANT SELECT ON published_label_scores TO ci_search;
GRANT SELECT, INSERT, UPDATE, DELETE ON label_feedback TO ci_operator;
GRANT SELECT ON ops_label_calibration, ops_action_feedback TO ci_operator;
