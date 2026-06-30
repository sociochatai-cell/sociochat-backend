-- Migration 016: Per-workspace lead auto-discovery / intent-routing / nurture settings.
-- Backs whatsapp.lead_growth_models.WhatsAppLeadGrowthSettings and the lead-growth
-- config API + inbound auto-discovery pipeline. workspace_id is INTEGER to match
-- leads.workspace_id / workspaces2.id; UNIQUE => exactly one row per workspace.

CREATE TABLE IF NOT EXISTS whatsapp_lead_growth_settings (
    id                      SERIAL PRIMARY KEY,
    workspace_id            INTEGER NOT NULL UNIQUE,
    intent_rules            JSONB   NOT NULL DEFAULT '{}'::jsonb,
    confidence_threshold    DOUBLE PRECISION NOT NULL DEFAULT 0.55,
    auto_discovery_enabled  BOOLEAN NOT NULL DEFAULT FALSE,
    notify_enabled          BOOLEAN NOT NULL DEFAULT FALSE,
    notify_template         VARCHAR(255),
    notify_destinations     JSONB   NOT NULL DEFAULT '[]'::jsonb,
    nurture_enabled         BOOLEAN NOT NULL DEFAULT FALSE,
    created_at              TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at              TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS ix_whatsapp_lead_growth_settings_workspace_id
    ON whatsapp_lead_growth_settings (workspace_id);

COMMENT ON TABLE whatsapp_lead_growth_settings IS
    'Per-workspace toggles + tunables for WhatsApp inbound lead auto-discovery, intent->stage routing, new-lead notification and nurture.';
