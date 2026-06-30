-- 007: Platform V2 — onboarding sessions, trust / reputation snapshots, webhook & restriction fields
-- Expand-only, idempotent. Safe to re-run after partial failure.
-- Rollback: drop new tables / columns only in a controlled maintenance window (not automated here).

-- ── whatsapp_accounts: lifecycle, warmup, operational mode, restrictions ─────────
ALTER TABLE whatsapp_accounts ADD COLUMN IF NOT EXISTS onboarding_lifecycle_state VARCHAR(32) NOT NULL DEFAULT 'active';
ALTER TABLE whatsapp_accounts ADD COLUMN IF NOT EXISTS warmup_started_at TIMESTAMPTZ;
ALTER TABLE whatsapp_accounts ADD COLUMN IF NOT EXISTS warmup_ends_at TIMESTAMPTZ;
ALTER TABLE whatsapp_accounts ADD COLUMN IF NOT EXISTS warmup_config JSONB;

ALTER TABLE whatsapp_accounts ADD COLUMN IF NOT EXISTS operational_mode VARCHAR(24) NOT NULL DEFAULT 'normal';
ALTER TABLE whatsapp_accounts ADD COLUMN IF NOT EXISTS safe_mode_advisory_only BOOLEAN NOT NULL DEFAULT true;
ALTER TABLE whatsapp_accounts ADD COLUMN IF NOT EXISTS safe_mode_reason TEXT;

ALTER TABLE whatsapp_accounts ADD COLUMN IF NOT EXISTS restriction_state VARCHAR(40) NOT NULL DEFAULT 'none';
ALTER TABLE whatsapp_accounts ADD COLUMN IF NOT EXISTS restriction_reason TEXT;
ALTER TABLE whatsapp_accounts ADD COLUMN IF NOT EXISTS restriction_detected_at TIMESTAMPTZ;
ALTER TABLE whatsapp_accounts ADD COLUMN IF NOT EXISTS restriction_source VARCHAR(64);

-- ── whatsapp_accounts: webhook health + coexistence echo validation (JSON map) ───
ALTER TABLE whatsapp_accounts ADD COLUMN IF NOT EXISTS webhook_subscription_status VARCHAR(32);
ALTER TABLE whatsapp_accounts ADD COLUMN IF NOT EXISTS webhook_last_success_at TIMESTAMPTZ;
ALTER TABLE whatsapp_accounts ADD COLUMN IF NOT EXISTS webhook_last_failure_at TIMESTAMPTZ;
ALTER TABLE whatsapp_accounts ADD COLUMN IF NOT EXISTS webhook_last_error TEXT;
ALTER TABLE whatsapp_accounts ADD COLUMN IF NOT EXISTS webhook_health VARCHAR(24) NOT NULL DEFAULT 'unknown';
ALTER TABLE whatsapp_accounts ADD COLUMN IF NOT EXISTS webhook_last_checked_at TIMESTAMPTZ;
ALTER TABLE whatsapp_accounts ADD COLUMN IF NOT EXISTS webhook_subscribed_app_verified BOOLEAN;
ALTER TABLE whatsapp_accounts ADD COLUMN IF NOT EXISTS last_inbound_webhook_at TIMESTAMPTZ;
ALTER TABLE whatsapp_accounts ADD COLUMN IF NOT EXISTS webhook_echo_validation JSONB;

-- ── whatsapp_accounts: token health, embedded signup versioning, message health ──
ALTER TABLE whatsapp_accounts ADD COLUMN IF NOT EXISTS token_health VARCHAR(24) NOT NULL DEFAULT 'unknown';
ALTER TABLE whatsapp_accounts ADD COLUMN IF NOT EXISTS token_health_checked_at TIMESTAMPTZ;
ALTER TABLE whatsapp_accounts ADD COLUMN IF NOT EXISTS token_health_detail JSONB;

ALTER TABLE whatsapp_accounts ADD COLUMN IF NOT EXISTS embedded_signup_version VARCHAR(32);
ALTER TABLE whatsapp_accounts ADD COLUMN IF NOT EXISTS graph_version_used VARCHAR(16);
ALTER TABLE whatsapp_accounts ADD COLUMN IF NOT EXISTS fb_sdk_version VARCHAR(32);
ALTER TABLE whatsapp_accounts ADD COLUMN IF NOT EXISTS whatsapp_config_id_used VARCHAR(64);

ALTER TABLE whatsapp_accounts ADD COLUMN IF NOT EXISTS message_health_metrics JSONB;

ALTER TABLE whatsapp_accounts ADD COLUMN IF NOT EXISTS trust_score NUMERIC(6,2);
ALTER TABLE whatsapp_accounts ADD COLUMN IF NOT EXISTS trust_score_computed_at TIMESTAMPTZ;

