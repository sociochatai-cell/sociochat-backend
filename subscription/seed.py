"""Seed subscription plans, features, and default access matrix."""

import logging
from typing import Dict, List, Tuple

from shared_models import db
from subscription.constants import PLAN_FEATURES, VALID_PLANS, UNLIMITED
from subscription.plan_models import SubscriptionPlan, SubscriptionFeature, PlanFeatureAccess

logger = logging.getLogger(__name__)

# SocioChat feature catalog (frontend keys + backend limit keys)
FEATURE_CATALOG: List[Tuple[str, str, str, str, str]] = [
    # key, label, category, route_path, feature_type
    ("whatsapp_inbox", "WhatsApp Inbox", "whatsapp", "/dashboard/inbox", "access"),
    ("whatsapp_coexistence", "WhatsApp Coexistence", "whatsapp", "/dashboard/coexistence", "access"),
    ("whatsapp_templates", "Message Templates", "whatsapp", "/dashboard/templates", "access"),
    ("whatsapp_automation", "Automation", "whatsapp", "/dashboard/automation", "access"),
    ("whatsapp_drip", "Drip Campaigns", "whatsapp", "/dashboard/drip", "access"),
    ("whatsapp_interactive_automation", "Conversational Flows", "whatsapp", "/dashboard/interactive-automation", "access"),
    ("whatsapp_flows", "WhatsApp Forms", "whatsapp", "/dashboard/flows", "access"),
    ("whatsapp_analytics", "Analytics Hub", "whatsapp", "/dashboard/analytics", "access"),
    ("whatsapp_contacts", "Contacts", "whatsapp", "/dashboard/contacts", "access"),
    ("whatsapp_datasets", "Datasets", "whatsapp", "/dashboard/datasets", "access"),
    ("whatsapp_bulk_messaging", "Bulk Messaging", "whatsapp", "/dashboard/bulk", "access"),
    ("whatsapp_tracking", "Link Tracking", "whatsapp", "/dashboard/tracking", "access"),
    ("whatsapp_catalog", "Product Catalog", "whatsapp", "/dashboard/catalog", "access"),
    ("whatsapp_ctwa", "Click-to-WhatsApp Ads", "whatsapp", "/ctwa/create", "access"),
    ("whatsapp_status_ads", "WhatsApp Status Ads", "whatsapp", "/ctwa/status/create", "access"),
    ("whatsapp_smart_ai", "Smart AI", "whatsapp", None, "access"),
    ("image_generation", "AI Image Generation", "ai", None, "access"),
    ("ai_chatbot_dashboard", "AI Chatbot Dashboard", "ai", None, "access"),
    ("human_agent_whatsapp", "Human Agent Handoff", "whatsapp", None, "access"),
    ("unified_dashboard_analytics", "Unified Dashboard", "analytics", "/dashboard/hub", "access"),
    ("crm", "CRM", "crm", "/dashboard/crm", "access"),
    ("messages_per_day", "Messages per day", "limits", None, "limit"),
    ("interactive_flows", "Interactive flow limit", "limits", None, "limit"),
    ("workspaces", "Workspaces", "limits", None, "limit"),
    ("users", "Team users", "limits", None, "limit"),
    ("image_credits", "Image credits / month", "limits", None, "limit"),
]

# slug -> (display name, description, price_monthly_inr, is_public, sort_order)
# Display names mirror the AiSensy-style tier ladder: Free / Basic / Pro /
# Premium / Ultimate. Slugs stay stable (beta/starter/growth/premium/enterprise).
PLAN_META = {
    "beta": ("Free", "Free forever — get started", 0, True, 0),
    "starter": ("Basic", "For small businesses starting out", 999, True, 1),
    "growth": ("Pro", "Scale with AI and automation", 2999, True, 2),
    "premium": ("Premium", "For high-volume teams", 8999, True, 3),
    "enterprise": ("Ultimate", "White-label, unlimited scale", 39999, True, 4),
}

