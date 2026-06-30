"""
Apply missing expand-only DDL for whatsapp_accounts at process startup.

Cloud Run / staging often points at a DB where manual migration 010+011 was never run,
which causes ORM queries (e.g. connection-path) to 500 with UndefinedColumn.

Behavior:
  - PostgreSQL only; no-op for SQLite and other dialects.
  - Default ON whenever the DB is missing patched columns (set WHATSAPP_AUTO_SCHEMA_PATCH=0 to disable).
  - WHATSAPP_AUTO_SCHEMA_PATCH=1 is the same as unset for PostgreSQL (explicit enable).

Uses pg_try_advisory_lock so concurrent Gunicorn workers / Cloud Run revisions serialize DDL.

Important: SQLAlchemy 2 connections begin a transaction implicitly; DDL must be followed by
commit() or it is rolled back when the connection is returned to the pool (this was the
root cause of "patch ran" but columns still missing).
"""
from __future__ import annotations

import logging
import os
import re
import sys
import time
from pathlib import Path
from typing import List

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from core.deployment_safety import is_non_dev_environment

logger = logging.getLogger(__name__)

_ADVISORY_LOCK_KEY = 942_001_001

_SERVICE_ROOT = Path(__file__).resolve().parents[1]
_MIGRATIONS_DIR = _SERVICE_ROOT / "whatsapp" / "migrations"
_MIGRATION_010 = _MIGRATIONS_DIR / "010_whatsapp_accounts_safe_mode_operational_expand.sql"
_MIGRATION_011 = _MIGRATIONS_DIR / "011_whatsapp_accounts_last_error_code_type_normalize.sql"
_MIGRATION_012 = _MIGRATIONS_DIR / "012_whatsapp_operational_logs.sql"


def _auto_schema_patch_allowed() -> bool:
    raw = (os.getenv("WHATSAPP_AUTO_SCHEMA_PATCH") or "").strip().lower()
    if raw in ("0", "false", "no", "off"):
        return False
    return True


def _table_exists(conn: Connection) -> bool:
    row = conn.execute(
        text(
            """
            SELECT 1
            FROM information_schema.tables
            WHERE table_schema = 'public'
              AND table_name = 'whatsapp_accounts'
            """
        )
    ).fetchone()
    return row is not None


def _column_missing(conn: Connection, column: str) -> bool:
    if not _table_exists(conn):
        return False
    row = conn.execute(
        text(
            """
            SELECT 1
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = 'whatsapp_accounts'
              AND column_name = :col
            """
        ),
        {"col": column},
    ).fetchone()
    return row is None


def _last_error_code_is_character_type(conn: Connection) -> bool:
    if not _table_exists(conn):
        return False
    row = conn.execute(
        text(
            """
            SELECT data_type
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = 'whatsapp_accounts'
              AND column_name = 'last_error_code'
            """
        )
    ).fetchone()
    if not row:
        return False
    dt = (row[0] or "").lower()
    return dt in ("character varying", "text", "character")


