-- 0012_cost_switches.sql
-- Operator switches for the two paid per-character checks, so they can be
-- turned off from the admin console while testing (Matthew, 2026-10-28):
-- Prompt Shields (prompt-injection detection) and Azure AI Language masking.
-- Messages processed with either off are recorded in policy_decisions with
-- service_ref 'disabled_by_operator'; `ci process --rescreen` redoes them.
-- Apply after the code that reads these columns is deployed (older code
-- reads ops_settings with SELECT * into a fixed set of fields).

SET search_path = ci, public;

ALTER TABLE ops_settings
    ADD COLUMN IF NOT EXISTS prompt_shield_enabled boolean NOT NULL DEFAULT true,
    ADD COLUMN IF NOT EXISTS masking_enabled boolean NOT NULL DEFAULT true;
