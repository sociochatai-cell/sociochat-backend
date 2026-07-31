"""
Subscription Module - Constants
===============================

Plan definitions and feature limits for Sociovia subscription tiers.
"""

# Plan names. Display labels (Free/Basic/Pro/Premium/Ultimate) live in the plan
# catalog (seed.PLAN_META); these slugs are the stable internal identifiers.
PLAN_BETA = "beta"           # display: Free
PLAN_STARTER = "starter"     # display: Basic
PLAN_GROWTH = "growth"       # display: Pro
PLAN_PREMIUM = "premium"     # display: Premium
PLAN_ENTERPRISE = "enterprise"  # display: Ultimate

# All valid plans
VALID_PLANS = [PLAN_BETA, PLAN_STARTER, PLAN_GROWTH, PLAN_PREMIUM, PLAN_ENTERPRISE]

# Billing / plan scope
BILLING_SCOPE_GLOBAL = "global"
BILLING_SCOPE_PRIVATE = "private"
PLAN_SCOPE_GLOBAL = "global"
PLAN_SCOPE_PRIVATE = "private"

# Global tier plans assignable inside private slot
PRIVATE_SLOT_GLOBAL_TIERS = [PLAN_STARTER, PLAN_GROWTH, PLAN_PREMIUM, PLAN_ENTERPRISE, PLAN_BETA]

# -1 means unlimited
UNLIMITED = -1

# Feature flags and limits per plan
PLAN_FEATURES = {
    # Free (beta) - bare bones: WhatsApp API + single-agent live chat + contacts/CRM.
    # Mirrors AiSensy's Free Forever (no broadcasts/templates/campaign tools/AI).
    PLAN_BETA: {
        "workspaces": 1,
        "users": 1,
        "messages_per_day": 250,
        "interactive_flows": 0,
        "image_credits": 0,
        "ad_spend_limit": 0,
        # Feature access — only inbox/contacts/CRM/coexistence
        "unified_dashboard_analytics": False,
        "meta_ai_ad_optimization": False,
        "whatsapp_automation": False,
        "image_generation": False,
        "ai_chatbot_dashboard": False,
        "whatsapp_smart_ai": False,
        "human_agent_whatsapp": False,
        "crm": True,
        "whatsapp_coexistence": True,
    },
    # Basic (starter) - essentials: broadcasts, templates, 5 seats, flows, smart AI,
    # CRM, analytics. Advanced campaign tools (ads/retargeting/tracking/drip) + AI
    # generation/chatbot are Pro+.
    PLAN_STARTER: {
        "workspaces": 1,
        "users": 5,
        "messages_per_day": 1000,
        "interactive_flows": 5,
        "image_credits": 0,
        "ad_spend_limit": 100000,  # INR
        # Feature access
        "unified_dashboard_analytics": True,
        "meta_ai_ad_optimization": False,
        "whatsapp_automation": True,
        "image_generation": False,
        "ai_chatbot_dashboard": False,
        "whatsapp_smart_ai": True,
        "human_agent_whatsapp": True,
        "crm": True,
        "whatsapp_coexistence": True,
    },
    # Pro (growth) - everything unlocked incl AI, mid volume.
    PLAN_GROWTH: {
        "workspaces": 3,
        "users": 5,
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
        "crm": True,
        "whatsapp_coexistence": True,
    },
    # Premium - high-volume tier (between Growth and Enterprise)
    PLAN_PREMIUM: {
        "workspaces": 10,
        "users": 15,
        "messages_per_day": 20000,
        "interactive_flows": 50,
        "image_credits": 500000,
        "ad_spend_limit": 1000000,  # INR (₹10L)
        # Feature access — everything on
        "unified_dashboard_analytics": True,
        "meta_ai_ad_optimization": True,
        "whatsapp_automation": True,
        "image_generation": True,
        "ai_chatbot_dashboard": True,
        "whatsapp_smart_ai": True,
        "human_agent_whatsapp": True,
        "crm": True,
        "whatsapp_coexistence": True,
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
        "crm": True,
        "whatsapp_coexistence": True,
    },
}


def get_plan_features(plan: str) -> dict:
    """Get features for a plan. Defaults to starter if plan not found."""
    return PLAN_FEATURES.get(plan, PLAN_FEATURES[PLAN_STARTER])


def is_unlimited(limit: int) -> bool:
    """Check if a limit value means unlimited."""
    return limit == UNLIMITED
