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
DEFAULT_ADMIN_PASSWORD = os.getenv("DEFAULT_ADMIN_PASSWORD") or os.getenv("DEFAULT_ADMIN_PASS", "SocioChat@Admin1")


def ensure_default_admin() -> None:
    admin = Admin.query.filter_by(email=DEFAULT_ADMIN_EMAIL).first()
    if not admin:
        admin = Admin(
            email=DEFAULT_ADMIN_EMAIL,
            password_hash=generate_password_hash(DEFAULT_ADMIN_PASSWORD),
            is_superadmin=True,
        )
        db.session.add(admin)
        db.session.commit()
        logger.info("Default admin created: %s", DEFAULT_ADMIN_EMAIL)


def get_current_admin():
    admin_id = session.get("admin_id") or request.headers.get("X-Admin-Id")
    if not admin_id:
        return None
    try:
        return db.session.get(Admin, int(admin_id))
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
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""

    if not email or not password:
        return jsonify({"success": False, "error": "email_password_required"}), 400

    admin = Admin.query.filter_by(email=email).first()
    if admin and check_password_hash(admin.password_hash, password):
        session["admin_id"] = admin.id
        session.modified = True
        return jsonify({
            "success": True,
            "user": {"id": admin.id, "email": admin.email, "role": "admin", "name": "Administrator"},
        })

    # Bootstrap login with default credentials
    if email == DEFAULT_ADMIN_EMAIL and password == DEFAULT_ADMIN_PASSWORD:
        ensure_default_admin()
        admin = Admin.query.filter_by(email=DEFAULT_ADMIN_EMAIL).first()
        if admin:
            session["admin_id"] = admin.id
            session.modified = True
            return jsonify({
                "success": True,
                "user": {"id": admin.id, "email": admin.email, "role": "admin", "name": "Administrator"},
            })

    return jsonify({"success": False, "error": "invalid_credentials"}), 401


@admin_bp.route("/api/admin/logout", methods=["POST"])
def admin_logout():
    session.pop("admin_id", None)
    return jsonify({"success": True})


@admin_bp.route("/api/admin/me", methods=["GET"])
@require_admin
def admin_me(admin):
    return jsonify({"success": True, "admin": {"id": admin.id, "email": admin.email, "role": "admin"}})


@admin_bp.route("/api/admin/users", methods=["GET"])
@require_admin
def list_users(admin):
    users = User.query.order_by(User.created_at.desc()).all()
    return jsonify([serialize_user(u) for u in users])


@admin_bp.route("/api/admin/review", methods=["POST"])
@require_admin
def review_users(admin):
    users = User.query.filter_by(status="under_review").order_by(User.created_at.desc()).all()
    return jsonify({
        "success": True,
        "users": [serialize_user(u) for u in users],
    })


@admin_bp.route("/api/admin/approve/<int:user_id>", methods=["POST"])
@require_admin
def approve_user(admin, user_id):
    user = db.session.get(User, user_id)
    if not user:
        return jsonify({"success": False, "error": "user_not_found"}), 404
    user.status = "active"
    _audit(admin, "approved", user_id)
    db.session.commit()
    return jsonify({"success": True, "user": serialize_user(user)})


@admin_bp.route("/api/admin/reject/<int:user_id>", methods=["POST"])
@require_admin
def reject_user(admin, user_id):
    data = request.get_json(silent=True) or {}
    reason = (data.get("reason") or "Rejected by admin").strip()
    user = db.session.get(User, user_id)
    if not user:
        return jsonify({"success": False, "error": "user_not_found"}), 404
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

    user = db.session.get(User, user_id)
    if not user:
        return jsonify({"success": False, "error": "user_not_found"}), 404

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


@admin_bp.route("/api/admin/login-as-user", methods=["POST"])
@require_admin
def login_as_user(admin):
    data = request.get_json(silent=True) or {}
    user_id = data.get("user_id") or data.get("userId")
    email = (data.get("email") or "").strip().lower()

    user = None
    if user_id:
        try:
            user = db.session.get(User, int(user_id))
        except (TypeError, ValueError):
            return jsonify({"success": False, "error": "user_id_invalid"}), 400
    elif email:
        user = User.query.filter_by(email=email).first()

    if not user:
        return jsonify({"success": False, "error": "user_not_found"}), 404

    session["user_id"] = user.id
    session.modified = True
    _audit(admin, "admin_login_as_user", user.id, {"email": user.email})
    db.session.commit()

    workspaces = Workspace.query.filter_by(user_id=user.id).all()
    return jsonify({
        "success": True,
        "user": {
            **serialize_user(user),
            "admin_override": True,
            "admin_override_actor": admin.email,
        },
        "workspaces": [{"id": w.id, "name": w.business_name or f"Workspace {w.id}"} for w in workspaces],
    })
