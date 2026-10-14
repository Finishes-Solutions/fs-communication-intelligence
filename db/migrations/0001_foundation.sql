-- 0001_foundation.sql
-- Source-neutral foundation shared by email (first source, ADR-014), Teams and
-- any later source. Provider-specific detail lives in extension tables
-- (0002_email.sql) or content-free `provider_attributes` JSONB.
--
-- Checked in CI against PostgreSQL 18 + pgvector 0.8.2 (db/run_checks.py).
-- Not yet applied to any Azure database. Apply with db/migrate.py, which runs
-- each file in one transaction and records it in ci.schema_migrations, so
-- files must not contain BEGIN/COMMIT. Never edit a migration after it has
-- been applied anywhere; add a new one.
--
-- Azure Database for PostgreSQL Flexible Server: add VECTOR to the
-- azure.extensions server parameter before applying this migration.

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pgcrypto;  -- gen_random_uuid() on PG < 13 images

CREATE SCHEMA IF NOT EXISTS ci;
SET search_path = ci, public;

-- ---------------------------------------------------------------------------
-- Datasets and runtime mode (replaces App Configuration for the pilot; ADR-011)
-- ---------------------------------------------------------------------------

CREATE TABLE datasets (
    dataset_key text PRIMARY KEY CHECK (dataset_key IN ('test', 'production')),
    description text NOT NULL
);
INSERT INTO datasets VALUES
    ('test', 'Explicitly selected test scopes'),
    ('production', 'Production-approved scopes');

-- Exactly one active revision. Workers stamp jobs with the revision they read
-- and must re-check it before source reads and before publication.
CREATE TABLE runtime_config (
    revision          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    mode              text NOT NULL CHECK (mode IN ('test', 'production', 'paused')),
    enabled_providers text[] NOT NULL DEFAULT '{}',
    is_active         boolean NOT NULL DEFAULT false,
    activated_at      timestamptz,
    activated_by      text NOT NULL,
    notes             text,
    created_at        timestamptz NOT NULL DEFAULT now(),
    CHECK (enabled_providers <@ ARRAY['email', 'teams', 'fireflies', 'dialpad']::text[])
);
CREATE UNIQUE INDEX runtime_config_one_active ON runtime_config ((true)) WHERE is_active;

-- The dataset a mode writes to / reads from. Paused has none.
CREATE FUNCTION active_dataset() RETURNS text LANGUAGE sql STABLE AS $$
    SELECT CASE mode WHEN 'test' THEN 'test' WHEN 'production' THEN 'production' END
    FROM ci.runtime_config WHERE is_active
$$;

-- ---------------------------------------------------------------------------
-- Sources, scopes, corpora and reader grants
-- ---------------------------------------------------------------------------

CREATE TABLE source_connections (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    provider       text NOT NULL CHECK (provider IN ('email', 'teams', 'fireflies', 'dialpad')),
    account_ref    text NOT NULL,          -- Entra tenant id, provider account, ...
    display_name   text NOT NULL,
    credential_ref text,                   -- Key Vault secret NAME, never a value
    enabled        boolean NOT NULL DEFAULT false,
    created_at     timestamptz NOT NULL DEFAULT now(),
    UNIQUE (provider, account_ref)
);

-- A corpus is the unit readers are granted, e.g. 'email-enrolled'.
CREATE TABLE corpora (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    corpus_key  text NOT NULL UNIQUE,
    description text NOT NULL
);

-- Capture scopes: a mailbox, a Teams chat or channel, ...
-- scope_state is what inventory observed (written by ingestion); the
-- *_approved flags are operator decisions (ingestion cannot change them).
-- A scope is captured and searchable only when it is 'enrolled', approved for
-- the active dataset, and has no active exclusion. Exclusions always win.
CREATE TABLE source_scopes (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    connection_id       uuid NOT NULL REFERENCES source_connections(id),
    scope_kind          text NOT NULL CHECK (scope_kind IN ('mailbox', 'teams_chat', 'teams_channel',
                                                            'fireflies_user', 'dialpad_user')),
    native_scope_id     text NOT NULL,     -- mailbox Entra object id, chat id, ...
    display_label       text NOT NULL,     -- non-sensitive label for operators
    corpus_id           uuid NOT NULL REFERENCES corpora(id),
    scope_state         text NOT NULL DEFAULT 'not_approved'
                        CHECK (scope_state IN ('enrolled', 'excluded_by_rule', 'not_approved',
                                               'no_exchange_access', 'no_mailbox', 'paused')),
    state_reason_code   text,
    state_checked_at    timestamptz,
    test_approved       boolean NOT NULL DEFAULT false,
    production_approved boolean NOT NULL DEFAULT false,
    backfill_since      timestamptz,       -- NULL blocks initial capture
    policy_version      text NOT NULL,
    approved_by         text,
    approved_at         timestamptz,
    UNIQUE (connection_id, scope_kind, native_scope_id)
);

