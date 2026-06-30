"""
Warmup enforcement hooks — advisory-first denials.

Mapped to CapabilityResult in capabilities.py to avoid import cycles.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Optional, Tuple

import os

from .warmup_config import merged_profile, warmup_enabled
from .warmup_engine import evaluate_warmup
from .warmup_signals import infer_country_code_from_account
from .warmup_types import LIFECYCLE_RESTRICTED, WarmupGateDenial

if TYPE_CHECKING:
    from .models import WhatsAppAccount

logger = logging.getLogger(__name__)


def _structured(kind: str, **kwargs) -> None:
    logger.info("warmup_%s %s", kind, " ".join(f"{k}={v!r}" for k, v in sorted(kwargs.items())))


def _load_account(account_id: int, session) -> Optional["WhatsAppAccount"]:
    from .models import WhatsAppAccount

    try:
        return session.query(WhatsAppAccount).filter_by(id=account_id).first()
    except Exception as e:
        logger.warning("warmup account load failed: %s", e)
        return None


def log_restriction_detected(account_id: int, source: str, detail: str) -> None:
    logger.info("restriction_detected account_id=%s source=%s detail=%s", account_id, source, detail[:500])


def log_safe_mode_triggered(account_id: int, reasons: list) -> None:
    logger.info("safe_mode_triggered account_id=%s reasons=%s", account_id, reasons[:20])


def warmup_denial_broadcast(account_id: int, session) -> Optional[WarmupGateDenial]:
    if not warmup_enabled():
        return None
    acc = _load_account(account_id, session)
    if not acc:
        return None
    ev = evaluate_warmup(acc)
    if ev.lifecycle_effective == LIFECYCLE_RESTRICTED:
        log_restriction_detected(account_id, "broadcast", "restricted_lifecycle")
    if ev.advisory_safe_mode and ev.safe_mode_reasons:
        log_safe_mode_triggered(account_id, ev.safe_mode_reasons)
    if ev.blocks.get("broadcast"):
        _structured("enforcement", account_id=account_id, gate="broadcast", allowed=False)
        return WarmupGateDenial(
            "Broadcasts and drip campaigns are limited during the WhatsApp warmup period.",
            "warmup_no_broadcast",
        )
    return None


def warmup_denial_drip(account_id: int, session) -> Optional[WarmupGateDenial]:
    if not warmup_enabled():
        return None
    acc = _load_account(account_id, session)
    if not acc:
        return None
    ev = evaluate_warmup(acc)
    if ev.blocks.get("drip"):
        _structured("enforcement", account_id=account_id, gate="drip", allowed=False)
        return WarmupGateDenial("Drip campaigns are paused during the WhatsApp warmup period.", "warmup_no_drip")
    return None


def _block_reactive_automation_during_warmup() -> bool:
    """
    Whether warmup should BLOCK reactive auto-replies (keyword/FAQ/AI responses to an
    inbound message), not just throttle them.

    Default False: warmup is meant to limit PROACTIVE/aggressive outbound (broadcast, drip —
    gated separately). A 1:1 reply to a customer who just messaged is inside the WhatsApp
    customer-service window and is safe + expected during warmup. AI is still THROTTLED
    (token/temperature caps via ai_throttle_params) — it is simply no longer hard-blocked,
    which previously made every new number deflect all chats to "a team member will call you"
    for its first 7 days.

    Set WH_WARMUP_BLOCK_REACTIVE_AUTOMATION=1 to restore the strict templates-only posture.
    """
    v = (os.getenv("WH_WARMUP_BLOCK_REACTIVE_AUTOMATION") or "").strip().lower()
    return v in ("1", "true", "yes", "on")


def warmup_denial_automation(account_id: int, session, response_type: str) -> Optional[WarmupGateDenial]:
    if not warmup_enabled():
        return None
    # Reactive replies (text/faq/ai/interactive) to an inbound message are allowed during
    # warmup by default — they are throttled elsewhere, not blocked here.
    if not _block_reactive_automation_during_warmup():
        return None
    acc = _load_account(account_id, session)
    if not acc:
        return None
    ev = evaluate_warmup(acc)
    rt = (response_type or "").lower()
    if ev.blocks.get("automation_non_template") and rt in ("text", "interactive", "faq", "ai"):
        _structured("enforcement", account_id=account_id, gate="automation", response_type=rt, allowed=False)
        return WarmupGateDenial(
            "Automated sends (except approved templates) are limited during warmup. Use templates or reply manually.",
            "warmup_automation_limited",
        )
    return None


def warmup_denial_ai(account_id: int, session) -> Optional[WarmupGateDenial]:
    if not warmup_enabled():
        return None
    acc = _load_account(account_id, session)
    if not acc:
        return None
    ev = evaluate_warmup(acc)
    if ev.lifecycle_effective == LIFECYCLE_RESTRICTED:
        return WarmupGateDenial(
            "AI auto-replies are limited while Meta restriction signals are present.",
            "restriction_ai_limited",
        )
    return None


def ai_throttle_params(account_id: int, session) -> Tuple[int, float]:
    """Returns (max_tokens_cap, temperature_cap) for merge into AI config."""
    if not warmup_enabled():
        return 10_000, 1.0
    acc = _load_account(account_id, session)
    if not acc:
        return 10_000, 1.0
    ev = evaluate_warmup(acc)
    if not ev.in_warmup_window:
        return 10_000, 1.0
    cc = infer_country_code_from_account(acc)
    p = merged_profile(cc, bool(getattr(acc, "is_coexistence", False)))
    if not p.throttle_ai:
        return 10_000, 1.0
    _structured("enforcement", account_id=account_id, gate="ai_throttle", allowed=True)
    return p.ai_max_tokens_cap, p.ai_temperature_max


def warmup_outbound_send_kind_denial(
    account_id: int,
    session,
    *,
    send_kind: str,
) -> Optional[WarmupGateDenial]:
    if not warmup_enabled():
        return None
    acc = _load_account(account_id, session)
    if not acc:
        return None
    ev = evaluate_warmup(acc)
    if not ev.in_warmup_window:
        return None
    sk = (send_kind or "unknown").lower()
    if sk in ("manual", "template", "media", "interactive", "flow", "unknown"):
        return None
    if sk in ("bulk", "broadcast"):
        _structured("enforcement", account_id=account_id, gate="outbound", send_kind=sk, allowed=False)
        return WarmupGateDenial("This send path is limited during warmup.", "warmup_outbound_path")
    return None
