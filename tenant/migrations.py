"""
Tenant Module - Schema migrations & backfill
============================================

``create_all`` creates NEW tables but never ALTERs existing ones, so the
multi-tenant columns are added here idempotently (mirrors
subscription/schema_migrations.py). Safe to run on every boot.

Order matters:
  1. add columns (users.tenant_id + denormalized tenant_id on flagged tables)
  2. ensure the internal tenant T0000 exists
  3. backfill tenant_id on existing rows
  4. switch users.email uniqueness from global -> (tenant_id, email)
"""

import logging
from sqlalchemy import inspect, text

from models import db

logger = logging.getLogger(__name__)

# Cross-cutting / admin tables that get a denormalized tenant_id for the
# "Hybrid" isolation model (direct tenant filtering without a join chain).
_TENANT_ID_TABLES_VIA_USER = [
    # (table, user_id_column) — backfilled by joining users.tenant_id
    ("audit_logs", "user_id"),
    ("plan_change_history", "user_id"),
    ("user_feature_access", "user_id"),
    ("ai_usage", "user_id"),
]


def _dialect() -> str:
    try:
        return db.engine.dialect.name
    except Exception:
        return ""


def _column_names(table: str) -> set:
    try:
        insp = inspect(db.engine)
        return {c["name"] for c in insp.get_columns(table)}
    except Exception:
        return set()


def _table_exists(table: str) -> bool:
    try:
        return inspect(db.engine).has_table(table)
    except Exception:
        return False


def _add_int_column(table: str, column: str) -> None:
    """Add a nullable integer column + index if missing (no hard FK)."""
    if not _table_exists(table):
        return
    cols = _column_names(table)
    if cols and column not in cols:
        try:
            db.session.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} INTEGER"))
            db.session.commit()
            db.session.execute(
                text(f"CREATE INDEX IF NOT EXISTS ix_{table}_{column} ON {table} ({column})")
            )
            db.session.commit()
            logger.info("Added %s.%s column", table, column)
        except Exception:
            db.session.rollback()
            logger.exception("Failed adding %s.%s", table, column)


