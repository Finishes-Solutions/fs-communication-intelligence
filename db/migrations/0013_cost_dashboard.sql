-- 0013_cost_dashboard.sql
-- The admin console's cost page (Matthew, 2026-10-28: "see exactly what my
-- costs are"). Two sources: Azure's billed cost (read live from Cost
-- Management, not stored) and this metered usage, recorded per processing run
-- from what each paid call reported (tokens) or sent (billable text records),
-- priced with editable unit prices. Counts only, never content.

SET search_path = ci, public;

CREATE TABLE ai_usage (
    id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id        bigint REFERENCES ops_runs(id) ON DELETE SET NULL,
    source        text NOT NULL CHECK (source IN ('processing', 'cli', 'lab')),
    meter         text NOT NULL
                  CHECK (meter IN ('openai_chat', 'openai_embedding', 'language_pii',
                                   'prompt_shields')),
    model         text NOT NULL,
    calls         bigint NOT NULL DEFAULT 0 CHECK (calls >= 0),
    input_tokens  bigint NOT NULL DEFAULT 0 CHECK (input_tokens >= 0),
    output_tokens bigint NOT NULL DEFAULT 0 CHECK (output_tokens >= 0),
    characters    bigint NOT NULL DEFAULT 0 CHECK (characters >= 0),
    records       bigint NOT NULL DEFAULT 0 CHECK (records >= 0),
    recorded_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ai_usage_recent ON ai_usage (recorded_at DESC);
CREATE INDEX ai_usage_run ON ai_usage (run_id);

-- Unit prices for the estimate. The defaults are unverified list prices
-- (verified = false) to be checked against the Azure pricing page or the
-- billed figures and corrected in the console.
CREATE TABLE cost_prices (
    key        text PRIMARY KEY
               CHECK (key IN ('openai_chat_input', 'openai_chat_output', 'openai_embedding',
                              'language_pii', 'prompt_shields')),
    label      text NOT NULL,
    per_units  bigint NOT NULL CHECK (per_units > 0),  -- the price is per this many units
    unit       text NOT NULL CHECK (unit IN ('tokens', 'text records')),
    usd        numeric(12, 6) NOT NULL CHECK (usd >= 0),
    verified   boolean NOT NULL DEFAULT false,
    updated_at timestamptz NOT NULL DEFAULT now(),
    updated_by text NOT NULL DEFAULT 'migration'
);
INSERT INTO cost_prices (key, label, per_units, unit, usd) VALUES
    ('openai_chat_input', 'Azure OpenAI chat (gpt-4.1-mini), input', 1000000, 'tokens', 0.44),
    ('openai_chat_output', 'Azure OpenAI chat (gpt-4.1-mini), output', 1000000, 'tokens', 1.76),
    ('openai_embedding', 'Azure OpenAI embeddings (text-embedding-3-large)', 1000000, 'tokens',
     0.13),
    ('language_pii', 'Azure AI Language PII detection (masking)', 1000, 'text records', 1.00),
    ('prompt_shields', 'Content Safety Prompt Shields', 1000, 'text records', 0.38);

-- Each usage row priced with the current unit prices.
CREATE VIEW ops_usage_cost AS
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
           WHEN 'openai_chat' THEN u.input_tokens * p.chat_in + u.output_tokens * p.chat_out
           WHEN 'openai_embedding' THEN u.input_tokens * p.embedding
           WHEN 'language_pii' THEN u.records * p.pii
           WHEN 'prompt_shields' THEN u.records * p.shields
       END::numeric(14, 6) AS estimated_usd
FROM ai_usage u CROSS JOIN p;

GRANT SELECT, INSERT ON ai_usage TO ci_ingest;
GRANT SELECT, INSERT ON ai_usage TO ci_operator;  -- the console's lab records its calls
GRANT SELECT ON ops_usage_cost TO ci_operator;
GRANT SELECT, UPDATE (usd, verified, updated_at, updated_by) ON cost_prices TO ci_operator;
