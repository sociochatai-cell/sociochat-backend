"""
WhatsApp Coexistence Routes
============================

API endpoints for WhatsApp Coexistence mode - allows businesses to keep
their existing WhatsApp mobile app active while also using the Cloud API
through Sociovia.

Endpoints:
    POST /api/whatsapp/coexistence/connect          - Connect via Embedded Signup (coexistence)
    POST /api/whatsapp/coexistence/pair              - Complete QR handshake pairing
    GET  /api/whatsapp/coexistence/status             - Get coexistence account status
    POST /api/whatsapp/coexistence/upgrade            - Upgrade from coexistence to standard
    GET  /api/whatsapp/coexistence/device-activity    - Device activity monitoring
    GET  /api/whatsapp/coexistence/history-sync       - History sync status
    POST /api/whatsapp/coexistence/subscribe-app      - Subscribe WABA to Sociovia app
"""

import os
import logging
import json
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, Optional

import requests as http_requests
from flask import Blueprint, request, jsonify

from shared_models import db
from .models import WhatsAppAccount, WhatsAppConversation, WhatsAppMessage
from .encryption import encrypt_token
from .connection_guard import check_phone_available
from .rate_limiter import WhatsAppRateLimiter
from .utils import subscribe_waba_to_app

logger = logging.getLogger(__name__)

coexistence_bp = Blueprint("coexistence", __name__, url_prefix="/api/whatsapp/coexistence")

META_API_VERSION = os.getenv("WHATSAPP_API_VERSION", "v22.0")
META_GRAPH = f"https://graph.facebook.com/{META_API_VERSION}"


def _coexistence_connect_error(exc: Exception) -> tuple[Dict[str, Any], int]:
    """Map connect failures to client-actionable HTTP status + error_code."""
    msg = str(exc)
    lower = msg.lower()
    if "token exchange failed" in lower:
        code = "OAUTH_CODE_EXPIRED" if "expired" in lower else "OAUTH_CODE_INVALID"
        return {
            "success": False,
            "error": msg,
            "error_code": code,
            "hint": (
                "Authorization codes expire in ~30 seconds. Click Connect again, "
                "finish the Meta popup promptly, and do not reuse an old code."
            ),
        }, 400
    if "multiple whatsapp assets" in lower:
        return {
            "success": False,
            "error": msg,
            "error_code": "AMBIGUOUS_WABA",
            "requires_user_choice": True,
            "hint": "Include business_id, waba_id, and phone_number_id from WA_EMBEDDED_SIGNUP session logging.",
        }, 409
    if "could not retrieve whatsapp" in lower or "no whatsapp business accounts" in lower:
        return {
            "success": False,
            "error": msg,
            "error_code": "WABA_DISCOVERY_FAILED",
            "hint": (
                "Finish the full Meta coexistence popup (WhatsApp Business app onboarding), "
                "not just Facebook login. Session logging must capture waba_id and phone_number_id."
            ),
        }, 400
    if "not a whatsapp embedded signup business token" in lower:
        return {
            "success": False,
            "error": msg,
            "error_code": "INVALID_EMBEDDED_SIGNUP_TOKEN",
            "hint": (
                "Meta returned a Facebook user session instead of a WhatsApp business token. "
                "Complete the coexistence flow in the popup, wait for it to finish, then retry."
            ),
        }, 400
    return {"success": False, "error": msg}, 500


# ============================================================
# Connect via Embedded Signup (Coexistence)
# ============================================================

