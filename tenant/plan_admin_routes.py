"""
Tenant Module - Custom Per-Tenant Subscription Plans (Super Admin API)
======================================================================

A *custom plan* is a ``SubscriptionPlan`` that exists for exactly ONE tenant
(``tenant_id`` set, ``is_public=False``, ``plan_scope='private'``). Because
``subscription/service.py::load_plan_matrix`` already resolves any active plan
from its ``PlanFeatureAccess`` rows, a custom plan "just works" the moment it
exists — assigning it via the existing ``PUT /tenants/<id>/subscription`` stamps
``user.plan`` for every tenant user.

This module only DEFINES the blueprint + migration helper. The integrator wires
them into ``app.py`` (registers ``tenant_plans_bp`` and calls
``ensure_custom_plan_schema()``).
"""

import re
import logging
import secrets

from flask import Blueprint, jsonify, request
from sqlalchemy import inspect, text

from models import db
from subscription.plan_models import SubscriptionPlan, PlanFeatureAccess
from tenant.context import require_super_admin

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Idempotent schema patch (mirrors subscription/schema_migrations.py)
# --------------------------------------------------------------------------- #
def _column_names(table: str) -> set:
    try:
        insp = inspect(db.engine)
        return {c["name"] for c in insp.get_columns(table)}
    except Exception:
        return set()


def ensure_custom_plan_schema() -> None:
    """Add ``subscription_plans.tenant_id`` (+ index) if missing.

    Idempotent and safe to call on every boot. Any failure is logged and rolled
    back so it never blocks startup.
    """
    try:
        plan_cols = _column_names("subscription_plans")
        if plan_cols and "tenant_id" not in plan_cols:
            db.session.execute(
                text("ALTER TABLE subscription_plans ADD COLUMN tenant_id INTEGER")
            )
            db.session.commit()
            logger.info("Added subscription_plans.tenant_id column")
            try:
                db.session.execute(
                    text(
                        "CREATE INDEX IF NOT EXISTS ix_subscription_plans_tenant_id "
                        "ON subscription_plans (tenant_id)"
                    )
                )
                db.session.commit()
                logger.info("Created index ix_subscription_plans_tenant_id")
            except Exception:
                db.session.rollback()
                logger.exception("Creating subscription_plans.tenant_id index failed")
    except Exception:
        db.session.rollback()
        logger.exception("Custom plan schema migration failed")


# --------------------------------------------------------------------------- #
# Blueprint
# --------------------------------------------------------------------------- #
tenant_plans_bp = Blueprint("tenant_plans", __name__, url_prefix="/api/superadmin")


def _slugify(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (value or "").lower()).strip("-")


def _unique_slug(base: str) -> str:
    """Return ``base`` or, if taken, ``base`` with a short token appended."""
    base = (base or "plan")[:32].strip("-") or "plan"
    if not SubscriptionPlan.query.filter_by(slug=base).first():
        return base
    for _ in range(40):
        token = secrets.token_hex(2)
        candidate = f"{base[:24].strip('-')}-{token}"[:32]
        if not SubscriptionPlan.query.filter_by(slug=candidate).first():
            return candidate
    # Extremely unlikely fallback.
    return f"{base[:18].strip('-')}-{secrets.token_hex(6)}"[:32]


def _get_tenant_or_404(tenant_id: int):
    from tenant.models import Tenant

    return db.session.get(Tenant, tenant_id)


