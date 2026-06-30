-- Customer webhook forwarding URL per WhatsApp account (expand-only, idempotent).
ALTER TABLE whatsapp_accounts ADD COLUMN IF NOT EXISTS customer_webhook_url VARCHAR(512);
ALTER TABLE whatsapp_accounts ADD COLUMN IF NOT EXISTS customer_webhook_secret_header VARCHAR(128);
ALTER TABLE whatsapp_accounts ADD COLUMN IF NOT EXISTS customer_webhook_secret_value VARCHAR(512);
