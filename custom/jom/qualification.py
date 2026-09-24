"""
Phase 2 — Qualification (JOM vertical).

Decides the QUALIFIED tag by combining THREE signals (any one qualifies):
  1. AI intent  — existing whatsapp.ai_chatbot.classify_intent (intent + confidence)
  2. Keywords   — client-defined keywords per department / a global qualify list
  3. Flow node  — the qualification flow marked complete (passed in by caller)

Also maintains the per-lead behaviour score (engagement + consecutive-no-reply),
which Phase 3 uses to decide Ghosted. All workspace-scoped; call behind is_jom_workspace().
"""
import logging
from datetime import datetime, timezone

from models import db
from .models import JomBehaviorScore, JomLifecycleState
from . import config as cfg
from . import departments as dept_svc

logger = logging.getLogger(__name__)


def _now():
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- #
# Behaviour score
# --------------------------------------------------------------------------- #
def _get_or_create_score(workspace_id, customer_phone):
    row = JomBehaviorScore.query.filter_by(
        workspace_id=int(workspace_id), customer_phone=str(customer_phone)
    ).first()
    if not row:
        row = JomBehaviorScore(workspace_id=int(workspace_id), customer_phone=str(customer_phone))
        db.session.add(row)
    return row


def record_inbound(workspace_id, customer_phone, intent=None, confidence=None):
    """A customer message arrived → engagement up, reset the no-reply streak."""
    row = _get_or_create_score(workspace_id, customer_phone)
    row.total_inbound = (row.total_inbound or 0) + 1
    row.episode_inbound = (row.episode_inbound or 0) + 1   # current episode only
    row.engagement_score = (row.engagement_score or 0) + 1
    row.consecutive_no_reply = 0
    if intent is not None:
        row.last_intent = str(intent)[:32]
    if confidence is not None:
        try:
            row.last_confidence = float(confidence)
        except (TypeError, ValueError):
            pass
    row.updated_at = _now()
    db.session.commit()
    return row


def record_outbound_no_reply(workspace_id, customer_phone):
    """We sent a message and the customer did not reply → increment the streak.
    Phase 3 reads consecutive_no_reply to auto-mark Ghosted."""
    row = _get_or_create_score(workspace_id, customer_phone)
    row.total_outbound = (row.total_outbound or 0) + 1
    row.consecutive_no_reply = (row.consecutive_no_reply or 0) + 1
    row.updated_at = _now()
    db.session.commit()
    return row


# --------------------------------------------------------------------------- #
# Qualification decision
# --------------------------------------------------------------------------- #
def _ai_intent(message_text, workspace_id=None):
    """Call the EXISTING intent classifier. Fail-safe: returns (intent, confidence)
    or (None, None) if unavailable."""
    try:
        from whatsapp.ai_chatbot import classify_intent
        # Pass workspace_id so it uses the tenant's configured Gemini key (the
        # global client isn't configured on this backend). Without it the call
        # returns success=False and the lead's last_intent is never recorded.
        res = classify_intent(
            message_text,
            workspace_id=str(workspace_id) if workspace_id is not None else None,
        )
        intent = getattr(res, "intent", None)
        conf = getattr(res, "confidence", None)
        ok = getattr(res, "success", True)
        if not ok:
            return None, None
        return intent, conf
    except Exception as exc:  # noqa: BLE001
        logger.debug("[jom] classify_intent unavailable: %s", exc)
        return None, None


def _keyword_hit(workspace_id, message_text):
    """Qualified if the message contains a keyword from ANY active department
    (reuses the department keywords the client already defines)."""
    if not message_text:
        return False
    low = str(message_text).lower()
    for dep in dept_svc.list_departments(workspace_id, active_only=True):
        for kw in (dep.match_keywords or []):
            if kw and kw in low:
                return True
    return False


def is_negative(message_text):
    """Clear disinterest in free text → the caller closes the lead immediately.
    Deliberately keyword-based: cheaper and far more reliable than asking the LLM,
    and the same signals arrive as button taps (see config.BUTTON_ACTIONS)."""
    if not message_text:
        return False
    low = str(message_text).lower()
    return any(kw in low for kw in cfg.NEGATIVE_KEYWORDS)


