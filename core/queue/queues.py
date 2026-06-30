"""
Named job queues with priority tiers for WhatsApp workers.
"""

from __future__ import annotations

import os
from typing import Dict, List, Tuple

# Realtime ingest (webhooks, automations routing) — highest priority
QUEUE_REALTIME = os.getenv("QUEUE_REALTIME_NAME", os.getenv("JOB_QUEUE_NAME", "sociovia:jobs:v2"))

# Heavy AI generation (embeddings, RAG, Gemini)
QUEUE_AI = os.getenv("QUEUE_AI_NAME", "sociovia:jobs:ai")

# Background / bulk / drip / notifications
QUEUE_BACKGROUND = os.getenv("QUEUE_BACKGROUND_NAME", "sociovia:jobs:background")

ALL_QUEUES: Tuple[str, ...] = (QUEUE_REALTIME, QUEUE_AI, QUEUE_BACKGROUND)

JOB_QUEUE_MAP: Dict[str, str] = {
    "whatsapp-webhook": QUEUE_REALTIME,
    "whatsapp-ai-generate": QUEUE_AI,
    "whatsapp-drip": QUEUE_BACKGROUND,
    "whatsapp-drip-trigger-campaign": QUEUE_BACKGROUND,
    "send-notification": QUEUE_BACKGROUND,
    "whatsapp-bulk-send": QUEUE_BACKGROUND,
}


def resolve_queue_for_job(job_name: str, explicit_queue: str | None = None) -> str:
    if explicit_queue:
        return explicit_queue
    return JOB_QUEUE_MAP.get(job_name, QUEUE_REALTIME)


def processing_queue_name(queue_name: str) -> str:
    return f"{queue_name}:processing"


def dead_letter_queue_name(queue_name: str) -> str:
    return f"{queue_name}:dead"


def worker_poll_queues(mode: str | None = None) -> List[str]:
    """Return Redis queue keys this worker should poll, highest priority first."""
    mode = (mode or os.getenv("WORKER_MODE", "all")).strip().lower()
    if mode == "realtime":
        return [QUEUE_REALTIME, QUEUE_BACKGROUND]
    if mode == "ai":
        return [QUEUE_AI]
    if mode == "background":
        return [QUEUE_BACKGROUND]
    # all / default dev — realtime first, then AI, then background
    return [QUEUE_REALTIME, QUEUE_AI, QUEUE_BACKGROUND]
