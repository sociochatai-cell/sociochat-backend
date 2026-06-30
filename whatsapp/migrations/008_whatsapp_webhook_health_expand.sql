-- 008: Webhook health + integrity persistence (expand-only)
-- Advisory-only fields; no enforcement triggers.

ALTER TABLE whatsapp_accounts ADD COLUMN IF NOT EXISTS webhook_last_validated_at TIMESTAMPTZ;
ALTER TABLE whatsapp_accounts ADD COLUMN IF NOT EXISTS webhook_last_event_at TIMESTAMPTZ;
ALTER TABLE whatsapp_accounts ADD COLUMN IF NOT EXISTS webhook_failure_reason TEXT;
ALTER TABLE whatsapp_accounts ADD COLUMN IF NOT EXISTS webhook_failure_count INTEGER NOT NULL DEFAULT 0;
