"""
Dedicated AI generation job — runs on the AI queue, isolated from webhook ingest.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, Mapping

logger = logging.getLogger(__name__)


def get_ai_concurrency_limit() -> int:
    return max(1, int(os.getenv("MAX_CONCURRENT_AI_JOBS", "2")))


def prewarm_ai_runtime() -> None:
    """Initialize GenAI / RAG clients at worker startup (not on first live message)."""
    try:
        from whatsapp.ai_chatbot import get_genai_runtime_status, get_rag_module

        status = get_genai_runtime_status()
        logger.info("[ai_job] prewarm GenAI available=%s", status.get("available"))
        rag = get_rag_module()
        if rag:
            logger.info("[ai_job] prewarm RAG module ready")
    except Exception as exc:
        logger.warning("[ai_job] prewarm skipped: %s", exc)


def run_ai_generation(payload: Mapping[str, Any]) -> Dict[str, Any]:
    """Execute AI response generation and WhatsApp send on the dedicated AI queue."""
    from whatsapp.fast_router import _send_ai_response
    from whatsapp.trace_debug import trace_event

    data = dict(payload or {})
    account_id = int(data["account_id"])
    conversation_id = int(data["conversation_id"])
    response_config = dict(data.get("response_config") or {})
    inbound_wamid = response_config.get("inbound_wamid")
    to_phone = str(data.get("to_phone") or "")
    rule_name = data.get("rule_name")
    started_at = float(data.get("started_at") or time.time())
    enqueued_at = data.get("enqueued_at")

    queue_wait_ms = 0
    if enqueued_at:
        try:
            from datetime import datetime, timezone

            enq = datetime.fromisoformat(str(enqueued_at).replace("Z", "+00:00"))
            queue_wait_ms = int((datetime.now(timezone.utc) - enq).total_seconds() * 1000)
        except Exception:
            pass

    gen_start = time.perf_counter()
    trace_event(
        stage="ai.job.start",
        status="running",
        wamid=inbound_wamid,
        conversation_id=conversation_id,
        account_id=account_id,
        details={"rule_name": rule_name, "queue_wait_ms": queue_wait_ms},
    )
    _send_ai_response(
        account_id=account_id,
        conversation_id=conversation_id,
        response_config=response_config,
        to_phone=to_phone,
        rule_name=rule_name,
        started_at=started_at,
    )
    gen_ms = int((time.perf_counter() - gen_start) * 1000)
    trace_event(
        stage="ai.job.done",
        status="ok",
        wamid=inbound_wamid,
        conversation_id=conversation_id,
        account_id=account_id,
        details={"generation_ms": gen_ms, "rule_name": rule_name},
    )
    logger.info(
        "[ai_job] completed conversation=%s queue_wait_ms=%s gen_ms=%s rule=%s",
        conversation_id,
        queue_wait_ms,
        gen_ms,
        rule_name,
    )
    return {
        "success": True,
        "conversation_id": conversation_id,
        "queue_wait_ms": queue_wait_ms,
        "generation_ms": gen_ms,
    }
