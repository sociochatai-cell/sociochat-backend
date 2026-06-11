import logging
from datetime import datetime, timezone
from typing import Optional

from notifications import notification_manager
from core.deployment_safety import slog

from .models import WhatsAppConversation, WhatsAppMessage, WhatsAppUsageEvent

logger = logging.getLogger(__name__)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _build_message_sent_payload(
    *,
    account_id: int,
    message: WhatsAppMessage,
    conversation: WhatsAppConversation,
) -> dict:
    return {
        "event": "message_sent",
        "category": "outbound",
        "account_id": account_id,
        "conversation_id": conversation.id,
        "message_id": message.id,
        "wamid": message.wamid,
        "message_type": message.type,
        "timestamp": _utc_now().isoformat(),
    }


def emit_message_sent_usage_event(
    *,
    db_session,
    account_id: int,
    message: WhatsAppMessage,
    conversation: WhatsAppConversation,
) -> Optional[WhatsAppUsageEvent]:
    """
    Insert one durable message_sent usage event.

    Idempotency key preference:
      1) WhatsApp `wamid` (stable across retries)
      2) local message id (fallback when wamid missing)
    """
    if not account_id or not message or message.direction != "outgoing" or message.status != "sent":
        return None

    base_key = message.wamid or f"message:{message.id}"
    event_key = f"message_sent:{base_key}"

    existing = db_session.query(WhatsAppUsageEvent).filter_by(event_key=event_key).first()
    if existing:
        slog(
            "usage_event_emit_dedupe",
            event_type="message_sent",
            event_key=event_key,
            account_id=account_id,
            message_id=message.id,
            existing_usage_event_id=existing.id,
        )
        return existing

    usage_event = WhatsAppUsageEvent(
        event_type="message_sent",
        event_key=event_key,
        account_id=account_id,
        message_id=message.id,
        wamid=message.wamid,
        occurred_at=_utc_now(),
        payload=_build_message_sent_payload(
            account_id=account_id,
            message=message,
            conversation=conversation,
        ),
    )
    db_session.add(usage_event)
    db_session.flush()
    slog(
        "usage_event_emit",
        event_type="message_sent",
        event_key=event_key,
        account_id=account_id,
        message_id=message.id,
        wamid=message.wamid,
        usage_event_id=usage_event.id,
    )
    return usage_event


def broadcast_usage_event(usage_event: Optional[WhatsAppUsageEvent]) -> None:
    """Best-effort realtime fanout for consumers that subscribe to events."""
    if not usage_event:
        return
    try:
        notification_manager.broadcast("whatsapp_usage_event", usage_event.to_dict())
    except Exception as exc:
        logger.warning("Usage event realtime broadcast failed (non-fatal): %s", exc)