-- Grants are to Entra app roles (token `roles` claim) or individual users.
-- The API derives principals from the validated token only.
CREATE TABLE access_grants (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    corpus_id           uuid NOT NULL REFERENCES corpora(id),
    principal_type      text NOT NULL CHECK (principal_type IN ('entra_app_role', 'entra_user')),
    principal_id        text NOT NULL,     -- app role value or user object id
    granted_by          text NOT NULL,
    valid_from          timestamptz NOT NULL DEFAULT now(),
    valid_to            timestamptz,
    revoked_at          timestamptz,
    UNIQUE (corpus_id, principal_type, principal_id, valid_from)
);

-- Operator exclusions. Checked at capture (before content is staged) and
-- again by the retrieval views, so an exclusion added later still hides
-- evidence already indexed. Kinds:
--   item                 one source item (connection + native id)
--   scope                a whole scope, e.g. a mailbox (scope kind + native scope id)
--   participant_address  any occurrence with this participant address, in any mailbox
--   participant_domain   any occurrence with a participant in this domain (pattern = 'example.com')
-- Excluding a mailbox does not exclude that person's mail in other mailboxes;
-- use participant_address for that.
CREATE TABLE exclusions (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    exclusion_kind  text NOT NULL CHECK (exclusion_kind IN ('item', 'scope', 'participant_address',
                                                            'participant_domain')),
    connection_id   uuid REFERENCES source_connections(id),
    native_id       text,
    scope_kind      text,
    native_scope_id text,
    pattern         text CHECK (pattern = lower(pattern) AND pattern NOT LIKE '@%'),
    reason_code     text NOT NULL,
    created_by      text NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now(),
    revoked_at      timestamptz,
    revoked_by      text,
    CHECK (CASE exclusion_kind
        WHEN 'item' THEN connection_id IS NOT NULL AND native_id IS NOT NULL
                         AND scope_kind IS NULL AND native_scope_id IS NULL AND pattern IS NULL
        WHEN 'scope' THEN scope_kind IS NOT NULL AND native_scope_id IS NOT NULL
                          AND native_id IS NULL AND pattern IS NULL
        ELSE pattern IS NOT NULL AND native_id IS NULL AND scope_kind IS NULL
             AND native_scope_id IS NULL
    END),
    CHECK ((revoked_at IS NULL) = (revoked_by IS NULL))
);
CREATE UNIQUE INDEX exclusions_active_item ON exclusions (connection_id, native_id)
    WHERE exclusion_kind = 'item' AND revoked_at IS NULL;
CREATE UNIQUE INDEX exclusions_active_scope ON exclusions (scope_kind, native_scope_id)
    WHERE exclusion_kind = 'scope' AND revoked_at IS NULL;
CREATE UNIQUE INDEX exclusions_active_pattern ON exclusions (exclusion_kind, pattern)
    WHERE pattern IS NOT NULL AND revoked_at IS NULL;

-- ---------------------------------------------------------------------------
-- Communications, versions and evidence
-- ---------------------------------------------------------------------------

-- Identity: dataset + connection (provider/account) + provider native id.
-- Email immutable ids are per-mailbox, so each mailbox copy is its own row
-- with one communication_scopes link (docs/EMAIL_DEDUPLICATION.md). A source
-- whose items are shared across scopes may link one row to several scopes.
CREATE TABLE communications (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    dataset_key         text NOT NULL REFERENCES datasets(dataset_key),
    connection_id       uuid NOT NULL REFERENCES source_connections(id),
    native_id           text NOT NULL,
    kind                text NOT NULL CHECK (kind IN ('meeting', 'email', 'chat_message', 'channel_message', 'call')),
    corpus_id           uuid NOT NULL REFERENCES corpora(id),
    title               text,
    occurred_at         timestamptz NOT NULL,          -- UTC start / sent time
    ended_at            timestamptz,
    duration_seconds    integer,
    source_url          text,
    lifecycle_state     text NOT NULL DEFAULT 'active'
                        CHECK (lifecycle_state IN ('active', 'deleted_at_source', 'withdrawn', 'excluded')),
    active_version_id   uuid,                           -- FK added below; the publication fence
    provider_attributes jsonb NOT NULL DEFAULT '{}',    -- content-free provider metadata
    first_seen_at       timestamptz NOT NULL DEFAULT now(),
    last_seen_at        timestamptz NOT NULL DEFAULT now(),
    UNIQUE (dataset_key, connection_id, native_id)
);
CREATE INDEX communications_occurred ON communications (dataset_key, occurred_at DESC);

