"""
Subscription API Routes
=======================

API endpoints for subscription management:
- User-facing: Get plan limits, usage stats
- Admin-facing: List users, change plans, view usage

All routes return JSON responses.
"""

import os
from datetime import date, timedelta

import jwt
from flask import Blueprint, current_app, has_request_context, jsonify, request, session
from sqlalchemy import func

from shared_models import db, User, Workspace, Admin
from subscription.constants import VALID_PLANS, PLAN_FEATURES
from subscription.models import SubscriptionUsage, AdSpendTracking, PlanChangeHistory
from subscription.service import (
    get_user_plan,
    get_plan_limits,
    get_user_usage_stats,
    change_user_plan,
)


subscription_bp = Blueprint("subscription", __name__, url_prefix="/api/subscription")


# =============================================================================
# Helper Functions
# =============================================================================

def _coerce_user_pk(val):
    """Best-effort parse of JWT / header values into an integer users.id."""
    if val is None:
        return None
    if isinstance(val, bool):
        return None
    if isinstance(val, int):
        return val
    s = str(val).strip()
    if s.isdigit():
        return int(s)
    return None


def _user_id_from_bearer_jwt():
    """
    Resolve numeric user id from Authorization Bearer when X-User-Id / session are absent.
    Used for cross-origin WhatsApp API calls (Dev Tunnels) where session cookies are not shared.
    """
    auth = (request.headers.get("Authorization") or "").strip()
    if not auth.startswith("Bearer "):
        return None
    token = auth[7:].strip()
    if not token or token.count(".") != 2:
        return None

    secrets = []
    if has_request_context():
        sk = getattr(current_app, "secret_key", None)
        if sk:
            secrets.append(sk)
    for env_key in ("SECRET_KEY", "SESSION_SECRET"):
        v = os.environ.get(env_key)
        if v and v not in secrets:
            secrets.append(v)

    payload = None
    for sec in secrets:
        if not sec:
            continue
        try:
            payload = jwt.decode(token, sec, algorithms=["HS256"])
            break
        except jwt.InvalidTokenError:
            continue
    if payload is None:
        try:
            payload = jwt.decode(
                token,
                options={"verify_signature": False},
                algorithms=["HS256", "RS256"],
            )
        except jwt.InvalidTokenError:
            return None

    for key in ("user_id", "id", "sub", "userId", "uid"):
        uid = _coerce_user_pk(payload.get(key))
        if uid is not None:
            return uid
    return None


def get_current_user():
    """Get current user from session, X-User-Id, or Bearer JWT (same DB as monolith)."""
    user_id = session.get("user_id") or request.headers.get("X-User-Id")
    if not user_id:
        user_id = _user_id_from_bearer_jwt()
    if not user_id:
        return None
    try:
        return db.session.get(User, int(user_id))
    except Exception:
        return None


def get_current_admin():
    """Get current admin from session or headers."""
    admin_id = session.get("admin_id")
    
    # Fallback to X-Admin-Id header if session is not available (cross-origin)
    if not admin_id:
        admin_id = request.headers.get("X-Admin-Id")
    
    # Fallback to query param for testing
    if not admin_id:
        admin_id = request.args.get("admin_id")
    
    if not admin_id:
        return None
    try:
        return db.session.get(Admin, int(admin_id))
    except Exception:
        return None


def require_auth(f):
    """Decorator to require user authentication."""
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        user = get_current_user()
        if not user:
            return jsonify({"success": False, "error": "authentication_required"}), 401
        return f(user, *args, **kwargs)
    return decorated


def require_admin(f):
    """Decorator to require admin authentication."""
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        admin = get_current_admin()
        if not admin:
            return jsonify({"success": False, "error": "admin_required"}), 403
        return f(admin, *args, **kwargs)
    return decorated


# =============================================================================
# User-Facing Routes
# =============================================================================

@subscription_bp.route("/plans", methods=["GET"])
def list_plans():
    """
    GET /api/subscription/plans
    
    List all available plans with their features and limits.
    No authentication required.
    
    Response:
    {
        "success": true,
        "plans": {
            "starter": { ... },
            "growth": { ... },
            "enterprise": { ... }
        }
    }
    """
    # Exclude beta from public list
    plans = {k: v for k, v in PLAN_FEATURES.items() if k != "beta"}
    return jsonify({
        "success": True,
        "plans": plans
    })


