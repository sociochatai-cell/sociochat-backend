"""
Onboarding session HTTP API (Embedded Signup orchestration).
"""

import logging
from flask import Blueprint, request, jsonify

from .models import OnboardingEvent, OnboardingSession
from .onboarding_session_manager import (
    create_session,
    get_session,
    get_active_session,
    append_embedded_event,
    resume_session,
    mark_abandoned,
    verify_resume,
    sweep_expired_sessions,
)

logger = logging.getLogger(__name__)

onboarding_bp = Blueprint("whatsapp_onboarding", __name__)


def _json():
    return request.get_json(silent=True) or {}


@onboarding_bp.route("/sessions", methods=["POST"])
def create_onboarding_session():
    """
    POST /onboarding/sessions
    Body: { workspace_id, user_id, onboarding_path?, onboarding_method?, is_coexistence?,
            embedded_signup_version?, graph_version?, sdk_version?, config_id? }
    """
    data = _json()
    wid = str(data.get("workspace_id") or "").strip()
    uid = str(data.get("user_id") or "").strip()
    if not wid or not uid:
        return jsonify({"success": False, "error": "workspace_id and user_id are required"}), 400
    try:
        row, resume_plain = create_session(
            workspace_id=wid,
            user_id=uid,
            onboarding_path=str(data.get("onboarding_path") or "embedded")[:40],
            onboarding_method=(data.get("onboarding_method") or data.get("onboarding_path") or None),
            is_coexistence=bool(data.get("is_coexistence")),
            embedded_signup_version=data.get("embedded_signup_version"),
            graph_version=data.get("graph_version"),
            sdk_version=data.get("sdk_version"),
            config_id=data.get("config_id"),
            correlation_id=data.get("correlation_id"),
        )
        return jsonify(
            {
                "success": True,
                "session": row.to_dict(),
                "resume_token": resume_plain,
                "correlation_id": row.correlation_id,
            }
        )
    except Exception as e:
        logger.exception("create_onboarding_session: %s", e)
        return jsonify({"success": False, "error": str(e)}), 500


@onboarding_bp.route("/sessions/active", methods=["GET"])
def active_onboarding_session():
    """GET /onboarding/sessions/active?workspace_id=&user_id="""
    wid = str(request.args.get("workspace_id") or "").strip()
    uid = str(request.args.get("user_id") or "").strip()
    if not wid or not uid:
        return jsonify({"success": False, "error": "workspace_id and user_id are required"}), 400
    row = get_active_session(wid, uid)
    return jsonify({"success": True, "session": row.to_dict() if row else None})


@onboarding_bp.route("/sessions/<session_id>", methods=["GET"])
def get_onboarding_session(session_id: str):
    """GET /onboarding/sessions/<uuid> — progress / status polling."""
    row = get_session(session_id)
    if not row:
        return jsonify({"success": False, "error": "session_not_found"}), 404
    events = (
        OnboardingEvent.query.filter_by(session_id=row.id)
        .order_by(OnboardingEvent.created_at.desc())
        .limit(50)
        .all()
    )
    return jsonify(
        {
            "success": True,
            "session": row.to_dict(),
            "recent_events": [
                {
                    "id": e.id,
                    "event_type": e.event_type,
                    "payload": e.payload,
                    "created_at": e.created_at.isoformat() if e.created_at else None,
                }
                for e in reversed(events)
            ],
        }
    )


@onboarding_bp.route("/sessions/<session_id>/events", methods=["POST"])
def post_onboarding_event(session_id: str):
    """
    POST /onboarding/sessions/<id>/events
    Body: { event_type, payload?, map_to_status? } — mirrors postMessage / SDK hooks.
    """
    row = get_session(session_id)
    if not row:
        return jsonify({"success": False, "error": "session_not_found"}), 404
    data = _json()
    et = str(data.get("event_type") or data.get("type") or "").strip()
    if not et:
        return jsonify({"success": False, "error": "event_type is required"}), 400
    try:
        out = append_embedded_event(
            row,
            event_type=et,
            payload=data.get("payload") if isinstance(data.get("payload"), dict) else None,
            map_to_status=(data.get("map_to_status") or None),
        )
        return jsonify(out)
    except ValueError as ve:
        return jsonify({"success": False, "error": str(ve), "session": row.to_dict()}), 400
    except Exception as e:
        logger.exception("post_onboarding_event: %s", e)
        return jsonify({"success": False, "error": str(e)}), 500


@onboarding_bp.route("/sessions/<session_id>/assets", methods=["POST"])
def post_onboarding_assets(session_id: str):
    """POST .../assets — explicit WABA / phone / business selection (ambiguity resolution)."""
    from .onboarding_session_manager import record_asset_hints, transition, ST_WABA_SELECTED, ST_PHONE_SELECTED

    row = get_session(session_id)
    if not row:
        return jsonify({"success": False, "error": "session_not_found"}), 404
    data = _json()
    try:
        record_asset_hints(
            row,
            business_manager_id=data.get("business_manager_id") or data.get("business_id"),
            waba_id=data.get("waba_id"),
            phone_number_id=data.get("phone_number_id"),
        )
        if data.get("waba_id"):
            transition(row, ST_WABA_SELECTED, last_step="waba_selected", log_type="onboarding_step_transition")
        if data.get("phone_number_id"):
            transition(row, ST_PHONE_SELECTED, last_step="phone_selected", log_type="onboarding_step_transition")
        return jsonify({"success": True, "session": row.to_dict()})
    except Exception as e:
        logger.exception("post_onboarding_assets: %s", e)
        return jsonify({"success": False, "error": str(e)}), 500


