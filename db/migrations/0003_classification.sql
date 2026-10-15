-- 0003_classification.sql
-- Classification proposals (ADR-017) and the content-free views search needs
-- to flag review items and report coverage. A proposal is never an approval:
-- it records what a classifier said, under which model, policy and input, and
-- which outcome the policy derived from it.

SET search_path = ci, public;

-- One classifier pass over one processed version.
CREATE TABLE classification_runs (
    id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    version_id         uuid NOT NULL REFERENCES communication_versions(id) ON DELETE CASCADE,
    classifier_backend text NOT NULL CHECK (classifier_backend IN ('azure_openai', 'decisions_api', 'fake')),
    model              text NOT NULL,
    policy_version     text NOT NULL,     -- label set + thresholds + outcome rules
    input_sha256       text NOT NULL,     -- hash of the exact classifier input
    outcome            text NOT NULL CHECK (outcome IN ('index', 'review', 'restricted', 'not_indexed')),
    outcome_reasons    text[] NOT NULL DEFAULT '{}',   -- reason codes, never content
    created_at         timestamptz NOT NULL DEFAULT now(),
    UNIQUE (version_id, policy_version, input_sha256)
);

-- Per-label answers. A missing answer is 'unresolved' with a NULL
-- probability, never 0.
CREATE TABLE classification_labels (
    run_id         uuid NOT NULL REFERENCES classification_runs(id) ON DELETE CASCADE,
    label          text NOT NULL,
    probability    numeric(5, 4) CHECK (probability IS NULL OR probability BETWEEN 0 AND 1),
    no_threshold   numeric(5, 4) NOT NULL,
    yes_threshold  numeric(5, 4) NOT NULL,
    decision       text NOT NULL CHECK (decision IN ('yes', 'no', 'review', 'unresolved')),
    PRIMARY KEY (run_id, label),
    CHECK ((probability IS NULL) = (decision = 'unresolved'))
);

-- Review flags for published evidence ("indexed and flagged").
CREATE VIEW published_review_flags AS
SELECT pc.id AS communication_id, pc.corpus_id, r.outcome_reasons
FROM published_communications pc
JOIN classification_runs r ON r.version_id = pc.active_version_id
WHERE r.outcome = 'review';

-- Coverage counts for the search coverage block: how many items in the active
-- dataset are held or deliberately not indexed, per corpus. Counts only.
CREATE VIEW coverage_summary AS
SELECT c.corpus_id,
       count(*) FILTER (WHERE v.processing_state = 'held') AS held_count,
       count(*) FILTER (WHERE v.processing_state = 'rejected') AS not_indexed_count
FROM communications c
JOIN LATERAL (SELECT processing_state FROM communication_versions cv
              WHERE cv.communication_id = c.id
              ORDER BY cv.version_no DESC LIMIT 1) v ON true
WHERE c.dataset_key = active_dataset()
  AND c.lifecycle_state = 'active'
GROUP BY c.corpus_id;

GRANT SELECT, INSERT, UPDATE, DELETE ON classification_runs, classification_labels TO ci_ingest;
GRANT SELECT ON published_review_flags, coverage_summary TO ci_search;
