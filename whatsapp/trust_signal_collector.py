"""
Collect advisory trust / reputation signals from DB (no hard scoring).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, TYPE_CHECKING

from sqlalchemy import func, or_

from shared_models import db

if TYPE_CHECKING:
    from .models import WhatsAppAccount


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _effective_quality(account: "WhatsAppAccount") -> Optional[str]:
    sr = getattr(account, "status_record", None)
    q = (sr.quality_rating if sr and sr.quality_rating else None) or account.quality_score
    if not q:
        return None
    return str(q).strip().upper() or None


def _messaging_tier_label(account: "WhatsAppAccount") -> Optional[str]:
    ml = account.messaging_limit
    if ml is None:
        return None
    try:
        n = int(ml)
        if n >= 100000:
            return "100K+"
        if n >= 10000:
            return "10K"
        if n >= 1000:
            return "1K"
        return str(n)
    except (TypeError, ValueError):
        return str(ml)[:32]


def _name_status(account: "WhatsAppAccount") -> Optional[str]:
    """Display / business name verification hint (DB-only, not Meta code_verification_status)."""
    sr = getattr(account, "status_record", None)
    if sr and sr.is_verified:
        return "verified"
    if account.is_verified:
        return "verified"
    if (account.verified_name or "").strip():
        return "name_present_unverified"
    return "unknown"


def _verification_status(account: "WhatsAppAccount") -> Optional[str]:
    sr = getattr(account, "status_record", None)
    if sr and sr.is_verified is not None:
        return "verified" if sr.is_verified else "unverified"
    if account.is_verified is not None:
        return "verified" if account.is_verified else "unverified"
    return None


def collect_message_volume_signals(account_id: int, session=None) -> Dict[str, Any]:
    """Outbound volume and failure counts for spike / health signals (UTC windows)."""
    from .models import WhatsAppConversation, WhatsAppMessage

    sess = session or db.session
    now = _now()
    since_24h = now - timedelta(hours=24)
    since_7d = now - timedelta(days=7)

    base = (
        sess.query(func.count(WhatsAppMessage.id))
        .join(WhatsAppConversation, WhatsAppMessage.conversation_id == WhatsAppConversation.id)
        .filter(
            WhatsAppConversation.account_id == account_id,
            WhatsAppMessage.direction == "outgoing",
        )
    )
    out_24h = int(base.filter(WhatsAppMessage.created_at >= since_24h).scalar() or 0)
    out_7d = int(base.filter(WhatsAppMessage.created_at >= since_7d).scalar() or 0)
    failed_24h = int(
        base.filter(
            WhatsAppMessage.created_at >= since_24h,
            or_(
                WhatsAppMessage.status == "failed",
                WhatsAppMessage.error_code.isnot(None),
            ),
        ).scalar()
        or 0
    )
    failed_7d = int(
        base.filter(
            WhatsAppMessage.created_at >= since_7d,
            or_(
                WhatsAppMessage.status == "failed",
                WhatsAppMessage.error_code.isnot(None),
            ),
        ).scalar()
        or 0
    )

    rate_limited_24h = int(
        base.filter(
            WhatsAppMessage.created_at >= since_24h,
            WhatsAppMessage.error_code == "131029",
        ).scalar()
        or 0
    )

    return {
        "outbound_count_24h": out_24h,
        "outbound_count_7d": out_7d,
        "failed_outbound_24h": failed_24h,
        "failed_outbound_7d": failed_7d,
        "rate_limit_errors_24h": rate_limited_24h,
        "failure_rate_24h": (failed_24h / out_24h) if out_24h else 0.0,
    }


def collect_account_trust_signals(account: "WhatsAppAccount", session=None) -> Dict[str, Any]:
    """Full inputs blob for trust_snapshots.inputs + column backfill."""
    sess = session or db.session
    vol = collect_message_volume_signals(account.id, sess)
    sr = getattr(account, "status_record", None)

    enforcement_hints: Dict[str, Any] = {
        "onboarding_lifecycle_state": account.onboarding_lifecycle_state,
        "operational_mode": account.operational_mode,
        "warmup_active": bool(
            account.warmup_ends_at and account.warmup_started_at and (account.onboarding_lifecycle_state or "") == "warmup"
        ),
        "safe_mode_reason": (account.safe_mode_reason or "")[:500] or None,
    }

    return {
        "captured_at": _now().isoformat(),
        "account_id": account.id,
        "workspace_id": account.workspace_id,
        "is_coexistence": bool(getattr(account, "is_coexistence", False)),
        "webhook_failure_count": int(account.webhook_failure_count or 0),
        "webhook_last_failure_at": account.webhook_last_failure_at.isoformat() if account.webhook_last_failure_at else None,
        "last_inbound_webhook_at": account.last_inbound_webhook_at.isoformat() if account.last_inbound_webhook_at else None,
        "token_health": account.token_health,
        "message_health_metrics": account.message_health_metrics if isinstance(account.message_health_metrics, dict) else None,
        "status_slice": {
            "status": sr.status if sr else None,
            "quality_rating": sr.quality_rating if sr else None,
            "last_error_code": sr.last_error_code if sr else None,
        }
        if sr
        else None,
        "volume": vol,
        "enforcement_hints": enforcement_hints,
        "graph_fetch": None,
    }


def snapshot_column_values(account: "WhatsAppAccount", inputs: Dict[str, Any]) -> Dict[str, Any]:
    """Map account + inputs to TrustSnapshot ORM columns."""
    sr = getattr(account, "status_record", None)
    return {
        "quality_rating": _effective_quality(account),
        "messaging_tier": _messaging_tier_label(account),
        "name_status": _name_status(account),
        "verification_status": _verification_status(account),
        "webhook_health": (account.webhook_health or "unknown")[:24],
        "webhook_subscription_status": (account.webhook_subscription_status or "")[:32] or None,
        "restriction_state": (account.restriction_state or "none")[:40],
        "operational_mode": (account.operational_mode or "normal")[:24],
        "trust_score": account.trust_score,
        "notes": None,
    }
