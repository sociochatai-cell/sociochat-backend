"""
Webhook payload classification for queue routing.

Status-only webhooks (sent/delivered/read) are handled inline in the API process.
Message webhooks are enqueued to the worker for automations / AI.
"""

from __future__ import annotations

from typing import Any, Dict, List, Tuple

# Fields that always need full worker processing
_HEAVY_FIELDS = frozenset({
    "message_echoes",
    "smb_message_echoes",
    "history_sync",
    "history",
    "smb_app_state_sync",
    "message_template_status_update",
    "message_template_quality_update",
    "template_category_update",
    "account_update",
})

_MESSAGES_FIELD = "messages"


def _change_has_inbound_work(change: Dict[str, Any]) -> bool:
    field = change.get("field", "")
    if field in _HEAVY_FIELDS:
        return True
    if field != _MESSAGES_FIELD:
        return False

    value = change.get("value") or {}
    if value.get("messages"):
        return True
    if value.get("errors"):
        return True
    if value.get("history"):
        return True
    if "smb_app_state_sync" in value:
        return True
    if value.get("contacts") and not value.get("messages") and not value.get("statuses"):
        return True
    return False


def _change_is_status_only(change: Dict[str, Any]) -> bool:
    field = change.get("field", "")
    if field != _MESSAGES_FIELD:
        return False
    value = change.get("value") or {}
    if not value.get("statuses"):
        return False
    return not _change_has_inbound_work(change)


def classify_webhook_payload(payload: Dict[str, Any]) -> str:
    """
    Classify a Meta WhatsApp webhook payload.

    Returns:
        status_only — only delivery/read receipts; safe to process inline
        message     — inbound messages / echoes / sync; enqueue to worker
        empty       — no actionable changes
    """
    if payload.get("object") != "whatsapp_business_account":
        return "message"

    entries = payload.get("entry") or []
    if not entries:
        return "empty"

    has_status = False
    has_heavy = False

    for entry in entries:
        for change in entry.get("changes") or []:
            if _change_has_inbound_work(change):
                has_heavy = True
            elif _change_is_status_only(change):
                has_status = True

    if has_heavy:
        return "message"
    if has_status:
        return "status_only"
    return "empty"


def split_status_and_message_payload(payload: Dict[str, Any]) -> Tuple[Dict[str, Any] | None, Dict[str, Any] | None]:
    """
    Split a mixed payload into (status_part, message_part).
    Either may be None when not applicable.
    """
    if payload.get("object") != "whatsapp_business_account":
        return None, payload

    status_entries: List[Dict[str, Any]] = []
    message_entries: List[Dict[str, Any]] = []

    for entry in payload.get("entry") or []:
        status_changes: List[Dict[str, Any]] = []
        message_changes: List[Dict[str, Any]] = []

        for change in entry.get("changes") or []:
            if _change_is_status_only(change):
                status_changes.append(change)
            elif _change_has_inbound_work(change):
                message_changes.append(change)

        if status_changes:
            status_entries.append({"id": entry.get("id"), "changes": status_changes})
        if message_changes:
            message_entries.append({"id": entry.get("id"), "changes": message_changes})

    status_payload = (
        {"object": "whatsapp_business_account", "entry": status_entries}
        if status_entries
        else None
    )
    message_payload = (
        {"object": "whatsapp_business_account", "entry": message_entries}
        if message_entries
        else None
    )
    return status_payload, message_payload
