"""
Tenant Module - Tenant-scoped Private Slot
==========================================

A TENANT-scoped mirror of the PLATFORM super-admin private-slot surface
(``subscription/routes.py``), but locked to the calling tenant admin's OWN
tenant. This is security-critical: EVERY query in this module is filtered by
``admin.tenant_id``.

* Users: only ``User.query.filter_by(tenant_id=admin.tenant_id)``. Any user_id
  not in the admin's tenant returns 404.
* Plans: private-slot plans are ``plan_scope='private'``,
  ``tenant_id=admin.tenant_id``, ``is_public=False``. A plan whose
  ``tenant_id != admin.tenant_id`` is never read or modified (returns 404).

No DB schema changes — ``billing_scope`` (User), ``plan_scope`` / ``tenant_id``
(SubscriptionPlan) already exist.
"""

import logging

from flask import Blueprint, jsonify, request

from models import db, User
from tenant.context import require_tenant_admin

logger = logging.getLogger(__name__)

tenant_private_slot_bp = Blueprint(
    "tenant_private_slot", __name__, url_prefix="/api/tenant/admin/private-slot"
)


# --------------------------------------------------------------------------- #
# Serializers (mirror subscription.routes._serialize_slot_user, tenant-trimmed)
# --------------------------------------------------------------------------- #
def _serialize_slot_user(user: User) -> dict:
    from subscription.constants import BILLING_SCOPE_GLOBAL

    return {
        "id": user.id,
        "name": user.name,
        "email": user.email,
        "billing_scope": getattr(user, "billing_scope", None) or BILLING_SCOPE_GLOBAL,
        "plan": user.plan or "beta",
    }


def _serialize_private_plan(plan) -> dict:
    """Private plan with its PlanFeatureAccess matrix (keyed by feature key)."""
    from subscription.plan_models import PlanFeatureAccess

    features = {}
    for acc in PlanFeatureAccess.query.filter_by(plan_id=plan.id).all():
        features[acc.feature_key] = {
            "enabled": bool(acc.enabled),
            "limit_value": acc.limit_value,
        }
    return {
        "id": plan.id,
        "slug": plan.slug,
        "name": plan.name,
        "offer_text": plan.offer_text,
        "price_monthly_inr": plan.price_monthly_inr,
        "billing_period": getattr(plan, "billing_period", None) or "monthly",
        "is_active": bool(plan.is_active),
        "features": features,
    }


def _tenant_private_plans(tenant_id):
    """All private-scope plans owned by THIS tenant (never globals/other tenants)."""
    from subscription.plan_models import SubscriptionPlan
    from subscription.constants import PLAN_SCOPE_PRIVATE

    return (
        SubscriptionPlan.query
        .filter_by(plan_scope=PLAN_SCOPE_PRIVATE, tenant_id=tenant_id)
        .order_by(SubscriptionPlan.sort_order, SubscriptionPlan.created_at.asc())
        .all()
    )


def _get_owned_private_plan(plan_id, tenant_id):
    """Return a private plan ONLY if it belongs to this tenant, else None."""
    from subscription.plan_models import SubscriptionPlan
    from subscription.constants import PLAN_SCOPE_PRIVATE

    plan = db.session.get(SubscriptionPlan, plan_id)
    if (
        not plan
        or plan.tenant_id != tenant_id
        or (plan.plan_scope or "global") != PLAN_SCOPE_PRIVATE
    ):
        return None
    return plan


_VALID_BILLING_PERIODS = ("monthly", "quarterly", "yearly")


def _normalize_billing_period(value) -> str:
    """Normalize billing_period to {monthly, quarterly, yearly}; else 'monthly'."""
    period = (str(value or "")).strip().lower()
    return period if period in _VALID_BILLING_PERIODS else "monthly"


def _slugify(value: str) -> str:
    import re

    return re.sub(r"[^a-z0-9]+", "-", (value or "").lower()).strip("-")


def _unique_plan_slug(base: str) -> str:
    """Return ``base`` or, if taken, ``base`` with a short token appended."""
    import secrets
    from subscription.plan_models import SubscriptionPlan

    base = (base or "plan")[:32].strip("-") or "plan"
    if not SubscriptionPlan.query.filter_by(slug=base).first():
        return base
    for _ in range(40):
        token = secrets.token_hex(2)
        candidate = f"{base[:24].strip('-')}-{token}"[:32]
        if not SubscriptionPlan.query.filter_by(slug=candidate).first():
            return candidate
    return f"{base[:18].strip('-')}-{secrets.token_hex(6)}"[:32]


