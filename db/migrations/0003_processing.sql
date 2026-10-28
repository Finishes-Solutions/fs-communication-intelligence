-- 0003_processing.sql
-- Processing state for the first real-mail run: normalized text per version,
-- attachment extraction results, classification proposals, and the read-only
-- views search uses for excerpts, labels and coverage.
--
-- Text lives in PostgreSQL (not Blob) for now: one store, cascade deletes with
-- the version, and no extra storage role assignments for the first run. Move
-- originals and large text to Blob when E2 adds Defender for Storage.

SET search_path = ci, public;

-- Normalized email body and extracted attachment text, per version.
CREATE TABLE version_texts (
    version_id  uuid NOT NULL REFERENCES communication_versions(id) ON DELETE CASCADE,
    text_kind   text NOT NULL CHECK (text_kind IN ('email_body', 'attachment')),
    ref         text NOT NULL,           -- 'body' or the native attachment id
    content     text NOT NULL,
    sha256      text NOT NULL,           -- of content
    truncated   boolean NOT NULL DEFAULT false,
    created_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (version_id, text_kind, ref)
);

-- Extraction outcome per attachment. sha256 is of the downloaded bytes, so the
-- same file in several messages is extracted once.
ALTER TABLE email_attachments
    ADD COLUMN sha256 text,
    ADD COLUMN extraction_status text NOT NULL DEFAULT 'not_attempted'
        CHECK (extraction_status IN ('not_attempted', 'extracted', 'no_text', 'unsupported',
                                     'too_large', 'failed', 'timeout')),
    ADD COLUMN extractor text,
    ADD COLUMN extracted_chars integer;
CREATE INDEX email_attachments_sha256 ON email_attachments (sha256) WHERE sha256 IS NOT NULL;

-- Attachment text becomes its own chunk kind; source_ref names the attachment.
ALTER TABLE chunks DROP CONSTRAINT chunks_chunk_kind_check;
ALTER TABLE chunks ADD CONSTRAINT chunks_chunk_kind_check
    CHECK (chunk_kind IN ('transcript_window', 'provider_summary', 'email_body', 'pdf_page',
                          'chat_context', 'attachment_text'));
ALTER TABLE chunks ADD COLUMN source_ref text;

-- Content-free error code for failed processing (retry with `ci process --retry-failed`).
ALTER TABLE communication_versions ADD COLUMN last_error_code text;

-- Classification proposals (CLAUDE.md section 7). Never approvals. summary,
-- action_text, action_for and due_text are AI-generated and labelled as such.
CREATE TABLE classification_runs (
    id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    version_id     uuid NOT NULL REFERENCES communication_versions(id) ON DELETE CASCADE,
    model          text NOT NULL,
    policy_version text NOT NULL,
    input_sha256   text NOT NULL,
    disposition    text NOT NULL CHECK (disposition IN ('index', 'review', 'not_indexed',
                                                        'restricted', 'held')),
    reason_codes   text[] NOT NULL DEFAULT '{}',
    summary        text,
    action_text    text,
    action_for     text[] NOT NULL DEFAULT '{}',   -- lowercase addresses asked to act
    due_text       text,
    reused_from    bigint REFERENCES classification_runs(id),
    created_at     timestamptz NOT NULL DEFAULT now(),
    UNIQUE (version_id, model, policy_version)
);
CREATE INDEX classification_runs_input ON classification_runs (input_sha256, model, policy_version);

CREATE TABLE classification_labels (
    run_id      bigint NOT NULL REFERENCES classification_runs(id) ON DELETE CASCADE,
    label       text NOT NULL,
    probability real CHECK (probability IS NULL OR (probability >= 0 AND probability <= 1)),
    decision    text NOT NULL CHECK (decision IN ('no', 'yes', 'review', 'unresolved')),
    PRIMARY KEY (run_id, label)
);

-- published_chunks gains source_ref (appended, so existing columns keep their order).
CREATE OR REPLACE VIEW published_chunks AS
SELECT ch.id, ch.communication_id, ch.chunk_kind, ch.seq, ch.segment_from, ch.segment_to,
       ch.start_ms, ch.end_ms, ch.page_from, ch.page_to, ch.speakers, ch.display_text,
       ch.embedding_model, ch.embedding, ch.tsv,
       pc.corpus_id, pc.title, pc.occurred_at, pc.kind, pc.source_url,
       ch.source_ref
FROM chunks ch
JOIN published_communications pc ON pc.id = ch.communication_id
                                AND pc.active_version_id = ch.version_id;

-- Text of published versions only, for bounded excerpts.
CREATE VIEW published_texts AS
SELECT pc.id AS communication_id, pc.corpus_id, t.text_kind, t.ref, t.content, t.truncated
FROM published_communications pc
JOIN version_texts t ON t.version_id = pc.active_version_id;

-- Latest classification of each published communication.
CREATE VIEW published_classifications AS
SELECT DISTINCT ON (pc.id)
       pc.id AS communication_id, pc.corpus_id, r.disposition, r.summary, r.action_text,
       r.action_for, r.due_text, r.model, r.policy_version,
       ARRAY(SELECT l.label FROM classification_labels l
             WHERE l.run_id = r.id AND l.decision = 'yes' ORDER BY l.label) AS yes_labels,
       ARRAY(SELECT l.label FROM classification_labels l
             WHERE l.run_id = r.id AND l.decision IN ('review', 'unresolved')
             ORDER BY l.label) AS uncertain_labels
FROM published_communications pc
JOIN classification_runs r ON r.version_id = pc.active_version_id
ORDER BY pc.id, r.id DESC;

-- Attachment names and extraction outcome of published versions (for citations).
CREATE VIEW published_email_attachments AS
SELECT pc.id AS communication_id, pc.corpus_id, a.native_attachment_id, a.name,
       a.content_type, a.size_bytes, a.is_inline, a.extraction_status
FROM published_communications pc
JOIN email_attachments a ON a.version_id = pc.active_version_id;

-- Counts only (no ids, no content) for the coverage block in search results.
CREATE VIEW coverage_counts AS
SELECT v.processing_state, coalesce(v.hold_reason_code, '') AS reason, count(*) AS messages
FROM communications c
JOIN LATERAL (SELECT processing_state, hold_reason_code FROM ci.communication_versions v
              WHERE v.communication_id = c.id ORDER BY v.version_no DESC LIMIT 1) v ON true
WHERE c.dataset_key = ci.active_dataset()
GROUP BY v.processing_state, coalesce(v.hold_reason_code, '');

GRANT SELECT, INSERT, UPDATE, DELETE ON version_texts, classification_runs, classification_labels
    TO ci_ingest;
GRANT SELECT ON published_texts, published_classifications, published_email_attachments,
    coverage_counts TO ci_search;
GRANT SELECT ON published_chunks TO ci_search;
