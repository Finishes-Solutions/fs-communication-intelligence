-- 0017_risk_ratings.sql
-- A 3x3 risk matrix on the console's Risk page (Matthew, 2026-10-29): every
-- email the classifier marks as a project risk also gets an AI rating of
-- impact and likelihood (low / medium / high) and a one-line reason. The
-- rating is a separate, small model call made by the processing run for risk
-- emails only (ci_email.risk_ratings), new and earlier ones alike; identical
-- copies of one email in several mailboxes share one call through
-- input_sha256. Ratings are AI proposals, uncalibrated until reviewed.

SET search_path = ci, public;

CREATE TABLE IF NOT EXISTS risk_ratings (
    version_id     uuid PRIMARY KEY REFERENCES communication_versions(id) ON DELETE CASCADE,
    impact         text NOT NULL CHECK (impact IN ('low', 'medium', 'high')),
    likelihood     text NOT NULL CHECK (likelihood IN ('low', 'medium', 'high')),
    reason         text CHECK (reason IS NULL OR length(reason) <= 200),
    input_sha256   text NOT NULL,
    model          text NOT NULL,
    rating_version text NOT NULL,
    rated_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS risk_ratings_input
    ON risk_ratings (input_sha256, model, rating_version);

-- Ratings of published versions only, with the corpus for authorization.
CREATE OR REPLACE VIEW published_risk_ratings AS
SELECT pc.id AS communication_id, pc.corpus_id, r.impact, r.likelihood, r.reason
FROM published_communications pc
JOIN risk_ratings r ON r.version_id = pc.active_version_id;

GRANT SELECT, INSERT, UPDATE, DELETE ON risk_ratings TO ci_ingest;
GRANT SELECT ON published_risk_ratings TO ci_search;