@coexistence_bp.route("/connect", methods=["POST"])
def connect_coexistence():
    """
    Connect an existing WhatsApp Business Account in coexistence mode.
    
    This allows the user to keep their WhatsApp mobile app active while
    also using the Cloud API through Sociovia. The key difference from
    standard connection is:
    - is_coexistence = True
    - mps_limit = 5 (coexistence rate limit)
    - sync_status = 'syncing'
    
    POST /api/whatsapp/coexistence/connect
    Body: {
        "code": "auth_code_from_embedded_signup",
        "workspace_id": "123",
        "user_id": "456"  // optional
    }
    """
    data = request.get_json(silent=True) or {}
    code = (data.get("code") or "").strip()
    workspace_id = data.get("workspace_id")
    user_id = data.get("user_id")
    from .tech_provider_onboarding import (
        coexistence_next_steps,
        merge_connect_hints,
        payment_method_next_step,
    )

    ob_session = None
    onboarding_session_id = data.get("onboarding_session_id")
    resume_token = str(data.get("resume_token") or "").strip()
    if onboarding_session_id:
        from .onboarding_session_manager import get_session as _ob_get_session, verify_resume as _ob_verify_resume

        ob_session = _ob_get_session(str(onboarding_session_id))
        if not ob_session or str(ob_session.workspace_id) != str(workspace_id):
            return jsonify({"success": False, "error": "invalid_onboarding_session"}), 400
        if not _ob_verify_resume(ob_session, resume_token):
            return jsonify({"success": False, "error": "invalid_or_missing_resume_token"}), 401

    hints = merge_connect_hints(data, ob_session)
    is_coexistence_finish = bool(
        data.get("is_coexistence_finish")
        or data.get("embedded_signup_event") == "FINISH_WHATSAPP_BUSINESS_APP_ONBOARDING"
    )
    
    if not workspace_id:
        return jsonify({"success": False, "error": "workspace_id is required"}), 400

    if data.get("access_token") and not code:
        return jsonify({
            "success": False,
            "error": "Embedded Signup must return an authorization code, not a Facebook user access token.",
            "error_code": "EMBEDDED_SIGNUP_CODE_REQUIRED",
            "hint": (
                "Use FB.login with response_type=code and override_default_response_type=true. "
                "Do not send access_token to coexistence/connect — it cannot access WhatsApp Business assets."
            ),
        }), 400

    if not code:
        return jsonify({
            "success": False,
            "error": "Authorization code is required",
            "error_code": "EMBEDDED_SIGNUP_CODE_REQUIRED",
            "hint": "Finish the Meta Embedded Signup popup; FB.login must return authResponse.code.",
        }), 400

    from . import oauth as _wa_oauth

    # Resolve the tenant's own Meta app (per-field env fallback for T0000 /
    # unconfigured tenants). A real sub-tenant uses ITS OWN app_id/app_secret so
    # the token exchange + WABA discovery happen under the tenant's Meta app.
    try:
        from tenant.integration import get_tenant_meta_config
        cfg = get_tenant_meta_config(workspace_id=workspace_id)
        app_id = cfg.app_id or _wa_oauth.META_APP_ID
        app_secret = cfg.app_secret or _wa_oauth.META_APP_SECRET
    except Exception as cfg_err:
        logger.warning("coexistence/connect: tenant meta-config resolution failed (%s); using env", cfg_err)
        app_id = _wa_oauth.META_APP_ID
        app_secret = _wa_oauth.META_APP_SECRET

    if not app_id or not app_secret:
        return jsonify({
            "success": False,
            "error": "Meta app credentials not configured for coexistence/connect",
            "hint": "Set META_APP_ID + META_APP_SECRET or FB_APP_* on whatsapp-api.",
        }), 503
    
    try:
        if ob_session and hints:
            try:
                from .onboarding_session_manager import record_asset_hints

                record_asset_hints(
                    ob_session,
                    business_manager_id=hints.get("business_id"),
                    waba_id=hints.get("waba_id"),
                    phone_number_id=hints.get("phone_number_id"),
                )
            except Exception as hint_e:
                logger.warning("record_asset_hints during coexistence/connect: %s", hint_e)

        token_resp = http_requests.get(
            f"{META_GRAPH}/oauth/access_token",
            params={
                "client_id": app_id,
                "client_secret": app_secret,
                "code": code,
            },
            timeout=15,
        ).json()

        if "error" in token_resp:
            raise ValueError(f"Token exchange failed: {token_resp['error'].get('message', 'Unknown error')}")

        access_token = token_resp.get("access_token")
        if not access_token:
            raise ValueError("No access_token in response")

        # Keep Meta business integration token from Embedded Signup (do not fb_exchange by default)
        if os.getenv("WHATSAPP_EMBEDDED_SIGNUP_LONG_LIVED", "false").lower() == "true":
            try:
                long_token_resp = http_requests.get(
                    f"{META_GRAPH}/oauth/access_token",
                    params={
                        "grant_type": "fb_exchange_token",
                        "client_id": app_id,
                        "client_secret": app_secret,
                        "fb_exchange_token": access_token,
                    },
                    timeout=15,
                ).json()
                if "access_token" in long_token_resp:
                    access_token = long_token_resp["access_token"]
                    logger.info("Got long-lived token for coexistence")
            except Exception as e:
                logger.warning(f"Failed to get long-lived token: {e}")
        
        waba_id, phone_number_id, display_phone_number, verified_name, meta_business_id = \
            _discover_waba(access_token, app_id, app_secret, hints=hints or None)
        
        if not waba_id or not phone_number_id:
            raise ValueError(
                "Could not retrieve WhatsApp Business Account. "
                "Make sure you completed the signup flow and shared your WABA."
            )
        
        # GUARD: Block cross-workspace conflicts
        conflict = check_phone_available(phone_number_id, workspace_id)
        if conflict:
            return jsonify({
                "success": False,
                "error": conflict["error"],
                "error_code": conflict["error_code"]
            }), 409
        
        # Save or update account with coexistence flag
        existing = WhatsAppAccount.query.filter_by(phone_number_id=phone_number_id).first()
        existed_before = existing is not None

        if existing:
            existing.workspace_id = workspace_id
            existing.waba_id = waba_id
            existing.set_access_token(access_token, token_type="permanent")
            existing.is_active = True
            existing.is_coexistence = True
            existing.mps_limit = 20  # Coexistence limit (per Meta docs)
            existing.meta_business_id = meta_business_id
            existing.sync_status = "syncing"
            existing.display_phone_number = display_phone_number
            existing.verified_name = verified_name
            existing.connected_by_user_id = user_id
            existing.coexistence_paired_at = None
            account = existing
        else:
            account = WhatsAppAccount(
                workspace_id=workspace_id,
                waba_id=waba_id,
                phone_number_id=phone_number_id,
                display_phone_number=display_phone_number,
                verified_name=verified_name,
                connected_by_user_id=user_id,
                is_active=True,
                is_coexistence=True,
                mps_limit=20,
                meta_business_id=meta_business_id,
                sync_status="syncing",
                coexistence_paired_at=None,
            )
            account.set_access_token(access_token, token_type="permanent")
            db.session.add(account)
        
        db.session.commit()

        try:
            from .warmup_account_ops import ensure_mature_relink_skips_warmup, start_warmup_for_new_account

            if not existed_before:
                start_warmup_for_new_account(account, source="coexistence_signup")
            else:
                ensure_mature_relink_skips_warmup(account)
            db.session.commit()
        except Exception as w_e:
            logger.warning("warmup bootstrap coexistence connect: %s", w_e)
            db.session.rollback()
        
        logger.info(f"WhatsApp coexistence account connected: {phone_number_id} for workspace {workspace_id}")
        
        # Meta Step 2: subscribe webhooks (includes history, smb_app_state_sync, smb_message_echoes)
        subscribe_result = subscribe_waba_to_app(waba_id, access_token)
        subscribe_ok = bool(subscribe_result.get("success"))
        if not subscribe_ok:
            logger.warning("Coexistence webhook subscribe failed: %s", subscribe_result.get("error"))

        # Coexistence: skip phone registration — already on WhatsApp Business app
        logger.info(
            "Skipping phone registration for coexistence account %s (already registered via WA Business app)",
            phone_number_id,
        )

        onboarding_status = _check_coexistence_onboarding_status(phone_number_id, access_token)
        if onboarding_status.get("is_on_biz_app") and onboarding_status.get("platform_type") == "CLOUD_API":
            account.coexistence_paired_at = datetime.now(timezone.utc)
            db.session.commit()

        sync_results = _initiate_coexistence_sync(phone_number_id, access_token)
        sync_ok = bool(
            (sync_results.get("contacts") or {}).get("success")
            and (sync_results.get("history") or {}).get("success")
        )

        next_steps = coexistence_next_steps() + [payment_method_next_step()]
        finalize_ok = subscribe_ok and sync_ok

        if not finalize_ok:
            return jsonify({
                "success": False,
                "error": subscribe_result.get("error") or "Coexistence provisioning incomplete",
                "error_code": "COEXISTENCE_PROVISIONING_INCOMPLETE",
                "account": account.to_dict(),
                "webhook_subscribed": subscribe_ok,
                "sync": sync_results,
                "onboarding_status": onboarding_status,
                "is_coexistence_finish": is_coexistence_finish,
                "next_steps": next_steps,
            }), 422

        return jsonify({
            "success": True,
            "message": (
                "WhatsApp Business app connected in coexistence mode. "
                "Contacts and chat history sync have started — keep the mobile app open."
            ),
            "account": account.to_dict(),
            "webhook_subscribed": True,
            "sync": sync_results,
            "onboarding_status": onboarding_status,
            "is_coexistence_finish": is_coexistence_finish,
            "next_steps": next_steps,
        })
        
    except Exception as e:
        db.session.rollback()
        logger.exception("Coexistence connect error: %s", e)
        body, status = _coexistence_connect_error(e)
        if ob_session:
            body["onboarding_session_id"] = str(ob_session.id)
        return jsonify(body), status


