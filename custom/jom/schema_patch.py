"""
Idempotent schema for the JOM vertical (mirrors agent_auth/schema_patch.py).
All new tables — nothing existing is altered. Safe + idempotent (CREATE TABLE IF
NOT EXISTS). Never raises fatally; a failure is logged and the app continues.
"""
import logging
from sqlalchemy import text

logger = logging.getLogger(__name__)

_STATEMENTS = [
    # Destination departments (dynamic, seasonal on/off) --------------------
    """CREATE TABLE IF NOT EXISTS jom_departments (
        id            SERIAL PRIMARY KEY,
        workspace_id  INTEGER NOT NULL REFERENCES workspaces2(id) ON DELETE CASCADE,
        name          VARCHAR(80) NOT NULL,
        match_keywords JSONB NOT NULL DEFAULT '[]'::jsonb,
        routing_mode  VARCHAR(16) NOT NULL DEFAULT 'round_robin',
        is_active     BOOLEAN NOT NULL DEFAULT TRUE,
        sort_order    INTEGER NOT NULL DEFAULT 0,
        last_agent_id INTEGER,
        created_at    TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at    TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
        CONSTRAINT uq_jom_dept_ws_name UNIQUE (workspace_id, name)
    )""",
    "CREATE INDEX IF NOT EXISTS ix_jom_departments_ws ON jom_departments(workspace_id)",

    # Which agents staff which department ----------------------------------
    """CREATE TABLE IF NOT EXISTS jom_department_agents (
        id            SERIAL PRIMARY KEY,
        department_id INTEGER NOT NULL REFERENCES jom_departments(id) ON DELETE CASCADE,
        agent_id      INTEGER NOT NULL REFERENCES workspace_agents(id) ON DELETE CASCADE,
        created_at    TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
        CONSTRAINT uq_jom_dept_agent UNIQUE (department_id, agent_id)
    )""",
    "CREATE INDEX IF NOT EXISTS ix_jom_dept_agents_dept ON jom_department_agents(department_id)",
    "CREATE INDEX IF NOT EXISTS ix_jom_dept_agents_agent ON jom_department_agents(agent_id)",

    # Per-lead (per customer phone) lifecycle state ------------------------
    """CREATE TABLE IF NOT EXISTS jom_lifecycle_state (
        id                SERIAL PRIMARY KEY,
        workspace_id      INTEGER NOT NULL REFERENCES workspaces2(id) ON DELETE CASCADE,
        customer_phone    VARCHAR(32) NOT NULL,
        department_id     INTEGER REFERENCES jom_departments(id) ON DELETE SET NULL,
        status            VARCHAR(24) NOT NULL DEFAULT 'new',
        assigned_agent_id INTEGER REFERENCES workspace_agents(id) ON DELETE SET NULL,
        drip_stage        VARCHAR(16),
        last_inbound_at   TIMESTAMP WITH TIME ZONE,
        last_outbound_at  TIMESTAMP WITH TIME ZONE,
        created_at        TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at        TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
        CONSTRAINT uq_jom_lifecycle_ws_phone UNIQUE (workspace_id, customer_phone)
    )""",
    "CREATE INDEX IF NOT EXISTS ix_jom_lifecycle_ws ON jom_lifecycle_state(workspace_id)",
    "CREATE INDEX IF NOT EXISTS ix_jom_lifecycle_status ON jom_lifecycle_state(status)",
    # Phase 3 scheduling columns (additive)
    "ALTER TABLE jom_lifecycle_state ADD COLUMN IF NOT EXISTS status_since TIMESTAMP WITH TIME ZONE",
    "ALTER TABLE jom_lifecycle_state ADD COLUMN IF NOT EXISTS next_followup_at TIMESTAMP WITH TIME ZONE",
    "CREATE INDEX IF NOT EXISTS ix_jom_lifecycle_next_followup ON jom_lifecycle_state(next_followup_at)",
    # Phase 4/5 columns (additive)
    "ALTER TABLE jom_lifecycle_state ADD COLUMN IF NOT EXISTS meta_lead_id VARCHAR(40)",
    "ALTER TABLE jom_lifecycle_state ADD COLUMN IF NOT EXISTS email VARCHAR(160)",
    "ALTER TABLE jom_lifecycle_state ADD COLUMN IF NOT EXISTS reopen_count INTEGER NOT NULL DEFAULT 0",

    # Behaviour / engagement score per lead --------------------------------
    """CREATE TABLE IF NOT EXISTS jom_behavior_score (
        id                  SERIAL PRIMARY KEY,
        workspace_id        INTEGER NOT NULL REFERENCES workspaces2(id) ON DELETE CASCADE,
        customer_phone      VARCHAR(32) NOT NULL,
        engagement_score    INTEGER NOT NULL DEFAULT 0,
        consecutive_no_reply INTEGER NOT NULL DEFAULT 0,
        total_inbound       INTEGER NOT NULL DEFAULT 0,
        total_outbound      INTEGER NOT NULL DEFAULT 0,
        last_intent         VARCHAR(32),
        last_confidence     REAL,
        updated_at          TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
        CONSTRAINT uq_jom_behavior_ws_phone UNIQUE (workspace_id, customer_phone)
    )""",
    "CREATE INDEX IF NOT EXISTS ix_jom_behavior_ws ON jom_behavior_score(workspace_id)",
]


def ensure_jom_schema(engine) -> None:
    """Apply idempotent CREATE statements. No-op if tables already exist. Never fatal."""
    try:
        with engine.begin() as conn:
            for stmt in _STATEMENTS:
                conn.execute(text(stmt))
        logger.info("[jom] schema ensured (%d statements)", len(_STATEMENTS))
    except Exception as exc:  # noqa: BLE001
        logger.warning("[jom] schema ensure failed (non-fatal): %s", exc)
