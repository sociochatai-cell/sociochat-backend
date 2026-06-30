-- Expand-and-contract Phase 1: credential slice (NO drops from whatsapp_accounts)
-- Run once against the shared PostgreSQL database.
-- Tokens stay encrypted (same ciphertext as whatsapp_accounts.access_token_encrypted).

CREATE TABLE IF NOT EXISTS whatsapp_credentials (
    id SERIAL PRIMARY KEY,
    account_id INTEGER UNIQUE NOT NULL,
    access_token_encrypted TEXT,
    phone_number_id VARCHAR(64),
    waba_id VARCHAR(64),
    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_whatsapp_credentials_account
        FOREIGN KEY (account_id)
        REFERENCES whatsapp_accounts (id)
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_whatsapp_credentials_account
    ON whatsapp_credentials (account_id);

-- Idempotent backfill from legacy columns (monolith + microservice keep reading these)
INSERT INTO whatsapp_credentials (account_id, access_token_encrypted, phone_number_id, waba_id)
SELECT
    wa.id AS account_id,
    wa.access_token_encrypted,
    wa.phone_number_id,
    wa.waba_id
FROM whatsapp_accounts wa
WHERE wa.access_token_encrypted IS NOT NULL
   OR wa.phone_number_id IS NOT NULL
   OR wa.waba_id IS NOT NULL
ON CONFLICT (account_id) DO UPDATE SET
    access_token_encrypted = COALESCE(EXCLUDED.access_token_encrypted, whatsapp_credentials.access_token_encrypted),
    phone_number_id = COALESCE(EXCLUDED.phone_number_id, whatsapp_credentials.phone_number_id),
    waba_id = COALESCE(EXCLUDED.waba_id, whatsapp_credentials.waba_id),
    updated_at = CURRENT_TIMESTAMP;

-- Optional sanity check (comment out in automation if noisy)
-- SELECT
--   (SELECT COUNT(*) FROM whatsapp_accounts WHERE access_token_encrypted IS NOT NULL) AS accounts_with_token,
--   (SELECT COUNT(*) FROM whatsapp_credentials WHERE access_token_encrypted IS NOT NULL) AS cred_rows_with_token;