CREATE TABLE communication_scopes (
    communication_id uuid NOT NULL REFERENCES communications(id) ON DELETE CASCADE,
    scope_id         uuid NOT NULL REFERENCES source_scopes(id),
    first_seen_at    timestamptz NOT NULL DEFAULT now(),
    last_seen_at     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (communication_id, scope_id)
);

-- One row per processed source revision x processing profile.
CREATE TABLE communication_versions (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    communication_id  uuid NOT NULL REFERENCES communications(id) ON DELETE CASCADE,
    version_no        integer NOT NULL,
    source_revision   text NOT NULL,     -- sha256 of the canonical normalized source payload
    raw_blob_key      text,              -- private Blob path of the fetched payload
    profile           jsonb NOT NULL,    -- normalizer/chunker/embedding model/policy versions
    processing_state  text NOT NULL DEFAULT 'fetched'
                      CHECK (processing_state IN ('fetched', 'normalized', 'screened', 'embedded',
                                                  'published', 'held', 'rejected', 'superseded',
                                                  'withdrawn', 'failed')),
    hold_reason_code  text,
    created_at        timestamptz NOT NULL DEFAULT now(),
    published_at      timestamptz,
    UNIQUE (communication_id, version_no),
    UNIQUE (communication_id, source_revision, profile),
    CHECK ((processing_state IN ('held', 'rejected')) = (hold_reason_code IS NOT NULL))
);

ALTER TABLE communications
    ADD CONSTRAINT communications_active_version_fk
    FOREIGN KEY (active_version_id) REFERENCES communication_versions(id)
    DEFERRABLE INITIALLY DEFERRED;

CREATE TABLE participants (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    provider          text NOT NULL,
    provider_identity text NOT NULL,     -- normalized email, else provider user/speaker id
    email_normalized  text,
    display_name      text,
    entra_object_id   uuid,              -- resolved later; unresolved identities are kept
    UNIQUE (provider, provider_identity)
);
CREATE INDEX participants_email ON participants (email_normalized);
CREATE INDEX participants_email_domain ON participants (split_part(email_normalized, '@', 2));

CREATE TABLE communication_participants (
    communication_id uuid NOT NULL REFERENCES communications(id) ON DELETE CASCADE,
    participant_id   uuid NOT NULL REFERENCES participants(id),
    role             text NOT NULL CHECK (role IN ('from', 'sender', 'to', 'cc', 'bcc', 'author',
                                                   'mentioned', 'organizer', 'host', 'attendee',
                                                   'speaker', 'caller', 'callee')),
    PRIMARY KEY (communication_id, participant_id, role)
);
CREATE INDEX communication_participants_participant ON communication_participants (participant_id);

CREATE TABLE artifacts (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    version_id  uuid NOT NULL REFERENCES communication_versions(id) ON DELETE CASCADE,
    kind        text NOT NULL CHECK (kind IN ('raw_payload', 'transcript', 'provider_summary',
                                              'email_body', 'attachment', 'extracted_text')),
    blob_key    text,
    mime_type   text,
    sha256      text,
    size_bytes  bigint,
    status      text NOT NULL DEFAULT 'stored',
    UNIQUE (version_id, kind, blob_key)
);

-- Normalized utterances (meeting sources; unused by email). Text lives here (not in Blob) because it is small,
-- cited directly and needed for transcript windows.
CREATE TABLE transcript_segments (
    version_id      uuid NOT NULL REFERENCES communication_versions(id) ON DELETE CASCADE,
    seq             integer NOT NULL,             -- provider sentence index order
    speaker_label   text,                         -- as provided (name or "Speaker 2")
    participant_id  uuid REFERENCES participants(id),
    start_ms        integer,
    end_ms          integer,
    text            text NOT NULL,
    PRIMARY KEY (version_id, seq),
    CHECK (start_ms IS NULL OR end_ms IS NULL OR end_ms >= start_ms)
);

