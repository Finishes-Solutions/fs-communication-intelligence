-- 0023_learn_from_reviews.sql
-- The classifier learns from Label Review (Matthew, 2026-10-29: "add the AI
-- learning from my label answers"). Each classification call shows the model a
-- few emails a reviewer judged, with the reviewer's answers, as examples:
-- corrections first (where the reviewer disagreed with the model), from the
-- same sender domain when there are any (ci_email.review_examples).
--
-- review_examples is how many per call; 0 turns it off. Each example costs
-- roughly 150 to 250 input tokens. Thresholds still change only by a person.
-- Processing (ci_ingest) reads the reviewers' answers; it already reads the
-- emails' stored (masked) text.

SET search_path = ci, public;

ALTER TABLE ops_settings
    ADD COLUMN IF NOT EXISTS review_examples smallint NOT NULL DEFAULT 6
        CHECK (review_examples BETWEEN 0 AND 12);

GRANT SELECT ON label_feedback TO ci_ingest;
