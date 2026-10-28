-- 0006_attachment_originals.sql
-- Original attachment files kept in Blob Storage (container "attachments" of
-- the evidence storage account), one blob per distinct file, named by SHA-256.
-- Approved by Matthew on 2026-10-28 WITHOUT malware scanning (an exception to
-- L11, like attachment extraction); Defender for Storage scanning is still
-- required before production. Files are served only through the admin
-- console after the same authorization as the message they belong to, so
-- attachments of restricted, held or excluded mail are never served.

SET search_path = ci, public;

ALTER TABLE email_attachments ADD COLUMN original_stored_at timestamptz;

CREATE OR REPLACE VIEW published_email_attachments AS
SELECT pc.id AS communication_id, pc.corpus_id, a.native_attachment_id, a.name,
       a.content_type, a.size_bytes, a.is_inline, a.extraction_status,
       a.sha256, a.original_stored_at IS NOT NULL AS original_stored
FROM published_communications pc
JOIN email_attachments a ON a.version_id = pc.active_version_id;

CREATE OR REPLACE VIEW ops_original_counts AS
SELECT (original_stored_at IS NOT NULL) AS stored, is_inline, count(*) AS attachments
FROM email_attachments GROUP BY 1, 2;
GRANT SELECT ON ops_original_counts TO ci_operator;
