"""
Main handler — orchestrates: config check → media download → vision analysis → WhatsApp reply.

Called from fast_router.py when msg_type == "image".
"""

import logging
from typing import Optional, Dict, Any

from .config import is_food_analysis_enabled
from .media import download_image
from .vision import analyze_food_image
from .prompts import PROCESSING_ERROR_FALLBACK

logger = logging.getLogger(__name__)


def handle_food_image(
    account,
    conversation,
    raw_message: Dict[str, Any],
    from_phone: str,
    workspace_id: Optional[str] = None,
) -> bool:
    """Process an incoming food image and reply with nutritional analysis.

    Returns True if handled (reply sent or attempted), False if this workspace
    doesn't have food analysis enabled (so fast_router can fall through).
    """
    ws_id = workspace_id or getattr(account, "workspace_id", None)

    if not is_food_analysis_enabled(ws_id):
        return False

    logger.info(
        "[food_analysis] Processing image for account=%s workspace=%s phone=%s",
        account.id, ws_id, from_phone,
    )

    # 1. Extract media_id from the raw Meta message
    image_data = raw_message.get("image", {})
    media_id = image_data.get("id")
    if not media_id:
        logger.warning("[food_analysis] No media_id in image message for account %s", account.id)
        return False

    # 2. Get access token from account
    access_token = _get_access_token(account)
    if not access_token:
        logger.error("[food_analysis] No access token for account %s", account.id)
        return False

    # 3. Send "processing" message immediately so user knows we're working on it
    _send_reply(account, from_phone, conversation.id,
                "Analyzing your food image... please wait a moment while I prepare your nutritional assessment.")

    # 4. Download image from Meta
    result = download_image(media_id, access_token)
    if result is None:
        _send_reply(account, from_phone, conversation.id, PROCESSING_ERROR_FALLBACK)
        return True

    image_bytes, mime_type = result

    # 4. Run vision analysis via OpenAI gateway
    analysis_text = analyze_food_image(
        image_bytes=image_bytes,
        mime_type=mime_type,
        workspace_id=str(ws_id),
    )

    if not analysis_text:
        _send_reply(account, from_phone, conversation.id, PROCESSING_ERROR_FALLBACK)
        return True

    # 5. Send the analysis back via WhatsApp
    _send_reply(account, from_phone, conversation.id, analysis_text)
    return True


def _get_access_token(account) -> Optional[str]:
    """Resolve the WhatsApp access token for this account."""
    try:
        token = account.get_access_token()
        if token:
            return token
    except Exception:
        pass

    import os
    return os.environ.get("WHATSAPP_ACCESS_TOKEN")


def _send_reply(account, to_phone: str, conversation_id: int, text: str):
    """Send a text reply via WhatsApp."""
    try:
        from whatsapp.services import WhatsAppService

        service = WhatsAppService(
            access_token=_get_access_token(account),
            phone_number_id=getattr(account, "phone_number_id", None),
            waba_id=getattr(account, "waba_id", None),
            workspace_id=getattr(account, "workspace_id", None),
        )

        result = service.send_text(
            to_phone,
            text,
            conversation_id=conversation_id,
            defer_post_send=True,
            broadcast_on_success=True,
        )

        if result.get("success"):
            logger.info("[food_analysis] Reply sent to %s (wamid=%s)", to_phone, result.get("message_id", "?"))
        else:
            logger.error("[food_analysis] Reply send failed: %s", result.get("error", "unknown"))
    except Exception as e:
        logger.exception("[food_analysis] Failed to send reply: %s", e)
