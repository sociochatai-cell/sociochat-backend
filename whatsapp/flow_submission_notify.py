"""
Send a WhatsApp message to the form owner when a form submission arrives,
if the flow has notify_owner_whatsapp enabled.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict

logger = logging.getLogger(__name__)

FORM_NOTIFY_TEMPLATE = "form_submission_alert"
FALLBACK_TEMPLATE = "human_required"


def send_flow_submission_whatsapp_notify_bg(
    account_id: int,
    message_id: int,
    submission: Dict[str, Any],
    flow_id_str: str = "",
) -> None:
    from .models import WhatsAppAccount, WhatsAppFlow, WhatsAppMessage, WhatsAppTemplate

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
                "[flow_notify] No owner phone for account %s -- skipping",
                account_id,
            )
            return

        message = WhatsAppMessage.query.get(message_id)
        customer_phone = "Unknown"
        if message and message.conversation:
            customer_phone = message.conversation.user_phone or "Unknown"

        details_str = _format_submission_details(submission)
        now = datetime.now(timezone.utc).strftime("%d %b %Y, %I:%M %p UTC")

        tpl_name, components = _build_template_payload(
            account, flow, customer_phone, details_str, now,
        )
        if not tpl_name:
            logger.warning("[flow_notify] No approved template for account %s", account_id)
            return

        from .services import WhatsAppService
        svc = WhatsAppService(account_id=account.id)
        result = svc.send_template(
            to=owner_phone,
            template_name=tpl_name,
            language_code="en_US",
            components=components,
        )

        if result.get("success"):
            logger.info(
                "[flow_notify] Sent notification to %s for flow %s via template '%s'",
                owner_phone, flow.id, tpl_name,
            )
        else:
            logger.warning(
                "[flow_notify] Notification failed: %s",
                result.get("error") or result.get("message"),
            )
    except Exception:
        logger.exception("[flow_notify] Error sending WA self-notification")


def _build_template_payload(account, flow, customer_phone, details_str, timestamp):
    from .models import WhatsAppTemplate

    primary = WhatsAppTemplate.query.filter_by(
        account_id=account.id, name=FORM_NOTIFY_TEMPLATE, status="APPROVED",
    ).first()
    if primary:
        return FORM_NOTIFY_TEMPLATE, [{
            "type": "body",
            "parameters": [
                {"type": "text", "text": flow.name},
                {"type": "text", "text": customer_phone},
                {"type": "text", "text": details_str},
                {"type": "text", "text": timestamp},
            ],
        }]

    fallback = WhatsAppTemplate.query.filter_by(
        account_id=account.id, name=FALLBACK_TEMPLATE, status="APPROVED",
    ).first()
    if fallback:
        return FALLBACK_TEMPLATE, [{
            "type": "body",
            "named_params": {
                "user_name": customer_phone,
                "user_phone": customer_phone,
                "user_query": f"Form: {flow.name} | {details_str}",
                "timestamp": timestamp,
            },
        }]

    return None, None


def _resolve_flow(account_id: int, flow_id_str: str):
    from .models import WhatsAppFlow

    if flow_id_str:
        flow = WhatsAppFlow.query.filter_by(
            account_id=account_id, meta_flow_id=str(flow_id_str)
        ).first()
        if not flow and str(flow_id_str).isdigit():
            flow = WhatsAppFlow.query.filter_by(
                account_id=account_id, id=int(flow_id_str)
            ).first()
        if flow:
            return flow

    return WhatsAppFlow.query.filter_by(
        account_id=account_id, notify_owner_whatsapp=True
    ).first()


def _resolve_owner_phone(account):
    phone = (account.notification_phone_number or "").strip()
    if phone:
        digits = "".join(c for c in phone if c.isdigit())
        return digits if digits else None

    return None


def _format_submission_details(submission: Dict[str, Any]) -> str:
    skip_keys = {"flow_token", "version", "action", "screen"}
    lines = []
    for key, val in submission.items():
        if key in skip_keys or str(key).startswith("__"):
            continue
        label = key.replace("_", " ").replace(".", " > ").title()
        lines.append(f"{label}: {val}")
    return " | ".join(lines) if lines else "No fields submitted"
