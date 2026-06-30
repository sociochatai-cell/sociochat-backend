-- Entitlement projection (expand-only). Monolith (or its worker) is the writer of truth.
-- WhatsApp microservice reads this table only — no billing/plan joins here.

CREATE TABLE IF NOT EXISTS whatsapp_account_capabilities (
    account_id INTEGER PRIMARY KEY REFERENCES whatsapp_accounts (id) ON DELETE CASCADE,

    subscription_status VARCHAR(32) NOT NULL DEFAULT 'ACTIVE',
    ai_enabled BOOLEAN NOT NULL DEFAULT TRUE,
    automation_enabled BOOLEAN NOT NULL DEFAULT TRUE,
    broadcast_enabled BOOLEAN NOT NULL DEFAULT TRUE,

    daily_message_limit INTEGER,
    monthly_ai_tokens BIGINT,

    projection_version INTEGER NOT NULL DEFAULT 1,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_whatsapp_account_capabilities_updated
    ON whatsapp_account_capabilities (updated_at DESC);