def _load_010_statements() -> List[str]:
    if not _MIGRATION_010.is_file():
        raise FileNotFoundError(f"Missing migration file: {_MIGRATION_010}")
    content = _MIGRATION_010.read_text(encoding="utf-8")
    found = re.findall(
        r"ALTER\s+TABLE\s+whatsapp_accounts\s+ADD\s+COLUMN\s+IF\s+NOT\s+EXISTS[^;]+;",
        content,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if len(found) < 4:
        raise ValueError(
            f"Expected 4 ALTER statements in {_MIGRATION_010.name}, found {len(found)}"
        )
    return found


def _load_011_sql() -> str:
    if not _MIGRATION_011.is_file():
        raise FileNotFoundError(f"Missing migration file: {_MIGRATION_011}")
    raw = _MIGRATION_011.read_text(encoding="utf-8")
    lines = [ln for ln in raw.splitlines() if not ln.strip().startswith("--")]
    sql = "\n".join(lines).strip()
    if not sql:
        raise ValueError(f"Empty migration after stripping comments: {_MIGRATION_011.name}")
    return sql


def _operational_logs_table_missing(conn: Connection) -> bool:
    row = conn.execute(
        text(
            """
            SELECT 1
            FROM information_schema.tables
            WHERE table_schema = 'public'
              AND table_name = 'whatsapp_operational_logs'
            """
        )
    ).fetchone()
    return row is None


def _load_012_statements() -> List[str]:
    if not _MIGRATION_012.is_file():
        raise FileNotFoundError(f"Missing migration file: {_MIGRATION_012}")
    raw = _MIGRATION_012.read_text(encoding="utf-8")
    lines = [ln for ln in raw.splitlines() if not ln.strip().startswith("--")]
    sql = "\n".join(lines).strip()
    if not sql:
        raise ValueError(f"Empty migration after stripping comments: {_MIGRATION_012.name}")
    parts: List[str] = []
    for chunk in sql.split(";"):
        c = chunk.strip()
        if c:
            parts.append(c + ";")
    if not parts:
        raise ValueError(f"No SQL statements in {_MIGRATION_012.name}")
    return parts


def _drip_step_scheduled_at_missing(conn: Connection) -> bool:
    row = conn.execute(
        text(
            """
            SELECT 1
            FROM information_schema.tables
            WHERE table_schema = 'public'
              AND table_name = 'whatsapp_drip_steps'
            """
        )
    ).fetchone()
    if not row:
        return False
    
    col_row = conn.execute(
        text(
            """
            SELECT 1
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = 'whatsapp_drip_steps'
              AND column_name = 'scheduled_at'
            """
        )
    ).fetchone()
    return col_row is None


_CASCADE_CONSTRAINTS = [
    # (table, column, ref_table, constraint_name)
    ("whatsapp_templates", "account_id", "whatsapp_accounts", "whatsapp_templates_account_id_fkey"),
    ("whatsapp_flows", "account_id", "whatsapp_accounts", "whatsapp_flows_account_id_fkey"),
    ("whatsapp_conversations", "account_id", "whatsapp_accounts", "whatsapp_conversations_account_id_fkey"),
    ("whatsapp_triggers", "account_id", "whatsapp_accounts", "whatsapp_triggers_account_id_fkey"),
    ("whatsapp_faqs", "account_id", "whatsapp_accounts", "whatsapp_faqs_account_id_fkey"),
    ("whatsapp_drip_campaigns", "account_id", "whatsapp_accounts", "whatsapp_drip_campaigns_account_id_fkey"),
    ("whatsapp_automation_rules", "account_id", "whatsapp_accounts", "whatsapp_automation_rules_account_id_fkey"),
    ("whatsapp_business_hours", "account_id", "whatsapp_accounts", "whatsapp_business_hours_account_id_fkey"),
    ("contact_automation_overrides", "account_id", "whatsapp_accounts", "contact_automation_overrides_account_id_fkey"),
    ("whatsapp_visual_automations", "account_id", "whatsapp_accounts", "whatsapp_visual_automations_account_id_fkey"),
    ("whatsapp_usage_events", "account_id", "whatsapp_accounts", "whatsapp_usage_events_account_id_fkey"),
    ("whatsapp_usage_events", "message_id", "whatsapp_messages", "whatsapp_usage_events_message_id_fkey"),
    ("whatsapp_drip_steps", "campaign_id", "whatsapp_drip_campaigns", "whatsapp_drip_steps_campaign_id_fkey"),
    ("whatsapp_drip_enrollments", "campaign_id", "whatsapp_drip_campaigns", "whatsapp_drip_enrollments_campaign_id_fkey"),
    ("whatsapp_automation_logs", "rule_id", "whatsapp_automation_rules", "whatsapp_automation_logs_rule_id_fkey"),
    ("whatsapp_automation_logs", "conversation_id", "whatsapp_conversations", "whatsapp_automation_logs_conversation_id_fkey"),
    ("whatsapp_automation_nodes", "automation_id", "whatsapp_visual_automations", "whatsapp_automation_nodes_automation_id_fkey"),
    ("whatsapp_trigger_logs", "trigger_id", "whatsapp_triggers", "whatsapp_trigger_logs_trigger_id_fkey"),
    ("whatsapp_trigger_logs", "account_id", "whatsapp_accounts", "whatsapp_trigger_logs_account_id_fkey"),
    ("whatsapp_credentials", "account_id", "whatsapp_accounts", "whatsapp_credentials_account_id_fkey"),
    ("whatsapp_bot_settings", "account_id", "whatsapp_accounts", "whatsapp_bot_settings_account_id_fkey"),
    ("whatsapp_account_status", "account_id", "whatsapp_accounts", "whatsapp_account_status_account_id_fkey"),
    ("whatsapp_account_capabilities", "account_id", "whatsapp_accounts", "whatsapp_account_capabilities_account_id_fkey"),
    ("trust_snapshots", "account_id", "whatsapp_accounts", "trust_snapshots_account_id_fkey"),
    ("whatsapp_phone_reputation_snapshots", "account_id", "whatsapp_accounts", "whatsapp_phone_reputation_snapshots_account_id_fkey"),
    ("whatsapp_operational_logs", "account_id", "whatsapp_accounts", "whatsapp_operational_logs_account_id_fkey"),
]

_SET_NULL_CONSTRAINTS = [
    # (table, column, ref_table, constraint_name)
    ("whatsapp_flows", "parent_flow_id", "whatsapp_flows", "whatsapp_flows_parent_flow_id_fkey"),
    ("whatsapp_conversation_states", "automation_id", "whatsapp_visual_automations", "whatsapp_conversation_states_automation_id_fkey"),
    ("onboarding_sessions", "account_id", "whatsapp_accounts", "onboarding_sessions_account_id_fkey"),
]


def _check_table_exists(conn: Connection, table: str) -> bool:
    row = conn.execute(
        text(
            """
            SELECT 1
            FROM information_schema.tables
            WHERE table_schema = 'public'
              AND table_name = :t
            """
        ),
        {"t": table}
    ).fetchone()
    return row is not None


def _get_foreign_keys_on_column(conn: Connection, table: str, column: str) -> List[tuple[str, str]]:
    try:
        rows = conn.execute(
            text(
                """
                SELECT c.conname, c.confdeltype
                FROM pg_constraint c
                JOIN pg_class t ON c.conrelid = t.oid
                JOIN pg_namespace n ON t.relnamespace = n.oid
                JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = ANY(c.conkey)
                WHERE t.relname = :table
                  AND n.nspname = 'public'
                  AND c.contype = 'f'
                  AND a.attname = :column
                """
            ),
            {"table": table, "column": column}
        ).fetchall()
        return [(r[0], r[1]) for r in rows]
    except Exception as e:
        logger.warning("Error fetching foreign keys for %s.%s: %s", table, column, e)
        return []


def _fk_cascade_missing(conn: Connection) -> bool:
    # 1. Check CASCADE constraints
    for table, col, ref_table, _ in _CASCADE_CONSTRAINTS:
        if not _check_table_exists(conn, table) or not _check_table_exists(conn, ref_table):
            continue
        fks = _get_foreign_keys_on_column(conn, table, col)
        if not fks or any(del_type != "c" for _, del_type in fks):
            return True
            
    # 2. Check SET NULL constraints
    for table, col, ref_table, _ in _SET_NULL_CONSTRAINTS:
        if not _check_table_exists(conn, table) or not _check_table_exists(conn, ref_table):
            continue
        fks = _get_foreign_keys_on_column(conn, table, col)
        if not fks or any(del_type != "n" for _, del_type in fks):
            return True
            
    return False


def _patch_fk_cascades(conn: Connection) -> None:
    # 1. CASCADE constraints
    for table, col, ref_table, standard_name in _CASCADE_CONSTRAINTS:
        if not _check_table_exists(conn, table) or not _check_table_exists(conn, ref_table):
            continue
            
        fks = _get_foreign_keys_on_column(conn, table, col)
        needs_recreate = True
        
        # If there are existing FKs on this column, check if it's already cascade
        for name, del_type in fks:
            if del_type == "c":
                # Already CASCADE, no need to recreate
                needs_recreate = False
            else:
                # Drop the incorrect constraint
                logger.warning("WHATSAPP_SCHEMA_PATCH: dropping constraint %s on %s.%s", name, table, col)
                conn.execute(text(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {name};"))
                
        if needs_recreate:
            logger.warning("WHATSAPP_SCHEMA_PATCH: creating CASCADE constraint %s on %s.%s -> %s(id)", standard_name, table, col, ref_table)
            conn.execute(text(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {standard_name};"))
            conn.execute(text(f"ALTER TABLE {table} ADD CONSTRAINT {standard_name} FOREIGN KEY ({col}) REFERENCES {ref_table}(id) ON DELETE CASCADE;"))

    # 2. SET NULL constraints
    for table, col, ref_table, standard_name in _SET_NULL_CONSTRAINTS:
        if not _check_table_exists(conn, table) or not _check_table_exists(conn, ref_table):
            continue
            
        fks = _get_foreign_keys_on_column(conn, table, col)
        needs_recreate = True
        
        # If there are existing FKs on this column, check if it's already SET NULL
        for name, del_type in fks:
            if del_type == "n":
                # Already SET NULL, no need to recreate
                needs_recreate = False
            else:
                # Drop the incorrect constraint
                logger.warning("WHATSAPP_SCHEMA_PATCH: dropping constraint %s on %s.%s", name, table, col)
                conn.execute(text(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {name};"))
                
        if needs_recreate:
            logger.warning("WHATSAPP_SCHEMA_PATCH: creating SET NULL constraint %s on %s.%s -> %s(id)", standard_name, table, col, ref_table)
            conn.execute(text(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {standard_name};"))
            conn.execute(text(f"ALTER TABLE {table} ADD CONSTRAINT {standard_name} FOREIGN KEY ({col}) REFERENCES {ref_table}(id) ON DELETE SET NULL;"))


def _needs_patch(conn: Connection) -> bool:
    return (
        _column_missing(conn, "safe_mode_reason_code")
        or _last_error_code_is_character_type(conn)
        or _operational_logs_table_missing(conn)
        or _drip_step_scheduled_at_missing(conn)
        or _fk_cascade_missing(conn)
    )


def _unlock_and_commit(conn: Connection) -> None:
    conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": _ADVISORY_LOCK_KEY})
    conn.commit()


def ensure_whatsapp_accounts_orm_columns(engine: Engine) -> None:
    if engine.dialect.name != "postgresql":
        return
    if not _auto_schema_patch_allowed():
        logger.info("WHATSAPP_SCHEMA_PATCH disabled (WHATSAPP_AUTO_SCHEMA_PATCH=0)")
        return

    with engine.connect() as probe:
        if not _needs_patch(probe):
            return

    deadline = time.monotonic() + 120.0
    while time.monotonic() < deadline:
        with engine.connect() as conn:
            if not _needs_patch(conn):
                conn.rollback()
                logger.info("WHATSAPP_SCHEMA_PATCH skipped (already applied by peer)")
                return
            got = conn.execute(
                text("SELECT pg_try_advisory_lock(:k)"), {"k": _ADVISORY_LOCK_KEY}
            ).scalar()
            if not got:
                conn.rollback()
                time.sleep(0.25)
                continue
            try:
                if _column_missing(conn, "safe_mode_reason_code"):
                    logger.warning(
                        "WHATSAPP_SCHEMA_PATCH applying %s (missing ORM columns on whatsapp_accounts)",
                        _MIGRATION_010.name,
                    )
                    for stmt in _load_010_statements():
                        conn.execute(text(stmt))
                if _last_error_code_is_character_type(conn):
                    logger.warning(
                        "WHATSAPP_SCHEMA_PATCH applying %s (last_error_code still character type)",
                        _MIGRATION_011.name,
                    )
                    conn.execute(text(_load_011_sql()))
                if _operational_logs_table_missing(conn):
                    logger.warning(
                        "WHATSAPP_SCHEMA_PATCH applying %s (whatsapp_operational_logs missing)",
                        _MIGRATION_012.name,
                    )
                    for stmt in _load_012_statements():
                        conn.execute(text(stmt))
                if _drip_step_scheduled_at_missing(conn):
                    logger.warning(
                        "WHATSAPP_SCHEMA_PATCH adding scheduled_at column to whatsapp_drip_steps"
                    )
                    conn.execute(text("ALTER TABLE whatsapp_drip_steps ADD COLUMN IF NOT EXISTS scheduled_at TIMESTAMP WITH TIME ZONE;"))
                
                # Apply Foreign Key Cascade patches
                if _fk_cascade_missing(conn):
                    _patch_fk_cascades(conn)
                    
                _unlock_and_commit(conn)
                logger.info("WHATSAPP_SCHEMA_PATCH completed successfully")
            except Exception:
                conn.rollback()
                logger.exception("WHATSAPP_SCHEMA_PATCH failed")
                try:
                    _unlock_and_commit(conn)
                except Exception:
                    logger.exception("WHATSAPP_SCHEMA_PATCH unlock after failure also failed")
                if is_non_dev_environment():
                    sys.exit(1)
                raise
            return

    with engine.connect() as probe:
        if not _needs_patch(probe):
            logger.info("WHATSAPP_SCHEMA_PATCH resolved during lock wait (peer applied)")
            return

    logger.error(
        "WHATSAPP_SCHEMA_PATCH timed out waiting for pg_advisory_lock(%s); "
        "another instance may be stuck, or apply migrations manually.",
        _ADVISORY_LOCK_KEY,
    )
    if is_non_dev_environment():
        sys.exit(1)


def ensure_whatsapp_orm_columns_full(engine: Engine) -> None:
    """Generic expand-only schema sync for EVERY ``whatsapp_*`` table.

    The targeted migrations above (010/011/012, FK cascades) only cover a fixed set of
    columns. The ORM models have since grown more columns (notification_email, warmup_config,
    trust_score, ...) that the .sql migrations never added, so an existing prod DB stays
    under-migrated and ORM SELECTs 500 with UndefinedColumn.

    This compares each whatsapp_* table's ORM columns against the live DB and ``ALTER TABLE
    ... ADD COLUMN IF NOT EXISTS`` for any that are missing, always NULLABLE (safe on tables
    that already have rows). Idempotent + concurrency-safe via IF NOT EXISTS; new tables are
    left to create_all(). Scoped to whatsapp_* so tenant/CRM/core tables are never touched.
    """
    if engine.dialect.name != "postgresql":
        return
    if not _auto_schema_patch_allowed():
        return
    try:
        from sqlalchemy import inspect as _sa_inspect
        from models import db as _db
    except Exception:
        logger.exception("WHATSAPP_ORM_COLUMN_SYNC: could not import db/inspect")
        return

    insp = _sa_inspect(engine)
    added = 0
    for table_name, table in _db.metadata.tables.items():
        if not table_name.startswith("whatsapp"):
            continue
        try:
            if not insp.has_table(table_name):
                continue  # brand-new table -> create_all() builds it fully
            existing = {c["name"] for c in insp.get_columns(table_name)}
        except Exception:
            continue
        for col in table.columns:
            if col.name in existing:
                continue
            try:
                coltype = col.type.compile(dialect=engine.dialect)
            except Exception:
                coltype = "TEXT"
            ddl = f'ALTER TABLE "{table_name}" ADD COLUMN IF NOT EXISTS "{col.name}" {coltype}'
            try:
                with engine.begin() as conn:
                    conn.execute(text(ddl))
                added += 1
                logger.info("WHATSAPP_ORM_COLUMN_SYNC added %s.%s (%s)", table_name, col.name, coltype)
            except Exception as exc:
                logger.warning("WHATSAPP_ORM_COLUMN_SYNC skip %s.%s: %s", table_name, col.name, exc)

    if added:
        logger.warning(
            "WHATSAPP_ORM_COLUMN_SYNC: added %s missing column(s) to existing whatsapp_* tables", added
        )
    else:
        logger.info("WHATSAPP_ORM_COLUMN_SYNC: no missing columns")