# Map legacy constant keys → SocioChat feature keys for seeding access
_LEGACY_TO_SOCIOCHAT = {
    "whatsapp_automation": ["whatsapp_automation"],
    "whatsapp_smart_ai": ["whatsapp_smart_ai"],
    "image_generation": ["image_generation"],
    "ai_chatbot_dashboard": ["ai_chatbot_dashboard", "whatsapp_smart_ai"],
    "human_agent_whatsapp": ["human_agent_whatsapp", "whatsapp_inbox"],
    "unified_dashboard_analytics": ["unified_dashboard_analytics", "whatsapp_analytics"],
    "meta_ai_ad_optimization": ["whatsapp_ctwa"],
}

_DEFAULT_STARTER_OFF = {
    "whatsapp_flows", "whatsapp_datasets", "whatsapp_drip",
    "whatsapp_bulk_messaging", "whatsapp_tracking", "whatsapp_catalog",
    "whatsapp_ctwa", "image_generation", "ai_chatbot_dashboard",
}


def _seed_features() -> Dict[str, SubscriptionFeature]:
    by_key: Dict[str, SubscriptionFeature] = {}
    for idx, (key, label, category, route_path, ftype) in enumerate(FEATURE_CATALOG):
        row = SubscriptionFeature.query.filter_by(key=key).first()
        if not row:
            row = SubscriptionFeature(
                key=key,
                label=label,
                category=category,
                route_path=route_path,
                feature_type=ftype,
                sort_order=idx,
            )
            db.session.add(row)
        else:
            row.label = label
            row.category = category
            row.route_path = route_path
            row.feature_type = ftype
            row.sort_order = idx
        by_key[key] = row
    db.session.flush()
    return by_key


# Pre-relaunch seeded defaults. An existing row still holding EXACTLY one of these
# values is auto-migrated to the new ladder (PLAN_META) on boot; any OTHER value
# means a super-admin customised it in the portal, so it is left untouched. This
# is what makes portal price/name edits DURABLE across restarts — PLAN_META is
# only the first-time default + a one-time migration, not a boot-time override.
_LEGACY_PLAN_DEFAULTS = {
    "beta": ("Beta", 0),
    "starter": ("Starter", 1999),
    "growth": ("Growth", 4999),
    "enterprise": ("Enterprise", None),
}


def _seed_plans() -> Dict[str, SubscriptionPlan]:
    by_slug: Dict[str, SubscriptionPlan] = {}
    for slug, (name, desc, price, is_public, sort_order) in PLAN_META.items():
        row = SubscriptionPlan.query.filter_by(slug=slug).first()
        if not row:
            # New plan (e.g. premium) — create with the launch defaults.
            row = SubscriptionPlan(
                slug=slug,
                name=name,
                description=desc,
                price_monthly_inr=price,
                is_public=is_public,
                sort_order=sort_order,
                plan_scope="global",
            )
            db.session.add(row)
        else:
            # Existing plan — the DB (admin portal) is the source of truth. Migrate
            # ONLY a row that is STILL fully at its old seeded default (both name
            # AND price match the legacy pair) to the new ladder — a genuine
            # one-time move. Any admin customisation (name, price, visibility, or
            # ordering) is then left untouched on every subsequent boot, so portal
            # edits are durable. A NULL/custom price (e.g. an admin-set "Contact
            # us" tier) is preserved because it no longer matches the legacy pair.
            legacy = _LEGACY_PLAN_DEFAULTS.get(slug)
            if legacy is not None and row.name == legacy[0] and row.price_monthly_inr == legacy[1]:
                row.name = name
                row.description = desc
                row.price_monthly_inr = price
                row.is_public = is_public
                row.sort_order = sort_order
            row.plan_scope = getattr(row, "plan_scope", None) or "global"
        by_slug[slug] = row
    db.session.flush()
    return by_slug


def _legacy_bool(plan_slug: str, legacy_key: str) -> bool:
    feats = PLAN_FEATURES.get(plan_slug, PLAN_FEATURES["starter"])
    return bool(feats.get(legacy_key, False))


def _legacy_limit(plan_slug: str, limit_key: str) -> int:
    feats = PLAN_FEATURES.get(plan_slug, PLAN_FEATURES["starter"])
    return int(feats.get(limit_key, 0))