def _apply_plan_features(plan_id, features):
    """Replace/upsert a plan's PlanFeatureAccess rows from a {key: spec} dict."""
    from subscription.plan_models import PlanFeatureAccess

    if not isinstance(features, dict):
        return
    for feature_key, spec in features.items():
        if not isinstance(spec, dict):
            spec = {"enabled": bool(spec)}
        enabled = spec.get("enabled")
        limit_value = spec.get("limit_value")
        if limit_value in ("",):
            limit_value = None
        row = PlanFeatureAccess.query.filter_by(
            plan_id=plan_id, feature_key=feature_key
        ).first()
        if row:
            row.enabled = True if enabled is None else bool(enabled)
            row.limit_value = limit_value
        else:
            db.session.add(PlanFeatureAccess(
                plan_id=plan_id,
                feature_key=feature_key,
                enabled=True if enabled is None else bool(enabled),
                limit_value=limit_value,
            ))


def _feature_catalog():
    """DB-driven feature catalog (active features only)."""
    try:
        from subscription.plan_models import SubscriptionFeature
        rows = (
            SubscriptionFeature.query
            .filter_by(is_active=True)
            .order_by(SubscriptionFeature.sort_order)
            .all()
        )
        return [
            {
                "key": r.key,
                "label": getattr(r, "label", r.key),
                "category": getattr(r, "category", "general"),
                "feature_type": getattr(r, "feature_type", "access"),
            }
            for r in rows
        ]
    except Exception:
        logger.exception("private-slot feature catalog load failed")
        return []


# --------------------------------------------------------------------------- #
# Overview (scoped to this tenant)
# --------------------------------------------------------------------------- #
@tenant_private_slot_bp.route("", methods=["GET"])
@require_tenant_admin
def private_slot_overview(admin):
    """All this-tenant users, private members, this-tenant private plans, catalog."""
    from subscription.constants import BILLING_SCOPE_PRIVATE, PRIVATE_SLOT_GLOBAL_TIERS

    # TENANT SCOPE: users limited to admin.tenant_id.
    users = (
        User.query
        .filter_by(tenant_id=admin.tenant_id)
        .order_by(User.created_at.desc())
        .all()
    )
    serialized_users = [_serialize_slot_user(u) for u in users]
    private_members = [
        u for u in serialized_users
        if u["billing_scope"] == BILLING_SCOPE_PRIVATE
    ]

    # TENANT SCOPE: only plan_scope=private + tenant_id=admin.tenant_id.
    plans = _tenant_private_plans(admin.tenant_id)

    return jsonify({
        "success": True,
        "users": serialized_users,
        "private_members": private_members,
        "private_plans": [_serialize_private_plan(p) for p in plans],
        "global_tiers": list(PRIVATE_SLOT_GLOBAL_TIERS),
        "feature_catalog": _feature_catalog(),
    })


# --------------------------------------------------------------------------- #
# Scope management (own-tenant users only)
# --------------------------------------------------------------------------- #
@tenant_private_slot_bp.route("/users/<int:user_id>/scope", methods=["PATCH"])
@require_tenant_admin
def private_slot_set_scope(admin, user_id):
    """Move one of THIS tenant's users between global and private slot."""
    from subscription.constants import BILLING_SCOPE_GLOBAL, BILLING_SCOPE_PRIVATE

    # TENANT SCOPE: user must belong to admin.tenant_id.
    target = db.session.get(User, user_id)
    if not target or target.tenant_id != admin.tenant_id:
        return jsonify({"success": False, "error": "user_not_in_tenant"}), 404

    data = request.get_json(silent=True) or {}
    scope = (data.get("scope") or "").strip().lower()
    if scope not in (BILLING_SCOPE_GLOBAL, BILLING_SCOPE_PRIVATE):
        return jsonify({"success": False, "error": "invalid_scope"}), 400

    target.billing_scope = scope
    db.session.commit()
    return jsonify({"success": True, "user": _serialize_slot_user(target)})


