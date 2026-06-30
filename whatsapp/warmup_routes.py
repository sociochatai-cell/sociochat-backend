"""
Warmup HTTP API — state, diagnostics, restrictions; admin mutations (secret-gated).
"""

from __future__ import annotations

import logging
import os
from typing import Optional

from flask import Blueprint, jsonify, request

from shared_models import db

from .models import WhatsAppAccount
from .warmup_account_ops import (
    admin_override_lifecycle,
    build_public_state,
    extend_warmup_days,
    start_warmup_for_new_account,
)
from .warmup_engine import evaluate_warmup

logger = logging.getLogger(__name__)

warmup_bp = Blueprint("whatsapp_warmup", __name__)


def _admin_secret_ok() -> bool:
    secret = (os.getenv("WH_WARMUP_ADMIN_SECRET") or os.getenv("WHATSAPP_CAPABILITIES_UPSERT_SECRET") or "").strip()
    if not secret:
        return False
    auth = request.headers.get("Authorization", "")
    token = auth.split()[-1] if auth.lower().startswith("bearer ") else auth.strip()
    return token == secret


def _get_account_for_workspace(account_id: int, workspace_id: str) -> Optional[WhatsAppAccount]:
    if not workspace_id:
        return None
    acc = WhatsAppAccount.query.get(account_id)
    if not acc or str(acc.workspace_id or "") != str(workspace_id):
        return None
    return acc


@warmup_bp.route("/state", methods=["GET"])
def get_warmup_state():
    """GET /warmup/state?account_id=&workspace_id="""
    try:
        aid = int(request.args.get("account_id") or 0)
    except (TypeError, ValueError):
        return jsonify({"success": False, "error": "account_id required"}), 400
    wid = str(request.args.get("workspace_id") or "").strip()
    acc = _get_account_for_workspace(aid, wid)
    if not acc:
        return jsonify({"success": False, "error": "account_not_found"}), 404
    state = build_public_state(acc)
    return jsonify({"success": True, **state})


@warmup_bp.route("/restrictions", methods=["GET"])
def get_warmup_restrictions():
    """GET /warmup/restrictions?account_id=&workspace_id= — feature flags for UI."""
    try:
        aid = int(request.args.get("account_id") or 0)
    except (TypeError, ValueError):
        return jsonify({"success": False, "error": "account_id required"}), 400
    wid = str(request.args.get("workspace_id") or "").strip()
    acc = _get_account_for_workspace(aid, wid)
    if not acc:
        return jsonify({"success": False, "error": "account_not_found"}), 404
    st = build_public_state(acc)
    return jsonify({"success": True, "restrictions": st.get("restrictions", []), "in_warmup_window": st.get("in_warmup_window")})


@warmup_bp.route("/diagnostics", methods=["GET"])
def get_warmup_diagnostics():
    """GET /warmup/diagnostics?account_id=&workspace_id="""
    try:
        aid = int(request.args.get("account_id") or 0)
    except (TypeError, ValueError):
        return jsonify({"success": False, "error": "account_id required"}), 400
    wid = str(request.args.get("workspace_id") or "").strip()
    acc = _get_account_for_workspace(aid, wid)
    if not acc:
        return jsonify({"success": False, "error": "account_not_found"}), 404
    ev = evaluate_warmup(acc)
    return jsonify(
        {
            "success": True,
            "account_id": acc.id,
            "evaluation": {
                "lifecycle_effective": ev.lifecycle_effective,
                "in_warmup_window": ev.in_warmup_window,
                "advisory_safe_mode": ev.advisory_safe_mode,
                "safe_mode_reasons": ev.safe_mode_reasons,
                "effective_daily_send_cap": ev.effective_daily_send_cap,
                "blocks": ev.blocks,
                "countdown_seconds": ev.countdown_seconds,
                "diagnostics": ev.diagnostics,
            },
        }
    )


@warmup_bp.route("/admin/override", methods=["POST"])
def admin_override():
    if not _admin_secret_ok():
        return jsonify({"success": False, "error": "Unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    try:
        aid = int(data.get("account_id") or 0)
    except (TypeError, ValueError):
        return jsonify({"success": False, "error": "account_id required"}), 400
    target = str(data.get("target_lifecycle") or data.get("target") or "").strip()
    reason = str(data.get("reason") or "admin_override")[:2000]
    actor = str(data.get("actor") or "operator")[:128]
    acc = WhatsAppAccount.query.get(aid)
    if not acc:
        return jsonify({"success": False, "error": "account_not_found"}), 404
    try:
        admin_override_lifecycle(acc, target, reason=reason, actor=actor)
        db.session.commit()
    except ValueError as ve:
        return jsonify({"success": False, "error": str(ve)}), 400
    except Exception as e:
        logger.exception("admin_override: %s", e)
        db.session.rollback()
        return jsonify({"success": False, "error": str(e)}), 500
    return jsonify({"success": True, "account": acc.to_dict()})


@warmup_bp.route("/admin/extend", methods=["POST"])
def admin_extend():
    if not _admin_secret_ok():
        return jsonify({"success": False, "error": "Unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    try:
        aid = int(data.get("account_id") or 0)
        delta = int(data.get("extend_days") or data.get("delta_days") or 0)
    except (TypeError, ValueError):
        return jsonify({"success": False, "error": "account_id and extend_days required"}), 400
    actor = str(data.get("actor") or "operator")[:128]
    acc = WhatsAppAccount.query.get(aid)
    if not acc:
        return jsonify({"success": False, "error": "account_not_found"}), 404
    try:
        extend_warmup_days(acc, delta, actor=actor)
        db.session.commit()
    except Exception as e:
        logger.exception("admin_extend: %s", e)
        db.session.rollback()
        return jsonify({"success": False, "error": str(e)}), 500
    return jsonify({"success": True, "account": acc.to_dict()})


@warmup_bp.route("/admin/start-warmup", methods=["POST"])
def admin_start_warmup():
    """Operator-only: force-start warmup (e.g. missed hook)."""
    if not _admin_secret_ok():
        return jsonify({"success": False, "error": "Unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    try:
        aid = int(data.get("account_id") or 0)
    except (TypeError, ValueError):
        return jsonify({"success": False, "error": "account_id required"}), 400
    actor = str(data.get("actor") or "operator")[:128]
    acc = WhatsAppAccount.query.get(aid)
    if not acc:
        return jsonify({"success": False, "error": "account_not_found"}), 404
    start_warmup_for_new_account(acc, source=f"admin:{actor}")
    try:
        db.session.commit()
    except Exception as e:
        logger.exception("admin_start_warmup: %s", e)
        db.session.rollback()
        return jsonify({"success": False, "error": str(e)}), 500
    return jsonify({"success": True, "account": acc.to_dict()})
