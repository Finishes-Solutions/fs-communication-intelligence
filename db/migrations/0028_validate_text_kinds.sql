-- 0028_validate_text_kinds.sql
-- Validates the text-kind check 0027 added NOT VALID. VALIDATE CONSTRAINT takes only
-- a SHARE UPDATE EXCLUSIVE lock, so captures and processing keep writing while the
-- existing rows are checked. Its own transaction, so 0027's exclusive locks are
-- released before the scan starts.

SET search_path = ci, public;

ALTER TABLE version_texts VALIDATE CONSTRAINT version_texts_text_kind_check;
