"""
Tenant Module - Super Admin API
===============================

Platform-owner-only endpoints for tenant management. Every route is gated by
``require_super_admin`` (the platform Admin realm). No tenant user or tenant
admin can reach these.
"""

import logging
from datetime import datetime

from flask import Blueprint, jsonify, request, session

from models import db, User, Workspace
from tenant.models import Tenant, TenantSubscription, TenantFeatureOverride
from tenant.context import require_super_admin
from tenant import service as tenant_service
from tenant.service import TenantError

logger = logging.getLogger(__name__)

superadmin_bp = Blueprint("superadmin", __name__, url_prefix="/api/superadmin")


def _parse_dt(val):
    if not val:
        return None
    try:
        return datetime.fromisoformat(str(val).replace("Z", "+00:00"))
    except ValueError:
        return None


# --------------------------------------------------------------------------- #
# Tenant CRUD
# --------------------------------------------------------------------------- #
@superadmin_bp.route("/tenants", methods=["GET"])
@require_super_admin
def list_tenants(admin):
    tenants = Tenant.query.order_by(Tenant.created_at.desc()).all()
    # Bulk-fetch each tenant's subscription expiry so every row can show WHEN it
    # lapses, without an N+1 query inside the serialize loop.
    expiry_by_tenant = {
        s.tenant_id: s.subscription_expires_at
        for s in TenantSubscription.query.all()
    }
    out = []
    for t in tenants:
        data = t.serialize()
        exp = expiry_by_tenant.get(t.id)
        data["subscription_expires_at"] = exp.isoformat() if exp else None
        out.append(data)
    return jsonify({"success": True, "tenants": out})


@superadmin_bp.route("/tenants", methods=["POST"])
@require_super_admin
def create_tenant(admin):
    payload = request.get_json(silent=True) or {}
    try:
        tenant, credentials = tenant_service.create_tenant(admin, payload)
    except TenantError as e:
        db.session.rollback()
        return jsonify({"success": False, "error": e.code}), e.status
    except Exception:
        db.session.rollback()
        logger.exception("create_tenant failed")
        return jsonify({"success": False, "error": "internal_error"}), 500
    return jsonify({
        "success": True,
        "tenant": tenant.serialize(),
        "credentials": credentials,
    }), 201


@superadmin_bp.route("/tenants/<int:tenant_id>", methods=["GET"])
@require_super_admin
def get_tenant(admin, tenant_id):
    tenant = db.session.get(Tenant, tenant_id)
    if not tenant:
        return jsonify({"success": False, "error": "tenant_not_found"}), 404
    data = tenant_service.serialize_tenant_detail(tenant)
    return jsonify({"success": True, **data})


@superadmin_bp.route("/tenants/<int:tenant_id>", methods=["PUT", "PATCH"])
@require_super_admin
def update_tenant(admin, tenant_id):
    tenant = db.session.get(Tenant, tenant_id)
    if not tenant:
        return jsonify({"success": False, "error": "tenant_not_found"}), 404
    try:
        tenant_service.update_tenant(tenant, request.get_json(silent=True) or {})
    except TenantError as e:
        db.session.rollback()
        return jsonify({"success": False, "error": e.code}), e.status
    return jsonify({"success": True, "tenant": tenant.serialize()})


@superadmin_bp.route("/tenants/<int:tenant_id>", methods=["DELETE"])
@require_super_admin
def delete_tenant(admin, tenant_id):
    tenant = db.session.get(Tenant, tenant_id)
    if not tenant:
        return jsonify({"success": False, "error": "tenant_not_found"}), 404
    try:
        tenant_service.delete_tenant(tenant)
    except TenantError as e:
        db.session.rollback()
        return jsonify({"success": False, "error": e.code}), e.status
    return jsonify({"success": True})


@superadmin_bp.route("/tenants/<int:tenant_id>/suspend", methods=["POST"])
@require_super_admin
def suspend_tenant(admin, tenant_id):
    tenant = db.session.get(Tenant, tenant_id)
    if not tenant:
        return jsonify({"success": False, "error": "tenant_not_found"}), 404
    tenant_service.suspend_tenant(tenant)
    return jsonify({"success": True, "status": tenant.status})


@superadmin_bp.route("/tenants/<int:tenant_id>/activate", methods=["POST"])
@require_super_admin
def activate_tenant(admin, tenant_id):
    tenant = db.session.get(Tenant, tenant_id)
    if not tenant:
        return jsonify({"success": False, "error": "tenant_not_found"}), 404
    tenant_service.activate_tenant(tenant)
    return jsonify({"success": True, "status": tenant.status})