# ============================================================
# QR Handshake Pairing
# ============================================================

@coexistence_bp.route("/pair", methods=["POST"])
def pair_coexistence():
    """
    Mark account as paired after QR handshake completion.
    The QR handshake happens on Meta's side - user scans QR in mobile app.
    
    POST /api/whatsapp/coexistence/pair
    Body: {
        "account_id": 123,
        "workspace_id": "456"
    }
    """
    data = request.get_json(silent=True) or {}
    account_id = data.get("account_id")
    workspace_id = data.get("workspace_id")
    
    if not account_id or not workspace_id:
        return jsonify({"success": False, "error": "account_id and workspace_id required"}), 400
    
    account = WhatsAppAccount.query.filter_by(
        id=account_id,
        workspace_id=workspace_id,
        is_coexistence=True,
    ).first()
    
    if not account:
        return jsonify({"success": False, "error": "Coexistence account not found"}), 404
    
    account.sync_status = "synced"
    account.coexistence_paired_at = datetime.now(timezone.utc)
    db.session.commit()
    
    return jsonify({
        "success": True,
        "message": "QR pairing confirmed",
        "account": account.to_dict(),
    })


# ============================================================
# Coexistence Status
# ============================================================

@coexistence_bp.route("/status", methods=["GET"])
def coexistence_status():
    """
    Get coexistence status for a workspace.
    
    GET /api/whatsapp/coexistence/status?workspace_id=123
    """
    workspace_id = request.args.get("workspace_id")
    if not workspace_id:
        return jsonify({"success": False, "error": "workspace_id required"}), 400
    
    accounts = WhatsAppAccount.query.filter_by(
        workspace_id=workspace_id,
        is_active=True,
    ).all()
    
    coex_accounts = [a for a in accounts if a.is_coexistence]
    standard_accounts = [a for a in accounts if not a.is_coexistence]
    
    # Device activity check
    device_alerts = []
    for acc in coex_accounts:
        if acc.last_echo_at:
            days_since_echo = (datetime.now(timezone.utc) - acc.last_echo_at.replace(tzinfo=timezone.utc)
                               if acc.last_echo_at.tzinfo is None
                               else datetime.now(timezone.utc) - acc.last_echo_at).days
            if days_since_echo > 10:
                device_alerts.append({
                    "account_id": acc.id,
                    "phone_number_id": acc.phone_number_id,
                    "display_phone_number": acc.display_phone_number,
                    "days_inactive": days_since_echo,
                    "severity": "critical" if days_since_echo > 20 else "warning",
                    "message": f"No mobile WhatsApp activity detected for {days_since_echo} days. "
                               f"Please ensure the WhatsApp mobile app is active to maintain coexistence."
                })
    
    return jsonify({
        "success": True,
        "coexistence_accounts": [a.to_dict() for a in coex_accounts],
        "standard_accounts": [a.to_dict() for a in standard_accounts],
        "device_alerts": device_alerts,
        "total_accounts": len(accounts),
    })