@onboarding_bp.route("/sessions/<session_id>/resume", methods=["POST"])
def post_onboarding_resume(session_id: str):
    """POST .../resume Body: { resume_token }"""
    row = get_session(session_id)
    if not row:
        return jsonify({"success": False, "error": "session_not_found"}), 404
    tok = str((_json()).get("resume_token") or "").strip()
    out = resume_session(row, tok)
    code = 200 if out.get("success") else 400
    return jsonify(out), code


@onboarding_bp.route("/sessions/<session_id>/abandon", methods=["POST"])
def post_onboarding_abandon(session_id: str):
    row = get_session(session_id)
    if not row:
        return jsonify({"success": False, "error": "session_not_found"}), 404
    reason = str(_json().get("reason") or "user_abandoned")
    mark_abandoned(row, reason)
    return jsonify({"success": True, "session": row.to_dict()})


@onboarding_bp.route("/sessions/<session_id>/verify", methods=["POST"])
def post_onboarding_verify(session_id: str):
    """Validate resume_token without mutating state."""
    row = get_session(session_id)
    if not row:
        return jsonify({"success": False, "error": "session_not_found"}), 404
    tok = str(_json().get("resume_token") or "").strip()
    ok = verify_resume(row, tok)
    return jsonify({"success": True, "valid": ok})


@onboarding_bp.route("/sessions/sweep-expired", methods=["POST"])
def post_sweep_expired():
    """Internal / operator — optional shared secret WH_ONBOARDING_SWEEP_SECRET."""
    import os

    secret = (os.getenv("WH_ONBOARDING_SWEEP_SECRET") or "").strip()
    if secret:
        auth = request.headers.get("Authorization", "")
        token = auth.split()[-1] if auth.lower().startswith("bearer ") else auth
        if token != secret:
            return jsonify({"success": False, "error": "Unauthorized"}), 401
    n = sweep_expired_sessions()
    return jsonify({"success": True, "expired_count": n})


@onboarding_bp.route("/meta-developer-config", methods=["GET"])
def meta_developer_config():
    """
    GET /api/whatsapp/onboarding/meta-developer-config

    Returns URLs and tokens to paste into Meta App Dashboard (developers.facebook.com).
    """
    import os
    from .provisioning_types import FULL_WEBHOOK_FIELDS

    app_id = os.getenv("FB_APP_ID") or os.getenv("META_APP_ID") or ""
    base_url = (os.getenv("APP_BASE_URL") or request.host_url).rstrip("/")
    verify_token = os.getenv("WHATSAPP_VERIFY_TOKEN") or ""
    frontend = os.getenv("FRONTEND_BASE_URL") or os.getenv("FRONTEND_ORIGIN") or ""

    webhook_callback = f"{base_url}/api/whatsapp/webhook"
    oauth_callback = f"{base_url}/api/whatsapp/connect/callback"

    return jsonify({
        "success": True,
        "meta_app_id": app_id,
        "app_dashboard": f"https://developers.facebook.com/apps/{app_id}/" if app_id else None,
        "whatsapp_configuration": (
            f"https://developers.facebook.com/apps/{app_id}/whatsapp-business/wa-settings/"
            if app_id else None
        ),
        "facebook_login_for_business": (
            f"https://developers.facebook.com/apps/{app_id}/fb-login/settings/"
            if app_id else None
        ),
        "embedded_signup": {
            "session_info_version": 4,
            "standard_feature_type": "",
            "coexistence_feature_type": "whatsapp_business_app_onboarding",
            "frontend_connect_path": f"{frontend}/dashboard/whatsapp/setup" if frontend else None,
        },
        "webhook": {
            "callback_url": webhook_callback,
            "verify_token": verify_token,
            "verify_token_configured": bool(verify_token),
            "test_url": f"{base_url}/api/whatsapp/webhook/test",
            "subscribed_fields": FULL_WEBHOOK_FIELDS,
            "coexistence_extra_fields": ["history", "smb_app_state_sync", "smb_message_echoes"],
        },
        "oauth_redirect_urls": [
            oauth_callback,
            f"{frontend}/dashboard/whatsapp/setup" if frontend else None,
            f"{frontend}/dashboard/whatsapp/coexistence" if frontend else None,
        ],
        "allowed_domains_for_js_sdk": [
            frontend.replace("https://", "") if frontend else None,
            "localhost",
        ],
        "api_endpoints": {
            "connect_exchange": f"{base_url}/api/whatsapp/connect/exchange",
            "coexistence_connect": f"{base_url}/api/whatsapp/coexistence/connect",
            "onboarding_sessions": f"{base_url}/api/whatsapp/onboarding/sessions",
        },
        "instructions": [
            "WhatsApp → Configuration: set Callback URL and Verify Token; subscribe to all listed webhook fields.",
            "Facebook Login for Business: add OAuth redirect URLs and enable Embedded Signup (config_id → VITE_WHATSAPP_CONFIG_ID).",
            "App Settings → Basic: add pre-prod domain to App Domains and Site URL.",
            "Embedded Signup v4: frontend sends WA_EMBEDDED_SIGNUP session events with waba_id / phone_number_id / business_id.",
        ],
    })

