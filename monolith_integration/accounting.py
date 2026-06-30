"""Apply ``message_sent`` usage events to existing billing tables (replay-safe via consumer dedupe)."""

from __future__ import annotations

from typing import Any, Dict

from shared_models import User, Workspace, db
from whatsapp.models import WhatsAppAccount

from .structured_log import mi_log


def default_message_sent_billing_handler(event: Dict[str, Any], payload: Dict[str, Any]) -> None:
    """
    Increment ``SubscriptionUsage`` for the workspace owner (legacy path).

    Idempotency is enforced by ``whatsapp_usage_events_processed`` in the consumer,
    not inside this function.
    """
    from subscription.service import record_message_sent

    account_id = int(payload.get("account_id") or event.get("account_id") or 0)
    if not account_id:
        raise ValueError("missing_account_id")

    acc = db.session.get(WhatsAppAccount, account_id)
    if not acc or not acc.workspace_id:
        mi_log("usage_billing_skip", reason="no_account_or_workspace", account_id=account_id)
        return

    try:
        wid_int = int(str(acc.workspace_id).strip())
    except (TypeError, ValueError):
        mi_log("usage_billing_skip", reason="invalid_workspace_id", account_id=account_id, workspace_id=acc.workspace_id)
        return

    ws = db.session.get(Workspace, wid_int)
    if not ws:
        mi_log("usage_billing_skip", reason="workspace_not_found", workspace_id=wid_int)
        return

    user = db.session.get(User, ws.user_id)
    if not user:
        mi_log("usage_billing_skip", reason="user_not_found", user_id=ws.user_id)
        return

    record_message_sent(user, workspace_id=wid_int, count=1, _commit=False)
    mi_log(
        "usage_billing_recorded",
        account_id=account_id,
        user_id=user.id,
        workspace_id=wid_int,
        message_id=payload.get("message_id"),
        wamid=payload.get("wamid"),
    )
