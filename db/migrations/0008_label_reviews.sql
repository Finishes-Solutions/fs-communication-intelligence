-- 0008_label_reviews.sql
-- Hand review of classifier labels (CLAUDE.md section 11: precision and recall
-- per label on a hand-labelled sample). A reviewer says whether each label
-- applies to a published message; the AI's decision and probability at that
-- moment are copied next to it, so later reclassification does not rewrite
-- history. Reviews hold label decisions only, never message text.

SET search_path = ci, public;

-- Per-label classifier output of published messages, for the review screen.
-- Same exposure as published_classifications, plus probabilities.
CREATE VIEW published_classification_labels AS
SELECT pc.id AS communication_id, pc.corpus_id, r.id AS run_id, r.model, r.policy_version,
       l.label, l.probability, l.decision
FROM published_communications pc
JOIN LATERAL (
    SELECT cr.id, cr.model, cr.policy_version FROM classification_runs cr
    WHERE cr.version_id = pc.active_version_id ORDER BY cr.id DESC LIMIT 1
) r ON true
JOIN classification_labels l ON l.run_id = r.id;

CREATE TABLE label_reviews (
    id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    communication_id uuid NOT NULL REFERENCES communications(id) ON DELETE CASCADE,
    run_id           bigint NOT NULL REFERENCES classification_runs(id) ON DELETE CASCADE,
    label            text NOT NULL,
    ai_decision      text NOT NULL CHECK (ai_decision IN ('no', 'yes', 'review', 'unresolved')),
    ai_probability   real CHECK (ai_probability IS NULL
                                 OR (ai_probability >= 0 AND ai_probability <= 1)),
    human_decision   text NOT NULL CHECK (human_decision IN ('yes', 'no')),
    -- How the message was chosen. Only 'random' gives unbiased precision/recall.
    sample           text NOT NULL CHECK (sample IN ('random', 'uncertain', 'label', 'browse')),
    model            text NOT NULL,
    policy_version   text NOT NULL,
    reviewer_id      text NOT NULL,      -- Entra object id
    reviewed_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (run_id, label, reviewer_id)
);
CREATE INDEX label_reviews_label ON label_reviews (label, policy_version);
CREATE INDEX label_reviews_reviewer ON label_reviews (reviewer_id, communication_id);

GRANT SELECT ON published_classification_labels TO ci_search;
GRANT SELECT, INSERT, UPDATE, DELETE ON label_reviews TO ci_operator;
