"""
Human escalation when AI cannot answer confidently.

Triggers:
  - RAG confidence below threshold (when WHATSAPP_AI_ESCALATE_ON_LOW_RAG is enabled)
  - AI generation failure / account AI disabled (fallback path)
  - AI reply indicates missing knowledge ("I don't have that information", etc.)

Actions:
  - Send standard handoff message to the customer on WhatsApp
  - Mark conversation human_required + needs_attention (stored in attribution_data)
  - Email workspace notification address (notification_email or ADMIN_EMAILS)
  - Broadcast inbox SSE update
"""

from __future__ import annotations

import logging
import os
import re
import smtplib
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any, Dict, Optional, Tuple

from sqlalchemy.orm.attributes import flag_modified

from notifications import notification_manager
from shared_models import db, Workspace, User
from .trace_debug import trace_event

logger = logging.getLogger(__name__)

HANDOFF_CUSTOMER_MESSAGE = (
    "I don't have that information on this, we are arranging call from our Team. "
    "Please let us know your convenient time to discuss on call."
)

_NO_KNOWLEDGE_PATTERNS = (
    r"don'?t have (?:that|such|this|any|the)?\s*information",
    r"do not have (?:that|such|this|any|the)?\s*information",
    r"i don'?t have (?:that|such|this|any|the)?\s*information",
    r"not in (?:my|the|our) knowledge",
    r"not in the knowledge base",
    r"cannot find (?:that|this|any)",
    r"can'?t find (?:that|this|any)",
    r"no information (?:about|on|regarding)",
    r"unable to find (?:that|this|any)",
)


def _escalation_debug_enabled() -> bool:
    return os.getenv("WHATSAPP_ESCALATION_DEBUG", "true").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def handoff_customer_message() -> str:
    custom = (os.getenv("WHATSAPP_HANDOFF_CUSTOMER_MESSAGE") or "").strip()
    return custom or HANDOFF_CUSTOMER_MESSAGE


def _escalate_on_low_rag() -> bool:
    # Default OFF: low-RAG should not automatically override AI replies with handoff.
    # Enable explicitly in environments that want aggressive human takeover.
    return os.getenv("WHATSAPP_AI_ESCALATE_ON_LOW_RAG", "false").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _escalate_on_no_knowledge_reply() -> bool:
    return os.getenv("WHATSAPP_AI_ESCALATE_ON_NO_KNOWLEDGE", "false").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def message_indicates_no_knowledge(text: str) -> bool:
    normalized = (text or "").strip().lower()
    if not normalized:
        return False
    for pattern in _NO_KNOWLEDGE_PATTERNS:
        if re.search(pattern, normalized):
            return True
    return False


def should_escalate_to_human(
    *,
    success: bool,
    reply_text: str,
    low_rag_confidence: bool = False,
    has_rag_context: bool = False,
    prep_run_failed: bool = False,
) -> Tuple[bool, str]:
    """Return (escalate, reason_code)."""
    if _escalation_debug_enabled():
        logger.info(
            "[human_escalation] evaluate success=%s prep_run_failed=%s low_rag_confidence=%s "
            "has_rag_context=%s no_knowledge_reply=%s",
            success,
            prep_run_failed,
            low_rag_confidence,
            has_rag_context,
            message_indicates_no_knowledge(reply_text),
        )
    if prep_run_failed:
        if _escalation_debug_enabled():
            logger.info("[human_escalation] decision escalate=True reason=ai_disabled_or_fallback")
        return True, "ai_disabled_or_fallback"
    if not success:
        if _escalation_debug_enabled():
            logger.info("[human_escalation] decision escalate=True reason=ai_generation_failed")
        return True, "ai_generation_failed"
    if low_rag_confidence and _escalate_on_low_rag():
        # Only auto-escalate when retrieval truly found nothing useful
        if _escalation_debug_enabled():
            logger.info("[human_escalation] decision escalate=True reason=low_rag_confidence")
        return True, "low_rag_confidence"
    # Avoid false handoffs when we actually have KB context but the model answers cautiously.
    if (
        message_indicates_no_knowledge(reply_text)
        and _escalate_on_no_knowledge_reply()
        and not has_rag_context
    ):
        if _escalation_debug_enabled():
            logger.info("[human_escalation] decision escalate=True reason=no_knowledge_in_reply")
        return True, "no_knowledge_in_reply"
    if _escalation_debug_enabled():
        logger.info("[human_escalation] decision escalate=False reason=")
    return False, ""


