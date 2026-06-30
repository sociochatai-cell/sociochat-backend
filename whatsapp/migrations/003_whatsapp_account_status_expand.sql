-- Expand-and-contract Phase 3: account status / heartbeat slice (NO drops from whatsapp_accounts)
-- Run once against the shared PostgreSQL database.
-- High-churn fields (webhook timestamps, errors) update whatsapp_account_status to reduce row locks on whatsapp_accounts.

-- 0) Legacy mirror columns on whatsapp_accounts (monolith / CRM reads)
ALTER TABLE whatsapp_accounts
    ADD COLUMN IF NOT EXISTS connection_status VARCHAR(50),
    ADD COLUMN IF NOT EXISTS is_verified BOOLEAN,
    ADD COLUMN IF NOT EXISTS last_webhook_received TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS last_outbound_sent TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS last_error_message TEXT,
    ADD COLUMN IF NOT EXISTS last_error_code INTEGER;

-- 1) Status slice (one row per account)
CREATE TABLE IF NOT EXISTS whatsapp_account_status (
    id SERIAL PRIMARY KEY,
    account_id INTEGER NOT NULL UNIQUE REFERENCES whatsapp_accounts (id) ON DELETE CASCADE,

    status VARCHAR(50) NOT NULL DEFAULT 'connected',
    quality_rating VARCHAR(20),
    is_verified BOOLEAN DEFAULT FALSE NOT NULL,

    last_webhook_received TIMESTAMPTZ,
    last_outbound_sent TIMESTAMPTZ,

    last_error_message TEXT,
    last_error_code INTEGER,

    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_whatsapp_account_status_account
    ON whatsapp_account_status (account_id);

-- 2) Optional backfill when legacy / Meta fields already populated (no mass insert of defaults)
INSERT INTO whatsapp_account_status (
    account_id,
    status,
    quality_rating,
    is_verified,
    last_webhook_received,
    last_outbound_sent,
    last_error_message,
    last_error_code
)
SELECT
    wa.id,
    COALESCE(wa.connection_status, 'connected'),
    wa.quality_score,
    COALESCE(wa.is_verified, FALSE),
    wa.last_webhook_received,
    wa.last_outbound_sent,
    wa.last_error_message,
    CASE
        WHEN wa.last_error_code IS NULL THEN NULL
        WHEN trim(wa.last_error_code::text) ~ '^-?[0-9]+$' THEN trim(wa.last_error_code::text)::integer
        ELSE NULL
    END AS last_error_code
FROM whatsapp_accounts wa
WHERE wa.connection_status IS NOT NULL
   OR wa.quality_score IS NOT NULL
   OR wa.is_verified IS NOT NULL
   OR wa.last_webhook_received IS NOT NULL
   OR wa.last_outbound_sent IS NOT NULL
   OR wa.last_error_message IS NOT NULL
   OR wa.last_error_code IS NOT NULL
ON CONFLICT (account_id) DO UPDATE SET
    status = COALESCE(EXCLUDED.status, whatsapp_account_status.status),
    quality_rating = COALESCE(EXCLUDED.quality_rating, whatsapp_account_status.quality_rating),
    is_verified = COALESCE(EXCLUDED.is_verified, whatsapp_account_status.is_verified),
    last_webhook_received = COALESCE(EXCLUDED.last_webhook_received, whatsapp_account_status.last_webhook_received),
    last_outbound_sent = COALESCE(EXCLUDED.last_outbound_sent, whatsapp_account_status.last_outbound_sent),
    last_error_message = COALESCE(EXCLUDED.last_error_message, whatsapp_account_status.last_error_message),
    last_error_code = COALESCE(EXCLUDED.last_error_code, whatsapp_account_status.last_error_code),
    updated_at = NOW();
