-- 0026_action_resolutions.sql
-- Whether an action item has been dealt with over email (Matthew, 2026-11-03:
-- "it never shows when an action item has been resolved"). For each published
-- email the classifier marked action_required or approval_required, a later
-- email in the same conversation triggers one small model call that reads the
-- request and the replies and answers resolved / in_progress / open, with a
-- one-line reason and the email that settled it (ci_email.resolutions). One row
-- per action item, replaced when a newer reply arrives until it is resolved.
-- AI proposals, uncalibrated until reviewed; the resolving email is shown to a
-- reader only when they may read it.

SET search_path = ci, public;

CREATE TABLE IF NOT EXISTS action_resolutions (
    version_id                uuid PRIMARY KEY
                              REFERENCES communication_versions(id) ON DELETE CASCADE,
    status                    text NOT NULL CHECK (status IN ('resolved', 'in_progress', 'open')),
    confidence                real CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1)),
    reason                    text CHECK (reason IS NULL OR length(reason) <= 200),
    resolver_communication_id uuid REFERENCES communications(id) ON DELETE SET NULL,
    resolver_at               timestamptz,
    checked_through           timestamptz NOT NULL,   -- newest reply the call saw
    replies_seen              integer NOT NULL DEFAULT 0,
    input_sha256              text NOT NULL,
    model                     text NOT NULL,
    resolution_version        text NOT NULL,
    checked_at                timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS action_resolutions_input
    ON action_resolutions (input_sha256, model, resolution_version);
CREATE INDEX IF NOT EXISTS action_resolutions_open
    ON action_resolutions (checked_through) WHERE status <> 'resolved';

-- Resolutions of published action items, with the corpus for authorization.
-- The resolving email is a communication id; retrieval checks the reader may
-- read it before naming it.
CREATE OR REPLACE VIEW published_action_resolutions AS
SELECT pc.id AS communication_id, pc.corpus_id, r.status, r.confidence, r.reason,
       r.resolver_communication_id, r.resolver_at, r.checked_through, r.replies_seen,
       r.checked_at
FROM published_communications pc
JOIN action_resolutions r ON r.version_id = pc.active_version_id;

GRANT SELECT, INSERT, UPDATE, DELETE ON action_resolutions TO ci_ingest;
GRANT SELECT ON published_action_resolutions TO ci_search;
GRANT SELECT ON published_action_resolutions TO ci_operator;
