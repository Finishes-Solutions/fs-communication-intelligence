-- Behavioural checks for db/migrations/0001_foundation.sql.
-- Run against a THROWAWAY cluster (PostgreSQL 16 + pgvector) as a superuser; it
-- creates cluster-level test roles t_ingest, t_search and t_operator:
--   psql -d ci_test -v ON_ERROR_STOP=1 -f db/migrations/0001_foundation.sql
--   psql -d ci_test -t -f db/tests/0001_foundation_checks.sql
-- Expected: every "(expect N)" line shows N, and every NOTICE starts with "OK:".
-- Uses synthetic text only. Random vectors stand in for embeddings.
SET search_path = ci, public;
CREATE ROLE t_ingest LOGIN IN ROLE ci_ingest;
CREATE ROLE t_search LOGIN IN ROLE ci_search;
CREATE ROLE t_operator LOGIN IN ROLE ci_operator;
-- mode: test, fireflies enabled
INSERT INTO runtime_config (mode, enabled_providers, is_active, activated_at, activated_by) VALUES ('test','{fireflies}',true,now(),'op');
INSERT INTO source_connections (id, provider, account_ref, display_name, credential_ref, enabled)
  VALUES ('00000000-0000-0000-0000-00000000000c','fireflies','team-1','Fireflies','kv:fireflies-api-key',true);
INSERT INTO corpora (id, corpus_key, description) VALUES ('00000000-0000-0000-0000-0000000000c0','fireflies-executive','pilot'),
                                                       ('00000000-0000-0000-0000-0000000000c1','fireflies-hr','restricted');
INSERT INTO source_scopes (id, connection_id, scope_kind, native_scope_id, display_label, corpus_id, test_approved, backfill_since, policy_version)
  VALUES ('00000000-0000-0000-0000-00000000005c','00000000-0000-0000-0000-00000000000c','fireflies_user','QY','Matthew','00000000-0000-0000-0000-0000000000c0',true,'2026-01-01','p1');
INSERT INTO access_grants (corpus_id, principal_type, principal_id, granted_by)
  VALUES ('00000000-0000-0000-0000-0000000000c0','entra_app_role','Communications.Reader.Executive','op');

-- ingest as t_ingest: job admission dedupe
SET ROLE t_ingest;
INSERT INTO jobs (dataset_key, config_revision, job_type, dedupe_key, payload)
  VALUES ('test',1,'ff_fetch','c:MEET1','{"native_id":"MEET1"}') ON CONFLICT DO NOTHING;
INSERT INTO jobs (dataset_key, config_revision, job_type, dedupe_key, payload)
  VALUES ('test',1,'ff_fetch','c:MEET1','{"native_id":"MEET1"}') ON CONFLICT DO NOTHING;
SELECT 'open jobs for MEET1 (expect 1): ' || count(*) FROM jobs WHERE dedupe_key='c:MEET1';
-- claim with SKIP LOCKED
WITH next AS (SELECT id FROM jobs WHERE state='queued' AND run_after<=now() ORDER BY priority, run_after
              FOR UPDATE SKIP LOCKED LIMIT 5)
UPDATE jobs j SET state='leased', lease_owner='w1', lease_expires_at=now()+interval '5 min', attempts=attempts+1, updated_at=now()
FROM next WHERE j.id=next.id RETURNING 'claimed job ' || j.id;

-- two meetings: M1 in executive corpus, M2 in HR corpus
INSERT INTO communications (id, dataset_key, connection_id, native_id, kind, corpus_id, title, occurred_at, duration_seconds)
VALUES ('00000000-0000-0000-0000-0000000000a1','test','00000000-0000-0000-0000-00000000000c','MEET1','meeting','00000000-0000-0000-0000-0000000000c0','Harbor Point change order review','2026-09-20 15:00Z',1800),
       ('00000000-0000-0000-0000-0000000000a2','test','00000000-0000-0000-0000-00000000000c','MEET2','meeting','00000000-0000-0000-0000-0000000000c1','HR compensation review','2026-09-21 15:00Z',1800);