def _resolve_notification_emails(account) -> list[str]:
    """Resolve recipients using Priority 1 (settings) and Priority 2 (workspace creator) fallback."""
    return resolve_template_notification_emails(account)


def _send_escalation_email(
    account,
    conversation,
    reason: str,
    customer_message_preview: str = "",
) -> None:
    recipients = _resolve_notification_emails(account)
    if not recipients:
        logger.warning(
            "[human_escalation] No notification email configured for account %s",
            getattr(account, "id", None),
        )
        return

    smtp_host = os.getenv("SMTP_HOST", "smtp.gmail.com")
    smtp_port = int(os.getenv("SMTP_PORT", "587"))
    smtp_user = os.getenv("SMTP_USER", "")
    smtp_pass = os.getenv("SMTP_PASS", "")
    mail_from = os.getenv("MAIL_FROM", smtp_user or "noreply@sociovia.com")

    if not smtp_user or not smtp_pass:
        logger.warning("[human_escalation] SMTP not configured — skipping escalation email")
        return

    business = (
        getattr(account, "custom_name", None)
        or getattr(account, "verified_name", None)
        or getattr(account, "display_phone_number", None)
        or f"Account {account.id}"
    )
    subject = f"[Sociovia] Human required — {business}"
    body = (
        f"A WhatsApp conversation needs a human agent.\n\n"
        f"Reason: {reason}\n"
        f"Workspace: {getattr(account, 'workspace_id', '')}\n"
        f"Account ID: {account.id}\n"
        f"Conversation ID: {conversation.id}\n"
        f"Customer phone: {conversation.user_phone}\n"
        f"Customer name: {conversation.user_name or 'Unknown'}\n"
    )
    if customer_message_preview:
        body += f"\nLast customer message:\n{customer_message_preview[:500]}\n"
    body += "\nOpen the inbox → Human Required filter to respond.\n"

    msg = MIMEMultipart()
    msg["From"] = mail_from
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain"))

    try:
        with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as server:
            server.starttls()
            server.login(smtp_user, smtp_pass)
            server.sendmail(mail_from, recipients, msg.as_string())
        logger.info(
            "[human_escalation] Escalation email sent to %s (conv=%s reason=%s)",
            recipients,
            conversation.id,
            reason,
        )
    except Exception as e:
        logger.exception("[human_escalation] Failed to send escalation email: %s", e)


def resolve_template_notification_emails(account) -> list[str]:
    """
    Resolve client notification emails based on priority fallback:
    1. WhatsAppAccount.notification_email
    2. Workspace.owner.email (via account.workspace_id)
    3. User.email (via account.connected_by_user_id)
    4. Default admin/notification emails from environment
    """
    emails: list[str] = []

    # 1. Direct notification email on account
    direct = (getattr(account, "notification_email", None) or "").strip()
    if direct:
        for part in re.split(r"[,;]", direct):
            addr = part.strip()
            if addr and "@" in addr:
                emails.append(addr)

    # 2. Workspace owner email
    if not emails:
        workspace_id_str = getattr(account, "workspace_id", None)
        if workspace_id_str:
            try:
                workspace_id_int = int(workspace_id_str)
                workspace = Workspace.query.get(workspace_id_int)
                if workspace and workspace.owner and workspace.owner.email:
                    email = workspace.owner.email.strip()
                    if email and "@" in email:
                        emails.append(email)
            except Exception as e:
                logger.warning(
                    f"[human_escalation] Failed to resolve workspace owner email for workspace_id={workspace_id_str}: {e}"
                )

    # 3. Connected by user email
    if not emails:
        connected_user_str = getattr(account, "connected_by_user_id", None)
        if connected_user_str:
            try:
                user_id_int = int(connected_user_str)
                user = User.query.get(user_id_int)
                if user and user.email:
                    email = user.email.strip()
                    if email and "@" in email:
                        emails.append(email)
            except Exception as e:
                logger.warning(
                    f"[human_escalation] Failed to resolve connected user email for user_id={connected_user_str}: {e}"
                )

    # 4. Default fallback from environment variables
    if not emails:
        raw = (
            os.getenv("WHATSAPP_NOTIFICATION_EMAIL")
            or os.getenv("ADMIN_EMAILS")
            or os.getenv("DEFAULT_ADMIN_EMAIL")
            or ""
        )
        for part in re.split(r"[,;]", raw):
            addr = part.strip()
            if addr and "@" in addr:
                emails.append(addr)

    # De-dupe preserving order
    seen = set()
    unique: list[str] = []
    for addr in emails:
        key = addr.lower()
        if key in seen:
            continue
        seen.add(key)
        unique.append(addr)
    return unique


