-- Expand-and-contract Phase 2: bot / AI settings slice (NO drops from whatsapp_accounts)
-- Run once against the shared PostgreSQL database.
-- Keeps legacy columns on whatsapp_accounts for monolith reads; microservice prefers whatsapp_bot_settings.

-- 0) Legacy columns on god-node table (nullable = "unspecified" for ai_enabled; monolith can ignore until ready)
ALTER TABLE whatsapp_accounts
    ADD COLUMN IF NOT EXISTS ai_enabled BOOLEAN,
    ADD COLUMN IF NOT EXISTS ai_model VARCHAR(50),
    ADD COLUMN IF NOT EXISTS temperature DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS max_tokens INTEGER,
    ADD COLUMN IF NOT EXISTS prompt_override TEXT,
    ADD COLUMN IF NOT EXISTS knowledge_base_id VARCHAR(100);

-- 1) Dedicated bot settings row (high-churn AI updates lock this table, not whatsapp_accounts)
CREATE TABLE IF NOT EXISTS whatsapp_bot_settings (
    id SERIAL PRIMARY KEY,
    account_id INTEGER NOT NULL UNIQUE REFERENCES whatsapp_accounts (id) ON DELETE CASCADE,

    ai_enabled BOOLEAN DEFAULT FALSE NOT NULL,
    ai_model VARCHAR(50) DEFAULT 'gemini-1.5-flash',
    temperature DOUBLE PRECISION DEFAULT 0.7,
    max_tokens INTEGER DEFAULT 1000,
    prompt_override TEXT,
    knowledge_base_id VARCHAR(100),

    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_whatsapp_bot_settings_account
    ON whatsapp_bot_settings (account_id);

-- 2) Backfill only when legacy AI fields are set (avoids creating all-false rows that would gate AI off for every account)
INSERT INTO whatsapp_bot_settings (
    account_id,
    ai_enabled,
    ai_model,
    temperature,
    max_tokens,
    prompt_override,
    knowledge_base_id
)
SELECT
    wa.id AS account_id,
    COALESCE(wa.ai_enabled, FALSE) AS ai_enabled,
    COALESCE(wa.ai_model, 'gemini-1.5-flash') AS ai_model,
    COALESCE(wa.temperature, 0.7) AS temperature,
    COALESCE(wa.max_tokens, 1000) AS max_tokens,
    wa.prompt_override,
    wa.knowledge_base_id
FROM whatsapp_accounts wa
WHERE wa.ai_enabled IS NOT NULL
   OR wa.ai_model IS NOT NULL
   OR wa.temperature IS NOT NULL
   OR wa.max_tokens IS NOT NULL
   OR wa.prompt_override IS NOT NULL
   OR wa.knowledge_base_id IS NOT NULL
ON CONFLICT (account_id) DO UPDATE SET
    ai_enabled = COALESCE(EXCLUDED.ai_enabled, whatsapp_bot_settings.ai_enabled),
    ai_model = COALESCE(EXCLUDED.ai_model, whatsapp_bot_settings.ai_model),
    temperature = COALESCE(EXCLUDED.temperature, whatsapp_bot_settings.temperature),
    max_tokens = COALESCE(EXCLUDED.max_tokens, whatsapp_bot_settings.max_tokens),
    prompt_override = COALESCE(EXCLUDED.prompt_override, whatsapp_bot_settings.prompt_override),
    knowledge_base_id = COALESCE(EXCLUDED.knowledge_base_id, whatsapp_bot_settings.knowledge_base_id),
    updated_at = NOW();
