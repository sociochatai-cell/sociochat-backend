"""SocioChat admin portal API."""

import json
import logging
import os
from datetime import datetime, timezone
from functools import wraps

from flask import Blueprint, jsonify, request, session
from werkzeug.security import check_password_hash, generate_password_hash

from models import db, User, Admin, AuditLog, Workspace

logger = logging.getLogger(__name__)

admin_bp = Blueprint("admin", __name__)

DEFAULT_ADMIN_EMAIL = os.getenv("DEFAULT_ADMIN_EMAIL", "admin@sociochat.ai").strip().lower()

# SECURITY: only an EXPLICIT env password is trustworthy. The built-in literal is a
# dev-only convenience - it must never seed or authenticate a super-admin in a real
# environment, otherwise anyone knowing it owns the whole platform (all tenants).
_ADMIN_PASSWORD_FROM_ENV = os.getenv("DEFAULT_ADMIN_PASSWORD") or os.getenv("DEFAULT_ADMIN_PASS")
_ADMIN_PASSWORD_DEV_FALLBACK = "SocioChat@Admin1"
DEFAULT_ADMIN_PASSWORD = _ADMIN_PASSWORD_FROM_ENV or _ADMIN_PASSWORD_DEV_FALLBACK


def _default_admin_password_allowed() -> bool:
    """Safe to seed / bootstrap-login the default admin only when a password was
    explicitly configured via env, or we are in a dev environment. In a non-dev
    environment with no explicit password the built-in fallback is refused."""
    if _ADMIN_PASSWORD_FROM_ENV:
        return True
    try:
        from core.deployment_safety import is_non_dev_environment
        return not is_non_dev_environment()
    except Exception:
        return False


def ensure_default_admin() -> None:
    admin = Admin.query.filter_by(email=DEFAULT_ADMIN_EMAIL).first()
    if admin:
        return
    if not _default_admin_password_allowed():
        logger.error(
            "Refusing to seed default super-admin '%s' with the built-in password in a "
            "non-dev environment. Set DEFAULT_ADMIN_PASSWORD to a strong secret and redeploy.",
            DEFAULT_ADMIN_EMAIL,
        )
        return
    admin = Admin(
        email=DEFAULT_ADMIN_EMAIL,
        password_hash=generate_password_hash(DEFAULT_ADMIN_PASSWORD),
        is_superadmin=True,
    )
    db.session.add(admin)
    db.session.commit()
    logger.info("Default admin created: %s", DEFAULT_ADMIN_EMAIL)


def get_current_admin():
    # Session OR a SIGNED admin JWT only — X-Admin-Id header is no longer trusted.
    from auth_core import authenticated_admin_id
    aid = authenticated_admin_id()
    if aid is None:
        return None
    try:
        return db.session.get(Admin, aid)
    except (TypeError, ValueError):
        return None


