-- Durable outbound usage events (expand-only).
-- WhatsApp service writes; billing/monolith reads with since_id cursor.

CREATE TABLE IF NOT EXISTS whatsapp_usage_events (
    id BIGSERIAL PRIMARY KEY,
    event_type VARCHAR(64) NOT NULL,
    event_key VARCHAR(191) NOT NULL UNIQUE,

    account_id INTEGER NOT NULL REFERENCES whatsapp_accounts (id) ON DELETE CASCADE,
    message_id INTEGER REFERENCES whatsapp_messages (id) ON DELETE SET NULL,
    wamid VARCHAR(128),

    occurred_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_whatsapp_usage_events_type
    ON whatsapp_usage_events (event_type);

CREATE INDEX IF NOT EXISTS idx_whatsapp_usage_events_account
    ON whatsapp_usage_events (account_id);

CREATE INDEX IF NOT EXISTS idx_whatsapp_usage_events_created
    ON whatsapp_usage_events (created_at DESC);

CREATE INDEX IF NOT EXISTS idx_whatsapp_usage_events_wamid
    ON whatsapp_usage_events (wamid);
