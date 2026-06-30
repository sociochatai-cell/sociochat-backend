"""
Internal API: entitlement projection upsert (monolith / worker → DB).

Secured with Bearer WHATSAPP_CAPABILITIES_UPSERT_SECRET (set in production).
"""

import os
import logging
from flask import Blueprint, request, jsonify

from shared_models import db
from core.deployment_safety import (
    capabilities_upsert_secret_configured,
    is_non_dev_environment,
    slog,
)
from .capabilities import upsert_capabilities_projection

logger = logging.getLogger(__name__)

capabilities_internal_bp = Blueprint(
    "capabilities_internal",
    __name__,
)

_SECRET = os.getenv("WHATSAPP_CAPABILITIES_UPSERT_SECRET", "")


def _verify_capabilities_secret() -> bool:
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


@capabilities_internal_bp.route("/accounts/<int:account_id>/capabilities", methods=["POST", "PUT"])
def put_account_capabilities(account_id: int):
    """
    Upsert operational capabilities for a WhatsApp account (integer PK).

    JSON body (all optional except you should send what changed):
        subscription_status, ai_enabled, automation_enabled, broadcast_enabled,
        daily_message_limit, monthly_ai_tokens, projection_version
    """
    if is_non_dev_environment() and not capabilities_upsert_secret_configured():
        logger.error("Capabilities upsert: secret unset in non-dev (misconfiguration)")
        return jsonify(
            {"success": False, "error": "Server misconfiguration: WHATSAPP_CAPABILITIES_UPSERT_SECRET unset"}
        ), 503

    if not _verify_capabilities_secret():
        logger.warning("Unauthorized capabilities upsert attempt from %s", request.remote_addr)
        return jsonify({"success": False, "error": "Unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    ok, msg = upsert_capabilities_projection(
        account_id,
        subscription_status=data.get("subscription_status"),
        ai_enabled=data.get("ai_enabled") if "ai_enabled" in data else None,
        automation_enabled=data.get("automation_enabled") if "automation_enabled" in data else None,
        broadcast_enabled=data.get("broadcast_enabled") if "broadcast_enabled" in data else None,
        daily_message_limit=data.get("daily_message_limit") if "daily_message_limit" in data else None,
        monthly_ai_tokens=data.get("monthly_ai_tokens") if "monthly_ai_tokens" in data else None,
        projection_version=data.get("projection_version") if "projection_version" in data else None,
        session=db.session,
    )
    if not ok:
        slog("capabilities_projection_upsert_rejected", account_id=account_id, error=msg, remote_addr=request.remote_addr)
        return jsonify({"success": False, "error": msg}), 400
    try:
        db.session.commit()
    except Exception as e:
        logger.exception("capabilities commit failed: %s", e)
        db.session.rollback()
        slog(
            "capabilities_projection_upsert_commit_failed",
            account_id=account_id,
            error=str(e),
            remote_addr=request.remote_addr,
        )
        return jsonify({"success": False, "error": str(e)}), 500

    updated_keys = [
        k
        for k in (
            "subscription_status",
            "ai_enabled",
            "automation_enabled",
            "broadcast_enabled",
            "daily_message_limit",
            "monthly_ai_tokens",
            "projection_version",
        )
        if k in data
    ]
    slog(
        "capabilities_projection_upsert",
        account_id=account_id,
        updated_keys=updated_keys,
        projection_version=data.get("projection_version"),
        remote_addr=request.remote_addr,
    )

    return jsonify({"success": True, "account_id": account_id}), 200
