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
from sqlalchemy import func, or_

from shared_models import db, User, Workspace, Admin
from subscription.constants import (
    VALID_PLANS, PLAN_FEATURES, BILLING_SCOPE_GLOBAL, BILLING_SCOPE_PRIVATE,
    PLAN_SCOPE_GLOBAL, PLAN_SCOPE_PRIVATE, PRIVATE_SLOT_GLOBAL_TIERS,
)
from subscription.models import SubscriptionUsage, AdSpendTracking, PlanChangeHistory
from subscription.service import (
    get_user_plan,
    get_plan_limits,
    get_user_usage_stats,
    change_user_plan,
    is_private_slot_user,
    is_plan_assignable_to_user,
    get_assignable_plan_slugs_for_user,
)


subscription_bp = Blueprint("subscription", __name__, url_prefix="/api/subscription")


# =============================================================================
# Helper Functions
# =============================================================================

_VALID_BILLING_PERIODS = ("monthly", "quarterly", "yearly")


def _normalize_billing_period(val):
    """Normalize a request-supplied billing_period to the allowed set.

    Anything not in {monthly, quarterly, yearly} falls back to 'monthly'.
    """
    period = (str(val or "")).strip().lower()
    return period if period in _VALID_BILLING_PERIODS else "monthly"


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
        # SECURITY: never fall back to an unverified decode — a forged token must
        # not authenticate. If no known secret verified the signature, reject.
        return None

    for key in ("user_id", "id", "sub", "userId", "uid"):
        uid = _coerce_user_pk(payload.get(key))
        if uid is not None:
            return uid
    return None


def get_current_user():
    """Current user from the server session or a SIGNED Bearer JWT only.
    The forgeable X-User-Id header is no longer trusted (see auth_core)."""
    from auth_core import authenticated_user_id
    uid = authenticated_user_id()
    if uid is None:
        return None
    try:
        return db.session.get(User, uid)
    except Exception:
        return None


def get_current_admin():
    """Current platform admin from the server session or a SIGNED admin JWT only.
    The forgeable X-Admin-Id header and ?admin_id= query param are no longer trusted."""
    from auth_core import authenticated_admin_id
    aid = authenticated_admin_id()
    if aid is None:
        return None
    try:
        return db.session.get(Admin, aid)
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

def _build_plans_payload(rows) -> dict:
    """Build the {slug: {...plan.to_dict(), ...feature matrix}} map for a list
    of SubscriptionPlan rows. Shared by /plans and /my-plans so the response
    shape is identical."""
    from subscription.plan_models import PlanFeatureAccess, SubscriptionFeature

    plans = {}
    for plan in rows:
        matrix = {}
        for acc in PlanFeatureAccess.query.filter_by(plan_id=plan.id).all():
            feat = SubscriptionFeature.query.filter_by(key=acc.feature_key).first()
            if feat and feat.feature_type == "limit":
                matrix[acc.feature_key] = acc.limit_value
            else:
                matrix[acc.feature_key] = acc.enabled
        plans[plan.slug] = {
            **plan.to_dict(),
            **matrix,
        }
    return plans


@subscription_bp.route("/plans", methods=["GET"])
def list_plans():
    """
    GET /api/subscription/plans — public plan catalog from DB.
    """
    from subscription.plan_models import SubscriptionPlan

    rows = SubscriptionPlan.query.filter(
        SubscriptionPlan.is_public.is_(True),
        SubscriptionPlan.is_active.is_(True),
        or_(SubscriptionPlan.plan_scope == PLAN_SCOPE_GLOBAL, SubscriptionPlan.plan_scope.is_(None)),
    ).order_by(
        SubscriptionPlan.sort_order
    ).all()

    if not rows:
        # No seeded catalog yet (fresh DB / seed not run). Return an EMPTY catalog
        # so the frontend shows its graceful empty-state — the raw PLAN_FEATURES
        # dicts lack name/price/slug and would render broken cards.
        return jsonify({"success": True, "plans": {}})

    return jsonify({"success": True, "plans": _build_plans_payload(rows)})


