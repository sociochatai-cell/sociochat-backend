"""
Subscription Module - Service Layer
====================================

Core service functions for checking plan limits and recording usage.
"""

from datetime import datetime, date, timedelta
from typing import Optional, Tuple, Dict, Any
import logging
from sqlalchemy import func

from decimal import Decimal
from shared_models import db, User, Workspace, AIUsage
from subscription.constants import (
    PLAN_FEATURES, VALID_PLANS, PLAN_STARTER, UNLIMITED, 
    get_plan_features, is_unlimited
)
from subscription.models import SubscriptionUsage, AdSpendTracking, PlanChangeHistory

# Base cost in INR for 1 Gemini image = 1 credit
GEMINI_IMAGE_BASE_COST = Decimal("2.80")

logger = logging.getLogger(__name__)


class SubscriptionError(Exception):
    """Exception raised when subscription limit is exceeded."""
    def __init__(self, message: str, limit_type: str, current: int, limit: int):
        self.message = message
        self.limit_type = limit_type
        self.current = current
        self.limit = limit
        super().__init__(message)


# =============================================================================
# Get Plan Information
# =============================================================================

def get_user_plan(user: User) -> str:
    """Get user's current plan, defaulting to starter if not set."""
    if not user:
        return PLAN_STARTER
    plan = getattr(user, "plan", None)
    return plan if plan in VALID_PLANS else PLAN_STARTER


def get_plan_limits(user: User) -> dict:
    """Get all limits for user's current plan."""
    plan = get_user_plan(user)
    features = get_plan_features(plan)
    return {
        "plan": plan,
        "limits": {
            "workspaces": features["workspaces"],
            "users": features["users"],
            "messages_per_day": features["messages_per_day"],
            "interactive_flows": features["interactive_flows"],
            "image_credits": features["image_credits"],
            "ad_spend_limit": features["ad_spend_limit"],
        },
        "features": {
            "unified_dashboard_analytics": features["unified_dashboard_analytics"],
            "meta_ai_ad_optimization": features["meta_ai_ad_optimization"],
            "whatsapp_automation": features["whatsapp_automation"],
            "image_generation": features["image_generation"],
            "ai_chatbot_dashboard": features["ai_chatbot_dashboard"],
            "whatsapp_smart_ai": features["whatsapp_smart_ai"],
            "human_agent_whatsapp": features["human_agent_whatsapp"],
        }
    }


# =============================================================================
# Feature Access Checks
# =============================================================================

def check_feature_access(user: User, feature: str) -> Tuple[bool, Optional[str]]:
    """
    Check if user has access to a specific feature.
    
    Returns:
        Tuple of (allowed, error_message)
    """
    plan = get_user_plan(user)
    features = get_plan_features(plan)
    
    if feature not in features:
        return False, f"Unknown feature: {feature}"
    
    if features.get(feature, False):
        return True, None
    
    return False, f"Feature '{feature}' is not available on your {plan} plan. Please upgrade."


def has_feature(user: User, feature: str) -> bool:
    """Simple boolean check for feature access."""
    allowed, _ = check_feature_access(user, feature)
    return allowed


# =============================================================================
# Limit Checks
# =============================================================================

def check_workspace_limit(user: User) -> Tuple[bool, int, int]:
    """
    Check if user can create more workspaces.
    
    Returns:
        Tuple of (allowed, current_count, limit)
    """
    plan = get_user_plan(user)
    features = get_plan_features(plan)
    limit = features["workspaces"]
    
    if is_unlimited(limit):
        return True, 0, UNLIMITED
    
    current_count = Workspace.query.filter_by(user_id=user.id).count()
    
    return current_count < limit, current_count, limit


def check_message_limit(user: User, workspace_id: Optional[int] = None) -> Tuple[bool, int, int]:
    """
    Check if user can send more messages today.
    
    Returns:
        Tuple of (allowed, current_count, limit)
    """
    plan = get_user_plan(user)
    features = get_plan_features(plan)
    limit = features["messages_per_day"]
    
    if is_unlimited(limit):
        return True, 0, UNLIMITED
    
    today = date.today()
    
    # Get today's usage
    query = SubscriptionUsage.query.filter(
        SubscriptionUsage.user_id == user.id,
        SubscriptionUsage.usage_date == today
    )
    if workspace_id:
        query = query.filter(SubscriptionUsage.workspace_id == workspace_id)
    
    usage = query.first()
    current_count = usage.messages_sent if usage else 0
    
    return current_count < limit, current_count, limit