def send_template_status_notification_email(
    account,
    template,
    old_status: str,
    new_status: str,
    reason: str | None = None,
) -> None:
    """
    Send an email notification to the client regarding a WhatsApp template status update from Meta.
    """
    recipients = resolve_template_notification_emails(account)
    if not recipients:
        logger.warning(
            "[human_escalation] No notification email resolved for template status update on account %s",
            getattr(account, "id", None),
        )
        return

    smtp_host = os.getenv("SMTP_HOST", "smtp.gmail.com")
    smtp_port = int(os.getenv("SMTP_PORT", "587"))
    smtp_user = os.getenv("SMTP_USER", "")
    smtp_pass = os.getenv("SMTP_PASS", "")
    mail_from = os.getenv("MAIL_FROM", smtp_user or "noreply@sociovia.com")

    if not smtp_user or not smtp_pass:
        logger.warning("[human_escalation] SMTP not configured — skipping template status update email")
        return

    business = (
        getattr(account, "custom_name", None)
        or getattr(account, "verified_name", None)
        or getattr(account, "display_phone_number", None)
        or f"Account {account.id}"
    )

    subject = f"[Sociovia] WhatsApp Template Status Updated — {template.name} is {new_status}"
    
    # Render body
    body = (
        f"Hello,\n\n"
        f"Your WhatsApp Message Template has been updated by Meta.\n\n"
        f"Template Details:\n"
        f"-----------------\n"
        f"Name: {template.name}\n"
        f"Language: {template.language}\n"
        f"Category: {template.category}\n"
        f"Previous Status: {old_status}\n"
        f"New Status: {new_status}\n"
    )
    if reason:
        body += f"Reason / Details: {reason}\n"
        
    body += (
        f"\nWhatsApp Account: {business}\n"
        f"Workspace: {getattr(account, 'workspace_id', '')}\n\n"
        f"Best regards,\n"
        f"The Sociovia Team\n"
    )

    msg = MIMEMultipart()
    msg["From"] = mail_from
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain"))

    try:
        with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as server:
            server.starttls()
            server.login(smtp_user, smtp_pass)
            server.sendmail(mail_from, recipients, msg.as_string())
        logger.info(
            "[human_escalation] Template status update email sent to %s (template=%s status=%s -> %s)",
            recipients,
            template.name,
            old_status,
            new_status,
        )
    except Exception as e:
        logger.exception("[human_escalation] Failed to send template status email: %s", e)


def send_template_status_notification_email_bg(
    account_id: int,
    template_id: int,
    old_status: str,
    new_status: str,
    reason: str | None = None,
) -> None:
    """
    Background wrapper to send the template status update email.
    Queries the database inside the worker thread context to prevent DetachedInstanceError.
    """
    from .models import WhatsAppAccount, WhatsAppTemplate
    try:
        account = WhatsAppAccount.query.get(account_id)
        template = WhatsAppTemplate.query.get(template_id)
        if not account or not template:
            logger.error(
                f"[human_escalation] Background template status notification skipped: "
                f"account_id={account_id} exists={account is not None}, "
                f"template_id={template_id} exists={template is not None}"
            )
            return
        
        send_template_status_notification_email(
            account=account,
            template=template,
            old_status=old_status,
            new_status=new_status,
            reason=reason,
        )
    except Exception as exc:
        logger.exception(f"[human_escalation] Failed to process template status background task: {exc}")


