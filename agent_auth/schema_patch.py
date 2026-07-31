# agent_auth/schema_patch.py
"""
Idempotent schema self-heal for the agent_auth tables.

`db.create_all()` creates missing TABLES but never ALTERs existing ones. When
the agent tables were first created (Phase 1) they lacked the Level-3 columns
(`inbox_scope`, `auto_assign`) and the assignment tables added later. This patch
adds anything missing on an already-provisioned DB. Safe + idempotent (ADD
COLUMN IF NOT EXISTS / CREATE TABLE IF NOT EXISTS) and a no-op on a fresh DB
where create_all already built the full schema. Never raises fatally — a failure
is logged and the app continues.
"""

import logging
from sqlalchemy import text

logger = logging.getLogger(__name__)

_STATEMENTS = [
    # Level-3 columns on the workspace grant (added after the table first shipped)
    "ALTER TABLE agent_workspaces ADD COLUMN IF NOT EXISTS inbox_scope VARCHAR(16) NOT NULL DEFAULT 'all'",
    "ALTER TABLE agent_workspaces ADD COLUMN IF NOT EXISTS auto_assign BOOLEAN NOT NULL DEFAULT FALSE",

    # Customer-number -> agent assignments
    """CREATE TABLE IF NOT EXISTS agent_number_assignments (
        id                  SERIAL PRIMARY KEY,
        agent_id            INTEGER NOT NULL REFERENCES workspace_agents(id) ON DELETE CASCADE,
        workspace_id        INTEGER NOT NULL REFERENCES workspaces2(id) ON DELETE CASCADE,
        customer_phone      VARCHAR(32) NOT NULL,
        assignment_type     VARCHAR(16) NOT NULL DEFAULT 'manual',
        assigned_by_user_id INTEGER,
        created_at          TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
        CONSTRAINT uq_agent_number_ws_phone UNIQUE (workspace_id, customer_phone)
    )""",
    "CREATE INDEX IF NOT EXISTS ix_agent_number_assignments_agent_id ON agent_number_assignments(agent_id)",
    "CREATE INDEX IF NOT EXISTS ix_agent_number_assignments_workspace_id ON agent_number_assignments(workspace_id)",
    "CREATE INDEX IF NOT EXISTS ix_agent_number_assignments_customer_phone ON agent_number_assignments(customer_phone)",

    # Per-workspace round-robin switch + rotation pointer
    """CREATE TABLE IF NOT EXISTS workspace_autoassign_state (
        workspace_id  INTEGER PRIMARY KEY REFERENCES workspaces2(id) ON DELETE CASCADE,
        enabled       BOOLEAN NOT NULL DEFAULT FALSE,
        last_agent_id INTEGER,
        updated_at    TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP
    )""",

    # Usernames became GLOBALLY unique (login by username + password, no Account
    # ID). Drop the old per-owner constraint and add a global unique index.
    "ALTER TABLE workspace_agents DROP CONSTRAINT IF EXISTS uq_workspace_agent_owner_username",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_workspace_agent_username ON workspace_agents (username)",
]


def ensure_agent_auth_schema(engine) -> None:
    """Apply idempotent ALTER/CREATE statements so an existing DB has the full
    agent_auth schema. Requires the base tables (workspace_agents,
    agent_workspaces) to already exist — db.create_all() builds those first."""
    try:
        with engine.begin() as conn:
            for stmt in _STATEMENTS:
                conn.execute(text(stmt))
        logger.info("agent_auth schema patch ensured")
    except Exception as e:
        logger.warning("agent_auth schema patch skipped: %s", e)