def check_flow_limit(user: User, workspace_id: int) -> Tuple[bool, int, int]:
    """
    Check if workspace can create more interactive flows.
    
    Returns:
        Tuple of (allowed, current_count, limit)
    """
    from sqlalchemy import text as sa_text
    
    plan = get_user_plan(user)
    features = get_plan_features(plan)
    limit = features["interactive_flows"]
    
    if is_unlimited(limit):
        return True, 0, UNLIMITED
    
    # Raw SQL to avoid cross-domain import of whatsapp.visual_automation_models
    current_count = db.session.execute(
        sa_text("SELECT COUNT(*) FROM whatsapp_visual_automations WHERE workspace_id = :wid"),
        {"wid": str(workspace_id)},
    ).scalar() or 0
    
    return current_count < limit, current_count, limit


def check_image_credits(user: User) -> Tuple[bool, float, int]:
    """
    Check if user has remaining image credits.
    
    Credits are calculated dynamically from AIUsage table:
    1 credit = cost of 1 Gemini image (GEMINI_IMAGE_BASE_COST INR).
    Other models consume proportional credits based on their cost.
    
    Returns:
        Tuple of (allowed, used_credits, limit)
    """
    plan = get_user_plan(user)
    features = get_plan_features(plan)
    limit = features["image_credits"]
    
    if is_unlimited(limit):
        return True, 0.0, UNLIMITED
    
    # Calculate credits from AIUsage cost_inr for image_generation feature
    first_of_month = date.today().replace(day=1)
    
    total_cost = db.session.query(func.coalesce(func.sum(AIUsage.cost_inr), 0)).filter(
        AIUsage.user_id == user.id,
        AIUsage.feature == "image_generation",
        AIUsage.created_at >= first_of_month
    ).scalar()
    
    # Convert cost to credits: cost_inr / GEMINI_IMAGE_BASE_COST
    used_credits = float(Decimal(str(total_cost)) / GEMINI_IMAGE_BASE_COST)
    
    return used_credits < limit, used_credits, limit


def check_ad_spend_limit(user: User, workspace_id: int) -> Tuple[bool, float, float]:
    """
    Check if workspace is within ad spend limit.
    
    Returns:
        Tuple of (allowed, current_spend_inr, limit_inr)
    """
    plan = get_user_plan(user)
    features = get_plan_features(plan)
    limit = features["ad_spend_limit"]
    
    if is_unlimited(limit):
        return True, 0, UNLIMITED
    
    # Get current billing period spend
    today = date.today()
    first_of_month = today.replace(day=1)
    
    tracking = AdSpendTracking.query.filter(
        AdSpendTracking.workspace_id == workspace_id,
        AdSpendTracking.billing_period_start <= today,
        AdSpendTracking.billing_period_end >= today
    ).first()
    
    current_spend = tracking.total_spend_inr if tracking else 0
    
    return current_spend < limit, current_spend, limit


# =============================================================================
# Usage Recording
# =============================================================================

def record_message_sent(
    user: User,
    workspace_id: Optional[int] = None,
    count: int = 1,
    *,
    _commit: bool = True,
) -> SubscriptionUsage:
    """Record that user sent messages. Creates/updates daily usage record."""
    today = date.today()
    
    usage = SubscriptionUsage.query.filter(
        SubscriptionUsage.user_id == user.id,
        SubscriptionUsage.workspace_id == workspace_id,
        SubscriptionUsage.usage_date == today
    ).first()
    
    if not usage:
        usage = SubscriptionUsage(
            user_id=user.id,
            workspace_id=workspace_id,
            usage_date=today,
            messages_sent=0,
            image_credits_used=0
        )
        db.session.add(usage)
    
    usage.messages_sent += count
    if _commit:
        db.session.commit()
    else:
        db.session.flush()
    
    return usage


def record_image_credits_used(user: User, workspace_id: Optional[int] = None, count: int = 1) -> SubscriptionUsage:
    """Record image credit usage."""
    today = date.today()
    
    usage = SubscriptionUsage.query.filter(
        SubscriptionUsage.user_id == user.id,
        SubscriptionUsage.workspace_id == workspace_id,
        SubscriptionUsage.usage_date == today
    ).first()
    
    if not usage:
        usage = SubscriptionUsage(
            user_id=user.id,
            workspace_id=workspace_id,
            usage_date=today,
            messages_sent=0,
            image_credits_used=0
        )
        db.session.add(usage)
    
    usage.image_credits_used += count
    db.session.commit()
    
    return usage