# ============================================================
# Upgrade from Coexistence to Standard
# ============================================================

@coexistence_bp.route("/upgrade", methods=["POST"])
def upgrade_to_standard():
    """
    Upgrade account from coexistence (5 MPS) to standard Cloud API (80-1000 MPS).
    This disconnects the mobile app.
    
    POST /api/whatsapp/coexistence/upgrade
    Body: {
        "account_id": 123,
        "workspace_id": "456"
    }
    """
    data = request.get_json(silent=True) or {}
    account_id = data.get("account_id")
    workspace_id = data.get("workspace_id")
    
    if not account_id or not workspace_id:
        return jsonify({"success": False, "error": "account_id and workspace_id required"}), 400
    
    account = WhatsAppAccount.query.filter_by(
        id=account_id,
        workspace_id=workspace_id,
        is_coexistence=True,
        is_active=True,
    ).first()
    
    if not account:
        return jsonify({"success": False, "error": "Coexistence account not found"}), 404
    
    # Register phone number to take over from mobile (this deactivates mobile app)
    access_token = account.get_access_token()
    if access_token:
        try:
            register_resp = http_requests.post(
                f"{META_GRAPH}/{account.phone_number_id}/register",
                json={
                    "messaging_product": "whatsapp",
                    "pin": "123456"
                },
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json"
                },
                timeout=15
            )
            logger.info(f"Phone registration for upgrade: {register_resp.json()}")
        except Exception as e:
            logger.warning(f"Phone registration for upgrade failed: {e}")
    
    # Update account: remove coexistence flags
    account.is_coexistence = False
    account.mps_limit = 80  # Standard Cloud API limit
    account.sync_status = "synced"
    
    db.session.commit()
    
    return jsonify({
        "success": True,
        "message": "Account upgraded to standard Cloud API. Mobile app has been disconnected.",
        "account": account.to_dict(),
    })


