-- Migration 014: Add Idempotency Table to Prevent Duplicate Sends
CREATE TABLE IF NOT EXISTS whatsapp_send_idempotency (
    dedup_key VARCHAR(255) PRIMARY KEY,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);