@subscription_bp.route("/limits", methods=["GET"])
@require_auth
def get_limits(user):
    """
    GET /api/subscription/limits
    
    Get current user's plan limits and feature access.
    
    Response:
    {
        "success": true,
        "plan": "growth",
        "limits": {
            "workspaces": 3,
            "users": 3,
            "messages_per_day": 5000,
            ...
        },
        "features": {
            "image_generation": true,
            "whatsapp_smart_ai": false,
            ...
        }
    }
    """
    plan_info = get_plan_limits(user)
    return jsonify({
        "success": True,
        **plan_info
    })


@subscription_bp.route("/usage", methods=["GET"])
@require_auth
def get_usage(user):
    """
    GET /api/subscription/usage
    GET /api/subscription/usage?workspace_id=123
    
    Get current user's usage statistics.
    
    Query Parameters:
    - workspace_id: Optional workspace ID for workspace-specific stats
    
    Response:
    {
        "success": true,
        "plan": "growth",
        "usage": {
            "messages_today": 150,
            "messages_limit": 5000,
            "image_credits_used": 50,
            "image_credits_limit": 100000,
            "workspaces": 2,
            "workspaces_limit": 3,
            "interactive_flows": 5,
            "interactive_flows_limit": 15,
            "ad_spend_inr": 25000,
            "ad_spend_limit": 500000
        }
    }
    """
    workspace_id = request.args.get("workspace_id", type=int)
    stats = get_user_usage_stats(user, workspace_id)
    return jsonify({
        "success": True,
        **stats
    })


@subscription_bp.route("/check-feature/<feature>", methods=["GET"])
@require_auth
def check_feature(user, feature):
    """
    GET /api/subscription/check-feature/<feature>
    
    Check if user has access to a specific feature.
    
    Response:
    {
        "success": true,
        "feature": "image_generation",
        "allowed": true,
        "plan": "growth"
    }
    or
    {
        "success": true,
        "feature": "whatsapp_smart_ai",
        "allowed": false,
        "plan": "growth",
        "message": "Feature 'whatsapp_smart_ai' is not available on your growth plan."
    }
    """
    from subscription.service import check_feature_access
    
    allowed, message = check_feature_access(user, feature)
    response = {
        "success": True,
        "feature": feature,
        "allowed": allowed,
        "plan": get_user_plan(user)
    }
    if not allowed:
        response["message"] = message
    
    return jsonify(response)


# =============================================================================
# Admin Routes
# =============================================================================

@subscription_bp.route("/admin/users", methods=["GET"])
@require_admin
def admin_list_users(admin):
    """
    GET /api/subscription/admin/users
    GET /api/subscription/admin/users?plan=starter&page=1&per_page=20&search=email@example.com
    
    List all users with their plan info and usage stats.
    
    Query Parameters:
    - plan: Filter by plan (starter, growth, enterprise)
    - page: Page number (default: 1)
    - per_page: Items per page (default: 20, max: 100)
    - search: Search by email or name
    
    Response:
    {
        "success": true,
        "users": [
            {
                "id": 1,
                "name": "John Doe",
                "email": "john@example.com",
                "plan": "growth",
                "status": "approved",
                "workspaces": 2,
                "messages_today": 150,
                "image_credits_used": 50,
                "created_at": "2025-01-01T00:00:00"
            }
        ],
        "pagination": {
            "page": 1,
            "per_page": 20,
            "total": 150,
            "pages": 8
        }
    }
    """
    page = request.args.get("page", 1, type=int)
    per_page = min(request.args.get("per_page", 20, type=int), 100)
    plan_filter = request.args.get("plan")
    search = request.args.get("search", "").strip()
    
    query = User.query
    
    if plan_filter and plan_filter in VALID_PLANS:
        query = query.filter(User.plan == plan_filter)
    
    if search:
        query = query.filter(
            db.or_(
                User.email.ilike(f"%{search}%"),
                User.name.ilike(f"%{search}%")
            )
        )
    
    query = query.order_by(User.created_at.desc())
    pagination = query.paginate(page=page, per_page=per_page, error_out=False)
    
    today = date.today()
    first_of_month = today.replace(day=1)
    
    users = []
    for user in pagination.items:
        # Get today's messages
        messages_today = db.session.query(
            func.coalesce(func.sum(SubscriptionUsage.messages_sent), 0)
        ).filter(
            SubscriptionUsage.user_id == user.id,
            SubscriptionUsage.usage_date == today
        ).scalar()
        
        # Get monthly image credits
        image_credits = db.session.query(
            func.coalesce(func.sum(SubscriptionUsage.image_credits_used), 0)
        ).filter(
            SubscriptionUsage.user_id == user.id,
            SubscriptionUsage.usage_date >= first_of_month
        ).scalar()
        
        # Get workspace count
        workspace_count = Workspace.query.filter_by(user_id=user.id).count()
        
        users.append({
            "id": user.id,
            "name": user.name,
            "email": user.email,
            "plan": user.plan or "beta",
            "status": user.status,
            "workspaces": workspace_count,
            "messages_today": messages_today,
            "image_credits_used": image_credits,
            "created_at": user.created_at.isoformat() if user.created_at else None,
        })
    
    return jsonify({
        "success": True,
        "users": users,
        "pagination": {
            "page": page,
            "per_page": per_page,
            "total": pagination.total,
            "pages": pagination.pages
        }
    })