# --------------------------------------------------------------------------- #
# Subscription + feature overrides
# --------------------------------------------------------------------------- #
@superadmin_bp.route("/tenants/<int:tenant_id>/subscription", methods=["PUT"])
@require_super_admin
def set_subscription(admin, tenant_id):
    tenant = db.session.get(Tenant, tenant_id)
    if not tenant:
        return jsonify({"success": False, "error": "tenant_not_found"}), 404
    data = request.get_json(silent=True) or {}
    plan = data.get("plan_slug") or data.get("plan")
    if not plan:
        return jsonify({"success": False, "error": "plan_required"}), 400
    sub = tenant_service.set_tenant_subscription(
        tenant, plan, expires_at=_parse_dt(data.get("subscription_expires_at")),
        admin=admin, reason=data.get("reason"),
        # A super admin assigning a license = granted/provisioned -> active.
        payment_status=(data.get("payment_status") or "active"),
    )
    return jsonify({"success": True, "subscription": sub.serialize()})


@superadmin_bp.route("/tenants/<int:tenant_id>/features", methods=["PUT"])
@require_super_admin
def set_features(admin, tenant_id):
    tenant = db.session.get(Tenant, tenant_id)
    if not tenant:
        return jsonify({"success": False, "error": "tenant_not_found"}), 404
    data = request.get_json(silent=True) or {}
    overrides = data.get("overrides") or data.get("features") or {}
    tenant_service.apply_feature_overrides(tenant, overrides)
    db.session.commit()
    rows = TenantFeatureOverride.query.filter_by(tenant_id=tenant.id).all()
    return jsonify({"success": True, "feature_overrides": [r.serialize() for r in rows]})


# --------------------------------------------------------------------------- #
# Impersonation
# --------------------------------------------------------------------------- #
@superadmin_bp.route("/tenants/<int:tenant_id>/impersonate", methods=["POST"])
@require_super_admin
def impersonate(admin, tenant_id):
    tenant = db.session.get(Tenant, tenant_id)
    if not tenant:
        return jsonify({"success": False, "error": "tenant_not_found"}), 404

    data = request.get_json(silent=True) or {}
    user_id = data.get("user_id")

    user = None
    if user_id:
        user = db.session.get(User, int(user_id))
        if not user or user.tenant_id != tenant.id:
            return jsonify({"success": False, "error": "user_not_in_tenant"}), 404
    else:
        # Default to the tenant admin, else the first user in the tenant.
        user = (User.query.filter_by(tenant_id=tenant.id, role="tenant_admin").first()
                or User.query.filter_by(tenant_id=tenant.id).first())
    if not user:
        return jsonify({"success": False, "error": "no_user_in_tenant"}), 404

    session["user_id"] = user.id
    # Impersonation is a FULL switch to the user — end the admin realm so the
    # session isn't both admin and user (which made post-payment/redirect land
    # back on the admin dashboard). Mirrors admin_routes.tenant-admin login.
    session.pop("admin_id", None)
    session.modified = True

    # Issue a USER token so the client can replace the admin Bearer — otherwise
    # /auth/me keeps resolving the admin identity (works cross-origin too).
    from auth_core import create_user_token
    user_token = create_user_token(user.id, user.email)

    workspaces = Workspace.query.filter_by(user_id=user.id).all()
    return jsonify({
        "success": True,
        "token": user_token,
        "user": {
            "id": user.id, "name": user.name, "email": user.email,
            "role": user.role, "tenant_id": user.tenant_id,
            "admin_override": True, "admin_override_actor": admin.email,
        },
        "tenant": {
            "tenant_code": tenant.tenant_code,
            "company_name": tenant.company_name,
            "branding": tenant.branding_dict(),
        },
        "workspaces": [{"id": w.id, "business_name": w.business_name} for w in workspaces],
    })


# --------------------------------------------------------------------------- #
# Tenant users (list + password recovery)
# --------------------------------------------------------------------------- #
@superadmin_bp.route("/tenants/<int:tenant_id>/users", methods=["GET"])
@require_super_admin
def list_tenant_users(admin, tenant_id):
    tenant = db.session.get(Tenant, tenant_id)
    if not tenant:
        return jsonify({"success": False, "error": "tenant_not_found"}), 404

    users = User.query.filter_by(tenant_id=tenant_id).all()
    return jsonify({
        "success": True,
        "users": [
            {
                "id": u.id,
                "name": u.name,
                "email": u.email,
                "role": u.role,
                "status": getattr(u, "status", None),
                "created_at": u.created_at.isoformat() if getattr(u, "created_at", None) else None,
            }
            for u in users
        ],
    })


