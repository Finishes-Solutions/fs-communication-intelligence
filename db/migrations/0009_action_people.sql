-- 0009_action_people.sql
-- Step 2 of action items (ci_processing.routing): per-person checks.
-- action_people holds administrator-written profiles (who someone is, their
-- addresses and role, and who they must not be confused with, e.g. Chuck Sr.
-- versus Chuck Jr.). action_routes holds one decision per processed version
-- and person: a probability, a person-specific action line and a reason code.
-- Profiles are configuration, not evidence; routes follow publication like
-- every other derived row and are read only through published_action_routes.

SET search_path = ci, public;

CREATE TABLE action_people (
    key            text PRIMARY KEY CHECK (key ~ '^[a-z0-9_]{2,40}$'),
    display_name   text NOT NULL,
    addresses      text[] NOT NULL CHECK (cardinality(addresses) > 0),
    role           text NOT NULL DEFAULT '',
    not_to_confuse text NOT NULL DEFAULT '',
    enabled        boolean NOT NULL DEFAULT true,
    updated_by     text NOT NULL,
    updated_at     timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE action_routes (
    version_id     uuid NOT NULL REFERENCES communication_versions(id) ON DELETE CASCADE,
    person_key     text NOT NULL REFERENCES action_people(key) ON DELETE CASCADE,
    input_sha256   text NOT NULL,
    probability    double precision,
    decision       text NOT NULL CHECK (decision IN ('yes', 'no', 'review', 'unresolved')),
    action_text    text,
    reason_code    text,
    model          text NOT NULL,
    policy_version text NOT NULL,
    created_at     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (version_id, person_key)
);
CREATE INDEX action_routes_reuse ON action_routes (input_sha256, person_key, policy_version);

CREATE VIEW published_action_routes AS
SELECT pc.id AS communication_id, pc.corpus_id, r.person_key, r.probability, r.decision,
       r.action_text, r.reason_code
FROM published_communications pc
JOIN action_routes r ON r.version_id = pc.active_version_id;

GRANT SELECT ON action_people TO ci_ingest, ci_search, ci_operator;
GRANT INSERT, UPDATE ON action_people TO ci_operator;
GRANT SELECT, INSERT, UPDATE, DELETE ON action_routes TO ci_ingest;
GRANT SELECT ON published_action_routes TO ci_search;
