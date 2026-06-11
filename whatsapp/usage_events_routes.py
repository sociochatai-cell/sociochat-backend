"""
Internal API for durable WhatsApp usage events.

Monolith/Billing reads usage events using a monotonic `since_id` cursor.
"""

import os
import logging

from flask import Blueprint, jsonify, request
from sqlalchemy import func

from shared_models import db
from core.deployment_safety import (
    is_non_dev_environment,
    slog,
    usage_events_read_secret_configured,
)
from .models import WhatsAppUsageEvent

logger = logging.getLogger(__name__)

usage_events_internal_bp = Blueprint(
    "usage_events_internal",
    __name__,
)

_SECRET = (
    os.getenv("WHATSAPP_USAGE_EVENTS_SECRET", "").strip()
    or os.getenv("WHATSAPP_CAPABILITIES_UPSERT_SECRET", "").strip()
)


def _verify_secret() -> bool:
    if not _SECRET:
        return False
    auth_header = request.headers.get("Authorization") or ""
    token = ""
    parts = auth_header.split()
    if len(parts) == 2 and parts[0].lower() == "bearer":
        token = parts[1]
    else:
        token = auth_header.strip()
    return token == _SECRET


@usage_events_internal_bp.route("/usage-events", methods=["GET"])
def list_usage_events():
    """
    Fetch durable usage events for billing consumers.

    Consumers MUST dedupe accounting on `event_key` and use monotonic `id`
    only for cursor/checkpointing. Response includes `stream_max_id` for lag.

    Query:
      - since_id (default 0)
      - limit (default 100, max 500)
      - event_type (optional)
    """
    if is_non_dev_environment() and not usage_events_read_secret_configured():
        logger.error("Usage-events read: no internal secret configured in non-dev")
        return jsonify(
            {
                "success": False,
                "error": "Server misconfiguration: set WHATSAPP_USAGE_EVENTS_SECRET or WHATSAPP_CAPABILITIES_UPSERT_SECRET",
            }
        ), 503

    if not _verify_secret():
        logger.warning("Unauthorized usage-event read attempt from %s", request.remote_addr)
        return jsonify({"success": False, "error": "Unauthorized"}), 401

    try:
        since_id = int((request.args.get("since_id") or "0").strip())
    except ValueError:
        return jsonify({"success": False, "error": "since_id must be an integer"}), 400

    try:
        limit = int((request.args.get("limit") or "100").strip())
    except ValueError:
        return jsonify({"success": False, "error": "limit must be an integer"}), 400

    limit = max(1, min(limit, 500))
    event_type = (request.args.get("event_type") or "").strip()

    query = WhatsAppUsageEvent.query.filter(WhatsAppUsageEvent.id > since_id)
    if event_type:
        query = query.filter(WhatsAppUsageEvent.event_type == event_type)

    rows = query.order_by(WhatsAppUsageEvent.id.asc()).limit(limit).all()
    last_id = rows[-1].id if rows else since_id

    # Head of stream (for consumer lag: stream_max_id vs consumer checkpoint).
    stream_max_id = db.session.query(func.max(WhatsAppUsageEvent.id)).scalar()
    stream_max_id = int(stream_max_id) if stream_max_id is not None else 0

    slog(
        "usage_events_poll",
        since_id=since_id,
        limit=limit,
        event_type=event_type or None,
        returned_count=len(rows),
        last_id=int(last_id) if last_id is not None else since_id,
        stream_max_id=stream_max_id,
        remote_addr=request.remote_addr,
    )

    return jsonify(
        {
            "success": True,
            "count": len(rows),
            "since_id": since_id,
            "last_id": int(last_id) if last_id is not None else since_id,
            "stream_max_id": stream_max_id,
            "events": [row.to_dict() for row in rows],
        }
    ), 200
