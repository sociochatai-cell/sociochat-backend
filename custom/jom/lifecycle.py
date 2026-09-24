"""
Phase 3 — Lifecycle orchestration (JOM vertical).

State machine on jom_lifecycle_state:
  ASSIGNED --(agent taps)--> GHOSTED  -> drip Day 1/5/15 -> CLOSED
  ASSIGNED --(agent taps)--> DORMANT  -> drip Day 15/30/45 -> CLOSED
  any inbound reply → ACTIVE, and RE-ENGAGE keeps the SAME agent (sticky).

Cadence is scheduled with status_since + next_followup_at. `tick(now)` sends any due
follow-up (via send_fn), advances the stage, and auto-closes after the last step.
Message SENDING is injected (send_fn) so the real WhatsApp send is wired at deploy;
the state logic here is fully unit-testable with a mock send_fn.
All workspace-scoped; call behind is_jom_workspace().
"""
import logging
from datetime import datetime, timezone, timedelta

from models import db
from .models import JomLifecycleState, JomBehaviorScore
from . import config as cfg

logger = logging.getLogger(__name__)


def _now():
    return datetime.now(timezone.utc)


def _cadence(status):
    return cfg.GHOSTED_DAYS if status == cfg.STATUS_GHOSTED else cfg.DORMANT_DAYS


def _get(workspace_id, customer_phone):
    return JomLifecycleState.query.filter_by(
        workspace_id=int(workspace_id), customer_phone=str(customer_phone)).first()


# --------------------------------------------------------------------------- #
# Enter a stall state → schedule the first follow-up
# --------------------------------------------------------------------------- #
def _enter_drip(workspace_id, customer_phone, status):
    """Tag the lead Ghosted/Dormant AND auto-enrol it in the matching drip.
    Setting the status IS the enrolment — status_since + drip_stage=0 + next_followup_at
    are what the scheduler tick picks up. Switching between Ghosted/Dormant re-enrols
    from step 0 (never both at once)."""
    row = _get(workspace_id, customer_phone)
    if not row:
        return None
    was = row.status                      # capture BEFORE mutating (was reported wrong)
    if was == status and row.next_followup_at is not None:
        return row                        # already enrolled in this drip — don't restart
    now = _now()
    days = _cadence(status)
    row.status = status
    row.status_since = now
    row.drip_stage = "0"
    row.next_followup_at = now + timedelta(days=days[0])
    row.close_reason = None
    row.updated_at = now
    db.session.commit()
    logger.info("[jom] %s auto-enrolled in %s drip (first step in %sd)", customer_phone, status, days[0])
    _fire_status_change(workspace_id, customer_phone, was, status)
    return row


def mark_ghosted(workspace_id, customer_phone):
    return _enter_drip(workspace_id, customer_phone, cfg.STATUS_GHOSTED)


def mark_dormant(workspace_id, customer_phone):
    return _enter_drip(workspace_id, customer_phone, cfg.STATUS_DORMANT)


def mark_closed(workspace_id, customer_phone, reason=None):
    """Close a lead NOW and STOP any running drip. Used when the customer shows clear
    disinterest (free text or a button) — the client's explicit rule is that we do NOT
    finish the remaining drip steps in that case."""
    row = _get(workspace_id, customer_phone)
    if not row:
        return None
    was = row.status
    if was == cfg.STATUS_CLOSED and row.next_followup_at is None:
        return row
    now = _now()
    row.status = cfg.STATUS_CLOSED
    row.status_since = now
    row.next_followup_at = None       # <- stops the drip dead
    row.drip_stage = None
    row.close_reason = reason
    row.updated_at = now
    db.session.commit()
    logger.info("[jom] %s CLOSED (reason=%s, was=%s) — drip stopped", customer_phone, reason, was)
    _fire_status_change(workspace_id, customer_phone, was, cfg.STATUS_CLOSED)
    return row


def mark_not_interested(workspace_id, customer_phone):
    """Explicit disinterest → close immediately with a reportable reason."""
    return mark_closed(workspace_id, customer_phone, reason=cfg.CLOSE_REASON_NOT_INTERESTED)


# --------------------------------------------------------------------------- #
# The scheduler tick — send due follow-ups, advance, auto-close
# --------------------------------------------------------------------------- #
def due_rows(workspace_id, now=None):
    now = now or _now()
    return JomLifecycleState.query.filter(
        JomLifecycleState.workspace_id == int(workspace_id),
        JomLifecycleState.status.in_([cfg.STATUS_GHOSTED, cfg.STATUS_DORMANT]),
        JomLifecycleState.next_followup_at.isnot(None),
        JomLifecycleState.next_followup_at <= now,
    ).all()


