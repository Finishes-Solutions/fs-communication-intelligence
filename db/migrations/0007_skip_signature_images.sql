-- 0007_skip_signature_images.sql
-- Signature images and logos are not processed (Matthew, 2026-10-28): every
-- inline image, and image attachments of 100 KB or less, get extraction_status
-- 'skipped'. They are never downloaded, extracted or kept as files. Existing
-- rows that match are relabelled so counts stop showing them as "unsupported".

SET search_path = ci, public;

ALTER TABLE email_attachments DROP CONSTRAINT email_attachments_extraction_status_check;
ALTER TABLE email_attachments ADD CONSTRAINT email_attachments_extraction_status_check
    CHECK (extraction_status IN ('not_attempted', 'extracted', 'no_text', 'unsupported',
                                 'too_large', 'failed', 'timeout', 'skipped'));

UPDATE email_attachments
SET extraction_status = 'skipped', original_stored_at = NULL
WHERE extraction_status IN ('not_attempted', 'unsupported')
  AND (lower(coalesce(content_type, '')) LIKE 'image/%'
       OR lower(coalesce(name, '')) ~ '\.(png|jpe?g|gif|bmp|tiff?|webp|emz|wmz)$')
  AND (is_inline OR size_bytes <= 100 * 1024);

DROP VIEW ops_original_counts;
CREATE VIEW ops_original_counts AS
SELECT CASE WHEN extraction_status = 'skipped' THEN 'signature image (skipped)'
            WHEN original_stored_at IS NOT NULL THEN 'stored'
            ELSE 'not stored' END AS original, count(*) AS attachments
FROM email_attachments GROUP BY 1;
GRANT SELECT ON ops_original_counts TO ci_operator;
