"""
Send push notifications to a user's mobile devices via Expo's Push service.

Delivery model (updated):
  - A message arrives for a WhatsApp account -> we notify the OWNER of that
    account's workspace, on ALL their registered devices (so the owner gets
    notifications for EVERY workspace they own, not just the selected one).
  - COEXISTENCE accounts default to OFF (the owner already gets notifications
    from the real WhatsApp app on their phone; a second push would duplicate).
  - An explicit PushPref row (the Settings toggle) overrides the default.

Everything here is BEST-EFFORT and fully guarded: any failure is caught and
logged, and it never raises into the caller (the webhook).
"""

import logging
import requests

from models import db, Workspace
from push.models import PushDevice, PushPref

logger = logging.getLogger(__name__)

EXPO_PUSH_URL = "https://exp.host/--/api/v2/push/send"


def _notifications_enabled(user_id: int, workspace_id: int, is_coexistence: bool) -> bool:
    """Explicit PushPref wins; otherwise coexistence -> OFF, else ON."""
    try:
        pref = PushPref.query.filter_by(user_id=user_id, workspace_id=workspace_id).first()
        if pref is not None:
            return bool(pref.enabled)
    except Exception:
        pass
    return not is_coexistence  # default: OFF for coexistence, ON otherwise


def _send_to_tokens(tokens, title, body, data):
    if not tokens:
        return 0
    messages = [
        {
            "to": t,
            "title": title,
            "body": body,
            "sound": "default",
            "data": data or {},
            "channelId": "default",
        }
        for t in tokens
    ]
    resp = requests.post(
        EXPO_PUSH_URL,
        json=messages,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        timeout=8,
    )
    if resp.status_code >= 400:
        logger.warning("Expo push non-200: %s %s", resp.status_code, resp.text[:300])
    return len(tokens)


def send_push_for_message(account, sender_name: str, body: str, conversation_id=None) -> int:
    """
    Notify the owner of `account`'s workspace about a new inbound message.
    Returns number of devices notified (0 on any skip/problem). Never raises.
    """
    try:
        if account is None or account.workspace_id is None:
            return 0

        ws = Workspace.query.get(account.workspace_id)
        if ws is None or ws.user_id is None:
            return 0

        owner_id = ws.user_id
        is_coex = bool(getattr(account, "is_coexistence", False))

        # Respect coexistence default + the user's Settings toggle
        if not _notifications_enabled(owner_id, account.workspace_id, is_coex):
            return 0

        # Notify ALL of the owner's devices (any workspace they registered under)
        devices = PushDevice.query.filter_by(user_id=owner_id).all()
        tokens = [d.expo_token for d in devices if d.expo_token]
        if not tokens:
            return 0

        ws_name = (ws.business_name or "").strip()
        title = f"{ws_name} · {sender_name}" if ws_name else sender_name
        data = {"type": "whatsapp_message", "workspace_id": account.workspace_id}
        if conversation_id is not None:
            data["conversation_id"] = conversation_id

        return _send_to_tokens(tokens, title, body, data)
    except Exception as e:  # noqa: BLE001 - best effort, never break the caller
        logger.warning("send_push_for_message failed: %s", e)
        return 0


# Backwards-compat shim (older callers). Notifies every device registered for a
# workspace, no ownership/coexistence logic. Prefer send_push_for_message.
def send_push_to_workspace(workspace_id, title: str, body: str, data: dict | None = None) -> int:
    try:
        if workspace_id is None:
            return 0
        devices = PushDevice.query.filter_by(workspace_id=workspace_id).all()
        tokens = [d.expo_token for d in devices if d.expo_token]
        return _send_to_tokens(tokens, title, body, data)
    except Exception as e:  # noqa: BLE001
        logger.warning("send_push_to_workspace failed: %s", e)
        return 0