-- Searchable units. Visible only when their version is the communication's
-- active_version_id (single-transaction publication; see docs/DATABASE.md).
CREATE TABLE chunks (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    version_id       uuid NOT NULL REFERENCES communication_versions(id) ON DELETE CASCADE,
    communication_id uuid NOT NULL REFERENCES communications(id) ON DELETE CASCADE,
    chunk_kind       text NOT NULL CHECK (chunk_kind IN ('transcript_window', 'provider_summary',
                                                         'email_body', 'pdf_page', 'chat_context')),
    seq              integer NOT NULL,
    segment_from     integer,                     -- transcript_segments.seq range
    segment_to       integer,
    start_ms         integer,
    end_ms           integer,
    page_from        integer,
    page_to          integer,
    speakers         text[] NOT NULL DEFAULT '{}',
    display_text     text NOT NULL,               -- returned as evidence
    embedding_input_sha256 text NOT NULL,         -- hash of contextualized text actually embedded
    embedding_model  text NOT NULL,
    embedding        vector(1536) NOT NULL,
    tsv              tsvector GENERATED ALWAYS AS (to_tsvector('english', display_text)) STORED,
    created_at       timestamptz NOT NULL DEFAULT now(),
    UNIQUE (version_id, chunk_kind, seq)
);
CREATE INDEX chunks_tsv ON chunks USING gin (tsv);
CREATE INDEX chunks_embedding ON chunks USING hnsw (embedding vector_cosine_ops);
CREATE INDEX chunks_version ON chunks (version_id);

CREATE TABLE relationships (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    from_communication_id uuid NOT NULL REFERENCES communications(id) ON DELETE CASCADE,
    to_communication_id   uuid NOT NULL REFERENCES communications(id) ON DELETE CASCADE,
    kind              text NOT NULL CHECK (kind IN ('same_meeting', 'thread', 'reply', 'related', 'possible_duplicate')),
    provenance        jsonb NOT NULL,
    created_at        timestamptz NOT NULL DEFAULT now(),
    UNIQUE (from_communication_id, to_communication_id, kind)
);

-- Survives purge of the communications row so replays/restores cannot resurrect it.
CREATE TABLE tombstones (
    dataset_key   text NOT NULL REFERENCES datasets(dataset_key),
    connection_id uuid NOT NULL REFERENCES source_connections(id),
    native_id     text NOT NULL,
    reason_code   text NOT NULL,
    created_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (dataset_key, connection_id, native_id)
);

-- ---------------------------------------------------------------------------
-- Durable execution: jobs, checkpoints, rate budgets, webhook receipts, outbox
-- ---------------------------------------------------------------------------

CREATE TABLE jobs (
    id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    dataset_key      text NOT NULL REFERENCES datasets(dataset_key),
    config_revision  bigint NOT NULL REFERENCES runtime_config(revision),
    job_type         text NOT NULL,       -- 'email_fetch', 'process_version', 'withdraw', ...
    dedupe_key       text NOT NULL,       -- e.g. connection_id:native_id
    payload          jsonb NOT NULL DEFAULT '{}',  -- identifiers only, never content
    state            text NOT NULL DEFAULT 'queued'
                     CHECK (state IN ('queued', 'leased', 'succeeded', 'failed', 'dead', 'cancelled')),
    priority         smallint NOT NULL DEFAULT 100, -- lower runs first; live < backfill
    attempts         integer NOT NULL DEFAULT 0,
    max_attempts     integer NOT NULL DEFAULT 8,
    run_after        timestamptz NOT NULL DEFAULT now(),
    lease_owner      text,
    lease_expires_at timestamptz,
    last_error_code  text,                -- content-free
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now()
);
-- Duplicate admission (repeat webhooks, overlapping reconciliation) collapses here.
CREATE UNIQUE INDEX jobs_open_dedupe ON jobs (dataset_key, job_type, dedupe_key)
    WHERE state IN ('queued', 'leased');
CREATE INDEX jobs_ready ON jobs (priority, run_after) WHERE state = 'queued';

CREATE TABLE checkpoints (
    checkpoint_key text PRIMARY KEY,     -- e.g. 'email:<connection>:<mailbox>:<folder>:delta'
    dataset_key    text NOT NULL REFERENCES datasets(dataset_key),
    cursor_value   jsonb NOT NULL,
    updated_at     timestamptz NOT NULL DEFAULT now()
);

