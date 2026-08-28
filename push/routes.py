"""
Mobile push registration endpoints (additive, mobile-only).

  POST /api/push/register    -> save/update this device's Expo token
  POST /api/push/unregister  -> remove this device's token (on logout)

Auth uses the same get_current_user() as the rest of the app (session cookie or
signed Bearer JWT). The web never calls these, so it is unaffected.
"""

import logging
from flask import Blueprint, request, jsonify

from models import db
from push.models import PushDevice

logger = logging.getLogger(__name__)

push_bp = Blueprint("push", __name__)


def _current_user():
    from tenant.context import get_current_user
    return get_current_user()


@push_bp.route("/register", methods=["POST"])
def register_device():
    user = _current_user()
    if not user:
        return jsonify({"success": False, "error": "authentication_required"}), 401

    body = request.get_json(silent=True) or {}
    token = (body.get("token") or body.get("expo_token") or "").strip()
    platform = (body.get("platform") or "").strip() or None
    workspace_id = body.get("workspace_id")

    if not token:
        return jsonify({"success": False, "error": "token_required"}), 400

    try:
        # Upsert by token: one row per physical device. If the same device logs
        # in as a different user/workspace, we just update the owner.
        device = PushDevice.query.filter_by(expo_token=token).first()
        if device is None:
            device = PushDevice(expo_token=token)
            db.session.add(device)
        device.user_id = user.id
        device.workspace_id = workspace_id
        device.platform = platform
        db.session.commit()
        return jsonify({"success": True, "device": device.to_dict()})
    except Exception as e:
        db.session.rollback()
        logger.warning("push register failed: %s", e)
        return jsonify({"success": False, "error": "register_failed"}), 500


@push_bp.route("/unregister", methods=["POST"])
def unregister_device():
    user = _current_user()
    if not user:
        return jsonify({"success": False, "error": "authentication_required"}), 401

    body = request.get_json(silent=True) or {}
    token = (body.get("token") or body.get("expo_token") or "").strip()
    if not token:
        return jsonify({"success": False, "error": "token_required"}), 400

    try:
        PushDevice.query.filter_by(expo_token=token).delete()
        db.session.commit()
        return jsonify({"success": True})
    except Exception as e:
        db.session.rollback()
        logger.warning("push unregister failed: %s", e)
        return jsonify({"success": False, "error": "unregister_failed"}), 500
