-- 0024_reader_restrictions.sql
-- Per-reader limits (Matthew, 2026-11-02: Will may see everything except PNS
-- Capital and Chuck's personal or private business mail, except emails he is on).
--
-- A rule hides matching mail from ONE reader, in every mailbox and every tool
-- (search, lists, stats, risks, knowledge, the email and file links), because
-- ci_retrieval.search adds ci.reader_hidden() to its one authorization check.
-- It only narrows what that reader's app role already allows; it never grants.
--
--   mailbox  value = a mailbox address: every copy stored in that mailbox
--   person   value = an address: every email with that person as sender or
--            recipient (to, cc, bcc), in any mailbox
--   domain   value = a domain: mailboxes and participants in that domain or
--            its subdomains
--   term     value = words: the subject or an AI-found entity name contains
--            them (case-insensitive), or the body or attachment text has them
--            as a phrase (the English full-text index of the stored chunks, so
--            "pns capital" also matches "PNS Capital VII, LLC" and "PNS-Capital")
--
-- Exception, for every rule: an email the reader is on (sender, to, cc or bcc)
-- or that sits in the reader's own mailbox is never hidden; they already have it.
-- Matching errs toward hiding: term rules read every stored version, not only
-- the published one.
-- Rules are keyed by the reader's sign-in address (the token's
-- preferred_username, lowercase); ci_retrieval refuses all results to a caller
-- without one while any rule exists, so a missing claim cannot skip the rules.

SET search_path = ci, public;

CREATE TABLE reader_restrictions (
    id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    reader_address text NOT NULL CHECK (reader_address = lower(reader_address)
                                        AND reader_address LIKE '%_@_%'),
    kind           text NOT NULL CHECK (kind IN ('mailbox', 'person', 'domain', 'term')),
    value          text NOT NULL CHECK (value = lower(value) AND length(value) BETWEEN 2 AND 200),
    reason_code    text NOT NULL CHECK (reason_code ~ '^[a-z0-9_]{3,64}$'),
    created_by     text NOT NULL,
    created_at     timestamptz NOT NULL DEFAULT now(),
    revoked_at     timestamptz,
    revoked_by     text
);
CREATE UNIQUE INDEX reader_restrictions_active
    ON reader_restrictions (reader_address, kind, value) WHERE revoked_at IS NULL;

-- True when this message must be hidden from this reader. SECURITY DEFINER so
-- ci_search can run it without reading the base tables; it returns only a boolean.
CREATE FUNCTION reader_hidden(cid uuid, reader text) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = ci, public
AS $$
    WITH r AS (
        SELECT kind, value FROM reader_restrictions
        WHERE reader_address = lower(reader) AND revoked_at IS NULL
    ), m AS (
        SELECT lower(mb.primary_smtp_address) AS address
        FROM email_messages em JOIN email_mailboxes mb ON mb.scope_id = em.mailbox_scope_id
        WHERE em.communication_id = cid
    ), p AS (
        SELECT lower(pt.email_normalized) AS address
        FROM communication_participants cp JOIN participants pt ON pt.id = cp.participant_id
        WHERE cp.communication_id = cid AND pt.email_normalized IS NOT NULL
    ), a AS (
        SELECT address FROM m UNION SELECT address FROM p
    )
    SELECT EXISTS (SELECT 1 FROM r)
       AND NOT EXISTS (SELECT 1 FROM a WHERE address = lower(reader))
       AND (
            EXISTS (SELECT 1 FROM r JOIN m ON r.kind = 'mailbox' AND m.address = r.value)
         OR EXISTS (SELECT 1 FROM r JOIN p ON r.kind = 'person' AND p.address = r.value)
         OR EXISTS (SELECT 1 FROM r JOIN a ON r.kind = 'domain'
                      AND (split_part(a.address, '@', 2) = r.value
                           OR split_part(a.address, '@', 2) LIKE '%.' || r.value))
         OR EXISTS (SELECT 1 FROM r JOIN communications c ON r.kind = 'term' AND c.id = cid
                      AND strpos(lower(coalesce(c.title, '')), r.value) > 0)
         OR EXISTS (SELECT 1 FROM r JOIN communication_versions v ON r.kind = 'term'
                      AND v.communication_id = cid
                    JOIN chunks ch ON ch.version_id = v.id
                    WHERE ch.tsv @@ phraseto_tsquery('english', r.value))
         OR EXISTS (SELECT 1 FROM r JOIN communication_versions v ON r.kind = 'term'
                      AND v.communication_id = cid
                    JOIN classification_runs cr ON cr.version_id = v.id
                    CROSS JOIN LATERAL unnest(cr.entities) AS e(name)
                    WHERE strpos(lower(e.name), r.value) > 0)
       )
$$;

-- Does this reader have any rule (and does anyone)? Lets the search code skip
-- the per-message check for everyone else, and refuse a caller with no address.
CREATE FUNCTION reader_restricted(reader text) RETURNS TABLE (mine boolean, anyone boolean)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = ci, public
AS $$
    SELECT EXISTS (SELECT 1 FROM reader_restrictions
                   WHERE reader_address = lower(coalesce(reader, '')) AND revoked_at IS NULL),
           EXISTS (SELECT 1 FROM reader_restrictions WHERE revoked_at IS NULL)
$$;

REVOKE ALL ON FUNCTION reader_hidden(uuid, text), reader_restricted(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION reader_hidden(uuid, text), reader_restricted(text) TO ci_search, ci_operator;
GRANT SELECT, INSERT, UPDATE ON reader_restrictions TO ci_operator;
