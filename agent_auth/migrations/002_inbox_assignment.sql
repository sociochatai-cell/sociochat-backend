-- =====================================================
-- Migration: 002_inbox_assignment.sql
-- Module: agent_auth — Level 3 inbox/number scoping (Phases 2 & 3)
-- Idempotent. db.create_all() builds the new tables on a fresh DB; the ALTERs
-- cover an already-provisioned DB where agent_workspaces exists from 001.
-- Run: psql -d <db> -f 002_inbox_assignment.sql
-- =====================================================

-- Per-workspace inbox scope + rotation participation on the Level-2 grant
ALTER TABLE agent_workspaces ADD COLUMN IF NOT EXISTS inbox_scope VARCHAR(16) NOT NULL DEFAULT 'all';
ALTER TABLE agent_workspaces ADD COLUMN IF NOT EXISTS auto_assign BOOLEAN NOT NULL DEFAULT FALSE;

-- Customer-number -> agent assignments (advance / manual / auto)
CREATE TABLE IF NOT EXISTS agent_number_assignments (
    id                   SERIAL PRIMARY KEY,
    agent_id             INTEGER NOT NULL REFERENCES workspace_agents(id) ON DELETE CASCADE,
    workspace_id         INTEGER NOT NULL REFERENCES workspaces2(id) ON DELETE CASCADE,
    customer_phone       VARCHAR(32) NOT NULL,
    assignment_type      VARCHAR(16) NOT NULL DEFAULT 'manual',
    assigned_by_user_id  INTEGER,
    created_at           TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_agent_number_ws_phone UNIQUE (workspace_id, customer_phone)
);
CREATE INDEX IF NOT EXISTS ix_agent_number_assignments_agent_id ON agent_number_assignments(agent_id);
CREATE INDEX IF NOT EXISTS ix_agent_number_assignments_workspace_id ON agent_number_assignments(workspace_id);
CREATE INDEX IF NOT EXISTS ix_agent_number_assignments_customer_phone ON agent_number_assignments(customer_phone);

-- Per-workspace round-robin switch + rotation pointer
CREATE TABLE IF NOT EXISTS workspace_autoassign_state (
    workspace_id  INTEGER PRIMARY KEY REFERENCES workspaces2(id) ON DELETE CASCADE,
    enabled       BOOLEAN NOT NULL DEFAULT FALSE,
    last_agent_id INTEGER,
    updated_at    TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- =====================================================
-- ROLLBACK
-- DROP TABLE IF EXISTS workspace_autoassign_state;
-- DROP TABLE IF EXISTS agent_number_assignments;
-- ALTER TABLE agent_workspaces DROP COLUMN IF EXISTS auto_assign;
-- ALTER TABLE agent_workspaces DROP COLUMN IF EXISTS inbox_scope;
-- =====================================================
