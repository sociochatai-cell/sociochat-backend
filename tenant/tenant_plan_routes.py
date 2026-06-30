"""
Tenant Module - White-Label License Catalog (Super Admin API)
=============================================================

CRUD for ``TenantPlan`` — the white-label LICENSE plans a tenant subscribes to
(distinct from end-user ``SubscriptionPlan``). Two scopes:

* Universal catalog  -> ``tenant_id IS NULL``  (every tenant can be put on it).
* Custom per-tenant  -> ``tenant_id = <id>``   (negotiated for one tenant).

Assigning a license to a tenant is done via the existing
``PUT /api/superadmin/tenants/<id>/subscription`` (which now takes a TenantPlan
slug). These routes only manage the CATALOG.
"""

import re
import logging
import secrets

from flask import Blueprint, jsonify, request

from models import db
from tenant.context import require_super_admin
from tenant.tenant_plan_models import TenantPlan

logger = logging.getLogger(__name__)

tenant_subscription_plans_bp = Blueprint(
    "tenant_subscription_plans", __name__, url_prefix="/api/superadmin"
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _slugify(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (value or "").lower()).strip("-")


def _unique_slug(base: str) -> str:
    base = (base or "plan")[:40].strip("-") or "plan"
    if not TenantPlan.query.filter_by(slug=base).first():
        return base
    for _ in range(40):
        candidate = f"{base[:36].strip('-')}-{secrets.token_hex(2)}"[:48]
        if not TenantPlan.query.filter_by(slug=candidate).first():
            return candidate
    return f"{base[:30].strip('-')}-{secrets.token_hex(6)}"[:48]


def _coerce_int(val, default=None):
    if val in (None, ""):
        return default
    try:
        return int(val)
    except (TypeError, ValueError):
        return default


def _normalize_features(raw) -> dict:
    """Normalize a feature matrix to ``{ key: {enabled, limit_value} }``.

    Accepts a dict ``{ feature_key: spec }`` where ``spec`` is either a dict
    ``{enabled?, limit_value?}`` or a bare bool (treated as ``enabled``). Anything
    that is not a dict yields an empty matrix.
    """
    if not isinstance(raw, dict):
        return {}
    out = {}
    for key, spec in raw.items():
        if isinstance(spec, dict):
            enabled = spec.get("enabled")
            enabled = None if enabled is None else bool(enabled)
            limit_value = _coerce_int(spec.get("limit_value"), None)
        else:
            enabled = bool(spec)
            limit_value = None
        out[str(key)] = {"enabled": enabled, "limit_value": limit_value}
    return out


def _apply_payload(plan: TenantPlan, data: dict) -> None:
    if "name" in data and (data.get("name") or "").strip():
        plan.name = data["name"].strip()
    if "description" in data:
        plan.description = data.get("description") or None
    if "price_inr" in data:
        plan.price_inr = _coerce_int(data.get("price_inr"), None)
    if "billing_period" in data and data.get("billing_period"):
        plan.billing_period = str(data["billing_period"]).strip()
    if "max_end_users" in data:
        plan.max_end_users = _coerce_int(data.get("max_end_users"), -1)
    if "max_workspaces" in data:
        plan.max_workspaces = _coerce_int(data.get("max_workspaces"), -1)
    if "is_active" in data:
        plan.is_active = bool(data.get("is_active"))
    if "is_public" in data:
        plan.is_public = bool(data.get("is_public"))
    if "sort_order" in data:
        plan.sort_order = _coerce_int(data.get("sort_order"), 0) or 0
    if "features" in data:
        plan.features = _normalize_features(data.get("features"))


# --------------------------------------------------------------------------- #
# Universal catalog (tenant_id IS NULL)
# --------------------------------------------------------------------------- #
@tenant_subscription_plans_bp.route("/tenant-plans", methods=["GET"])
@require_super_admin
def list_tenant_plans(admin):
    rows = (
        TenantPlan.query
        .filter(TenantPlan.tenant_id.is_(None))
        .order_by(TenantPlan.sort_order.asc(), TenantPlan.id.asc())
        .all()
    )
    return jsonify({"success": True, "plans": [p.serialize() for p in rows]})


@tenant_subscription_plans_bp.route("/tenant-plans", methods=["POST"])
@require_super_admin
def create_tenant_plan(admin):
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"success": False, "error": "name_required"}), 400

    slug = (data.get("slug") or "").strip() or _slugify(name)
    slug = _unique_slug(f"wl_{slug}" if not slug.startswith("wl_") else slug)

    plan = TenantPlan(slug=slug, name=name, tenant_id=None)
    _apply_payload(plan, data)
    db.session.add(plan)
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        logger.exception("create_tenant_plan failed")
        return jsonify({"success": False, "error": "internal_error"}), 500
    return jsonify({"success": True, "plan": plan.serialize()}), 201


