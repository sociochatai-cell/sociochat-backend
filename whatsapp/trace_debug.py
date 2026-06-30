"""
Lightweight webhook/AI trace timeline for deterministic debugging.

Stores short-lived structured events in Redis, keyed by wamid and conversation.
This is additive-only observability and does not alter business logic.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from core.cache import get_redis_client
from core.cache.redis_client import WA_KEY_PREFIX

logger = logging.getLogger(__name__)


def _trace_enabled() -> bool:
    raw = os.getenv("WHATSAPP_TRACE_ENABLED", "true")
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _trace_ttl_seconds() -> int:
    try:
        return max(3600, int(os.getenv("WHATSAPP_TRACE_TTL_SECONDS", "604800")))
    except Exception:
        return 604800  # 7 days


def _trace_max_events() -> int:
    try:
        return max(50, int(os.getenv("WHATSAPP_TRACE_MAX_EVENTS_PER_KEY", "500")))
    except Exception:
        return 500


def _sanitize(details: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    # Keep payloads compact and JSON-serializable.
    out: Dict[str, Any] = {}
    for k, v in (details or {}).items():
        if isinstance(v, (str, int, float, bool)) or v is None:
            out[k] = v
            continue
        if isinstance(v, dict):
            out[k] = {str(kk): vv for kk, vv in list(v.items())[:20]}
            continue
        if isinstance(v, list):
            out[k] = v[:20]
            continue
        out[k] = str(v)
    return out


def trace_event(
    *,
    stage: str,
    status: str = "ok",
    wamid: Optional[str] = None,
    conversation_id: Optional[int] = None,
    account_id: Optional[int] = None,
    details: Optional[Dict[str, Any]] = None,
) -> None:
    if not _trace_enabled():
        return

    event = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "stage": str(stage or "").strip() or "unknown_stage",
        "status": str(status or "").strip() or "ok",
        "wamid": str(wamid or "").strip() or None,
        "conversation_id": int(conversation_id) if conversation_id else None,
        "account_id": int(account_id) if account_id else None,
        "details": _sanitize(details),
    }

    # Always emit to normal logs for grepability.
    logger.info("[wa-trace] %s", json.dumps(event, sort_keys=True, default=str))
    try:
        from .debug_logger import debug_mode_enabled, wa_debug

        if debug_mode_enabled():
            wa_debug.after(
                "pipeline_trace",
                status=str(status or "ok"),
                acct_id=account_id,
                conv_id=conversation_id,
                wamid=wamid,
                details={"stage": event.get("stage"), **(event.get("details") or {})},
            )
    except Exception:
        pass

    client = get_redis_client()
    if client is None:
        return

    payload = json.dumps(event, separators=(",", ":"), sort_keys=True, default=str)
    ttl = _trace_ttl_seconds()
    max_events = _trace_max_events()

    try:
        if event["wamid"]:
            wkey = f"{WA_KEY_PREFIX}trace:wamid:{event['wamid']}"
            client.lpush(wkey, payload)
            client.ltrim(wkey, 0, max_events - 1)
            client.expire(wkey, ttl)

        if event["conversation_id"]:
            ckey = f"{WA_KEY_PREFIX}trace:conv:{event['conversation_id']}"
            client.lpush(ckey, payload)
            client.ltrim(ckey, 0, max_events - 1)
            client.expire(ckey, ttl)
    except Exception as exc:
        logger.warning("[wa-trace] redis write failed: %s", exc)


def _read_list(key: str, limit: int) -> List[Dict[str, Any]]:
    client = get_redis_client()
    if client is None:
        return []
    try:
        raw_items = client.lrange(key, 0, max(0, limit - 1))
    except Exception as exc:
        logger.warning("[wa-trace] redis read failed: %s", exc)
        return []
    events: List[Dict[str, Any]] = []
    for item in reversed(raw_items):
        try:
            parsed = json.loads(item)
            if isinstance(parsed, dict):
                events.append(parsed)
        except Exception:
            continue
    return events


def get_trace_by_wamid(wamid: str, limit: int = 200) -> List[Dict[str, Any]]:
    wamid = str(wamid or "").strip()
    if not wamid:
        return []
    key = f"{WA_KEY_PREFIX}trace:wamid:{wamid}"
    return _read_list(key, min(max(1, int(limit)), 1000))


def get_trace_by_conversation(conversation_id: int, limit: int = 300) -> List[Dict[str, Any]]:
    if not conversation_id:
        return []
    key = f"{WA_KEY_PREFIX}trace:conv:{int(conversation_id)}"
    return _read_list(key, min(max(1, int(limit)), 1000))