@tenant_private_slot_bp.route("/users/scope", methods=["PATCH"])
@require_tenant_admin
def private_slot_bulk_scope(admin):
    """Bulk move THIS tenant's users; ids outside the tenant are skipped."""
    from subscription.constants import BILLING_SCOPE_GLOBAL, BILLING_SCOPE_PRIVATE

    data = request.get_json(silent=True) or {}
    scope = (data.get("scope") or "").strip().lower()
    raw_ids = data.get("user_ids") or []

    if scope not in (BILLING_SCOPE_GLOBAL, BILLING_SCOPE_PRIVATE):
        return jsonify({"success": False, "error": "invalid_scope"}), 400
    if not isinstance(raw_ids, list) or not raw_ids:
        return jsonify({"success": False, "error": "user_ids_required"}), 400

    user_ids = []
    for val in raw_ids:
        try:
            user_ids.append(int(val))
        except (TypeError, ValueError):
            continue

    updated = 0
    if user_ids:
        # TENANT SCOPE: only users whose tenant_id == admin.tenant_id are loaded.
        targets = (
            User.query
            .filter(User.id.in_(user_ids), User.tenant_id == admin.tenant_id)
            .all()
        )
        for target in targets:
            if (target.billing_scope or BILLING_SCOPE_GLOBAL) == scope:
                continue
            target.billing_scope = scope
            updated += 1
        db.session.commit()

    return jsonify({"success": True, "updated": updated})


# --------------------------------------------------------------------------- #
# Per-user plan assignment (own-tenant users + own-tenant private plans)
# --------------------------------------------------------------------------- #
@tenant_private_slot_bp.route("/users/<int:user_id>/plan", methods=["PUT"])
@require_tenant_admin
def private_slot_set_user_plan(admin, user_id):
    """Assign a plan slug to one of THIS tenant's users."""
    from subscription.service import (
        is_plan_assignable_to_user, get_assignable_plan_slugs_for_user,
        change_user_plan,
    )
    from subscription.plan_models import SubscriptionPlan
    from subscription.constants import VALID_PLANS, PLAN_SCOPE_PRIVATE

    # TENANT SCOPE: user must belong to admin.tenant_id.
    target = db.session.get(User, user_id)
    if not target or target.tenant_id != admin.tenant_id:
        return jsonify({"success": False, "error": "user_not_in_tenant"}), 404

    data = request.get_json(silent=True) or {}
    plan_slug = (data.get("plan_slug") or "").strip()
    if not plan_slug:
        return jsonify({"success": False, "error": "plan_required"}), 400

    # Reject up-front: a private (non-global) plan must belong to THIS tenant.
    # (is_plan_assignable_to_user only checks plan_scope, not tenant_id.)
    if plan_slug not in VALID_PLANS:
        owned = SubscriptionPlan.query.filter_by(
            slug=plan_slug,
            plan_scope=PLAN_SCOPE_PRIVATE,
            tenant_id=admin.tenant_id,
        ).first()
        if not owned:
            return jsonify({"success": False, "error": "plan_not_in_tenant"}), 404

    if not is_plan_assignable_to_user(target, plan_slug):
        return jsonify({
            "success": False,
            "error": "invalid_plan",
            "valid_plans": get_assignable_plan_slugs_for_user(target),
        }), 400

    try:
        change_user_plan(
            target, plan_slug,
            reason="Tenant private slot plan change",
        )
    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 400

    # Set the subscription end date: from an explicit date if provided,
    # otherwise auto-compute from the plan's billing period.
    from datetime import datetime, timezone
    from subscription.service import expiry_from_period

    _exp_raw = (data.get("subscription_expires_at") or data.get("expires_at"))
    if _exp_raw:
        try:
            target.subscription_expires_at = datetime.fromisoformat(str(_exp_raw).replace("Z", "+00:00"))
        except (ValueError, TypeError):
            pass
    else:
        _sp = SubscriptionPlan.query.filter_by(slug=plan_slug).first()
        _auto = expiry_from_period(datetime.now(timezone.utc), getattr(_sp, "billing_period", None))
        if _auto is not None:
            target.subscription_expires_at = _auto
    db.session.commit()

    return jsonify({"success": True, "user": _serialize_slot_user(target)})


