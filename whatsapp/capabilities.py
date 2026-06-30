"""
Operational entitlement projection (read-only in this service).

Monolith owns billing and emits updates into `whatsapp_account_capabilities`.
This module only reads the snapshot and answers allow/deny for gates.
"""

from __future__ import annotations

import os
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, Tuple, Any

from sqlalchemy import func

from shared_models import db
from core.deployment_safety import slog

logger = logging.getLogger(__name__)

# When no row exists, gates are permissive unless strict mode is on.
_STRICT = os.getenv("WHATSAPP_CAPABILITIES_STRICT", "").lower() in ("1", "true", "yes")

_BLOCKED_SUBSCRIPTION = frozenset(
    {
        "CANCELED",
        "CANCELLED",
        "SUSPENDED",
        "INACTIVE",
        "PAST_DUE",
        "UNPAID",
    }
)


@dataclass
class CapabilityResult:
    ok: bool
    message: str = ""
    subscription_status: Optional[str] = None
    daily_message_limit: Optional[int] = None
    daily_messages_used: Optional[int] = None


def get_capabilities_row(account_id: int, session=None) -> Optional[Any]:
    """Return WhatsAppAccountCapabilities ORM row or None."""
    if not account_id:
        return None
    sess = session or db.session
    try:
        from .models import WhatsAppAccountCapabilities

        return sess.query(WhatsAppAccountCapabilities).filter_by(account_id=account_id).first()
    except Exception as e:
        logger.warning("capabilities lookup failed: %s", e)
        return None


def _subscription_blocks_send(row) -> bool:
    if row is None:
        return False
    st = (row.subscription_status or "").strip().upper()
    if not st or st == "ACTIVE" or st == "TRIAL":
        return False
    return st in _BLOCKED_SUBSCRIPTION


def outbound_send_capability_check(
    account_id: int, session=None, *, send_kind: Optional[str] = None
) -> CapabilityResult:
    """
    Gate 1:1 / outbound Cloud API sends driven by this WhatsApp account.

    - No row: permissive unless WHATSAPP_CAPABILITIES_STRICT=1 (then deny).
    - Row with blocked subscription_status: deny sends.
    - daily_message_limit: if set (>0), deny when today's outbound count >= limit (UTC day).
    - Warmup: merges a lower effective daily cap; optional send_kind blocks bulk-style sends.
    """
    sess = session or db.session

    row = get_capabilities_row(account_id, session)
    if row is None:
        if _STRICT:
            slog(
                "strict_mode_denial",
                gate="outbound_send",
                account_id=account_id,
                reason="missing_projection",
            )
            return CapabilityResult(False, "Capabilities projection missing (strict mode).")
        return CapabilityResult(True, "")

    if _subscription_blocks_send(row):
        st = (row.subscription_status or "").strip()
        slog(
            "strict_mode_denial",
            gate="outbound_send",
            account_id=account_id,
            reason="subscription_blocked",
            subscription_status=st or None,
        )
        return CapabilityResult(
            False,
            f"Messaging not allowed for subscription_status={st!r}.",
            subscription_status=st or None,
        )

    try:
        from .warmup_enforcement import warmup_outbound_send_kind_denial

        if send_kind:
            wk = warmup_outbound_send_kind_denial(account_id, sess, send_kind=send_kind)
            if wk:
                return CapabilityResult(False, wk.message)
    except Exception as e:
        logger.warning("warmup outbound kind gate failed open: %s", e)

    limit = row.daily_message_limit
    try:
        from .models import WhatsAppAccount
        from .warmup_engine import apply_effective_daily_cap, evaluate_warmup

        acc_row = sess.query(WhatsAppAccount).filter_by(id=account_id).first()
        if acc_row:
            ev = evaluate_warmup(acc_row)
            limit = apply_effective_daily_cap(limit, ev)
    except Exception as e:
        logger.warning("warmup daily cap merge failed open: %s", e)

    if limit is not None and int(limit) > 0:
        try:
            from .models import WhatsAppMessage, WhatsAppConversation

            start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
            used = (
                sess.query(func.count(WhatsAppMessage.id))
                .join(WhatsAppConversation, WhatsAppMessage.conversation_id == WhatsAppConversation.id)
                .filter(
                    WhatsAppConversation.account_id == account_id,
                    WhatsAppMessage.direction == "outgoing",
                    WhatsAppMessage.created_at >= start,
                )
                .scalar()
            ) or 0
            lim = int(limit)
            if int(used) >= lim:
                st = (row.subscription_status or "").strip() or None
                slog(
                    "strict_mode_denial",
                    gate="outbound_send",
                    account_id=account_id,
                    reason="daily_limit_reached",
                    daily_message_limit=lim,
                    daily_messages_used=int(used),
                )
                return CapabilityResult(
                    False,
                    f"Daily message limit reached ({used}/{lim}).",
                    subscription_status=st,
                    daily_message_limit=lim,
                    daily_messages_used=int(used),
                )
        except Exception as e:
            logger.warning("daily_message_limit check failed (fail-open): %s", e)

    return CapabilityResult(True, "")