def send_flow_submission_notification_email_bg(
    account_id: int,
    message_id: int,
    submission: Dict[str, Any],
) -> None:
    """Send Flow Form Submission Email in background context."""
    from .models import WhatsAppAccount, WhatsAppMessage
    from typing import Dict, Any
    try:
        account = WhatsAppAccount.query.get(account_id)
        message = WhatsAppMessage.query.get(message_id)
        if not account or not message:
            logger.error(f"[human_escalation] Background Flow submission skipped: account_id={account_id}, message_id={message_id}")
            return
            
        recipients = resolve_template_notification_emails(account)
        if not recipients:
            logger.warning(f"[human_escalation] No notification email resolved for Flow submission on account {account_id}")
            return

        smtp_host = os.getenv("SMTP_HOST", "smtp.gmail.com")
        smtp_port = int(os.getenv("SMTP_PORT", "587"))
        smtp_user = os.getenv("SMTP_USER", "")
        smtp_pass = os.getenv("SMTP_PASS", "")
        mail_from = os.getenv("MAIL_FROM", smtp_user or "noreply@sociovia.com")

        if not smtp_user or not smtp_pass:
            logger.warning("[human_escalation] SMTP not configured — skipping Flow submission email")
            return

        business = account.custom_name or account.verified_name or account.display_phone_number or f"Account {account.id}"
        subject = f"[Sociovia] WhatsApp Flow Form Submitted — {business}"
        
        body = (
            f"Hello,\n\n"
            f"A WhatsApp Flow form submission has been successfully received.\n\n"
            f"Submission Details:\n"
            f"-------------------\n"
            f"Workspace: {account.workspace_id}\n"
            f"Customer Phone: {message.conversation.user_phone if message.conversation else 'Unknown'}\n"
            f"Customer Name: {message.conversation.user_name if message.conversation else 'Unknown'}\n"
            f"Submitted At: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}\n\n"
            f"Form Fields:\n"
        )
        for key, val in submission.items():
            body += f"- {key}: {val}\n"
            
        body += (
            f"\nWhatsApp Account: {business}\n"
            f"Workspace ID: {account.workspace_id}\n\n"
            f"Best regards,\n"
            f"The Sociovia Team\n"
        )

        msg = MIMEMultipart()
        msg["From"] = mail_from
        msg["To"] = ", ".join(recipients)
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain"))

        with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as server:
            server.starttls()
            server.login(smtp_user, smtp_pass)
            server.sendmail(mail_from, recipients, msg.as_string())
            
        logger.info(f"[human_escalation] Flow submission email sent to {recipients} for message {message_id}")
    except Exception as exc:
        logger.exception(f"[human_escalation] Failed to process Flow submission background task: {exc}")


def send_cart_notification_email_bg(
    account_id: int,
    conversation_id: int,
    message_id: int,
    order_data: Dict[str, Any],
) -> None:
    """Send Catalog Cart Submitted Email in background context."""
    from .models import WhatsAppAccount, WhatsAppConversation, WhatsAppMessage
    from typing import Dict, Any
    try:
        print(f"[human_escalation] cart-email invoked account_id={account_id} conversation_id={conversation_id} message_id={message_id}")
        account = WhatsAppAccount.query.get(account_id)
        conversation = WhatsAppConversation.query.get(conversation_id)
        message = WhatsAppMessage.query.get(message_id)
        if not account or not conversation or not message:
            logger.error(f"[human_escalation] Background Catalog Cart skipped: account_id={account_id}, conversation_id={conversation_id}")
            return

        # Idempotency guard: avoid duplicate catalog cart emails.
        existing_content = message.content if isinstance(message.content, dict) else {}
        if existing_content.get("cart_email_sent") is True:
            logger.info(
                "[human_escalation] Catalog Cart email already sent for message %s — skipping",
                message_id,
            )
            return

        recipients = resolve_template_notification_emails(account)
        # Extra commerce notification recipients (SocioChat Payments settings; removable).
        try:
            from whatsapp.commerce_pay.models import WorkspacePaymentConfig
            _cfg = WorkspacePaymentConfig.query.filter_by(workspace_id=int(account.workspace_id)).first()
            if _cfg:
                for _e in _cfg.notify_emails_list():
                    if _e not in recipients:
                        recipients.append(_e)
        except Exception as _ce:
            logger.debug(f"[human_escalation] commerce notify_emails merge skipped: {_ce}")
        print(f"[human_escalation] cart-email resolved recipients={recipients}")
        if not recipients:
            logger.warning(f"[human_escalation] No notification email resolved for Catalog Cart on account {account_id}")
            return

        smtp_host = os.getenv("SMTP_HOST", "smtp.gmail.com")
        smtp_port = int(os.getenv("SMTP_PORT", "587"))
        smtp_user = os.getenv("SMTP_USER", "")
        smtp_pass = os.getenv("SMTP_PASS", "")
        mail_from = os.getenv("MAIL_FROM", smtp_user or "noreply@sociovia.com")
        print(
            "[human_escalation] cart-email smtp-check "
            f"user_set={bool(smtp_user)} pass_set={bool(smtp_pass)} host={smtp_host} from={mail_from}"
        )

        if not smtp_user or not smtp_pass:
            logger.warning("[human_escalation] SMTP not configured — skipping Catalog Cart email")
            return

        business = account.custom_name or account.verified_name or account.display_phone_number or f"Account {account.id}"
        subject = f"[Sociovia] WhatsApp Catalog Cart Received — {business}"
        
        body = (
            f"Hello,\n\n"
            f"A customer has added items to a catalog cart and submitted it on WhatsApp.\n\n"
            f"Cart & Customer Details:\n"
            f"-------------------------\n"
            f"Customer Phone: {conversation.user_phone}\n"
            f"Customer Name: {conversation.user_name or 'Unknown'}\n"
            f"Catalog ID: {order_data.get('catalog_id') or 'N/A'}\n"
            f"Submitted At: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}\n\n"
            f"Cart Items:\n"
        )
        items = order_data.get("product_items", [])
        for idx, item in enumerate(items):
            price_str = f" @ {item.get('currency', 'INR')} {item.get('item_price')}" if item.get('item_price') else ""
            item_name = item.get("name") or item.get("title") or "N/A"
            item_image = item.get("image_url") or item.get("image") or ""
            body += (
                f"{idx + 1}. "
                f"Name: {item_name}\n"
                f"   SKU: {item.get('product_retailer_id')}\n"
                f"   Qty: {item.get('quantity')}{price_str}\n"
            )
            if item_image:
                body += f"   Image: {item_image}\n"
            
        if order_data.get("text"):
            body += f"\nCustomer message:\n\"{order_data.get('text')}\"\n"
            
        body += (
            f"\nWhatsApp Account: {business}\n"
            f"Workspace ID: {account.workspace_id}\n\n"
            f"Best regards,\n"
            f"The Sociovia Team\n"
        )

        msg = MIMEMultipart()
        msg["From"] = mail_from
        msg["To"] = ", ".join(recipients)
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain"))

        with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as server:
            server.starttls()
            server.login(smtp_user, smtp_pass)
            server.sendmail(mail_from, recipients, msg.as_string())

        try:
            updated_content = dict(existing_content)
            updated_content["cart_email_sent"] = True
            updated_content["cart_email_sent_at"] = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
            message.content = updated_content
            db.session.commit()
        except Exception as mark_err:
            db.session.rollback()
            logger.warning("[human_escalation] Failed to mark cart email as sent for message %s: %s", message_id, mark_err)
            
        logger.info(f"[human_escalation] Catalog Cart email sent to {recipients} for conversation {conversation_id}")
    except Exception as exc:
        logger.exception(f"[human_escalation] Failed to process Catalog Cart background task: {exc}")


