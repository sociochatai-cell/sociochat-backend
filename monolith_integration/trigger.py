"""
Async (non-blocking) capability projection sync to WhatsApp internal API.

Uses a background thread + Flask app context. Safe to call from billing / OAuth paths.
"""

from __future__ import annotations

import logging
import threading
from typing import Iterable, List

from .structured_log import mi_log

logger = logging.getLogger(__name__)


def _iter_account_ids_for_user(user_id: int) -> List[int]:
    from shared_models import Workspace
    from whatsapp.models import WhatsAppAccount

    ids: List[int] = []
    workspaces = Workspace.query.filter_by(user_id=user_id).all()
    for ws in workspaces:
        wid = str(ws.id)
        for acc in WhatsAppAccount.query.filter(WhatsAppAccount.workspace_id == wid).all():
            ids.append(acc.id)
    return ids


def _run_capability_sync_user(user_id: int, reason: str) -> None:
    from runtime import app
    from shared_models import User, db
    from monolith_integration.capability_client import WhatsappCapabilitiesClient
    from monolith_integration.projection_builder import build_capability_projection_body

    with app.app_context():
        user = db.session.get(User, user_id)
        if not user:
            logger.warning("[whatsapp_integration] skip capability sync: user %s not found", user_id)
            return
        body = build_capability_projection_body(user)
        client = WhatsappCapabilitiesClient.from_env()
        if not client.is_configured():
            logger.debug("[whatsapp_integration] capability client not configured; skip")
            return
        account_ids = _iter_account_ids_for_user(user_id)
        if not account_ids:
            logger.debug("[whatsapp_integration] no whatsapp accounts for user %s", user_id)
            return
        for aid in account_ids:
            ok = client.push_account(aid, body)
            mi_log(
                "capability_sync_account",
                reason=reason,
                user_id=user_id,
                account_id=aid,
                ok=ok,
            )


def schedule_capabilities_resync_for_user(user_id: int, reason: str = "user_trigger") -> None:
    """Fire-and-forget: push plan-derived projection for all WhatsApp accounts owned by this user."""

    def _target() -> None:
        try:
            _run_capability_sync_user(int(user_id), reason)
        except Exception as exc:
            logger.warning("[whatsapp_integration] capability sync failed user=%s: %s", user_id, exc)

    threading.Thread(target=_target, name="wa-cap-sync", daemon=True).start()


def schedule_capabilities_resync_for_workspace(workspace_id: str, reason: str = "workspace_trigger") -> None:
    """Resolve workspace owner and run user-level projection push for their accounts."""

    def _target() -> None:
        try:
            from runtime import app
            from shared_models import Workspace, db

            with app.app_context():
                try:
                    wid = int(str(workspace_id).strip())
                except ValueError:
                    return
                ws = db.session.get(Workspace, wid)
                if not ws:
                    return
                _run_capability_sync_user(int(ws.user_id), reason)
        except Exception as exc:
            logger.warning("[whatsapp_integration] capability sync failed workspace=%s: %s", workspace_id, exc)

    threading.Thread(target=_target, name="wa-cap-sync-ws", daemon=True).start()


def schedule_capabilities_resync_for_accounts(account_ids: Iterable[int], user_id: int, reason: str) -> None:
    """Push the same projection body to explicit account ids (e.g. single reconnect)."""

    def _target() -> None:
        try:
            from runtime import app
            from shared_models import User, db
            from monolith_integration.capability_client import WhatsappCapabilitiesClient
            from monolith_integration.projection_builder import build_capability_projection_body

            with app.app_context():
                user = db.session.get(User, int(user_id))
                if not user:
                    return
                body = build_capability_projection_body(user)
                client = WhatsappCapabilitiesClient.from_env()
                if not client.is_configured():
                    return
                for aid in account_ids:
                    ok = client.push_account(int(aid), body)
                    mi_log(
                        "capability_sync_account",
                        reason=reason,
                        user_id=int(user_id),
                        account_id=int(aid),
                        ok=ok,
                    )
        except Exception as exc:
            logger.warning("[whatsapp_integration] capability sync accounts failed: %s", exc)

    threading.Thread(target=_target, name="wa-cap-sync-accs", daemon=True).start()
