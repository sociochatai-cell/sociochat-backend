"""
Tenant Module - Tenant Admin Portal + Branding API
==================================================

* Public branding lookup (pre-auth) so the login page can theme by tenant_code.
* Authenticated branding for the current tenant.
* Tenant Admin user management, STRICTLY scoped to the admin's own tenant
  (a tenant admin can never see or touch another tenant's users).
"""

import logging

from flask import Blueprint, jsonify, request, session
from werkzeug.security import generate_password_hash

from models import db, User, Workspace
from tenant.models import Tenant, TenantSubscription
from tenant.context import (
    require_tenant_admin, require_tenant_user, get_current_user,
    generate_password,
)
from tenant import service as tenant_service
from tenant.service import TenantError

logger = logging.getLogger(__name__)

tenant_bp = Blueprint("tenant_portal", __name__, url_prefix="/api/tenant")

# A tenant admin may only assign these roles inside their tenant.
_ASSIGNABLE_TENANT_ROLES = ("user", "tenant_admin")


def _serialize_tenant_user(u: User) -> dict:
    return {
        "id": u.id,
        "name": u.name,
        "email": u.email,
        "role": u.role,
        "status": u.status,
        "plan": u.plan or "beta",
        "business_name": u.business_name,
        "created_at": u.created_at.isoformat() if u.created_at else None,
    }


# --------------------------------------------------------------------------- #
# Branding
# --------------------------------------------------------------------------- #
@tenant_bp.route("/branding/by-code/<tenant_code>", methods=["GET"])
def branding_by_code(tenant_code):
    """PUBLIC: branding for a tenant code (used to theme the login page)."""
    code = (tenant_code or "").strip().upper()
    tenant = Tenant.query.filter_by(tenant_code=code).first()
    if not tenant:
        return jsonify({"success": False, "error": "tenant_not_found"}), 404
    return jsonify({"success": True, "tenant": tenant.public_branding()})


@tenant_bp.route("/branding", methods=["GET"])
@require_tenant_user
def current_branding(user):
    """Branding for the authenticated user's tenant."""
    if not getattr(user, "tenant_id", None):
        return jsonify({"success": False, "error": "no_tenant"}), 404
    tenant = db.session.get(Tenant, user.tenant_id)
    if not tenant:
        return jsonify({"success": False, "error": "tenant_not_found"}), 404
    return jsonify({
        "success": True,
        "tenant_code": tenant.tenant_code,
        "company_name": tenant.company_name,
        "branding": tenant.branding_dict(),
    })


# --------------------------------------------------------------------------- #
# Tenant Admin overview
# --------------------------------------------------------------------------- #
@tenant_bp.route("/admin/overview", methods=["GET"])
@require_tenant_admin
def admin_overview(admin_user):
    tenant = db.session.get(Tenant, admin_user.tenant_id)
    if not tenant:
        return jsonify({"success": False, "error": "tenant_not_found"}), 404
    sub = TenantSubscription.query.filter_by(tenant_id=tenant.id).first()
    user_count = User.query.filter_by(tenant_id=tenant.id).count()
    return jsonify({
        "success": True,
        "tenant": tenant.serialize(),
        "subscription": sub.serialize() if sub else None,
        # The resolved white-label LICENSE (name/price/limits) for display.
        "tenant_plan": tenant_service.resolve_tenant_plan(
            sub.plan_slug if sub else tenant.subscription_plan
        ),
        "users_count": user_count,
    })


# --------------------------------------------------------------------------- #
# Tenant Admin — own white-label LICENSE (view + self-service switch)
# --------------------------------------------------------------------------- #
@tenant_bp.route("/admin/subscription", methods=["GET"])
@require_tenant_admin
def admin_get_subscription(admin_user):
    """The tenant's OWN white-label license: current plan + assignable catalog."""
    tenant = db.session.get(Tenant, admin_user.tenant_id)
    if not tenant:
        return jsonify({"success": False, "error": "tenant_not_found"}), 404
    sub = TenantSubscription.query.filter_by(tenant_id=tenant.id).first()
    return jsonify({
        "success": True,
        "subscription": sub.serialize() if sub else None,
        "tenant_plan": tenant_service.resolve_tenant_plan(
            sub.plan_slug if sub else tenant.subscription_plan
        ),
        "available_plans": tenant_service.get_assignable_tenant_plans(tenant.id),
    })


