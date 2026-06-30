-- 012: Operational / safe-mode explainability log (ORM: WhatsAppOperationalLog)
-- Expand-only. Required for GET .../operational-timeline and safe_mode_engine logging.

CREATE TABLE IF NOT EXISTS whatsapp_operational_logs (
    id BIGSERIAL PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES whatsapp_accounts(id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    event_type VARCHAR(64) NOT NULL,
    previous_mode VARCHAR(64),
    new_mode VARCHAR(64),
    reason TEXT,
    context JSONB,
    expires_at TIMESTAMPTZ,
    actor VARCHAR(64) NOT NULL DEFAULT 'system'
);

CREATE INDEX IF NOT EXISTS ix_whatsapp_op_logs_account_time
    ON whatsapp_operational_logs (account_id, created_at DESC);
