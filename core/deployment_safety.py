"""
Deployment-time checks: secrets, schema presence, strict-mode vs projection coverage.

Used at API startup only; does not change request-path architecture.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List, Optional, Set, Tuple

from sqlalchemy import text
from sqlalchemy.engine import Engine

from core.config import env_flag

logger = logging.getLogger(__name__)

# Migration 001–006: canonical new tables (expand-only migrations).
_EXPECTED_TABLES: Tuple[str, ...] = (
    "whatsapp_credentials",
    "whatsapp_bot_settings",
    "whatsapp_account_status",
    "whatsapp_account_capabilities",
    "whatsapp_usage_events",
    "whatsapp_usage_event_checkpoints",
    "whatsapp_usage_events_processed",
    "whatsapp_usage_events_quarantine",
)

_DEFAULT_INSECURE_SCHEDULER_TOKEN = "default-insecure-token-please-change-me"


def is_non_dev_environment() -> bool:
    """
    Treat as non-dev when FLASK_ENV/ENV indicate staging or production,
    or when WHATSAPP_DEPLOYMENT_ENV is set to staging|production|prod|stage.

    Explicit development: FLASK_ENV=development or ENV=development|local.
    """
    if env_flag("WHATSAPP_FORCE_DEV", False):
        return False
    # Cloud Run / similar: service name is set at runtime.
    if (os.getenv("K_SERVICE") or "").strip():
        return True
    fe = (os.getenv("FLASK_ENV") or "").strip().lower()
    if fe == "development":
        return False
    env = (os.getenv("ENV") or "").strip().lower()
    if env in ("development", "dev", "local"):
        return False
    deploy = (os.getenv("WHATSAPP_DEPLOYMENT_ENV") or "").strip().lower()
    if deploy in ("staging", "production", "prod", "stage"):
        return True
    if fe in ("production", "staging", "prod", "stage"):
        return True
    if env in ("production", "staging", "prod", "stage"):
        return True
    return False


def capabilities_upsert_secret_configured() -> bool:
    return bool((os.getenv("WHATSAPP_CAPABILITIES_UPSERT_SECRET") or "").strip())


def usage_events_read_secret_configured() -> bool:
    cap = (os.getenv("WHATSAPP_CAPABILITIES_UPSERT_SECRET") or "").strip()
    usage = (os.getenv("WHATSAPP_USAGE_EVENTS_SECRET") or "").strip()
    return bool(usage or cap)


def scheduler_secret_acceptable_for_environment() -> Tuple[bool, str]:
    raw = (os.getenv("SCHEDULER_SECRET_TOKEN") or "").strip()
    if not is_non_dev_environment():
        if not raw or raw == _DEFAULT_INSECURE_SCHEDULER_TOKEN:
            return True, "scheduler_using_default_token_dev_ok"
        return True, "scheduler_secret_set"
    if not raw:
        return False, "SCHEDULER_SECRET_TOKEN_unset_in_non_dev"
    if raw == _DEFAULT_INSECURE_SCHEDULER_TOKEN:
        return False, "SCHEDULER_SECRET_TOKEN_is_default_placeholder_in_non_dev"
    return True, "scheduler_secret_ok"


def whatsapp_capabilities_strict_enabled() -> bool:
    return env_flag("WHATSAPP_CAPABILITIES_STRICT", False)


def _existing_tables(engine: Engine) -> Set[str]:
    sql = text(
        """
        SELECT table_name
        FROM information_schema.tables
        WHERE table_schema = 'public'
          AND table_type = 'BASE TABLE'
        """
    )
    with engine.connect() as conn:
        rows = conn.execute(sql).fetchall()
    return {r[0] for r in rows if r and r[0]}


def migration_schema_status(engine: Engine) -> Tuple[List[str], List[str]]:
    """
    Returns (present_tables, missing_tables) among _EXPECTED_TABLES.
    """
    existing = _existing_tables(engine)
    present = [t for t in _EXPECTED_TABLES if t in existing]
    missing = [t for t in _EXPECTED_TABLES if t not in existing]
    return present, missing


def count_accounts_missing_capabilities(engine: Engine) -> Optional[int]:
    """None if capabilities table missing."""
    existing = _existing_tables(engine)
    if "whatsapp_account_capabilities" not in existing or "whatsapp_accounts" not in existing:
        return None
    sql = text(
        """
        SELECT COUNT(*)
        FROM whatsapp_accounts wa
        LEFT JOIN whatsapp_account_capabilities c ON wa.id = c.account_id
        WHERE c.account_id IS NULL
        """
    )
    with engine.connect() as conn:
        n = conn.execute(sql).scalar()
    return int(n or 0)


def count_stale_capabilities(engine: Engine, stale_hours: int = 24) -> Optional[int]:
    existing = _existing_tables(engine)
    if "whatsapp_account_capabilities" not in existing:
        return None
    sql = text(
        """
        SELECT COUNT(*)
        FROM whatsapp_account_capabilities
        WHERE updated_at < NOW() - (:hours || ' hours')::interval
        """
    )
    with engine.connect() as conn:
        n = conn.execute(sql, {"hours": int(stale_hours)}).scalar()
    return int(n or 0)


def _whatsapp_accounts_column_missing(engine: Engine, column: str) -> Optional[bool]:
    """
    True if column is missing, False if present, None if whatsapp_accounts table is absent.
    """
    existing = _existing_tables(engine)
    if "whatsapp_accounts" not in existing:
        return None
    sql = text(
        """
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'whatsapp_accounts'
          AND column_name = :col
        """
    )
    with engine.connect() as conn:
        row = conn.execute(sql, {"col": column}).fetchone()
    return row is None


def slog(event: str, **fields: Any) -> None:
    """Single-line JSON-ish structured log for grep-friendly aggregation."""
    payload: Dict[str, Any] = {"whatsapp_event": event, **fields}
    logger.info("%s %s", event, json.dumps(payload, default=str))


def run_startup_deployment_checks(engine: Engine) -> None:
    """
    Log warnings/errors for operators. Safe to call when DB is up.
    """
    non_dev = is_non_dev_environment()

    if non_dev and not capabilities_upsert_secret_configured():
        logger.error(
            "WHATSAPP_STARTUP_WHATSAPP_CAPABILITIES_UPSERT_SECRET unset in non-dev; "
            "internal capabilities upserts will return 401."
        )

    if non_dev and not usage_events_read_secret_configured():
        logger.error(
            "WHATSAPP_STARTUP usage-events read secret unset in non-dev "
            "(set WHATSAPP_USAGE_EVENTS_SECRET or WHATSAPP_CAPABILITIES_UPSERT_SECRET); "
            "GET /usage-events will return 401."
        )

    ok_sched, sched_reason = scheduler_secret_acceptable_for_environment()
    if not ok_sched:
        logger.error("WHATSAPP_STARTUP_SCHEDULER %s", sched_reason)

    present, missing = migration_schema_status(engine)
    if missing and present:
        logger.warning(
            "WHATSAPP_STARTUP_MIGRATION_PARTIAL missing_tables=%s present_count=%s",
            ",".join(missing),
            len(present),
        )
    elif missing and not present:
        logger.warning(
            "WHATSAPP_STARTUP_MIGRATION_NONE expected slice tables missing: %s",
            ",".join(missing),
        )

    if whatsapp_capabilities_strict_enabled():
        missing_cap = count_accounts_missing_capabilities(engine)
        if missing_cap is None:
            logger.warning(
                "WHATSAPP_STARTUP_STRICT_MODE capabilities table missing; strict gating may misbehave."
            )
        elif missing_cap > 0:
            logger.warning(
                "WHATSAPP_STARTUP_STRICT_MODE accounts_missing_capabilities=%s "
                "(WHATSAPP_CAPABILITIES_STRICT=1)",
                missing_cap,
            )

    stale_cap = count_stale_capabilities(engine, stale_hours=24)
    if stale_cap is not None:
        slog("projection_freshness_startup", stale_projection_count=stale_cap, stale_hours=24)

    missing_safe_mode_col = _whatsapp_accounts_column_missing(engine, "safe_mode_reason_code")
    if missing_safe_mode_col is True:
        logger.error(
            "WHATSAPP_STARTUP_SCHEMA whatsapp_accounts.safe_mode_reason_code missing; "
            "account routes will fail with UndefinedColumn. Apply "
            "whatsapp/migrations/010_whatsapp_accounts_safe_mode_operational_expand.sql "
            "to the shared database."
        )

