-- 0014_model_choice.sql
-- The Azure OpenAI chat deployment that classifies email (and runs the
-- per-person check) is chosen in the admin console (Matthew, 2026-10-28);
-- NULL keeps the app setting AZURE_OPENAI_CHAT_DEPLOYMENT. Prices per
-- deployment let the cost page compare models; a deployment without a row is
-- priced with the default chat prices in cost_prices.

SET search_path = ci, public;

ALTER TABLE ops_settings
    ADD COLUMN IF NOT EXISTS chat_deployment text
        CHECK (chat_deployment IS NULL OR chat_deployment ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$');

CREATE TABLE IF NOT EXISTS model_prices (
    deployment        text PRIMARY KEY CHECK (deployment ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$'),
    input_usd_per_1m  numeric(12, 6) NOT NULL CHECK (input_usd_per_1m >= 0),
    output_usd_per_1m numeric(12, 6) NOT NULL CHECK (output_usd_per_1m >= 0),
    verified          boolean NOT NULL DEFAULT false,
    updated_at        timestamptz NOT NULL DEFAULT now(),
    updated_by        text NOT NULL
);

-- Same columns as in 0013; chat usage is priced per deployment when a price is set.
CREATE OR REPLACE VIEW ops_usage_cost AS
WITH p AS (
    SELECT
        max(usd / per_units) FILTER (WHERE key = 'openai_chat_input') AS chat_in,
        max(usd / per_units) FILTER (WHERE key = 'openai_chat_output') AS chat_out,
        max(usd / per_units) FILTER (WHERE key = 'openai_embedding') AS embedding,
        max(usd / per_units) FILTER (WHERE key = 'language_pii') AS pii,
        max(usd / per_units) FILTER (WHERE key = 'prompt_shields') AS shields
    FROM cost_prices
)
SELECT u.id, u.run_id, u.source, u.meter, u.model, u.calls, u.input_tokens, u.output_tokens,
       u.characters, u.records, u.recorded_at,
       CASE u.meter
           WHEN 'openai_chat' THEN
               u.input_tokens * coalesce(m.input_usd_per_1m / 1000000, p.chat_in)
               + u.output_tokens * coalesce(m.output_usd_per_1m / 1000000, p.chat_out)
           WHEN 'openai_embedding' THEN u.input_tokens * p.embedding
           WHEN 'language_pii' THEN u.records * p.pii
           WHEN 'prompt_shields' THEN u.records * p.shields
       END::numeric(14, 6) AS estimated_usd
FROM ai_usage u
CROSS JOIN p
LEFT JOIN model_prices m ON u.meter = 'openai_chat' AND u.model = 'azure-openai:' || m.deployment;

GRANT SELECT ON model_prices TO ci_ingest;
GRANT SELECT, INSERT, UPDATE, DELETE ON model_prices TO ci_operator;