def _custom_plan_dict(plan: SubscriptionPlan) -> dict:
    return {
        "id": plan.id,
        "slug": plan.slug,
        "name": plan.name,
        "price_monthly_inr": plan.price_monthly_inr,
        "tenant_id": plan.tenant_id,
    }


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
@tenant_plans_bp.route("/tenants/<int:tenant_id>/plans", methods=["POST"])
@require_super_admin
def create_custom_plan(admin, tenant_id):
    """Create a tenant-only custom plan + its PlanFeatureAccess matrix."""
    tenant = _get_tenant_or_404(tenant_id)
    if not tenant:
        return jsonify({"success": False, "error": "tenant_not_found"}), 404

    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"success": False, "error": "name_required"}), 400

    try:
        price = data.get("price_monthly_inr")
        price = int(price) if price not in (None, "") else 0
    except (TypeError, ValueError):
        price = 0

    features = data.get("features") or {}
    if not isinstance(features, dict):
        features = {}

    tenant_code = (getattr(tenant, "tenant_code", "") or "").lower()
    base_slug = f"t{tenant_code}_{_slugify(name)}"
    slug = _unique_slug(base_slug)

    try:
        plan = SubscriptionPlan(
            slug=slug,
            name=name,
            price_monthly_inr=price,
            is_public=False,
            is_active=True,
            plan_scope="private",
            tenant_id=tenant_id,
        )
        db.session.add(plan)
        db.session.flush()  # need plan.id for feature access rows

        for feature_key, spec in features.items():
            if not isinstance(spec, dict):
                spec = {"enabled": bool(spec)}
            enabled = spec.get("enabled")
            limit_value = spec.get("limit_value")
            db.session.add(PlanFeatureAccess(
                plan_id=plan.id,
                feature_key=feature_key,
                enabled=True if enabled is None else bool(enabled),
                limit_value=limit_value if limit_value not in ("",) else None,
            ))

        db.session.commit()
    except Exception:
        db.session.rollback()
        logger.exception("create_custom_plan failed tenant=%s", tenant_id)
        return jsonify({"success": False, "error": "internal_error"}), 500

    return jsonify({"success": True, "plan": _custom_plan_dict(plan)}), 201


@tenant_plans_bp.route("/tenants/<int:tenant_id>/plans", methods=["GET"])
@require_super_admin
def list_custom_plans(admin, tenant_id):
    """List this tenant's custom plans plus the global plan catalog."""
    tenant = _get_tenant_or_404(tenant_id)
    if not tenant:
        return jsonify({"success": False, "error": "tenant_not_found"}), 404

    custom_plans = [
        _custom_plan_dict(p)
        for p in SubscriptionPlan.query
        .filter_by(tenant_id=tenant_id)
        .order_by(SubscriptionPlan.created_at.asc())
        .all()
    ]

    # Global catalog = constant VALID_PLANS + active public global (tenant_id NULL) plans.
    global_plans = []
    seen = set()
    try:
        from subscription.constants import VALID_PLANS
        for slug in VALID_PLANS:
            if slug not in seen:
                global_plans.append({"slug": slug, "name": slug.title()})
                seen.add(slug)
    except Exception:
        logger.exception("VALID_PLANS import failed")

    try:
        rows = (
            SubscriptionPlan.query
            .filter(
                SubscriptionPlan.tenant_id.is_(None),
                SubscriptionPlan.is_public.is_(True),
                SubscriptionPlan.is_active.is_(True),
            )
            .all()
        )
        for row in rows:
            if row.slug not in seen:
                global_plans.append({"slug": row.slug, "name": getattr(row, "name", row.slug)})
                seen.add(row.slug)
    except Exception:
        logger.exception("global plan catalog load failed")

    return jsonify({
        "success": True,
        "custom_plans": custom_plans,
        "global_plans": global_plans,
    })


@tenant_plans_bp.route("/tenants/<int:tenant_id>/plans/<int:plan_id>", methods=["DELETE"])
@require_super_admin
def delete_custom_plan(admin, tenant_id, plan_id):
    """Delete a custom plan + its PlanFeatureAccess rows. Never touches globals."""
    tenant = _get_tenant_or_404(tenant_id)
    if not tenant:
        return jsonify({"success": False, "error": "tenant_not_found"}), 404

    plan = db.session.get(SubscriptionPlan, plan_id)
    if not plan or plan.tenant_id != tenant_id:
        # Only a custom plan owned by THIS tenant may be deleted.
        return jsonify({"success": False, "error": "plan_not_found"}), 404

    try:
        PlanFeatureAccess.query.filter_by(plan_id=plan.id).delete(synchronize_session=False)
        db.session.delete(plan)
        db.session.commit()
    except Exception:
        db.session.rollback()
        logger.exception("delete_custom_plan failed tenant=%s plan=%s", tenant_id, plan_id)
        return jsonify({"success": False, "error": "internal_error"}), 500

    return jsonify({"success": True})