@tenant_bp.route("/admin/subscription", methods=["POST"])
@require_tenant_admin
def admin_change_subscription(admin_user):
    """Tenant owner self-selects a white-label license.

    PLACEHOLDER for the future payment gateway: the chosen plan is applied and
    marked ``payment_status='pending'`` (a real gateway will confirm/activate it
    later). It does NOT touch any end-user plans.
    """
    tenant = db.session.get(Tenant, admin_user.tenant_id)
    if not tenant:
        return jsonify({"success": False, "error": "tenant_not_found"}), 404
    data = request.get_json(silent=True) or {}
    slug = (data.get("plan_slug") or data.get("plan") or "").strip()
    if not slug:
        return jsonify({"success": False, "error": "plan_required"}), 400
    if not tenant_service.is_tenant_plan_assignable(tenant.id, slug):
        return jsonify({"success": False, "error": "invalid_plan"}), 400

    # Payment gate: a PRICED license must be paid via PayU (settles to the
    # platform) and only activates in the verified callback. Free/custom
    # (price 0 / null, e.g. enterprise) plans are applied directly as a pending
    # choice for manual super-admin confirmation.
    from tenant.tenant_plan_models import TenantPlan
    plan_row = TenantPlan.query.filter_by(slug=slug).first()
    price = plan_row.price_inr if plan_row else None
    if price and price > 0:
        return jsonify({
            "success": False,
            "error": "payment_required",
            "requires_payment": True,
            "plan_slug": slug,
        }), 402

    # admin=None: changed_by_admin_id FKs the Admin realm, not tenant users.
    sub = tenant_service.set_tenant_subscription(
        tenant, slug, admin=None, reason="tenant_self_select", payment_status="pending",
    )
    return jsonify({
        "success": True,
        "subscription": sub.serialize(),
        "tenant_plan": tenant_service.resolve_tenant_plan(slug),
    })


@tenant_bp.route("/admin/subscription/checkout", methods=["POST"])
@require_tenant_admin
def admin_subscription_checkout(admin_user):
    """Step 1 of the payment flow (PLACEHOLDER): record the chosen plan as
    'pending' and return the amount due. When a real gateway is added, create the
    order here and return its checkout URL / order id."""
    tenant = db.session.get(Tenant, admin_user.tenant_id)
    if not tenant:
        return jsonify({"success": False, "error": "tenant_not_found"}), 404
    data = request.get_json(silent=True) or {}
    slug = (data.get("plan_slug") or data.get("plan") or "").strip()
    if not slug:
        return jsonify({"success": False, "error": "plan_required"}), 400
    if not tenant_service.is_tenant_plan_assignable(tenant.id, slug):
        return jsonify({"success": False, "error": "invalid_plan"}), 400
    plan = tenant_service.resolve_tenant_plan(slug) or {}
    # Record the intent as pending — NOT active until payment is confirmed.
    tenant_service.set_tenant_subscription(
        tenant, slug, admin=None, reason="tenant_checkout", payment_status="pending",
    )
    return jsonify({
        "success": True,
        "checkout": {
            "plan_slug": slug,
            "plan_name": plan.get("name"),
            "amount_inr": plan.get("price_inr"),
            "billing_period": plan.get("billing_period"),
            "currency": "INR",
            "status": "pending",
            "gateway": None,  # no gateway configured yet (placeholder)
        },
    })


@tenant_bp.route("/admin/subscription/confirm", methods=["POST"])
@require_tenant_admin
def admin_subscription_confirm(admin_user):
    """DEPRECATED placeholder. A PRICED license can ONLY be activated by a
    verified PayU payment (payments/payu/return|webhook), never here — otherwise
    this would be a free-license bypass. Free/custom (price 0/null) plans may
    still be applied directly."""
    tenant = db.session.get(Tenant, admin_user.tenant_id)
    if not tenant:
        return jsonify({"success": False, "error": "tenant_not_found"}), 404
    data = request.get_json(silent=True) or {}
    slug = (data.get("plan_slug") or data.get("plan") or "").strip()
    if not slug:
        return jsonify({"success": False, "error": "plan_required"}), 400
    if not tenant_service.is_tenant_plan_assignable(tenant.id, slug):
        return jsonify({"success": False, "error": "invalid_plan"}), 400

    from tenant.tenant_plan_models import TenantPlan
    plan_row = TenantPlan.query.filter_by(slug=slug).first()
    price = plan_row.price_inr if plan_row else None
    if price and price > 0:
        # No free activation of a paid plan — must go through PayU.
        return jsonify({
            "success": False,
            "error": "payment_required",
            "requires_payment": True,
            "plan_slug": slug,
        }), 402

    sub = tenant_service.set_tenant_subscription(
        tenant, slug, admin=None, reason="tenant_self_select", payment_status="none",
    )
    return jsonify({
        "success": True,
        "subscription": sub.serialize(),
        "tenant_plan": tenant_service.resolve_tenant_plan(slug),
    })


