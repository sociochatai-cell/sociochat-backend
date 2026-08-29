"""
Mobile push registration endpoints (additive, mobile-only).

  POST /api/push/register    -> save/update this device's Expo token
  POST /api/push/unregister  -> remove this device's token (on logout)

Auth uses the same get_current_user() as the rest of the app (session cookie or
signed Bearer JWT). The web never calls these, so it is unaffected.
"""

import logging
from flask import Blueprint, request, jsonify

from models import db, Workspace
from push.models import PushDevice, PushPref

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


@push_bp.route("/prefs", methods=["GET"])
def get_prefs():
    """List the user's workspaces with notification on/off state + coexistence flag.
    Used by the mobile Settings screen. `enabled` is the EFFECTIVE value
    (explicit pref, else coexistence->off / normal->on)."""
    user = _current_user()
    if not user:
        return jsonify({"success": False, "error": "authentication_required"}), 401
    try:
        from whatsapp.models import WhatsAppAccount
        workspaces = Workspace.query.filter_by(user_id=user.id).all()
        prefs = {p.workspace_id: p.enabled for p in PushPref.query.filter_by(user_id=user.id).all()}

        out = []
        for ws in workspaces:
            # A workspace is "coexistence" if any of its WA accounts is coexistence
            coex = (
                db.session.query(WhatsAppAccount.id)
                .filter_by(workspace_id=ws.id, is_coexistence=True)
                .first()
                is not None
            )
            if ws.id in prefs:
                enabled = bool(prefs[ws.id])
            else:
                enabled = not coex  # default: off for coexistence, on otherwise
            out.append({
                "workspace_id": ws.id,
                "name": ws.business_name or f"Workspace {ws.id}",
                "is_coexistence": coex,
                "enabled": enabled,
                "explicit": ws.id in prefs,
            })
        return jsonify({"success": True, "workspaces": out})
    except Exception as e:
        logger.warning("get_prefs failed: %s", e)
        return jsonify({"success": False, "error": "prefs_failed"}), 500


@push_bp.route("/prefs", methods=["POST"])
def set_pref():
    """Set the notification on/off toggle for one workspace (the Settings toggle)."""
    user = _current_user()
    if not user:
        return jsonify({"success": False, "error": "authentication_required"}), 401
    body = request.get_json(silent=True) or {}
    workspace_id = body.get("workspace_id")
    enabled = body.get("enabled")
    if workspace_id is None or enabled is None:
        return jsonify({"success": False, "error": "workspace_id_and_enabled_required"}), 400
    try:
        # Only allow toggling a workspace the user owns
        ws = Workspace.query.filter_by(id=workspace_id, user_id=user.id).first()
        if ws is None:
            return jsonify({"success": False, "error": "forbidden_workspace"}), 403
        pref = PushPref.query.filter_by(user_id=user.id, workspace_id=workspace_id).first()
        if pref is None:
            pref = PushPref(user_id=user.id, workspace_id=workspace_id)
            db.session.add(pref)
        pref.enabled = bool(enabled)
        db.session.commit()
        return jsonify({"success": True, "pref": pref.to_dict()})
    except Exception as e:
        db.session.rollback()
        logger.warning("set_pref failed: %s", e)
        return jsonify({"success": False, "error": "set_pref_failed"}), 500