# ============================================================
# Device Activity Monitoring
# ============================================================

@coexistence_bp.route("/device-activity", methods=["GET"])
def device_activity():
    """
    Monitor mobile device activity for coexistence accounts.
    Checks last_echo_at timestamps to detect inactive devices.
    
    GET /api/whatsapp/coexistence/device-activity?workspace_id=123
    """
    workspace_id = request.args.get("workspace_id")
    if not workspace_id:
        return jsonify({"success": False, "error": "workspace_id required"}), 400
    
    accounts = WhatsAppAccount.query.filter_by(
        workspace_id=workspace_id,
        is_coexistence=True,
        is_active=True,
    ).all()
    
    results = []
    for acc in accounts:
        now = datetime.now(timezone.utc)
        last_echo = acc.last_echo_at
        if last_echo and last_echo.tzinfo is None:
            last_echo = last_echo.replace(tzinfo=timezone.utc)
        
        days_since = (now - last_echo).days if last_echo else None
        
        status = "active"
        if days_since is None:
            status = "unknown"
        elif days_since > 20:
            status = "critical"
        elif days_since > 10:
            status = "warning"
        elif days_since > 5:
            status = "attention"
        
        results.append({
            "account_id": acc.id,
            "phone_number_id": acc.phone_number_id,
            "display_phone_number": acc.display_phone_number,
            "verified_name": acc.custom_name or acc.verified_name,
            "last_echo_at": last_echo.isoformat() if last_echo else None,
            "days_since_echo": days_since,
            "device_status": status,
            "is_coexistence": acc.is_coexistence,
            "mps_limit": acc.mps_limit,
            "sync_status": acc.sync_status,
        })
    
    return jsonify({
        "success": True,
        "devices": results,
        "alerts_count": sum(1 for r in results if r["device_status"] in ("warning", "critical")),
    })


# ============================================================
# Initiate Sync (Contacts + History)
# ============================================================