-- Shared request budget per provider connection.
CREATE TABLE rate_budgets (
    connection_id  uuid PRIMARY KEY REFERENCES source_connections(id),
    window_seconds integer NOT NULL,
    max_requests   integer NOT NULL,
    window_start   timestamptz NOT NULL DEFAULT now(),
    used           integer NOT NULL DEFAULT 0,
    reserve_for_live integer NOT NULL DEFAULT 0   -- requests backfill may not consume
);

CREATE TABLE webhook_receipts (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    provider        text NOT NULL,
    received_at     timestamptz NOT NULL DEFAULT now(),
    signature_valid boolean NOT NULL,
    event_type      text,
    native_id       text,
    job_id          bigint REFERENCES jobs(id),
    outcome         text NOT NULL        -- 'enqueued', 'ignored_event', 'bad_signature', 'out_of_scope', ...
);

CREATE TABLE outbox (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    destination     text NOT NULL,       -- 'monday', ...
    idempotency_key text NOT NULL UNIQUE,
    payload         jsonb NOT NULL,      -- governed projection only
    state           text NOT NULL DEFAULT 'pending' CHECK (state IN ('pending', 'delivered', 'failed', 'uncertain')),
    attempts        integer NOT NULL DEFAULT 0,
    created_at      timestamptz NOT NULL DEFAULT now(),
    delivered_at    timestamptz
);

-- ---------------------------------------------------------------------------
-- Policy and audit (content-free)
-- ---------------------------------------------------------------------------

CREATE TABLE policy_decisions (
    id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    version_id     uuid NOT NULL REFERENCES communication_versions(id) ON DELETE CASCADE,
    check_name     text NOT NULL,        -- 'pii', 'prompt_shield', 'exclusion', ...
    decision       text NOT NULL CHECK (decision IN ('allow', 'hold', 'reject', 'pending')),
    reason_code    text,
    policy_version text NOT NULL,
    service_ref    text,                 -- service + model/api version
    decided_at     timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE audit_events (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    occurred_at timestamptz NOT NULL DEFAULT now(),
    actor_type  text NOT NULL,           -- 'user', 'service', 'operator'
    actor_id    text NOT NULL,
    action      text NOT NULL,           -- 'search', 'get_email', 'mode_change', 'exclude', ...
    target_type text,
    target_id   text,
    details     jsonb NOT NULL DEFAULT '{}'  -- counts, ids, reason codes; never query text by default
);
CREATE INDEX audit_events_time ON audit_events (occurred_at DESC);

-- ---------------------------------------------------------------------------
-- Retrieval views: the ONLY objects the search role can read content from.
-- They expose published, active evidence in the active dataset, from at least
-- one scope that is enrolled, approved for that dataset and not excluded, with
-- no active item or participant exclusion and no tombstone.
-- Views run with owner privileges, so ci_search never needs base-table access
-- to chunks, versions or segments. Corpus authorization is applied on top by
-- the API query (docs/SEARCH_AND_AGENTS.md).
-- ---------------------------------------------------------------------------

CREATE VIEW published_communications AS
SELECT c.id, c.dataset_key, c.connection_id, c.native_id, c.kind, c.corpus_id, c.title,
       c.occurred_at, c.ended_at, c.duration_seconds, c.source_url, c.active_version_id,
       v.published_at
FROM communications c
JOIN communication_versions v ON v.id = c.active_version_id
WHERE c.lifecycle_state = 'active'
  AND v.processing_state = 'published'
  AND c.dataset_key = ci.active_dataset()
  AND EXISTS (SELECT 1 FROM ci.communication_scopes cs
              JOIN ci.source_scopes s ON s.id = cs.scope_id
              WHERE cs.communication_id = c.id
                AND s.scope_state = 'enrolled'
                AND CASE c.dataset_key WHEN 'test' THEN s.test_approved
                                       WHEN 'production' THEN s.production_approved END
                AND NOT EXISTS (SELECT 1 FROM ci.exclusions e
                                WHERE e.exclusion_kind = 'scope' AND e.revoked_at IS NULL
                                  AND e.scope_kind = s.scope_kind
                                  AND e.native_scope_id = s.native_scope_id))
  AND NOT EXISTS (SELECT 1 FROM ci.exclusions e
                  WHERE e.exclusion_kind = 'item' AND e.revoked_at IS NULL
                    AND e.connection_id = c.connection_id AND e.native_id = c.native_id)
  AND NOT EXISTS (SELECT 1 FROM ci.communication_participants cp
                  JOIN ci.participants p ON p.id = cp.participant_id
                  JOIN ci.exclusions e ON e.revoked_at IS NULL
                   AND ((e.exclusion_kind = 'participant_address'
                         AND e.pattern = p.email_normalized)
                     OR (e.exclusion_kind = 'participant_domain'
                         AND e.pattern = split_part(p.email_normalized, '@', 2)))
                  WHERE cp.communication_id = c.id)
  AND NOT EXISTS (SELECT 1 FROM ci.tombstones t
                  WHERE t.dataset_key = c.dataset_key AND t.connection_id = c.connection_id
                    AND t.native_id = c.native_id);

CREATE VIEW published_chunks AS
SELECT ch.id, ch.communication_id, ch.chunk_kind, ch.seq, ch.segment_from, ch.segment_to,
       ch.start_ms, ch.end_ms, ch.page_from, ch.page_to, ch.speakers, ch.display_text,
       ch.embedding_model, ch.embedding, ch.tsv,
       pc.corpus_id, pc.title, pc.occurred_at, pc.kind, pc.source_url
FROM chunks ch
JOIN published_communications pc ON pc.id = ch.communication_id
                                AND pc.active_version_id = ch.version_id;

CREATE VIEW published_segments AS
SELECT s.seq, s.speaker_label, s.start_ms, s.end_ms, s.text,
       pc.id AS communication_id, pc.corpus_id
FROM transcript_segments s
JOIN published_communications pc ON pc.active_version_id = s.version_id;

CREATE VIEW published_participants AS
SELECT cp.communication_id, cp.role, p.display_name, p.email_normalized, pc.corpus_id
FROM communication_participants cp
JOIN participants p ON p.id = cp.participant_id
JOIN published_communications pc ON pc.id = cp.communication_id;

-- ---------------------------------------------------------------------------
-- Roles. Azure managed identities are created with pgaadauth_create_principal
-- outside this migration and then GRANTed membership in these NOLOGIN roles.
-- ---------------------------------------------------------------------------

DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ci_ingest') THEN CREATE ROLE ci_ingest NOLOGIN; END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ci_search') THEN CREATE ROLE ci_search NOLOGIN; END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ci_operator') THEN CREATE ROLE ci_operator NOLOGIN; END IF;
END $$;