INSERT INTO communication_versions (id, communication_id, version_no, source_revision, profile, processing_state)
VALUES ('00000000-0000-0000-0000-0000000000b1','00000000-0000-0000-0000-0000000000a1',1,'sha-r1','{"chunker":"tw-1","embed":"te3l-1536","policy":"p1"}','embedded'),
       ('00000000-0000-0000-0000-0000000000b2','00000000-0000-0000-0000-0000000000a2',1,'sha-r2','{"chunker":"tw-1","embed":"te3l-1536","policy":"p1"}','embedded');
INSERT INTO transcript_segments VALUES
 ('00000000-0000-0000-0000-0000000000b1',0,'Matthew',NULL,0,5000,'Let us review the Harbor Point change order for the lobby tile.'),
 ('00000000-0000-0000-0000-0000000000b1',1,'Vendor',NULL,5000,9000,'The revised amount is forty two thousand dollars.'),
 ('00000000-0000-0000-0000-0000000000b2',0,'HR',NULL,0,5000,'Compensation bands for the tile installers.');
CREATE TEMP TABLE qv AS SELECT (SELECT array_agg(random())::vector(1536) FROM generate_series(1,1536)) v;
GRANT SELECT ON qv TO t_search;
INSERT INTO chunks (version_id, communication_id, chunk_kind, seq, segment_from, segment_to, start_ms, end_ms, speakers, display_text, embedding_input_sha256, embedding_model, embedding)
SELECT '00000000-0000-0000-0000-0000000000b1'::uuid,'00000000-0000-0000-0000-0000000000a1'::uuid,'transcript_window',0,0,1,0,9000,'{Matthew,Vendor}'::text[],
 'Matthew: Let us review the Harbor Point change order for the lobby tile. Vendor: The revised amount is forty two thousand dollars.','h1','te3l-1536', v FROM qv
UNION ALL
SELECT '00000000-0000-0000-0000-0000000000b2'::uuid,'00000000-0000-0000-0000-0000000000a2'::uuid,'transcript_window',0,0,0,0,5000,'{HR}'::text[],
 'HR: Compensation bands for the tile installers.','h2','te3l-1536', v FROM qv;
RESET ROLE;

-- search role before publication: expect 0
SET ROLE t_search;
SELECT 'visible before publish (expect 0): ' || count(*) FROM published_chunks;
RESET ROLE;

-- publish both in one transaction (the fence)
SET ROLE t_ingest;
BEGIN;
UPDATE communication_versions SET processing_state='published', published_at=now() WHERE id IN ('00000000-0000-0000-0000-0000000000b1','00000000-0000-0000-0000-0000000000b2');
UPDATE communications SET active_version_id = CASE id WHEN '00000000-0000-0000-0000-0000000000a1' THEN '00000000-0000-0000-0000-0000000000b1'::uuid ELSE '00000000-0000-0000-0000-0000000000b2'::uuid END;
COMMIT;
RESET ROLE;

