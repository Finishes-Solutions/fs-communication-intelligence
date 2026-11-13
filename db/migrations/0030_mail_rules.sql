-- 0030_mail_rules.sql
-- Mail rules (Matthew, 2026-11-13): recurring emails that read like risks or
-- requests but are not useful as such (e.g. automated "ESCALATION: Unresolved
-- Governance Violations" notices sent from a person's own address) can be kept
-- out of risks and action items while staying searchable, or not processed at all.
--
-- A rule matches an email whose subject contains `subject_contains`
-- (case-insensitive), optionally only from `sender_address`. Replies and
-- forwards (RE:, FW:, FWD:) never match: a person answering such an email is
-- kept. Effects:
--
--   quiet  The email is processed and searchable as usual, but its labels
--          project_risk, action_required and approval_required are set to "no"
--          and its per-person action routes to "no", so it is in no risk list,
--          risk chart, action list or count. The AI's original answers are kept
--          in `muted_from` and restored when the rule is removed.
--   skip   The email is not processed at all (not_indexed, reason operator_rule,
--          no paid call), like the bulk prefilter. Applies to mail processed
--          after the rule exists; ci_email.process reads the rule.
--
-- apply_mail_rules() makes the stored labels and routes match the current
-- quiet rules: it mutes what matches and restores what no longer does. The
-- console calls it after a rule changes; processing calls it for each message
-- before publication, so a matching email is never listed as a risk.

SET search_path = ci, public;

CREATE TABLE mail_rules (
    id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    subject_contains text NOT NULL CHECK (subject_contains = lower(subject_contains)
                                          AND length(btrim(subject_contains)) BETWEEN 6 AND 200),
    sender_address   text CHECK (sender_address IS NULL
                                 OR (sender_address = lower(sender_address)
                                     AND sender_address LIKE '%_@_%')),
    effect           text NOT NULL CHECK (effect IN ('quiet', 'skip')),
    note             text CHECK (note IS NULL OR length(note) <= 200),
    created_by       text NOT NULL,
    created_at       timestamptz NOT NULL DEFAULT now(),
    revoked_at       timestamptz,
    revoked_by       text
);
CREATE UNIQUE INDEX mail_rules_active
    ON mail_rules (subject_contains, coalesce(sender_address, ''), effect)
    WHERE revoked_at IS NULL;

ALTER TABLE classification_labels
    ADD COLUMN muted_from text CHECK (muted_from IN ('no', 'yes', 'review', 'unresolved'));
ALTER TABLE action_routes
    ADD COLUMN muted_from text CHECK (muted_from IN ('no', 'yes', 'review', 'unresolved'));
CREATE INDEX classification_labels_muted ON classification_labels (run_id)
    WHERE muted_from IS NOT NULL;
CREATE INDEX action_routes_muted ON action_routes (version_id) WHERE muted_from IS NOT NULL;

-- The id of an active rule with this effect that the email matches, if any.
CREATE FUNCTION mail_rule_for(subject text, sender text, wanted text) RETURNS bigint
LANGUAGE sql STABLE
SET search_path = ci, public
AS $$
    SELECT r.id FROM mail_rules r
    WHERE r.revoked_at IS NULL AND r.effect = wanted
      AND coalesce(subject, '') !~* '^\s*(re|fw|fwd)\s*:'
      AND strpos(lower(coalesce(subject, '')), r.subject_contains) > 0
      AND (r.sender_address IS NULL OR r.sender_address = lower(coalesce(sender, '')))
    ORDER BY r.id LIMIT 1
$$;

-- Mutes the risk and action answers of emails matching a quiet rule and restores
-- those that no longer match. `only_version` limits it to one message version
-- (processing); NULL does every email (after a rule change). SECURITY DEFINER:
-- the operator role changes rules but has no write access to labels.
CREATE FUNCTION apply_mail_rules(only_version uuid DEFAULT NULL)
RETURNS TABLE (muted_emails bigint, restored_emails bigint)
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = ci, public
AS $$
DECLARE
    quiet uuid[];
    muted bigint := 0;
    restored bigint := 0;
BEGIN
    SELECT coalesce(array_agg(v.id), '{}') INTO quiet
    FROM communication_versions v
    JOIN communications c ON c.id = v.communication_id
    WHERE (only_version IS NULL OR v.id = only_version)
      AND EXISTS (SELECT 1 FROM mail_rules r WHERE r.revoked_at IS NULL AND r.effect = 'quiet'
                    AND strpos(lower(coalesce(c.title, '')), r.subject_contains) > 0)
      AND mail_rule_for(
            c.title,
            (SELECT p.email_normalized FROM communication_participants cp
               JOIN participants p ON p.id = cp.participant_id
              WHERE cp.communication_id = c.id AND cp.role = 'from' LIMIT 1),
            'quiet') IS NOT NULL;

    WITH done AS (
        UPDATE classification_labels l SET muted_from = l.decision, decision = 'no'
        FROM classification_runs r
        WHERE l.run_id = r.id AND r.version_id = ANY(quiet)
          AND l.muted_from IS NULL AND l.decision <> 'no'
          AND l.label IN ('project_risk', 'action_required', 'approval_required')
        RETURNING r.version_id),
    routes AS (
        UPDATE action_routes a SET muted_from = a.decision, decision = 'no'
        WHERE a.version_id = ANY(quiet) AND a.muted_from IS NULL AND a.decision <> 'no'
        RETURNING a.version_id)
    SELECT count(DISTINCT version_id) INTO muted
    FROM (SELECT version_id FROM done UNION ALL SELECT version_id FROM routes) x;

    WITH done AS (
        UPDATE classification_labels l SET decision = l.muted_from, muted_from = NULL
        FROM classification_runs r
        WHERE l.run_id = r.id AND l.muted_from IS NOT NULL
          AND (only_version IS NULL OR r.version_id = only_version)
          AND NOT (r.version_id = ANY(quiet))
        RETURNING r.version_id),
    routes AS (
        UPDATE action_routes a SET decision = a.muted_from, muted_from = NULL
        WHERE a.muted_from IS NOT NULL
          AND (only_version IS NULL OR a.version_id = only_version)
          AND NOT (a.version_id = ANY(quiet))
        RETURNING a.version_id)
    SELECT count(DISTINCT version_id) INTO restored
    FROM (SELECT version_id FROM done UNION ALL SELECT version_id FROM routes) x;

    RETURN QUERY SELECT muted, restored;
END
$$;

-- How many stored emails an active rule matches, for the console's list.
-- SECURITY DEFINER: the operator role cannot read participants; only a count leaves.
CREATE FUNCTION mail_rule_matches(rule_id bigint) RETURNS bigint
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = ci, public
AS $$
    SELECT count(*) FROM mail_rules r JOIN communications c
      ON strpos(lower(coalesce(c.title, '')), r.subject_contains) > 0
     AND coalesce(c.title, '') !~* '^\s*(re|fw|fwd)\s*:'
    WHERE r.id = rule_id AND r.revoked_at IS NULL
      AND (r.sender_address IS NULL OR EXISTS (
            SELECT 1 FROM communication_participants cp
            JOIN participants p ON p.id = cp.participant_id
            WHERE cp.communication_id = c.id AND cp.role = 'from'
              AND p.email_normalized = r.sender_address))
$$;

REVOKE ALL ON FUNCTION mail_rule_matches(bigint) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION mail_rule_matches(bigint) TO ci_operator;
REVOKE ALL ON FUNCTION apply_mail_rules(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION apply_mail_rules(uuid) TO ci_ingest, ci_operator;
GRANT EXECUTE ON FUNCTION mail_rule_for(text, text, text) TO ci_ingest, ci_operator;
GRANT SELECT ON mail_rules TO ci_ingest, ci_operator;
GRANT INSERT, UPDATE ON mail_rules TO ci_operator;