def send_booking_notification_email_bg(
    account_id: int,
    conversation_id: int,
    message_id: int,
    trigger_text: str,
) -> None:
    """Send Meeting/Call booking intent notification email."""
    from .models import WhatsAppAccount, WhatsAppConversation, WhatsAppMessage
    try:
        account = WhatsAppAccount.query.get(account_id)
        conversation = WhatsAppConversation.query.get(conversation_id)
        message = WhatsAppMessage.query.get(message_id)
        if not account or not conversation or not message:
            logger.error(f"[human_escalation] Background Booking skipped: account_id={account_id}, conversation_id={conversation_id}")
            return

        recipients = resolve_template_notification_emails(account)
        if not recipients:
            logger.warning(f"[human_escalation] No notification email resolved for Booking trigger on account {account_id}")
            return

        smtp_host = os.getenv("SMTP_HOST", "smtp.gmail.com")
        smtp_port = int(os.getenv("SMTP_PORT", "587"))
        smtp_user = os.getenv("SMTP_USER", "")
        smtp_pass = os.getenv("SMTP_PASS", "")
        mail_from = os.getenv("MAIL_FROM", smtp_user or "noreply@sociovia.com")

        if not smtp_user or not smtp_pass:
            logger.warning("[human_escalation] SMTP not configured — skipping Booking email")
            return

        business = account.custom_name or account.verified_name or account.display_phone_number or f"Account {account.id}"
        subject = f"[Sociovia] Meeting/Call Booking Requested — {business}"
        
        body = (
            f"Hello,\n\n"
            f"A customer wants to book a meeting or call with your team.\n\n"
            f"Customer Details:\n"
            f"-----------------\n"
            f"Customer Phone: {conversation.user_phone}\n"
            f"Customer Name: {conversation.user_name or 'Unknown'}\n"
            f"Requested At: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}\n\n"
            f"Customer Message:\n"
            f"\"{trigger_text}\"\n\n"
            f"Please contact them immediately on WhatsApp or call to schedule a convenient time.\n\n"
            f"WhatsApp Account: {business}\n"
            f"Workspace ID: {account.workspace_id}\n\n"
            f"Best regards,\n"
            f"The Sociovia Team\n"
        )

        msg = MIMEMultipart()
        msg["From"] = mail_from
        msg["To"] = ", ".join(recipients)
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain"))

        with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as server:
            server.starttls()
            server.login(smtp_user, smtp_pass)
            server.sendmail(mail_from, recipients, msg.as_string())
            
        logger.info(f"[human_escalation] Booking intent email sent to {recipients} for conversation {conversation_id}")
    except Exception as exc:
        logger.exception(f"[human_escalation] Failed to process Booking background task: {exc}")