@subscription_bp.route("/contact-sales", methods=["POST"])
def contact_sales():
    """Public: Enterprise 'Contact Sales' form -> email the team. No auth (pricing page)."""
    import os
    import re
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    email = (data.get("email") or "").strip()
    phone = (data.get("phone") or "").strip()
    plan = (data.get("plan") or "Enterprise").strip()[:80]
    company = (data.get("company") or "").strip()[:120]
    message = (data.get("message") or "").strip()[:2000]
    if not name or not email or not phone:
        return jsonify({"success": False, "error": "name, email and phone are required"}), 400
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        return jsonify({"success": False, "error": "invalid email"}), 400

    dest_raw = (
        os.getenv("SALES_NOTIFY_EMAIL")
        or os.getenv("WAITLIST_NOTIFY_EMAIL")
        or os.getenv("DEFAULT_ADMIN_EMAIL")
        or "sociovia.ai@gmail.com"
    )
    to = [e.strip() for e in dest_raw.split(",") if e.strip()]
    subject = "[SocioChat] " + plan + " sales enquiry - " + name
    lines = [
        "New Contact Sales enquiry from the pricing page.",
        "",
        "Plan: " + plan,
        "Name: " + name,
        "Email: " + email,
        "Phone: " + phone,
    ]
    if company:
        lines.append("Company: " + company)
    if message:
        lines.append("Message: " + message)
    body = "\n".join(lines) + "\n"
    try:
        from mailer import send_mail
        send_mail(to, subject, body)
    except Exception as e:
        current_app.logger.exception("contact-sales email failed: %s", e)
        return jsonify({"success": False, "error": "could not send enquiry"}), 500
    return jsonify({"success": True, "message": "Thanks! Our team will reach out shortly."})


