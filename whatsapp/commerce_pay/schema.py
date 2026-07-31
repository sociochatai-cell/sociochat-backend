# whatsapp/commerce_pay/schema.py
"""Idempotent DDL for the commerce_pay tables (self-heal on startup)."""

import logging
from sqlalchemy import text

logger = logging.getLogger(__name__)

_STATEMENTS = [
    """CREATE TABLE IF NOT EXISTS workspace_payment_configs (
        id                       SERIAL PRIMARY KEY,
        workspace_id             INTEGER NOT NULL REFERENCES workspaces2(id) ON DELETE CASCADE,
        provider                 VARCHAR(16) NOT NULL DEFAULT 'payu',
        merchant_key             VARCHAR(255),
        merchant_salt_encrypted  TEXT,
        mode                     VARCHAR(8) NOT NULL DEFAULT 'test',
        is_active                BOOLEAN NOT NULL DEFAULT TRUE,
        created_at               TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at               TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
        CONSTRAINT uq_workspace_payment_config UNIQUE (workspace_id)
    )""",
    "CREATE INDEX IF NOT EXISTS ix_workspace_payment_configs_workspace_id ON workspace_payment_configs(workspace_id)",
    """CREATE TABLE IF NOT EXISTS commerce_orders (
        id               SERIAL PRIMARY KEY,
        workspace_id     INTEGER NOT NULL REFERENCES workspaces2(id) ON DELETE CASCADE,
        txnid            VARCHAR(64) NOT NULL UNIQUE,
        customer_phone   VARCHAR(32) NOT NULL,
        customer_name    VARCHAR(120),
        customer_email   VARCHAR(160),
        conversation_id  INTEGER,
        amount           NUMERIC(12,2) NOT NULL,
        currency         VARCHAR(8) NOT NULL DEFAULT 'INR',
        productinfo      VARCHAR(160) NOT NULL DEFAULT 'Order',
        status           VARCHAR(16) NOT NULL DEFAULT 'pending',
        payu_mihpayid    VARCHAR(64),
        mode             VARCHAR(8) NOT NULL DEFAULT 'test',
        created_at       TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at       TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
        paid_at          TIMESTAMP WITH TIME ZONE
    )""",
    "CREATE INDEX IF NOT EXISTS ix_commerce_orders_workspace_id ON commerce_orders(workspace_id)",
    "CREATE INDEX IF NOT EXISTS ix_commerce_orders_txnid ON commerce_orders(txnid)",
    "CREATE INDEX IF NOT EXISTS ix_commerce_orders_status ON commerce_orders(status)",
    # --- auto-payment + notifications (added later; idempotent ALTERs) ---
    "ALTER TABLE workspace_payment_configs ADD COLUMN IF NOT EXISTS auto_request_payment BOOLEAN NOT NULL DEFAULT FALSE",
    "ALTER TABLE workspace_payment_configs ADD COLUMN IF NOT EXISTS notify_emails TEXT",
    "ALTER TABLE commerce_orders ADD COLUMN IF NOT EXISTS source_message_id VARCHAR(128)",
    "ALTER TABLE commerce_orders ADD COLUMN IF NOT EXISTS origin VARCHAR(8) NOT NULL DEFAULT 'manual'",
    "CREATE INDEX IF NOT EXISTS ix_commerce_orders_source_message_id ON commerce_orders(source_message_id)",
    """CREATE TABLE IF NOT EXISTS commerce_chat_overrides (
        id               SERIAL PRIMARY KEY,
        workspace_id     INTEGER NOT NULL,
        conversation_id  INTEGER NOT NULL,
        mode             VARCHAR(8) NOT NULL DEFAULT 'on',
        updated_at       TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
        CONSTRAINT uq_commerce_chat_override_conv UNIQUE (conversation_id)
    )""",
    "CREATE INDEX IF NOT EXISTS ix_commerce_chat_overrides_workspace_id ON commerce_chat_overrides(workspace_id)",
]


def ensure_commerce_pay_schema(engine) -> None:
    """Apply each DDL statement in its OWN transaction so one failure can never
    roll back the others (a partial failure previously dropped ALL the new
    columns, which made a connected PayU config read as 'lost')."""
    ok, failed = 0, 0
    for stmt in _STATEMENTS:
        try:
            with engine.begin() as conn:
                conn.execute(text(stmt))
            ok += 1
        except Exception as e:
            failed += 1
            logger.warning("commerce_pay schema stmt skipped: %s | %s",
                           stmt.split("(")[0].strip()[:80], e)
    logger.info("commerce_pay schema ensured (%s ok, %s skipped)", ok, failed)
