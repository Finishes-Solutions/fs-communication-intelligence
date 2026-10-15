-- Classification proposals: integrity rules and what the search role can see.
\ir seed/email_seed.psql
\ir seed/publish.psql
SET search_path = ci, public;

SET ROLE t_ingest;
INSERT INTO classification_runs (id, version_id, classifier_backend, model, policy_version,
                                 input_sha256, outcome, outcome_reasons)
SELECT '00000000-0000-0000-0000-0000000000f1', active_version_id, 'fake', 'm', 'p1', 'h1',
       'review', '{phishing_yes}'
FROM communications WHERE id = '00000000-0000-0000-0000-0000000000a1';
INSERT INTO classification_labels (run_id, label, probability, no_threshold, yes_threshold, decision)
VALUES ('00000000-0000-0000-0000-0000000000f1', 'phishing', 0.96, 0.10, 0.80, 'yes'),
       ('00000000-0000-0000-0000-0000000000f1', 'spam', NULL, 0.15, 0.95, 'unresolved');

DO $$ BEGIN
  BEGIN
    INSERT INTO ci.classification_labels VALUES
      ('00000000-0000-0000-0000-0000000000f1', 'pii', NULL, 0.2, 0.8, 'no');
    RAISE NOTICE 'FAIL: a missing probability was accepted as "no"';
  EXCEPTION WHEN check_violation THEN RAISE NOTICE 'OK: a missing answer must be unresolved';
  END;
  BEGIN
    INSERT INTO ci.classification_runs (version_id, classifier_backend, model, policy_version,
                                        input_sha256, outcome)
    SELECT active_version_id, 'fake', 'm', 'p1', 'h2', 'approved'
    FROM ci.communications WHERE id = '00000000-0000-0000-0000-0000000000a1';
    RAISE NOTICE 'FAIL: an approval outcome was accepted';
  EXCEPTION WHEN check_violation THEN RAISE NOTICE 'OK: classification is a proposal, never an approval';
  END;
END $$;
RESET ROLE;

SET ROLE t_search;
SELECT 'review flags visible to search (expect 1): ' || count(*) FROM published_review_flags;
DO $$ BEGIN
  PERFORM 1 FROM ci.classification_runs;
  RAISE NOTICE 'FAIL: search read classification_runs';
EXCEPTION WHEN insufficient_privilege THEN RAISE NOTICE 'OK: search cannot read classification tables';
END $$;
RESET ROLE;

-- Pausing hides review flags along with everything else.
UPDATE runtime_config SET is_active = false;
INSERT INTO runtime_config (mode, enabled_providers, is_active, activated_at, activated_by)
  VALUES ('paused', '{email}', true, now(), 'op');
SET ROLE t_search;
SELECT 'review flags while paused (expect 0): ' || count(*) FROM published_review_flags;
SELECT 'coverage rows while paused (expect 0): ' || count(*) FROM coverage_summary;
RESET ROLE;
