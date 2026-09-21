"""
Thin entrypoints that EXISTING SocioChat code calls, always guarded by
is_jom_workspace(). Non-gated workspaces short-circuit to a no-op, so wiring a call
site changes nothing for every other tenant. Keeps the custom vertical in ONE place.
"""
import logging

from .gate import is_jom_workspace
from . import config as cfg

logger = logging.getLogger(__name__)


def on_inbound_message(workspace_id, customer_phone, message_text, meta=None, flow_completed=False, send_fn=None):
    """Called from whatsapp/webhook.py on each inbound. Gated.
    - re-engages a stalled/closed lead to the SAME agent,
    - qualifies + routes a new lead to a destination department.
    Returns a dict outcome (or False when not gated)."""
    if not is_jom_workspace(workspace_id):
        return False
    try:
        from . import lifecycle, qualification, extras
        # 1) re-engagement first (reply to a Ghosted/Dormant/Closed lead → Active, same agent)
        re = lifecycle.on_reply(workspace_id, customer_phone)
        if re.get("reengaged"):
            return {"action": "reengaged", **re}
        # 2) otherwise qualify + route
        out = qualification.qualify_and_route(workspace_id, customer_phone, message_text, flow_completed)
        if out.get("qualified"):
            on_lead_status_change(workspace_id, customer_phone, None, cfg.STATUS_QUALIFIED)
            if not out.get("department_id"):
                extras.handle_out_of_season(workspace_id, customer_phone, message_text, send_fn=send_fn)
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
