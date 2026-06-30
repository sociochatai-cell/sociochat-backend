"""HTTP client for AgentOS Gateway (whatsapp-api → agentos-api)."""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional

import requests

logger = logging.getLogger(__name__)


def agentos_enabled() -> bool:
    return bool(os.environ.get("AGENTOS_API_BASE", "").strip())


def get_agentos_base() -> str:
    return os.environ.get("AGENTOS_API_BASE", "http://127.0.0.1:8090").rstrip("/")


def proxy_chat(
    *,
    user_id: int,
    workspace_id: int,
    message: str,
    channel: str = "whatsapp",
    mode: str = "execute_with_approval",
) -> Dict[str, Any]:
    url = f"{get_agentos_base()}/api/v1/agent/chat"
    secret = os.environ.get("AGENTOS_SERVICE_SECRET", "")
    headers = {"Content-Type": "application/json"}
    if secret:
        headers["Authorization"] = f"Bearer {secret}"
    payload = {
        "user_id": user_id,
        "workspace_id": workspace_id,
        "message": message,
        "channel": channel,
        "mode": mode,
    }
    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=90)
        data = resp.json() if resp.content else {}
        if resp.status_code >= 400:
            return {"success": False, "error": data.get("error", resp.text), "status_code": resp.status_code}
        return {"success": True, "data": data}
    except Exception as exc:
        logger.exception("AgentOS proxy chat failed")
        return {"success": False, "error": str(exc)}
