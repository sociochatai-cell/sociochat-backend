"""
Derive operational signals for warmup evaluation (read-only).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from .models import WhatsAppAccount

from .warmup_types import WarmupOperationalSignals


def _age_days(created_at: Optional[datetime]) -> float:
    if not created_at:
        return 0.0
    now = datetime.now(timezone.utc)
    ca = created_at
    if ca.tzinfo is None:
        ca = ca.replace(tzinfo=timezone.utc)
    return max(0.0, (now - ca).total_seconds() / 86400.0)


def infer_country_code_from_account(account: "WhatsAppAccount") -> Optional[str]:
    """Best-effort region from display phone (E.164)."""
    raw = (account.display_phone_number or "").strip()
    if not raw:
        return None
    try:
        import phonenumbers
        from phonenumbers import region_code_for_number

        parsed = phonenumbers.parse(raw, None)
        if not phonenumbers.is_possible_number(parsed):
            return None
        return region_code_for_number(parsed)
    except Exception:
        return None


def effective_quality_rating(account: "WhatsAppAccount") -> Optional[str]:
    sr = getattr(account, "status_record", None)
    q = (sr.quality_rating if sr and sr.quality_rating else None) or account.quality_score
    if not q:
        return None
    return str(q).strip().upper()


def collect_signals(account: "WhatsAppAccount") -> WarmupOperationalSignals:
    onboarding_complete = bool(
        account.warmup_started_at is not None
        or account.onboarding_lifecycle_state in ("active", "trusted", "warmup")
        or (account.webhook_health or "").lower() in ("healthy", "ok", "degraded")
    )
    # If webhooks never validated, still allow warmup rules to apply conservatively
    if account.last_inbound_webhook_at or account.webhook_last_event_at:
        onboarding_complete = True

    return WarmupOperationalSignals(
        account_age_days=_age_days(account.created_at),
        quality_rating=effective_quality_rating(account),
        webhook_health=(account.webhook_health or "unknown").lower(),
        onboarding_complete=onboarding_complete,
        restriction_state=(account.restriction_state or "none").lower(),
        restriction_reason=account.restriction_reason,
        is_coexistence=bool(getattr(account, "is_coexistence", False)),
        message_health=account.message_health_metrics if isinstance(account.message_health_metrics, dict) else None,
    )
