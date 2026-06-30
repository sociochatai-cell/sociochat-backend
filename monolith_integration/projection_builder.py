"""Build capability projection JSON from monolith User + plan features."""

from __future__ import annotations

import time
from typing import Any, Dict, Optional

from shared_models import User
from subscription.constants import get_plan_features
from subscription.service import get_user_plan


def _subscription_status_from_user(user: User) -> str:
    """Map monolith user.status to projection subscription_status (short string)."""
    raw = (getattr(user, "status", None) or "").strip().lower()
    if raw in ("active", "verified", "approved"):
        return "ACTIVE"
    if raw in ("trial", "trialing"):
        return "TRIAL"
    if raw in ("past_due", "pastdue"):
        return "PAST_DUE"
    if raw in ("canceled", "cancelled"):
        return "CANCELED"
    if raw in ("suspended", "banned"):
        return "SUSPENDED"
    if raw in ("unpaid",):
        return "UNPAID"
    if raw in ("inactive", "disabled"):
        return "INACTIVE"
    return "ACTIVE"


def build_capability_projection_body(user: User, *, projection_version: Optional[int] = None) -> Dict[str, Any]:
    """
    Fields accepted by WhatsApp internal capabilities API (omit nulls server-side).
    ``updated_at`` is set by the WhatsApp service on upsert — not sent here.
    """
    plan = get_user_plan(user)
    features = get_plan_features(plan)
    daily = features.get("messages_per_day")
    if daily is not None and daily < 0:
        daily = None

    pv = projection_version if projection_version is not None else int(time.time())

    body: Dict[str, Any] = {
        "subscription_status": _subscription_status_from_user(user),
        "ai_enabled": bool(features.get("whatsapp_smart_ai")),
        "automation_enabled": bool(features.get("whatsapp_automation")),
        "broadcast_enabled": bool(features.get("whatsapp_automation")),
        "projection_version": pv,
    }
    if daily is not None:
        body["daily_message_limit"] = daily
    return body