GRANT USAGE ON SCHEMA ci TO ci_ingest, ci_search, ci_operator;

-- Ingestion/processing: read/write evidence and execution state; records what
-- inventory observed about scopes; cannot change mode, scope approvals,
-- exclusions or reader grants.
GRANT SELECT ON ALL TABLES IN SCHEMA ci TO ci_ingest;
GRANT INSERT, UPDATE, DELETE ON communications, communication_scopes, communication_versions,
    participants, communication_participants, artifacts, transcript_segments, chunks,
    relationships, tombstones, jobs, checkpoints, rate_budgets, webhook_receipts, outbox,
    policy_decisions TO ci_ingest;
GRANT INSERT (connection_id, scope_kind, native_scope_id, display_label, corpus_id, scope_state,
              state_reason_code, state_checked_at, policy_version)
    ON source_scopes TO ci_ingest;
GRANT UPDATE (display_label, scope_state, state_reason_code, state_checked_at)
    ON source_scopes TO ci_ingest;
GRANT INSERT ON audit_events TO ci_ingest;

-- Search: read-only over what retrieval needs; append-only audit.
GRANT SELECT ON published_communications, published_chunks, published_segments,
    published_participants, access_grants, corpora, runtime_config TO ci_search;
GRANT INSERT ON audit_events TO ci_search;
GRANT EXECUTE ON FUNCTION active_dataset() TO ci_ingest, ci_search, ci_operator;

-- Operator (admin_mode function / approved administrators): mode, scope
-- approvals, reader grants and exclusions. No access to evidence content.
GRANT SELECT, INSERT, UPDATE ON runtime_config, source_connections, source_scopes, corpora,
    access_grants, exclusions, rate_budgets TO ci_operator;
GRANT SELECT ON datasets TO ci_operator;
GRANT INSERT ON audit_events TO ci_operator;