@subscription_bp.route("/my-plans", methods=["GET"])
@require_auth
def list_my_plans(user):
    """
    GET /api/subscription/my-plans — tenant-aware plan catalog for the current
    user. Same response shape as /plans but built ONLY from the rows visible to
    this user (their tenant's plans, or the global SocioChat catalog for
    internal users).
    """
    from subscription.service import get_user_plan_rows, get_current_subscription_info

    rows = get_user_plan_rows(user)
    return jsonify({
        "success": True,
        "plans": _build_plans_payload(rows),
        "current": get_current_subscription_info(user),
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
        "billing_scope": getattr(user, "billing_scope", None) or "global",
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

    if not is_plan_assignable_to_user(user, new_plan):
        return jsonify({
            "success": False,
            "error": "invalid_plan",
            "valid_plans": get_assignable_plan_slugs_for_user(user),
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


@subscription_bp.route("/select-plan", methods=["POST"])
@require_auth
def select_plan(user):
    """User selects a plan after signup (before or without payment)."""
    from datetime import datetime, timezone, timedelta

    from subscription.service import get_assignable_user_plan_slugs

    data = request.get_json() or {}
    new_plan = (data.get("plan") or "").strip().lower()

    assignable = get_assignable_user_plan_slugs(user)
    if new_plan not in assignable:
        return jsonify({"success": False, "error": "invalid_plan", "valid_plans": assignable}), 400

    # Payment gate: only FREE plans (beta / price 0) may be granted directly.
    # A PRICED plan must go through PayU — it only activates in the verified
    # payment callback, so the paid tiers cannot be bypassed here. A custom
    # (null-price, e.g. enterprise) plan is NOT self-serviceable: it requires a
    # sales/admin path, so we never auto-grant it for free.
    from subscription.plan_models import SubscriptionPlan
    plan_row = SubscriptionPlan.query.filter_by(slug=new_plan).first()
    price = plan_row.price_monthly_inr if plan_row else None
    if new_plan != "beta":
        if price is None:
            return jsonify({
                "success": False,
                "error": "contact_sales",
                "plan": new_plan,
            }), 400
        if price > 0:
            return jsonify({
                "success": False,
                "error": "payment_required",
                "requires_payment": True,
                "plan": new_plan,
            }), 402

    old_plan = user.plan or "beta"
    if new_plan == "beta":
        user.beta_expires_at = datetime.now(timezone.utc) + timedelta(days=30)

    user.plan = new_plan
    db.session.commit()

    return jsonify({
        "success": True,
        "plan": new_plan,
        "old_plan": old_plan,
        "limits": get_plan_limits(user),
    })


# =============================================================================
# Admin — Plan & Feature Matrix
# =============================================================================

@subscription_bp.route("/admin/plans", methods=["GET"])
@require_admin
def admin_list_plans(admin):
    from subscription.plan_models import SubscriptionPlan, SubscriptionFeature, PlanFeatureAccess

    plans = SubscriptionPlan.query.filter(
        or_(SubscriptionPlan.plan_scope == PLAN_SCOPE_GLOBAL, SubscriptionPlan.plan_scope.is_(None))
    ).order_by(SubscriptionPlan.sort_order).all()
    features = SubscriptionFeature.query.filter_by(is_active=True).order_by(
        SubscriptionFeature.sort_order
    ).all()

    matrix = {}
    for plan in plans:
        matrix[plan.slug] = {}
        for acc in PlanFeatureAccess.query.filter_by(plan_id=plan.id).all():
            matrix[plan.slug][acc.feature_key] = {
                "enabled": acc.enabled,
                "limit_value": acc.limit_value,
            }

    return jsonify({
        "success": True,
        "plans": [_plan_admin_dict(p) for p in plans],
        "features": [f.to_dict() for f in features],
        "matrix": matrix,
    })


def _plan_admin_dict(plan) -> dict:
    """Plan row for admin UI with usage + delete eligibility."""
    from subscription.constants import VALID_PLANS

    scope = getattr(plan, "plan_scope", None) or PLAN_SCOPE_GLOBAL
    if scope == PLAN_SCOPE_PRIVATE:
        user_count = User.query.filter_by(plan=plan.slug, billing_scope=BILLING_SCOPE_PRIVATE).count()
    else:
        user_count = User.query.filter_by(plan=plan.slug).count()
    is_system = plan.slug in VALID_PLANS and scope == PLAN_SCOPE_GLOBAL
    data = plan.to_dict()
    data["user_count"] = user_count
    data["is_system"] = is_system
    data["is_deletable"] = (not is_system) and user_count == 0
    return data


@subscription_bp.route("/admin/plans", methods=["POST"])
@require_admin
def admin_create_plan(admin):
    from subscription.plan_models import SubscriptionPlan, SubscriptionFeature, PlanFeatureAccess, PlanConfigAuditLog

    data = request.get_json() or {}
    slug = (data.get("slug") or "").strip().lower()
    name = (data.get("name") or "").strip()

    if not slug or not name:
        return jsonify({"success": False, "error": "slug_and_name_required"}), 400

    if SubscriptionPlan.query.filter_by(slug=slug).first():
        return jsonify({"success": False, "error": "slug_exists"}), 400

    plan = SubscriptionPlan(
        slug=slug,
        name=name,
        description=data.get("description"),
        offer_text=(data.get("offer_text") or None),
        price_monthly_inr=data.get("price_monthly_inr"),
        billing_period=_normalize_billing_period(data.get("billing_period")),
        is_public=bool(data.get("is_public", True)),
        sort_order=int(data.get("sort_order", 99)),
        plan_scope=PLAN_SCOPE_GLOBAL,
    )
    db.session.add(plan)
    db.session.flush()

    for feat in SubscriptionFeature.query.filter_by(is_active=True).all():
        db.session.add(PlanFeatureAccess(
            plan_id=plan.id,
            feature_key=feat.key,
            enabled=bool(data.get("default_enabled", False)),
            limit_value=-1 if feat.feature_type == "limit" else None,
        ))

    db.session.add(PlanConfigAuditLog(
        admin_id=admin.id,
        admin_email=admin.email,
        action="create_plan",
        plan_slug=slug,
        new_value=name,
    ))
    db.session.commit()

    return jsonify({"success": True, "plan": _plan_admin_dict(plan)}), 201


@subscription_bp.route("/admin/plans/<slug>", methods=["PUT"])
@require_admin
def admin_update_plan_catalog(admin, slug):
    from subscription.plan_models import SubscriptionPlan, PlanConfigAuditLog

    plan = SubscriptionPlan.query.filter_by(slug=slug).first()
    if not plan:
        return jsonify({"success": False, "error": "plan_not_found"}), 404

    data = request.get_json() or {}
    old_name = plan.name

    for field in ("name", "description", "price_monthly_inr", "is_public", "is_active", "sort_order"):
        if field in data:
            setattr(plan, field, data[field])
    if "offer_text" in data:
        plan.offer_text = (data.get("offer_text") or None)
    if "billing_period" in data:
        plan.billing_period = _normalize_billing_period(data.get("billing_period"))

    db.session.add(PlanConfigAuditLog(
        admin_id=admin.id,
        admin_email=admin.email,
        action="update_plan",
        plan_slug=slug,
        old_value=old_name,
        new_value=plan.name,
    ))
    db.session.commit()

    return jsonify({"success": True, "plan": _plan_admin_dict(plan)})


@subscription_bp.route("/admin/plans/<slug>", methods=["DELETE"])
@require_admin
def admin_delete_plan(admin, slug):
    from subscription.plan_models import SubscriptionPlan, PlanFeatureAccess, PlanConfigAuditLog
    from subscription.constants import VALID_PLANS

    plan = SubscriptionPlan.query.filter_by(slug=slug).first()
    if not plan:
        return jsonify({"success": False, "error": "plan_not_found"}), 404

    if slug in VALID_PLANS:
        plan_row = SubscriptionPlan.query.filter_by(slug=slug).first()
        if plan_row and (getattr(plan_row, "plan_scope", None) or PLAN_SCOPE_GLOBAL) == PLAN_SCOPE_GLOBAL:
            return jsonify({
                "success": False,
                "error": "system_plan_protected",
                "message": "Built-in plans cannot be deleted. Set inactive instead.",
            }), 400

    user_count = User.query.filter_by(plan=slug).count()
    if user_count > 0:
        return jsonify({
            "success": False,
            "error": "plan_in_use",
            "message": f"Cannot delete: {user_count} user(s) are on this plan.",
            "user_count": user_count,
        }), 400

    PlanFeatureAccess.query.filter_by(plan_id=plan.id).delete(synchronize_session=False)
    plan_name = plan.name
    db.session.delete(plan)
    db.session.add(PlanConfigAuditLog(
        admin_id=admin.id,
        admin_email=admin.email,
        action="delete_plan",
        plan_slug=slug,
        old_value=plan_name,
    ))
    db.session.commit()

    return jsonify({"success": True, "deleted": slug})


@subscription_bp.route("/admin/plans/<slug>/features", methods=["PUT"])
@require_admin
def admin_update_plan_features(admin, slug):
    from subscription.plan_models import SubscriptionPlan, PlanFeatureAccess, PlanConfigAuditLog

    plan = SubscriptionPlan.query.filter_by(slug=slug).first()
    if not plan:
        return jsonify({"success": False, "error": "plan_not_found"}), 404

    data = request.get_json() or {}
    updates = data.get("features") or data.get("matrix") or {}

    for feature_key, cfg in updates.items():
        if isinstance(cfg, bool):
            enabled, limit_value = cfg, None
        else:
            enabled = bool(cfg.get("enabled", False))
            limit_value = cfg.get("limit_value")

        row = PlanFeatureAccess.query.filter_by(plan_id=plan.id, feature_key=feature_key).first()
        old_val = None
        if row:
            old_val = f"enabled={row.enabled},limit={row.limit_value}"
            row.enabled = enabled
            if limit_value is not None:
                row.limit_value = int(limit_value)
        else:
            row = PlanFeatureAccess(
                plan_id=plan.id,
                feature_key=feature_key,
                enabled=enabled,
                limit_value=int(limit_value) if limit_value is not None else None,
            )
            db.session.add(row)

        db.session.add(PlanConfigAuditLog(
            admin_id=admin.id,
            admin_email=admin.email,
            action="update_feature_access",
            plan_slug=slug,
            feature_key=feature_key,
            old_value=old_val,
            new_value=f"enabled={enabled},limit={limit_value}",
        ))

    db.session.commit()
    return jsonify({"success": True, "message": "features_updated"})


@subscription_bp.route("/admin/users/<int:user_id>/features", methods=["GET"])
@require_admin
def admin_get_user_features(admin, user_id):
    """
    GET /api/subscription/admin/users/<user_id>/features

    Return access-type features, plan defaults, and per-user overrides.
    """
    from subscription.plan_models import SubscriptionFeature, UserFeatureAccess
    from subscription.service import load_plan_matrix, LIMIT_KEYS

    user = db.session.get(User, user_id)
    if not user:
        return jsonify({"success": False, "error": "user_not_found"}), 404

    feats = SubscriptionFeature.query.filter_by(
        is_active=True, feature_type="access"
    ).order_by(SubscriptionFeature.sort_order).all()

    matrix = load_plan_matrix(get_user_plan(user))
    plan_defaults = {f.key: bool(matrix.get(f.key, False)) for f in feats}

    overrides = {
        ov.feature_key: bool(ov.enabled)
        for ov in UserFeatureAccess.query.filter_by(user_id=user.id)
        if ov.feature_key not in LIMIT_KEYS
    }

    return jsonify({
        "success": True,
        "user": {
            "id": user.id,
            "name": user.name,
            "email": user.email,
            "plan": user.plan or "beta",
            "billing_scope": getattr(user, "billing_scope", None) or BILLING_SCOPE_GLOBAL,
        },
        "features": [f.to_dict() for f in feats],
        "plan_defaults": plan_defaults,
        "overrides": overrides,
    })


@subscription_bp.route("/admin/users/<int:user_id>/features", methods=["PUT"])
@require_admin
def admin_update_user_features(admin, user_id):
    """
    PUT /api/subscription/admin/users/<user_id>/features

    Body: {"overrides": {"<feature_key>": <value>}}
    - access feature:  true -> force ON, false -> force OFF, null -> inherit
    - limit  feature:  <int> -> set the per-user limit (e.g. max workspaces;
                       -1 = unlimited), null -> inherit from plan/tenant
    """
    from subscription.plan_models import SubscriptionFeature, UserFeatureAccess, PlanConfigAuditLog
    from subscription.service import LIMIT_KEYS

    user = db.session.get(User, user_id)
    if not user:
        return jsonify({"success": False, "error": "user_not_found"}), 404

    data = request.get_json() or {}
    overrides = data.get("overrides")
    if not isinstance(overrides, dict):
        return jsonify({"success": False, "error": "overrides_required"}), 400

    valid_access_keys = {
        f.key for f in SubscriptionFeature.query.filter_by(
            is_active=True, feature_type="access"
        ).all()
    }
    valid_limit_keys = {
        f.key for f in SubscriptionFeature.query.filter_by(
            is_active=True, feature_type="limit"
        ).all()
    }

    def _state_str(val):
        if val is None:
            return "inherit"
        return "on" if val else "off"

    for key, val in overrides.items():
        is_limit = key in valid_limit_keys or key in LIMIT_KEYS
        if key not in valid_access_keys and not is_limit:
            continue

        row = UserFeatureAccess.query.filter_by(user_id=user.id, feature_key=key).first()

        if is_limit:
            # Per-user NUMERIC limit override (e.g. max workspaces).
            old_state = ("inherit" if row is None or row.limit_value is None
                         else str(row.limit_value))
            if val is None:
                if row is not None:
                    db.session.delete(row)
                new_state = "inherit"
            else:
                try:
                    limit_value = int(val)
                except (TypeError, ValueError):
                    continue
                if row is not None:
                    row.limit_value = limit_value
                    if row.enabled is None:
                        row.enabled = True
                else:
                    db.session.add(UserFeatureAccess(
                        user_id=user.id,
                        feature_key=key,
                        enabled=True,
                        limit_value=limit_value,
                    ))
                new_state = str(limit_value)
        else:
            old_state = ("inherit" if row is None
                         else ("on" if row.enabled else "off"))
            if val is None:
                if row is not None:
                    db.session.delete(row)
                new_state = "inherit"
            else:
                enabled = bool(val)
                if row is not None:
                    row.enabled = enabled
                else:
                    db.session.add(UserFeatureAccess(
                        user_id=user.id,
                        feature_key=key,
                        enabled=enabled,
                    ))
                new_state = _state_str(enabled)

        db.session.add(PlanConfigAuditLog(
            admin_id=admin.id,
            admin_email=admin.email,
            action="update_user_feature_access",
            plan_slug=user.plan,
            feature_key=key,
            old_value=old_state,
            new_value=new_state,
        ))

    db.session.commit()

    try:
        from monolith_integration.trigger import schedule_capabilities_resync_for_user
        schedule_capabilities_resync_for_user(int(user.id), reason="user_feature_override")
    except Exception:
        pass

    access_overrides = {}
    limit_overrides = {}
    for ov in UserFeatureAccess.query.filter_by(user_id=user.id):
        if ov.feature_key in LIMIT_KEYS or ov.limit_value is not None:
            limit_overrides[ov.feature_key] = ov.limit_value
        else:
            access_overrides[ov.feature_key] = bool(ov.enabled)

    return jsonify({
        "success": True,
        "overrides": access_overrides,
        "limit_overrides": limit_overrides,
    })


@subscription_bp.route("/admin/audit", methods=["GET"])
@require_admin
def admin_plan_audit(admin):
    from subscription.plan_models import PlanConfigAuditLog

    limit = min(request.args.get("limit", 50, type=int), 200)
    rows = PlanConfigAuditLog.query.order_by(PlanConfigAuditLog.created_at.desc()).limit(limit).all()
    return jsonify({"success": True, "logs": [r.to_dict() for r in rows]})


# =============================================================================
# Admin — Private Slot (one shared pool)
# =============================================================================

def _serialize_slot_user(user: User) -> dict:
    return {
        "id": user.id,
        "name": user.name,
        "email": user.email,
        "phone": user.phone,
        "plan": user.plan or "beta",
        "billing_scope": getattr(user, "billing_scope", None) or BILLING_SCOPE_GLOBAL,
        "status": user.status,
        "created_at": user.created_at.isoformat() if user.created_at else None,
    }


def _private_plans_matrix():
    from subscription.plan_models import SubscriptionPlan, SubscriptionFeature, PlanFeatureAccess

    plans = SubscriptionPlan.query.filter_by(plan_scope=PLAN_SCOPE_PRIVATE).order_by(
        SubscriptionPlan.sort_order
    ).all()
    features = SubscriptionFeature.query.filter_by(is_active=True).order_by(
        SubscriptionFeature.sort_order
    ).all()
    matrix = {}
    for plan in plans:
        matrix[plan.slug] = {}
        for acc in PlanFeatureAccess.query.filter_by(plan_id=plan.id).all():
            matrix[plan.slug][acc.feature_key] = {
                "enabled": acc.enabled,
                "limit_value": acc.limit_value,
            }
    return plans, features, matrix


@subscription_bp.route("/admin/private-slot", methods=["GET"])
@require_admin
def admin_private_slot_overview(admin):
    """Overview: all users, private members, private plans matrix."""
    search = (request.args.get("search") or "").strip().lower()

    query = User.query.order_by(User.created_at.desc())
    if search:
        like = f"%{search}%"
        filters = [
            User.email.ilike(like),
            User.name.ilike(like),
            User.phone.ilike(like),
        ]
        if search.isdigit():
            filters.append(User.id == int(search))
        query = query.filter(or_(*filters))

    users = query.limit(500).all()
    private_members = [
        _serialize_slot_user(u) for u in users
        if (getattr(u, "billing_scope", None) or BILLING_SCOPE_GLOBAL) == BILLING_SCOPE_PRIVATE
    ]

    plans, features, matrix = _private_plans_matrix()

    return jsonify({
        "success": True,
        "users": [_serialize_slot_user(u) for u in users],
        "private_members": private_members,
        "private_plans": [_plan_admin_dict(p) for p in plans],
        "global_plan_options": PRIVATE_SLOT_GLOBAL_TIERS,
        "features": [f.to_dict() for f in features],
        "matrix": matrix,
        "stats": {
            "private_count": User.query.filter_by(billing_scope=BILLING_SCOPE_PRIVATE).count(),
            "global_count": User.query.filter(
                or_(User.billing_scope == BILLING_SCOPE_GLOBAL, User.billing_scope.is_(None))
            ).count(),
        },
    })


@subscription_bp.route("/admin/private-slot/users/<int:user_id>/scope", methods=["PATCH"])
@require_admin
def admin_private_slot_set_scope(admin, user_id):
    """Move user between global and private slot."""
    from subscription.plan_models import PlanConfigAuditLog

    user = db.session.get(User, user_id)
    if not user:
        return jsonify({"success": False, "error": "user_not_found"}), 404

    data = request.get_json() or {}
    scope = (data.get("billing_scope") or "").strip().lower()
    if scope not in (BILLING_SCOPE_GLOBAL, BILLING_SCOPE_PRIVATE):
        return jsonify({"success": False, "error": "invalid_billing_scope"}), 400

    old_scope = getattr(user, "billing_scope", None) or BILLING_SCOPE_GLOBAL
    user.billing_scope = scope

    db.session.add(PlanConfigAuditLog(
        admin_id=admin.id,
        admin_email=admin.email,
        action="private_slot_scope_change",
        plan_slug=user.plan,
        old_value=old_scope,
        new_value=scope,
    ))
    db.session.commit()

    return jsonify({"success": True, "user": _serialize_slot_user(user)})


@subscription_bp.route("/admin/private-slot/users/scope", methods=["PATCH"])
@require_admin
def admin_private_slot_bulk_scope(admin):
    """Bulk move users between global and private slot."""
    from subscription.plan_models import PlanConfigAuditLog

    data = request.get_json() or {}
    scope = (data.get("billing_scope") or "").strip().lower()
    raw_ids = data.get("user_ids") or []

    if scope not in (BILLING_SCOPE_GLOBAL, BILLING_SCOPE_PRIVATE):
        return jsonify({"success": False, "error": "invalid_billing_scope"}), 400

    if not isinstance(raw_ids, list) or not raw_ids:
        return jsonify({"success": False, "error": "user_ids_required"}), 400

    user_ids = []
    for val in raw_ids:
        try:
            user_ids.append(int(val))
        except (TypeError, ValueError):
            continue

    if not user_ids:
        return jsonify({"success": False, "error": "user_ids_invalid"}), 400

    updated = []
    for uid in user_ids:
        user = db.session.get(User, uid)
        if not user:
            continue
        old_scope = getattr(user, "billing_scope", None) or BILLING_SCOPE_GLOBAL
        if old_scope == scope:
            continue
        user.billing_scope = scope
        db.session.add(PlanConfigAuditLog(
            admin_id=admin.id,
            admin_email=admin.email,
            action="private_slot_scope_change_bulk",
            plan_slug=user.plan,
            old_value=old_scope,
            new_value=scope,
        ))
        updated.append(_serialize_slot_user(user))

    db.session.commit()

    return jsonify({
        "success": True,
        "updated_count": len(updated),
        "users": updated,
        "billing_scope": scope,
    })


@subscription_bp.route("/admin/private-slot/users/<int:user_id>/plan", methods=["PUT"])
@require_admin
def admin_private_slot_set_plan(admin, user_id):
    """Assign plan to a private-slot user (global tiers + private plans)."""
    user = db.session.get(User, user_id)
    if not user:
        return jsonify({"success": False, "error": "user_not_found"}), 404

    if not is_private_slot_user(user):
        return jsonify({
            "success": False,
            "error": "not_private_slot_user",
            "message": "User must be in private slot before assigning a private plan.",
        }), 400

    data = request.get_json() or {}
    new_plan = (data.get("plan") or "").strip().lower()
    if not new_plan:
        return jsonify({"success": False, "error": "plan_required"}), 400

    if not is_plan_assignable_to_user(user, new_plan):
        return jsonify({
            "success": False,
            "error": "invalid_plan",
            "valid_plans": get_assignable_plan_slugs_for_user(user),
        }), 400

    old_plan = user.plan or "beta"
    try:
        change_user_plan(user, new_plan, admin_id=admin.id, reason=data.get("reason") or "Private slot plan change")
    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 400

    # Set the subscription end date: from an explicit date if provided,
    # otherwise auto-compute from the plan's billing period.
    from datetime import datetime, timezone
    from subscription.service import expiry_from_period
    from subscription.plan_models import SubscriptionPlan

    _exp_raw = (data.get("subscription_expires_at") or data.get("expires_at"))
    if _exp_raw:
        try:
            user.subscription_expires_at = datetime.fromisoformat(str(_exp_raw).replace("Z", "+00:00"))
        except (ValueError, TypeError):
            pass
    else:
        _sp = SubscriptionPlan.query.filter_by(slug=new_plan).first()
        _auto = expiry_from_period(datetime.now(timezone.utc), getattr(_sp, "billing_period", None))
        if _auto is not None:
            user.subscription_expires_at = _auto
    db.session.commit()

    return jsonify({
        "success": True,
        "user": _serialize_slot_user(user),
        "old_plan": old_plan,
        "new_plan": new_plan,
    })


@subscription_bp.route("/admin/private-slot/plans", methods=["POST"])
@require_admin
def admin_private_slot_create_plan(admin):
    """Create a plan that only applies to private-slot users."""
    from subscription.plan_models import SubscriptionPlan, SubscriptionFeature, PlanFeatureAccess, PlanConfigAuditLog

    data = request.get_json() or {}
    slug = (data.get("slug") or "").strip().lower()
    name = (data.get("name") or "").strip()

    if not slug or not name:
        return jsonify({"success": False, "error": "slug_and_name_required"}), 400

    if SubscriptionPlan.query.filter_by(slug=slug).first():
        return jsonify({"success": False, "error": "slug_exists"}), 400

    plan = SubscriptionPlan(
        slug=slug,
        name=name,
        description=data.get("description"),
        offer_text=(data.get("offer_text") or None),
        price_monthly_inr=data.get("price_monthly_inr"),
        billing_period=_normalize_billing_period(data.get("billing_period")),
        is_public=False,
        is_active=True,
        sort_order=int(data.get("sort_order", 99)),
        plan_scope=PLAN_SCOPE_PRIVATE,
    )
    db.session.add(plan)
    db.session.flush()

    for feat in SubscriptionFeature.query.filter_by(is_active=True).all():
        db.session.add(PlanFeatureAccess(
            plan_id=plan.id,
            feature_key=feat.key,
            enabled=bool(data.get("default_enabled", False)),
            limit_value=-1 if feat.feature_type == "limit" else None,
        ))

    db.session.add(PlanConfigAuditLog(
        admin_id=admin.id,
        admin_email=admin.email,
        action="create_private_plan",
        plan_slug=slug,
        new_value=name,
    ))
    db.session.commit()

    return jsonify({"success": True, "plan": _plan_admin_dict(plan)}), 201


@subscription_bp.route("/admin/private-slot/plans/<slug>", methods=["PUT"])
@require_admin
def admin_private_slot_update_plan(admin, slug):
    from subscription.plan_models import SubscriptionPlan, PlanConfigAuditLog

    plan = SubscriptionPlan.query.filter_by(slug=slug, plan_scope=PLAN_SCOPE_PRIVATE).first()
    if not plan:
        return jsonify({"success": False, "error": "plan_not_found"}), 404

    data = request.get_json() or {}
    old_name = plan.name
    for field in ("name", "description", "price_monthly_inr", "is_active", "sort_order"):
        if field in data:
            setattr(plan, field, data[field])
    if "offer_text" in data:
        plan.offer_text = (data.get("offer_text") or None)
    if "billing_period" in data:
        plan.billing_period = _normalize_billing_period(data.get("billing_period"))

    db.session.add(PlanConfigAuditLog(
        admin_id=admin.id,
        admin_email=admin.email,
        action="update_private_plan",
        plan_slug=slug,
        old_value=old_name,
        new_value=plan.name,
    ))
    db.session.commit()
    return jsonify({"success": True, "plan": _plan_admin_dict(plan)})


@subscription_bp.route("/admin/private-slot/plans/<slug>", methods=["DELETE"])
@require_admin
def admin_private_slot_delete_plan(admin, slug):
    from subscription.plan_models import SubscriptionPlan, PlanFeatureAccess, PlanConfigAuditLog

    plan = SubscriptionPlan.query.filter_by(slug=slug, plan_scope=PLAN_SCOPE_PRIVATE).first()
    if not plan:
        return jsonify({"success": False, "error": "plan_not_found"}), 404

    user_count = User.query.filter_by(plan=slug, billing_scope=BILLING_SCOPE_PRIVATE).count()
    if user_count > 0:
        return jsonify({
            "success": False,
            "error": "plan_in_use",
            "message": f"Cannot delete: {user_count} private user(s) on this plan.",
            "user_count": user_count,
        }), 400

    PlanFeatureAccess.query.filter_by(plan_id=plan.id).delete(synchronize_session=False)
    plan_name = plan.name
    db.session.delete(plan)
    db.session.add(PlanConfigAuditLog(
        admin_id=admin.id,
        admin_email=admin.email,
        action="delete_private_plan",
        plan_slug=slug,
        old_value=plan_name,
    ))
    db.session.commit()
    return jsonify({"success": True, "deleted": slug})


@subscription_bp.route("/admin/private-slot/plans/<slug>/features", methods=["PUT"])
@require_admin
def admin_private_slot_update_plan_features(admin, slug):
    from subscription.plan_models import SubscriptionPlan, PlanFeatureAccess, PlanConfigAuditLog

    plan = SubscriptionPlan.query.filter_by(slug=slug, plan_scope=PLAN_SCOPE_PRIVATE).first()
    if not plan:
        return jsonify({"success": False, "error": "plan_not_found"}), 404

    data = request.get_json() or {}
    features = data.get("features") or {}

    for feature_key, cfg in features.items():
        if not isinstance(cfg, dict):
            continue
        enabled = bool(cfg.get("enabled", False))
        limit_value = cfg.get("limit_value")
        row = PlanFeatureAccess.query.filter_by(plan_id=plan.id, feature_key=feature_key).first()
        old_val = None
        if row:
            old_val = f"enabled={row.enabled},limit={row.limit_value}"
            row.enabled = enabled
            if limit_value is not None:
                row.limit_value = limit_value
        else:
            db.session.add(PlanFeatureAccess(
                plan_id=plan.id,
                feature_key=feature_key,
                enabled=enabled,
                limit_value=limit_value,
            ))

        db.session.add(PlanConfigAuditLog(
            admin_id=admin.id,
            admin_email=admin.email,
            action="update_private_feature_access",
            plan_slug=slug,
            feature_key=feature_key,
            old_value=old_val,
            new_value=f"enabled={enabled},limit={limit_value}",
        ))

    db.session.commit()
    return jsonify({"success": True, "message": "features_updated"})
