"""
Internal read-only verification endpoints for rollout confidence.
"""

from __future__ import annotations

import os
import logging
from flask import Blueprint, jsonify, request
from sqlalchemy import func, text

from shared_models import db
from core.deployment_safety import (
    is_non_dev_environment,
    usage_events_read_secret_configured,
    whatsapp_capabilities_strict_enabled,
    slog,
)
from .models import WhatsAppAccount, WhatsAppAccountCapabilities, WhatsAppUsageEvent

logger = logging.getLogger(__name__)

admin_verification_internal_bp = Blueprint(
    "admin_verification_internal",
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


def _parse_consumer_name() -> str:
    return (request.args.get("consumer_name") or "billing_default").strip() or "billing_default"


def _stale_projection_count(hours: int = 24) -> int:
    sql = text(
        """
        SELECT COUNT(*)
        FROM whatsapp_account_capabilities
        WHERE updated_at < NOW() - (:hours || ' hours')::interval
        """
    )
    value = db.session.execute(sql, {"hours": int(hours)}).scalar()
    return int(value or 0)


def _verification_snapshot(consumer_name: str) -> dict:
    total_accounts = db.session.query(func.count(WhatsAppAccount.id)).scalar() or 0
    covered_accounts = db.session.query(func.count(WhatsAppAccountCapabilities.account_id)).scalar() or 0
    missing_projection_count = int(total_accounts) - int(covered_accounts)

    latest_usage_event_id = db.session.query(func.max(WhatsAppUsageEvent.id)).scalar() or 0
    checkpoint_row = db.session.execute(
        text(
            "SELECT last_event_id FROM whatsapp_usage_event_checkpoints "
            "WHERE consumer_name = :cn LIMIT 1"
        ),
        {"cn": consumer_name},
    ).fetchone()
    consumer_checkpoint = int(checkpoint_row[0]) if checkpoint_row and checkpoint_row[0] is not None else 0
    consumer_lag_estimate = max(0, int(latest_usage_event_id) - consumer_checkpoint)

    stale_projection_count = _stale_projection_count(24)
    strict_mode = whatsapp_capabilities_strict_enabled()

    freshness_percent = 0.0
    if int(covered_accounts) > 0:
        freshness_percent = round(
            ((int(covered_accounts) - stale_projection_count) / float(int(covered_accounts))) * 100.0,
            2,
        )

    return {
        "consumer_name": consumer_name,
        "strict_mode_enabled": strict_mode,
        "total_accounts": int(total_accounts),
        "covered_accounts": int(covered_accounts),
        "missing_projection_count": int(max(0, missing_projection_count)),
        "stale_projection_count": int(stale_projection_count),
        "projection_coverage_percent": round(
            (float(int(covered_accounts)) / float(int(total_accounts)) * 100.0) if int(total_accounts) > 0 else 100.0,
            2,
        ),
        "projection_freshness_percent_24h": freshness_percent,
        "latest_usage_event_id": int(latest_usage_event_id),
        "consumer_checkpoint_id": int(consumer_checkpoint),
        "consumer_lag_estimate": int(consumer_lag_estimate),
    }


def _auth_or_503():
    if is_non_dev_environment() and not usage_events_read_secret_configured():
        logger.error("Admin verification: no internal secret configured in non-dev")
        return jsonify(
            {
                "success": False,
                "error": "Server misconfiguration: set WHATSAPP_USAGE_EVENTS_SECRET or WHATSAPP_CAPABILITIES_UPSERT_SECRET",
            }
        ), 503
    if not _verify_secret():
        return jsonify({"success": False, "error": "Unauthorized"}), 401
    return None


@admin_verification_internal_bp.route("/admin/verification-summary", methods=["GET"])
def verification_summary():
    auth = _auth_or_503()
    if auth:
        return auth
    consumer_name = _parse_consumer_name()
    snapshot = _verification_snapshot(consumer_name)
    slog("projection_freshness_summary", **snapshot)
    return jsonify({"success": True, "summary": snapshot}), 200


@admin_verification_internal_bp.route("/admin/projection-coverage", methods=["GET"])
def projection_coverage():
    auth = _auth_or_503()
    if auth:
        return auth
    consumer_name = _parse_consumer_name()
    snapshot = _verification_snapshot(consumer_name)
    return jsonify(
        {
            "success": True,
            "total_accounts": snapshot["total_accounts"],
            "covered_accounts": snapshot["covered_accounts"],
            "missing_projection_count": snapshot["missing_projection_count"],
            "projection_coverage_percent": snapshot["projection_coverage_percent"],
        }
    ), 200


@admin_verification_internal_bp.route("/admin/usage-lag", methods=["GET"])
def usage_lag():
    auth = _auth_or_503()
    if auth:
        return auth
    consumer_name = _parse_consumer_name()
    snapshot = _verification_snapshot(consumer_name)
    return jsonify(
        {
            "success": True,
            "consumer_name": consumer_name,
            "latest_usage_event_id": snapshot["latest_usage_event_id"],
            "consumer_checkpoint_id": snapshot["consumer_checkpoint_id"],
            "consumer_lag_estimate": snapshot["consumer_lag_estimate"],
        }
    ), 200


@admin_verification_internal_bp.route("/admin/latest-usage-event-id", methods=["GET"])
def latest_usage_event_id():
    auth = _auth_or_503()
    if auth:
        return auth
    latest_usage_event_id_value = db.session.query(func.max(WhatsAppUsageEvent.id)).scalar() or 0
    return jsonify({"success": True, "latest_usage_event_id": int(latest_usage_event_id_value)}), 200


@admin_verification_internal_bp.route("/admin/missing-projection-count", methods=["GET"])
def missing_projection_count():
    auth = _auth_or_503()
    if auth:
        return auth
    total_accounts = db.session.query(func.count(WhatsAppAccount.id)).scalar() or 0
    covered_accounts = db.session.query(func.count(WhatsAppAccountCapabilities.account_id)).scalar() or 0
    missing_count = int(total_accounts) - int(covered_accounts)
    return jsonify({"success": True, "missing_projection_count": int(max(0, missing_count))}), 200


@admin_verification_internal_bp.route("/admin/stale-projection-count", methods=["GET"])
def stale_projection_count():
    auth = _auth_or_503()
    if auth:
        return auth
    stale_count = _stale_projection_count(24)
    return jsonify({"success": True, "stale_projection_count": int(stale_count), "stale_hours": 24}), 200


@admin_verification_internal_bp.route("/admin/strict-mode-state", methods=["GET"])
def strict_mode_state():
    auth = _auth_or_503()
    if auth:
        return auth
    return jsonify(
        {
            "success": True,
            "strict_mode_enabled": whatsapp_capabilities_strict_enabled(),
        }
    ), 200
