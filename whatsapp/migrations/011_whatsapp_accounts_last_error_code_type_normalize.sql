-- 011: Normalize whatsapp_accounts.last_error_code to INTEGER when legacy DDL used VARCHAR/TEXT.
-- Expand-only, idempotent for already-integer columns.

DO $$
BEGIN
  IF EXISTS (
    SELECT 1
    FROM information_schema.columns
    WHERE table_schema = 'public'
      AND table_name = 'whatsapp_accounts'
      AND column_name = 'last_error_code'
      AND data_type IN ('character varying', 'text', 'character')
  ) THEN
    ALTER TABLE whatsapp_accounts
      ALTER COLUMN last_error_code TYPE integer USING (
        CASE
          WHEN last_error_code IS NULL THEN NULL
          WHEN trim(last_error_code::text) ~ '^-?[0-9]+$' THEN trim(last_error_code::text)::integer
          ELSE NULL
        END
      );
  END IF;
END $$;