-- Legal / ops: explicit ownership acknowledgement (customer owns WABA; Sociovia is delegated)
ALTER TABLE whatsapp_accounts ADD COLUMN IF NOT EXISTS ownership_model_version VARCHAR(32) NOT NULL DEFAULT 'customer_waba_v1';

-- Backfill: existing rows keep active + normal + none + unknown health (defaults already applied on ADD COLUMN)

-- ── onboarding_sessions (abandonment recovery, embedded signup correlation) ──────
CREATE TABLE IF NOT EXISTS onboarding_sessions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id VARCHAR(255) NOT NULL,
    user_id VARCHAR(64) NOT NULL,
    account_id INTEGER REFERENCES whatsapp_accounts(id) ON DELETE SET NULL,
    correlation_id VARCHAR(64) NOT NULL,
    onboarding_path VARCHAR(40) NOT NULL DEFAULT 'embedded',
    business_manager_id VARCHAR(64),
    waba_id VARCHAR(64),
    phone_number_id VARCHAR(64),
    embedded_signup_version VARCHAR(32),
    graph_version VARCHAR(16),
    sdk_version VARCHAR(32),
    config_id VARCHAR(64),
    status VARCHAR(32) NOT NULL DEFAULT 'started',
    last_step VARCHAR(64),
    session_payload JSONB,
    last_error TEXT,
    resume_token_hash VARCHAR(128),
    abandoned_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    last_event_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_onboarding_sessions_workspace ON onboarding_sessions(workspace_id);
CREATE INDEX IF NOT EXISTS ix_onboarding_sessions_correlation ON onboarding_sessions(correlation_id);
CREATE INDEX IF NOT EXISTS ix_onboarding_sessions_status ON onboarding_sessions(status);
CREATE INDEX IF NOT EXISTS ix_onboarding_sessions_user ON onboarding_sessions(user_id);

-- ── onboarding_events (audit trail per session) ───────────────────────────────────
CREATE TABLE IF NOT EXISTS onboarding_events (
    id BIGSERIAL PRIMARY KEY,
    session_id UUID NOT NULL REFERENCES onboarding_sessions(id) ON DELETE CASCADE,
    account_id INTEGER REFERENCES whatsapp_accounts(id) ON DELETE SET NULL,
    event_type VARCHAR(80) NOT NULL,
    correlation_id VARCHAR(64),
    payload JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_onboarding_events_session ON onboarding_events(session_id, created_at DESC);

-- ── trust_snapshots (daily / on-demand; advisory trust inputs, not auto-ban logic) ─
CREATE TABLE IF NOT EXISTS trust_snapshots (
    id BIGSERIAL PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES whatsapp_accounts(id) ON DELETE CASCADE,
    captured_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    quality_rating VARCHAR(16),
    messaging_tier VARCHAR(32),
    name_status VARCHAR(64),
    verification_status VARCHAR(64),
    webhook_health VARCHAR(24),
    webhook_subscription_status VARCHAR(32),
    restriction_state VARCHAR(40),
    operational_mode VARCHAR(24),
    trust_score NUMERIC(6,2),
    inputs JSONB,
    notes TEXT
);

CREATE INDEX IF NOT EXISTS ix_trust_snapshots_account_time ON trust_snapshots(account_id, captured_at DESC);

-- ── whatsapp_phone_reputation_snapshots (quality / tier / name over time) ─────────
CREATE TABLE IF NOT EXISTS whatsapp_phone_reputation_snapshots (
    id BIGSERIAL PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES whatsapp_accounts(id) ON DELETE CASCADE,
    phone_number_id VARCHAR(64) NOT NULL,
    captured_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    quality_rating VARCHAR(16),
    messaging_tier VARCHAR(32),
    name_status VARCHAR(64),
    raw_graph JSONB
);

CREATE INDEX IF NOT EXISTS ix_phone_rep_account_time ON whatsapp_phone_reputation_snapshots(account_id, captured_at DESC);
CREATE INDEX IF NOT EXISTS ix_phone_rep_phone_time ON whatsapp_phone_reputation_snapshots(phone_number_id, captured_at DESC);

-- ── meta_app_health_snapshots (platform-wide signals vs tenant-specific) ───────────
CREATE TABLE IF NOT EXISTS meta_app_health_snapshots (
    id BIGSERIAL PRIMARY KEY,
    captured_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    window_minutes INTEGER NOT NULL DEFAULT 30,
    graph_error_rate NUMERIC(8,6),
    webhook_callback_error_count INTEGER,
    rate_limit_hits INTEGER,
    verification_failure_count INTEGER,
    details JSONB
);

CREATE INDEX IF NOT EXISTS ix_meta_app_health_time ON meta_app_health_snapshots(captured_at DESC);