def record_ad_spend(workspace_id: int, amount_paise: int) -> AdSpendTracking:
    """Record ad spend for a workspace."""
    today = date.today()
    first_of_month = today.replace(day=1)
    
    # Calculate last day of month
    if today.month == 12:
        last_of_month = today.replace(year=today.year + 1, month=1, day=1) - timedelta(days=1)
    else:
        last_of_month = today.replace(month=today.month + 1, day=1) - timedelta(days=1)
    
    tracking = AdSpendTracking.query.filter(
        AdSpendTracking.workspace_id == workspace_id,
        AdSpendTracking.billing_period_start == first_of_month
    ).first()
    
    if not tracking:
        tracking = AdSpendTracking(
            workspace_id=workspace_id,
            billing_period_start=first_of_month,
            billing_period_end=last_of_month,
            total_spend_paise=0
        )
        db.session.add(tracking)
    
    tracking.total_spend_paise += amount_paise
    db.session.commit()
    
    return tracking


# =============================================================================
# Usage Statistics
# =============================================================================

def get_user_usage_stats(user: User, workspace_id: Optional[int] = None) -> Dict[str, Any]:
    """Get comprehensive usage statistics for a user."""
    plan = get_user_plan(user)
    features = get_plan_features(plan)
    today = date.today()
    first_of_month = today.replace(day=1)
    
    # Today's messages
    msg_query = db.session.query(func.coalesce(func.sum(SubscriptionUsage.messages_sent), 0)).filter(
        SubscriptionUsage.user_id == user.id,
        SubscriptionUsage.usage_date == today
    )
    if workspace_id:
        msg_query = msg_query.filter(SubscriptionUsage.workspace_id == workspace_id)
    messages_today = msg_query.scalar()
    
    # Monthly image credits (calculated from AIUsage cost_inr)
    total_image_cost = db.session.query(func.coalesce(func.sum(AIUsage.cost_inr), 0)).filter(
        AIUsage.user_id == user.id,
        AIUsage.feature == "image_generation",
        AIUsage.created_at >= first_of_month
    ).scalar()
    image_credits_used = float(Decimal(str(total_image_cost)) / GEMINI_IMAGE_BASE_COST)
    
    # Workspace count
    workspace_count = Workspace.query.filter_by(user_id=user.id).count()
    
    # Interactive flows (if workspace specified)
    flow_count = 0
    if workspace_id:
        try:
            from sqlalchemy import text as sa_text
            flow_count = db.session.execute(
                sa_text("SELECT COUNT(*) FROM whatsapp_visual_automations WHERE workspace_id = :wid"),
                {"wid": str(workspace_id)},
            ).scalar() or 0
        except Exception:
            pass
    
    # Ad spend (if workspace specified)
    ad_spend_inr = 0
    if workspace_id:
        tracking = AdSpendTracking.query.filter(
            AdSpendTracking.workspace_id == workspace_id,
            AdSpendTracking.billing_period_start <= today,
            AdSpendTracking.billing_period_end >= today
        ).first()
        ad_spend_inr = tracking.total_spend_inr if tracking else 0
    
    return {
        "plan": plan,
        "usage": {
            "messages_today": messages_today,
            "messages_limit": features["messages_per_day"],
            "image_credits_used": image_credits_used,
            "image_credits_limit": features["image_credits"],
            "workspaces": workspace_count,
            "workspaces_limit": features["workspaces"],
            "interactive_flows": flow_count,
            "interactive_flows_limit": features["interactive_flows"],
            "ad_spend_inr": ad_spend_inr,
            "ad_spend_limit": features["ad_spend_limit"],
        }
    }


# =============================================================================
# Admin Functions
# =============================================================================

def change_user_plan(user: User, new_plan: str, admin_id: Optional[int] = None, reason: Optional[str] = None) -> PlanChangeHistory:
    """Change user's plan and record the change."""
    if new_plan not in VALID_PLANS:
        raise ValueError(f"Invalid plan: {new_plan}. Must be one of {VALID_PLANS}")
    
    old_plan = get_user_plan(user)
    
    # Record history
    history = PlanChangeHistory(
        user_id=user.id,
        old_plan=old_plan,
        new_plan=new_plan,
        changed_by_admin_id=admin_id,
        reason=reason
    )
    db.session.add(history)
    
    # Update user plan
    user.plan = new_plan
    db.session.commit()

    try:
        from monolith_integration.trigger import schedule_capabilities_resync_for_user

        schedule_capabilities_resync_for_user(int(user.id), reason=reason or "plan_change")
    except Exception as exc:
        logger.warning("[whatsapp_integration] capability projection schedule failed: %s", exc)

    return history
