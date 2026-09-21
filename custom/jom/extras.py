"""
Phase 5 — Returning-fresh-lead, out-of-season fallback, owner notifications (JOM).

- reopen_as_fresh: a returning contact (e.g. off-season, different destination) is
  treated as a FRESH inquiry but their behaviour history is preserved (reopen_count++).
- handle_out_of_season: a lead for a paused/no-matching department → holding message to
  the customer + email the workspace owner so a human picks it up.
All workspace-scoped; call behind is_jom_workspace().
"""
import logging
from datetime import datetime, timezone

from sqlalchemy import text

from models import db
from .models import JomLifecycleState
from . import config as cfg

logger = logging.getLogger(__name__)

HOLDING_MESSAGE = ("Thank you for reaching out! Our team will get back to you shortly "
                   "with the best options for your trip.")


def _now():
    return datetime.now(timezone.utc)


def reopen_as_fresh(workspace_id, customer_phone):
    """Returning contact → fresh inquiry, history kept. Resets the lifecycle to NEW,
    clears department/drip, increments reopen_count. assigned_agent_id retained as a
    prior-agent hint (re-engage keeps the same agent; a brand-new inquiry may re-route)."""
    row = JomLifecycleState.query.filter_by(
        workspace_id=int(workspace_id), customer_phone=str(customer_phone)).first()
    if not row:
        row = JomLifecycleState(workspace_id=int(workspace_id), customer_phone=str(customer_phone))
        db.session.add(row)
    row.status = cfg.STATUS_NEW
    row.department_id = None
    row.drip_stage = None
    row.next_followup_at = None
    row.status_since = _now()
    row.reopen_count = (row.reopen_count or 0) + 1
    row.updated_at = _now()
    db.session.commit()
    logger.info("[jom] reopened %s as fresh (reopen_count=%s)", customer_phone, row.reopen_count)
    return row


def owner_email(workspace_id):
    """Resolve the workspace owner's email (workspaces2.user_id -> users.email)."""
    try:
        row = db.session.execute(text(
            "SELECT u.email FROM workspaces2 w JOIN users u ON u.id = w.user_id WHERE w.id = :w"
        ), {"w": int(workspace_id)}).first()
        return row[0] if row else None
    except Exception as exc:  # noqa: BLE001
        logger.warning("[jom] owner_email lookup failed: %s", exc)
        return None


def notify_owner(workspace_id, subject, body):
    to = owner_email(workspace_id)
    if not to:
        return False
    try:
        from mailer import send_mail
        send_mail(to, subject, body, workspace_id=int(workspace_id))
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("[jom] notify_owner failed: %s", exc)
        return False


def handle_out_of_season(workspace_id, customer_phone, message_text, send_fn=None):
    """No active department matched → hold the customer + alert the owner."""
    if send_fn:
        try:
            send_fn(workspace_id, customer_phone, HOLDING_MESSAGE)
        except Exception:  # noqa: BLE001
            pass
    notify_owner(
        workspace_id,
        subject="New lead needs attention (out-of-season / unrouted)",
        body=f"A WhatsApp lead {customer_phone} came in but no active department matched.\n"
             f"Message: {message_text}\nPlease assign it manually.",
    )
    return True
