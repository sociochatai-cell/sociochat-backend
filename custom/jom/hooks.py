"""
Thin entrypoints that EXISTING SocioChat code calls, always guarded by
is_jom_workspace(). Non-gated workspaces short-circuit to a no-op, so wiring a call
site changes nothing for every other tenant. Keeps the custom vertical in ONE place.
"""
import logging

from .gate import is_jom_workspace
from . import config as cfg

logger = logging.getLogger(__name__)


def _as_text(payload):
    """Inbound content may be a plain string or the webhook's content dict."""
    if isinstance(payload, dict):
        for k in ("text", "body", "button_title", "button_text", "caption"):
            v = payload.get(k)
            if v:
                return str(v)
        return ""
    return str(payload or "")


def _extract_button(payload):
    """(button_key, button_text) from an inbound payload, or (None, None) if not a tap."""
    if not isinstance(payload, dict):
        return (None, None)
    bid = payload.get("button_id") or payload.get("button_payload") or payload.get("payload")
    btxt = payload.get("button_title") or payload.get("button_text")
    return (str(bid) if bid else None, str(btxt) if btxt else None)


def _button_action(bid, btxt):
    """Map a tapped follow-up button onto a lifecycle action (agent/dormant/close)."""
    if bid and bid in cfg.BUTTON_ACTIONS:
        return cfg.BUTTON_ACTIONS[bid]
    hay = " ".join(x for x in (bid, btxt) if x).lower()
    for needle, action in cfg.BUTTON_TEXT_ACTIONS:
        if needle in hay:
            return action
    return None


def _handle_button(workspace_id, customer_phone, action, label):
    """Apply the button outcome. Runs BEFORE re-engagement on purpose: a 'booked' /
    'not planning' tap must CLOSE the lead, not re-activate it."""
    from . import lifecycle, qualification
    qualification.record_inbound(workspace_id, customer_phone)   # the tap is still a reply
    low = (label or "").lower()
    if action == cfg.ACTION_CLOSE:
        reason = (cfg.CLOSE_REASON_BOOKED_ELSEWHERE if "book" in low
                  else cfg.CLOSE_REASON_NOT_INTERESTED)
        lifecycle.mark_closed(workspace_id, customer_phone, reason=reason)
        return {"action": "button_close", "button": label, "reason": reason}
    if action == cfg.ACTION_DORMANT:
        # "need more time" / "still planning" → stay in (or enter) the nurture drip.
        # mark_dormant is a no-op when already enrolled, so the drip CONTINUES from
        # where it was rather than restarting.
        lifecycle.mark_dormant(workspace_id, customer_phone)
        return {"action": "button_dormant", "button": label}
    # ACTION_AGENT — "rework my plan" / "different budget": stop the drip, hand back to
    # the SAME (sticky) agent and let the conversation resume.
    re = lifecycle.on_reply(workspace_id, customer_phone)
    return {"action": "button_agent", "button": label, **re}


def on_inbound_message(workspace_id, customer_phone, message_text, meta=None, flow_completed=False, send_fn=None):
    """Called from whatsapp/webhook.py on each inbound. Gated.
    Order matters:
      0) a follow-up BUTTON tap decides the lifecycle outcome outright,
      1) otherwise re-engage a stalled/closed lead to the SAME agent,
      2) otherwise qualify + route a new lead to a destination department.
    Returns a dict outcome (or False when not gated)."""
    if not is_jom_workspace(workspace_id):
        return False
    try:
        from . import lifecycle, qualification, extras

        # 0) Follow-up button tap → direct lifecycle outcome (close / dormant / agent)
        bid, btxt = _extract_button(message_text)
        if bid or btxt:
            action = _button_action(bid, btxt)
            if action:
                return _handle_button(workspace_id, customer_phone, action, btxt or bid)

        text = _as_text(message_text)

        # 0b) Clear disinterest in FREE TEXT also outranks re-engagement. Without this a
        # "not interested" reply from a Ghosted/Dormant lead would be treated as them
        # coming back and re-activate them — the opposite of what the client wants.
        if qualification.is_negative(text):
            qualification.record_inbound(workspace_id, customer_phone)
            lifecycle.mark_not_interested(workspace_id, customer_phone)
            logger.info("[jom] %s closed — disinterest in free text", customer_phone)
            return {"action": "closed_disinterest", "reason": cfg.CLOSE_REASON_NOT_INTERESTED}

        # 1) re-engagement (reply to a Ghosted/Dormant/Closed lead → Active, same agent)
        re = lifecycle.on_reply(workspace_id, customer_phone)
        if re.get("reengaged"):
            # Count the message + capture intent, but do NOT re-route: the agent is sticky.
            qualification.record_inbound(workspace_id, customer_phone)
            return {"action": "reengaged", **re}

        # 2) otherwise qualify + route
        out = qualification.qualify_and_route(workspace_id, customer_phone, text, flow_completed)
        if out.get("qualified"):
            on_lead_status_change(workspace_id, customer_phone, None, cfg.STATUS_QUALIFIED)
            if not out.get("department_id"):
                extras.handle_out_of_season(workspace_id, customer_phone, text, send_fn=send_fn)
        return {"action": "qualify", **out}
    except Exception as exc:  # noqa: BLE001
        logger.warning("[jom] on_inbound_message failed for %s: %s", customer_phone, exc)
        return False


def on_lead_status_change(workspace_id, customer_phone, old_status, new_status):
    """Fire Meta CAPI down-funnel events. Gated + fail-safe."""
    if not is_jom_workspace(workspace_id):
        return False
    try:
        from . import capi_leads
        if new_status == cfg.STATUS_QUALIFIED:
            capi_leads.on_qualified(workspace_id, customer_phone)
        elif new_status in ("converted", "won"):
            capi_leads.on_converted(workspace_id, customer_phone)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.debug("[jom] on_lead_status_change noop/failed: %s", exc)
        return False