def tick(workspace_id, now=None, send_fn=None):
    """Process all due follow-ups for a workspace. Returns a summary dict.
    send_fn(workspace_id, phone, status, stage) -> None sends the actual message.
    Each row is DRAINED of all stages overdue as of `now` (catch-up safe), so a
    single call fully advances a lead whether the scheduler runs daily or misses cycles."""
    now = now or _now()
    sent = closed = 0
    for row in due_rows(workspace_id, now):
        # drain every stage that is due as of `now`
        while (row.status in (cfg.STATUS_GHOSTED, cfg.STATUS_DORMANT)
               and row.next_followup_at is not None and row.next_followup_at <= now):
            days = _cadence(row.status)
            stage = int(row.drip_stage or 0)
            if send_fn:
                try:
                    send_fn(workspace_id, row.customer_phone, row.status, stage)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("[jom] follow-up send failed %s: %s", row.customer_phone, exc)
            row.last_outbound_at = now
            _bump_no_reply(workspace_id, row.customer_phone)
            sent += 1
            next_stage = stage + 1
            if next_stage < len(days):
                row.drip_stage = str(next_stage)
                row.next_followup_at = (row.status_since or now) + timedelta(days=days[next_stage])
            else:
                if send_fn:
                    try:
                        send_fn(workspace_id, row.customer_phone, cfg.STATUS_CLOSED, -1)
                    except Exception:  # noqa: BLE001
                        pass
                row.status = cfg.STATUS_CLOSED
                row.next_followup_at = None
                row.drip_stage = None
                row.close_reason = cfg.CLOSE_REASON_DRIP_EXHAUSTED
                closed += 1
                _fire_status_change(workspace_id, row.customer_phone, cfg.STATUS_GHOSTED, cfg.STATUS_CLOSED)
            row.updated_at = now
            db.session.commit()
    return {"sent": sent, "closed": closed}


def _bump_no_reply(workspace_id, customer_phone):
    s = JomBehaviorScore.query.filter_by(
        workspace_id=int(workspace_id), customer_phone=str(customer_phone)).first()
    if s:
        s.consecutive_no_reply = (s.consecutive_no_reply or 0) + 1
        s.total_outbound = (s.total_outbound or 0) + 1
        s.updated_at = _now()
        db.session.commit()


# --------------------------------------------------------------------------- #
# Re-engagement — any reply → Active, KEEP the same agent (client's explicit ask)
# --------------------------------------------------------------------------- #
def on_reply(workspace_id, customer_phone):
    """Customer replied. If they were Ghosted/Dormant/Closed → re-activate and return
    the SAME agent who worked them (sticky). Returns dict with agent_id (may need
    fallback in Phase 5 if that agent is gone)."""
    row = _get(workspace_id, customer_phone)
    if not row:
        return {"reengaged": False, "agent_id": None}
    was = row.status
    reengaged = was in (cfg.STATUS_GHOSTED, cfg.STATUS_DORMANT, cfg.STATUS_CLOSED)
    if reengaged:
        row.status = cfg.STATUS_ACTIVE
        row.next_followup_at = None
        row.drip_stage = None
        row.status_since = _now()
    row.last_inbound_at = _now()
    row.updated_at = _now()
    db.session.commit()
    # Reset the no-reply streak. NOTE: message COUNTING lives in
    # qualification.record_inbound (single source of truth) — incrementing here too
    # double-counted every inbound. On a genuine re-engagement we also start a FRESH
    # episode so the "qualified within 5-6 turns?" verdict restarts.
    s = JomBehaviorScore.query.filter_by(
        workspace_id=int(workspace_id), customer_phone=str(customer_phone)).first()
    if s:
        s.consecutive_no_reply = 0
        if reengaged:
            s.episode_inbound = 0
        s.updated_at = _now()
        db.session.commit()
    if reengaged:
        _fire_status_change(workspace_id, customer_phone, was, cfg.STATUS_ACTIVE)
    return {"reengaged": reengaged, "from_status": was, "agent_id": row.assigned_agent_id}


# --------------------------------------------------------------------------- #
# Behaviour-based auto-Ghosted (Phase 3 v2 — optional; agent-tag is v1)
# --------------------------------------------------------------------------- #
def maybe_auto_ghost(workspace_id, customer_phone):
    """If consecutive no-replies crossed the threshold while ASSIGNED → mark Ghosted."""
    row = _get(workspace_id, customer_phone)
    if not row or row.status != cfg.STATUS_ASSIGNED:
        return False
    s = JomBehaviorScore.query.filter_by(
        workspace_id=int(workspace_id), customer_phone=str(customer_phone)).first()
    if s and (s.consecutive_no_reply or 0) >= cfg.GHOSTED_AFTER_NO_REPLY:
        mark_ghosted(workspace_id, customer_phone)
        return True
    return False


# --------------------------------------------------------------------------- #
# Status-change fan-out (Phase 4 wires CAPI here)
# --------------------------------------------------------------------------- #
def _fire_status_change(workspace_id, customer_phone, old_status, new_status):
    try:
        from .hooks import on_lead_status_change
        on_lead_status_change(workspace_id, customer_phone, old_status, new_status)
    except Exception as exc:  # noqa: BLE001
        logger.debug("[jom] status-change hook noop/failed: %s", exc)
