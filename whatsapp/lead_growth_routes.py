"""
WhatsApp Lead Growth Config API
===============================

CRUD over the per-workspace lead auto-discovery / intent-routing / nurture
settings that gate the inbound-message lead pipeline (see lead_growth_models.py
and lead_action_service.py).

Endpoints (mounted under ``/api/whatsapp``):

  * GET  /api/whatsapp/lead-growth/settings?workspace_id=<id>
        -> the workspace's settings row, or the defaults dict when no row exists.

  * PUT  /api/whatsapp/lead-growth/settings
        body: {workspace_id, intent_rules?, confidence_threshold?,
               auto_discovery_enabled?, notify_enabled?, notify_template?,
               notify_destinations?, nurture_enabled?}
        -> UPSERTs the row (partial updates allowed) and returns the saved dict.

  * GET  /api/whatsapp/lead-growth/intents
        -> the valid intent labels the classifier emits (for building the
           intent_rules mapping UI) plus the high-intent subset that drives
           auto-discovery.

All responses are JSON ``{"success": bool, ...}``. workspace_id is INTEGER on the
shared CRM tables; a non-integer value yields a 400.
"""

from __future__ import annotations

import logging

from flask import Blueprint, jsonify, request

from shared_models import db
from .lead_growth_models import get_lead_growth_settings, set_lead_growth_settings
from .lead_intent_router import INTENT_KEYWORDS
from .lead_action_service import _HIGH_INTENT
from .flow_access import _require_workspace_scope

logger = logging.getLogger(__name__)

lead_growth_bp = Blueprint("whatsapp_lead_growth", __name__, url_prefix="/api/whatsapp")


def _resolve_workspace_id(source: dict):
    """Pull workspace_id from a dict (query args or JSON body). Returns (ws_id, error_response).

    On success returns (int, None); on a missing/invalid value returns (None, (json, status)).

    Also enforces tenant scope: the requested workspace_id must match the trusted
    workspace derived from the request (X-Workspace-ID header / session) — same
    ownership check used by require_account_access via flow_access. A mismatch is
    rejected with 403 to prevent IDOR (one tenant reading/writing another's config).
    """
    raw = source.get("workspace_id")
    if raw in (None, ""):
        return None, (jsonify({"success": False, "error": "workspace_id is required"}), 400)
    try:
        ws_id = int(raw)
    except (TypeError, ValueError):
        return None, (jsonify({"success": False, "error": "workspace_id must be an integer"}), 400)

    # Reject when the trusted/session workspace does not match the requested one.
    denied = _require_workspace_scope(str(ws_id))
    if denied is not None:
        body, status = denied
        return None, (jsonify(body), status)

    return ws_id, None


@lead_growth_bp.route("/lead-growth/settings", methods=["GET"])
def get_settings():
    """Return the lead-growth settings for a workspace (defaults when unset)."""
    ws_id, error = _resolve_workspace_id(request.args)
    if error is not None:
        return error

    try:
        settings = get_lead_growth_settings(ws_id)
        return jsonify({"success": True, "workspace_id": ws_id, "settings": settings}), 200
    except Exception:
        logger.exception("[lead_growth_routes] get_settings failed ws=%s", ws_id)
        return jsonify({"success": False, "error": "failed to load settings"}), 500


@lead_growth_bp.route("/lead-growth/settings", methods=["PUT"])
def put_settings():
    """UPSERT the lead-growth settings for a workspace. Partial updates allowed."""
    body = request.get_json(silent=True) or {}
    ws_id, error = _resolve_workspace_id(body)
    if error is not None:
        return error

    try:
        saved = set_lead_growth_settings(ws_id, body)
        db.session.commit()
        return jsonify({"success": True, "workspace_id": ws_id, "settings": saved}), 200
    except ValueError as exc:
        db.session.rollback()
        return jsonify({"success": False, "error": str(exc)}), 400
    except Exception:
        db.session.rollback()
        logger.exception("[lead_growth_routes] put_settings failed ws=%s", ws_id)
        return jsonify({"success": False, "error": "failed to save settings"}), 500


@lead_growth_bp.route("/lead-growth/intents", methods=["GET"])
def list_intents():
    """Return the valid intent labels (and the high-intent auto-discovery subset)."""
    # Enforce tenant scope when a workspace_id is supplied (mirror the settings
    # endpoints). The payload is static, but rejecting cross-tenant requests keeps
    # the IDOR-hardening uniform across all three lead-growth endpoints.
    raw_ws = request.args.get("workspace_id")
    if raw_ws not in (None, ""):
        try:
            ws_id = int(raw_ws)
        except (TypeError, ValueError):
            return jsonify({"success": False, "error": "workspace_id must be an integer"}), 400
        denied = _require_workspace_scope(str(ws_id))
        if denied is not None:
            body, status = denied
            return jsonify(body), status

    try:
        intents = sorted(INTENT_KEYWORDS.keys())
        return (
            jsonify(
                {
                    "success": True,
                    "intents": intents,
                    "high_intent": sorted(_HIGH_INTENT),
                }
            ),
            200,
        )
    except Exception:
        logger.exception("[lead_growth_routes] list_intents failed")
        return jsonify({"success": False, "error": "failed to load intents"}), 500
