"""
Detect growth/automation intents on WhatsApp and hand off to AgentOS.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, Optional, Tuple

from core.db import db

logger = logging.getLogger(__name__)

_GROWTH_PATTERNS = [
    r"\bgenerate\s+leads?\b",
    r"\bcreate\s+campaign\b",
    r"\brun\s+ads?\b",
    r"\bmeta\s+ads?\b",
    r"\bfacebook\s+ads?\b",
    r"\bmarketing\s+plan\b",
    r"\bgrowth\s+plan\b",
    r"\bbudget\s+₹?\s*[\d,]+",
    r"\b₹\s*[\d,]+\s*k?\b",
    r"\bschedule\s+bulk\b",
    r"\bdrip\s+campaign\b",
    r"\bhey\s+sociovia\b",
]


def is_agentos_growth_intent(message: str) -> bool:
    text = (message or "").strip().lower()
    if len(text) < 8:
        return False
    return any(re.search(p, text, re.I) for p in _GROWTH_PATTERNS)


def _parse_user_id(account) -> Optional[int]:
    raw = getattr(account, "connected_by_user_id", None)
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _store_run_on_conversation(conversation_id: int, meta: Dict[str, Any]) -> None:
    from .models import WhatsAppConversation

    conv = WhatsAppConversation.query.get(conversation_id)
    if not conv:
        return
    data = dict(conv.attribution_data or {})
    data["agentos_run_id"] = meta.get("agentos_run_id")
    data["agentos_status"] = meta.get("agentos_status")
    conv.attribution_data = data
    db.session.commit()


def handle_inbound_agentos(
    *,
    account,
    conversation_id: int,
    incoming_message: str,
    service,
    to_phone: str,
) -> Tuple[bool, Optional[Dict[str, Any]]]:
    """
    Handle AgentOS growth intents and APPROVE replies.
    Returns (handled, send_result).
    """
    from integrations.agentos_client import agentos_enabled, get_agentos_base, proxy_chat
    import requests

    if not agentos_enabled():
        return False, None

    msg = (incoming_message or "").strip()
    user_id = _parse_user_id(account)
    workspace_id = getattr(account, "workspace_id", None)

    # APPROVE follow-up
    if msg.upper() in ("APPROVE", "YES", "OK") and user_id and workspace_id:
        from .models import WhatsAppConversation

        conv = WhatsAppConversation.query.get(conversation_id)
        run_id = None
        if conv and isinstance(conv.attribution_data, dict):
            run_id = conv.attribution_data.get("agentos_run_id")
        if run_id:
            secret = __import__("os").environ.get("AGENTOS_SERVICE_SECRET", "")
            headers = {"Content-Type": "application/json"}
            if secret:
                headers["Authorization"] = f"Bearer {secret}"
            url = f"{get_agentos_base()}/api/v1/agent/runs/{run_id}/approve"
            try:
                resp = requests.post(url, json={"user_id": user_id}, headers=headers, timeout=60)
                data = resp.json() if resp.content else {}
                if resp.status_code < 400:
                    reply = "Approved. Your growth plan is now executing. I'll update you on progress."
                    send_result = service.send_text(to_phone, reply)
                    return True, send_result
                reply = f"Could not approve plan: {data.get('error', 'unknown error')}"
                return True, service.send_text(to_phone, reply)
            except Exception as exc:
                logger.warning("AgentOS approve failed: %s", exc)

    handled, reply, meta = try_agentos_handoff(
        workspace_id=workspace_id,
        user_id=user_id,
        message=msg,
    )
    if not handled or not reply:
        return False, None

    if meta:
        try:
            _store_run_on_conversation(conversation_id, meta)
        except Exception as exc:
            logger.debug("Could not store agentos run on conversation: %s", exc)

    send_result = service.send_text(to_phone, reply)
    return True, send_result


def try_agentos_handoff(
    *,
    workspace_id: str | int,
    user_id: Optional[int],
    message: str,
) -> Tuple[bool, Optional[str], Optional[Dict[str, Any]]]:
    """Returns (handled, reply_text, metadata)."""
    try:
        from integrations.agentos_client import agentos_enabled, proxy_chat
    except ImportError:
        return False, None, None

    if not agentos_enabled() or not is_agentos_growth_intent(message):
        return False, None, None

    uid = user_id or 0
    try:
        wid = int(workspace_id)
    except (TypeError, ValueError):
        return False, None, None

    if uid <= 0:
        logger.debug("AgentOS handoff skipped: no user_id for workspace %s", wid)
        return False, None, None

    result = proxy_chat(user_id=uid, workspace_id=wid, message=message, channel="whatsapp")
    if not result.get("success"):
        logger.warning("AgentOS handoff failed: %s", result.get("error"))
        return False, None, None

    data = result.get("data") or {}
    proposal = (data.get("proposal") or "").strip()
    if not proposal:
        proposal = "I've started preparing a growth plan for you. Reply APPROVE when you're ready to proceed."

    status = data.get("status")
    if status == "awaiting_approval":
        proposal += "\n\nReply *APPROVE* to start execution."

    meta = {
        "agentos_run_id": data.get("run_id"),
        "agentos_status": status,
        "agentos_plan": data.get("plan"),
    }
    return True, proposal[:4096], meta