def send_button_click_notification_email_bg(
    account_id: int,
    conversation_id: Optional[int],
    button_label: str,
    phone_number: Optional[str],
    user_name: Optional[str],
    collected_fields: Optional[Dict[str, Any]] = None,
    flow_name: Optional[str] = None,
) -> None:
    """Send a per-button-click notification email when a flow button flagged
    with `notifyEmail: true` is clicked by a customer. Recipient is the
    account's configured `notification_email` (same field used by other
    workspace notifications). No-op if SMTP or notification_email is unset."""
    from .models import WhatsAppAccount
    try:
        account = WhatsAppAccount.query.get(account_id)
        if not account:
            logger.error(f"[button_notify] account {account_id} not found — skipping")
            return

        recipients = resolve_template_notification_emails(account)
        if not recipients:
            logger.info(f"[button_notify] no notification_email for account {account_id} — skipping")
            return

        smtp_host = os.getenv("SMTP_HOST", "smtp.gmail.com")
        smtp_port = int(os.getenv("SMTP_PORT", "587"))
        smtp_user = os.getenv("SMTP_USER", "")
        smtp_pass = os.getenv("SMTP_PASS", "")
        mail_from = os.getenv("MAIL_FROM", smtp_user or "noreply@sociovia.com")

        if not smtp_user or not smtp_pass:
            logger.warning("[button_notify] SMTP not configured — skipping")
            return

        business = (
            account.custom_name
            or account.verified_name
            or account.display_phone_number
            or f"Account {account.id}"
        )

        label_clean = (button_label or "action").strip()
        name_clean = (user_name or "").strip() or "Customer"
        phone_clean = (phone_number or "").strip() or "unknown number"
        label_lower = label_clean.lower()

        # Choose a natural sentence based on button label keywords
        if any(k in label_lower for k in ("call", "callback", "call me")):
            action_line = f"A call has been requested by {name_clean} ({phone_clean})."
        elif any(k in label_lower for k in ("meeting", "schedule", "book", "appointment")):
            action_line = f"A meeting has been scheduled by {name_clean} ({phone_clean})."
        elif any(k in label_lower for k in ("message", "chat", "talk", "counsellor", "counselor")):
            action_line = f"{name_clean} ({phone_clean}) wants to talk to your team."
        elif any(k in label_lower for k in ("visit", "website", "url", "link")):
            action_line = f"{name_clean} ({phone_clean}) clicked \"{label_clean}\"."
        elif any(k in label_lower for k in ("interest", "not interested")):
            action_line = f"{name_clean} ({phone_clean}) responded: \"{label_clean}\"."
        elif any(k in label_lower for k in ("demo", "trial")):
            action_line = f"A demo has been requested by {name_clean} ({phone_clean})."
        else:
            action_line = f"{name_clean} ({phone_clean}) clicked \"{label_clean}\"."

        subject = f"[SocioChat] {label_clean} — {name_clean} — {business}"

        body_lines = [
            f"Hello,",
            "",
            action_line,
            "",
            f"Button clicked: {label_clean}",
            f"Customer name : {name_clean}",
            f"Phone         : {phone_clean}",
        ]
        if flow_name:
            body_lines.append(f"Flow          : {flow_name}")
        body_lines.append(
            f"Time          : {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}"
        )

        # Include collected fields (name, email, city, course, etc.) if present
        if isinstance(collected_fields, dict) and collected_fields:
            interesting = {
                k: v for k, v in collected_fields.items()
                if v not in (None, "", []) and not str(k).startswith("_")
            }
            if interesting:
                body_lines.append("")
                body_lines.append("Additional details collected:")
                for k, v in interesting.items():
                    body_lines.append(f"  - {k}: {v}")

        body_lines.extend([
            "",
            f"WhatsApp account: {business}",
            f"Workspace ID    : {account.workspace_id}",
            "",
            "You can reply to this customer directly from the SocioChat inbox.",
            "",
            "— SocioChat",
        ])
        body = "\n".join(body_lines)

        msg = MIMEMultipart()
        msg["From"] = mail_from
        msg["To"] = ", ".join(recipients)
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain"))

        with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as server:
            server.starttls()
            server.login(smtp_user, smtp_pass)
            server.sendmail(mail_from, recipients, msg.as_string())

        logger.info(
            "[button_notify] sent to %s (account=%s button=%r name=%s phone=%s)",
            recipients, account_id, label_clean, name_clean, phone_clean,
        )
    except Exception as exc:
        logger.exception(f"[button_notify] failed: {exc}")


