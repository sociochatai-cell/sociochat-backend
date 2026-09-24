"""
WhatsApp 24-hour window management for JOM.

THE RULE THAT GOVERNS EVERYTHING:
  * INSIDE the 24h customer-service window (i.e. within 24h of the customer's last
    message) we may send FREE TEXT — this is where the warm AI conversation lives.
  * OUTSIDE it, WhatsApp accepts ONLY approved templates.
  * Any customer reply opens a FRESH 24 hours.

So when a live conversation goes quiet and the window lapses, the AI literally cannot
speak again. This module finds those leads and sends the "Continue" template — a
Meta-approved skeleton whose {{2}} variable we fill AT SEND TIME with a line about
their actual conversation ("Last time we were looking at beach villas in Bali..."),
which is what makes an approved template feel personal. Their reply re-opens the
window and the AI picks the conversation back up with the full memory card.

One nudge per silence period: continue_sent_at is stamped on send and cleared on the
next inbound, so we never nag.
"""
import logging
from datetime import datetime, timezone, timedelta

from models import db
from .models import JomLifecycleState
from . import config as cfg

logger = logging.getLogger(__name__)

WINDOW_HOURS = 24
# Wait a little after the window shuts before nudging, so we don't fire the instant
# it lapses while the customer is simply asleep.
NUDGE_AFTER_HOURS = 26
# Only nudge conversations that were actually live — not leads already in a drip.
NUDGEABLE_STATUSES = (cfg.STATUS_ACTIVE, cfg.STATUS_ASSIGNED, cfg.STATUS_QUALIFIED)


def _now():
    return datetime.now(timezone.utc)


def _aware(dt):
    """DB values can come back naive; compare safely."""
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def is_window_open(row, now=None):
    """True when we may still send FREE TEXT to this customer."""
    now = now or _now()
    last_in = _aware(getattr(row, "last_inbound_at", None))
    if not last_in:
        return False          # they never wrote to us → business-initiated only
    return (now - last_in) < timedelta(hours=WINDOW_HOURS)


def can_send_free_text(workspace_id, customer_phone, now=None):
    """Convenience for callers that only have the phone."""
    row = JomLifecycleState.query.filter_by(
        workspace_id=int(workspace_id), customer_phone=str(customer_phone)).first()
    return bool(row) and is_window_open(row, now)


def context_line(workspace_id, customer_phone):
    """The dynamic {{2}} for the Continue template — what makes it feel personal.
    Prefers the running summary the AI maintains; falls back to the destination; then
    to a warm generic line. Never returns empty (templates reject blank variables)."""
    try:
        from . import memory
        card = memory.get_card(workspace_id, customer_phone)
        summary = (card.get("last_summary") or "").strip()
        if summary:
            return summary if len(summary) <= 500 else summary[:497] + "..."
        dest = card.get("destination")
        if dest:
            return f"We were in the middle of planning your {dest} trip."
    except Exception as exc:  # noqa: BLE001
        logger.debug("[jom] context_line failed: %s", exc)
    return "We were in the middle of planning your trip."


def due_continue_nudges(workspace_id, now=None, limit=200):
    """Live conversations whose 24h window has lapsed and that haven't been nudged
    since the customer's last message."""
    now = now or _now()
    cutoff = now - timedelta(hours=NUDGE_AFTER_HOURS)
    rows = JomLifecycleState.query.filter(
        JomLifecycleState.workspace_id == int(workspace_id),
        JomLifecycleState.status.in_(NUDGEABLE_STATUSES),
        JomLifecycleState.last_inbound_at.isnot(None),
        JomLifecycleState.last_inbound_at <= cutoff,
    ).limit(limit).all()
    due = []
    for r in rows:
        sent = _aware(getattr(r, "continue_sent_at", None))
        last_in = _aware(r.last_inbound_at)
        if sent and last_in and sent > last_in:
            continue          # already nudged for THIS silence period
        due.append(r)
    return due


def run_continue_nudges(workspace_id, now=None, send_fn=None):
    """Send the Continue re-opener to every lead whose window has lapsed.
    send_fn(workspace_id, phone, template_name, variables: dict) -> None
    Returns a summary dict. Fail-safe: one bad send never stops the sweep."""
    now = now or _now()
    sent = 0
    for row in due_continue_nudges(workspace_id, now):
        variables = {
            "1": (getattr(row, "customer_name", None) or "there"),
            "2": context_line(workspace_id, row.customer_phone),
        }
        if send_fn:
            try:
                send_fn(workspace_id, row.customer_phone, cfg.TPL_CONTINUE, variables)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[jom] continue nudge failed for %s: %s", row.customer_phone, exc)
                continue
        row.continue_sent_at = now
        row.last_outbound_at = now
        row.updated_at = now
        db.session.commit()
        sent += 1
        logger.info("[jom] continue re-opener sent to %s", row.customer_phone)
    return {"continue_sent": sent}