@coexistence_bp.route("/initiate-sync", methods=["POST"])
def initiate_sync():
    """
    Manually trigger contacts + history sync for a coexistence account.
    Useful if the automatic sync after connect didn't complete.
    Must be done within 24 hours of onboarding.
    
    POST /api/whatsapp/coexistence/initiate-sync
    Body: { "account_id": 123, "workspace_id": "456" }
    """
    data = request.get_json(silent=True) or {}
    account_id = data.get("account_id")
    workspace_id = data.get("workspace_id")
    
    if not account_id or not workspace_id:
        return jsonify({"success": False, "error": "account_id and workspace_id required"}), 400
    
    account = WhatsAppAccount.query.filter_by(
        id=account_id,
        workspace_id=workspace_id,
        is_coexistence=True,
        is_active=True,
    ).first()
    
    if not account:
        return jsonify({"success": False, "error": "Coexistence account not found"}), 404
    
    access_token = account.get_access_token()
    if not access_token:
        return jsonify({"success": False, "error": "No access token available"}), 400
    
    results = _initiate_coexistence_sync(account.phone_number_id, access_token)
    
    return jsonify({
        "success": True,
        "message": "Sync initiated. Keep WhatsApp Business app open to facilitate sync.",
        "sync_results": results,
    })


# ============================================================
# History Sync Status
# ============================================================

@coexistence_bp.route("/history-sync", methods=["GET"])
def history_sync_status():
    """
    Get history sync status for coexistence accounts.
    After QR handshake, Meta sends up to 180 days of chat history.
    
    GET /api/whatsapp/coexistence/history-sync?workspace_id=123
    """
    workspace_id = request.args.get("workspace_id")
    if not workspace_id:
        return jsonify({"success": False, "error": "workspace_id required"}), 400
    
    accounts = WhatsAppAccount.query.filter_by(
        workspace_id=workspace_id,
        is_coexistence=True,
        is_active=True,
    ).all()
    
    results = []
    for acc in accounts:
        # Count synced conversations and messages
        conv_count = WhatsAppConversation.query.filter_by(account_id=acc.id).count()
        msg_count = WhatsAppMessage.query.join(WhatsAppConversation).filter(
            WhatsAppConversation.account_id == acc.id
        ).count()
        
        results.append({
            "account_id": acc.id,
            "phone_number_id": acc.phone_number_id,
            "display_phone_number": acc.display_phone_number,
            "sync_status": acc.sync_status,
            "history_sync_completed": acc.history_sync_completed,
            "conversations_count": conv_count,
            "messages_count": msg_count,
            "paired_at": acc.coexistence_paired_at.isoformat() if acc.coexistence_paired_at else None,
        })
    
    return jsonify({
        "success": True,
        "accounts": results,
    })


# ============================================================
# Subscribe WABA to Sociovia App
# ============================================================

@coexistence_bp.route("/subscribe-app", methods=["POST"])
def subscribe_app():
    """
    Subscribe a WABA to Sociovia's Meta App for webhook events.
    Required for receiving messages, statuses, and echoes.
    
    POST /api/whatsapp/coexistence/subscribe-app
    Body: { "account_id": 123, "workspace_id": "456" }
    """
    data = request.get_json(silent=True) or {}
    account_id = data.get("account_id")
    workspace_id = data.get("workspace_id")
    
    if not account_id or not workspace_id:
        return jsonify({"success": False, "error": "account_id and workspace_id required"}), 400
    
    account = WhatsAppAccount.query.filter_by(
        id=account_id,
        workspace_id=workspace_id,
        is_active=True,
    ).first()
    
    if not account:
        return jsonify({"success": False, "error": "Account not found"}), 404
    
    access_token = account.get_access_token()
    if not access_token:
        return jsonify({"success": False, "error": "No access token available"}), 400
    
    result = subscribe_waba_to_app(account.waba_id, access_token)
    
    return jsonify(result)


# ============================================================
# Rate Limit Check
# ============================================================

