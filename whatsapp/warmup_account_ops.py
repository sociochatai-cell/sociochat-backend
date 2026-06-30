"""
Persisted warmup lifecycle mutations (DB). Keep small — evaluation stays in warmup_engine.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, TYPE_CHECKING

from shared_models import db

from .warmup_config import maturity_skip_days, merged_profile, warmup_enabled
from .warmup_engine import evaluate_warmup
from .warmup_signals import infer_country_code_from_account
from .warmup_types import (
    LIFECYCLE_ACTIVE,
    LIFECYCLE_RESTRICTED,
    LIFECYCLE_TRUSTED,
    LIFECYCLE_WARMUP,
    OP_ADVISORY_WARMUP,
    OP_NORMAL,
)

if TYPE_CHECKING:
    from .models import WhatsAppAccount

logger = logging.getLogger(__name__)


def _structured(kind: str, **kwargs: Any) -> None:
    logger.info("warmup_%s %s", kind, " ".join(f"{k}={v!r}" for k, v in sorted(kwargs.items())))


def _now() -> datetime:
    return datetime.now(timezone.utc)


def start_warmup_for_new_account(account: "WhatsAppAccount", *, source: str) -> None:
    """Begin warmup window for a newly connected account (Embedded Signup or coexistence new row)."""
    if not warmup_enabled():
        account.onboarding_lifecycle_state = LIFECYCLE_ACTIVE
        account.operational_mode = OP_NORMAL
        return

    cc = infer_country_code_from_account(account)
    profile = merged_profile(cc, bool(getattr(account, "is_coexistence", False)))
    now = _now()
    account.warmup_started_at = now
    account.warmup_ends_at = now + timedelta(days=profile.duration_days)
    account.onboarding_lifecycle_state = LIFECYCLE_WARMUP
    account.operational_mode = OP_ADVISORY_WARMUP
    account.safe_mode_advisory_only = True
    account.safe_mode_reason = f"warmup:{source}"
    cfg = dict(account.warmup_config or {})
    cfg.update(
        {
            "source": source,
            "duration_days": profile.duration_days,
            "started_at": now.isoformat(),
            "ends_at": account.warmup_ends_at.isoformat(),
            "profile": profile.__dict__,
        }
    )
    account.warmup_config = cfg
    _structured("started", account_id=account.id, source=source, ends_at=str(account.warmup_ends_at))


def ensure_mature_relink_skips_warmup(account: "WhatsAppAccount") -> None:
    """
    If an existing long-lived account is re-linked, keep it active (no retro-warmup).
    """
    if not warmup_enabled():
        return
    try:
        age_days = (_now() - (account.created_at.replace(tzinfo=timezone.utc) if account.created_at.tzinfo is None else account.created_at.astimezone(timezone.utc))).days
    except Exception:
        age_days = 0
    if age_days >= maturity_skip_days() and not account.warmup_started_at:
        account.onboarding_lifecycle_state = LIFECYCLE_ACTIVE
        account.operational_mode = OP_NORMAL


def complete_warmup_if_due(account: "WhatsAppAccount", *, force: bool = False) -> bool:
    """If warmup ended, move to active. Returns True if state changed."""
    if not warmup_enabled() and not force:
        return False
    if (account.onboarding_lifecycle_state or "").lower() != LIFECYCLE_WARMUP:
        return False
    ends = account.warmup_ends_at
    if not ends:
        return False
    if ends.tzinfo is None:
        ends = ends.replace(tzinfo=timezone.utc)
    if _now() < ends and not force:
        return False
    account.onboarding_lifecycle_state = LIFECYCLE_ACTIVE
    account.operational_mode = OP_NORMAL
    account.safe_mode_reason = None
    _structured("completed", account_id=account.id)
    return True


def extend_warmup_days(account: "WhatsAppAccount", delta_days: int, *, actor: str) -> None:
    if not account.warmup_ends_at:
        return
    ends = account.warmup_ends_at
    if ends.tzinfo is None:
        ends = ends.replace(tzinfo=timezone.utc)
    account.warmup_ends_at = ends + timedelta(days=int(delta_days))
    cfg = dict(account.warmup_config or {})
    cfg["last_extend"] = {"delta_days": delta_days, "actor": actor, "at": _now().isoformat()}
    account.warmup_config = cfg
    _structured("extended", account_id=account.id, delta_days=delta_days, actor=actor)


def admin_override_lifecycle(account: "WhatsAppAccount", target: str, *, reason: str, actor: str) -> None:
    t = (target or "").strip().lower()
    if t not in (LIFECYCLE_ACTIVE, LIFECYCLE_WARMUP, LIFECYCLE_TRUSTED, LIFECYCLE_RESTRICTED):
        raise ValueError("invalid_target")
    account.onboarding_lifecycle_state = t
    account.operational_mode = OP_ADVISORY_WARMUP if t == LIFECYCLE_WARMUP else OP_NORMAL
    if t != LIFECYCLE_WARMUP:
        account.safe_mode_reason = None
    cfg = dict(account.warmup_config or {})
    cfg["admin_override"] = {"target": t, "reason": reason, "actor": actor, "at": _now().isoformat()}
    account.warmup_config = cfg
    _structured("admin_override", account_id=account.id, target=t, actor=actor)


def build_public_state(account: "WhatsAppAccount") -> Dict[str, Any]:
    ev = evaluate_warmup(account)
    restrictions = []
    if ev.blocks.get("broadcast"):
        restrictions.append(
            {"feature": "broadcast", "enabled": False, "reason": ev.block_reasons.get("broadcast", "warmup")}
        )
    if ev.blocks.get("drip"):
        restrictions.append({"feature": "drip", "enabled": False, "reason": ev.block_reasons.get("drip", "warmup")})
    if ev.blocks.get("automation_non_template"):
        restrictions.append(
            {
                "feature": "automation_non_template",
                "enabled": False,
                "reason": ev.block_reasons.get("automation_non_template", "warmup"),
            }
        )
    if ev.in_warmup_window:
        restrictions.append(
            {
                "feature": "ai_auto_reply",
                "enabled": True,
                "reason": "warmup_throttle",
                "detail": "Reduced tokens / temperature during warmup",
            }
        )
    return {
        "account_id": account.id,
        "lifecycle_db": account.onboarding_lifecycle_state,
        "lifecycle_effective": ev.lifecycle_effective,
        "in_warmup_window": ev.in_warmup_window,
        "warmup_started_at": account.warmup_started_at.isoformat() if account.warmup_started_at else None,
        "warmup_ends_at": account.warmup_ends_at.isoformat() if account.warmup_ends_at else None,
        "countdown_seconds": ev.countdown_seconds,
        "advisory_safe_mode": ev.advisory_safe_mode,
        "safe_mode_reasons": ev.safe_mode_reasons,
        "effective_daily_send_cap": ev.effective_daily_send_cap,
        "operational_mode": account.operational_mode,
        "restrictions": restrictions,
        "diagnostics": ev.diagnostics,
    }
