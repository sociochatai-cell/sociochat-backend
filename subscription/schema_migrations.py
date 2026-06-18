"""Lightweight schema patches (create_all does not ALTER existing tables)."""

import logging
from sqlalchemy import inspect, text

from models import db

logger = logging.getLogger(__name__)


def _column_names(table: str) -> set[str]:
    try:
        insp = inspect(db.engine)
        return {c["name"] for c in insp.get_columns(table)}
    except Exception:
        return set()


def ensure_private_slot_schema() -> None:
    """Add billing_scope on users and plan_scope on subscription_plans if missing."""
    try:
        user_cols = _column_names("users")
        if user_cols and "billing_scope" not in user_cols:
            db.session.execute(
                text("ALTER TABLE users ADD COLUMN billing_scope VARCHAR(16) NOT NULL DEFAULT 'global'")
            )
            db.session.commit()
            logger.info("Added users.billing_scope column")

        plan_cols = _column_names("subscription_plans")
        if plan_cols and "plan_scope" not in plan_cols:
            db.session.execute(
                text(
                    "ALTER TABLE subscription_plans "
                    "ADD COLUMN plan_scope VARCHAR(16) NOT NULL DEFAULT 'global'"
                )
            )
            db.session.commit()
            logger.info("Added subscription_plans.plan_scope column")

        # Backfill NULL/empty scopes on existing rows
        if plan_cols:
            db.session.execute(
                text("UPDATE subscription_plans SET plan_scope = 'global' WHERE plan_scope IS NULL OR plan_scope = ''")
            )
            db.session.commit()
    except Exception:
        db.session.rollback()
        logger.exception("Private slot schema migration failed")