@coexistence_bp.route("/rate-limit", methods=["GET"])
def check_rate_limit():
    """
    Check rate limit status for an account.
    
    GET /api/whatsapp/coexistence/rate-limit?phone_number_id=xxx
    """
    phone_number_id = request.args.get("phone_number_id")
    if not phone_number_id:
        return jsonify({"success": False, "error": "phone_number_id required"}), 400
    
    account = WhatsAppAccount.query.filter_by(
        phone_number_id=phone_number_id,
        is_active=True,
    ).first()
    
    if not account:
        return jsonify({"success": False, "error": "Account not found"}), 404
    
    limiter = WhatsAppRateLimiter()
    available = limiter.check_available(phone_number_id)
    
    return jsonify({
        "success": True,
        "phone_number_id": phone_number_id,
        "is_coexistence": account.is_coexistence,
        "mps_limit": account.mps_limit,
        "tokens_available": available,
        "can_send": available > 0,
    })


# ============================================================
# Meta Error Monitoring
# ============================================================

@coexistence_bp.route("/errors", methods=["GET"])
def get_meta_errors():
    """
    Get recent Meta API errors for coexistence monitoring.
    
    Important error codes:
    - 130429: Rate limit exceeded
    - 131056: Pair rate limit exceeded  
    - 131000: Invalid token
    - 131031: Business account locked
    
    GET /api/whatsapp/coexistence/errors?workspace_id=123&limit=50
    """
    from .models import WhatsAppWebhookLog
    
    workspace_id = request.args.get("workspace_id")
    limit = int(request.args.get("limit", 50))
    
    if not workspace_id:
        return jsonify({"success": False, "error": "workspace_id required"}), 400
    
    # Get account phone_number_ids for this workspace
    accounts = WhatsAppAccount.query.filter_by(
        workspace_id=workspace_id,
        is_active=True,
    ).all()
    
    phone_ids = [a.phone_number_id for a in accounts]
    
    if not phone_ids:
        return jsonify({"success": True, "errors": [], "total": 0})
    
    # Query error logs
    error_logs = WhatsAppWebhookLog.query.filter(
        WhatsAppWebhookLog.phone_number_id.in_(phone_ids),
        WhatsAppWebhookLog.event_type == "error",
    ).order_by(
        WhatsAppWebhookLog.received_at.desc()
    ).limit(limit).all()
    
    # Also get failed message status events
    from .models import MessageStatusEvent
    failed_events = MessageStatusEvent.query.filter(
        MessageStatusEvent.status == "failed",
    ).order_by(
        MessageStatusEvent.created_at.desc()
    ).limit(limit).all()
    
    critical_codes = {"130429", "131056", "131000", "131031"}
    
    errors = []
    for log in error_logs:
        try:
            payload = json.loads(log.raw_json) if log.raw_json else {}
        except (json.JSONDecodeError, TypeError):
            payload = {}
        
        error_code = log.error_message or ""
        errors.append({
            "id": log.id,
            "type": "webhook_error",
            "phone_number_id": log.phone_number_id,
            "error_code": error_code,
            "is_critical": any(c in error_code for c in critical_codes),
            "timestamp": log.received_at.isoformat() if log.received_at else None,
        })
    
    for evt in failed_events:
        errors.append({
            "id": evt.id,
            "type": "message_failed",
            "wamid": evt.wamid,
            "error_code": evt.error_code,
            "error_message": evt.error_message,
            "is_critical": evt.error_code in critical_codes if evt.error_code else False,
            "timestamp": evt.created_at.isoformat() if evt.created_at else None,
        })
    
    # Sort by timestamp
    errors.sort(key=lambda x: x.get("timestamp") or "", reverse=True)
    
    return jsonify({
        "success": True,
        "errors": errors[:limit],
        "total": len(errors),
        "critical_count": sum(1 for e in errors if e.get("is_critical")),
    })


# ============================================================
# Helper Functions
# ============================================================

def _discover_waba(
    access_token: str,
    app_id: str,
    app_secret: str,
    hints: Optional[Dict[str, Optional[str]]] = None,
):
    """
    Discover WABA, phone number, and business id from access token (canonical).

    Returns: (waba_id, phone_number_id, display_phone_number, verified_name, meta_business_id)
    """
    from .meta_asset_discovery import DiscoveryAmbiguousError, resolve_binding_for_auto_connect
    from .tech_provider_onboarding import apply_hint_overrides

    try:
        binding, _disc = resolve_binding_for_auto_connect(
            access_token,
            api_version=META_API_VERSION,
            app_id=app_id,
            app_secret=app_secret,
            hints=hints,
            allow_legacy_single_guess=False,
        )
    except DiscoveryAmbiguousError as e:
        raise ValueError(
            "Multiple WhatsApp assets match this token. "
            "POST business_id, waba_id, and/or phone_number_id with the coexistence connect request."
        ) from e

    binding = apply_hint_overrides(binding, hints or {})

    return (
        binding["waba_id"],
        binding["phone_number_id"],
        binding.get("display_phone_number"),
        binding.get("verified_name"),
        binding.get("meta_business_id"),
    )