@superadmin_bp.route(
    "/tenants/<int:tenant_id>/users/<int:user_id>/usage", methods=["GET"]
)
@require_super_admin
def get_tenant_user_usage(admin, tenant_id, user_id):
    """Usage/exhaustion stats for a specific user within a tenant."""
    user = db.session.get(User, user_id)
    if not user or user.tenant_id != tenant_id:
        return jsonify({"success": False, "error": "user_not_in_tenant"}), 404

    from subscription.service import get_user_usage_stats
    return jsonify({"success": True, **get_user_usage_stats(user)})


@superadmin_bp.route(
    "/tenants/<int:tenant_id>/users/<int:user_id>/reset-password", methods=["POST"]
)
@require_super_admin
def reset_tenant_user_password(admin, tenant_id, user_id):
    from tenant.context import generate_password
    from werkzeug.security import generate_password_hash

    user = db.session.get(User, user_id)
    if not user or user.tenant_id != tenant_id:
        return jsonify({"success": False, "error": "user_not_in_tenant"}), 404

    new_pw = generate_password()
    user.password_hash = generate_password_hash(new_pw)
    db.session.commit()

    logger.info(
        "superadmin %s reset password for user %s in tenant %s",
        getattr(admin, "email", admin), user.id, tenant_id,
    )
    return jsonify({"success": True, "email": user.email, "password": new_pw})


@superadmin_bp.route(
    "/tenants/<int:tenant_id>/users/<int:user_id>", methods=["DELETE"]
)
@require_super_admin
def delete_tenant_user(admin, tenant_id, user_id):
    """Permanently delete a tenant user. Requires the super-admin to re-enter
    their OWN password (sent as X-Confirm-Password) to confirm the action."""
    from werkzeug.security import check_password_hash

    confirm_pw = (
        request.headers.get("X-Confirm-Password")
        or (request.get_json(silent=True) or {}).get("password")
        or ""
    )
    if not confirm_pw or not check_password_hash(getattr(admin, "password_hash", "") or "", confirm_pw):
        return jsonify({"success": False, "error": "invalid_password"}), 403

    user = db.session.get(User, user_id)
    if not user or user.tenant_id != tenant_id:
        return jsonify({"success": False, "error": "user_not_in_tenant"}), 404

    # Don't remove the tenant's last admin (would orphan the tenant).
    if user.role == "tenant_admin":
        remaining = User.query.filter_by(tenant_id=tenant_id, role="tenant_admin").count()
        if remaining <= 1:
            return jsonify({"success": False, "error": "cannot_delete_last_admin"}), 400

    Workspace.query.filter_by(user_id=user.id).delete(synchronize_session=False)
    db.session.delete(user)
    db.session.commit()
    logger.info(
        "superadmin %s DELETED user %s in tenant %s",
        getattr(admin, "email", admin), user_id, tenant_id,
    )
    return jsonify({"success": True})


# --------------------------------------------------------------------------- #
# Catalog helpers for the creation wizard
# --------------------------------------------------------------------------- #
@superadmin_bp.route("/features", methods=["GET"])
@require_super_admin
def list_features(admin):
    """Feature catalog used to render the wizard's Features step."""
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
        logger.exception("feature catalog from DB failed; falling back to constants")

    if not features:
        # Fallback to the static plan-feature matrix keys.
        try:
            from subscription.constants import PLAN_FEATURES, PLAN_ENTERPRISE, LIMIT_KEYS  # type: ignore
        except Exception:
            PLAN_FEATURES, PLAN_ENTERPRISE, LIMIT_KEYS = {}, "enterprise", set()
        limit_keys = {
            "workspaces", "users", "messages_per_day",
            "interactive_flows", "image_credits", "ad_spend_limit",
        }
        sample = PLAN_FEATURES.get(PLAN_ENTERPRISE, {}) if isinstance(PLAN_FEATURES, dict) else {}
        for key in sample:
            features.append({
                "key": key,
                "label": key.replace("_", " ").title(),
                "category": "limits" if key in limit_keys else "features",
                "feature_type": "limit" if key in limit_keys else "access",
            })

    return jsonify({"success": True, "features": features})