# --------------------------------------------------------------------------- #
# Tenant Admin user management (own tenant only)
# --------------------------------------------------------------------------- #
@tenant_bp.route("/admin/users", methods=["GET"])
@require_tenant_admin
def list_tenant_users(admin_user):
    users = (User.query
             .filter_by(tenant_id=admin_user.tenant_id)
             .order_by(User.created_at.desc())
             .all())
    return jsonify({"success": True, "users": [_serialize_tenant_user(u) for u in users]})


@tenant_bp.route("/admin/users", methods=["POST"])
@require_tenant_admin
def create_tenant_user(admin_user):
    tenant = db.session.get(Tenant, admin_user.tenant_id)
    if not tenant:
        return jsonify({"success": False, "error": "tenant_not_found"}), 404

    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or generate_password()
    role = (data.get("role") or "user").strip()
    if role not in _ASSIGNABLE_TENANT_ROLES:
        role = "user"
    if not email:
        return jsonify({"success": False, "error": "email_required"}), 400

    try:
        user = tenant_service.create_tenant_user(
            tenant, name or email, email, password, role=role, status="active",
        )
        db.session.commit()
    except TenantError as e:
        db.session.rollback()
        return jsonify({"success": False, "error": e.code}), e.status
    except Exception:
        db.session.rollback()
        logger.exception("create_tenant_user failed")
        return jsonify({"success": False, "error": "internal_error"}), 500

    resp = {"success": True, "user": _serialize_tenant_user(user)}
    # Surface the generated password once if the admin didn't set one.
    if not data.get("password"):
        resp["generated_password"] = password
    return jsonify(resp), 201


@tenant_bp.route("/admin/users/<int:user_id>", methods=["PATCH"])
@require_tenant_admin
def update_tenant_user(admin_user, user_id):
    target = db.session.get(User, user_id)
    if not target or target.tenant_id != admin_user.tenant_id:
        return jsonify({"success": False, "error": "user_not_found"}), 404

    data = request.get_json(silent=True) or {}
    if "name" in data and data["name"]:
        target.name = data["name"].strip()
    if "status" in data and data["status"]:
        target.status = data["status"]
    if "role" in data and data["role"] in _ASSIGNABLE_TENANT_ROLES:
        target.role = data["role"]
    if data.get("password"):
        target.password_hash = generate_password_hash(data["password"])
    db.session.commit()
    return jsonify({"success": True, "user": _serialize_tenant_user(target)})


@tenant_bp.route("/admin/users/<int:user_id>/reset-password", methods=["POST"])
@require_tenant_admin
def reset_tenant_user_password(admin_user, user_id):
    target = db.session.get(User, user_id)
    if not target or target.tenant_id != admin_user.tenant_id:
        return jsonify({"success": False, "error": "user_not_found"}), 404
    new_password = generate_password()
    target.password_hash = generate_password_hash(new_password)
    db.session.commit()
    return jsonify({"success": True, "password": new_password})


@tenant_bp.route("/admin/users/<int:user_id>", methods=["DELETE"])
@require_tenant_admin
def delete_tenant_user(admin_user, user_id):
    # Destructive action — require the admin to re-enter their OWN password.
    from werkzeug.security import check_password_hash
    confirm_pw = (
        request.headers.get("X-Confirm-Password")
        or (request.get_json(silent=True) or {}).get("password")
        or ""
    )
    if not confirm_pw or not check_password_hash(admin_user.password_hash, confirm_pw):
        return jsonify({"success": False, "error": "invalid_password"}), 403

    if user_id == admin_user.id:
        return jsonify({"success": False, "error": "cannot_delete_self"}), 400
    target = db.session.get(User, user_id)
    if not target or target.tenant_id != admin_user.tenant_id:
        return jsonify({"success": False, "error": "user_not_found"}), 404

    # Don't allow removing the tenant's last admin.
    if target.role == "tenant_admin":
        remaining_admins = (User.query
                            .filter_by(tenant_id=admin_user.tenant_id, role="tenant_admin")
                            .count())
        if remaining_admins <= 1:
            return jsonify({"success": False, "error": "cannot_delete_last_admin"}), 400

    Workspace.query.filter_by(user_id=target.id).delete(synchronize_session=False)
    db.session.delete(target)
    db.session.commit()
    return jsonify({"success": True})