def require_admin(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        admin = get_current_admin()
        if not admin:
            return jsonify({"success": False, "error": "admin_not_authenticated"}), 401
        return f(admin, *args, **kwargs)
    return decorated


def _sociovia_linked(user_id: int) -> bool:
    """Whether this user has a cross-app Sociovia link (drives the admin toggle state)."""
    try:
        from sociovia_sync import is_user_linked
        return is_user_linked(user_id)
    except Exception:
        return False


def serialize_user(user: User) -> dict:
    return {
        "id": user.id,
        "name": user.name,
        "email": user.email,
        "phone": user.phone,
        "role": getattr(user, "role", "user"),
        "status": user.status,
        "plan": user.plan or "beta",
        "billing_scope": getattr(user, "billing_scope", None) or "global",
        "business_name": user.business_name,
        "industry": user.industry,
        "beta_expires_at": user.beta_expires_at.isoformat() if user.beta_expires_at else None,
        "subscription_expires_at": user.subscription_expires_at.isoformat() if user.subscription_expires_at else None,
        "created_at": user.created_at.isoformat() if user.created_at else None,
        "sociovia_linked": _sociovia_linked(user.id),
    }


def _audit(admin: Admin, action: str, user_id: int | None = None, meta: dict | None = None) -> None:
    db.session.add(AuditLog(
        actor=admin.email,
        action=action,
        user_id=user_id,
        meta=json.dumps(meta or {}),
    ))


@admin_bp.route("/api/admin/login", methods=["POST"])
def admin_login():
    from tenant.models import Tenant
    from tenant.branding import INTERNAL_TENANT_CODE

    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""
    code = (data.get("tenant_code") or "").strip().upper()

    if not email or not password:
        return jsonify({"success": False, "error": "email_password_required"}), 400

    def _super_admin_response(admin: Admin):
        from auth_core import create_admin_token
        session["admin_id"] = admin.id
        session.modified = True
        return jsonify({
            "success": True,
            "role": "super_admin",
            "token": create_admin_token(admin.id, admin.email),
            "user": {"id": admin.id, "email": admin.email, "role": "admin", "name": "Administrator"},
            "redirect": "/superadmin/tenants",
        })

    # 1. Super Admin path — empty code or the internal tenant code.
    if code == "" or code == INTERNAL_TENANT_CODE:
        if email != DEFAULT_ADMIN_EMAIL:
            # SECURITY: the admin portal (empty code OR internal T0000) is EXCLUSIVELY
            # the super admin (admin@sociovia.com). Never fall through to the tenant-admin
            # path for T0000, so no other T0000 user/email can reach the admin portal.
            return jsonify({"success": False, "error": "invalid_credentials"}), 401
        else:
            admin = Admin.query.filter_by(email=email).first()
            if admin and check_password_hash(admin.password_hash, password):
                return _super_admin_response(admin)

        # Bootstrap login with default credentials
        if _default_admin_password_allowed() and email == DEFAULT_ADMIN_EMAIL and password == DEFAULT_ADMIN_PASSWORD:
            ensure_default_admin()
            admin = Admin.query.filter_by(email=DEFAULT_ADMIN_EMAIL).first()
            if admin:
                return _super_admin_response(admin)

        # Admin auth failed for the super admin. Always 401 here (both empty code
        # and T0000) — the admin portal is locked to admin@sociovia.com only.
        return jsonify({"success": False, "error": "invalid_credentials"}), 401

    # 2. Tenant Admin path — a real tenant code (or T0000 fall-through).
    tenant = Tenant.query.filter_by(tenant_code=code).first()
    if not tenant:
        return jsonify({"success": False, "error": "invalid_credentials"}), 401
    if tenant.status == "suspended":
        return jsonify({"success": False, "error": "tenant_suspended"}), 403

    user = User.query.filter_by(tenant_id=tenant.id, email=email).first()
    if not user or not check_password_hash(user.password_hash, password):
        return jsonify({"success": False, "error": "invalid_credentials"}), 401
    if user.role not in ("tenant_admin", "admin"):
        return jsonify({"success": False, "error": "not_an_admin"}), 403

    # Establish a normal user session for the tenant admin.
    session["user_id"] = user.id
    session.pop("admin_id", None)
    session.modified = True

    from auth_core import create_user_token
    workspaces = Workspace.query.filter_by(user_id=user.id).all()
    return jsonify({
        "success": True,
        "role": "tenant_admin",
        "token": create_user_token(user.id, user.email),
        "user": {
            "id": user.id,
            "name": user.name,
            "email": user.email,
            "role": user.role,
            "tenant_id": user.tenant_id,
        },
        "tenant": {
            "tenant_code": tenant.tenant_code,
            "company_name": tenant.company_name,
            "branding": tenant.branding_dict(),
        },
        "workspaces": [{"id": w.id, "business_name": w.business_name} for w in workspaces],
        "redirect": "/tenant-admin",
    })


@admin_bp.route("/api/admin/logout", methods=["POST"])
def admin_logout():
    session.pop("admin_id", None)
    return jsonify({"success": True})


@admin_bp.route("/api/admin/me", methods=["GET"])
@require_admin
def admin_me(admin):
    return jsonify({"success": True, "admin": {"id": admin.id, "email": admin.email, "role": "admin"}})


def _internal_user_filter(query):
    """Restrict a User query to the platform's OWN tenant (T0000).

    The Super Admin's "Users" portal manages the platform's own application
    users only — tenant users are managed under Tenant Management, kept fully
    separate. Legacy rows with NULL tenant_id are treated as internal.
    """
    from sqlalchemy import or_, false as _sa_false
    from tenant.models import Tenant
    from tenant.branding import INTERNAL_TENANT_CODE

    internal = Tenant.query.filter_by(tenant_code=INTERNAL_TENANT_CODE).first()
    if internal:
        return query.filter(or_(User.tenant_id == internal.id, User.tenant_id.is_(None)))
    # Fail CLOSED: if the internal tenant can't be resolved, do NOT fall back to an
    # unfiltered query (that would return every tenant's users). Return nothing.
    return query.filter(_sa_false())


def _get_internal_user_or_404(user_id):
    """Fetch a user by id but ONLY within the platform's own (T0000) tenant.

    The Super Admin "Users" portal manages SocioChat's own users only; other
    tenants' users are managed under Tenant Management. This prevents admin
    actions (approve/reject/update/impersonate) from reaching across tenants.
    Returns (user, None) or (None, (response, status)).
    """
    user = _internal_user_filter(User.query).filter(User.id == user_id).first()
    if not user:
        return None, (jsonify({"success": False, "error": "user_not_found"}), 404)
    return user, None


@admin_bp.route("/api/admin/users", methods=["GET"])
@require_admin
def list_users(admin):
    users = _internal_user_filter(User.query).order_by(User.created_at.desc()).all()
    return jsonify([serialize_user(u) for u in users])


@admin_bp.route("/api/admin/review", methods=["POST"])
@require_admin
def review_users(admin):
    users = (_internal_user_filter(User.query.filter_by(status="under_review"))
             .order_by(User.created_at.desc()).all())
    return jsonify({
        "success": True,
        "users": [serialize_user(u) for u in users],
    })


@admin_bp.route("/api/admin/approve/<int:user_id>", methods=["POST"])
@require_admin
def approve_user(admin, user_id):
    user, err = _get_internal_user_or_404(user_id)
    if err:
        return err
    user.status = "active"
    _audit(admin, "approved", user_id)
    db.session.commit()
    return jsonify({"success": True, "user": serialize_user(user)})


@admin_bp.route("/api/admin/reject/<int:user_id>", methods=["POST"])
@require_admin
def reject_user(admin, user_id):
    data = request.get_json(silent=True) or {}
    reason = (data.get("reason") or "Rejected by admin").strip()
    user, err = _get_internal_user_or_404(user_id)
    if err:
        return err
    user.status = "rejected"
    user.rejection_reason = reason
    _audit(admin, "rejected", user_id, {"reason": reason})
    db.session.commit()
    return jsonify({"success": True})


@admin_bp.route("/api/admin/users/<int:user_id>", methods=["PATCH"])
@require_admin
def update_user(admin, user_id):
    from subscription.constants import VALID_PLANS
    from subscription.service import change_user_plan

    user, err = _get_internal_user_or_404(user_id)
    if err:
        return err

    data = request.get_json() or {}

    if "plan" in data:
        new_plan = data["plan"]
        if new_plan and new_plan not in VALID_PLANS:
            return jsonify({"success": False, "error": "invalid_plan", "valid_plans": VALID_PLANS}), 400
        if new_plan and new_plan != (user.plan or "beta"):
            change_user_plan(user, new_plan, admin_id=admin.id, reason=data.get("reason") or "Admin update")

    if "status" in data:
        user.status = data["status"]
    if "role" in data:
        user.role = data["role"]

    for field in ("beta_expires_at", "subscription_expires_at"):
        if field in data:
            val = data[field]
            if val:
                try:
                    setattr(user, field, datetime.fromisoformat(str(val).replace("Z", "+00:00")))
                except ValueError:
                    return jsonify({"success": False, "error": f"invalid_{field}"}), 400
            else:
                setattr(user, field, None)

    _audit(admin, "admin_update_user", user_id, data)
    db.session.commit()
    return jsonify({"success": True, "user": serialize_user(user)})


@admin_bp.route("/api/admin/users/<int:user_id>/sociovia-link", methods=["POST"])
@require_admin
def set_user_sociovia_link(admin, user_id):
    """Per-user admin toggle: link this user to their Sociovia account (matched by
    email) and mirror their workspaces, or unlink. This is the request the toggle
    fires. No signup checkbox — admin-controlled, per user."""
    user, err = _get_internal_user_or_404(user_id)
    if err:
        return err
    data = request.get_json(silent=True) or {}
    enable = bool(data.get("enable", True))
    from sociovia_sync import trigger_sociovia_link
    result = trigger_sociovia_link(user.id, user.email, enable)
    _audit(admin, "admin_sociovia_link", user_id, {"enable": enable, "result": result})
    db.session.commit()
    return jsonify({"success": bool(result.get("ok")), **result, "user": serialize_user(user)}), (200 if result.get("ok") else 400)


@admin_bp.route("/api/admin/login-as-user", methods=["POST"])
@require_admin
def login_as_user(admin):
    data = request.get_json(silent=True) or {}
    user_id = data.get("user_id") or data.get("userId")
    email = (data.get("email") or "").strip().lower()

    # Impersonation is restricted to the platform's OWN (T0000) users. To reach a
    # tenant's user, a super admin must use the per-tenant impersonation flow
    # (/api/superadmin/tenants/<id>/impersonate), which is scoped to that tenant.
    user = None
    if user_id:
        try:
            uid = int(user_id)
        except (TypeError, ValueError):
            return jsonify({"success": False, "error": "user_id_invalid"}), 400
        user = _internal_user_filter(User.query).filter(User.id == uid).first()
    elif email:
        user = _internal_user_filter(User.query).filter(User.email == email).first()

    if not user:
        return jsonify({"success": False, "error": "user_not_found"}), 404

    session["user_id"] = user.id
    # Full switch to the user — drop the admin realm so the session isn't dual
    # (dual identity made post-payment redirects resolve back to the admin).
    session.pop("admin_id", None)
    session.modified = True
    _audit(admin, "admin_login_as_user", user.id, {"email": user.email})
    db.session.commit()

    from auth_core import create_user_token
    user_token = create_user_token(user.id, user.email)

    workspaces = Workspace.query.filter_by(user_id=user.id).all()
    return jsonify({
        "success": True,
        "token": user_token,
        "user": {
            **serialize_user(user),
            "admin_override": True,
            "admin_override_actor": admin.email,
        },
        "workspaces": [{"id": w.id, "name": w.business_name or f"Workspace {w.id}"} for w in workspaces],
    })


@admin_bp.route("/api/admin/analytics/users", methods=["GET"])
@require_admin
def admin_user_analytics(admin):
    from datetime import timedelta
    from sqlalchemy import func

    now = datetime.utcnow()
    thirty_days_ago = now - timedelta(days=30)
    soon_cutoff = now + timedelta(days=7)

    users = _internal_user_filter(User.query).all()

    rows = []
    summary = {
        "total_users": 0,
        "paid_users": 0,
        "free_users": 0,
        "active_subscriptions": 0,
        "expiring_soon": 0,
        "expired": 0,
        "meta_connected": 0,
        "auto_renew": 0,
        "new_last_30d": 0,
        "inactive_dead": 0,
        "plan_mix": {},
    }

    paid_plans = {"starter", "growth", "enterprise"}

    for u in users:
        summary["total_users"] += 1
        plan = u.plan or "beta"
        summary["plan_mix"][plan] = summary["plan_mix"].get(plan, 0) + 1

        is_paid = plan in paid_plans
        if is_paid:
            summary["paid_users"] += 1
        else:
            summary["free_users"] += 1

        exp = u.subscription_expires_at
        if is_paid and exp:
            if exp > now:
                summary["active_subscriptions"] += 1
                if exp <= soon_cutoff:
                    summary["expiring_soon"] += 1
                    sub_status = "expiring"
                else:
                    sub_status = "active"
            else:
                summary["expired"] += 1
                sub_status = "expired"
        else:
            sub_status = "free"

        days_to_expiry = None
        if exp and exp > now:
            days_to_expiry = (exp - now).days

        is_linked = _sociovia_linked(u.id)
        if is_linked:
            summary["meta_connected"] += 1

        is_dead = u.status in ("rejected", "suspended") or (
            u.status == "pending_verification"
            and u.created_at
            and u.created_at < thirty_days_ago
        )
        if is_dead:
            summary["inactive_dead"] += 1

        if u.created_at and u.created_at >= thirty_days_ago:
            summary["new_last_30d"] += 1

        rows.append({
            "id": u.id,
            "name": u.name,
            "email": u.email,
            "phone": u.phone,
            "role": getattr(u, "role", "user"),
            "status": u.status,
            "plan": plan,
            "sub_status": sub_status,
            "days_to_expiry": days_to_expiry,
            "subscription_expires_at": exp.isoformat() if exp else None,
            "auto_renew": False,
            "meta_connected": is_linked,
            "is_dead": is_dead,
            "created_at": u.created_at.isoformat() if u.created_at else None,
        })

    return jsonify({"success": True, "summary": summary, "rows": rows})