# --------------------------------------------------------------------------- #
# Private plan CRUD (all owned by THIS tenant)
# --------------------------------------------------------------------------- #
@tenant_private_slot_bp.route("/plans", methods=["POST"])
@require_tenant_admin
def private_slot_create_plan(admin):
    """Create a private-scope plan owned by THIS tenant + its feature matrix."""
    from subscription.plan_models import SubscriptionPlan
    from subscription.constants import PLAN_SCOPE_PRIVATE
    from tenant.models import Tenant

    tenant = db.session.get(Tenant, admin.tenant_id)
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

    tenant_code = (getattr(tenant, "tenant_code", "") or "").lower()
    base_slug = f"t{tenant_code}_ps_{_slugify(name)}"
    slug = _unique_plan_slug(base_slug)

    try:
        # TENANT SCOPE: plan created as private + owned by admin.tenant_id.
        plan = SubscriptionPlan(
            slug=slug,
            name=name,
            offer_text=(data.get("offer_text") or None),
            price_monthly_inr=price,
            billing_period=_normalize_billing_period(data.get("billing_period")),
            is_public=False,
            is_active=True,
            plan_scope=PLAN_SCOPE_PRIVATE,
            tenant_id=admin.tenant_id,
        )
        db.session.add(plan)
        db.session.flush()  # need plan.id for feature access rows
        _apply_plan_features(plan.id, data.get("features") or {})
        db.session.commit()
    except Exception:
        db.session.rollback()
        logger.exception(
            "private_slot_create_plan failed tenant=%s", admin.tenant_id
        )
        return jsonify({"success": False, "error": "internal_error"}), 500

    return jsonify({"success": True, "plan": _serialize_private_plan(plan)}), 201


@tenant_private_slot_bp.route("/plans/<int:plan_id>", methods=["PUT"])
@require_tenant_admin
def private_slot_update_plan(admin, plan_id):
    """Update name/is_active of a private plan owned by THIS tenant."""
    # TENANT SCOPE: plan must be private + tenant_id == admin.tenant_id.
    plan = _get_owned_private_plan(plan_id, admin.tenant_id)
    if not plan:
        return jsonify({"success": False, "error": "plan_not_found"}), 404

    data = request.get_json(silent=True) or {}
    if "name" in data and (data.get("name") or "").strip():
        plan.name = data["name"].strip()
    if "offer_text" in data:
        plan.offer_text = (data.get("offer_text") or None)
    if "price_monthly_inr" in data:
        try:
            price = data.get("price_monthly_inr")
            plan.price_monthly_inr = int(price) if price not in (None, "") else 0
        except (TypeError, ValueError):
            plan.price_monthly_inr = 0
    if "is_active" in data:
        plan.is_active = bool(data["is_active"])
    if "billing_period" in data:
        plan.billing_period = _normalize_billing_period(data.get("billing_period"))
    db.session.commit()

    return jsonify({"success": True, "plan": _serialize_private_plan(plan)})


@tenant_private_slot_bp.route("/plans/<int:plan_id>", methods=["DELETE"])
@require_tenant_admin
def private_slot_delete_plan(admin, plan_id):
    """Delete a private plan owned by THIS tenant; refuse if any user uses it."""
    from subscription.plan_models import PlanFeatureAccess

    # TENANT SCOPE: plan must be private + tenant_id == admin.tenant_id.
    plan = _get_owned_private_plan(plan_id, admin.tenant_id)
    if not plan:
        return jsonify({"success": False, "error": "plan_not_found"}), 404

    # TENANT SCOPE: only count assignments among THIS tenant's users.
    user_count = User.query.filter_by(
        tenant_id=admin.tenant_id, plan=plan.slug
    ).count()
    if user_count > 0:
        return jsonify({
            "success": False,
            "error": "plan_in_use",
            "user_count": user_count,
        }), 400

    try:
        PlanFeatureAccess.query.filter_by(plan_id=plan.id).delete(
            synchronize_session=False
        )
        db.session.delete(plan)
        db.session.commit()
    except Exception:
        db.session.rollback()
        logger.exception(
            "private_slot_delete_plan failed tenant=%s plan=%s",
            admin.tenant_id, plan_id,
        )
        return jsonify({"success": False, "error": "internal_error"}), 500

    return jsonify({"success": True})


@tenant_private_slot_bp.route("/plans/<int:plan_id>/features", methods=["PUT"])
@require_tenant_admin
def private_slot_update_plan_features(admin, plan_id):
    """Replace/upsert PlanFeatureAccess for a private plan owned by THIS tenant."""
    # TENANT SCOPE: plan must be private + tenant_id == admin.tenant_id.
    plan = _get_owned_private_plan(plan_id, admin.tenant_id)
    if not plan:
        return jsonify({"success": False, "error": "plan_not_found"}), 404

    data = request.get_json(silent=True) or {}
    features = data.get("features") or {}
    if not isinstance(features, dict):
        features = {}

    try:
        _apply_plan_features(plan.id, features)
        db.session.commit()
    except Exception:
        db.session.rollback()
        logger.exception(
            "private_slot_update_plan_features failed tenant=%s plan=%s",
            admin.tenant_id, plan_id,
        )
        return jsonify({"success": False, "error": "internal_error"}), 500

    return jsonify({"success": True, "plan": _serialize_private_plan(plan)})
