"""
Subscription Module - Constants
===============================

Plan definitions and feature limits for Sociovia subscription tiers.
"""

# Plan names
PLAN_BETA = "beta"
PLAN_STARTER = "starter"
PLAN_GROWTH = "growth"
PLAN_ENTERPRISE = "enterprise"

# All valid plans
VALID_PLANS = [PLAN_BETA, PLAN_STARTER, PLAN_GROWTH, PLAN_ENTERPRISE]

# -1 means unlimited
UNLIMITED = -1

# Feature flags and limits per plan
PLAN_FEATURES = {
    # Beta - permissive for testing
    PLAN_BETA: {
        "workspaces": UNLIMITED,
        "users": UNLIMITED,
        "messages_per_day": UNLIMITED,
        "interactive_flows": UNLIMITED,
        "image_credits": 10,
        "ad_spend_limit": UNLIMITED,
        # Feature access
        "unified_dashboard_analytics": True,
        "meta_ai_ad_optimization": True,
        "whatsapp_automation": True,
        "image_generation": True,
        "ai_chatbot_dashboard": True,
        "whatsapp_smart_ai": True,
        "human_agent_whatsapp": True,
    },
    # Starter - basic tier
    PLAN_STARTER: {
        "workspaces": 1,
        "users": 1,
        "messages_per_day": 1000,
        "interactive_flows": 5,
        "image_credits": 0,
        "ad_spend_limit": 100000,  # INR
        # Feature access
        "unified_dashboard_analytics": True,
        "meta_ai_ad_optimization": True,
        "whatsapp_automation": True,
        "image_generation": False,
        "ai_chatbot_dashboard": False,
        "whatsapp_smart_ai": True,
        "human_agent_whatsapp": True,
    },
    # Growth - mid tier
    PLAN_GROWTH: {
        "workspaces": 3,
        "users": 3,
        "messages_per_day": 5000,
        "interactive_flows": 15,
        "image_credits": 100000,
        "ad_spend_limit": 500000,  # INR
        # Feature access
        "unified_dashboard_analytics": True,
        "meta_ai_ad_optimization": True,
        "whatsapp_automation": True,
        "image_generation": True,
        "ai_chatbot_dashboard": True,
        "whatsapp_smart_ai": True,
        "human_agent_whatsapp": True,
    },
    # Enterprise - all features
    PLAN_ENTERPRISE: {
        "workspaces": UNLIMITED,
        "users": UNLIMITED,
        "messages_per_day": UNLIMITED,
        "interactive_flows": UNLIMITED,
        "image_credits": UNLIMITED,
        "ad_spend_limit": UNLIMITED,
        # Feature access
        "unified_dashboard_analytics": True,
        "meta_ai_ad_optimization": True,
        "whatsapp_automation": True,
        "image_generation": True,
        "ai_chatbot_dashboard": True,
        "whatsapp_smart_ai": True,
        "human_agent_whatsapp": True,
    },
}


def get_plan_features(plan: str) -> dict:
    """Get features for a plan. Defaults to starter if plan not found."""
    return PLAN_FEATURES.get(plan, PLAN_FEATURES[PLAN_STARTER])


def is_unlimited(limit: int) -> bool:
    """Check if a limit value means unlimited."""
    return limit == UNLIMITED
