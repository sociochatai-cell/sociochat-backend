-- 010: Safe-mode reason code + override window, webhook cooldown, operational metrics JSON
-- Expand-only, idempotent. Aligns whatsapp_accounts with whatsapp.models.WhatsAppAccount.

ALTER TABLE whatsapp_accounts ADD COLUMN IF NOT EXISTS safe_mode_reason_code VARCHAR(64);
ALTER TABLE whatsapp_accounts ADD COLUMN IF NOT EXISTS safe_mode_override_expires_at TIMESTAMPTZ;

ALTER TABLE whatsapp_accounts ADD COLUMN IF NOT EXISTS webhook_cooldown_ends_at TIMESTAMPTZ;

ALTER TABLE whatsapp_accounts ADD COLUMN IF NOT EXISTS operational_metrics JSONB;
