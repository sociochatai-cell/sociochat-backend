"""
Subscription Module
===================

Provides subscription tier management including:
- Plan definitions and feature limits
- Usage tracking and limit enforcement
- Decorators for route protection
- Admin functions for plan management

Plans: beta, starter, growth, enterprise
"""

from subscription.constants import (
    PLAN_BETA,
    PLAN_STARTER,
    PLAN_GROWTH,
    PLAN_ENTERPRISE,
    VALID_PLANS,
    UNLIMITED,
    PLAN_FEATURES,
    get_plan_features,
    is_unlimited,
)

from subscription.models import (
    SubscriptionUsage,
    AdSpendTracking,
    PlanChangeHistory,
)

from subscription.service import (
    get_user_plan,
    get_plan_limits,
    check_feature_access,
    has_feature,
    check_workspace_limit,
    check_message_limit,
    check_flow_limit,
    check_image_credits,
    check_ad_spend_limit,
    record_message_sent,
    record_image_credits_used,
    record_ad_spend,
    get_user_usage_stats,
    change_user_plan,
    SubscriptionError,
)

from subscription.decorators import (
    require_feature,
    require_message_limit,
    require_image_credits,
    require_workspace_limit,
    require_flow_limit,
)

from subscription.routes import subscription_bp

__all__ = [
    # Constants
    "PLAN_BETA",
    "PLAN_STARTER", 
    "PLAN_GROWTH",
    "PLAN_ENTERPRISE",
    "VALID_PLANS",
    "UNLIMITED",
    "PLAN_FEATURES",
    "get_plan_features",
    "is_unlimited",
    # Models
    "SubscriptionUsage",
    "AdSpendTracking",
    "PlanChangeHistory",
    # Service functions
    "get_user_plan",
    "get_plan_limits",
    "check_feature_access",
    "has_feature",
    "check_workspace_limit",
    "check_message_limit",
    "check_flow_limit",
    "check_image_credits",
    "check_ad_spend_limit",
    "record_message_sent",
    "record_image_credits_used",
    "record_ad_spend",
    "get_user_usage_stats",
    "change_user_plan",
    "SubscriptionError",
    # Decorators
    "require_feature",
    "require_message_limit",
    "require_image_credits",
    "require_workspace_limit",
    "require_flow_limit",
    # Blueprint
    "subscription_bp",
]
