-- Notification email for AI escalation alerts + optional inbox flags live in attribution_data JSON.
ALTER TABLE whatsapp_accounts
    ADD COLUMN IF NOT EXISTS notification_email VARCHAR(255);