@tenant_subscription_plans_bp.route("/tenant-plans/<int:plan_id>", methods=["PUT", "PATCH"])
@require_super_admin
def update_tenant_plan(admin, plan_id):
    plan = db.session.get(TenantPlan, plan_id)
    if not plan or plan.tenant_id is not None:
        return jsonify({"success": False, "error": "plan_not_found"}), 404
    _apply_payload(plan, request.get_json(silent=True) or {})
    db.session.commit()
    return jsonify({"success": True, "plan": plan.serialize()})


@tenant_subscription_plans_bp.route("/tenant-plans/<int:plan_id>", methods=["DELETE"])
@require_super_admin
def delete_tenant_plan(admin, plan_id):
    plan = db.session.get(TenantPlan, plan_id)
    if not plan or plan.tenant_id is not None:
        return jsonify({"success": False, "error": "plan_not_found"}), 404
    db.session.delete(plan)
    db.session.commit()
    return jsonify({"success": True})


# --------------------------------------------------------------------------- #
# Per-tenant view + custom license plans
# --------------------------------------------------------------------------- #
@tenant_subscription_plans_bp.route("/tenants/<int:tenant_id>/tenant-plans", methods=["GET"])
@require_super_admin
def list_assignable_tenant_plans(admin, tenant_id):
    """Universal catalog + this tenant's custom license plans (for the picker)."""
    from tenant.models import Tenant
    if not db.session.get(Tenant, tenant_id):
        return jsonify({"success": False, "error": "tenant_not_found"}), 404

    universal = (
        TenantPlan.query
        .filter(TenantPlan.tenant_id.is_(None), TenantPlan.is_active.is_(True))
        .order_by(TenantPlan.sort_order.asc(), TenantPlan.id.asc())
        .all()
    )
    custom = (
        TenantPlan.query
        .filter_by(tenant_id=tenant_id)
        .order_by(TenantPlan.created_at.asc())
        .all()
    )
    return jsonify({
        "success": True,
        "universal_plans": [p.serialize() for p in universal],
        "custom_plans": [p.serialize() for p in custom],
    })


@tenant_subscription_plans_bp.route("/tenants/<int:tenant_id>/tenant-plans", methods=["POST"])
@require_super_admin
def create_custom_tenant_plan(admin, tenant_id):
    from tenant.models import Tenant
    tenant = db.session.get(Tenant, tenant_id)
    if not tenant:
        return jsonify({"success": False, "error": "tenant_not_found"}), 404

    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"success": False, "error": "name_required"}), 400

    code = (getattr(tenant, "tenant_code", "") or "").lower()
    slug = _unique_slug(f"wl_t{code}_{_slugify(name)}")
    plan = TenantPlan(slug=slug, name=name, tenant_id=tenant_id, is_public=False)
    _apply_payload(plan, data)
    db.session.add(plan)
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        logger.exception("create_custom_tenant_plan failed tenant=%s", tenant_id)
        return jsonify({"success": False, "error": "internal_error"}), 500
    return jsonify({"success": True, "plan": plan.serialize()}), 201


@tenant_subscription_plans_bp.route(
    "/tenants/<int:tenant_id>/tenant-plans/<int:plan_id>", methods=["DELETE"]
)
@require_super_admin
def delete_custom_tenant_plan(admin, tenant_id, plan_id):
    plan = db.session.get(TenantPlan, plan_id)
    if not plan or plan.tenant_id != tenant_id:
        return jsonify({"success": False, "error": "plan_not_found"}), 404
    db.session.delete(plan)
    db.session.commit()
    return jsonify({"success": True})