# --------------------------------------------------------------------------- #
# Tenant Admin impersonation (own tenant only)
# --------------------------------------------------------------------------- #
@tenant_bp.route("/admin/users/<int:user_id>/impersonate", methods=["POST"])
@require_tenant_admin
def impersonate_tenant_user(admin_user, user_id):
    """Log in as one of the admin's OWN tenant users (scoped to the tenant)."""
    target = db.session.get(User, user_id)
    if not target or target.tenant_id != admin_user.tenant_id:
        return jsonify({"success": False, "error": "user_not_in_tenant"}), 404

    tenant = db.session.get(Tenant, admin_user.tenant_id)
    if not tenant:
        return jsonify({"success": False, "error": "tenant_not_found"}), 404

    # Switch the server session to the target user (impersonate within tenant).
    session["user_id"] = target.id
    session.modified = True

    workspaces = Workspace.query.filter_by(user_id=target.id).all()
    return jsonify({
        "success": True,
        "user": {
            "id": target.id,
            "name": target.name,
            "email": target.email,
            "role": target.role,
            "tenant_id": target.tenant_id,
        },
        "tenant": {
            "tenant_code": tenant.tenant_code,
            "company_name": tenant.company_name,
            "branding": tenant.branding_dict(),
        },
        "workspaces": [
            {"id": w.id, "business_name": w.business_name} for w in workspaces
        ],
    })


# --------------------------------------------------------------------------- #
# Tenant Admin — per-user usage (own tenant only)
# --------------------------------------------------------------------------- #
@tenant_bp.route("/admin/users/<int:user_id>/usage", methods=["GET"])
@require_tenant_admin
def admin_get_user_usage(admin_user, user_id):
    """Usage/exhaustion stats for one of the admin's OWN tenant users."""
    target = db.session.get(User, user_id)
    if not target or target.tenant_id != admin_user.tenant_id:
        return jsonify({"success": False, "error": "user_not_in_tenant"}), 404

    from subscription.service import get_user_usage_stats
    return jsonify({"success": True, **get_user_usage_stats(target)})


# --------------------------------------------------------------------------- #
# Tenant Admin plan & features view (read-only, own tenant only)
# --------------------------------------------------------------------------- #
@tenant_bp.route("/admin/plan", methods=["GET"])
@require_tenant_admin
def admin_plan(admin_user):
    """Read-only view of the tenant's effective plan, limits and feature flags."""
    from subscription.service import LIMIT_KEYS

    tenant = db.session.get(Tenant, admin_user.tenant_id)
    if not tenant:
        return jsonify({"success": False, "error": "tenant_not_found"}), 404

    sub = TenantSubscription.query.filter_by(tenant_id=tenant.id).first()
    matrix = tenant_service.tenant_feature_matrix(tenant)

    limits = {}
    features = {}
    for key, val in matrix.items():
        if key in LIMIT_KEYS:
            limits[key] = val
        else:
            features[key] = bool(val)

    users_count = User.query.filter_by(tenant_id=tenant.id).count()
    license_slug = sub.plan_slug if sub else tenant.subscription_plan
    return jsonify({
        "success": True,
        # The tenant's white-label LICENSE (clearly labeled — NOT an end-user plan).
        "license_plan_slug": license_slug,
        "license_plan": tenant_service.resolve_tenant_plan(license_slug),
        # Baseline END-USER plan slug the matrix below is derived from.
        "plan_slug": tenant_service.tenant_baseline_user_plan_slug(tenant),
        "subscription_expires_at": (
            sub.subscription_expires_at.isoformat()
            if sub and sub.subscription_expires_at else None
        ),
        "limits": limits,
        "features": features,
        "users_count": users_count,
    })


