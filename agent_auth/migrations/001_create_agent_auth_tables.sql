-- =====================================================
-- Migration: 001_create_agent_auth_tables.sql
-- Module: agent_auth (workspace sub-logins)
-- Idempotent. db.create_all() also builds these on a fresh DB; this file is for
-- applying to an already-provisioned (prod) database.
-- Run: psql -d <db> -f 001_create_agent_auth_tables.sql
-- =====================================================

-- Agent accounts (restricted sub-logins owned by a users row)
CREATE TABLE IF NOT EXISTS workspace_agents (
    id              SERIAL PRIMARY KEY,
    owner_user_id   INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    tenant_id       INTEGER,
    username        VARCHAR(100) NOT NULL,
    password_hash   VARCHAR(256) NOT NULL,
    display_name    VARCHAR(255),
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    allowed_pages   JSONB NOT NULL DEFAULT '[]'::jsonb,
    allowed_paths   JSONB NOT NULL DEFAULT '[]'::jsonb,
    last_login_at   TIMESTAMP WITH TIME ZONE,
    login_count     INTEGER NOT NULL DEFAULT 0,
    created_at      TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_workspace_agent_owner_username UNIQUE (owner_user_id, username)
);
CREATE INDEX IF NOT EXISTS ix_workspace_agents_owner_user_id ON workspace_agents(owner_user_id);
CREATE INDEX IF NOT EXISTS ix_workspace_agents_tenant_id ON workspace_agents(tenant_id);

-- Which workspaces an agent may access (Level 2)
CREATE TABLE IF NOT EXISTS agent_workspaces (
    id            SERIAL PRIMARY KEY,
    agent_id      INTEGER NOT NULL REFERENCES workspace_agents(id) ON DELETE CASCADE,
    workspace_id  INTEGER NOT NULL REFERENCES workspaces2(id) ON DELETE CASCADE,
    created_at    TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_agent_workspace UNIQUE (agent_id, workspace_id)
);
CREATE INDEX IF NOT EXISTS ix_agent_workspaces_agent_id ON agent_workspaces(agent_id);
CREATE INDEX IF NOT EXISTS ix_agent_workspaces_workspace_id ON agent_workspaces(workspace_id);

-- Audit trail
CREATE TABLE IF NOT EXISTS agent_auth_audit_logs (
    id             SERIAL PRIMARY KEY,
    agent_id       INTEGER,
    owner_user_id  INTEGER,
    action         VARCHAR(64) NOT NULL,
    resource       VARCHAR(255),
    meta           JSONB,
    ip_address     VARCHAR(45),
    user_agent     VARCHAR(512),
    created_at     TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS ix_agent_auth_audit_logs_agent_id ON agent_auth_audit_logs(agent_id);
CREATE INDEX IF NOT EXISTS ix_agent_auth_audit_logs_owner_user_id ON agent_auth_audit_logs(owner_user_id);
CREATE INDEX IF NOT EXISTS ix_agent_auth_audit_logs_created_at ON agent_auth_audit_logs(created_at DESC);

-- =====================================================
-- ROLLBACK
-- DROP TABLE IF EXISTS agent_auth_audit_logs;
-- DROP TABLE IF EXISTS agent_workspaces;
-- DROP TABLE IF EXISTS workspace_agents;
-- =====================================================
