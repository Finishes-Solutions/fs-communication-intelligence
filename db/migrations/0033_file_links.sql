-- 0033_file_links.sql
-- Short-lived download links for agents (Matthew, 2026-11-18: agents should get
-- files and emails "without needing to use my computer or browser").
--
-- The search app signs a link (ci_retrieval.file_links) and the admin console,
-- which can read the stored originals, checks it at /dl/<link>. Both reach the
-- key through file_link_key(), so no new secret has to be shared between the two
-- apps. The key is made here from two random UUIDs (244 random bits).
--
-- Reading the key is granted to ci_search. A holder of ci_search can already read
-- every published email and file through the views, so it adds no reach; links
-- are checked again at download against the reader they were made for.
-- rotate_file_link_key() (operators) replaces the key, which ends every link made
-- with the old one at once.

SET search_path = ci, public;

CREATE TABLE IF NOT EXISTS file_link_keys (
    id          integer PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    key         bytea NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);

INSERT INTO file_link_keys (key)
SELECT decode(replace(gen_random_uuid()::text, '-', '')
              || replace(gen_random_uuid()::text, '-', ''), 'hex')
WHERE NOT EXISTS (SELECT 1 FROM file_link_keys);

REVOKE ALL ON file_link_keys FROM PUBLIC;

CREATE OR REPLACE FUNCTION file_link_key() RETURNS bytea
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = ci, public
AS $$ SELECT key FROM file_link_keys WHERE id = 1 $$;

CREATE OR REPLACE FUNCTION rotate_file_link_key() RETURNS void
LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path = ci, public
AS $$
    UPDATE file_link_keys
    SET key = decode(replace(gen_random_uuid()::text, '-', '')
                     || replace(gen_random_uuid()::text, '-', ''), 'hex'),
        created_at = now()
    WHERE id = 1
$$;

REVOKE ALL ON FUNCTION file_link_key(), rotate_file_link_key() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION file_link_key() TO ci_search;
GRANT EXECUTE ON FUNCTION rotate_file_link_key() TO ci_operator;
