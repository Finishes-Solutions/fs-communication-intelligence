-- Core behavior checks for 0001_foundation.sql: queue, publication fence,
-- authorization before ranking, role boundaries, tombstones and mode.
-- Run by db/run_checks.py against a freshly migrated throwaway database.
-- Every "(expect N): M" line must show M = N; every NOTICE must start "OK:".
\ir seed/email_seed.psql
SET search_path = ci, public;

-- queue: duplicate admission collapses; SKIP LOCKED claim
SET ROLE t_ingest;
INSERT INTO jobs (dataset_key, config_revision, job_type, dedupe_key, payload)
  VALUES ('test', 1, 'email_fetch', 'e1:oid-a:imm-a-9', '{"native_id":"imm-a-9"}') ON CONFLICT DO NOTHING;
INSERT INTO jobs (dataset_key, config_revision, job_type, dedupe_key, payload)
  VALUES ('test', 1, 'email_fetch', 'e1:oid-a:imm-a-9', '{"native_id":"imm-a-9"}') ON CONFLICT DO NOTHING;
SELECT 'open jobs after duplicate admission (expect 1): ' || count(*) FROM jobs WHERE dedupe_key = 'e1:oid-a:imm-a-9';
WITH next AS (SELECT id FROM jobs WHERE state = 'queued' AND run_after <= now()
              ORDER BY priority, run_after FOR UPDATE SKIP LOCKED LIMIT 5)
UPDATE jobs j SET state = 'leased', lease_owner = 'w1', lease_expires_at = now() + interval '5 min',
                  attempts = attempts + 1, updated_at = now()
FROM next WHERE j.id = next.id;
SELECT 'leased jobs (expect 1): ' || count(*) FROM jobs WHERE state = 'leased';
RESET ROLE;

-- publication fence: nothing visible before publish
SET ROLE t_search;
SELECT 'visible before publish (expect 0): ' || count(*) FROM published_chunks;
RESET ROLE;
\ir seed/publish.psql

-- after publish: M1, M2, M3, M5 (M4's mailbox is not approved)
SET ROLE t_search;
SELECT 'visible after publish, before grants (expect 4): ' || count(*) FROM published_chunks;
\ir seed/search_fn.psql
SELECT 'search with Reader.All role (expect 3): ' || count(*)
  FROM pg_temp.search('invoice', (SELECT v FROM qv), '{Communications.Reader.All}', 'user-x', 10);
SELECT 'search with no role (expect 0): ' || count(*)
  FROM pg_temp.search('invoice', (SELECT v FROM qv), '{}', 'user-x', 10);
SELECT 'search with an unknown role (expect 0): ' || count(*)
  FROM pg_temp.search('invoice', (SELECT v FROM qv), '{Communications.Reader.Everything}', 'user-x', 10);
DO $$ BEGIN PERFORM 1 FROM ci.chunks LIMIT 1; RAISE EXCEPTION 'search role read chunks!';
  EXCEPTION WHEN insufficient_privilege THEN RAISE NOTICE 'OK: search role denied on ci.chunks'; END $$;
DO $$ BEGIN PERFORM 1 FROM ci.email_messages LIMIT 1; RAISE EXCEPTION 'search role read email_messages!';
  EXCEPTION WHEN insufficient_privilege THEN RAISE NOTICE 'OK: search role denied on ci.email_messages'; END $$;
DO $$ BEGIN INSERT INTO ci.access_grants (corpus_id, principal_type, principal_id, granted_by)
  VALUES ('00000000-0000-0000-0000-0000000000c1', 'entra_user', 'user-x', 'x'); RAISE EXCEPTION 'search role granted!';
  EXCEPTION WHEN insufficient_privilege THEN RAISE NOTICE 'OK: search role denied grant'; END $$;
INSERT INTO audit_events (actor_type, actor_id, action) VALUES ('user', 'user-x', 'search');
RESET ROLE;

-- revoked grant: zero results
UPDATE access_grants SET revoked_at = now() WHERE principal_id = 'Communications.Reader.All';
SET ROLE t_search;
SELECT 'search after grant revoked (expect 0): ' || count(*)
  FROM pg_temp.search('invoice', (SELECT v FROM qv), '{Communications.Reader.All}', 'user-x', 10);
RESET ROLE;
UPDATE access_grants SET revoked_at = NULL WHERE principal_id = 'Communications.Reader.All';

-- ingestion cannot change mode, grants, exclusions or approvals, but can record scope state
SET ROLE t_ingest;
DO $$ BEGIN UPDATE ci.runtime_config SET mode = 'production'; RAISE EXCEPTION 'ingest changed mode!';
  EXCEPTION WHEN insufficient_privilege THEN RAISE NOTICE 'OK: ingest denied mode change'; END $$;
DO $$ BEGIN INSERT INTO ci.access_grants (corpus_id, principal_type, principal_id, granted_by)
  VALUES ('00000000-0000-0000-0000-0000000000c1', 'entra_user', 'x', 'x'); RAISE EXCEPTION 'ingest granted!';
  EXCEPTION WHEN insufficient_privilege THEN RAISE NOTICE 'OK: ingest denied grant'; END $$;
DO $$ BEGIN INSERT INTO ci.exclusions (exclusion_kind, pattern, reason_code, created_by)
  VALUES ('participant_domain', 'x.example', 'r', 'x'); RAISE EXCEPTION 'ingest excluded!';
  EXCEPTION WHEN insufficient_privilege THEN RAISE NOTICE 'OK: ingest denied exclusion'; END $$;
DO $$ BEGIN UPDATE ci.source_scopes SET test_approved = true WHERE native_scope_id = 'oid-u';
  RAISE EXCEPTION 'ingest approved a scope!';
  EXCEPTION WHEN insufficient_privilege THEN RAISE NOTICE 'OK: ingest denied scope approval'; END $$;
UPDATE source_scopes SET state_checked_at = now() WHERE native_scope_id = 'oid-a';
SELECT 'ingest recorded scope state (expect 1): ' || count(*) FROM source_scopes WHERE state_checked_at IS NOT NULL;
-- deletion at source: tombstone hides immediately
INSERT INTO tombstones (dataset_key, connection_id, native_id, reason_code)
  VALUES ('test', '00000000-0000-0000-0000-0000000000e1', 'imm-a-2', 'deleted_at_source');
RESET ROLE;
SET ROLE t_search;
SELECT 'M3 visible after tombstone (expect 0): ' || count(*) FROM published_communications WHERE native_id = 'imm-a-2';
RESET ROLE;

-- operator cannot read evidence; paused mode hides everything
SET ROLE t_operator;
DO $$ BEGIN PERFORM 1 FROM ci.chunks LIMIT 1; RAISE EXCEPTION 'operator read chunks!';
  EXCEPTION WHEN insufficient_privilege THEN RAISE NOTICE 'OK: operator denied on ci.chunks'; END $$;
BEGIN;
UPDATE ci.runtime_config SET is_active = false;
INSERT INTO ci.runtime_config (mode, is_active, activated_at, activated_by) VALUES ('paused', true, now(), 'op');
COMMIT;
RESET ROLE;
SET ROLE t_search;
SELECT 'visible while paused (expect 0): ' || count(*) FROM published_chunks;
RESET ROLE;
DO $$ BEGIN INSERT INTO ci.runtime_config (mode, is_active, activated_at, activated_by)
  VALUES ('production', true, now(), 'op'); RAISE EXCEPTION 'two active!';
  EXCEPTION WHEN unique_violation THEN RAISE NOTICE 'OK: only one active revision'; END $$;