def episode_turns(workspace_id, customer_phone):
    """How many customer messages in the CURRENT episode (reset on re-engage/reopen)."""
    row = JomBehaviorScore.query.filter_by(
        workspace_id=int(workspace_id), customer_phone=str(customer_phone)).first()
    return int(getattr(row, "episode_inbound", 0) or 0) if row else 0


def evaluate(workspace_id, customer_phone, message_text, flow_completed=False):
    """Return (is_qualified: bool, signals: dict). Records behaviour + AI signals.
    Does NOT route — caller decides (route on qualify)."""
    intent, conf = _ai_intent(message_text, workspace_id)
    record_inbound(workspace_id, customer_phone, intent=intent, confidence=conf)

    ai_ok = bool(
        intent and str(intent).lower() in cfg.QUALIFY_INTENTS
        and conf is not None and conf >= cfg.QUALIFY_CONFIDENCE
    )
    kw_ok = _keyword_hit(workspace_id, message_text)
    is_qualified = bool(ai_ok or kw_ok or flow_completed)

    signals = {"ai_intent": intent, "ai_confidence": conf, "ai_ok": ai_ok,
               "keyword_ok": kw_ok, "flow_completed": bool(flow_completed)}
    return is_qualified, signals


def qualify_and_route(workspace_id, customer_phone, message_text, flow_completed=False):
    """Full step: evaluate → on qualify, tag Qualified + route to department/agent.
    Returns dict describing the outcome."""
    # 0) CLEAR DISINTEREST WINS OVER EVERYTHING — close now, skip the rest of the cycle.
    #    (Client rule: if they're not interested we do NOT keep drip-messaging them.)
    if is_negative(message_text):
        from . import lifecycle
        record_inbound(workspace_id, customer_phone)   # still count the message
        lifecycle.mark_not_interested(workspace_id, customer_phone)
        logger.info("[jom] %s closed — disinterest detected in message", customer_phone)
        return {"qualified": False, "closed": True, "reason": cfg.CLOSE_REASON_NOT_INTERESTED,
                "signals": {"negative": True}, "department": None, "agent_id": None}

    is_qualified, signals = evaluate(workspace_id, customer_phone, message_text, flow_completed)
    if not is_qualified:
        # Engagement verdict: if we've had QUALIFY_MAX_TURNS customer messages in this
        # episode with no buyer signal AND they're barely engaging, stop interrogating
        # and let them cool off in the Dormant nurture track instead.
        turns = episode_turns(workspace_id, customer_phone)
        score = 0
        _s = JomBehaviorScore.query.filter_by(
            workspace_id=int(workspace_id), customer_phone=str(customer_phone)).first()
        if _s:
            score = int(_s.engagement_score or 0)
        if turns >= cfg.QUALIFY_MAX_TURNS and score <= cfg.STALL_ENGAGEMENT_MAX:
            from . import lifecycle
            lifecycle.mark_dormant(workspace_id, customer_phone)
            logger.info("[jom] %s → dormant (no buyer signal after %s turns, engagement=%s)",
                        customer_phone, turns, score)
            return {"qualified": False, "stalled": True, "turns": turns,
                    "signals": signals, "department": None, "agent_id": None}
        # keep/refresh status as contacted (they replied) without assigning
        row = JomLifecycleState.query.filter_by(
            workspace_id=int(workspace_id), customer_phone=str(customer_phone)).first()
        if row and row.status == cfg.STATUS_NEW:
            row.status = cfg.STATUS_CONTACTED
            row.updated_at = _now()
            db.session.commit()
        return {"qualified": False, "signals": signals, "department": None, "agent_id": None}

    dep, agent_id = dept_svc.route_qualified_lead(workspace_id, customer_phone, message_text)
    return {
        "qualified": True, "signals": signals,
        "department": dep.name if dep else None,
        "department_id": dep.id if dep else None,
        "agent_id": agent_id,
    }
