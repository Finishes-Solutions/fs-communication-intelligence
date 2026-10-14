-- Email checks for 0002_email.sql and the scope/exclusion rules in 0001:
-- mailbox exclusions, participant address and domain exclusions in every
-- mailbox, scope state, citation/coverage views and constraints.
\ir seed/email_seed.psql
\ir seed/publish.psql
SET search_path = ci, public;

SET ROLE t_search;
SELECT 'published email messages (expect 4): ' || count(*) FROM published_email_messages;
SELECT 'copies of the invoice across mailboxes (expect 2): ' || count(*)
  FROM published_email_messages WHERE internet_message_id = '<inv-4471@acme-sitework.example>';
SELECT 'distinct messages after collapsing copies (expect 3): ' || count(DISTINCT internet_message_id)
  FROM published_email_messages;
SELECT 'unapproved mailbox visible (expect 0): ' || count(*)
  FROM published_email_messages WHERE mailbox_address = 'shared.example@finishes.example';
SELECT 'M1 partial attachments (expect 1): ' || coalesce(sum(attachment_count), 0)
  FROM published_email_attachment_coverage
  WHERE communication_id = '00000000-0000-0000-0000-0000000000a1' AND status = 'unsupported_partial';
RESET ROLE;

-- excluding mailbox B hides only B's copy; Jordan's mail in mailbox A stays
SET ROLE t_operator;
INSERT INTO exclusions (exclusion_kind, scope_kind, native_scope_id, reason_code, created_by)
  VALUES ('scope', 'mailbox', 'oid-b', 'keep_out', 'op');
RESET ROLE;
SET ROLE t_search;
SELECT 'mailbox B messages after scope exclusion (expect 0): ' || count(*)
  FROM published_email_messages WHERE mailbox_address = 'sam.example@finishes.example';
SELECT 'invoice copies after mailbox B exclusion (expect 1): ' || count(*)
  FROM published_email_messages WHERE internet_message_id = '<inv-4471@acme-sitework.example>';
RESET ROLE;
SET ROLE t_operator;
UPDATE exclusions SET revoked_at = now(), revoked_by = 'op' WHERE native_scope_id = 'oid-b';
RESET ROLE;
SET ROLE t_search;
SELECT 'mailbox B messages after exclusion revoked (expect 1): ' || count(*)
  FROM published_email_messages WHERE mailbox_address = 'sam.example@finishes.example';
RESET ROLE;

-- participant address exclusion hides that person's mail in every mailbox
SET ROLE t_operator;
INSERT INTO exclusions (exclusion_kind, pattern, reason_code, created_by)
  VALUES ('participant_address', 'jordan.rivera@acme-sitework.example', 'keep_out', 'op');
RESET ROLE;
SET ROLE t_search;
SELECT 'messages involving excluded address, any mailbox (expect 0): ' || count(*)
  FROM published_email_messages WHERE internet_message_id = '<inv-4471@acme-sitework.example>';
SELECT 'other messages still visible (expect 1): ' || count(*) FROM published_email_messages;
RESET ROLE;
SET ROLE t_operator;
UPDATE exclusions SET revoked_at = now(), revoked_by = 'op' WHERE exclusion_kind = 'participant_address';
-- domain exclusion
INSERT INTO exclusions (exclusion_kind, pattern, reason_code, created_by)
  VALUES ('participant_domain', 'payroll-vendor.example', 'keep_out', 'op');
RESET ROLE;
SET ROLE t_search;
SELECT 'messages from excluded domain (expect 0): ' || count(*)
  FROM published_email_messages WHERE internet_message_id = '<pay-cal@payroll-vendor.example>';
SELECT 'visible after domain exclusion (expect 3): ' || count(*) FROM published_email_messages;
RESET ROLE;

-- inventory reports Graph 403 for mailbox A: its messages leave search
SET ROLE t_ingest;
UPDATE source_scopes SET scope_state = 'no_exchange_access', state_reason_code = 'graph_403',
                         state_checked_at = now() WHERE native_scope_id = 'oid-a';
RESET ROLE;
SET ROLE t_search;
SELECT 'mailbox A messages after 403 (expect 0): ' || count(*)
  FROM published_email_messages WHERE mailbox_address = 'pat.example@finishes.example';
RESET ROLE;

-- constraints
DO $$ BEGIN INSERT INTO ci.exclusions (exclusion_kind, pattern, reason_code, created_by)
  VALUES ('participant_domain', '@bad.example', 'r', 'op'); RAISE EXCEPTION 'accepted @domain pattern!';
  EXCEPTION WHEN check_violation THEN RAISE NOTICE 'OK: domain pattern stored without @'; END $$;
DO $$ BEGIN INSERT INTO ci.exclusions (exclusion_kind, pattern, reason_code, created_by)
  VALUES ('participant_address', 'Mixed@Case.example', 'r', 'op'); RAISE EXCEPTION 'accepted mixed case!';
  EXCEPTION WHEN check_violation THEN RAISE NOTICE 'OK: exclusion patterns are lowercase'; END $$;
DO $$ BEGIN INSERT INTO ci.exclusions (exclusion_kind, native_scope_id, reason_code, created_by)
  VALUES ('item', 'oid-a', 'r', 'op'); RAISE EXCEPTION 'accepted malformed item exclusion!';
  EXCEPTION WHEN check_violation THEN RAISE NOTICE 'OK: exclusion fields must match kind'; END $$;
DO $$ BEGIN INSERT INTO ci.source_scopes (connection_id, scope_kind, native_scope_id, display_label, corpus_id, policy_version)
  VALUES ('00000000-0000-0000-0000-0000000000e1', 'inbox', 'x', 'x', '00000000-0000-0000-0000-0000000000c0', 'p1');
  RAISE EXCEPTION 'accepted unknown scope kind!';
  EXCEPTION WHEN check_violation THEN RAISE NOTICE 'OK: unknown scope kind rejected'; END $$;
DO $$ BEGIN INSERT INTO ci.email_mailboxes (scope_id, primary_smtp_address, recipient_type)
  VALUES ('00000000-0000-0000-0000-00000000005c', 'UPPER@finishes.example', 'UserMailbox');
  RAISE EXCEPTION 'accepted uppercase mailbox address!';
  EXCEPTION WHEN check_violation THEN RAISE NOTICE 'OK: mailbox address must be lowercase'; END $$;
