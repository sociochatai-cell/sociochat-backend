"""
Trust snapshot engine — persist trust_snapshots + phone reputation rows; diff + logs.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

from shared_models import db

from .models import TrustSnapshot, WhatsAppAccount, WhatsAppPhoneReputationSnapshot, accounts_query_with_any_token
from .trust_app_health_aggregate import build_meta_app_health_snapshot
from .trust_reputation_poll import fetch_phone_graph_profile
from .trust_signal_collector import collect_account_trust_signals, snapshot_column_values
from .trust_snapshot_diff import diff_trust_snapshots

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _env_bool(name: str, default: bool) -> bool:
    v = (os.getenv(name) or "").strip().lower()
    if not v:
        return default
    return v in ("1", "true", "yes", "on")


def trust_snapshots_enabled() -> bool:
    return _env_bool("WH_TRUST_SNAPSHOT_ENABLED", True)


def snapshot_already_today(account_id: int, session) -> bool:
    """At most one calendar-day snapshot per account (UTC) unless forced."""
    start = _now().replace(hour=0, minute=0, second=0, microsecond=0)
    exists = (
        session.query(TrustSnapshot.id)
        .filter(TrustSnapshot.account_id == account_id, TrustSnapshot.captured_at >= start)
        .first()
    )
    return exists is not None


def _prev_snapshot_dict(session, account_id: int) -> Optional[Dict[str, Any]]:
    row = (
        session.query(TrustSnapshot)
        .filter(TrustSnapshot.account_id == account_id)
        .order_by(TrustSnapshot.captured_at.desc())
        .first()
    )
    if not row:
        return None
    return {
        "quality_rating": row.quality_rating,
        "messaging_tier": row.messaging_tier,
        "webhook_health": row.webhook_health,
        "restriction_state": row.restriction_state,
        "operational_mode": row.operational_mode,
        "inputs": row.inputs if isinstance(row.inputs, dict) else {},
    }


def capture_trust_snapshot_for_account(
    account: WhatsAppAccount,
    *,
    session,
    force: bool = False,
    skip_if_same_day: bool = True,
) -> Tuple[Optional[TrustSnapshot], str]:
    """
    Persist trust + phone reputation snapshots for one account.
    Returns (snapshot_or_none, status).
    """
    if not trust_snapshots_enabled():
        return None, "disabled"

    if skip_if_same_day and not force and snapshot_already_today(account.id, session):
        return None, "skipped_same_day"

    inputs = collect_account_trust_signals(account, session)
    graph = fetch_phone_graph_profile(account)
    if graph:
        inputs["graph_fetch"] = graph
        if graph.get("quality_rating"):
            inputs["graph_quality_rating"] = graph.get("quality_rating")
        if graph.get("name_status"):
            inputs["graph_name_status"] = graph.get("name_status")

    cols = snapshot_column_values(account, inputs)
    if graph:
        if graph.get("quality_rating"):
            cols["quality_rating"] = str(graph.get("quality_rating")).strip().upper()[:16]
        if graph.get("name_status"):
            cols["name_status"] = str(graph.get("name_status"))[:64]

    prev = _prev_snapshot_dict(session, account.id)
    cur_for_diff = {**cols, "inputs": inputs}
    events = diff_trust_snapshots(prev, cur_for_diff, account_id=account.id)

    ts = TrustSnapshot(
        account_id=account.id,
        captured_at=_now(),
        quality_rating=cols.get("quality_rating"),
        messaging_tier=cols.get("messaging_tier"),
        name_status=cols.get("name_status"),
        verification_status=cols.get("verification_status"),
        webhook_health=cols.get("webhook_health"),
        webhook_subscription_status=cols.get("webhook_subscription_status"),
        restriction_state=cols.get("restriction_state"),
        operational_mode=cols.get("operational_mode"),
        trust_score=cols.get("trust_score"),
        inputs=inputs,
        notes=",".join(events) if events else None,
    )
    session.add(ts)

    rep = WhatsAppPhoneReputationSnapshot(
        account_id=account.id,
        phone_number_id=account.phone_number_id,
        captured_at=_now(),
        quality_rating=cols.get("quality_rating"),
        messaging_tier=cols.get("messaging_tier"),
        name_status=cols.get("name_status"),
        raw_graph=graph,
    )
    session.add(rep)

    logger.info(
        "trust_snapshot_created account_id=%s events=%s",
        account.id,
        events or "none",
    )
    return ts, "created"


def run_trust_snapshot_batch(
    *,
    limit: int = 500,
    include_inactive: bool = False,
    force: bool = False,
) -> Dict[str, Any]:
    """
    Process active accounts (token present). One meta_app_health row per batch.
    """
    if not trust_snapshots_enabled():
        return {"status": "disabled", "processed": 0, "snapshots": 0, "skipped": 0, "errors": 0}

    q = WhatsAppAccount.query
    if not include_inactive:
        q = q.filter_by(is_active=True)
        q = accounts_query_with_any_token(q)
    q = q.order_by(WhatsAppAccount.id.asc()).limit(max(1, limit))
    rows = q.all()

    created = 0
    skipped = 0
    errors = 0
    sess = db.session

    try:
        build_meta_app_health_snapshot()
    except Exception as e:
        logger.warning("meta_app_health in batch: %s", e)

    for acc in rows:
        try:
            snap, st = capture_trust_snapshot_for_account(acc, session=sess, force=force, skip_if_same_day=not force)
            if st == "created":
                created += 1
            elif st == "skipped_same_day":
                skipped += 1
            elif st == "disabled":
                break
        except Exception as e:
            errors += 1
            logger.exception("trust snapshot account_id=%s: %s", acc.id, e)

    try:
        sess.commit()
    except Exception as e:
        logger.exception("trust batch commit: %s", e)
        sess.rollback()
        return {"status": "commit_failed", "error": str(e), "processed": len(rows), "snapshots": 0}

    return {
        "status": "ok",
        "processed": len(rows),
        "snapshots_created": created,
        "skipped_same_day": skipped,
        "errors": errors,
    }