# --------------------------------------------------------------------------- #
# Tenant Admin subscription & per-user feature management (own tenant only)
# --------------------------------------------------------------------------- #
# Limit-type feature keys (mirror subscription.service.LIMIT_KEYS). Defined
# locally so the catalog fallback works even if the service import fails.
_LIMIT_KEYS = {
    "workspaces", "users", "messages_per_day",
    "interactive_flows", "image_credits", "ad_spend_limit",
}


_VALID_BILLING_PERIODS = ("monthly", "quarterly", "yearly")


def _normalize_billing_period(value) -> str:
    """Normalize billing_period to {monthly, quarterly, yearly}; else 'monthly'."""
    period = (str(value or "")).strip().lower()
    return period if period in _VALID_BILLING_PERIODS else "monthly"


def _tenant_slugify(value: str) -> str:
    import re
    return re.sub(r"[^a-z0-9]+", "-", (value or "").lower()).strip("-")


def _tenant_unique_plan_slug(base: str) -> str:
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


def _custom_plan_brief(plan) -> dict:
    """Serialize a tenant custom plan + its PlanFeatureAccess matrix.

    The ``features`` map (keyed by feature key) lets the tenant-admin Plan &
    Features matrix editor prefill from a plan's current access/limit values.
    """
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
        "is_active": bool(getattr(plan, "is_active", True)),
        "features": features,
    }


def _get_owned_custom_plan(plan_id, tenant_id):
    """Return a tenant custom plan ONLY if it belongs to this tenant, else None.

    Tenant custom plans are created with ``plan_scope='private'`` +
    ``tenant_id`` set (see ``admin_create_plan``). Mirrors the private-slot
    ``_get_owned_private_plan`` ownership guard, scoped to this tenant.
    """
    from subscription.plan_models import SubscriptionPlan

    plan = db.session.get(SubscriptionPlan, plan_id)
    if not plan or plan.tenant_id != tenant_id:
        return None
    return plan


def _apply_tenant_plan_features(plan_id, features):
    """Replace/upsert a plan's PlanFeatureAccess rows from a {key: spec} dict.

    Mirrors the feature-writing logic used by ``admin_create_plan`` (and the
    private-slot ``_apply_plan_features``): upsert each row so an existing
    matrix is updated in place rather than duplicated.
    """
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


@tenant_bp.route("/admin/features-catalog", methods=["GET"])
@require_tenant_admin
def admin_features_catalog(admin_user):
    """Feature catalog (DB-driven, constants fallback) for building plans."""
    features = []
    try:
        from subscription.plan_models import SubscriptionFeature
        rows = SubscriptionFeature.query.filter_by(is_active=True).all()
        features = [
            {
                "key": r.key,
                "label": getattr(r, "label", r.key),
                "category": getattr(r, "category", "general"),
                "feature_type": getattr(r, "feature_type", "access"),
            }
            for r in rows
        ]
    except Exception:
        logger.exception("tenant feature catalog from DB failed; using constants")

    if not features:
        try:
            from subscription.constants import PLAN_FEATURES, PLAN_ENTERPRISE  # type: ignore
        except Exception:
            PLAN_FEATURES, PLAN_ENTERPRISE = {}, "enterprise"
        sample = PLAN_FEATURES.get(PLAN_ENTERPRISE, {}) if isinstance(PLAN_FEATURES, dict) else {}
        for key in sample:
            is_limit = key in _LIMIT_KEYS
            features.append({
                "key": key,
                "label": key.replace("_", " ").title(),
                "category": "limits" if is_limit else "features",
                "feature_type": "limit" if is_limit else "access",
            })

    return jsonify({"success": True, "features": features})


@tenant_bp.route("/admin/plans", methods=["GET"])
@require_tenant_admin
def admin_list_plans(admin_user):
    """This tenant's custom (private) plans plus the global base plan catalog."""
    from subscription.plan_models import SubscriptionPlan

    custom_plans = [
        _custom_plan_brief(p)
        for p in SubscriptionPlan.query
        .filter_by(tenant_id=admin_user.tenant_id)
        .order_by(SubscriptionPlan.created_at.asc())
        .all()
    ]

    # SocioChat's global base plans must NOT leak into the tenant-admin view.
    # Keep the key for response-shape stability; tenants only see custom plans.
    base_plans = []

    return jsonify({
        "success": True,
        "custom_plans": custom_plans,
        "base_plans": base_plans,
    })


