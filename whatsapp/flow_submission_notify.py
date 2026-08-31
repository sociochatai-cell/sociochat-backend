"""
Send a WhatsApp message to the form owner when a form submission arrives,
if the flow has notify_owner_whatsapp enabled.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict

logger = logging.getLogger(__name__)


def send_flow_submission_whatsapp_notify_bg(
    account_id: int,
    message_id: int,
    submission: Dict[str, Any],
    flow_id_str: str = "",
) -> None:
    from .models import WhatsAppAccount, WhatsAppFlow, WhatsAppMessage

    try:
        account = WhatsAppAccount.query.get(account_id)
        if not account:
            return

        flow = _resolve_flow(account_id, flow_id_str)
        if not flow or not flow.notify_owner_whatsapp:
            return

        owner_phone = _resolve_owner_phone(account)
        if not owner_phone:
            logger.info(
                "[flow_notify] No owner phone for account %s — skipping WA notification",
                account_id,
            )
            return

        message = WhatsAppMessage.query.get(message_id)
        customer_phone = "Unknown"
        if message and message.conversation:
            customer_phone = message.conversation.user_phone or "Unknown"

        text = _format_notification(flow.name, customer_phone, submission)

        from .services import WhatsAppService
        svc = WhatsAppService(account_id=account.id)
        result = svc.send_text(to=owner_phone, text=text)

        if result.get("success"):
            logger.info(
                "[flow_notify] Sent form submission WA notification to %s for flow %s",
                owner_phone, flow.id,
            )
        else:
            logger.warning(
                "[flow_notify] WA notification send failed: %s",
                result.get("error") or result.get("message"),
            )
    except Exception:
        logger.exception("[flow_notify] Error sending WA self-notification")


def _resolve_flow(account_id: int, flow_id_str: str):
    from .models import WhatsAppFlow

    if not flow_id_str:
        return None
    flow = WhatsAppFlow.query.filter_by(
        account_id=account_id, meta_flow_id=str(flow_id_str)
    ).first()
    if not flow and str(flow_id_str).isdigit():
        flow = WhatsAppFlow.query.filter_by(
            account_id=account_id, id=int(flow_id_str)
        ).first()
    return flow


def _resolve_owner_phone(account):
    phone = (account.notification_phone_number or "").strip()
    if phone:
        return phone.lstrip("+")

    display = (account.display_phone_number or "").strip()
    if display:
        return "".join(c for c in display if c.isdigit())

    return None


def _format_notification(
    flow_name: str,
    customer_phone: str,
    submission: Dict[str, Any],
) -> str:
    now = datetime.now(timezone.utc).strftime("%d %b %Y, %I:%M %p UTC")
    lines = [
        f"New Form Submission",
        f"Form: {flow_name}",
        f"From: {customer_phone}",
        "",
    ]
    skip_keys = {"flow_token", "version", "action", "screen"}
    for key, val in submission.items():
        if key in skip_keys or str(key).startswith("__"):
            continue
        label = key.replace("_", " ").replace(".", " > ").title()
        lines.append(f"{label}: {val}")

    lines.append("")
    lines.append(f"Submitted at: {now}")
    return "\n".join(lines)