BOOKING_TRIGGERS = [
    "book meeting", "book a meeting", "book call", "book a call",
    "schedule call", "schedule a call", "schedule meeting", "schedule a meeting",
    "call back", "arrange a call", "talk to expert", "speak to team", "book a demo", "schedule demo"
]

def check_and_trigger_booking_notification(account, conversation, message_text: str, message_id: int) -> None:
    """Scan message text for booking keywords and trigger background notification email."""
    if not message_text:
        return
        
    text_lower = message_text.lower()
    matched = False
    for trigger in BOOKING_TRIGGERS:
        if trigger in text_lower:
            matched = True
            break
            
    if matched:
        try:
            from .background_processor import bg_processor
            bg_processor.submit(
                send_booking_notification_email_bg,
                account_id=account.id,
                conversation_id=conversation.id,
                message_id=message_id,
                trigger_text=message_text,
            )
            logger.info(f"[human_escalation] Triggered booking notification for message {message_id}")
        except Exception as e:
            logger.error(f"[human_escalation] Failed to schedule booking notification: {e}")



def mark_conversation_human_required(
    conversation_id: int,
    reason: str,
    *,
    needs_attention: bool = True,
) -> Optional[Dict[str, Any]]:
    """Persist inbox flags on the conversation (attribution_data JSON)."""
    from .models import WhatsAppConversation

    conversation = WhatsAppConversation.query.get(conversation_id)
    if not conversation:
        return None

    now = datetime.now(timezone.utc).isoformat()
    data = dict(conversation.attribution_data or {})
    data["human_required"] = True
    data["human_required_at"] = now
    data["human_required_reason"] = reason
    if needs_attention:
        data["needs_attention"] = True
        data["needs_attention_at"] = now
        data["needs_attention_reason"] = reason

    conversation.attribution_data = data
    flag_modified(conversation, "attribution_data")
    conversation.updated_at = datetime.now(timezone.utc)
    db.session.commit()
    return conversation.to_dict()


def clear_conversation_human_required(
    conversation_id: int,
    *,
    reason: str = "agent_replied",
) -> Optional[Dict[str, Any]]:
    """Clear human_required / needs_attention after an agent replies from the inbox."""
    from .models import WhatsAppAccount, WhatsAppConversation

    conversation = WhatsAppConversation.query.get(conversation_id)
    if not conversation:
        return None

    data = dict(conversation.attribution_data or {})
    was_escalated = bool(data.get("human_required")) or bool(data.get("needs_attention"))
    if not was_escalated:
        return conversation.to_dict()

    now = datetime.now(timezone.utc).isoformat()
    data["human_required"] = False
    data["human_required_at"] = None
    data["human_required_reason"] = None
    data["needs_attention"] = False
    data["needs_attention_at"] = None
    data["needs_attention_reason"] = None
    data["human_cleared_at"] = now
    data["human_cleared_reason"] = reason

    conversation.attribution_data = data
    flag_modified(conversation, "attribution_data")
    conversation.updated_at = datetime.now(timezone.utc)
    db.session.commit()

    conv_dict = conversation.to_dict()
    account = WhatsAppAccount.query.get(conversation.account_id)
    workspace_id = getattr(account, "workspace_id", None) if account else None

    try:
        notification_manager.broadcast(
            "whatsapp_conversation_updated",
            {
                "conversation": conv_dict,
                "conversation_id": conversation_id,
                "account_id": conversation.account_id,
                "workspace_id": workspace_id,
                "human_required": False,
                "needs_attention": False,
                "reason": reason,
            },
        )
    except Exception as broadcast_err:
        logger.warning("[human_escalation] clear SSE broadcast failed: %s", broadcast_err)

    logger.info(
        "[human_escalation] Cleared escalation conv=%s reason=%s",
        conversation_id,
        reason,
    )
    return conv_dict


def enrich_agent_send_result(
    result: Dict[str, Any],
    *,
    reason: str = "agent_replied",
) -> Dict[str, Any]:
    """After a successful manual/agent outbound send, clear escalation and pause AI chat."""
    if not result or not result.get("success"):
        return result
    cid = result.get("conversation_id")
    if not cid:
        return result
    try:
        from .automation_models import pause_ai_chatbot_for_agent_handoff
        from .models import WhatsAppConversation

        conv_dict = clear_conversation_human_required(int(cid), reason=reason)
        overrides = pause_ai_chatbot_for_agent_handoff(int(cid), reason=reason)
        if overrides:
            result["automation_overrides"] = overrides

        conversation = WhatsAppConversation.query.get(int(cid))
        if conversation:
            result["conversation"] = conversation.to_dict()
        elif conv_dict:
            result["conversation"] = conv_dict
    except Exception as exc:
        logger.warning("[human_escalation] enrich_agent_send_result failed: %s", exc)
    return result


