"""
Phase (go-live) — JOM scheduler: fires due Ghosted/Dormant follow-ups + auto-close.

Called from the existing periodic sweep (whatsapp/scheduler_routes.py::scheduler_tick).
For each JOM workspace it builds a real WhatsApp sender from that workspace's account and
runs lifecycle.tick(). Gated + fail-safe: does nothing for non-JOM workspaces, never raises
into the scheduler.

NOTE (WhatsApp 24h rule): free-text sends only work while the customer session is open.
Day-5/15 follow-ups fall OUTSIDE the 24h window and in production must be APPROVED TEMPLATE
messages. This MVP uses send_text (fine for in-session demo); swap to send_template with the
client's approved templates for go-live.
"""
import logging

from models import db
from .gate import allowed_workspace_ids
from . import config as cfg
from . import lifecycle

logger = logging.getLogger(__name__)


def _make_send_fn(workspace_id):
    """Build a sender bound to the workspace's WhatsApp account, or None if unavailable."""
    try:
        from whatsapp.models import WhatsAppAccount
        from whatsapp.services import WhatsAppService
    except Exception as exc:  # noqa: BLE001
        logger.warning("[jom] sender imports unavailable: %s", exc)
        return None
    acc = WhatsAppAccount.query.filter_by(workspace_id=str(workspace_id)).first()
    if not acc:
        logger.warning("[jom] no WhatsApp account for workspace %s — cannot send follow-ups", workspace_id)
        return None
    try:
        svc = WhatsAppService(db.session, acc.phone_number_id, acc.get_access_token())
    except Exception as exc:  # noqa: BLE001
        logger.warning("[jom] could not build WhatsAppService for ws %s: %s", workspace_id, exc)
        return None

    def _send(ws, phone, status, stage):
        text = cfg.followup_text(status, stage)
        try:
            svc.send_text(to=phone, text=text)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[jom] follow-up send_text failed %s: %s", phone, exc)

    return _send


def run_jom_ticks():
    """Sweep all JOM workspaces, firing due follow-ups. Returns a per-workspace summary."""
    results = {}
    for ws in allowed_workspace_ids():
        try:
            send_fn = _make_send_fn(ws)
            results[str(ws)] = lifecycle.tick(ws, send_fn=send_fn)
        except Exception as exc:  # noqa: BLE001
            logger.exception("[jom] tick failed for ws %s: %s", ws, exc)
            results[str(ws)] = {"error": str(exc)}
    return results