@superadmin_bp.route("/plans", methods=["GET"])
@require_super_admin
def list_plans(admin):
    """Assignable GLOBAL plan catalog for the wizard / edit-page plan picker.

    Each plan carries ``price_monthly_inr`` + ``description`` when a DB row
    exists, so the UI can show all available subscriptions richly (price, blurb)
    and highlight which one is selected. Custom per-tenant plans are returned by
    the separate per-tenant ``/tenants/<id>/plans`` endpoint, not here.
    """
    # Pull rich details (price/description/name) from the DB catalog, keyed by slug.
    detail_by_slug = {}
    try:
        from subscription.plan_models import SubscriptionPlan
        for row in SubscriptionPlan.query.filter_by(is_active=True).all():
            if getattr(row, "tenant_id", None):
                continue  # custom per-tenant plan — not part of the global catalog
            detail_by_slug[row.slug] = {
                "name": getattr(row, "name", None) or row.slug.title(),
                "price_monthly_inr": row.price_monthly_inr,
                "description": row.description,
            }
    except Exception:
        logger.exception("plan catalog enrichment failed; names only")

    # Preserve the canonical tier order, then append any DB-only plans.
    try:
        from subscription.constants import VALID_PLANS
        slugs = list(VALID_PLANS)
    except Exception:
        slugs = ["beta", "starter", "growth", "premium", "enterprise"]
    for slug in detail_by_slug:
        if slug not in slugs:
            slugs.append(slug)

    plans = []
    for slug in slugs:
        d = detail_by_slug.get(slug, {})
        plans.append({
            "slug": slug,
            "name": d.get("name") or slug.title(),
            "price_monthly_inr": d.get("price_monthly_inr"),
            "description": d.get("description"),
        })
    return jsonify({"success": True, "plans": plans})


# --------------------------------------------------------------------------- #
# WhatsApp number reclaim/transfer
# --------------------------------------------------------------------------- #
# A phone_number_id can be ACTIVE in only one workspace (Meta limitation +
# connection_guard). When a connect fails with "already connected to another
# workspace" (409 ALREADY_CONNECTED_OTHER), a super admin can look up where the
# number lives and reclaim it to the right workspace. This reuses the existing,
# safe connection_guard helpers — the cross-workspace guard itself is unchanged.
@superadmin_bp.route("/whatsapp/lookup", methods=["GET"])
@require_super_admin
def whatsapp_lookup(admin):
    """Report which workspace currently owns a phone_number_id."""
    pid = (request.args.get("phone_number_id") or "").strip()
    if not pid:
        return jsonify({"success": False, "error": "phone_number_id_required"}), 400
    from whatsapp.models import WhatsAppAccount
    acct = WhatsAppAccount.query.filter_by(phone_number_id=pid).first()
    if not acct:
        return jsonify({"success": True, "found": False})

    # Resolve the owning workspace -> user -> tenant so the admin knows whose it is.
    owner = None
    tenant_info = None
    try:
        ws = db.session.get(Workspace, int(acct.workspace_id)) if acct.workspace_id else None
        if ws:
            u = db.session.get(User, ws.user_id)
            if u:
                owner = {
                    "user_id": u.id, "email": u.email,
                    "name": u.name, "role": u.role, "tenant_id": getattr(u, "tenant_id", None),
                }
                if getattr(u, "tenant_id", None):
                    t = db.session.get(Tenant, u.tenant_id)
                    if t:
                        tenant_info = {
                            "id": t.id, "tenant_code": t.tenant_code,
                            "company_name": t.company_name,
                        }
    except Exception:
        logger.exception("whatsapp_lookup owner resolution failed")

    return jsonify({
        "success": True,
        "found": True,
        "phone_number_id": pid,
        "workspace_id": str(acct.workspace_id) if acct.workspace_id else None,
        "is_active": bool(acct.is_active),
        "owner": owner,
        "tenant": tenant_info,
    })


@superadmin_bp.route("/whatsapp/transfer", methods=["POST"])
@require_super_admin
def whatsapp_transfer(admin):
    """Reclaim/transfer a WhatsApp number to a target workspace.

    Body: {phone_number_id, workspace_id, force?}. With force=True (default)
    the number is deactivated in its old workspace and moved to the target —
    the documented way to release a number that is active elsewhere.
    """
    data = request.get_json(silent=True) or {}
    pid = (data.get("phone_number_id") or "").strip()
    target_ws = data.get("workspace_id")
    force = bool(data.get("force", True))
    if not pid or not target_ws:
        return jsonify({"success": False, "error": "phone_number_id_and_workspace_id_required"}), 400

    from whatsapp.connection_guard import transfer_account_to_workspace
    result = transfer_account_to_workspace(pid, str(target_ws), force=force)
    logger.info(
        "superadmin %s transferred WABA %s -> ws %s (force=%s): %s",
        getattr(admin, "email", admin), pid, target_ws, force, result.get("message"),
    )
    return jsonify(result), (200 if result.get("success") else 409)
