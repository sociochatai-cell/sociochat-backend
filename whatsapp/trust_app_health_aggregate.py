"""
Platform-level Meta app health aggregation for meta_app_health_snapshots (advisory).
Uses only DB rollups — no external Graph 'health' API required.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Tuple

from sqlalchemy import func, or_

from shared_models import db

from .models import WhatsAppAccount, WhatsAppConversation, WhatsAppMessage, MetaAppHealthSnapshot

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def build_meta_app_health_snapshot(window_minutes: int = 1440) -> MetaAppHealthSnapshot:
    """
    One row per scheduler run. window_minutes default 1440 (24h) for daily job.
    """
    now = _now()
    since = now - timedelta(minutes=max(1, window_minutes))

    sess = db.session
    degraded = (
        sess.query(func.count(WhatsAppAccount.id))
        .filter(
            WhatsAppAccount.is_active.is_(True),
            WhatsAppAccount.webhook_health.notin_(["healthy", "ok", "unknown", ""]),
        )
        .scalar()
        or 0
    )

    wh_failures = (
        sess.query(func.coalesce(func.sum(WhatsAppAccount.webhook_failure_count), 0)).scalar() or 0
    )

    msg_q = (
        sess.query(func.count(WhatsAppMessage.id))
        .join(WhatsAppConversation, WhatsAppMessage.conversation_id == WhatsAppConversation.id)
        .filter(
            WhatsAppMessage.direction == "outgoing",
            WhatsAppMessage.created_at >= since,
        )
    )
    rate_hits = int(
        msg_q.filter(
            or_(
                WhatsAppMessage.error_code == "131029",
                WhatsAppMessage.error_code == "130429",
            )
        ).scalar()
        or 0
    )

    failed_msgs = int(
        msg_q.filter(
            or_(
                WhatsAppMessage.status == "failed",
                WhatsAppMessage.error_code.isnot(None),
            )
        ).scalar()
        or 0
    )

    total_out = int(msg_q.scalar() or 0)
    fail_rate = float(failed_msgs) / float(total_out) if total_out else 0.0

    details: Dict[str, Any] = {
        "accounts_webhook_non_ok": int(degraded),
        "rollup_webhook_failure_counter_sum": int(wh_failures),
        "outbound_in_window": total_out,
        "failed_outbound_in_window": failed_msgs,
        "window_minutes": window_minutes,
    }

    row = MetaAppHealthSnapshot(
        captured_at=now,
        window_minutes=window_minutes,
        graph_error_rate=None,
        webhook_callback_error_count=int(failed_msgs),
        rate_limit_hits=int(rate_hits),
        verification_failure_count=None,
        details=details,
    )
    sess.add(row)
    logger.info(
        "trust_meta_app_health_snapshot_created window_minutes=%s outbound=%s failed=%s rate_limits=%s",
        window_minutes,
        total_out,
        failed_msgs,
        rate_hits,
    )
    return row


def persist_app_health_if_enabled(enabled: bool) -> Tuple[bool, str]:
    if not enabled:
        return False, "disabled"
    try:
        build_meta_app_health_snapshot()
        return True, "ok"
    except Exception as e:
        logger.exception("meta_app_health_snapshot: %s", e)
        return False, str(e)