@tenant_bp.route("/admin/plans", methods=["POST"])
@require_tenant_admin
def admin_create_plan(admin_user):
    """Create a tenant-only custom plan (+ its PlanFeatureAccess matrix)."""
    from subscription.plan_models import SubscriptionPlan

    tenant = db.session.get(Tenant, admin_user.tenant_id)
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
    base_slug = f"t{tenant_code}_{_tenant_slugify(name)}"
    slug = _tenant_unique_plan_slug(base_slug)

    try:
        plan = SubscriptionPlan(
            slug=slug,
            name=name,
            offer_text=(data.get("offer_text") or None),
            price_monthly_inr=price,
            billing_period=_normalize_billing_period(data.get("billing_period")),
            is_public=False,
            is_active=True,
            plan_scope="private",
            tenant_id=admin_user.tenant_id,
        )
        db.session.add(plan)
        db.session.flush()  # need plan.id for feature access rows

        _apply_tenant_plan_features(plan.id, features)

        db.session.commit()
    except Exception:
        db.session.rollback()
        logger.exception("admin_create_plan failed tenant=%s", admin_user.tenant_id)
        return jsonify({"success": False, "error": "internal_error"}), 500

    return jsonify({"success": True, "plan": _custom_plan_brief(plan)}), 201


@tenant_bp.route("/admin/plans/<int:plan_id>", methods=["DELETE"])
@require_tenant_admin
def admin_delete_plan(admin_user, plan_id):
    """Delete a custom plan owned by THIS tenant (never globals/other tenants)."""
    from subscription.plan_models import SubscriptionPlan, PlanFeatureAccess

    plan = db.session.get(SubscriptionPlan, plan_id)
    if not plan or plan.tenant_id != admin_user.tenant_id:
        return jsonify({"success": False, "error": "plan_not_found"}), 404

    try:
        PlanFeatureAccess.query.filter_by(plan_id=plan.id).delete(synchronize_session=False)
        db.session.delete(plan)
        db.session.commit()
    except Exception:
        db.session.rollback()
        logger.exception("admin_delete_plan failed tenant=%s plan=%s",
                         admin_user.tenant_id, plan_id)
        return jsonify({"success": False, "error": "internal_error"}), 500

    return jsonify({"success": True})


@tenant_bp.route("/admin/plans/<int:plan_id>", methods=["PUT"])
@require_tenant_admin
def admin_update_plan(admin_user, plan_id):
    """Update name / active state of a custom plan owned by THIS tenant.

    TENANT SCOPE: only a plan whose ``tenant_id == admin_user.tenant_id`` may be
    edited; anything else (globals / other tenants) returns 404.
    """
    plan = _get_owned_custom_plan(plan_id, admin_user.tenant_id)
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

    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        logger.exception("admin_update_plan failed tenant=%s plan=%s",
                         admin_user.tenant_id, plan_id)
        return jsonify({"success": False, "error": "internal_error"}), 500

    return jsonify({"success": True, "plan": _custom_plan_brief(plan)})


@tenant_bp.route("/admin/plans/<int:plan_id>/features", methods=["PUT"])
@require_tenant_admin
def admin_update_plan_features(admin_user, plan_id):
    """Replace/upsert the PlanFeatureAccess matrix for THIS tenant's plan.

    TENANT SCOPE: only a plan owned by ``admin_user.tenant_id`` may be written.
    """
    plan = _get_owned_custom_plan(plan_id, admin_user.tenant_id)
    if not plan:
        return jsonify({"success": False, "error": "plan_not_found"}), 404

    data = request.get_json(silent=True) or {}
    features = data.get("features") or {}
    if not isinstance(features, dict):
        features = {}

    try:
        _apply_tenant_plan_features(plan.id, features)
        db.session.commit()
    except Exception:
        db.session.rollback()
        logger.exception("admin_update_plan_features failed tenant=%s plan=%s",
                         admin_user.tenant_id, plan_id)
        return jsonify({"success": False, "error": "internal_error"}), 500

    return jsonify({"success": True, "plan": _custom_plan_brief(plan)})


