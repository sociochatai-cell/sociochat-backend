"""
Pure warmup evaluation (no DB writes). Uses account ORM row + config + signals.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Dict, Optional

from .warmup_config import merged_profile, warmup_enabled
from .warmup_signals import collect_signals, infer_country_code_from_account
from .warmup_types import (
    LIFECYCLE_ACTIVE,
    LIFECYCLE_RESTRICTED,
    LIFECYCLE_WARMUP,
    OP_ADVISORY_WARMUP,
    WarmupEvaluation,
)

if TYPE_CHECKING:
    from .models import WhatsAppAccount

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _naive_utc(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def evaluate_warmup(account: "WhatsAppAccount") -> WarmupEvaluation:
    """
    Compute enforcement hints for an account. When WH_WARMUP_ENABLED=0, returns inactive evaluation.
    """
    if not warmup_enabled():
        return WarmupEvaluation(
            lifecycle_effective=(account.onboarding_lifecycle_state or LIFECYCLE_ACTIVE),
            in_warmup_window=False,
            advisory_safe_mode=False,
            diagnostics={"warmup_engine": "disabled"},
        )

    signals = collect_signals(account)
    cc = infer_country_code_from_account(account)
    profile = merged_profile(cc, signals.is_coexistence)
    now = _utcnow()

    raw_life = (account.onboarding_lifecycle_state or LIFECYCLE_ACTIVE).lower()
    ends = _naive_utc(account.warmup_ends_at)
    started = _naive_utc(account.warmup_started_at)

    in_window = bool(
        raw_life == LIFECYCLE_WARMUP and started and ends and started <= now <= ends
    )

    # Expired warmup row still marked warmup — treat as tail; scheduler should flip to active
    if raw_life == LIFECYCLE_WARMUP and ends and now > ends:
        in_window = False

    lifecycle_effective = raw_life
    if raw_life == LIFECYCLE_WARMUP and not in_window:
        lifecycle_effective = LIFECYCLE_ACTIVE

    advisory = bool(in_window or account.operational_mode == OP_ADVISORY_WARMUP)
    reasons: list[str] = []
    diagnostics: Dict[str, Any] = {
        "profile_key": "coexistence" if signals.is_coexistence else (cc or "default"),
        "rules_profile": profile.__dict__,
        "signals": signals.__dict__,
    }

    countdown_seconds: Optional[int] = None
    if in_window and ends:
        countdown_seconds = max(0, int((ends - now).total_seconds()))

    effective_cap = profile.daily_send_cap

    q = (signals.quality_rating or "").upper()
    if q in ("YELLOW", "RED"):
        effective_cap = max(10, effective_cap // 2)
        reasons.append(f"quality_rating_{q.lower()}")
        advisory = True

    if signals.webhook_health not in ("healthy", "ok", "unknown", ""):
        reasons.append(f"webhook_{signals.webhook_health}")
        effective_cap = max(10, int(effective_cap * 0.75))
        advisory = True

    if signals.restriction_state and signals.restriction_state not in ("none", ""):
        lifecycle_effective = LIFECYCLE_RESTRICTED
        reasons.append("meta_restriction_signal")
        advisory = True

    blocks: Dict[str, bool] = {}
    block_reasons: Dict[str, str] = {}

    if in_window:
        if profile.block_broadcast:
            blocks["broadcast"] = True
            block_reasons["broadcast"] = "warmup_no_broadcast"
        if profile.block_drip:
            blocks["drip"] = True
            block_reasons["drip"] = "warmup_no_drip"
        if profile.block_aggressive_automation:
            blocks["automation_non_template"] = True
            block_reasons["automation_non_template"] = "warmup_templates_only_for_automation"

    diag_safe = (account.safe_mode_reason or "").strip()
    if diag_safe:
        reasons.append(diag_safe[:120])

    return WarmupEvaluation(
        lifecycle_effective=lifecycle_effective,
        in_warmup_window=in_window,
        advisory_safe_mode=advisory,
        safe_mode_reasons=reasons,
        effective_daily_send_cap=effective_cap,
        blocks=blocks,
        block_reasons=block_reasons,
        countdown_seconds=countdown_seconds,
        rules_profile=diagnostics["profile_key"],
        diagnostics=diagnostics,
    )


def apply_effective_daily_cap(base_limit: Optional[int], evaluation: WarmupEvaluation) -> Optional[int]:
    """Merge monolith projection cap with warmup advisory cap (lowest positive wins)."""
    wcap = evaluation.effective_daily_send_cap
    if wcap is None or not evaluation.in_warmup_window:
        return base_limit
    if base_limit is None or int(base_limit) <= 0:
        return int(wcap)
    return min(int(base_limit), int(wcap))


def enforce_warmup_gate(account: "WhatsAppAccount", action: str) -> Dict[str, Any]:
    """
    Enforcement gate: check if an action is allowed during warmup.

    Args:
        account: WhatsApp account
        action: "broadcast" | "bulk_send" | "campaign" | "drip" | "automation"

    Returns:
        {"allowed": bool, "reason": str | None, "countdown_seconds": int | None}
    """
    ev = evaluate_warmup(account)

    ACTION_BLOCK_MAP = {
        "broadcast": "broadcast",
        "bulk_send": "broadcast",
        "campaign": "broadcast",
        "drip": "drip",
        "automation": "automation_non_template",
    }

    block_key = ACTION_BLOCK_MAP.get(action)
    if block_key and ev.blocks.get(block_key):
        return {
            "allowed": False,
            "reason": ev.block_reasons.get(block_key, "Account is in warmup period"),
            "countdown_seconds": ev.countdown_seconds,
            "warmup_ends_at": (
                account.warmup_ends_at.isoformat() if account.warmup_ends_at else None
            ),
            "lifecycle": ev.lifecycle_effective,
        }

    return {"allowed": True, "reason": None, "countdown_seconds": ev.countdown_seconds}
