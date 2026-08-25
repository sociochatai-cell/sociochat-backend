"""
Outbound duplicate detector (observability + alerting).

When the same outgoing message is recorded twice for one conversation inside a
short window, we log the event (with both message ids, both wamids, and the gap
in milliseconds) to `whatsapp_duplicate_events` and raise an ops alert. This is
how we catch a partner automation (e.g. FirstConnection) that sends the opening
step twice — the moment it happens, with hard data and the latency between the
two sends.

Design rules:
  * Never breaks a send. Every path is wrapped; its own writes use a separate
    short-lived connection so a failure here cannot poison the send transaction.
  * Opt-in per account. Off for everyone unless WHATSAPP_DUPLICATE_DETECT=1
    (global) or the account id is listed in WHATSAPP_DUPLICATE_DETECT_ACCOUNTS.
  * Cheap. One indexed lookup over the last few outbound rows of the same
    conversation.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_TABLE = "whatsapp_duplicate_events"
_table_ready = False
_table_lock = threading.Lock()

# How far back to look for an identical earlier send (seconds).
_WINDOW_SEC = 90


def _enabled(account_id: Optional[int]) -> bool:
    if str(os.getenv("WHATSAPP_DUPLICATE_DETECT") or "").strip().lower() in ("1", "true", "yes", "on"):
        return True
    allow = str(os.getenv("WHATSAPP_DUPLICATE_DETECT_ACCOUNTS") or "").strip()
    if not allow or account_id is None:
        return False
    return str(account_id) in {a.strip() for a in allow.split(",") if a.strip()}


def _content_text(content: Any) -> str:
    """Stable text signature for a stored message content blob."""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, dict):
        t = content.get("text")
        if isinstance(t, str):
            return t.strip()
        if isinstance(t, dict):
            return str(t.get("body", "")).strip()
        try:
            return json.dumps(content, sort_keys=True, separators=(",", ":"))[:2000]
        except Exception:
            return ""
    return ""


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# Number of leading (normalized) characters used to fingerprint an opening
# message. Two sends that share this opening are treated as the same step even
# if their tails differ (e.g. one appends "Q1 of 6…" and the other doesn't) —
# which is exactly how a partner's two automations produce near-identical
# openings.
_SIG_PREFIX_LEN = 60


def _signature(content: Any) -> str:
    """Fingerprint the OPENING of a message (whitespace-normalized prefix)."""
    text = _content_text(content)
    if not text:
        return ""
    norm = re.sub(r"\s+", " ", text.strip().lower())
    return _hash(norm[:_SIG_PREFIX_LEN])


def _ensure_table() -> None:
    global _table_ready
    if _table_ready:
        return
    with _table_lock:
        if _table_ready:
            return
        from sqlalchemy import text as sa_text
        from shared_models import db

        with db.engine.begin() as conn:
            conn.execute(sa_text(
                f"CREATE TABLE IF NOT EXISTS {_TABLE} ("
                " id SERIAL PRIMARY KEY,"
                " conversation_id INTEGER,"
                " account_id INTEGER,"
                " content_hash VARCHAR(64),"
                " first_message_id INTEGER,"
                " first_wamid TEXT,"
                " second_message_id INTEGER,"
                " second_wamid TEXT,"
                " first_at TIMESTAMPTZ,"
                " second_at TIMESTAMPTZ,"
                " gap_ms INTEGER,"
                " inbound_wamid TEXT,"
                " detected_at TIMESTAMPTZ NOT NULL DEFAULT now())"
            ))
            conn.execute(sa_text(
                f"CREATE INDEX IF NOT EXISTS ix_{_TABLE}_conv ON {_TABLE} (conversation_id, detected_at)"
            ))
        _table_ready = True


def _record_event(row: Dict[str, Any]) -> None:
    """Persist the duplicate event on its OWN connection (independent of the send)."""
    from sqlalchemy import text as sa_text
    from shared_models import db

    with db.engine.begin() as conn:
        conn.execute(sa_text(
            f"INSERT INTO {_TABLE} (conversation_id, account_id, content_hash,"
            " first_message_id, first_wamid, second_message_id, second_wamid,"
            " first_at, second_at, gap_ms, inbound_wamid) VALUES"
            " (:conversation_id, :account_id, :content_hash, :first_message_id,"
            " :first_wamid, :second_message_id, :second_wamid, :first_at, :second_at,"
            " :gap_ms, :inbound_wamid)"
        ), row)


def _alert(row: Dict[str, Any]) -> None:
    """Best-effort ops alert. Never raises."""
    try:
        from core.ops.slack import notify_ops

        notify_ops(
            "whatsapp.duplicate_send",
            f"Duplicate outbound in conversation {row['conversation_id']} "
            f"(gap {row['gap_ms']} ms) — same message sent twice.",
            severity="warning",
            details={
                "account_id": row.get("account_id"),
                "gap_ms": row.get("gap_ms"),
                "first_wamid": row.get("first_wamid"),
                "second_wamid": row.get("second_wamid"),
                "inbound_wamid": row.get("inbound_wamid"),
            },
        )
    except Exception:
        pass


def check_and_log_duplicate(db_session, message, conversation) -> None:
    """Call right after an outgoing message row is stored. Fail-safe: swallows all errors."""
    try:
        account_id = getattr(conversation, "account_id", None)
        if not _enabled(account_id):
            return

        sig = _signature(getattr(message, "content", None))
        if not sig:
            return

        from .models import WhatsAppMessage

        now = datetime.now(timezone.utc)
        window_start = now - timedelta(seconds=_WINDOW_SEC)

        recent = (
            db_session.query(WhatsAppMessage)
            .filter(
                WhatsAppMessage.conversation_id == conversation.id,
                WhatsAppMessage.id != message.id,
                WhatsAppMessage.direction.in_(("outgoing", "echo")),
                WhatsAppMessage.created_at >= window_start,
            )
            .order_by(WhatsAppMessage.created_at.desc())
            .limit(8)
            .all()
        )

        twin = None
        for prev in recent:
            if _signature(getattr(prev, "content", None)) == sig:
                twin = prev
                break
        if twin is None:
            return  # first send of this content — nothing to flag

        # Correlate the customer message that triggered this exchange.
        inbound = (
            db_session.query(WhatsAppMessage)
            .filter(
                WhatsAppMessage.conversation_id == conversation.id,
                WhatsAppMessage.direction == "incoming",
            )
            .order_by(WhatsAppMessage.created_at.desc())
            .first()
        )

        first_at = getattr(twin, "created_at", None)
        gap_ms = None
        if first_at is not None:
            if first_at.tzinfo is None:
                first_at = first_at.replace(tzinfo=timezone.utc)
            gap_ms = int((now - first_at).total_seconds() * 1000)

        row = {
            "conversation_id": conversation.id,
            "account_id": account_id,
            "content_hash": sig[:64],
            "first_message_id": getattr(twin, "id", None),
            "first_wamid": getattr(twin, "wamid", None),
            "second_message_id": getattr(message, "id", None),
            "second_wamid": getattr(message, "wamid", None),
            "first_at": first_at,
            "second_at": now,
            "gap_ms": gap_ms,
            "inbound_wamid": getattr(inbound, "wamid", None) if inbound else None,
        }

        _ensure_table()
        _record_event(row)
        logger.warning(
            "[duplicate-send] conv=%s account=%s gap_ms=%s first=%s second=%s inbound=%s",
            row["conversation_id"], row["account_id"], row["gap_ms"],
            row["first_wamid"], row["second_wamid"], row["inbound_wamid"],
        )
        _alert(row)
    except Exception as exc:
        logger.debug("[duplicate-send] detector skipped: %s", exc)