def _add_text_column(table: str, column: str, col_type: str = "TEXT") -> None:
    """Add a nullable text/varchar column if missing.

    ``col_type`` is plain SQL accepted by both SQLite and Postgres
    (e.g. ``TEXT``, ``VARCHAR(255)``).
    """
    if not _table_exists(table):
        return
    cols = _column_names(table)
    if cols and column not in cols:
        try:
            db.session.execute(
                text(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
            )
            db.session.commit()
            logger.info("Added %s.%s column", table, column)
        except Exception:
            db.session.rollback()
            logger.exception("Failed adding %s.%s", table, column)


def ensure_tenant_columns() -> None:
    """Add tenant_id to users and to the flagged cross-cutting tables."""
    _add_int_column("users", "tenant_id")
    for table, _ in _TENANT_ID_TABLES_VIA_USER:
        _add_int_column(table, "tenant_id")


def ensure_landing_columns() -> None:
    """Add the landing-page hero columns to ``tenants`` if missing."""
    for column, col_type in (
        ("landing_video_url", "VARCHAR(1000)"),
        ("landing_image_url", "VARCHAR(1000)"),
        ("landing_headline", "VARCHAR(255)"),
        ("landing_subheadline", "TEXT"),
        ("landing_cta_text", "VARCHAR(500)"),
        ("phone_number", "VARCHAR(32)"),
    ):
        _add_text_column("tenants", column, col_type)


def ensure_appearance_columns() -> None:
    """Add the appearance columns (UI chrome colors + typography + shape) to
    ``tenants`` if missing."""
    for column, col_type in (
        ("background_color", "VARCHAR(32)"),
        ("surface_color", "VARCHAR(32)"),
        ("text_color", "VARCHAR(32)"),
        ("border_color", "VARCHAR(32)"),
        ("heading_font_family", "VARCHAR(120)"),
        ("corner_radius", "VARCHAR(32)"),
    ):
        _add_text_column("tenants", column, col_type)


def ensure_integration_columns() -> None:
    """Add the per-tenant integration columns (SMTP / AI keys / API versions) to
    ``tenant_integration`` if missing. create_all() never ALTERs existing tables."""
    for column, col_type in (
        ("whatsapp_api_version", "VARCHAR(16)"),
        ("fb_api_version", "VARCHAR(16)"),
        ("smtp_host", "VARCHAR(255)"),
        ("smtp_port", "INTEGER"),
        ("smtp_user", "VARCHAR(255)"),
        ("smtp_pass_enc", "TEXT"),
        ("mail_from", "VARCHAR(255)"),
        ("gemini_api_key_enc", "TEXT"),
        ("google_sa_json_enc", "TEXT"),
        ("sms_provider", "VARCHAR(32)"),
        ("sms_sender_id", "VARCHAR(32)"),
        ("sms_api_key_enc", "TEXT"),
        ("payu_key", "VARCHAR(128)"),
        ("payu_salt_enc", "TEXT"),
        ("payu_mode", "VARCHAR(12)"),
        ("payu_salt_version", "VARCHAR(4)"),
    ):
        _add_text_column("tenant_integration", column, col_type)


def ensure_tenant_plan_schema() -> None:
    """Create ``tenant_plans`` + add the payment columns to ``tenant_subscriptions``.

    The 503 ``database_unavailable`` errors happen when the model declares columns
    (``payment_status`` etc.) or a table (``tenant_plans``) that the live Postgres
    schema lacks — ``create_all()`` never ALTERs existing tables. This adds them
    idempotently and is safe to run on every boot.
    """
    # 1) Create the NEW tenant_plans table if create_all() didn't (defensive —
    #    e.g. if the model was registered after create_all on an older boot).
    try:
        from tenant.tenant_plan_models import TenantPlan
        TenantPlan.__table__.create(bind=db.engine, checkfirst=True)
    except Exception:
        db.session.rollback()
        logger.exception("ensure tenant_plans table failed")

    # Add the feature/limit matrix column to pre-existing tenant_plans tables.
    _add_text_column("tenant_plans", "features", "JSON")

    # 2) Add the payment-placeholder columns to the existing tenant_subscriptions.
    for column, col_type in (
        ("payment_status", "VARCHAR(16)"),
        ("payment_provider", "VARCHAR(32)"),
        ("payment_ref", "VARCHAR(128)"),
        ("started_at", "TIMESTAMP"),
    ):
        _add_text_column("tenant_subscriptions", column, col_type)
    # Backfill a sane default so existing rows aren't NULL on a NOT NULL column.
    if _table_exists("tenant_subscriptions") and "payment_status" in _column_names("tenant_subscriptions"):
        try:
            db.session.execute(text(
                "UPDATE tenant_subscriptions SET payment_status = 'none' "
                "WHERE payment_status IS NULL"
            ))
            db.session.commit()
        except Exception:
            db.session.rollback()


def ensure_internal_tenant():
    """Create the internal SocioChat tenant (T0000) if absent; return it."""
    from tenant.models import Tenant, TenantSubscription
    from tenant.branding import INTERNAL_TENANT_CODE, INTERNAL_TENANT_NAME

    tenant = Tenant.query.filter_by(tenant_code=INTERNAL_TENANT_CODE).first()
    if not tenant:
        tenant = Tenant(
            tenant_code=INTERNAL_TENANT_CODE,
            company_name=INTERNAL_TENANT_NAME,
            status="active",
            subscription_plan="enterprise",
        )
        db.session.add(tenant)
        db.session.commit()
        logger.info("Created internal tenant %s", INTERNAL_TENANT_CODE)

    sub = TenantSubscription.query.filter_by(tenant_id=tenant.id).first()
    if not sub:
        db.session.add(TenantSubscription(
            tenant_id=tenant.id,
            plan_slug=tenant.subscription_plan or "enterprise",
            billing_scope="global",
        ))
        db.session.commit()
    return tenant


def backfill_tenant_ids(internal_tenant_id: int) -> None:
    """Assign existing rows to the internal tenant and propagate to children."""
    try:
        # Existing users with no tenant -> internal tenant T0000.
        db.session.execute(
            text("UPDATE users SET tenant_id = :tid WHERE tenant_id IS NULL"),
            {"tid": internal_tenant_id},
        )
        db.session.commit()
    except Exception:
        db.session.rollback()
        logger.exception("Backfill users.tenant_id failed")

    # Denormalized tenant_id on child tables, derived from the owning user.
    for table, user_col in _TENANT_ID_TABLES_VIA_USER:
        if not _table_exists(table) or "tenant_id" not in _column_names(table):
            continue
        try:
            db.session.execute(text(
                f"UPDATE {table} SET tenant_id = ("
                f"  SELECT u.tenant_id FROM users u WHERE u.id = {table}.{user_col}"
                f") WHERE tenant_id IS NULL AND {user_col} IS NOT NULL"
            ))
            db.session.commit()
        except Exception:
            db.session.rollback()
            logger.exception("Backfill %s.tenant_id failed", table)


def ensure_email_uniqueness() -> None:
    """Switch users.email from globally-unique to unique-per-tenant.

    Same email may exist in different tenants (ABC001+admin@x / XYZ001+admin@x).
    """
    if not _table_exists("users"):
        return
    dialect = _dialect()
    try:
        # New composite uniqueness (works on both Postgres and SQLite).
        db.session.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_users_tenant_email "
            "ON users (tenant_id, email)"
        ))
        db.session.commit()
    except Exception:
        db.session.rollback()
        logger.exception("Failed creating composite (tenant_id, email) unique index")

    # Drop the old global unique on email so duplicates across tenants are allowed.
    if dialect == "postgresql":
        for stmt in (
            "ALTER TABLE users DROP CONSTRAINT IF EXISTS users_email_key",
            "DROP INDEX IF EXISTS users_email_key",
            "DROP INDEX IF EXISTS ix_users_email_unique",
        ):
            try:
                db.session.execute(text(stmt))
                db.session.commit()
            except Exception:
                db.session.rollback()

    # The live DB may STILL carry a UNIQUE index literally named ``ix_users_email``
    # (created back when User.email was globally unique). The model is now
    # non-unique (``index=True`` only — uniqueness is per-tenant via
    # ``uq_users_tenant_email`` created above), but create_all() never drops an
    # existing index. That stale unique index makes tenant creation fail with
    # "duplicate key value violates unique constraint ix_users_email" whenever a
    # tenant's admin/demo email already exists in ANOTHER tenant. Replace it with
    # a plain (non-unique) index. Idempotent: only acts when it's actually unique.
    try:
        existing = {i.get("name"): i for i in inspect(db.engine).get_indexes("users")}
        ix = existing.get("ix_users_email")
        if ix and ix.get("unique"):
            db.session.execute(text("DROP INDEX IF EXISTS ix_users_email"))
            db.session.commit()
            db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_users_email ON users (email)"))
            db.session.commit()
            logger.info("Dropped legacy UNIQUE ix_users_email; recreated as non-unique (per-tenant email)")
    except Exception:
        db.session.rollback()
        logger.exception("Failed normalizing ix_users_email to non-unique")

    # SQLite cannot drop a column-level UNIQUE without a table rebuild. Fresh
    # SQLite DBs already get the per-tenant model definition, so existing dev
    # DBs are left as-is (acceptable: dev only).


def run_tenant_migrations() -> None:
    """Entry point called once at startup (inside app context)."""
    try:
        ensure_tenant_columns()
        ensure_landing_columns()
        ensure_appearance_columns()
        ensure_integration_columns()
        ensure_tenant_plan_schema()
        internal = ensure_internal_tenant()
        backfill_tenant_ids(internal.id)
        ensure_email_uniqueness()
        logger.info("Tenant migrations complete")
    except Exception:
        db.session.rollback()
        logger.exception("Tenant migrations failed")