@subscription_bp.route("/admin/users/<int:user_id>", methods=["GET"])
@require_admin
def admin_get_user(admin, user_id):
    """
    GET /api/subscription/admin/users/<user_id>
    
    Get detailed usage for a specific user.
    
    Response:
    {
        "success": true,
        "user": {
            "id": 1,
            "name": "John Doe",
            "email": "john@example.com",
            "plan": "growth",
            ...
        },
        "usage": { ... },
        "limits": { ... },
        "plan_history": [ ... ],
        "daily_usage": [ ... ]  // Last 30 days
    }
    """
    user = db.session.get(User, user_id)
    if not user:
        return jsonify({"success": False, "error": "user_not_found"}), 404
    
    # Get plan info
    plan_info = get_plan_limits(user)
    usage_stats = get_user_usage_stats(user)
    
    # Get plan change history
    history = PlanChangeHistory.query.filter_by(user_id=user_id).order_by(
        PlanChangeHistory.created_at.desc()
    ).limit(10).all()
    
    # Get daily usage for last 30 days
    thirty_days_ago = date.today() - timedelta(days=30)
    daily_usage = SubscriptionUsage.query.filter(
        SubscriptionUsage.user_id == user_id,
        SubscriptionUsage.usage_date >= thirty_days_ago
    ).order_by(SubscriptionUsage.usage_date.desc()).all()
    
    # Get workspace count
    workspace_count = Workspace.query.filter_by(user_id=user.id).count()
    
    return jsonify({
        "success": True,
        "user": {
            "id": user.id,
            "name": user.name,
            "email": user.email,
            "phone": user.phone,
            "plan": user.plan or "beta",
            "status": user.status,
            "business_name": user.business_name,
            "industry": user.industry,
            "workspaces": workspace_count,
            "created_at": user.created_at.isoformat() if user.created_at else None,
            "updated_at": user.updated_at.isoformat() if user.updated_at else None,
        },
        "limits": plan_info["limits"],
        "features": plan_info["features"],
        "usage": usage_stats["usage"],
        "plan_history": [h.to_dict() for h in history],
        "daily_usage": [u.to_dict() for u in daily_usage],
    })