# AiSensy-mirrored gating: the minimum plan RANK at which each SocioChat feature
# turns ON. Free=0 (beta), Basic=1 (starter), Pro=2 (growth), Premium=3,
# Ultimate=4. A feature not listed defaults to 0 (available on every plan).
_PLAN_RANK = {"beta": 0, "starter": 1, "growth": 2, "premium": 3, "enterprise": 4}
_FEATURE_MIN_RANK = {
    # Free+ (0): WhatsApp API + single-agent live chat + contacts + CRM.
    "whatsapp_inbox": 0, "whatsapp_coexistence": 0, "whatsapp_contacts": 0, "crm": 0,
    # Basic+ (1): broadcasts, templates, bulk, analytics, flows, smart AI, human agent.
    "whatsapp_templates": 1, "whatsapp_automation": 1, "whatsapp_bulk_messaging": 1,
    "whatsapp_analytics": 1, "unified_dashboard_analytics": 1,
    "whatsapp_interactive_automation": 1, "whatsapp_smart_ai": 1, "human_agent_whatsapp": 1,
    # Pro+ (2): campaign scheduler/drip, click tracking, ads/retargeting, catalog,
    # datasets, forms, AI image generation, AI chatbot.
    "whatsapp_drip": 2, "whatsapp_tracking": 2, "whatsapp_ctwa": 2, "whatsapp_catalog": 2,
    "whatsapp_datasets": 2, "whatsapp_flows": 2, "image_generation": 2, "ai_chatbot_dashboard": 2,
    "whatsapp_status_ads": 2,
}


def _access_for_plan(plan_slug: str, feature_key: str, ftype: str) -> Tuple[bool, int | None]:
    # Limit-type features carry the numeric cap from PLAN_FEATURES.
    if ftype == "limit":
        return True, _legacy_limit(plan_slug, feature_key)
    # Access-type features: enabled when the plan's rank meets the feature threshold.
    rank = _PLAN_RANK.get(plan_slug, 1)
    min_rank = _FEATURE_MIN_RANK.get(feature_key, 0)
    return rank >= min_rank, None


def _seed_access(plans: Dict[str, SubscriptionPlan]) -> None:
    features = {f.key: f for f in SubscriptionFeature.query.all()}
    for slug, plan in plans.items():
        for key, feat in features.items():
            enabled, limit_val = _access_for_plan(slug, key, feat.feature_type)
            row = PlanFeatureAccess.query.filter_by(plan_id=plan.id, feature_key=key).first()
            if not row:
                row = PlanFeatureAccess(
                    plan_id=plan.id,
                    feature_key=key,
                    enabled=enabled,
                    limit_value=limit_val,
                )
                db.session.add(row)
            elif PlanFeatureAccess.query.count() < len(VALID_PLANS) * len(features):
                # Only fill missing defaults on first seed — don't overwrite admin edits
                pass


def seed_subscription_catalog(force_access: bool = False) -> None:
    """Idempotent seed for plans, features, and initial access matrix."""
    try:
        features = _seed_features()
        plans = _seed_plans()

        if force_access or PlanFeatureAccess.query.count() == 0:
            for slug, plan in plans.items():
                for key, feat in features.items():
                    enabled, limit_val = _access_for_plan(slug, key, feat.feature_type)
                    row = PlanFeatureAccess.query.filter_by(plan_id=plan.id, feature_key=key).first()
                    if not row:
                        db.session.add(PlanFeatureAccess(
                            plan_id=plan.id,
                            feature_key=key,
                            enabled=enabled,
                            limit_value=limit_val,
                        ))
                    elif force_access:
                        row.enabled = enabled
                        row.limit_value = limit_val
        else:
            # On an already-seeded DB, insert-if-missing for newly introduced
            # catalog features so each plan gains a togglable access row without
            # a DB wipe. Existing rows (admin edits) are never overwritten.
            for slug, plan in plans.items():
                for key, feat in features.items():
                    row = PlanFeatureAccess.query.filter_by(plan_id=plan.id, feature_key=key).first()
                    if not row:
                        enabled, limit_val = _access_for_plan(slug, key, feat.feature_type)
                        db.session.add(PlanFeatureAccess(
                            plan_id=plan.id,
                            feature_key=key,
                            enabled=enabled,
                            limit_value=limit_val,
                        ))

        db.session.commit()
        logger.info("Subscription catalog seeded (%d plans, %d features)", len(plans), len(features))
    except Exception:
        db.session.rollback()
        logger.exception("Subscription catalog seed failed")