def _check_coexistence_onboarding_status(phone_number_id: str, access_token: str) -> Dict[str, Any]:
    """GET is_on_biz_app + platform_type — Meta coexistence onboarding check."""
    try:
        resp = http_requests.get(
            f"{META_GRAPH}/{phone_number_id}",
            params={
                "fields": "is_on_biz_app,platform_type,display_phone_number,verified_name",
                "access_token": access_token,
            },
            timeout=15,
        )
        if resp.status_code != 200:
            err = resp.json().get("error", {})
            return {"ok": False, "error": err.get("message", "status check failed")}
        body = resp.json()
        return {
            "ok": True,
            "is_on_biz_app": body.get("is_on_biz_app"),
            "platform_type": body.get("platform_type"),
            "onboarded": bool(body.get("is_on_biz_app")) and body.get("platform_type") == "CLOUD_API",
            "display_phone_number": body.get("display_phone_number"),
            "verified_name": body.get("verified_name"),
        }
    except Exception as e:
        logger.warning("coexistence onboarding status check failed: %s", e)
        return {"ok": False, "error": str(e)}


def _initiate_coexistence_sync(phone_number_id: str, access_token: str) -> dict:
    """
    Initiate contacts and history sync from WhatsApp Business app.
    
    Per Meta docs, after coexistence onboarding:
    1. POST /<phone_id>/smb_app_data with sync_type='smb_app_state_sync' (contacts)
    2. POST /<phone_id>/smb_app_data with sync_type='history' (chat history)
    
    Must be done within 24 hours of onboarding.
    """
    results = {"contacts": None, "history": None}
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }
    
    # Step 1: Initiate contacts sync
    try:
        resp = http_requests.post(
            f"{META_GRAPH}/{phone_number_id}/smb_app_data",
            json={
                "messaging_product": "whatsapp",
                "sync_type": "smb_app_state_sync",
            },
            headers=headers,
            timeout=15,
        )
        resp_data = resp.json()
        logger.info(f"Contacts sync initiation response: {resp_data}")
        results["contacts"] = {
            "success": "request_id" in resp_data or resp.ok,
            "request_id": resp_data.get("request_id"),
            "response": resp_data,
        }
    except Exception as e:
        logger.error(f"Failed to initiate contacts sync: {e}")
        results["contacts"] = {"success": False, "error": str(e)}
    
    # Step 2: Initiate history sync
    try:
        resp = http_requests.post(
            f"{META_GRAPH}/{phone_number_id}/smb_app_data",
            json={
                "messaging_product": "whatsapp",
                "sync_type": "history",
            },
            headers=headers,
            timeout=15,
        )
        resp_data = resp.json()
        logger.info(f"History sync initiation response: {resp_data}")
        results["history"] = {
            "success": "request_id" in resp_data or resp.ok,
            "request_id": resp_data.get("request_id"),
            "response": resp_data,
        }
    except Exception as e:
        logger.error(f"Failed to initiate history sync: {e}")
        results["history"] = {"success": False, "error": str(e)}
    
    return results


def _register_phone_number(phone_number_id: str, access_token: str):
    """Register phone number with WhatsApp Business API."""
    try:
        resp = http_requests.post(
            f"{META_GRAPH}/{phone_number_id}/register",
            json={
                "messaging_product": "whatsapp",
                "pin": "123456"
            },
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json"
            },
            timeout=15
        )
        logger.info(f"Phone registration response: {resp.json()}")
    except Exception as e:
        logger.warning(f"Phone registration failed (may already be registered): {e}")