@subscription_bp.route("/admin/users/<int:user_id>/plan", methods=["PUT"])
@require_admin
def admin_update_plan(admin, user_id):
    """
    PUT /api/subscription/admin/users/<user_id>/plan
    
    Update a user's subscription plan.
    
    Request Body:
    {
        "plan": "growth",
        "reason": "Upgraded after payment"  // optional
    }
    
    Response:
    {
        "success": true,
        "user_id": 1,
        "old_plan": "starter",
        "new_plan": "growth",
        "changed_at": "2025-01-22T10:30:00"
    }
    """
    user = db.session.get(User, user_id)
    if not user:
        return jsonify({"success": False, "error": "user_not_found"}), 404
    
    data = request.get_json() or {}
    new_plan = data.get("plan")
    reason = data.get("reason")
    
    if not new_plan:
        return jsonify({"success": False, "error": "plan_required"}), 400
    
    if new_plan not in VALID_PLANS:
        return jsonify({
            "success": False,
            "error": "invalid_plan",
            "valid_plans": VALID_PLANS
        }), 400
    
    old_plan = user.plan or "beta"
    
    try:
        history = change_user_plan(user, new_plan, admin_id=admin.id, reason=reason)
        return jsonify({
            "success": True,
            "user_id": user_id,
            "old_plan": old_plan,
            "new_plan": new_plan,
            "changed_at": history.created_at.isoformat() if history.created_at else None
        })
    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 400


@subscription_bp.route("/admin/stats", methods=["GET"])
@require_admin
def admin_stats(admin):
    """
    GET /api/subscription/admin/stats
    
    Get overall subscription statistics.
    
    Response:
    {
        "success": true,
        "stats": {
            "users_by_plan": {
                "beta": 50,
                "starter": 100,
                "growth": 25,
                "enterprise": 5
            },
            "total_users": 180,
            "total_workspaces": 250,
            "messages_today": 15000,
            "image_credits_this_month": 50000
        }
    }
    """
    today = date.today()
    first_of_month = today.replace(day=1)
    
    # Users by plan
    plan_counts = db.session.query(
        User.plan,
        func.count(User.id)
    ).group_by(User.plan).all()
    
    users_by_plan = {}
    for plan, count in plan_counts:
        users_by_plan[plan or "beta"] = count
    
    # Fill in missing plans with 0
    for plan in VALID_PLANS:
        if plan not in users_by_plan:
            users_by_plan[plan] = 0
    
    total_users = User.query.count()
    total_workspaces = Workspace.query.count()
    
    messages_today = db.session.query(
        func.coalesce(func.sum(SubscriptionUsage.messages_sent), 0)
    ).filter(SubscriptionUsage.usage_date == today).scalar()
    
    image_credits_month = db.session.query(
        func.coalesce(func.sum(SubscriptionUsage.image_credits_used), 0)
    ).filter(SubscriptionUsage.usage_date >= first_of_month).scalar()
    
    return jsonify({
        "success": True,
        "stats": {
            "users_by_plan": users_by_plan,
            "total_users": total_users,
            "total_workspaces": total_workspaces,
            "messages_today": messages_today,
            "image_credits_this_month": image_credits_month
        }
    })


@subscription_bp.route("/admin/usage/daily", methods=["GET"])
@require_admin
def admin_daily_usage(admin):
    """
    GET /api/subscription/admin/usage/daily
    GET /api/subscription/admin/usage/daily?days=30
    
    Get daily usage aggregates for all users.
    
    Query Parameters:
    - days: Number of days to look back (default: 7, max: 90)
    
    Response:
    {
        "success": true,
        "daily_usage": [
            {
                "date": "2025-01-22",
                "total_messages": 5000,
                "total_image_credits": 500,
                "active_users": 50
            }
        ]
    }
    """
    days = min(request.args.get("days", 7, type=int), 90)
    start_date = date.today() - timedelta(days=days - 1)
    
    daily_stats = db.session.query(
        SubscriptionUsage.usage_date,
        func.sum(SubscriptionUsage.messages_sent).label("total_messages"),
        func.sum(SubscriptionUsage.image_credits_used).label("total_image_credits"),
        func.count(func.distinct(SubscriptionUsage.user_id)).label("active_users")
    ).filter(
        SubscriptionUsage.usage_date >= start_date
    ).group_by(
        SubscriptionUsage.usage_date
    ).order_by(
        SubscriptionUsage.usage_date.desc()
    ).all()
    
    return jsonify({
        "success": True,
        "daily_usage": [
            {
                "date": row.usage_date.isoformat(),
                "total_messages": row.total_messages or 0,
                "total_image_credits": row.total_image_credits or 0,
                "active_users": row.active_users or 0
            }
            for row in daily_stats
        ]
    })
