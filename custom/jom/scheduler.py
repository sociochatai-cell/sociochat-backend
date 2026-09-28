"""
JOM scheduler — fires due Ghosted/Dormant follow-ups, the 24h "Continue" re-opener,
and auto-closes at the end of a drip. Called from the existing periodic sweep
(whatsapp/scheduler_routes.py::scheduler_tick).

WINDOW AWARENESS (the important bit): WhatsApp only accepts FREE TEXT within 24h of
the customer's last message. Every Ghosted/Dormant step lands far outside that window,
so those MUST go out as Meta-approved TEMPLATES. This module picks the right channel
per lead: template when the window is shut, plain text when it happens to be open.

Gated + fail-safe: does nothing for non-JOM workspaces, never raises into the scheduler.
"""
import logging

from models import db
from .gate import allowed_workspace_ids
from . import config as cfg
from . import lifecycle, window

logger = logging.getLogger(__name__)


def _service_for(workspace_id):
    """Build a WhatsApp sender bound to this workspace's account, or None."""
    try:
        from whatsapp.models import WhatsAppAccount
        from whatsapp.services import WhatsAppService
    except Exception as exc:  # noqa: BLE001
        logger.warning("[jom] sender imports unavailable: %s", exc)
        return None
    acc = WhatsAppAccount.query.filter_by(workspace_id=str(workspace_id)).first()
    if not acc:
        logger.warning("[jom] no WhatsApp account for workspace %s — cannot send", workspace_id)
        return None
    try:
        return WhatsAppService(db.session, acc.phone_number_id, acc.get_access_token())
    except Exception as exc:  # noqa: BLE001
        logger.warning("[jom] could not build WhatsAppService for ws %s: %s", workspace_id, exc)
        return None


def _body_components(variables):
    """Meta body component from ordered {"1": .., "2": ..} template variables."""
    ordered = [variables[k] for k in sorted(variables, key=lambda x: int(x))]
    return [{"type": "body", "parameters": [{"type": "text", "text": str(v)} for v in ordered]}]


def _template_vars(workspace_id, phone, status, stage):
    """Variables for a follow-up template: {{1}} name, {{2}} destination, {{3}} tip."""
    from . import memory, settings
    card = memory.get_card(workspace_id, phone)
    variables = {
        "1": card.get("customer_name") or "there",
        "2": card.get("destination") or "your trip",
    }
    _, uses_tip = settings.template_for(workspace_id, status, stage)
    if uses_tip:
        try:
            variables["3"] = cfg.DORMANT_MESSAGES[int(stage)]
        except (TypeError, ValueError, IndexError):
            variables["3"] = cfg.DORMANT_MESSAGES[0]
    return variables


def _make_drip_send_fn(workspace_id, svc):
    """send_fn(workspace_id, phone, status, stage) used by lifecycle.tick()."""
    def _send(ws, phone, status, stage):
        if svc is None:
            return
        open_window = window.can_send_free_text(ws, phone)
        if open_window:
            # Rare (they replied within 24h) but then plain text is warmer.
            try:
                svc.send_text(to=phone, text=cfg.followup_text(status, stage))
                return
            except Exception as exc:  # noqa: BLE001
                logger.warning("[jom] free-text follow-up failed %s: %s", phone, exc)
                return
        from . import settings
        tpl_name, _uses_tip = settings.template_for(ws, status, stage)
        variables = _template_vars(ws, phone, status, stage)
        try:
            svc.send_template(
                to=phone,
                template_name=tpl_name,
                components=_body_components(variables),
            )
            logger.info("[jom] follow-up template %s -> %s (stage %s)", tpl_name, phone, stage)
        except Exception as exc:  # noqa: BLE001
            # Almost always: template not approved yet. Log loudly — the drip is
            # useless until Meta approves these.
            logger.warning("[jom] TEMPLATE SEND FAILED (%s -> %s): %s — is it approved?",
                           tpl_name, phone, exc)
    return _send


def _make_continue_send_fn(svc):
    """send_fn(workspace_id, phone, template_name, variables) for the re-opener."""
    def _send(ws, phone, template_name, variables):
        if svc is None:
            return
        svc.send_template(
            to=phone,
            template_name=template_name,
            components=_body_components(variables),
        )
    return _send


def run_jom_ticks():
    """Sweep all JOM workspaces: due drip steps + 24h continue nudges."""
    results = {}
    for ws in allowed_workspace_ids():
        try:
            svc = _service_for(ws)
            summary = lifecycle.tick(ws, send_fn=_make_drip_send_fn(ws, svc))
            try:
                summary.update(window.run_continue_nudges(ws, send_fn=_make_continue_send_fn(svc)))
            except Exception as exc:  # noqa: BLE001
                logger.warning("[jom] continue-nudge sweep failed for ws %s: %s", ws, exc)
            results[str(ws)] = summary
        except Exception as exc:  # noqa: BLE001
            logger.exception("[jom] tick failed for ws %s: %s", ws, exc)
            results[str(ws)] = {"error": str(exc)}
    return results
