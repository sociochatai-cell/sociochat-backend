-- Add notify_owner_whatsapp toggle to whatsapp_flows
-- When TRUE, form submissions send a WhatsApp message to the owner's registered number
ALTER TABLE whatsapp_flows
    ADD COLUMN IF NOT EXISTS notify_owner_whatsapp BOOLEAN DEFAULT FALSE NOT NULL;