def automation_capability_check(account_id: int, session=None) -> CapabilityResult:
    row = get_capabilities_row(account_id, session)
    if row is None:
        if _STRICT:
            slog(
                "strict_mode_denial",
                gate="automation",
                account_id=account_id,
                reason="missing_projection",
            )
            return CapabilityResult(False, "Capabilities projection missing (strict mode).")
        return CapabilityResult(True, "")
    if not row.automation_enabled:
        slog(
            "strict_mode_denial",
            gate="automation",
            account_id=account_id,
            reason="automation_disabled",
        )
        return CapabilityResult(False, "Automation is disabled for this account.")
    if _subscription_blocks_send(row):
        slog(
            "strict_mode_denial",
            gate="automation",
            account_id=account_id,
            reason="subscription_blocked",
            subscription_status=(row.subscription_status or "").strip() or None,
        )
        return CapabilityResult(False, "Automation blocked for current subscription status.")
    return CapabilityResult(True, "")


def ai_capability_check(account_id: int, session=None) -> CapabilityResult:
    row = get_capabilities_row(account_id, session)
    if row is None:
        if _STRICT:
            slog(
                "strict_mode_denial",
                gate="ai",
                account_id=account_id,
                reason="missing_projection",
            )
            return CapabilityResult(False, "Capabilities projection missing (strict mode).")
        return CapabilityResult(True, "")
    if not row.ai_enabled:
        slog(
            "strict_mode_denial",
            gate="ai",
            account_id=account_id,
            reason="ai_disabled",
        )
        return CapabilityResult(False, "AI messaging is disabled for this account.")
    if _subscription_blocks_send(row):
        slog(
            "strict_mode_denial",
            gate="ai",
            account_id=account_id,
            reason="subscription_blocked",
            subscription_status=(row.subscription_status or "").strip() or None,
        )
        return CapabilityResult(False, "AI blocked for current subscription status.")
    try:
        from .warmup_enforcement import warmup_denial_ai

        wd = warmup_denial_ai(account_id, session or db.session)
        if wd:
            return CapabilityResult(False, wd.message)
    except Exception as e:
        logger.warning("warmup ai gate failed open: %s", e)
    return CapabilityResult(True, "")


def broadcast_capability_check(account_id: int, session=None) -> CapabilityResult:
    row = get_capabilities_row(account_id, session)
    if row is None:
        if _STRICT:
            slog(
                "strict_mode_denial",
                gate="broadcast",
                account_id=account_id,
                reason="missing_projection",
            )
            return CapabilityResult(False, "Capabilities projection missing (strict mode).")
        return CapabilityResult(True, "")
    if not row.broadcast_enabled:
        slog(
            "strict_mode_denial",
            gate="broadcast",
            account_id=account_id,
            reason="broadcast_disabled",
        )
        return CapabilityResult(False, "Broadcasts / bulk campaigns are disabled for this account.")
    if _subscription_blocks_send(row):
        slog(
            "strict_mode_denial",
            gate="broadcast",
            account_id=account_id,
            reason="subscription_blocked",
            subscription_status=(row.subscription_status or "").strip() or None,
        )
        return CapabilityResult(False, "Broadcast blocked for current subscription status.")
    try:
        from .warmup_enforcement import warmup_denial_broadcast

        wb = warmup_denial_broadcast(account_id, session or db.session)
        if wb:
            return CapabilityResult(False, wb.message)
    except Exception as e:
        logger.warning("warmup broadcast gate failed open: %s", e)
    return CapabilityResult(True, "")


def upsert_capabilities_projection(
    account_id: int,
    *,
    subscription_status: Optional[str] = None,
    ai_enabled: Optional[bool] = None,
    automation_enabled: Optional[bool] = None,
    broadcast_enabled: Optional[bool] = None,
    daily_message_limit: Optional[int] = None,
    monthly_ai_tokens: Optional[int] = None,
    projection_version: Optional[int] = None,
    session=None,
) -> Tuple[bool, str]:
    """
    Upsert projection row (used by internal API; monolith should call that API, not import this).
    """
    sess = session or db.session
    try:
        from .models import WhatsAppAccountCapabilities, WhatsAppAccount

        acc = sess.query(WhatsAppAccount).filter_by(id=account_id).first()
        if not acc:
            return False, "account_not_found"

        row = sess.query(WhatsAppAccountCapabilities).filter_by(account_id=account_id).first()
        if row is None:
            row = WhatsAppAccountCapabilities(account_id=account_id)
            sess.add(row)

        if subscription_status is not None:
            row.subscription_status = str(subscription_status)[:32]
        if ai_enabled is not None:
            row.ai_enabled = bool(ai_enabled)
        if automation_enabled is not None:
            row.automation_enabled = bool(automation_enabled)
        if broadcast_enabled is not None:
            row.broadcast_enabled = bool(broadcast_enabled)
        if daily_message_limit is not None:
            row.daily_message_limit = int(daily_message_limit)
        if monthly_ai_tokens is not None:
            row.monthly_ai_tokens = int(monthly_ai_tokens)
        if projection_version is not None:
            row.projection_version = int(projection_version)
        row.updated_at = datetime.now(timezone.utc)

        sess.flush()
        return True, "ok"
    except Exception as e:
        logger.exception("upsert_capabilities_projection failed: %s", e)
        return False, str(e)