def set_conversation_needs_attention(
    conversation_id: int,
    needs_attention: bool = True,
    reason: Optional[str] = None,
) -> None:
    """Compatibility helper used by fast_router fallback path."""
    if needs_attention:
        mark_conversation_human_required(
            conversation_id,
            reason or "needs_attention",
            needs_attention=True,
        )
        return

    from .models import WhatsAppConversation

    conversation = WhatsAppConversation.query.get(conversation_id)
    if not conversation:
        return
    data = dict(conversation.attribution_data or {})
    data["needs_attention"] = False
    data["needs_attention_reason"] = None
    data["needs_attention_at"] = None
    conversation.attribution_data = data
    flag_modified(conversation, "attribution_data")
    db.session.commit()


def apply_human_handoff(
    *,
    account_id: int,
    conversation_id: int,
    to_phone: str,
    reason: str,
    incoming_message: str = "",
    customer_message: Optional[str] = None,
) -> Tuple[bool, Optional[str]]:
    """
    Full escalation: flag conversation, message customer, email team, SSE broadcast.

    Returns (success, error_message).
    """
    from .models import WhatsAppAccount, WhatsAppConversation
    from .services import WhatsAppService

    try:
        trace_event(
            stage="handoff.start",
            status="attempt",
            conversation_id=conversation_id,
            account_id=account_id,
            details={"reason": reason, "incoming_preview": (incoming_message or "")[:120]},
        )
        account = WhatsAppAccount.query.get(account_id)
        conversation = WhatsAppConversation.query.get(conversation_id)
        if not account or not conversation:
            return False, "Account or conversation not found"

        conv_dict = mark_conversation_human_required(conversation_id, reason)
        if not conv_dict:
            return False, "Failed to update conversation"

        access_token = account.get_access_token()
        wa_send_ok = False
        wa_send_error: Optional[str] = None
        if not access_token:
            wa_send_error = "No access token"
            logger.warning(
                "[human_escalation] Handoff WhatsApp send skipped (no access token) account=%s conv=%s",
                account_id,
                conversation_id,
            )
        else:
            handoff_text = customer_message or handoff_customer_message()
            service = WhatsAppService(
                access_token=access_token,
                phone_number_id=account.phone_number_id,
                waba_id=account.waba_id,
                workspace_id=account.workspace_id,
            )
            send_result = service.send_text(
                to_phone,
                handoff_text,
                conversation_id=conversation_id,
                broadcast_on_success=True,
            )
            wa_send_ok = bool(send_result.get("success"))
            if not wa_send_ok:
                wa_send_error = str(send_result.get("error") or "unknown_error")
                logger.warning(
                    "[human_escalation] Handoff WhatsApp send failed: %s",
                    wa_send_error,
                )

        _send_escalation_email(account, conversation, reason, incoming_message)

        try:
            notification_manager.broadcast(
                "whatsapp_conversation_updated",
                {
                    "conversation": conv_dict,
                    "conversation_id": conversation_id,
                    "account_id": account_id,
                    "workspace_id": account.workspace_id,
                    "human_required": True,
                    "needs_attention": True,
                    "reason": reason,
                },
            )
        except Exception as broadcast_err:
            logger.warning("[human_escalation] SSE broadcast failed: %s", broadcast_err)

        logger.info(
            "[human_escalation] Applied handoff conv=%s account=%s reason=%s wa_send_ok=%s wa_send_error=%s",
            conversation_id,
            account_id,
            reason,
            wa_send_ok,
            wa_send_error or "",
        )
        trace_event(
            stage="handoff.done",
            status="ok",
            conversation_id=conversation_id,
            account_id=account_id,
            details={"reason": reason, "wa_send_ok": wa_send_ok, "wa_send_error": wa_send_error},
        )
        return True, None
    except Exception as e:
        logger.exception("[human_escalation] apply_human_handoff failed: %s", e)
        trace_event(
            stage="handoff.done",
            status="error",
            conversation_id=conversation_id,
            account_id=account_id,
            details={"reason": reason, "error": str(e)},
        )
        try:
            db.session.rollback()
        except Exception:
            pass
        return False, str(e)