SET ROLE t_search;
SELECT 'visible after publish, unfiltered (expect 2): ' || count(*) FROM published_chunks;
-- the documented hybrid query, authorized by roles claim
PREPARE hybrid(text, vector, text[], text, timestamptz, timestamptz, int) AS
WITH allowed AS (
  SELECT DISTINCT g.corpus_id FROM ci.access_grants g
  WHERE g.revoked_at IS NULL AND g.valid_from <= now() AND (g.valid_to IS NULL OR g.valid_to > now())
    AND ((g.principal_type='entra_app_role' AND g.principal_id = ANY($3))
      OR (g.principal_type='entra_user' AND g.principal_id = $4))
), cand AS (
  SELECT * FROM ci.published_chunks pc
  WHERE pc.corpus_id IN (SELECT corpus_id FROM allowed)
    AND pc.occurred_at >= $5 AND pc.occurred_at < $6
), vec AS (
  SELECT id, row_number() OVER (ORDER BY embedding <=> $2) r FROM cand ORDER BY embedding <=> $2 LIMIT 50
), kw AS (
  SELECT id, row_number() OVER (ORDER BY ts_rank_cd(tsv, q) DESC) r
  FROM cand, websearch_to_tsquery('english', $1) q WHERE tsv @@ q
  ORDER BY ts_rank_cd(tsv, q) DESC LIMIT 50
), fused AS (
  SELECT id, sum(1.0/(60+r)) score FROM (SELECT * FROM vec UNION ALL SELECT * FROM kw) u GROUP BY id
)
SELECT c.communication_id, c.title, c.start_ms, c.end_ms, round(f.score,4) score
FROM fused f JOIN cand c USING (id) ORDER BY f.score DESC LIMIT $7;
SELECT v::text AS qvec FROM qv \gset
SELECT 'exec search:' ; EXECUTE hybrid('tile change order', :'qvec'::vector, '{Communications.Reader.Executive}', 'user-x', '2026-01-01', '2027-01-01', 10);
SELECT 'no-role search (expect none):'; EXECUTE hybrid('tile', :'qvec'::vector, '{}', 'user-x', '2026-01-01', '2027-01-01', 10);
-- direct base table access must fail
DO $$ BEGIN PERFORM 1 FROM ci.chunks LIMIT 1; RAISE EXCEPTION 'search role read chunks!'; EXCEPTION WHEN insufficient_privilege THEN RAISE NOTICE 'OK: search role denied on ci.chunks'; END $$;
DO $$ BEGIN PERFORM 1 FROM ci.transcript_segments LIMIT 1; RAISE EXCEPTION 'search role read segments!'; EXCEPTION WHEN insufficient_privilege THEN RAISE NOTICE 'OK: search role denied on ci.transcript_segments'; END $$;
INSERT INTO audit_events (actor_type, actor_id, action) VALUES ('user','user-x','search');
SELECT 'audit insert ok';
RESET ROLE;

-- ingest role must not change mode or grants
SET ROLE t_ingest;
DO $$ BEGIN UPDATE ci.runtime_config SET mode='production'; RAISE EXCEPTION 'ingest changed mode!'; EXCEPTION WHEN insufficient_privilege THEN RAISE NOTICE 'OK: ingest denied mode change'; END $$;
DO $$ BEGIN INSERT INTO ci.access_grants (corpus_id, principal_type, principal_id, granted_by) VALUES ('00000000-0000-0000-0000-0000000000c1','entra_user','x','x'); RAISE EXCEPTION 'ingest granted!'; EXCEPTION WHEN insufficient_privilege THEN RAISE NOTICE 'OK: ingest denied grant'; END $$;
-- deletion at source: tombstone hides immediately
INSERT INTO tombstones (dataset_key, connection_id, native_id, reason_code) VALUES ('test','00000000-0000-0000-0000-00000000000c','MEET1','deleted_at_source');
RESET ROLE;
SET ROLE t_search;
SELECT 'MEET1 visible after tombstone (expect 0): ' || count(*) FROM published_communications WHERE native_id='MEET1';
RESET ROLE;

-- operator cannot read evidence
SET ROLE t_operator;
DO $$ BEGIN PERFORM 1 FROM ci.chunks LIMIT 1; RAISE EXCEPTION 'operator read chunks!'; EXCEPTION WHEN insufficient_privilege THEN RAISE NOTICE 'OK: operator denied on ci.chunks'; END $$;
-- paused mode hides everything (operator performs the switch)
BEGIN;
UPDATE ci.runtime_config SET is_active=false;
INSERT INTO ci.runtime_config (mode, is_active, activated_at, activated_by) VALUES ('paused', true, now(), 'op');
COMMIT;
RESET ROLE;
SET ROLE t_search;
SELECT 'visible while paused (expect 0): ' || count(*) FROM published_chunks;
RESET ROLE;
-- second active revision must be rejected
DO $$ BEGIN INSERT INTO ci.runtime_config (mode, is_active, activated_at, activated_by) VALUES ('production', true, now(), 'op'); RAISE EXCEPTION 'two active!'; EXCEPTION WHEN unique_violation THEN RAISE NOTICE 'OK: only one active revision'; END $$;
