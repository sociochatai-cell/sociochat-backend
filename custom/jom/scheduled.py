"""
Agent-scheduled one-off follow-ups (JOM vertical).

An agent (or the owner) picks a future date/time + an approved template + its
variables for one customer. We store it and the EXISTING periodic scheduler tick
(whatsapp/scheduler_routes.py) fires it — no new loop, no new cron, so the shared
scheduler is never abused. Each due row is claimed and marked in one pass, so it
can never double-send. When the customer replies after the send, the normal JOM
re-engagement path returns them to the SAME agent (with department/general
fallback — see departments.resolve_agent_with_fallback).

All workspace-scoped and JOM-gated; fail-safe (never raises into the tick).
"""
import logging
from datetime import datetime, timezone

from models import db
from .models import JomScheduledMessage

logger = logging.getLogger(__name__)

_MAX_HORIZON_DAYS = 400   # sanity cap so a typo can't schedule years out
STATUS_PENDING = "pending"
STATUS_SENT = "sent"
STATUS_CANCELLED = "cancelled"
STATUS_FAILED = "failed"


def _now():
    return datetime.now(timezone.utc)


def _to_json(r):
    return {
        "id": r.id,
        "customer_phone": r.customer_phone,
        "template_name": r.template_name,
        "variables": r.variables or {},
        "scheduled_at": r.scheduled_at.isoformat() if r.scheduled_at else None,
        "status": r.status,
        "assigned_agent_id": r.assigned_agent_id,
        "created_by_kind": r.created_by_kind,
        "created_by_id": r.created_by_id,
        "note": r.note,
        "sent_at": r.sent_at.isoformat() if r.sent_at else None,
        "error": r.error,
        "created_at": r.created_at.isoformat() if r.created_at else None,
    }


# --------------------------------------------------------------------------- #
# Create / list / cancel
# --------------------------------------------------------------------------- #
def create(workspace_id, customer_phone, template_name, scheduled_at,
           variables=None, note=None, created_by_kind=None, created_by_id=None):
    """Create a pending scheduled send. `scheduled_at` must be a tz-aware UTC datetime
    in the future. Snapshots the lead's current agent for reply re-assignment.
    Returns (row_json, None) or (None, error_message)."""
    if not customer_phone or not template_name:
        return None, "customer_phone and template_name are required"
    now = _now()
    if scheduled_at.tzinfo is None:
        scheduled_at = scheduled_at.replace(tzinfo=timezone.utc)
    if scheduled_at <= now:
        return None, "scheduled_at must be in the future"
    if (scheduled_at - now).days > _MAX_HORIZON_DAYS:
        return None, f"scheduled_at is too far out (max {_MAX_HORIZON_DAYS} days)"

    # snapshot the current sticky agent so a reply after the send returns to them
    assigned_agent_id = None
    try:
        from .models import JomLifecycleState
        row = JomLifecycleState.query.filter_by(
            workspace_id=int(workspace_id), customer_phone=str(customer_phone)).first()
        if row:
            assigned_agent_id = row.assigned_agent_id
    except Exception:  # noqa: BLE001
        pass

    r = JomScheduledMessage(
        workspace_id=int(workspace_id), customer_phone=str(customer_phone),
        template_name=str(template_name), variables=variables or {},
        scheduled_at=scheduled_at, status=STATUS_PENDING,
        assigned_agent_id=assigned_agent_id,
        created_by_kind=created_by_kind, created_by_id=created_by_id, note=note,
    )
    db.session.add(r)
    db.session.commit()
    logger.info("[jom] scheduled %s -> %s at %s (tpl=%s) by %s#%s",
                r.id, customer_phone, scheduled_at.isoformat(), template_name,
                created_by_kind, created_by_id)
    return _to_json(r), None


def list_for_lead(workspace_id, customer_phone, include_done=True):
    q = JomScheduledMessage.query.filter_by(
        workspace_id=int(workspace_id), customer_phone=str(customer_phone))
    if not include_done:
        q = q.filter(JomScheduledMessage.status == STATUS_PENDING)
    rows = q.order_by(JomScheduledMessage.scheduled_at.asc()).limit(200).all()
    return [_to_json(r) for r in rows]


def list_pending(workspace_id):
    rows = JomScheduledMessage.query.filter_by(
        workspace_id=int(workspace_id), status=STATUS_PENDING
    ).order_by(JomScheduledMessage.scheduled_at.asc()).limit(500).all()
    return [_to_json(r) for r in rows]


def cancel(workspace_id, schedule_id):
    """Cancel a still-pending schedule. Returns (row_json, None) or (None, error)."""
    r = JomScheduledMessage.query.filter_by(
        id=int(schedule_id), workspace_id=int(workspace_id)).first()
    if not r:
        return None, "not_found"
    if r.status != STATUS_PENDING:
        return None, f"cannot cancel a {r.status} schedule"
    r.status = STATUS_CANCELLED
    r.updated_at = _now()
    db.session.commit()
    logger.info("[jom] scheduled %s cancelled", schedule_id)
    return _to_json(r), None


# --------------------------------------------------------------------------- #
# Firing (called from the existing periodic tick)
# --------------------------------------------------------------------------- #
def _body_components(variables):
    ordered = [variables[k] for k in sorted(variables, key=lambda x: int(x))] if variables else []
    if not ordered:
        return None
    return [{"type": "body", "parameters": [{"type": "text", "text": str(v)} for v in ordered]}]


def run_due(workspace_id, now=None, send_fn=None):
    """Fire every pending schedule due as of `now`. Claims each row (pending->sent)
    BEFORE sending so a crash or overlapping tick can't double-send. Returns a summary."""
    now = now or _now()
    due = JomScheduledMessage.query.filter(
        JomScheduledMessage.workspace_id == int(workspace_id),
        JomScheduledMessage.status == STATUS_PENDING,
        JomScheduledMessage.scheduled_at <= now,
    ).order_by(JomScheduledMessage.scheduled_at.asc()).limit(200).all()

    sent = failed = 0
    for r in due:
        # claim first (idempotent guard): flip to 'sent' now; revert to 'failed' on error
        r.status = STATUS_SENT
        r.sent_at = now
        r.updated_at = now
        db.session.commit()
        if send_fn is None:
            continue
        try:
            send_fn(r.customer_phone, r.template_name, r.variables or {})
            sent += 1
        except Exception as exc:  # noqa: BLE001
            r.status = STATUS_FAILED
            r.error = str(exc)[:500]
            r.updated_at = _now()
            db.session.commit()
            failed += 1
            logger.warning("[jom] scheduled send %s FAILED (%s -> %s): %s",
                           r.id, r.template_name, r.customer_phone, exc)
    return {"scheduled_sent": sent, "scheduled_failed": failed}


def _make_send_fn(workspace_id):
    """Build send_fn(phone, template_name, variables) bound to the workspace account."""
    from . import scheduler
    svc = scheduler._service_for(workspace_id)
    if svc is None:
        return None

    def _send(phone, template_name, variables):
        svc.send_template(
            to=phone,
            template_name=template_name,
            components=_body_components(variables),
        )
    return _send


def run_jom_scheduled_sends():
    """Sweep all JOM workspaces for due scheduled follow-ups. Called from the tick."""
    from .gate import allowed_workspace_ids
    results = {}
    for ws in allowed_workspace_ids():
        try:
            send_fn = _make_send_fn(ws)
            results[str(ws)] = run_due(ws, send_fn=send_fn)
        except Exception as exc:  # noqa: BLE001
            logger.exception("[jom] scheduled-send sweep failed ws %s: %s", ws, exc)
            results[str(ws)] = {"error": str(exc)}
    return results
