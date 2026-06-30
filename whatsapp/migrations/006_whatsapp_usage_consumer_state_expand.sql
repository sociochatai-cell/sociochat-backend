-- Consumer-side state for monolith/billing (expand-only, shared DB pattern).
-- WhatsApp service does not write these tables; billing worker does.
--
-- Idempotency: dedupe on event_key (same key across retries/replays).
-- Ordering / checkpoint: advance last_event_id using event id only after successful apply.

CREATE TABLE IF NOT EXISTS whatsapp_usage_event_checkpoints (
    consumer_name TEXT PRIMARY KEY,
    last_event_id BIGINT NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS whatsapp_usage_events_processed (
    event_key VARCHAR(191) PRIMARY KEY,
    source_event_id BIGINT,
    consumer_name TEXT NOT NULL,
    processed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_whatsapp_usage_events_processed_at
    ON whatsapp_usage_events_processed (processed_at DESC);

CREATE INDEX IF NOT EXISTS idx_whatsapp_usage_events_processed_consumer
    ON whatsapp_usage_events_processed (consumer_name, processed_at DESC);

-- Malformed / rejected payloads: park for ops without blocking the poll cursor.
CREATE TABLE IF NOT EXISTS whatsapp_usage_events_quarantine (
    id BIGSERIAL PRIMARY KEY,
    consumer_name TEXT NOT NULL,
    source_event_id BIGINT,
    event_key VARCHAR(191),
    raw_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    error_message TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_whatsapp_usage_events_quarantine_created
    ON whatsapp_usage_events_quarantine (created_at DESC);