@tenant_bp.route("/admin/users/<int:user_id>/plan", methods=["PUT"])
@require_tenant_admin
def admin_set_user_plan(admin_user, user_id):
    """Assign a plan slug to one of THIS tenant's users (per-user override)."""
    from subscription.constants import VALID_PLANS
    from subscription.plan_models import SubscriptionPlan

    target = db.session.get(User, user_id)
    if not target or target.tenant_id != admin_user.tenant_id:
        return jsonify({"success": False, "error": "user_not_in_tenant"}), 404

    data = request.get_json(silent=True) or {}
    plan_slug = (data.get("plan_slug") or "").strip()
    if not plan_slug:
        return jsonify({"success": False, "error": "invalid_plan"}), 400

    valid = plan_slug in VALID_PLANS
    if not valid:
        # Allow a custom plan ONLY if it belongs to this admin's tenant.
        custom = SubscriptionPlan.query.filter_by(
            slug=plan_slug, tenant_id=admin_user.tenant_id
        ).first()
        valid = custom is not None
    if not valid:
        return jsonify({"success": False, "error": "invalid_plan"}), 400

    target.plan = plan_slug
    db.session.commit()

    return jsonify({
        "success": True,
        "user": {
            "id": target.id,
            "name": target.name,
            "email": target.email,
            "role": target.role,
            "status": target.status,
            "plan": target.plan,
        },
    })


@tenant_bp.route("/admin/users/<int:user_id>/features", methods=["GET"])
@require_tenant_admin
def admin_get_user_features(admin_user, user_id):
    """Effective access flags + explicit per-user overrides for a tenant user."""
    from subscription.service import (
        load_user_matrix, load_plan_matrix, get_user_plan, LIMIT_KEYS,
    )
    from subscription.plan_models import UserFeatureAccess

    target = db.session.get(User, user_id)
    if not target or target.tenant_id != admin_user.tenant_id:
        return jsonify({"success": False, "error": "user_not_in_tenant"}), 404

    matrix = load_user_matrix(target)
    features = {
        key: bool(val)
        for key, val in matrix.items()
        if key not in LIMIT_KEYS
    }

    # PLAN access-feature defaults (BEFORE per-user overrides). Lets the UI show
    # the "Plan default (On/Off)" baseline for the inherit option. Mirrors the
    # platform super-admin handler's plan_defaults computation.
    plan_matrix = load_plan_matrix(get_user_plan(target))
    plan_defaults = {
        key: bool(val)
        for key, val in plan_matrix.items()
        if key not in LIMIT_KEYS
    }

    overrides = [
        {"feature_key": ov.feature_key, "enabled": bool(ov.enabled)}
        for ov in UserFeatureAccess.query.filter_by(user_id=target.id).all()
    ]

    return jsonify({
        "success": True,
        "plan": target.plan,
        "features": features,
        "plan_defaults": plan_defaults,
        "overrides": overrides,
    })


@tenant_bp.route("/admin/users/<int:user_id>/features", methods=["PUT"])
@require_tenant_admin
def admin_set_user_features(admin_user, user_id):
    """Upsert per-user access-type feature overrides for a tenant user.

    Body: {"overrides": {"<feature_key>": true|false|null}}
      - true/false -> force ON/OFF (upsert; backward compatible)
      - null        -> clear the override (revert to plan inherit)
    """
    from subscription.service import LIMIT_KEYS
    from subscription.plan_models import UserFeatureAccess

    target = db.session.get(User, user_id)
    if not target or target.tenant_id != admin_user.tenant_id:
        return jsonify({"success": False, "error": "user_not_in_tenant"}), 404

    data = request.get_json(silent=True) or {}
    overrides = data.get("overrides") or {}
    if not isinstance(overrides, dict):
        overrides = {}

    try:
        for key, value in overrides.items():
            if key in LIMIT_KEYS:
                continue  # access-type overrides only
            row = UserFeatureAccess.query.filter_by(
                user_id=target.id, feature_key=key
            ).first()
            if value is None:
                # null/None -> clear the override (revert to plan inherit).
                if row:
                    db.session.delete(row)
            elif row:
                row.enabled = bool(value)
            else:
                db.session.add(UserFeatureAccess(
                    user_id=target.id,
                    feature_key=key,
                    enabled=bool(value),
                ))
        db.session.commit()
    except Exception:
        db.session.rollback()
        logger.exception("admin_set_user_features failed tenant=%s user=%s",
                         admin_user.tenant_id, user_id)
        return jsonify({"success": False, "error": "internal_error"}), 500

    return jsonify({"success": True})
