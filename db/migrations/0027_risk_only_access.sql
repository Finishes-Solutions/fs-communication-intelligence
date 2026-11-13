-- 0027_risk_only_access.sql
-- Risk-only access (Matthew, 2026-11-13): an agent may surface project-risk
-- emails without returning email links, and how much of each risk email it
-- shows is chosen per reader.
--
-- Readers with the app role Communications.Reader.Risks (and no broader reading
-- role) see only published project-risk emails, from every enrolled mailbox,
-- through the risk tools only (ci_retrieval.risk_access). This table says which
-- fields of each risk email they are shown. No row, or an empty list, means
-- counts and charts only. Email links (view_url, download_url, web_link) and
-- email text are never shown to them, whatever is listed here.
--
-- Fields:
--   date           when the email was received
--   property       the property/project names the AI found in it
--   category       its cat_ labels (development, payment, legal ...)
--   summary        the AI's one-line risk summary
--   rating         the AI's impact and likelihood rating, with its reason
--   sender_domain  the sender's company domain (also the per-company counts)
--   subject        the email subject
--   sender         the sender's address
--   documents      attachment names and types (no files, no text, no links)
--
-- Keyed by sign-in address, like reader_restrictions (0024).

SET search_path = ci, public;

CREATE TABLE reader_risk_fields (
    reader_address text PRIMARY KEY CHECK (reader_address = lower(reader_address)
                                           AND reader_address LIKE '%_@_%'),
    fields         text[] NOT NULL DEFAULT '{}' CHECK (fields <@ ARRAY[
                       'date', 'property', 'category', 'summary', 'rating',
                       'sender_domain', 'subject', 'sender', 'documents']::text[]),
    updated_by     text NOT NULL,
    updated_at     timestamptz NOT NULL DEFAULT now()
);

-- The fields one reader is shown (NULL when none were chosen). SECURITY DEFINER
-- so ci_search reads only the caller's own row, never everyone's settings.
CREATE FUNCTION risk_fields(reader text) RETURNS text[]
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = ci, public
AS $$
    SELECT fields FROM reader_risk_fields WHERE reader_address = lower(coalesce(reader, ''))
$$;

REVOKE ALL ON FUNCTION risk_fields(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION risk_fields(text) TO ci_search, ci_operator;
GRANT SELECT, INSERT, UPDATE, DELETE ON reader_risk_fields TO ci_operator;
