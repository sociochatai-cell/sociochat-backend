"""JSON-style structured logs for monolith ↔ WhatsApp integration."""

from __future__ import annotations

import json
import logging
from typing import Any, Dict

logger = logging.getLogger("monolith_whatsapp_integration")


def mi_log(event: str, **fields: Any) -> None:
    payload: Dict[str, Any] = {"mi_event": event, **fields}
    logger.info("%s %s", event, json.dumps(payload, default=str))
