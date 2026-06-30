-- WhatsApp Flows table
CREATE TABLE IF NOT EXISTS whatsapp_flows (
    id SERIAL PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES whatsapp_accounts(id) ON DELETE CASCADE,
    
    -- Flow identity
    name VARCHAR(128) NOT NULL,
    category VARCHAR(32) NOT NULL,
    
    -- Versioning
    flow_version INTEGER DEFAULT 1 NOT NULL,
    parent_flow_id INTEGER NULL REFERENCES whatsapp_flows(id) ON DELETE SET NULL,
    
    -- Flow content
    flow_json JSONB NOT NULL,
    schema_version VARCHAR(16) DEFAULT '5.0' NOT NULL,
    entry_screen_id VARCHAR(64) NOT NULL,
    
    -- Meta sync
    meta_flow_id VARCHAR(64),
    status VARCHAR(16) DEFAULT 'DRAFT' NOT NULL,
    
    -- Timestamps
    created_at TIMESTAMP DEFAULT NOW() NOT NULL,
    updated_at TIMESTAMP DEFAULT NOW(),
    published_at TIMESTAMP,
    
    -- Constraints
    CONSTRAINT uq_flow_name_version UNIQUE(account_id, name, flow_version),
    CONSTRAINT chk_flow_status CHECK (status IN ('DRAFT', 'PUBLISHED', 'DEPRECATED'))
);

-- Indexes for performance
CREATE INDEX IF NOT EXISTS ix_whatsapp_flows_account ON whatsapp_flows(account_id);
CREATE INDEX IF NOT EXISTS ix_whatsapp_flows_status ON whatsapp_flows(status);
CREATE INDEX IF NOT EXISTS ix_whatsapp_flows_parent ON whatsapp_flows(parent_flow_id);
CREATE INDEX IF NOT EXISTS ix_whatsapp_flows_meta_id ON whatsapp_flows(meta_flow_id);
CREATE INDEX IF NOT EXISTS ix_whatsapp_flows_name ON whatsapp_flows(name);

-- Comment on table
COMMENT ON TABLE whatsapp_flows IS 'WhatsApp Flows for interactive data collection';
COMMENT ON COLUMN whatsapp_flows.flow_json IS 'Full WhatsApp Flow JSON structure';
COMMENT ON COLUMN whatsapp_flows.entry_screen_id IS 'ID of the first screen to show';
COMMENT ON COLUMN whatsapp_flows.meta_flow_id IS 'Flow ID from Meta after publishing';
