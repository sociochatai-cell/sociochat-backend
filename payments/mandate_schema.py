"""Idempotent DDL for PayU mandate (autopay) tables — self-heal on startup."""

import logging
from sqlalchemy import text

logger = logging.getLogger(__name__)

_STATEMENTS = [
    """CREATE TABLE IF NOT EXISTS payu_mandates (
        id                    BIGSERIAL PRIMARY KEY,
        user_id               INTEGER NOT NULL,
        tenant_id             INTEGER,
        plan_slug             VARCHAR(64) NOT NULL,
        billing_period        VARCHAR(16) NOT NULL DEFAULT 'monthly',
        amount                NUMERIC(12,2) NOT NULL,
        currency              VARCHAR(8) NOT NULL DEFAULT 'INR',
        status                VARCHAR(24) NOT NULL DEFAULT 'pending_registration',
        si_token              VARCHAR(128),
        registration_txnid    VARCHAR(64),
        payu_mode             VARCHAR(8) NOT NULL DEFAULT 'test',
        next_charge_at        TIMESTAMP WITH TIME ZONE,
        last_charge_at        TIMESTAMP WITH TIME ZONE,
        last_charge_status    VARCHAR(24),
        consecutive_failures  INTEGER NOT NULL DEFAULT 0,
        notified_at           TIMESTAMP WITH TIME ZONE,
        created_at            TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at            TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP
    )""",
    "CREATE INDEX IF NOT EXISTS ix_payu_mandates_user_id ON payu_mandates(user_id)",
    "CREATE INDEX IF NOT EXISTS ix_payu_mandates_status ON payu_mandates(status)",
    "CREATE INDEX IF NOT EXISTS ix_payu_mandates_reg_txnid ON payu_mandates(registration_txnid)",
    "CREATE INDEX IF NOT EXISTS ix_payu_mandates_status_next ON payu_mandates(status, next_charge_at)",
    # At most ONE live mandate per user (pending/active/paused).
    """CREATE UNIQUE INDEX IF NOT EXISTS uq_payu_mandate_live
        ON payu_mandates (user_id)
        WHERE status IN ('pending_registration','active','paused')""",
    """CREATE TABLE IF NOT EXISTS payu_mandate_charges (
        id          BIGSERIAL PRIMARY KEY,
        mandate_id  BIGINT NOT NULL,
        txnid       VARCHAR(64) NOT NULL UNIQUE,
        amount      NUMERIC(12,2),
        status      VARCHAR(24) NOT NULL DEFAULT 'created',
        payu_id     VARCHAR(64),
        error       TEXT,
        created_at  TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP
    )""",
    "CREATE INDEX IF NOT EXISTS ix_payu_mandate_charges_mandate ON payu_mandate_charges(mandate_id)",
]


def ensure_mandate_schema(engine) -> None:
    ok, failed = 0, 0
    for stmt in _STATEMENTS:
        try:
            with engine.begin() as conn:
                conn.execute(text(stmt))
            ok += 1
        except Exception as e:
            failed += 1
            logger.warning("mandate schema stmt skipped: %s | %s", stmt.split("(")[0].strip()[:70], e)
    logger.info("payu mandate schema ensured (%s ok, %s skipped)", ok, failed)
