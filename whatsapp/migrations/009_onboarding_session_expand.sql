-- 009: Onboarding session — expiration, coexistence flag, method label
-- Expand-only.

ALTER TABLE onboarding_sessions ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ;
ALTER TABLE onboarding_sessions ADD COLUMN IF NOT EXISTS is_coexistence BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE onboarding_sessions ADD COLUMN IF NOT EXISTS onboarding_method VARCHAR(40);

-- Optional: align method with path for existing rows (no-op if column absent before)
UPDATE onboarding_sessions SET onboarding_method = COALESCE(onboarding_method, onboarding_path) WHERE onboarding_method IS NULL;
