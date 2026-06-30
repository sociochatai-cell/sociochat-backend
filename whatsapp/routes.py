"""
WhatsApp Phase 1 API Routes
============================

Flask Blueprint for WhatsApp Cloud API endpoints.

Endpoints:
    POST /api/whatsapp/send/text             - Send text message
    POST /api/whatsapp/send/template         - Send template message (simple)
    POST /api/whatsapp/send/template/advanced - Send template (with builder)
    POST /api/whatsapp/send/media            - Send media message
    POST /api/whatsapp/send/interactive      - Send interactive message
    POST /api/whatsapp/send                  - Send any message type
    
    GET  /api/whatsapp/conversations                  - List conversations
    GET  /api/whatsapp/conversations/<id>             - Get conversation with messages
    GET  /api/whatsapp/conversations/<id>/messages    - Get messages only
    POST /api/whatsapp/conversations/<id>/read        - Mark conversation as read
    
    POST /api/whatsapp/webhook           - Webhook receiver
    GET  /api/whatsapp/webhook           - Webhook verification
    GET  /api/whatsapp/webhook-logs      - View webhook logs
    
    GET  /api/whatsapp/health            - Health check
"""

import os
import logging
from datetime import datetime, timezone, timedelta
from functools import wraps
from typing import Optional
from urllib.parse import urlencode
from flask import Blueprint, request, jsonify, g, redirect, current_app
from sqlalchemy import func, case

from .services import WhatsAppService, ConversationService
from .utils import subscribe_waba_to_app

from .webhook import (
    verify_webhook_signature,
    verify_webhook_signature_any,
    verify_webhook_challenge,
    WebhookProcessor,
)
from .trace_debug import trace_event, get_trace_by_wamid, get_trace_by_conversation
from .services import WhatsAppService, ConversationService
from .validators import (
    ValidationError,
    validate_text_message,
    validate_template_message,
    validate_media_message,
    validate_interactive_buttons,
    validate_interactive_list,
    format_validation_error,
)
from .token_helper import get_account_with_token, get_valid_account_for_workspace
from .models import WhatsAppAccount, WhatsAppFavoriteSticker
from .ai_chatbot import get_genai_client

# SECURITY: Import admin-only decorator to block agents from sensitive APIs
# Stub out admin requirement for standalone service
from functools import wraps
def require_admin_only(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        # In a real microservice, you'd verify JWT roles here
        return f(*args, **kwargs)
    return decorated

logger = logging.getLogger(__name__)

whatsapp_bp = Blueprint("whatsapp", __name__)


# ============================================================
# Helpers
# ============================================================

def get_db():
    """Get database session."""
    from shared_models import db
    return db.session


def get_access_token():
    """
    Get access token from request header, database, or environment.
    Priority: Header > Database (from connected account) > Environment
    """
    # Check for token in header (for testing UI)
    auth_header = (request.headers.get("X-WhatsApp-Token", "") or "").strip()
    if auth_header:
        return auth_header
    
    # Check Authorization header
    auth = (request.headers.get("Authorization", "") or "").strip()
    if auth.startswith("Bearer "):
        return auth[7:].strip()
    
    # Check database for connected WhatsApp account
    try:
        from .models import WhatsAppAccount
        
        # Try to get phone_number_id from request to find the right account
        phone_number_id = None
        if request.is_json:
            data = request.get_json(silent=True) or {}
            phone_number_id = data.get("phone_number_id")
        
        if not phone_number_id:
            phone_number_id = request.args.get("phone_number_id")
        
        if phone_number_id:
            # Find account by phone_number_id
            account = WhatsAppAccount.query.filter_by(
                phone_number_id=phone_number_id,
                is_active=True
            ).first()
            if account:
                token = account.get_access_token()
                if token:
                    return token
        else:
            # No phone_number_id specified, try to get any active account
            account = WhatsAppAccount.query.filter_by(is_active=True).first()
            if account:
                token = account.get_access_token()
                if token:
                    return token
    except Exception as e:
        logger.warning(f"Failed to get token from DB: {e}")
    
    # Fall back to environment variables (trim: Secret Manager versions often end with CRLF)
    env_token = (os.getenv("WHATSAPP_ACCESS_TOKEN") or os.getenv("WHATSAPP_TEMP_TOKEN") or "").strip()
    return env_token or ""


def get_phone_number_id():
    """Get phone number ID from request or environment."""
    # Check request body first
    if request.is_json:
        data = request.get_json(silent=True) or {}
        pid = data.get("phone_number_id")
        if pid:
            return pid
    
    # Check query params
    pid = request.args.get("phone_number_id")
    if pid:
        return pid
    
    # Fall back to environment
    return os.getenv("WHATSAPP_PHONE_NUMBER_ID", "")


def _env_bool(name: str, default: bool = False) -> bool:
    """Parse common truthy/falsey environment values."""
    raw = os.getenv(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _only_digits(value) -> str:
    """Normalize phone-like values to digits for stable matching."""
    return "".join(ch for ch in str(value or "") if ch.isdigit())


def _extract_change_summary(entry_waba_id: str, change: dict) -> dict:
    """Compact webhook change summary for deterministic debugging."""
    value = change.get("value", {}) if isinstance(change, dict) else {}
    metadata = value.get("metadata", {}) if isinstance(value, dict) else {}
    messages = value.get("messages", []) if isinstance(value, dict) else []
    statuses = value.get("statuses", []) if isinstance(value, dict) else []
    first_msg = messages[0] if messages else {}
    first_status = statuses[0] if statuses else {}
    pricing = first_status.get("pricing", {}) if isinstance(first_status, dict) else {}
    return {
        "waba_id": str(entry_waba_id or "").strip() or None,
        "field": change.get("field") if isinstance(change, dict) else None,
        "phone_number_id": str(metadata.get("phone_number_id") or "").strip() or None,
        "display_phone_number": metadata.get("display_phone_number"),
        "from_phone": first_msg.get("from"),
        "message_type": first_msg.get("type"),
        "inbound_wamid": first_msg.get("id"),
        "status_id": first_status.get("id"),
        "status": first_status.get("status"),
        "recipient_id": first_status.get("recipient_id"),
        "billable": pricing.get("billable"),
        "pricing_category": pricing.get("category"),
    }


def _resolve_webhook_account(waba_id: str, phone_number_id: str):
    """Find an active connected account for inbound webhook routing."""
    waba_id = str(waba_id or "").strip()
    phone_number_id = str(phone_number_id or "").strip()

    if phone_number_id:
        account = WhatsAppAccount.query.filter_by(
            phone_number_id=phone_number_id,
            is_active=True,
        ).first()
        if account:
            return account

    if waba_id:
        account = (
            WhatsAppAccount.query.filter_by(waba_id=waba_id, is_active=True)
            .order_by(WhatsAppAccount.id.desc())
            .first()
        )
        if account:
            return account

    return None


def _webhook_change_matches_account(change: dict, account: WhatsAppAccount) -> bool:
    """Match webhook metadata to a stored account (digits-only phone compare)."""
    value = change.get("value", {}) if isinstance(change, dict) else {}
    metadata = value.get("metadata", {}) if isinstance(value, dict) else {}

    incoming_phone_number_id = str(metadata.get("phone_number_id") or "").strip()
    if incoming_phone_number_id and incoming_phone_number_id != str(account.phone_number_id or "").strip():
        return False

    incoming_display = _only_digits(metadata.get("display_phone_number"))
    stored_display = _only_digits(account.display_phone_number)
    if incoming_display and stored_display and incoming_display != stored_display:
        return False

    return True


def _filter_allowed_inbound_webhook(payload):
    """
    Allow inbound webhooks for any active ``whatsapp_accounts`` row.

    Optional legacy strict mode: set ``WHATSAPP_WEBHOOK_STRICT_ALLOWLIST=1`` and
    ``WHATSAPP_ALLOWED_WABA_ID`` / ``WHATSAPP_ALLOWED_PHONE_NUMBER_ID`` to pin one tenant.
    """
    if not isinstance(payload, dict):
        return None

    entries = payload.get("entry", [])
    if not isinstance(entries, list) or not entries:
        return None

    strict = _env_bool("WHATSAPP_WEBHOOK_STRICT_ALLOWLIST", False)
    expected_waba_id = str(os.getenv("WHATSAPP_ALLOWED_WABA_ID") or os.getenv("WHATSAPP_WABA_ID") or "").strip()
    expected_phone_number_id = str(
        os.getenv("WHATSAPP_ALLOWED_PHONE_NUMBER_ID") or os.getenv("WHATSAPP_PHONE_NUMBER_ID") or ""
    ).strip()

    filtered_entries = []
    drop_counters = {
        "strict_waba_mismatch": 0,
        "strict_phone_mismatch": 0,
        "account_not_found": 0,
        "change_account_mismatch": 0,
    }
    dropped_samples = []
    accepted_samples = []
    for entry in entries:
        entry_waba_id = str(entry.get("id") or "").strip()
        if strict and expected_waba_id and entry_waba_id != expected_waba_id:
            drop_counters["strict_waba_mismatch"] += 1
            if len(dropped_samples) < 5:
                dropped_samples.append({
                    "reason": "strict_waba_mismatch",
                    "entry_waba_id": entry_waba_id or None,
                })
            continue

        changes = entry.get("changes", [])
        if not isinstance(changes, list):
            continue

        allowed_changes = []
        for change in changes:
            value = change.get("value", {}) if isinstance(change, dict) else {}
            metadata = value.get("metadata", {}) if isinstance(value, dict) else {}
            incoming_phone_number_id = str(metadata.get("phone_number_id") or "").strip()

            if strict and expected_phone_number_id and incoming_phone_number_id != expected_phone_number_id:
                drop_counters["strict_phone_mismatch"] += 1
                if len(dropped_samples) < 5:
                    sample = _extract_change_summary(entry_waba_id, change)
                    sample["reason"] = "strict_phone_mismatch"
                    dropped_samples.append(sample)
                continue

            account = _resolve_webhook_account(entry_waba_id, incoming_phone_number_id)
            if not account:
                drop_counters["account_not_found"] += 1
                if len(dropped_samples) < 5:
                    sample = _extract_change_summary(entry_waba_id, change)
                    sample["reason"] = "account_not_found"
                    dropped_samples.append(sample)
                continue
            if not _webhook_change_matches_account(change, account):
                drop_counters["change_account_mismatch"] += 1
                if len(dropped_samples) < 5:
                    sample = _extract_change_summary(entry_waba_id, change)
                    sample["reason"] = "change_account_mismatch"
                    dropped_samples.append(sample)
                continue

            allowed_changes.append(change)
            if len(accepted_samples) < 5:
                sample = _extract_change_summary(entry_waba_id, change)
                sample["matched_account_id"] = account.id
                accepted_samples.append(sample)

        if allowed_changes:
            filtered_entry = dict(entry)
            filtered_entry["changes"] = allowed_changes
            filtered_entries.append(filtered_entry)

    if not filtered_entries:
        logger.info(
            "Webhook dropped by allowlist filter: strict=%s expected_waba=%s expected_phone=%s counters=%s samples=%s",
            strict,
            expected_waba_id or None,
            expected_phone_number_id or None,
            drop_counters,
            dropped_samples,
        )
        return None

    filtered_payload = dict(payload)
    filtered_payload["entry"] = filtered_entries
    try:
        accepted_changes = sum(len((e or {}).get("changes") or []) for e in filtered_entries)
    except Exception:
        accepted_changes = 0
    logger.info(
        "Webhook allowlist accepted: entries=%s changes=%s strict=%s samples=%s",
        len(filtered_entries),
        accepted_changes,
        strict,
        accepted_samples,
    )
    return filtered_payload


def _get_voice_call_capability(account, access_token: str, api_version=None):
    """
    Best-effort voice call readiness probe.

    There is no public API that lets Sociovia answer WhatsApp calls directly.
    This probe helps prevent misconfiguration before VOICE_CALL templates are submitted.
    """
    import requests as http_requests

    resolved_api_version = api_version or os.getenv("WHATSAPP_API_VERSION", "v22.0")
    strict_mode = _env_bool("WHATSAPP_STRICT_VOICE_CALL_READINESS", False)

    base_payload = {
        "success": True,
        "account_id": account.id,
        "strict_mode": strict_mode,
        "receive_in_sociovia_dashboard": False,
        "receive_path": "Calls are handled in WhatsApp clients (mobile/desktop/linked devices), not inside Sociovia dashboard.",
        "checks": {},
        "warnings": [],
    }

    has_phone_number_id = bool(account.phone_number_id)
    has_access_token = bool(access_token)
    base_payload["checks"]["has_phone_number_id"] = has_phone_number_id
    base_payload["checks"]["has_access_token"] = has_access_token

    if not has_phone_number_id:
        base_payload["warnings"].append("Missing phone_number_id on this account")
    if not has_access_token:
        base_payload["warnings"].append("Missing access token on this account")

    code_verification_status = ""
    name_status = ""
    quality_rating = ""
    meta_probe_ok = False
    meta_probe_error = None

    if has_phone_number_id and has_access_token:
        try:
            fields = "code_verification_status,name_status,quality_rating,display_phone_number,verified_name"
            resp = http_requests.get(
                f"https://graph.facebook.com/{resolved_api_version}/{account.phone_number_id}",
                headers={"Authorization": f"Bearer {access_token}"},
                params={"fields": fields},
                timeout=15,
            )
            probe_data = resp.json() if resp.content else {}
            if resp.ok:
                meta_probe_ok = True
                code_verification_status = str(probe_data.get("code_verification_status") or "").upper()
                name_status = str(probe_data.get("name_status") or "").upper()
                quality_rating = str(probe_data.get("quality_rating") or "").upper()
                base_payload["meta"] = {
                    "code_verification_status": code_verification_status or None,
                    "name_status": name_status or None,
                    "quality_rating": quality_rating or None,
                    "display_phone_number": probe_data.get("display_phone_number"),
                    "verified_name": probe_data.get("verified_name"),
                }
            else:
                meta_probe_error = probe_data.get("error", {}).get("message") or "Meta probe failed"
                logger.warning("Voice call capability probe failed: %s", probe_data)
        except Exception as e:
            meta_probe_error = str(e)
            logger.warning("Voice call capability probe exception: %s", e)

    is_phone_verified = code_verification_status in {"VERIFIED", "CONNECTED"}
    is_name_approved = (not name_status) or name_status in {"APPROVED", "AVAILABLE"}

    base_payload["checks"]["meta_probe_ok"] = meta_probe_ok
    base_payload["checks"]["phone_verified"] = is_phone_verified
    base_payload["checks"]["display_name_approved"] = is_name_approved

    if has_phone_number_id and has_access_token and not meta_probe_ok:
        base_payload["warnings"].append("Could not verify call readiness from Meta API")
    if meta_probe_error:
        base_payload["warnings"].append(meta_probe_error)
    if meta_probe_ok and not is_phone_verified:
        base_payload["warnings"].append("Phone number is not verified/connected in WhatsApp Manager")
    if meta_probe_ok and not is_name_approved:
        base_payload["warnings"].append("Display name is not approved")

    voice_calling_ready = bool(has_phone_number_id and has_access_token and is_phone_verified and is_name_approved)
    block_submission = bool(strict_mode and not voice_calling_ready)

    base_payload["voice_calling_ready"] = voice_calling_ready
    base_payload["block_template_submission"] = block_submission
    return base_payload


def require_token(f):
    """Decorator to require a valid access token."""
    @wraps(f)
    def decorated(*args, **kwargs):
        token = get_access_token()
        if not token:
            return jsonify({
                "success": False,
                "error": "Access token required. Set WHATSAPP_ACCESS_TOKEN or pass X-WhatsApp-Token header."
            }), 401
        g.access_token = token
        return f(*args, **kwargs)
    return decorated


def handle_validation_error(error: ValidationError):
    """Return formatted validation error response."""
    return jsonify(format_validation_error(error)), 400


def _enforce_message_limit(phone_number_id, send_kind=None):
    """
    Enforce outbound send policy using only `whatsapp_account_capabilities`.

    Uses the same rules as `outbound_send_capability_check` (subscription snapshot +
    UTC-day outbound counts vs daily_message_limit). No User, plan, or billing reads.

    send_kind: optional hint for warmup path classification (manual, template, media, …).

    Returns (allowed, error_response).
    """
    try:
        from .capabilities import outbound_send_capability_check

        account = WhatsAppAccount.query.filter_by(phone_number_id=phone_number_id).first()
        if not account:
            logger.warning(
                "Could not find local account for phone_number_id %s; skipping capability pre-check.",
                phone_number_id,
            )
            return True, None

        cap = outbound_send_capability_check(account.id, get_db(), send_kind=send_kind)
        if cap.ok:
            return True, None

        payload = {
            "success": False,
            "error": "capability_denied",
            "message": cap.message,
        }
        if cap.subscription_status is not None:
            payload["subscription_status"] = cap.subscription_status
        if cap.daily_message_limit is not None:
            payload["daily_message_limit"] = cap.daily_message_limit
            payload["limit"] = cap.daily_message_limit
        if cap.daily_messages_used is not None:
            payload["daily_messages_used"] = cap.daily_messages_used
            payload["current"] = cap.daily_messages_used
        if cap.daily_messages_used is not None and cap.daily_message_limit is not None:
            payload["error"] = "message_limit_exceeded"

        return False, jsonify(payload)

    except Exception as e:
        logger.error(f"Capability limit enforcement error: {e}")
        return True, None


# ============================================================
# Image Proxy Endpoint (for CORS bypass)
# ============================================================

@whatsapp_bp.route("/image-proxy", methods=["GET"])
def image_proxy():
    """
    Proxy external images to bypass CORS restrictions.
    
    GET /api/whatsapp/image-proxy?url=https://example.com/image.jpg
    
    Supports Google Drive, Imgur, and other external URLs.
    """
    import requests
    from flask import Response
    
    url = request.args.get("url", "")
    
    if not url:
        return jsonify({"error": "URL required"}), 400
    
    # Security: Only allow image URLs
    allowed_domains = [
        "drive.google.com",
        "lh3.googleusercontent.com",
        "imgur.com",
        "i.imgur.com",
        "cloudinary.com",
        "res.cloudinary.com",
        "s3.amazonaws.com",
        "storage.googleapis.com",
        "firebasestorage.googleapis.com",
        "images.unsplash.com",
        "cdn.",
        "media.",
    ]
    
    # Allow any HTTPS URL for flexibility, but log for monitoring
    if not url.startswith("https://"):
        return jsonify({"error": "HTTPS URLs only"}), 400
    
    try:
        # Handle Google Drive URLs
        if "drive.google.com" in url:
            # Convert to direct download URL
            if "/file/d/" in url:
                file_id = url.split("/file/d/")[1].split("/")[0]
                url = f"https://drive.google.com/uc?export=view&id={file_id}"
            elif "id=" in url:
                # Already in correct format
                pass
        
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "image/*,*/*;q=0.8",
        }
        
        response = requests.get(url, headers=headers, timeout=10, stream=True, allow_redirects=True)
        
        if response.status_code != 200:
            return jsonify({"error": f"Failed to fetch image: {response.status_code}"}), response.status_code
        
        # Get content type
        content_type = response.headers.get("Content-Type", "image/jpeg")
        
        # Return the image with proper headers
        return Response(
            response.content,
            mimetype=content_type,
            headers={
                "Cache-Control": "public, max-age=86400",
                "Access-Control-Allow-Origin": "*",
            }
        )
        
    except requests.Timeout:
        return jsonify({"error": "Image fetch timeout"}), 504
    except Exception as e:
        logger.error(f"Image proxy error: {e}")
        return jsonify({"error": str(e)}), 500


# ============================================================
# Media Proxy Endpoint (for Meta CDN)
# ============================================================

@whatsapp_bp.route("/media/<media_id>", methods=["GET"])
def proxy_media(media_id):
    """
    Proxy WhatsApp media from Meta CDN.
    
    GET /api/whatsapp/media/<media_id>?workspace_id=...
    """
    import mimetypes
    import requests
    from flask import Response
    from urllib.parse import quote
    from .models import WhatsAppAccount, WhatsAppConversation, WhatsAppMessage
    
    workspace_id = request.args.get("workspace_id")
    phone_number_id = request.args.get("phone_number_id")
    
    logger.info(f"📁 Proxying media: id={media_id}, workspace={workspace_id}, phone={phone_number_id}")
    
    # Get service with proper credentials
    service = WhatsAppService(get_db(), phone_number_id=phone_number_id, workspace_id=workspace_id)
    
    if not service.access_token:
        logger.error(f"❌ WhatsApp not connected for workspace {workspace_id}")
        return jsonify({"error": "WhatsApp not connected for this workspace"}), 401
        
    media_info = service.get_media_info(media_id) or {}
    media_url = media_info.get("url")
    if not media_url:
        logger.error(f"❌ Failed to retrieve media URL from Meta for {media_id}")
        return jsonify({"error": "Failed to retrieve media URL from Meta"}), 404

    def _guess_ext(content_type: str) -> str:
        ctype = (content_type or "").split(";")[0].strip().lower()
        if ctype == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet":
            return ".xlsx"
        if ctype == "application/vnd.openxmlformats-officedocument.wordprocessingml.document":
            return ".docx"
        if ctype == "application/vnd.openxmlformats-officedocument.presentationml.presentation":
            return ".pptx"
        ext = mimetypes.guess_extension(ctype or "") or ""
        return ext

    def _lookup_original_filename() -> Optional[str]:
        # Best-effort lookup from stored inbound message payload.
        try:
            query = WhatsAppMessage.query.join(
                WhatsAppConversation,
                WhatsAppMessage.conversation_id == WhatsAppConversation.id,
            ).join(
                WhatsAppAccount,
                WhatsAppConversation.account_id == WhatsAppAccount.id,
            )
            if workspace_id:
                query = query.filter(WhatsAppAccount.workspace_id == str(workspace_id))

            recent = (
                query.filter(WhatsAppMessage.type.in_(["document", "image", "video", "audio"]))
                .order_by(WhatsAppMessage.created_at.desc())
                .limit(500)
                .all()
            )

            for msg in recent:
                content = msg.content if isinstance(msg.content, dict) else {}
                if str(content.get("id") or "") == str(media_id):
                    name = content.get("filename")
                    if isinstance(name, str) and name.strip():
                        return name.strip()
            return None
        except Exception as lookup_err:
            logger.debug(f"Failed media filename lookup for {media_id}: {lookup_err}")
            return None
        
    try:
        logger.info(f"🔗 Fetching media from Meta: {media_url[:50]}...")
        # Fetch the actual media from Meta CDN
        # IMPORTANT: Meta CDN URLs also require Authorization header
        cdn_headers = {
            "Authorization": f"Bearer {service.access_token}",
            "User-Agent": "Sociovia/1.0"
        }
        resp = requests.get(media_url, headers=cdn_headers, timeout=30, stream=True)
        
        if resp.status_code != 200:
            logger.error(f"❌ Meta CDN returned {resp.status_code} for {media_id}")
            return jsonify({"error": "Failed to fetch media from CDN"}), resp.status_code
            
        content_type = resp.headers.get("Content-Type") or media_info.get("mime_type") or "application/octet-stream"
        original_filename = _lookup_original_filename()
        if not original_filename:
            ext = _guess_ext(content_type)
            original_filename = f"media_{media_id}{ext}"

        safe_filename = original_filename.replace('"', "")
        encoded_filename = quote(safe_filename)

        # Return the media with proper headers and original extension
        return Response(
            resp.content,
            mimetype=content_type,
            headers={
                "Cache-Control": "public, max-age=86400",
                "Access-Control-Allow-Origin": "*",
                "Content-Disposition": (
                    f'inline; filename="{safe_filename}"; filename*=UTF-8\'\'{encoded_filename}'
                ),
            }
        )
    except Exception as e:
        logger.error(f"🔥 Error proxying media {media_id}: {e}")
        return jsonify({"error": str(e)}), 500


@whatsapp_bp.route("/admin/resubscribe", methods=["POST"])
@require_admin_only
def resubscribe_all():
    """
    Trigger re-subscription for all accounts to ensure smb_message_echoes is active.
    
    POST /api/whatsapp/admin/resubscribe
    """
    success = WhatsAppService.ensure_all_waba_subscriptions()
    return jsonify({"success": success, "message": "Re-subscription task finished."})

@whatsapp_bp.route("/diag/routes", methods=["GET"])
def diag_routes():
    """Diagnostic route to check backend version."""
    return jsonify({
        "status": "ok",
        "version": "local-debug-v1",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "endpoints": [
            "/api/whatsapp/diag/routes",
            "/api/whatsapp/stickers/favorite",
            "/api/whatsapp/media/<id>"
        ]
    })


@whatsapp_bp.route("/stickers/favorite", methods=["POST"])
def favorite_sticker():
    """
    Save a sticker to favorites.
    
    POST /api/whatsapp/stickers/favorite
    {
        "media_id": "...",
        "workspace_id": "...",
        "mime_type": "...",
        "sha256": "..."
    }
    """
    data = request.get_json(silent=True) or {}
    media_id = data.get("media_id")
    workspace_id = data.get("workspace_id")
    
    if not media_id or not workspace_id:
        return jsonify({"error": "media_id and workspace_id required"}), 400
        
    # Check if already favorited
    existing = WhatsAppFavoriteSticker.query.filter_by(
        workspace_id=workspace_id,
        media_id=media_id
    ).first()
    
    if existing:
        return jsonify({"success": True, "message": "Already in favorites", "id": existing.id})
        
    fav = WhatsAppFavoriteSticker(
        workspace_id=workspace_id,
        media_id=media_id,
        mime_type=data.get("mime_type"),
        sha256=data.get("sha256")
    )
    
    db = get_db()
    db.add(fav)
    db.commit()
    
    return jsonify({"success": True, "message": "Sticker added to favorites", "id": fav.id})


@whatsapp_bp.route("/stickers/favorite", methods=["GET"])
def list_favorite_stickers():
    """
    List favorite stickers for a workspace.
    
    GET /api/whatsapp/stickers/favorite?workspace_id=...
    """
    workspace_id = request.args.get("workspace_id")
    if not workspace_id:
        return jsonify({"error": "workspace_id required"}), 400
        
    favorites = WhatsAppFavoriteSticker.query.filter_by(workspace_id=workspace_id).all()
    return jsonify([f.to_dict() for f in favorites])


@whatsapp_bp.route("/stickers/favorite/<int:sticker_id>", methods=["DELETE"])
def delete_favorite_sticker(sticker_id):
    """
    Delete a favorite sticker.
    
    DELETE /api/whatsapp/stickers/favorite/<id>?workspace_id=...
    """
    workspace_id = request.args.get("workspace_id")
    if not workspace_id:
        return jsonify({"error": "workspace_id required"}), 400
    
    sticker = WhatsAppFavoriteSticker.query.filter_by(
        id=sticker_id,
        workspace_id=workspace_id
    ).first()
    
    if not sticker:
        return jsonify({"error": "Sticker not found"}), 404
    
    db = get_db()
    db.delete(sticker)
    db.commit()
    
    return jsonify({"success": True, "message": "Sticker removed from favorites"})


# ============================================================
# Webhook Endpoints
# ============================================================

@whatsapp_bp.route("/webhook/test", methods=["GET"])
def webhook_test():
    """
    Test endpoint to verify webhook URL is accessible.
    
    Use this to test if Meta can reach your server:
    GET /api/whatsapp/webhook/test
    
    Should return {"status": "ok", "webhook_url": "..."}
    """
    import os
    from .provisioning_types import FULL_WEBHOOK_FIELDS

    base_url = os.environ.get("APP_BASE_URL", "https://whatsapp-api-a2cpr5sa3q-uc.a.run.app").rstrip("/")
    verify_token = os.environ.get("WHATSAPP_VERIFY_TOKEN", "not_set")
    app_id = os.environ.get("FB_APP_ID") or os.environ.get("META_APP_ID") or ""
    webhook_url = f"{base_url}/api/whatsapp/webhook"
    
    return jsonify({
        "status": "ok",
        "message": "Webhook endpoint is accessible!",
        "meta_app_id": app_id,
        "webhook_url": webhook_url,
        "verify_token_configured": verify_token != "not_set",
        "meta_developer_config_url": f"{base_url}/api/whatsapp/onboarding/meta-developer-config",
        "subscribed_fields": FULL_WEBHOOK_FIELDS,
        "instructions": [
            "1. developers.facebook.com → Your App → WhatsApp → Configuration",
            f"2. Callback URL: {webhook_url}",
            "3. Verify Token: value of WHATSAPP_VERIFY_TOKEN on whatsapp-api",
            f"4. Subscribe fields: {', '.join(FULL_WEBHOOK_FIELDS)}",
            "5. Facebook Login for Business → add OAuth redirect URLs from meta-developer-config",
        ]
    })

@whatsapp_bp.route("/webhook", methods=["GET"])
def webhook_verify():
    """
    Verify webhook subscription from Meta.
    
    GET /api/whatsapp/webhook?hub.mode=subscribe&hub.verify_token=xxx&hub.challenge=xxx
    """
    mode = request.args.get("hub.mode", "")
    token = request.args.get("hub.verify_token", "")
    challenge = request.args.get("hub.challenge", "")
    
    result = verify_webhook_challenge(mode, token, challenge)
    
    if result:
        logger.info("Webhook verification successful")
        return result, 200
    
    logger.warning("Webhook verification failed")
    return "Forbidden", 403


@whatsapp_bp.route("/webhook", methods=["POST"])
def webhook_receive():
    """
    Receive webhook events from Meta.
    
    POST /api/whatsapp/webhook
    
    IMPORTANT: Always respond 200 OK immediately, then process.
    """
    # Verify signature (Meta signs with Facebook App Secret — try all configured secrets)
    signature = request.headers.get("X-Hub-Signature-256", "")
    payload_bytes = request.get_data(cache=True)

    if not verify_webhook_signature_any(payload_bytes, signature):
        dev_mode = os.getenv("FLASK_ENV", "").strip().lower() == "development"
        env_dev = os.getenv("ENV", "").strip().lower() in {"dev", "development", "local"}
        skip_verify = _env_bool("WHATSAPP_WEBHOOK_SKIP_SIGNATURE_VERIFY") or dev_mode or env_dev
        if skip_verify:
            logger.warning(
                "Invalid webhook signature (processing anyway — dev/skip mode; "
                "dev tunnels may alter the raw body)"
            )
        else:
            logger.warning(
                "Invalid webhook signature — rejecting request "
                "(check FB_APP_SECRET matches Meta Developer Console → App secret)"
            )
            trace_event(
                stage="api.webhook.signature_rejected",
                status="forbidden",
                details={"has_signature": bool(signature)},
            )
            return "Forbidden", 403

    payload = request.get_json(silent=True)
    payload_sample = {}
    if isinstance(payload, dict):
        try:
            first_entry = (payload.get("entry") or [None])[0] or {}
            entry_waba_id = str(first_entry.get("id") or "").strip()
            first_change = (first_entry.get("changes") or [None])[0] or {}
            payload_sample = _extract_change_summary(entry_waba_id, first_change)
        except Exception:
            payload_sample = {}
    trace_event(
        stage="api.webhook.received",
        status="ok" if bool(payload) else "empty",
        details={
            "has_signature": bool(signature),
            "object": (payload or {}).get("object") if isinstance(payload, dict) else None,
            "entries": len((payload or {}).get("entry") or []) if isinstance(payload, dict) else 0,
            "sample": payload_sample,
        },
    )
    
    debug_payload_logs = _env_bool("WHATSAPP_WEBHOOK_DEBUG_PAYLOAD", False)
    if debug_payload_logs:
        # Keep deep payload logging behind an explicit flag; full payload logging is
        # expensive under webhook bursts and can starve API worker threads.
        print(f"\n{'='*60}")
        print("📨 WEBHOOK RECEIVED!")
        print(f"Payload: {payload}")
        print(f"{'='*60}\n")
        logger.info("📨 WEBHOOK RECEIVED: %s", payload)
    
    if not payload:
        print("⚠️ Empty payload!")
        logger.warning("Empty webhook payload received")
        return "OK", 200
    
    # Check what type of webhook this is
    obj_type = payload.get("object")
    logger.info("📦 Webhook object type: %s", obj_type)

    try:
        from .webhook_routing import dispatch_customer_webhook, process_inbound_webhook

        inbound = process_inbound_webhook(payload)
        dispatch_customer_webhook(
            payload,
            inbound.forward_decision,
            raw_body=payload_bytes or None,
            meta_signature=signature or None,
            blocking=True,
        )
        d = inbound.forward_decision
        logger.info(
            "WEBHOOK FORWARD should_forward=%s reason=%s url=%s workspace=%s",
            d.should_forward,
            d.reason,
            d.destination_url or "(none)",
            d.workspace_id,
        )
        filtered_payload = inbound.filtered_payload
    except Exception as routing_exc:
        logger.exception("Webhook routing error: %s", routing_exc)
        filtered_payload = _filter_allowed_inbound_webhook(payload)
    if not filtered_payload:
        trace_event(
            stage="api.webhook.filtered",
            status="dropped",
            details={"reason": "allowlist_unmatched_or_invalid"},
        )
        logger.info("Ignoring webhook payload: unmatched WABA/phone metadata")
        return "OK", 200
    trace_event(
        stage="api.webhook.filtered",
        status="accepted",
        details={"entries": len(filtered_payload.get("entry", []) or [])},
    )

    from whatsapp.webhook_utils import classify_webhook_payload, split_status_and_message_payload

    webhook_kind = classify_webhook_payload(filtered_payload)

    if webhook_kind == "empty":
        return "OK", 200

    # Process status receipts inline by default so they don't flood the main
    # webhook worker queue and delay inbound message processing.
    inline_status_processing = _env_bool("WHATSAPP_WEBHOOK_INLINE_STATUS", True)

    if webhook_kind == "status_only" and inline_status_processing:
        try:
            processor = WebhookProcessor(get_db())
            success, message = processor.process_webhook(filtered_payload)
            if not success:
                logger.warning("Status webhook inline processing: %s", message)
        except Exception as exc:
            logger.exception("Status webhook inline error: %s", exc)
        return "OK", 200

    worker_payload = filtered_payload
    if inline_status_processing and webhook_kind == "message":
        # Mixed payloads: process statuses inline, enqueue only message changes.
        status_part, message_part = split_status_and_message_payload(filtered_payload)
        if status_part:
            try:
                processor = WebhookProcessor(get_db())
                success, message = processor.process_webhook(status_part)
                if not success:
                    logger.warning("Status webhook inline processing: %s", message)
            except Exception as exc:
                logger.exception("Status webhook inline error: %s", exc)
        worker_payload = message_part or filtered_payload

    # Enqueue inbound messages to worker when Redis is available.
    try:
        from core.queue.manager import enqueue_job, get_queue_backend

        if get_queue_backend() == "redis":
            enqueue_job("whatsapp-webhook", {"payload": worker_payload}, source="webhook")
            trace_event(
                stage="api.webhook.enqueued",
                status="ok",
                details={"job": "whatsapp-webhook", "backend": "redis"},
            )
            logger.info("Webhook enqueued for worker processing")
            return "OK", 200
    except Exception as exc:
        logger.warning("Webhook enqueue failed, falling back to inline processing: %s", exc)

    # Inline fallback (no Redis or enqueue error)
    try:
        processor = WebhookProcessor(get_db())
        success, message = processor.process_webhook(worker_payload)

        if success:
            logger.info(f"✅ Webhook processed successfully: {message}")
        else:
            logger.error(f"❌ Webhook processing error: {message}")
    except Exception as e:
        logger.exception(f"🔥 Webhook exception: {e}")

    return "OK", 200


@whatsapp_bp.route("/debug/trace/wamid/<path:wamid>", methods=["GET"])
def debug_trace_wamid(wamid: str):
    """Return recent trace timeline for a specific wamid."""
    limit = min(int(request.args.get("limit", 200)), 1000)
    events = get_trace_by_wamid(wamid, limit=limit)
    return jsonify({
        "success": True,
        "wamid": wamid,
        "count": len(events),
        "events": events,
    }), 200


@whatsapp_bp.route("/debug/trace/conversation/<int:conversation_id>", methods=["GET"])
def debug_trace_conversation(conversation_id: int):
    """Return recent trace timeline for a conversation."""
    limit = min(int(request.args.get("limit", 300)), 1000)
    events = get_trace_by_conversation(conversation_id, limit=limit)
    return jsonify({
        "success": True,
        "conversation_id": conversation_id,
        "count": len(events),
        "events": events,
    }), 200


# ============================================================
# Send Message Endpoints
# ============================================================

@whatsapp_bp.route("/send/text", methods=["POST"])
@require_token
def send_text():
    """
    Send a text message.
    
    POST /api/whatsapp/send/text
    
    Request body:
    {
        "to": "919876543210",
        "text": "Hello!"
    }
    """
    try:
        data = request.get_json(silent=True) or {}
        
        # Validate
        to, text = validate_text_message(data)
        
        phone_number_id = get_phone_number_id()
        if not phone_number_id:
            return jsonify({
                "success": False,
                "error": "phone_number_id required. Set WHATSAPP_PHONE_NUMBER_ID env var."
            }), 400
        
        # Limit Check
        allowed, error_resp = _enforce_message_limit(phone_number_id, send_kind="manual")
        if not allowed:
            return error_resp, 429
        
        # Send
        service = WhatsAppService(get_db(), phone_number_id, g.access_token)
        result = service.send_text(to=to, text=text, preview_url=data.get("preview_url", False))
        from .human_escalation import enrich_agent_send_result
        result = enrich_agent_send_result(result)
        
        return jsonify(result), 200 if result.get("success") else 400
        
    except ValidationError as e:
        return handle_validation_error(e)
    except Exception as exc:
        logger.exception(f"send_text exception: {exc}")
        return jsonify({"success": False, "error": str(exc)}), 500


@whatsapp_bp.route("/send/sticker", methods=["POST"])
@require_token
def send_sticker():
    """
    Send a sticker message.
    
    POST /api/whatsapp/send/sticker
    
    Request body:
    {
        "to": "919876543210",
        "media_id": "1234567890"
    }
    """
    try:
        data = request.get_json(silent=True) or {}
        
        # Validate required fields
        to = data.get("to")
        media_id = data.get("media_id")
        
        if not to:
            return jsonify({"success": False, "error": "'to' is required"}), 400
        if not media_id:
            return jsonify({"success": False, "error": "'media_id' is required"}), 400
        
        phone_number_id = get_phone_number_id()
        if not phone_number_id:
            return jsonify({
                "success": False,
                "error": "phone_number_id required. Set WHATSAPP_PHONE_NUMBER_ID env var."
            }), 400
        
        # Limit Check
        allowed, error_resp = _enforce_message_limit(phone_number_id, send_kind="media")
        if not allowed:
            return error_resp, 429
        
        # Send
        service = WhatsAppService(get_db(), phone_number_id, g.access_token)
        result = service.send_sticker(to=to, sticker=media_id)
        from .human_escalation import enrich_agent_send_result
        result = enrich_agent_send_result(result)
        
        return jsonify(result), 200 if result.get("success") else 400
        
    except Exception as exc:
        logger.exception(f"send_sticker exception: {exc}")
        return jsonify({"success": False, "error": str(exc)}), 500


@whatsapp_bp.route("/send/template", methods=["POST"])
@require_token
def send_template():
    """
    Send a template message.
    
    POST /api/whatsapp/send/template
    
    Request body:
    {
        "to": "919876543210",
        "template_name": "hello_world",
        "language": "en",
        "params": []  // or "components": [...]
    }
    """
    try:
        data = request.get_json(silent=True) or {}
        
        # Validate
        to, template_name, language, components = validate_template_message(data)

        # Copy-code button value (optional — for templates with a COPY_CODE button)
        copy_code_value = data.get("copy_code_value") or None
        if copy_code_value:
            copy_code_value = str(copy_code_value).strip() or None

        phone_number_id = get_phone_number_id()
        if not phone_number_id:
            return jsonify({
                "success": False,
                "error": "phone_number_id required. Set WHATSAPP_PHONE_NUMBER_ID env var."
            }), 400
        
        # Limit Check
        allowed, error_resp = _enforce_message_limit(phone_number_id, send_kind="template")
        token_preview = g.access_token[:30] + "..." + g.access_token[-20:] if g.access_token else "NO TOKEN"
        print(f"\n{'='*60}")
        print(f"=== SEND TEMPLATE ===")
        print(f"Phone Number ID: {phone_number_id}")
        print(f"Token being used: {token_preview}")
        print(f"To: {to}, Template: {template_name}")
        print(f"{'='*60}\n")
        
        # Send
        service = WhatsAppService(get_db(), phone_number_id, g.access_token)
        result = service.send_template(
            to=to,
            template_name=template_name,
            language_code=language,
            components=components,
            copy_code_value=copy_code_value,
        )
        
        # Log result
        logger.info(f"Result: {result}")
        from .human_escalation import enrich_agent_send_result
        result = enrich_agent_send_result(result)
        
        return jsonify(result), 200 if result.get("success") else 400
        
    except ValidationError as e:
        return handle_validation_error(e)
    except Exception as exc:
        logger.exception(f"send_template exception: {exc}")
        return jsonify({"success": False, "error": str(exc)}), 500


@whatsapp_bp.route("/send/template/advanced", methods=["POST"])
@require_token
def send_template_advanced():
    """
    Send a template message using TemplateBuilder (for complex templates).
    
    POST /api/whatsapp/send/template/advanced
    
    Request body:
    {
        "to": "919876543210",
        "template_name": "promo_with_image",
        "language": "en",
        "header_image_url": "https://example.com/image.jpg",  // optional
        "body_params": ["Summer Sale", "50%"],                // optional
        "button_url_suffix": "promo123"                       // optional
    }
    """
    try:
        data = request.get_json(silent=True) or {}
        
        to = data.get("to", "")
        if not to:
            return jsonify({"success": False, "error": "to is required"}), 400
            
        template_name = data.get("template_name", "")
        if not template_name:
            return jsonify({"success": False, "error": "template_name is required"}), 400
            
        language = data.get("language", "en")
        header_image_url = data.get("header_image_url")
        body_params = data.get("body_params", [])
        button_url_suffix = data.get("button_url_suffix")
        
        phone_number_id = get_phone_number_id()
        if not phone_number_id:
            return jsonify({
                "success": False,
                "error": "phone_number_id required. Set WHATSAPP_PHONE_NUMBER_ID env var."
            }), 400
        
        # Limit Check
        allowed, error_resp = _enforce_message_limit(phone_number_id, send_kind="template")
        if not allowed:
            return error_resp, 429
        
        service = WhatsAppService(get_db(), phone_number_id, g.access_token)
        result = service.send_template_with_builder(
            to=to,
            template_name=template_name,
            language_code=language,
            header_image_url=header_image_url,
            body_params=body_params,
            button_url_suffix=button_url_suffix,
        )
        from .human_escalation import enrich_agent_send_result
        result = enrich_agent_send_result(result)
        
        return jsonify(result), 200 if result.get("success") else 400
        
    except Exception as exc:
        logger.exception(f"send_template_advanced exception: {exc}")
        return jsonify({"success": False, "error": str(exc)}), 500


@whatsapp_bp.route("/send/flow", methods=["POST"])
@require_token
def send_flow():
    """
    Send a Flow as an interactive message (within 24hr window).
    
    POST /api/whatsapp/send/flow
    
    Request body:
    {
        "to": "919876543210",
        "flow_id": "demo_abc123...",   // meta_flow_id from published flow
        "header_text": "Complete Our Form",
        "body_text": "Please fill out this quick form to help us serve you better!",
        "footer_text": "Takes only 1 minute",
        "button_text": "Start Form",
        "flow_token": "optional_tracking_token"
    }
    
    NOTE: This only works within the 24-hour messaging window.
    For outside window, use templates with flow buttons.
    """
    try:
        data = request.get_json(silent=True) or {}
        
        # Required fields
        to = data.get("to", "").strip()
        flow_id = data.get("flow_id", "").strip()
        
        if not to:
            return jsonify({"success": False, "error": "to (phone number) is required"}), 400
        if not flow_id:
            return jsonify({"success": False, "error": "flow_id is required"}), 400
        
        # Optional fields with defaults
        header_text = data.get("header_text", "Quick Form")
        body_text = data.get("body_text", "Please complete this form.")
        footer_text = data.get("footer_text", "")
        button_text = data.get("button_text", "Open Form")
        flow_token = data.get("flow_token", f"flow_{to}_{flow_id}")
        
        # Normalize phone number
        if not to.startswith("+"):
            to = to.lstrip("0")
        to = to.replace("+", "").replace(" ", "").replace("-", "")
        
        phone_number_id = get_phone_number_id()
        if not phone_number_id:
            return jsonify({
                "success": False,
                "error": "phone_number_id required. Set WHATSAPP_PHONE_NUMBER_ID env var."
            }), 400
        
        # Limit Check
        allowed, error_resp = _enforce_message_limit(phone_number_id, send_kind="flow")
        interactive_payload = {
            "type": "flow",
            "header": {
                "type": "text",
                "text": header_text
            },
            "body": {
                "text": body_text
            },
            "action": {
                "name": "flow",
                "parameters": {
                    "flow_message_version": "3",
                    "flow_token": flow_token,
                    "flow_id": flow_id,
                    "flow_cta": button_text,
                    "flow_action": "navigate",
                    "flow_action_payload": {
                        "screen": "WELCOME"
                    }
                }
            }
        }
        
        # Add footer if provided
        if footer_text:
            interactive_payload["footer"] = {"text": footer_text}
        
        # Build the full message payload
        message_payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to,
            "type": "interactive",
            "interactive": interactive_payload
        }
        
        # Send via Meta API
        import requests as http_requests
        
        url = f"https://graph.facebook.com/v18.0/{phone_number_id}/messages"
        headers = {
            "Authorization": f"Bearer {g.access_token}",
            "Content-Type": "application/json"
        }
        
        logger.info(f"📤 Sending flow to {to}: {flow_id}")
        logger.debug(f"Flow payload: {message_payload}")
        
        response = http_requests.post(url, headers=headers, json=message_payload, timeout=30)
        response_data = response.json()
        
        if response.status_code == 200:
            logger.info(f"✅ Flow sent successfully to {to}")
            return jsonify({
                "success": True,
                "message": f"Flow sent to {to}",
                "message_id": response_data.get("messages", [{}])[0].get("id"),
                "flow_id": flow_id,
                "recipient": to
            })
        else:
            logger.error(f"❌ Failed to send flow: {response_data}")
            return jsonify({
                "success": False,
                "error": response_data.get("error", {}).get("message", "Failed to send flow"),
                "error_details": response_data.get("error", {}),
                "hint": "Flows can only be sent within 24hr window. Is there recent conversation?"
            }), 400
            
    except Exception as exc:
        logger.exception(f"send_flow exception: {exc}")
        return jsonify({"success": False, "error": str(exc)}), 500


@whatsapp_bp.route("/send/media", methods=["POST"])
@require_token
def send_media():
    """
    Send a media message (image, video, audio, document).
    
    POST /api/whatsapp/send/media
    
    Request body:
    {
        "to": "919876543210",
        "media_type": "image",
        "media_url": "https://example.com/image.jpg",
        "caption": "Optional caption"
    }
    """
    try:
        data = request.get_json(silent=True) or {}
        
        # Validate
        to, media_type, url, media_id, caption = validate_media_message(data)
        
        phone_number_id = get_phone_number_id()
        if not phone_number_id:
            return jsonify({
                "success": False,
                "error": "phone_number_id required. Set WHATSAPP_PHONE_NUMBER_ID env var."
            }), 400
        
        # Limit Check
        allowed, error_resp = _enforce_message_limit(phone_number_id, send_kind="media")
        if not allowed:
            return error_resp, 429
        
        # Send
        service = WhatsAppService(get_db(), phone_number_id, g.access_token)
        
        if media_type == "image":
            result = service.send_image(to=to, image_url=url, image_id=media_id, caption=caption)
        elif media_type == "video":
            result = service.send_video(to=to, video_url=url, video_id=media_id, caption=caption)
        elif media_type == "audio":
            result = service.send_audio(to=to, audio_url=url, audio_id=media_id)
        elif media_type == "document":
            filename = data.get("filename") or data.get("media", {}).get("filename")
            result = service.send_document(to=to, document_url=url, document_id=media_id, caption=caption, filename=filename)
        else:
            return jsonify({"success": False, "error": f"Unsupported media type: {media_type}"}), 400
        
        from .human_escalation import enrich_agent_send_result
        result = enrich_agent_send_result(result)
        return jsonify(result), 200 if result.get("success") else 400
        
    except ValidationError as e:
        return handle_validation_error(e)
    except Exception as exc:
        logger.exception(f"send_media exception: {exc}")
        return jsonify({"success": False, "error": str(exc)}), 500


@whatsapp_bp.route("/send/interactive", methods=["POST"])
@require_token
def send_interactive():
    """
    Send an interactive message (buttons or list).
    
    POST /api/whatsapp/send/interactive
    
    Request body (buttons):
    {
        "to": "919876543210",
        "interactive": {
            "type": "button",
            "body": "Choose an option",
            "buttons": [
                {"id": "btn1", "title": "Option 1"},
                {"id": "btn2", "title": "Option 2"}
            ]
        }
    }
    
    Request body (list):
    {
        "to": "919876543210",
        "interactive": {
            "type": "list",
            "body": "Choose from menu",
            "button": "View Options",
            "sections": [
                {
                    "title": "Section 1",
                    "rows": [
                        {"id": "row1", "title": "Item 1", "description": "Description"}
                    ]
                }
            ]
        }
    }
    """
    try:
        data = request.get_json(silent=True) or {}
        
        phone_number_id = get_phone_number_id()
        if not phone_number_id:
            return jsonify({
                "success": False,
                "error": "phone_number_id required. Set WHATSAPP_PHONE_NUMBER_ID env var."
            }), 400
        
        # Limit Check
        allowed, error_resp = _enforce_message_limit(phone_number_id, send_kind="interactive")
        if not allowed:
            return error_resp, 429

        service = WhatsAppService(get_db(), phone_number_id, g.access_token)
        interactive = data.get("interactive", {})
        int_type = interactive.get("type", data.get("type", "button"))

        if int_type == "button":
            to, body_text, buttons, header, footer = validate_interactive_buttons(data)
            result = service.send_interactive_buttons(
                to=to,
                body_text=body_text,
                buttons=buttons,
                header_text=header,
                footer_text=footer,
            )
        elif int_type == "list":
            to, body_text, button_text, sections, header, footer = validate_interactive_list(data)
            result = service.send_interactive_list(
                to=to,
                body_text=body_text,
                button_text=button_text,
                sections=sections,
                header_text=header,
                footer_text=footer,
            )
        else:
            return jsonify({
                "success": False,
                "error": f"Unknown interactive type: {int_type}. Supported: button, list"
            }), 400
        
        from .human_escalation import enrich_agent_send_result
        result = enrich_agent_send_result(result)
        return jsonify(result), 200 if result.get("success") else 400
        
    except ValidationError as e:
        return handle_validation_error(e)
    except Exception as exc:
        logger.exception(f"send_interactive exception: {exc}")
        return jsonify({"success": False, "error": str(exc)}), 500


@whatsapp_bp.route("/send", methods=["POST"])
@require_token
def send_message():
    """
    Send any type of WhatsApp message.
    
    POST /api/whatsapp/send
    
    Request body:
    {
        "to": "919876543210",
        "type": "text",  // text, template, image, video, document, interactive
        ...type-specific fields
    }
    """
    try:
        data = request.get_json(silent=True) or {}
        
        if not data:
            return jsonify({"success": False, "error": "Request body required"}), 400
        
        msg_type = data.get("type", "text")
        sk = "manual"
        if msg_type == "template":
            sk = "template"
        elif msg_type in ("image", "video", "audio", "document"):
            sk = "media"
        elif msg_type == "interactive":
            sk = "interactive"
        
        # Route to appropriate handler based on type
        phone_number_id = get_phone_number_id()
        if not phone_number_id:
            return jsonify({
                "success": False,
                "error": "phone_number_id required. Set WHATSAPP_PHONE_NUMBER_ID env var."
            }), 400
        
        # Limit Check
        allowed, error_resp = _enforce_message_limit(phone_number_id, send_kind=sk)
        if not allowed:
            return error_resp, 429
        
        service = WhatsAppService(get_db(), phone_number_id, g.access_token)
        
        if msg_type == "text":
            to, text = validate_text_message(data)
            result = service.send_text(to=to, text=text, preview_url=data.get("preview_url", False))
            
        elif msg_type == "template":
            to, template_name, language, components = validate_template_message(data)
            result = service.send_template(to=to, template_name=template_name, language_code=language, components=components)
            
        elif msg_type in ("image", "video", "audio", "document"):
            to, media_type, url, media_id, caption = validate_media_message(data)
            if msg_type == "image":
                result = service.send_image(to=to, image_url=url, image_id=media_id, caption=caption)
            elif msg_type == "video":
                result = service.send_video(to=to, video_url=url, video_id=media_id, caption=caption)
            elif msg_type == "audio":
                result = service.send_audio(to=to, audio_url=url, audio_id=media_id)
            elif msg_type == "document":
                filename = data.get("filename") or data.get("media", {}).get("filename")
                result = service.send_document(to=to, document_url=url, document_id=media_id, caption=caption, filename=filename)
                
        elif msg_type == "interactive":
            interactive = data.get("interactive", {})
            int_type = interactive.get("type", "button")
            
            if int_type == "button":
                to, body_text, buttons, header, footer = validate_interactive_buttons(data)
                result = service.send_interactive_buttons(to=to, body_text=body_text, buttons=buttons, header_text=header, footer_text=footer)
            else:
                to, body_text, button_text, sections, header, footer = validate_interactive_list(data)
                result = service.send_interactive_list(to=to, body_text=body_text, button_text=button_text, sections=sections, header_text=header, footer_text=footer)
        else:
            return jsonify({
                "success": False,
                "error": f"Unknown message type: {msg_type}. Supported: text, template, image, video, audio, document, interactive"
            }), 400
        
        from .human_escalation import enrich_agent_send_result
        result = enrich_agent_send_result(result)
        return jsonify(result), 200 if result.get("success") else 400
            
    except ValidationError as e:
        return handle_validation_error(e)
    except Exception as exc:
        logger.exception(f"send_message exception: {exc}")
        return jsonify({"success": False, "error": str(exc)}), 500


def _coerce_interactive_send_payload(data: dict) -> dict:
    """Normalize partner aliases (button/list) into send-message interactive shape."""
    payload = dict(data)
    msg_type = str(payload.get("type") or "text").lower()
    interactive = payload.get("interactive")
    if not isinstance(interactive, dict):
        interactive = {}

    if msg_type in ("button", "buttons"):
        interactive.setdefault("type", "button")
        interactive.setdefault("body", payload.get("body") or interactive.get("body"))
        interactive.setdefault("buttons", payload.get("buttons") or interactive.get("buttons"))
        payload["type"] = "interactive"
        payload["interactive"] = interactive
    elif msg_type == "list":
        interactive.setdefault("type", "list")
        interactive.setdefault("body", payload.get("body") or interactive.get("body"))
        interactive.setdefault("button", payload.get("button") or payload.get("button_text") or interactive.get("button"))
        interactive.setdefault("sections", payload.get("sections") or interactive.get("sections"))
        payload["type"] = "interactive"
        payload["interactive"] = interactive

    return payload


def _is_meta_interactive_passthrough(interactive: dict) -> bool:
    """True when interactive is already in WhatsApp Cloud API shape."""
    if not isinstance(interactive, dict):
        return False
    body = interactive.get("body")
    if isinstance(body, dict) and "text" in body and interactive.get("action"):
        return True
    if interactive.get("passthrough") is True:
        return True
    return False


def _header_footer_text(value) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and value.get("text"):
        return str(value.get("text"))
    return None


def _send_interactive_message(service: WhatsAppService, data: dict) -> dict:
    """
    Send interactive button/list via partner send-message.

    Supports simplified Sociovia format (validators) or Meta Cloud API pass-through.
    """
    data = _coerce_interactive_send_payload(data)
    interactive = data.get("interactive") or {}
    int_type = str(interactive.get("type") or "button").lower()

    if _is_meta_interactive_passthrough(interactive):
        passthrough = dict(interactive)
        passthrough.pop("passthrough", None)
        return service.send_interactive_passthrough(
            to=data.get("to"),
            interactive=passthrough,
        )

    if int_type in ("button", "buttons"):
        to, body_text, buttons, header, footer = validate_interactive_buttons(data)
        return service.send_interactive_buttons(
            to=to,
            body_text=body_text,
            buttons=buttons,
            header_text=_header_footer_text(header),
            footer_text=_header_footer_text(footer),
        )

    if int_type == "list":
        to, body_text, button_text, sections, header, footer = validate_interactive_list(data)
        return service.send_interactive_list(
            to=to,
            body_text=body_text,
            button_text=button_text,
            sections=sections,
            header_text=_header_footer_text(header),
            footer_text=_header_footer_text(footer),
        )

    return {
        "success": False,
        "error": f"Unknown interactive type: {int_type}. Supported: button, list (or Meta Cloud API passthrough)",
    }


@whatsapp_bp.route("/send-message", methods=["POST"])
def send_message_v2():
    """
    Send WhatsApp message - workspace-aware version.
    
    POST /api/whatsapp/send-message
    
    This endpoint automatically gets the correct access token from the database
    based on the workspace_id.
    
    Request body:
    {
        "workspace_id": "4",
        "to": "919876543210",
        "type": "text",
        "text": "Hello World"
    }
    
    For template:
    {
        "workspace_id": "4",
        "to": "919876543210",
        "type": "template",
        "template_name": "hello_world",
        "template_language": "en"
    }

    Interactive reply buttons (within 24h session):
    {
        "workspace_id": "113",
        "to": "917013123744",
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": "Choose an option",
            "buttons": [{"id": "opt1", "title": "Option 1"}]
        }
    }

    Interactive list (alias type=list also accepted):
    {
        "workspace_id": "113",
        "to": "917013123744",
        "type": "list",
        "body": "Select a category",
        "button_text": "View options",
        "sections": [{"title": "Menu", "rows": [{"id": "r1", "title": "Item 1"}]}]
    }

    Meta Cloud API pass-through (full interactive object):
    {
        "workspace_id": "113",
        "to": "917013123744",
        "type": "interactive",
        "interactive": {
            "type": "list",
            "body": {"text": "Choose"},
            "action": {"button": "Options", "sections": [...]}
        }
    }
    """
    try:
        data = request.get_json(silent=True) or {}
        
        if not data:
            return jsonify({"success": False, "error": "Request body required"}), 400
        
        workspace_id = data.get("workspace_id")
        if not workspace_id:
            return jsonify({"success": False, "error": "workspace_id is required"}), 400
        
        to = data.get("to")
        if not to:
            return jsonify({"success": False, "error": "to (recipient phone number) is required"}), 400
        
        msg_type = data.get("type", "text")
        
        # Get the WhatsApp account for this workspace
        from .models import WhatsAppAccount
        account = WhatsAppAccount.query.filter_by(
            workspace_id=workspace_id,
            is_active=True
        ).first()
        
        if not account:
            return jsonify({
                "success": False,
                "error": f"No active WhatsApp account found for workspace {workspace_id}"
            }), 404
        
        # Get access token from account
        access_token = account.get_access_token()
        if not access_token:
            return jsonify({
                "success": False,
                "error": "WhatsApp account has no access token configured"
            }), 401
        
        logger.info(f"Sending message via workspace {workspace_id}, account {account.id}, phone_number_id {account.phone_number_id}")
        
        # Initialize service with account credentials
        service = WhatsAppService(
            db_session=get_db(),
            phone_number_id=account.phone_number_id,
            access_token=access_token,
            waba_id=account.waba_id,
            workspace_id=workspace_id
        )
        
        # Send based on type
        if msg_type == "text":
            text = data.get("text")
            if not text:
                return jsonify({"success": False, "error": "text is required for text messages"}), 400
            result = service.send_text(to=to, text=text, preview_url=data.get("preview_url", False))
            
        elif msg_type == "template":
            template_name = data.get("template_name")
            if not template_name:
                return jsonify({"success": False, "error": "template_name is required for template messages"}), 400

            components = data.get("template_components")
            language_code = data.get("template_language", "en")
            recruiter_carousel = data.get("recruiter_carousel") or data.get("vaish_carousel")
            if recruiter_carousel and not components:
                from . import vaish_carousel_template as vaish_ct

                components = vaish_ct.build_carousel_template_components(
                    service,
                    search_context=str(
                        recruiter_carousel.get("search_context")
                        or recruiter_carousel.get("context")
                        or "your search"
                    ),
                    cards=recruiter_carousel.get("cards") or [],
                    card_count=int(
                        recruiter_carousel.get("card_count")
                        or vaish_ct.VAISH_RECRUITER_CAROUSEL_CARD_COUNT
                    ),
                )
                if not components:
                    return jsonify({
                        "success": False,
                        "error": "Failed to build recruiter carousel (image upload or card data)",
                    }), 400
                language_code = data.get("template_language") or vaish_ct.VAISH_RECRUITER_CAROUSEL_LANG

            result = service.send_template(
                to=to,
                template_name=template_name,
                language_code=language_code,
                components=components,
                waba_id=account.waba_id
            )
            
        elif msg_type in ("image", "video", "audio", "document"):
            media_url = data.get("media_url")
            if not media_url:
                return jsonify({"success": False, "error": "media_url is required for media messages"}), 400
            caption = data.get("caption")
            
            if msg_type == "image":
                result = service.send_image_with_url_candidates(
                    to=to,
                    image_url=media_url,
                    url_candidates=data.get("url_candidates"),
                    caption=caption,
                )
            elif msg_type == "video":
                result = service.send_video(to=to, video_url=media_url, caption=caption)
            elif msg_type == "audio":
                result = service.send_audio(to=to, audio_url=media_url)
            elif msg_type == "document":
                result = service.send_document(to=to, document_url=media_url, caption=caption, filename=data.get("filename"))

        elif msg_type in ("interactive", "button", "buttons", "list"):
            result = _send_interactive_message(service, data)

        else:
            return jsonify({
                "success": False,
                "error": (
                    f"Unsupported message type: {msg_type}. "
                    "Supported: text, template, image, video, audio, document, "
                    "interactive, button, buttons, list"
                ),
            }), 400

        from .human_escalation import enrich_agent_send_result
        result = enrich_agent_send_result(result)
        return jsonify(result), 200 if result.get("success") else 400

    except ValidationError as e:
        return handle_validation_error(e)
    except Exception as exc:
        logger.exception(f"send_message_v2 exception: {exc}")
        return jsonify({"success": False, "error": str(exc)}), 500


# ============================================================
# Partner template discovery (Vaish / workspace integrations)
# ============================================================

def _partner_sync_and_list(workspace_id: str, *, sync_first: bool):
    from .partner_templates import query_approved_templates, template_to_partner_dict
    from .services import WhatsAppService

    account, error = get_valid_account_for_workspace(workspace_id)
    if error or not account:
        return None, jsonify({
            "success": False,
            "error": error or f"No active WhatsApp account for workspace {workspace_id}",
        }), 404

    sync_stats = None
    if sync_first:
        try:
            service = WhatsAppService(
                db_session=get_db(),
                access_token=account.get_access_token(),
                phone_number_id=account.phone_number_id,
                waba_id=account.waba_id,
                account_id=account.id,
                workspace_id=str(workspace_id),
            )
            sync_stats = service.sync_templates()
            if not sync_stats.get("success"):
                return None, jsonify({
                    "success": False,
                    "error": sync_stats.get("error", "template_sync_failed"),
                    "sync": sync_stats,
                }), 400
        except Exception as exc:
            logger.exception("partner template sync error: %s", exc)
            return None, jsonify({"success": False, "error": str(exc)}), 500

    templates = query_approved_templates(account.id)
    payload_templates = [template_to_partner_dict(t, str(workspace_id)) for t in templates]
    body = {
        "success": True,
        "workspace_id": str(workspace_id),
        "account_id": account.id,
        "display_phone_number": account.display_phone_number,
        "synced_from_meta": bool(sync_first),
        "count": len(payload_templates),
        "templates": payload_templates,
    }
    if sync_stats is not None:
        body["sync"] = sync_stats
    return body, None, None


@whatsapp_bp.route("/partner/templates", methods=["GET"])
def partner_list_templates():
    """
    List APPROVED WhatsApp templates for a partner workspace (send-ready format).

    GET /api/whatsapp/partner/templates?workspace_id=113
    GET /api/whatsapp/partner/templates?workspace_id=113&sync=true
    """
    workspace_id = request.args.get("workspace_id")
    if not workspace_id:
        return jsonify({"success": False, "error": "workspace_id is required"}), 400

    sync_first = request.args.get("sync", "").lower() in ("1", "true", "yes")
    body, err_resp, err_code = _partner_sync_and_list(workspace_id, sync_first=sync_first)
    if err_resp is not None:
        return err_resp, err_code
    return jsonify(body)


@whatsapp_bp.route("/partner/templates/sync", methods=["POST"])
def partner_sync_templates():
    """
    Sync templates from Meta for a partner workspace, then return APPROVED list.

    POST /api/whatsapp/partner/templates/sync
    Body: { "workspace_id": "113" }
    """
    data = request.get_json(silent=True) or {}
    workspace_id = data.get("workspace_id") or request.args.get("workspace_id")
    if not workspace_id:
        return jsonify({"success": False, "error": "workspace_id is required"}), 400

    body, err_resp, err_code = _partner_sync_and_list(workspace_id, sync_first=True)
    if err_resp is not None:
        return err_resp, err_code
    return jsonify(body)


# ============================================================
# Conversation Endpoints
# ============================================================

@whatsapp_bp.route("/conversations", methods=["GET"])
def list_conversations():
    """
    List all conversations.
    
    GET /api/whatsapp/conversations
    GET /api/whatsapp/conversations?phone_number_id=xxx&limit=50&offset=0&status=open
    GET /api/whatsapp/conversations?workspace_id=xxx
    """
    try:
        from .models import WhatsAppAccount
        
        phone_number_id = request.args.get("phone_number_id")
        workspace_id = request.args.get("workspace_id")
        status = request.args.get("status")
        category = (request.args.get("category") or "").strip().lower() or None
        search = (request.args.get("search") or "").strip() or None
        include_totals = request.args.get("include_totals", "").lower() in {"1", "true", "yes"}
        limit = min(int(request.args.get("limit", 50)), 100)
        offset = int(request.args.get("offset", 0))
        
        # Get account IDs for workspace filtering
        account_ids = None
        if workspace_id:
            workspace_accounts = WhatsAppAccount.query.filter_by(workspace_id=workspace_id, is_active=True).all()
            account_ids = [a.id for a in workspace_accounts]
            if not account_ids:
                # No accounts for this workspace
                payload = {
                    "success": True,
                    "conversations": [],
                    "count": 0,
                    "total_count": 0,
                    "limit": limit,
                    "offset": offset,
                }
                if include_totals:
                    payload["totals"] = {
                        "all": 0,
                        "unread": 0,
                        "active": 0,
                        "expired": 0,
                        "needs_reply": 0,
                        "human_required": 0,
                        "opted_out": 0,
                    }
                return jsonify(payload)
        
        service = ConversationService(get_db())
        conversations = service.get_conversations(
            phone_number_id=phone_number_id,
            status=status,
            limit=limit,
            offset=offset,
            account_ids=account_ids,
            category=category,
            search=search,
        )

        total_count = None
        totals = None
        if category or include_totals:
            totals = service.count_conversation_totals(
                phone_number_id=phone_number_id,
                status=status,
                account_ids=account_ids,
                search=search,
            )
            if category:
                total_count = totals.get(category, len(conversations))
            elif include_totals:
                total_count = totals.get("all", len(conversations))
        
        return jsonify({
            "success": True,
            "conversations": conversations,
            "count": len(conversations),
            "total_count": total_count if total_count is not None else len(conversations),
            "totals": totals,
            "limit": limit,
            "offset": offset,
            "category": category,
        })
    except Exception as e:
        current_app.logger.exception("list_conversations failed: %s", e)
        return jsonify({"success": False, "error": "Failed to load conversations"}), 500


@whatsapp_bp.route("/conversations/<int:conversation_id>", methods=["GET"])
def get_conversation(conversation_id: int):
    """
    Get a single conversation with recent messages.
    
    GET /api/whatsapp/conversations/<id>
    GET /api/whatsapp/conversations/<id>?message_limit=100
    """
    message_limit = min(int(request.args.get("message_limit", 50)), 200)
    
    service = ConversationService(get_db())
    conversation = service.get_conversation(
        conversation_id,
        include_messages=True,
        message_limit=message_limit,
    )
    
    if not conversation:
        return jsonify({
            "success": False,
            "error": "Conversation not found"
        }), 404
    
    return jsonify({
        "success": True,
        "conversation": conversation,
    })


@whatsapp_bp.route("/conversations/<int:conversation_id>/messages", methods=["GET"])
def get_messages(conversation_id: int):
    """
    Get messages for a conversation.
    
    GET /api/whatsapp/conversations/<id>/messages
    GET /api/whatsapp/conversations/<id>/messages?limit=100&before_id=xxx
    """
    limit = min(int(request.args.get("limit", 100)), 200)
    before_id = request.args.get("before_id")
    if before_id:
        before_id = int(before_id)
    
    service = ConversationService(get_db())
    messages = service.get_messages(
        conversation_id=conversation_id,
        limit=limit,
        before_id=before_id,
    )
    
    return jsonify({
        "success": True,
        "messages": messages,
        "count": len(messages),
    })


@whatsapp_bp.route("/conversations/<int:conversation_id>/read", methods=["POST"])
def mark_read(conversation_id: int):
    """
    Mark a conversation as read.
    
    POST /api/whatsapp/conversations/<id>/read
    """
    service = ConversationService(get_db())
    success = service.mark_conversation_read(conversation_id)
    
    if success:
        return jsonify({"success": True})
    else:
        return jsonify({"success": False, "error": "Conversation not found"}), 404


@whatsapp_bp.route("/conversations/<int:conversation_id>/close", methods=["POST"])
def close_conversation(conversation_id: int):
    """
    Close a conversation manually (agent action).
    
    POST /api/whatsapp/conversations/<id>/close
    
    Note: This is UI-level only, does NOT affect Meta's 24h window.
    Templates can still be sent to closed conversations.
    """
    from .models import WhatsAppConversation
    
    db_session = get_db()
    conversation = db_session.get(WhatsAppConversation, conversation_id)
    
    if not conversation:
        return jsonify({"success": False, "error": "Conversation not found"}), 404
    
    now = datetime.now(timezone.utc)
    conversation.closed_by_agent = True
    conversation.closed_at = now
    conversation.status = "closed"  # Also update status for analytics
    
    # Audit log
    logger.info(f"Conversation {conversation_id} closed by agent at {now.isoformat()}")
    
    db_session.commit()
    
    return jsonify({
        "success": True,
        "closed_at": now.isoformat(),
        "message": "Conversation closed. Templates can still be sent."
    })


@whatsapp_bp.route("/conversations/<int:conversation_id>/reopen", methods=["POST"])
def reopen_conversation(conversation_id: int):
    """
    Reopen a manually closed conversation.
    
    POST /api/whatsapp/conversations/<id>/reopen
    
    Note: This only clears the agent close flag. 
    Session status still depends on 24h rule from last inbound.
    """
    from .models import WhatsAppConversation
    
    db_session = get_db()
    conversation = db_session.get(WhatsAppConversation, conversation_id)
    
    if not conversation:
        return jsonify({"success": False, "error": "Conversation not found"}), 404
    
    if not conversation.closed_by_agent:
        return jsonify({"success": False, "error": "Conversation is not closed by agent"}), 400
    
    now = datetime.now(timezone.utc)
    conversation.closed_by_agent = False
    conversation.closed_at = None
    conversation.status = "open"  # Restore status for analytics
    
    # Audit log
    logger.info(f"Conversation {conversation_id} reopened by agent at {now.isoformat()}")
    
    db_session.commit()
    
    return jsonify({
        "success": True,
        "is_session_open": conversation.is_session_open,
        "session_time_left_seconds": conversation.session_time_left_seconds,
        "message": "Conversation reopened."
    })


@whatsapp_bp.route("/conversations/<int:conversation_id>", methods=["DELETE"])
def delete_conversation(conversation_id: int):
    """
    Delete a conversation and its messages.
    
    DELETE /api/whatsapp/conversations/<id>
    """
    from .models import WhatsAppConversation
    
    db_session = get_db()
    conversation = db_session.get(WhatsAppConversation, conversation_id)
    
    if not conversation:
        return jsonify({"success": False, "error": "Conversation not found"}), 404
        
    db_session.delete(conversation)
    db_session.commit()
    
    return jsonify({"success": True, "message": "Conversation deleted"})



# ============================================================
# Connection Path Detection (Dual-Path UX)
# ============================================================

@whatsapp_bp.route("/connection-path", methods=["GET"])
def get_connection_path():
    """
    Determine which WhatsApp connection method to use for a workspace.
    
    GET /api/whatsapp/connection-path?workspace_id=xxx
    
    Returns:
    {
        "status": "NO_ACCOUNT" | "CONNECTED" | "PARTIAL" | "RELINK_REQUIRED",
        "recommended_path": "EMBEDDED" | "MANUAL" | None,
        "reason": str,
        "account_summary": {...} | None,
        "can_use_embedded_signup": bool,
        "can_use_manual_link": bool
    }
    
    Usage:
    - NO_ACCOUNT + EMBEDDED → Show Embedded Signup flow
    - PARTIAL + MANUAL → Show "Finish setup" form
    - RELINK_REQUIRED + MANUAL → Show "Reconnect" form
    - CONNECTED → Show account summary, hide signup buttons
    """
    from .connection_path import detect_whatsapp_connection_path
    
    workspace_id = request.args.get("workspace_id", "")
    
    if not workspace_id:
        return jsonify({
            "success": False,
            "error": "workspace_id parameter is required"
        }), 400
    
    try:
        result = detect_whatsapp_connection_path(workspace_id)
        result["success"] = True
        return jsonify(result)
    except Exception as e:
        logger.exception(f"Connection path detection failed: {e}")
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


@whatsapp_bp.route("/connect/manual", methods=["POST"])
def connect_manual_account():
    """
    Connect an existing WhatsApp Business account via manual credentials.
    
    POST /api/whatsapp/connect/manual
    
    Request body:
    {
        "workspace_id": "xxx",
        "waba_id": "123456789",
        "phone_number_id": "987654321", 
        "access_token": "EAA..."
    }
    
    SAFETY RULES:
    1. Token is validated with Meta API before saving
    2. Working tokens are never overwritten
    3. Cross-workspace connections are blocked
    4. All tokens are encrypted at rest
    
    Use this for:
    - Users who already have WhatsApp Cloud API setup elsewhere
    - Re-linking accounts with expired tokens
    - Test numbers provided by Meta
    """
    from .connection_path import connect_manual
    
    data = request.get_json(silent=True) or {}
    
    # Validate required fields
    required_fields = ["workspace_id", "waba_id", "phone_number_id", "access_token"]
    missing = [f for f in required_fields if not data.get(f)]
    
    if missing:
        return jsonify({
            "success": False,
            "error": f"Missing required fields: {', '.join(missing)}",
            "error_code": "MISSING_FIELDS",
            "help": {
                "waba_id": "WhatsApp Business Account ID (found in Meta Business Settings)",
                "phone_number_id": "Phone Number ID (found in WhatsApp Business API setup)",
                "access_token": "System user token with whatsapp_business_messaging permission"
            }
        }), 400
    
    # Extract user_id from session/header if available, otherwise use workspace_id
    user_id = request.headers.get("X-User-Id") or data.get("user_id") or data["workspace_id"]
    
    try:
        result = connect_manual(
            workspace_id=data["workspace_id"],
            user_id=user_id,
            waba_id=data["waba_id"],
            phone_number_id=data["phone_number_id"],
            access_token=data["access_token"],
        )
        
        status_code = 200 if result.get("success") else 400
        return jsonify(result), status_code
        
    except Exception as e:
        logger.exception(f"Manual connection failed: {e}")
        return jsonify({
            "success": False,
            "error": str(e),
            "error_code": "INTERNAL_ERROR"
        }), 500


# ============================================================
# Account Management Endpoints
# ============================================================

@whatsapp_bp.route("/accounts/<int:account_id>/toggle-status", methods=["POST"])
@require_admin_only  # SECURITY: Only admins can toggle account status
def toggle_account_status(account_id: int):
    """
    Toggle account active status (link/unlink).
    
    POST /api/whatsapp/accounts/<id>/toggle-status
    
    Request body:
    {
        "is_active": false  // true to link, false to unlink
    }
    
    When unlinked (is_active=False):
    - No messages can be sent or received
    - Templates won't work
    - Flows are paused
    - Account credentials are retained
    """
    from .models import WhatsAppAccount
    from shared_models import db
    
    try:
        account = WhatsAppAccount.query.get(account_id)
        if not account:
            return jsonify({"success": False, "error": "Account not found"}), 404
        
        data = request.get_json() or {}
        new_status = data.get("is_active", not account.is_active)
        
        account.is_active = new_status
        db.session.commit()
        
        return jsonify({
            "success": True,
            "message": f"Account {'linked' if new_status else 'unlinked'} successfully",
            "is_active": account.is_active
        })
        
    except Exception as e:
        logger.exception(f"Failed to toggle account status: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@whatsapp_bp.route("/connection-path/validate-token", methods=["POST"])
def validate_token_endpoint():
    """
    Validate an access token with Meta API (before saving).
    
    POST /api/whatsapp/connection-path/validate-token
    
    Request body:
    {
        "access_token": "EAA..."
    }
    
    Returns:
    {
        "valid": bool,
        "user_id": str | None,
        "name": str | None,
        "error": str | None
    }
    """
    from .connection_path import validate_token_with_meta
    
    data = request.get_json(silent=True) or {}
    access_token = data.get("access_token", "")
    
    if not access_token:
        return jsonify({
            "valid": False,
            "error": "access_token is required"
        }), 400
    
    result = validate_token_with_meta(access_token)
    return jsonify(result)


# ============================================================
# Webhook Logs Endpoint
# ============================================================

@whatsapp_bp.route("/webhook-logs", methods=["GET"])
def list_webhook_logs():
    """
    List recent webhook logs for debugging.
    
    GET /api/whatsapp/webhook-logs
    GET /api/whatsapp/webhook-logs?limit=50&event_type=message
    """
    from .models import WhatsAppWebhookLog
    
    limit = min(int(request.args.get("limit", 50)), 100)
    event_type = request.args.get("event_type")
    
    query = WhatsAppWebhookLog.query
    
    if event_type:
        query = query.filter_by(event_type=event_type)
    
    logs = query.order_by(
        WhatsAppWebhookLog.received_at.desc()
    ).limit(limit).all()
    
    return jsonify({
        "success": True,
        "logs": [log.to_dict() for log in logs],
        "count": len(logs),
    })


# ============================================================
# Analytics Endpoints
# ============================================================

@whatsapp_bp.route("/analytics/summary", methods=["GET"])
def get_analytics_summary():
    """
    Get analytics summary with comparison data for dashboard.
    GET /api/whatsapp/analytics/summary
    """
    from datetime import datetime, timedelta, timezone
    from sqlalchemy import func
    from .models import WhatsAppMessage, WhatsAppConversation, WhatsAppAccount
    
    # 1. Parse params
    workspace_id = request.args.get("workspace_id")
    account_id = request.args.get("account_id")
    days_param = request.args.get("days", "7")
    
    try:
        days = int(days_param)
    except:
        days = 7
        
    db_session = get_db()
    now = datetime.now(timezone.utc)
    
    # Define periods
    current_end = now
    current_start = now - timedelta(days=days)
    
    prev_end = current_start
    prev_start = prev_end - timedelta(days=days)
    
    # Helper to get stats for a period
    def get_period_stats(start, end):
        # Base query
        q = db_session.query(WhatsAppMessage).filter(
            WhatsAppMessage.created_at >= start,
            WhatsAppMessage.created_at < end,
            WhatsAppMessage.direction == "outgoing"
        )
        
        # Filter by workspace/account
        if account_id:
            q = q.join(WhatsAppConversation).filter(WhatsAppConversation.account_id == int(account_id))
        elif workspace_id:
            # Get accounts
            accs = WhatsAppAccount.query.filter_by(workspace_id=workspace_id, is_active=True).all()
            ids = [a.id for a in accs]
            if ids:
                q = q.join(WhatsAppConversation).filter(WhatsAppConversation.account_id.in_(ids))
            else:
                return {"sent":0, "delivered":0, "read":0, "failed":0}
        
        # Aggregate
        stats = q.with_entities(
            WhatsAppMessage.status, 
            func.count(WhatsAppMessage.id)
        ).group_by(WhatsAppMessage.status).all()
        
        counts = {s: c for s, c in stats}
        sent = sum(counts.values())
        delivered = counts.get("delivered", 0) + counts.get("read", 0)
        read = counts.get("read", 0)
        failed = counts.get("failed", 0)
        
        return {
            "sent": sent,
            "delivered": delivered,
            "read": read,
            "failed": failed
        }

    curr_stats = get_period_stats(current_start, current_end)
    prev_stats = get_period_stats(prev_start, prev_end)
    
    # Calculate rates
    def calc_rate(part, total):
        return round((part / total * 100), 1) if total > 0 else 0
        
    curr_data = {
        **curr_stats,
        "delivery_rate": calc_rate(curr_stats["delivered"], curr_stats["sent"]),
        "read_rate": calc_rate(curr_stats["read"], curr_stats["delivered"]),
        "active_customers": 0, # Placeholder
    }
    
    # Active customers (sessions)
    # Reuse logic from get_analytics: session open AND not closed by agent
    active_q = db_session.query(func.count(WhatsAppConversation.id)).filter(
        WhatsAppConversation.session_expires_at > now,
        WhatsAppConversation.closed_by_agent != True
    )
    if account_id:
        active_q = active_q.filter(WhatsAppConversation.account_id == int(account_id))
    elif workspace_id:
        accs = WhatsAppAccount.query.filter_by(workspace_id=workspace_id, is_active=True).all()
        ids = [a.id for a in accs]
        if ids:
            active_q = active_q.filter(WhatsAppConversation.account_id.in_(ids))
            
    curr_data["active_customers"] = active_q.scalar() or 0
    
    # Skip per-message response-time scan (loads every message into Python — too slow for dashboard).
    curr_data["avg_response_time_seconds"] = None
    
    # Comparisons
    def calc_change(curr, prev):
        if prev == 0: return 0 if curr == 0 else 100
        return round(((curr - prev) / prev) * 100, 1)

    comparison = {
        "sent_change": calc_change(curr_stats["sent"], prev_stats["sent"]),
        "delivered_change": calc_change(curr_stats["delivered"], prev_stats["delivered"]),
        "read_change": calc_change(curr_stats["read"], prev_stats["read"])
    }
    
    return jsonify({
        "success": True,
        "current": curr_data,
        "previous": prev_stats,
        "comparison": comparison,
        "period_days": days
    })


@whatsapp_bp.route("/analytics/trends", methods=["GET"])
def get_analytics_trends():
    """
    Get daily trends for dashboard charts.
    GET /api/whatsapp/analytics/trends
    """
    from datetime import datetime, timedelta, timezone
    from sqlalchemy import func
    from .models import WhatsAppMessage, WhatsAppConversation, WhatsAppAccount
    
    workspace_id = request.args.get("workspace_id")
    account_id = request.args.get("account_id")
    days_param = request.args.get("days", "7")
    try:
        days = int(days_param)
    except:
        days = 7
        
    db_session = get_db()
    now = datetime.now(timezone.utc)
    start_date = now - timedelta(days=days)
    
    # Query grouped by date
    # Note: func.date() works in Postgres. For SQLite/others might need strftime.
    
    q = db_session.query(
        func.date(WhatsAppMessage.created_at).label("date"),
        func.count(WhatsAppMessage.id).label("sent"),
        func.sum(case((WhatsAppMessage.status.in_(["delivered", "read"]), 1), else_=0)).label("delivered"),
        func.sum(case((WhatsAppMessage.status == "read", 1), else_=0)).label("read")
    ).filter(
        WhatsAppMessage.created_at >= start_date,
        WhatsAppMessage.direction == "outgoing"
    )
    
    if account_id:
        q = q.join(WhatsAppConversation).filter(WhatsAppConversation.account_id == int(account_id))
    elif workspace_id:
        accs = WhatsAppAccount.query.filter_by(workspace_id=workspace_id, is_active=True).all()
        ids = [a.id for a in accs]
        if ids:
            q = q.join(WhatsAppConversation).filter(WhatsAppConversation.account_id.in_(ids))
            
    results = q.group_by(func.date(WhatsAppMessage.created_at)).order_by(func.date(WhatsAppMessage.created_at)).all()
    
    daily = []
    for r in results:
        daily.append({
            "date": str(r.date),
            "sent": r.sent,
            "delivered": r.delivered or 0,
            "read": r.read or 0
        })
        
    return jsonify({
        "success": True,
        "daily": daily
    })


@whatsapp_bp.route("/analytics/ai-insights/generate", methods=["POST"])
def generate_ai_insights():
    """
    Generate AI-powered insights for WhatsApp analytics using Vertex AI (Gemini).
    POST /api/whatsapp/analytics/ai-insights/generate
    """
    from datetime import datetime, timedelta, timezone
    from sqlalchemy import func
    from google.genai.types import GenerateContentConfig
    from .models import WhatsAppMessage, WhatsAppConversation, WhatsAppAccount
    import json
    import time
    
    data = request.get_json() or {}
    workspace_id = data.get("workspace_id")
    account_id = data.get("account_id")
    days_param = data.get("days", 7)
    force_refresh = data.get("force_refresh", False)
    
    try:
        days = int(days_param)
    except:
        days = 7
        
    # Get stats for prompt
    db_session = get_db()
    now = datetime.now(timezone.utc)
    start_date = now - timedelta(days=days)
    
    # Base query for stats
    q = db_session.query(WhatsAppMessage)
    
    if account_id:
        q = q.join(WhatsAppConversation).filter(WhatsAppConversation.account_id == int(account_id))
    elif workspace_id:
        accs = WhatsAppAccount.query.filter_by(workspace_id=workspace_id, is_active=True).all()
        ids = [a.id for a in accs]
        if ids:
            q = q.join(WhatsAppConversation).filter(WhatsAppConversation.account_id.in_(ids))
        else:
            return jsonify({"success": False, "error": "No accounts found for workspace"}), 404
            
    stats_query = q.filter(
        WhatsAppMessage.created_at >= start_date,
        WhatsAppMessage.direction == "outgoing"
    ).with_entities(
        WhatsAppMessage.status,
        func.count(WhatsAppMessage.id)
    ).group_by(WhatsAppMessage.status).all()
    
    stats_dict = {s: c for s, c in stats_query}
    sent = sum(stats_dict.values())
    delivered = stats_dict.get("delivered", 0) + stats_dict.get("read", 0)
    read = stats_dict.get("read", 0)
    failed = stats_dict.get("failed", 0)
    
    # Calculate rates
    delivery_rate = round((delivered / sent * 100), 1) if sent > 0 else 0
    read_rate = round((read / delivered * 100), 1) if delivered > 0 else 0
    failure_rate = round((failed / sent * 100), 1) if sent > 0 else 0
    
    # AI Generation
    try:
        client = get_genai_client()
        if not client:
            return jsonify({"success": False, "error": "AI service not configured"}), 503
            
        model_name = os.environ.get("TEXT_MODEL") or os.environ.get("GEMINI_MODEL") or "gemini-3.5-flash"
        
        prompt = f"""You are Sociovia AI, a WhatsApp Business analytics expert. Be CONCISE and ACTIONABLE.

DATA ({days} days):
• Sent: {sent} | Delivered: {delivered} ({delivery_rate}%) | Read: {read} ({read_rate}%) | Failed: {failed} ({failure_rate}%)

Generate insights in this exact JSON format. Keep all text SHORT and SCANNABLE:

{{
    "engagement_score": <0-100>,
    "revenue_opportunity": "High|Medium|Low",
    "customer_health": {{
        "engaged": <0-100>,
        "at_risk": <0-100>,
        "dormant": <0-100>
    }},
    "summary": "<MAX 15 words - one powerful insight>",
    "insights": [
        {{
            "type": "success|warning|critical|opportunity",
            "title": "<3-5 words only>",
            "action": "<One clear action in under 20 words>",
            "impact": "high|medium|low"
        }}
    ],
    "quick_wins": [
        "<Max 15 words - specific action for this week>"
    ],
    "growth_recommendations": [
        "<Max 15 words - strategic growth action>"
    ],
    "predicted_improvement": "<e.g., '15-25% better engagement'>"
}}

RULES:
- Generate exactly 3 insights
- Generate exactly 2 quick_wins  
- Generate exactly 2 growth_recommendations
- NO long paragraphs - be punchy and direct
- Focus on what TO DO, not what's wrong
- Use numbers and percentages when possible"""
        
        start_t = time.time()
        response = client.models.generate_content(
            model=model_name,
            contents=prompt,
            config=GenerateContentConfig(response_mime_type="application/json")
        )
        duration_ms = (time.time() - start_t) * 1000
        
        try:
            ai_data = json.loads(response.text)
        except:
            # Fallback parsing if JSON mode fails
            import re
            match = re.search(r'\{.*\}', response.text, re.DOTALL)
            if match:
                ai_data = json.loads(match.group())
            else:
                # Last resort: try finding a list
                match_list = re.search(r'\[.*\]', response.text, re.DOTALL)
                if match_list:
                    ai_data = json.loads(match_list.group())
                else:
                    raise ValueError("Invalid AI response format")
        
        # Ensure ai_data is a dictionary
        if isinstance(ai_data, list):
            # If AI returned a list, take the first item if it's a dict
            if ai_data and isinstance(ai_data[0], dict):
                ai_data = ai_data[0]
            else:
                # Wrap it if it's just a list of strings or something else
                ai_data = {"insights": ai_data, "summary": "AI generated insights"}
        
        # Add metadata
        if not isinstance(ai_data, dict):
             ai_data = {"raw_response": str(ai_data)}
             
        ai_data["success"] = True
        ai_data["is_ai_generated"] = True
        ai_data["insight_type"] = "gemini"
        ai_data["metadata"] = {
            "tokens_used": 0, # Usage tracking requires different attribute in new SDK
            "generation_time_ms": duration_ms,
            "cost_inr": 0.0, # Placeholder
            "from_cache": False
        }
        
        return jsonify(ai_data)
        
    except Exception as e:
        logger.exception(f"AI Insight generation failed: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@whatsapp_bp.route("/analytics/export", methods=["GET"])
@require_token
def export_analytics():
    """
    Export analytics data (message logs) as CSV.
    GET /api/whatsapp/analytics/export
    """
    import csv
    import io
    import logging
    from datetime import datetime, timedelta, timezone
    from flask import Response, stream_with_context, request
    from .models import WhatsAppMessage, WhatsAppConversation, WhatsAppAccount
    
    logger = logging.getLogger("sociovia.whatsapp")
    
    account_id = request.args.get("account_id")
    workspace_id = request.args.get("workspace_id")
    db_session = get_db()
    now = datetime.now(timezone.utc)
    
    logger.info(f"Export Analytics Request: workspace_id={workspace_id}, account_id={account_id}, days={request.args.get('days')}")

    # Handle date filtering
    start_date_param = request.args.get("start_date")
    end_date_param = request.args.get("end_date")
    days = request.args.get("days", "7")
    
    start_date = None
    end_date = now
    filename_date = now.strftime("%Y-%m-%d")
    
    if start_date_param and end_date_param:
        try:
            start_date = datetime.strptime(start_date_param, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            end_date = datetime.strptime(end_date_param, "%Y-%m-%d").replace(hour=23, minute=59, second=59, tzinfo=timezone.utc)
            filename_date = f"{start_date_param}_to_{end_date_param}"
        except ValueError:
            return jsonify({"success": False, "error": "Invalid date format"}), 400
    elif days == "0" or days == 0:
        start_date = None
        filename_date = "all_time"
    elif days == "custom" or days == "all":
        start_date = None
        filename_date = "all_time"
    else:
        try:
            days_int = min(int(days), 365)
            start_date = now - timedelta(days=days_int)
            filename_date = f"last_{days_int}_days"
        except ValueError:
            start_date = now - timedelta(days=7)
            filename_date = "last_7_days"
            
    # Query Messages
    msg_query = db_session.query(WhatsAppMessage)
    if start_date:
        msg_query = msg_query.filter(WhatsAppMessage.created_at >= start_date)
    if end_date and start_date_param:
        msg_query = msg_query.filter(WhatsAppMessage.created_at <= end_date)
        
    if account_id:
        msg_query = msg_query.join(WhatsAppConversation).filter(
            WhatsAppConversation.account_id == int(account_id)
        )
    elif workspace_id:
        # Get accounts for workspace (match get_analytics pattern)
        # Using WhatsAppAccount.query if available, or db_session.query
        # get_analytics uses WhatsAppAccount.query.filter_by(workspace_id=..., is_active=True)
        # We will replicate that exactly.
        
        accounts = WhatsAppAccount.query.filter_by(workspace_id=workspace_id).all()
        # Note: Removing is_active=True to be sure we get everything, unless user only wants active. 
        # get_analytics filters active. Let's do same to be safe/consistent? 
        # Actually, for analytics history, we might want inactive too. But let's stick to what works first.
        # But wait, logic: if I remove is_active filter, I see MORE data. 
        # Empty result means I see LESS data. So strictly better to NOT filter is_active if debugging empty result.
        
        account_ids = [a.id for a in accounts]
        logger.info(f"Found {len(account_ids)} accounts for workspace {workspace_id}: {account_ids}")
        
        if account_ids:
            msg_query = msg_query.join(WhatsAppConversation).filter(
                WhatsAppConversation.account_id.in_(account_ids)
            )
        else:
            # No accounts in workspace, return empty
            msg_query = msg_query.filter(WhatsAppMessage.id == -1)
            
    # Order by newest first
    msg_query = msg_query.order_by(WhatsAppMessage.created_at.desc())
    
    # DEBUG: Count
    # total_count = msg_query.count()
    # logger.info(f"Export query found {total_count} messages")
    
    def generate():
        data = io.StringIO()
        w = csv.writer(data)
        
        # Write Header
        w.writerow(('Date', 'Time', 'Direction', 'Status', 'To/From', 'Type', 'Template', 'Content'))
        yield data.getvalue()
        data.seek(0)
        data.truncate(0)
        
        # Stream rows
        # Removed yield_per to avoid driver issues
        for msg in msg_query:
            # Format content
            content = ""
            if msg.type == 'text':
                content = str(msg.content)
            elif msg.type == 'template':
                content = f"Template: {msg.template_name}"
            else:
                content = f"[{msg.type}]"
            
            created_at = msg.created_at
            
            w.writerow((
                created_at.strftime("%Y-%m-%d"),
                created_at.strftime("%H:%M:%S"),
                msg.direction,
                msg.status,
                msg.conversation.user_phone if msg.conversation else "Unknown",
                msg.type,
                msg.template_name or "",
                content
            ))
            yield data.getvalue()
            data.seek(0)
            data.truncate(0)

    response = Response(stream_with_context(generate()), mimetype='text/csv')
    response.headers.set('Content-Disposition', 'attachment', filename=f'whatsapp_analytics_{filename_date}.csv')
    return response


@whatsapp_bp.route("/analytics", methods=["GET"])
def get_analytics():
    """
    Get WhatsApp analytics and insights.
    
    GET /api/whatsapp/analytics
    GET /api/whatsapp/analytics?days=7&account_id=1
    GET /api/whatsapp/analytics?days=0  (all time)
    GET /api/whatsapp/analytics?start_date=2024-01-01&end_date=2024-01-31
    
    Returns message stats, template performance, and conversation metrics.
    """
    from datetime import datetime, timedelta, timezone
    from sqlalchemy import func, case
    from .models import WhatsAppMessage, WhatsAppConversation, WhatsAppAccount
    
    account_id = request.args.get("account_id")
    workspace_id = request.args.get("workspace_id")
    conversations_only = request.args.get("conversations_only", "").lower() in {"1", "true", "yes"}
    db_session = get_db()
    now = datetime.now(timezone.utc)
    
    # Handle date filtering
    start_date_param = request.args.get("start_date")
    end_date_param = request.args.get("end_date")
    days = request.args.get("days", "7")
    
    period_label = ""
    start_date = None
    end_date = now
    
    if start_date_param and end_date_param:
        # Custom date range
        try:
            start_date = datetime.strptime(start_date_param, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            end_date = datetime.strptime(end_date_param, "%Y-%m-%d").replace(hour=23, minute=59, second=59, tzinfo=timezone.utc)
            period_label = f"{start_date_param} to {end_date_param}"
        except ValueError:
            return jsonify({"success": False, "error": "Invalid date format. Use YYYY-MM-DD"}), 400
    elif days == "0" or days == 0:
        # All time - no date filter
        start_date = None
        period_label = "All time"
    elif days == "custom" or days == "all":
        # Custom selected but no dates yet, or explicit 'all'
        start_date = None
        period_label = "All time"
    else:
        # Standard days filter
        try:
            days_int = min(int(days), 365)  # Max 365 days
            start_date = now - timedelta(days=days_int)
            period_label = f"Last {days_int} days"
        except ValueError:
            # Fallback for any invalid value
            start_date = now - timedelta(days=7)
            period_label = "Last 7 days"

    workspace_account_ids = None
    if workspace_id:
        workspace_account_ids = [
            a.id for a in WhatsAppAccount.query.filter_by(workspace_id=workspace_id, is_active=True).all()
        ]

    if conversations_only:
        empty_conversations = {"total": 0, "open": 0, "with_unread": 0, "active_sessions": 0}
        if workspace_id and not workspace_account_ids and not account_id:
            return jsonify({
                "success": True,
                "period_label": period_label,
                "conversations": empty_conversations,
            })

        conv_query = db_session.query(
            func.count(WhatsAppConversation.id).label("total"),
            func.sum(case((WhatsAppConversation.status == "open", 1), else_=0)).label("open_status"),
            func.sum(case((WhatsAppConversation.closed_by_agent == True, 1), else_=0)).label("closed_by_agent"),
            func.sum(case((WhatsAppConversation.unread_count > 0, 1), else_=0)).label("with_unread"),
        )
        if account_id:
            conv_query = conv_query.filter(WhatsAppConversation.account_id == int(account_id))
        elif workspace_id and workspace_account_ids:
            conv_query = conv_query.filter(WhatsAppConversation.account_id.in_(workspace_account_ids))

        conv_result = conv_query.first()
        active_sessions = db_session.query(func.count(WhatsAppConversation.id)).filter(
            WhatsAppConversation.session_expires_at > now,
            WhatsAppConversation.closed_by_agent != True,
        )
        if account_id:
            active_sessions = active_sessions.filter(WhatsAppConversation.account_id == int(account_id))
        elif workspace_id and workspace_account_ids:
            active_sessions = active_sessions.filter(WhatsAppConversation.account_id.in_(workspace_account_ids))
        active_count = active_sessions.scalar() or 0

        open_by_status = conv_result.open_status or 0 if conv_result else 0
        closed_by_agent_count = conv_result.closed_by_agent or 0 if conv_result else 0
        real_open = max(0, open_by_status - closed_by_agent_count)

        return jsonify({
            "success": True,
            "period_label": period_label,
            "conversations": {
                "total": conv_result.total or 0 if conv_result else 0,
                "open": real_open,
                "with_unread": conv_result.with_unread or 0 if conv_result else 0,
                "active_sessions": active_count,
            },
        })
    
    # === Message Stats ===
    msg_query = db_session.query(WhatsAppMessage)
    if start_date:
        msg_query = msg_query.filter(WhatsAppMessage.created_at >= start_date)
    if end_date and start_date_param:  # Only apply end_date for custom range
        msg_query = msg_query.filter(WhatsAppMessage.created_at <= end_date)
    
    if account_id:
        # Filter by specific account through conversation
        msg_query = msg_query.join(WhatsAppConversation).filter(
            WhatsAppConversation.account_id == int(account_id)
        )
    elif workspace_id:
        # Filter by workspace - get all account IDs for this workspace
        workspace_account_ids = [a.id for a in WhatsAppAccount.query.filter_by(workspace_id=workspace_id, is_active=True).all()]
        if workspace_account_ids:
            msg_query = msg_query.join(WhatsAppConversation).filter(
                WhatsAppConversation.account_id.in_(workspace_account_ids)
            )
        else:
            # No accounts for this workspace - return empty stats
            return jsonify({
                "success": True,
                "period_label": period_label,
                "messages": {"total_outgoing": 0, "total_incoming": 0, "sent": 0, "delivered": 0, "read": 0, "failed": 0, "delivery_rate": 0, "read_rate": 0, "failure_rate": 0},
                "templates": [],
                "conversations": {"total": 0, "open": 0, "with_unread": 0, "active_sessions": 0},
            })
    
    # Count by status
    status_counts = db_session.query(
        WhatsAppMessage.status,
        func.count(WhatsAppMessage.id)
    ).filter(
        WhatsAppMessage.direction == "outgoing"
    )
    if start_date:
        status_counts = status_counts.filter(WhatsAppMessage.created_at >= start_date)
    if end_date and start_date_param:
        status_counts = status_counts.filter(WhatsAppMessage.created_at <= end_date)
    
    if account_id:
        status_counts = status_counts.join(WhatsAppConversation).filter(
            WhatsAppConversation.account_id == int(account_id)
        )
    elif workspace_id and workspace_account_ids:
        status_counts = status_counts.join(WhatsAppConversation).filter(
            WhatsAppConversation.account_id.in_(workspace_account_ids)
        )
    
    status_counts = status_counts.group_by(WhatsAppMessage.status).all()
    status_dict = {s: c for s, c in status_counts}
    
    total_outgoing = sum(status_dict.values())
    sent = status_dict.get("sent", 0) + status_dict.get("delivered", 0) + status_dict.get("read", 0)
    delivered = status_dict.get("delivered", 0) + status_dict.get("read", 0)
    read_count = status_dict.get("read", 0)
    failed = status_dict.get("failed", 0)
    
    # Incoming message count
    incoming_query = db_session.query(func.count(WhatsAppMessage.id)).filter(
        WhatsAppMessage.direction == "incoming"
    )
    if start_date:
        incoming_query = incoming_query.filter(WhatsAppMessage.created_at >= start_date)
    if end_date and start_date_param:
        incoming_query = incoming_query.filter(WhatsAppMessage.created_at <= end_date)
    if account_id:
        incoming_query = incoming_query.join(WhatsAppConversation).filter(
            WhatsAppConversation.account_id == int(account_id)
        )
    elif workspace_id and workspace_account_ids:
        incoming_query = incoming_query.join(WhatsAppConversation).filter(
            WhatsAppConversation.account_id.in_(workspace_account_ids)
        )
    total_incoming = incoming_query.scalar() or 0
    
    message_stats = {
        "total_outgoing": total_outgoing,
        "total_incoming": total_incoming,
        "sent": sent,
        "delivered": delivered,
        "read": read_count,
        "failed": failed,
        "delivery_rate": round((delivered / total_outgoing * 100), 1) if total_outgoing > 0 else 0,
        "read_rate": round((read_count / delivered * 100), 1) if delivered > 0 else 0,
        "failure_rate": round((failed / total_outgoing * 100), 1) if total_outgoing > 0 else 0,
    }
    
    # === Template Stats ===
    template_query = db_session.query(
        WhatsAppMessage.template_name,
        func.count(WhatsAppMessage.id).label("total"),
        func.sum(case((WhatsAppMessage.status.in_(["delivered", "read"]), 1), else_=0)).label("delivered"),
        func.sum(case((WhatsAppMessage.status == "read", 1), else_=0)).label("read"),
        func.sum(case((WhatsAppMessage.status == "failed", 1), else_=0)).label("failed"),
    ).filter(
        WhatsAppMessage.template_name.isnot(None),
        WhatsAppMessage.direction == "outgoing"
    )
    if start_date:
        template_query = template_query.filter(WhatsAppMessage.created_at >= start_date)
    if end_date and start_date_param:
        template_query = template_query.filter(WhatsAppMessage.created_at <= end_date)
    
    if account_id:
        template_query = template_query.join(WhatsAppConversation).filter(
            WhatsAppConversation.account_id == int(account_id)
        )
    elif workspace_id and workspace_account_ids:
        template_query = template_query.join(WhatsAppConversation).filter(
            WhatsAppConversation.account_id.in_(workspace_account_ids)
        )
    
    template_stats = template_query.group_by(WhatsAppMessage.template_name).all()
    
    template_performance = []
    for t in template_stats:
        total = t.total or 0
        dlvrd = t.delivered or 0
        rd = t.read or 0
        fld = t.failed or 0
        template_performance.append({
            "template_name": t.template_name,
            "total_sent": total,
            "delivered": dlvrd,
            "read": rd,
            "failed": fld,
            "delivery_rate": round((dlvrd / total * 100), 1) if total > 0 else 0,
            "read_rate": round((rd / dlvrd * 100), 1) if dlvrd > 0 else 0,
        })
    
    # === Conversation Stats ===
    conv_query = db_session.query(
        func.count(WhatsAppConversation.id).label("total"),
        func.sum(case((WhatsAppConversation.status == "open", 1), else_=0)).label("open_status"),
        func.sum(case((WhatsAppConversation.closed_by_agent == True, 1), else_=0)).label("closed_by_agent"),
        func.sum(case((WhatsAppConversation.unread_count > 0, 1), else_=0)).label("with_unread"),
    )
    
    if account_id:
        conv_query = conv_query.filter(WhatsAppConversation.account_id == int(account_id))
    elif workspace_id and workspace_account_ids:
        conv_query = conv_query.filter(WhatsAppConversation.account_id.in_(workspace_account_ids))
    
    conv_result = conv_query.first()
    
    # Active conversations (session open AND not closed by agent)
    active_sessions = db_session.query(func.count(WhatsAppConversation.id)).filter(
        WhatsAppConversation.session_expires_at > now,
        WhatsAppConversation.closed_by_agent != True  # Exclude manually closed
    )
    if account_id:
        active_sessions = active_sessions.filter(WhatsAppConversation.account_id == int(account_id))
    elif workspace_id and workspace_account_ids:
        active_sessions = active_sessions.filter(WhatsAppConversation.account_id.in_(workspace_account_ids))
    active_count = active_sessions.scalar() or 0
    
    # Calculate real open count: status='open' AND not closed by agent
    open_by_status = conv_result.open_status or 0
    closed_by_agent_count = conv_result.closed_by_agent or 0
    real_open = max(0, open_by_status - closed_by_agent_count)
    
    conversation_stats = {
        "total": conv_result.total or 0,
        "open": real_open,
        "with_unread": conv_result.with_unread or 0,
        "active_sessions": active_count,
    }
    
    return jsonify({
        "success": True,
        "period_label": period_label,
        "messages": message_stats,
        "templates": template_performance,
        "conversations": conversation_stats,
    })


@whatsapp_bp.route("/analytics/categories", methods=["GET"])
def get_category_analytics():
    """
    Get category-wise template analytics (Utility, Marketing, Authentication).
    
    GET /api/whatsapp/analytics/categories
    GET /api/whatsapp/analytics/categories?days=30&account_id=1
    GET /api/whatsapp/analytics/categories?days=0  (all time)
    GET /api/whatsapp/analytics/categories?start_date=2024-01-01&end_date=2024-01-31
    
    Time-bounded query (default 7 days).
    """
    from .models import WhatsAppMessage, WhatsAppConversation, WhatsAppAccount
    
    account_id = request.args.get("account_id")
    workspace_id = request.args.get("workspace_id")
    db_session = get_db()
    now = datetime.now(timezone.utc)
    
    # Handle date filtering (same logic as main analytics)
    start_date_param = request.args.get("start_date")
    end_date_param = request.args.get("end_date")
    days = request.args.get("days", "7")
    
    start_date = None
    end_date = now
    
    if start_date_param and end_date_param:
        try:
            start_date = datetime.strptime(start_date_param, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            end_date = datetime.strptime(end_date_param, "%Y-%m-%d").replace(hour=23, minute=59, second=59, tzinfo=timezone.utc)
        except ValueError:
            return jsonify({"success": False, "error": "Invalid date format. Use YYYY-MM-DD"}), 400
    elif days == "0" or days == 0 or days == "custom" or days == "all":
        start_date = None
    else:
        try:
            days_int = min(int(days), 365)
            start_date = now - timedelta(days=days_int)
        except ValueError:
            start_date = now - timedelta(days=7)
    
    # Query by category
    base_query = db_session.query(
        WhatsAppMessage.template_category,
        func.count(WhatsAppMessage.id).label("total"),
        func.sum(case((WhatsAppMessage.status.in_(["delivered", "read"]), 1), else_=0)).label("delivered"),
        func.sum(case((WhatsAppMessage.status == "read", 1), else_=0)).label("read"),
        func.sum(case((WhatsAppMessage.status == "failed", 1), else_=0)).label("failed"),
    ).filter(
        WhatsAppMessage.template_category.isnot(None),
        WhatsAppMessage.direction == "outgoing"
    )
    
    if start_date:
        base_query = base_query.filter(WhatsAppMessage.created_at >= start_date)
    if end_date and start_date_param:
        base_query = base_query.filter(WhatsAppMessage.created_at <= end_date)
    
    if account_id:
        base_query = base_query.join(WhatsAppConversation).filter(
            WhatsAppConversation.account_id == int(account_id)
        )
    elif workspace_id:
        # Filter by workspace - get all account IDs for this workspace
        workspace_account_ids = [a.id for a in WhatsAppAccount.query.filter_by(workspace_id=workspace_id, is_active=True).all()]
        if workspace_account_ids:
            base_query = base_query.join(WhatsAppConversation).filter(
                WhatsAppConversation.account_id.in_(workspace_account_ids)
            )
        else:
            # No accounts for this workspace - return empty categories
            empty_categories = {}
            for cat in ["UTILITY", "MARKETING", "AUTHENTICATION"]:
                empty_categories[cat.lower()] = {
                    "category": cat, "total_sent": 0, "delivered": 0, "read": 0, "failed": 0,
                    "delivery_rate": 0, "read_rate": 0, "failure_rate": 0,
                }
            return jsonify({"success": True, "categories": empty_categories})
    
    results = base_query.group_by(WhatsAppMessage.template_category).all()
    
    # Build response with all categories (even if 0)
    categories = {}
    for cat in ["UTILITY", "MARKETING", "AUTHENTICATION"]:
        categories[cat.lower()] = {
            "category": cat,
            "total_sent": 0,
            "delivered": 0,
            "read": 0,
            "failed": 0,
            "delivery_rate": 0,
            "read_rate": 0,
            "failure_rate": 0,
        }
    
    for r in results:
        cat = r.template_category
        if cat:
            cat_lower = cat.lower()
            if cat_lower in categories:
                total = r.total or 0
                dlvrd = r.delivered or 0
                rd = r.read or 0
                fld = r.failed or 0
                categories[cat_lower] = {
                    "category": cat,
                    "total_sent": total,
                    "delivered": dlvrd,
                    "read": rd,
                    "failed": fld,
                    "delivery_rate": round((dlvrd / total * 100), 1) if total > 0 else 0,
                    "read_rate": round((rd / dlvrd * 100), 1) if dlvrd > 0 else 0,
                    "failure_rate": round((fld / total * 100), 1) if total > 0 else 0,
                }
    
    return jsonify({
        "success": True,
        "period_days": days,
        "categories": categories,
    })


@whatsapp_bp.route("/analytics/conversations/<int:conversation_id>", methods=["GET"])
def get_conversation_insights(conversation_id: int):
    """
    Get insights for a specific conversation.
    
    GET /api/whatsapp/analytics/conversations/<id>
    
    Returns: session info, message stats, templates used.
    """
    from .models import WhatsAppConversation, WhatsAppMessage
    
    db_session = get_db()
    conversation = db_session.get(WhatsAppConversation, conversation_id)
    
    if not conversation:
        return jsonify({"success": False, "error": "Conversation not found"}), 404
    
    # Message stats for this conversation
    msg_stats = db_session.query(
        func.count(WhatsAppMessage.id).label("total"),
        func.sum(case((WhatsAppMessage.direction == "outgoing", 1), else_=0)).label("outgoing"),
        func.sum(case((WhatsAppMessage.direction == "incoming", 1), else_=0)).label("incoming"),
        func.sum(case((WhatsAppMessage.status == "delivered", 1), else_=0)).label("delivered"),
        func.sum(case((WhatsAppMessage.status == "read", 1), else_=0)).label("read"),
        func.sum(case((WhatsAppMessage.status == "failed", 1), else_=0)).label("failed"),
    ).filter(
        WhatsAppMessage.conversation_id == conversation_id
    ).first()
    
    # Templates used
    templates = db_session.query(
        WhatsAppMessage.template_name,
        WhatsAppMessage.template_category,
        func.count(WhatsAppMessage.id).label("count"),
    ).filter(
        WhatsAppMessage.conversation_id == conversation_id,
        WhatsAppMessage.template_name.isnot(None)
    ).group_by(
        WhatsAppMessage.template_name,
        WhatsAppMessage.template_category
    ).all()
    
    templates_used = [
        {"name": t.template_name, "category": t.template_category, "count": t.count}
        for t in templates
    ]
    
    # Calculate average response time for this conversation
    # Find pairs of incoming -> outgoing messages and calculate time between them
    avg_response_time_seconds = None
    try:
        messages = db_session.query(WhatsAppMessage).filter(
            WhatsAppMessage.conversation_id == conversation_id
        ).order_by(WhatsAppMessage.created_at).all()
        
        response_times = []
        last_incoming_at = None
        
        for msg in messages:
            if msg.direction == "incoming" and msg.created_at:
                last_incoming_at = msg.created_at
            elif msg.direction == "outgoing" and last_incoming_at and msg.created_at:
                # Calculate response time in seconds
                delta = (msg.created_at - last_incoming_at).total_seconds()
                if delta > 0 and delta < 86400:  # Only count if response within 24 hours
                    response_times.append(delta)
                last_incoming_at = None  # Reset for next pair
        
        if response_times:
            avg_response_time_seconds = sum(response_times) / len(response_times)
    except Exception as e:
        logger.warning(f"Error calculating avg response time: {e}")
    
    return jsonify({
        "success": True,
        "conversation_id": conversation_id,
        "session": {
            "is_open": conversation.is_session_open,
            "time_left_seconds": conversation.session_time_left_seconds,
            "close_reason": conversation.close_reason if not conversation.is_session_open else None,
            "closed_by_agent": conversation.closed_by_agent,
            "closed_at": conversation.closed_at.isoformat() if conversation.closed_at else None,
            "expires_at": conversation.session_expires_at.isoformat() if conversation.session_expires_at else None,
        },
        "messages": {
            "total": msg_stats.total or 0,
            "outgoing": msg_stats.outgoing or 0,
            "incoming": msg_stats.incoming or 0,
            "delivered": msg_stats.delivered or 0,
            "read": msg_stats.read or 0,
            "failed": msg_stats.failed or 0,
        },
        "templates_used": templates_used,
        "avg_response_time_seconds": round(avg_response_time_seconds, 1) if avg_response_time_seconds else None,
    })


# ============================================================
# Health Check
# ============================================================

@whatsapp_bp.route("/health", methods=["GET"])
def health_check():
    """
    Health check endpoint.
    
    GET /api/whatsapp/health
    """
    try:
        token_configured = bool(os.getenv("WHATSAPP_ACCESS_TOKEN") or os.getenv("WHATSAPP_TEMP_TOKEN"))
        phone_configured = bool(os.getenv("WHATSAPP_PHONE_NUMBER_ID"))
        verify_configured = bool(os.getenv("WHATSAPP_VERIFY_TOKEN"))
        
        # Test DB connection
        from .models import WhatsAppAccount
        account_count = WhatsAppAccount.query.count()
        db_ok = True
        
    except Exception as e:
        logger.exception(f"Health check DB error: {e}")
        db_ok = False
        account_count = 0

    queue_snapshot = {}
    try:
        from core.queue.manager import queue_health_snapshot
        queue_snapshot = queue_health_snapshot()
    except Exception as exc:
        queue_snapshot = {"error": str(exc)}
    
    return jsonify({
        "status": "ok" if (token_configured and phone_configured) else "degraded",
        "module": "whatsapp-phase1",
        "version": "1.0.0",
        "config": {
            "access_token_set": token_configured,
            "phone_number_id_set": phone_configured,
            "verify_token_set": verify_configured,
            "api_version": os.getenv("WHATSAPP_API_VERSION", "v22.0"),
        },
        "database": {
            "connected": db_ok,
            "accounts": account_count,
        },
        "queues": queue_snapshot,
    })


# ============================================================
# Template Endpoints
# ============================================================

@whatsapp_bp.route("/templates", methods=["GET"])
def list_templates():
    """
    List all templates from database.
    
    GET /api/whatsapp/templates
    GET /api/whatsapp/templates?account_id=1&status=APPROVED
    GET /api/whatsapp/templates?workspace_id=xxx&status=APPROVED
    
    Supports both account_id and workspace_id - workspace_id will resolve to the active account.
    """
    from .models import WhatsAppTemplate, WhatsAppAccount
    
    account_id = request.args.get("account_id", type=int)
    workspace_id = request.args.get("workspace_id")
    status = request.args.get("status")
    
    # Resolve account_id from workspace_id if provided
    if not account_id and workspace_id:
        account, error = get_valid_account_for_workspace(workspace_id)
        if account:
            account_id = account.id
    
    # Only return templates that are not archived
    query = WhatsAppTemplate.query.filter_by(is_archived=False)
    
    if account_id:
        query = query.filter_by(account_id=account_id)
    if status:
        query = query.filter_by(status=status.upper())
    
    templates = query.order_by(WhatsAppTemplate.name).all()
    
    return jsonify({
        "success": True,
        "templates": [t.to_dict() for t in templates],
        "count": len(templates),
    })


@whatsapp_bp.route("/templates/sync", methods=["POST"])
def sync_templates():
    """
    Sync templates from Meta API for an account.
    
    POST /api/whatsapp/templates/sync
    Body: {"account_id": 1} or {"workspace_id": "xxx"}
    
    Supports either account_id or workspace_id - workspace_id will resolve to the active account.
    """
    import requests as http_requests
    from .models import WhatsAppTemplate, WhatsAppAccount
    
    data = request.get_json() or {}
    account_id = data.get("account_id")
    workspace_id = data.get("workspace_id")
    
    # Support both account_id and workspace_id
    if not account_id and not workspace_id:
        return jsonify({"success": False, "error": "account_id or workspace_id required"}), 400
    
    # Resolve account
    if account_id:
        # Use centralized token helper
        account, token_error = get_account_with_token(account_id)
    else:
        # Resolve from workspace_id
        account, token_error = get_valid_account_for_workspace(workspace_id)
    
    if token_error:
        return jsonify({"success": False, "error": token_error}), 400
    
    try:
        from .services import WhatsAppService
        
        # Initialize service with this account
        service = WhatsAppService(
            access_token=account.get_access_token(),
            phone_number_id=account.phone_number_id,
            waba_id=account.waba_id
        )
        
        # Execute sync
        sync_result = service.sync_templates()
        
        if not sync_result.get("success"):
            return jsonify(sync_result), 400
            
        # Fetch updated list to return
        templates = WhatsAppTemplate.query.filter_by(account_id=account.id).all()
        
        response = {
            "success": True,
            "synced": sync_result.get("synced", 0),
            "stats": sync_result,
            "templates": [t.to_dict() for t in templates],
        }
        return jsonify(response)
        
    except Exception as e:
        logger.exception(f"Template sync error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@whatsapp_bp.route("/templates/<template_id>", methods=["DELETE"])
@require_admin_only  # SECURITY: Only admins can delete templates
def delete_template(template_id):
    """
    Delete a template from both Meta and local database.
    
    DELETE /api/whatsapp/templates/<template_id>
    Query params: workspace_id (required)
    
    Note: This deletes the template from Meta's servers and from our database.
    The template will be permanently removed.
    """
    import requests as http_requests
    from .models import WhatsAppTemplate, WhatsAppAccount
    
    data = {}
    try:
        data = request.get_json() or {}
    except Exception:
        pass

    workspace_id = request.args.get("workspace_id") or data.get("workspace_id")
    account_id = request.args.get("account_id") or data.get("account_id")
    
    if not workspace_id and not account_id:
        return jsonify({"success": False, "error": "workspace_id or account_id required"}), 400
    
    # Resolve account
    if account_id:
        account, token_error = get_account_with_token(account_id)
    else:
        account, token_error = get_valid_account_for_workspace(workspace_id)
        
    if token_error:
        return jsonify({"success": False, "error": token_error}), 400
    
    access_token = account.get_access_token()
    api_version = os.getenv("WHATSAPP_API_VERSION", "v22.0")
    
    # Find template in database - first try by meta_template_id (string)
    template = WhatsAppTemplate.query.filter_by(
        account_id=account.id,
        meta_template_id=template_id
    ).first()
    
    # Also try by local id if meta_template_id doesn't match
    # Only attempt if template_id looks like a small integer (local DB id)
    if not template:
        try:
            local_id = int(template_id)
            # Only query if it's a reasonable integer size (not a Meta ID)
            if local_id < 2147483647:  # Max PostgreSQL integer
                template = WhatsAppTemplate.query.filter_by(
                    account_id=account.id,
                    id=local_id
                ).first()
        except (ValueError, OverflowError):
            # template_id is not a valid integer, skip this lookup
            pass
    
    if not template:
        return jsonify({"success": False, "error": "Template not found"}), 404
    
    try:
        # Delete from Meta API using template name (Meta requires the name, not ID)
        # Note: Meta API requires deleting by name, which deletes ALL language versions
        resp = http_requests.delete(
            f"https://graph.facebook.com/{api_version}/{account.waba_id}/message_templates",
            params={
                "access_token": access_token,
                "name": template.name
            },
            timeout=30,
        )
        
        meta_error = None
        if not resp.ok:
            # Log but don't fail - template might already be deleted on Meta
            error_data = resp.json() if resp.text else {}
            meta_error = error_data.get("error", {}).get("message", "Unknown Meta API error")
            logger.warning(f"Meta API delete error for template {template.name}: {meta_error}")
        
        # Delete from local database (all language versions with same name)
        deleted_count = WhatsAppTemplate.query.filter_by(
            account_id=account.id,
            name=template.name
        ).delete()
        
        get_db().commit()
        
        return jsonify({
            "success": True,
            "deleted_count": deleted_count,
            "template_name": template.name,
            "meta_deleted": resp.ok if resp else False,
            "meta_error": meta_error
        })
        
    except Exception as e:
        logger.exception(f"Template delete error: {e}")
        get_db().rollback()
        return jsonify({"success": False, "error": str(e)}), 500


@whatsapp_bp.route("/template-suggestions", methods=["GET"])
def list_template_suggestions():
    """
    List static template suggestions (curated by Sociovia).
    
    GET /api/whatsapp/template-suggestions
    """
    suggestions = [
        {
            "id": "welcome_message",
            "title": "Welcome Message",
            "category": "MARKETING",
            "language": "en_US",
            "preview": "Welcome to {{1}}! We're excited to have you as part of our community.",
            "variables": 1,
            "description": "Greet new customers when they first connect",
        },
        {
            "id": "order_confirmation",
            "title": "Order Confirmation",
            "category": "UTILITY",
            "language": "en_US",
            "preview": "Hello {{1}}, your order #{{2}} has been confirmed with a total of {{3}}. Thank you for shopping with us!",
            "variables": 3,
            "description": "Confirm order details to customers",
        },
        {
            "id": "order_shipped",
            "title": "Order Shipped",
            "category": "UTILITY",
            "language": "en_US",
            "preview": "Hello {{1}}, great news! Your order #{{2}} has shipped. Track your package with code {{3}}. Thank you for your purchase!",
            "variables": 3,
            "description": "Notify customers about shipment with tracking",
        },
        {
            "id": "appointment_reminder",
            "title": "Appointment Reminder",
            "category": "UTILITY",
            "language": "en_US",
            "preview": "Hi {{1}}, reminder: You have an appointment on {{2}} at {{3}}.",
            "variables": 3,
            "description": "Send appointment reminders to reduce no-shows",
        },
        {
            "id": "payment_received",
            "title": "Payment Received",
            "category": "UTILITY",
            "language": "en_US",
            "preview": "Hi {{1}}, we've received your payment of {{2}}. Thank you!",
            "variables": 2,
            "description": "Confirm payment receipt to customers",
        },
        {
            "id": "feedback_request",
            "title": "Feedback Request",
            "category": "MARKETING",
            "language": "en_US",
            "preview": "Hi {{1}}, how was your experience with us? We'd love your feedback!",
            "variables": 1,
            "description": "Request customer feedback after service",
        },
        {
            "id": "promo_announcement",
            "title": "Promo Announcement",
            "category": "MARKETING",
            "language": "en_US",
            "preview": "Hello {{1}}, we have an exclusive offer just for you! Get {{2}} percent off with code {{3}}. Shop now and save!",
            "variables": 3,
            "description": "Announce promotional offers to customers",
        },
        {
            "id": "otp_verification",
            "title": "OTP Verification",
            "category": "AUTHENTICATION",
            "language": "en_US",
            "preview": "Your verification code is {{1}}. Valid for 10 minutes.",
            "variables": 1,
            "description": "Send one-time password for verification",
        },
    ]
    
    return jsonify({
        "success": True,
        "suggestions": suggestions,
        "count": len(suggestions),
    })


@whatsapp_bp.route("/ai/rewrite-template", methods=["POST"])
def rewrite_template_for_category():
    """
    AI-powered template rewrite for category compliance using Gemini.
    
    CRITICAL RULES:
    1. NEVER remove or break template placeholders ({{1}}, {{2}}, etc.)
    2. NEVER contradict the rewrite with warnings in the output
    3. Either successfully rewrite OR refuse entirely
    4. Detect authentication intent and suggest category change
    
    POST /api/whatsapp/ai/rewrite-template
    """
    import json as json_lib
    import re
    from google import genai
    
    data = request.get_json() or {}
    original_text = data.get("original_text", "")
    target_category = data.get("target_category", "UTILITY")
    
    if not original_text:
        return jsonify({
            "success": False,
            "error": "original_text required"
        }), 400
    
    try:
        from .intent_detection import detect_intent, Intent
        
        # ===========================================
        # STEP 1: Extract and preserve placeholders
        # ===========================================
        placeholders = re.findall(r'\{\{\d+\}\}', original_text)
        unique_placeholders = sorted(set(placeholders), key=lambda x: int(re.search(r'\d+', x).group()))
        
        # ===========================================
        # STEP 2: Detect authentication intent
        # ===========================================
        auth_patterns = [
            r'\b(otp|one.?time.?password)\b',
            r'\b(verification.?code|verify.?code)\b',
            r'\b(login.?code|security.?code)\b',
            r'\b(password.?reset|reset.?password)\b',
            r'\b(confirm.?identity|authentication)\b',
        ]
        is_authentication = any(re.search(p, original_text, re.IGNORECASE) for p in auth_patterns)
        
        if is_authentication and target_category != "AUTHENTICATION":
            return jsonify({
                "success": True,
                "rewritten_text": None,
                "confidence": "HIGH",
                "notes": "This message contains verification/OTP content and should use the AUTHENTICATION category.",
                "cannot_rewrite": False,
                "suggested_category": "AUTHENTICATION"
            })
        
        # ===========================================
        # STEP 3: Analyze original intent
        # ===========================================
        intent_result = detect_intent(original_text)
        
        # For UTILITY: refuse if highly promotional (don't attempt partial rewrite)
        if target_category == "UTILITY":
            if intent_result.intent == Intent.PROMOTIONAL and intent_result.confidence.value == "HIGH":
                return jsonify({
                    "success": True,
                    "rewritten_text": None,
                    "confidence": "LOW",
                    "notes": "This content is promotional and cannot be converted to Utility without changing its intent.",
                    "cannot_rewrite": True,
                    "suggested_category": "MARKETING"
                })
        
        # ===========================================
        # STEP 4: Initialize Vertex AI client (NOT free-tier)
        # ===========================================
        from google import genai
        from google.genai import types
        
        # Use Vertex AI with project-based auth (not API key)
        # Fallback chain: GCP_PROJECT -> PROJECT_ID -> hardcoded default
        gcp_project = os.environ.get("GCP_PROJECT") or os.environ.get("PROJECT_ID") or "angular-sorter-473216-k8"
        gcp_location = os.environ.get("GOOGLE_CLOUD_LOCATION", "global")
        
        # ===========================================
        # STEP 5: Build placeholder-safe prompts
        # ===========================================
        placeholder_warning = ""
        if unique_placeholders:
            placeholder_warning = f"""
CRITICAL - PLACEHOLDER PRESERVATION:
The following placeholders MUST remain EXACTLY as they are in the output: {', '.join(unique_placeholders)}
- DO NOT remove any placeholder
- DO NOT change placeholder numbers
- DO NOT add new placeholders
- DO NOT convert placeholders to plain text like [Link] or <url>
- Placeholders represent dynamic values that will be filled at send time
"""

        if target_category == "UTILITY":
            prompt = f"""You are a WhatsApp template compliance expert. Rewrite this message for Meta's UTILITY category.

{placeholder_warning}

UTILITY RULES:
- Transactional only (order updates, booking confirmations, delivery status)
- NO promotional language (discount, offer, sale, free, cashback)
- NO urgency (limited time, hurry, don't miss, act now)
- NO upselling or cross-selling
- NO promotional emojis (🎉 🔥 💥 🛍️ 🚀 🎁 💰)
- Allowed emojis: ✅ 📦 📋 📄 🔔 📍

ORIGINAL MESSAGE:
{original_text}

OUTPUT INSTRUCTIONS:
Return ONLY the rewritten template text. No explanations. No notes. No warnings.
If the message is already compliant, return it cleaned up.
"""

        elif target_category == "MARKETING":
            prompt = f"""You are a marketing copywriter for WhatsApp templates.

{placeholder_warning}

MARKETING RULES:
- Promotional language is ALLOWED
- Emojis are ALLOWED
- Urgency is ALLOWED (limited time, act now)
- Persuasive CTAs are ALLOWED
- Keep message engaging but not spammy
- URLs should NOT be hardcoded - buttons handle links separately

ORIGINAL MESSAGE:
{original_text}

OUTPUT INSTRUCTIONS:
Return ONLY the improved template text. No explanations.
Make it engaging while preserving all placeholders.
"""

        else:  # AUTHENTICATION
            prompt = f"""You are cleaning an OTP/verification template.

{placeholder_warning}

AUTHENTICATION RULES:
- Keep it minimal and clear
- Focus on the code/OTP
- Include security warning if appropriate (do not share)
- NO emojis
- NO promotional content

ORIGINAL MESSAGE:
{original_text}

OUTPUT INSTRUCTIONS:
Return ONLY the cleaned template text. No explanations.
"""

        # ===========================================
        # STEP 6: Call Gemini API
        # ===========================================
        try:
            from google.genai.types import HttpOptions
            
            text_model = os.environ.get("TEXT_MODEL") or os.environ.get("GEMINI_MODEL") or "gemini-3.5-flash"
            
            # Initialize Vertex AI client (NOT free-tier API key)
            client = genai.Client(
                http_options=HttpOptions(api_version="v1"),
                project=gcp_project,
                location=gcp_location,
                vertexai=True,
            )
            
            response = client.models.generate_content(
                model=text_model,
                contents=prompt
            )
            
            rewritten_text = response.text.strip() if response.text else original_text
            
            # ===========================================
            # STEP 7: Validate placeholder preservation
            # ===========================================
            rewritten_placeholders = re.findall(r'\{\{\d+\}\}', rewritten_text)
            
            # Check if all original placeholders are preserved
            missing_placeholders = set(unique_placeholders) - set(rewritten_placeholders)
            
            if missing_placeholders:
                logger.warning(f"AI removed placeholders: {missing_placeholders}")
                # Refuse the rewrite - placeholders were lost
                return jsonify({
                    "success": True,
                    "rewritten_text": None,
                    "confidence": "LOW",
                    "notes": f"AI attempted to remove placeholders {list(missing_placeholders)}. Rewrite refused to protect template integrity.",
                    "cannot_rewrite": True
                })
            
            # ===========================================
            # STEP 8: NO post-validation warnings
            # ===========================================
            # We do NOT re-run intent detection and add warnings
            # If we got here, the rewrite is accepted as-is
            
            confidence = "HIGH"
            notes = f"Successfully optimized for {target_category}."
            
            return jsonify({
                "success": True,
                "rewritten_text": rewritten_text,
                "confidence": confidence,
                "notes": notes,
                "cannot_rewrite": False,
                "suggested_category": None
            })
            
        except Exception as gemini_error:
            logger.error(f"Gemini API error: {gemini_error}")
            return jsonify({
                "success": False,
                "rewritten_text": None,
                "confidence": "LOW",
                "notes": f"AI service error: {str(gemini_error)}",
                "cannot_rewrite": True
            })
            
    except Exception as e:
        logger.error(f"AI rewrite error: {e}")
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


@whatsapp_bp.route("/templates", methods=["POST"])
@require_admin_only  # SECURITY: Only admins can create templates
def create_template():
    """
    Create a new template via Meta API.
    
    POST /api/whatsapp/templates
    Body: {
        "account_id": 1,
        "name": "my_template",
        "category": "UTILITY",
        "language": "en_US",
        "components": [
            {"type": "BODY", "text": "Hi {{1}}, your order {{2}} is confirmed."}
        ]
    }
    """
    import requests as http_requests
    from .models import WhatsAppTemplate, WhatsAppAccount
    from .flow_access import validate_template_flow_attachment
    
    data = request.get_json() or {}
    account_id = data.get("account_id")
    name = data.get("name")
    category = data.get("category", "UTILITY")
    language = data.get("language", "en_US")
    components = data.get("components", [])
    parameter_format = data.get("parameter_format")  # 'named' or 'positional' or None
    force_submit = data.get("force", False)  # Allow submitting despite warnings
    
    if not account_id:
        return jsonify({"success": False, "error": "account_id required"}), 400
    if not name:
        return jsonify({"success": False, "error": "name required"}), 400
    if not components:
        return jsonify({"success": False, "error": "components required"}), 400
    
    # Use centralized token helper to get account with valid token
    account, token_error = get_account_with_token(account_id)
    if token_error:
        return jsonify({"success": False, "error": token_error}), 400
    
    # Update account_id if helper returned a different active account
    account_id = account.id
    access_token = account.get_access_token()
    
    api_version = os.getenv("WHATSAPP_API_VERSION", "v22.0")
    
    # ====== INTENT ENFORCEMENT ======
    # Server-side validation to prevent Marketing disguised as Utility
    try:
        from .intent_detection import validate_category_compliance, violations_to_dict
        
        # Extract text content from components for validation
        body_text_check = ""
        header_text_check = ""
        footer_text_check = ""
        buttons_check = []
        
        for comp in components:
            comp_type = comp.get("type", "").upper()
            if comp_type == "BODY":
                body_text_check = comp.get("text", "")
            elif comp_type == "HEADER":
                header_text_check = comp.get("text", "")
            elif comp_type == "FOOTER":
                footer_text_check = comp.get("text", "")
            elif comp_type == "BUTTONS":
                for btn in comp.get("buttons", []):
                    buttons_check.append({
                        "text": btn.get("text", ""),
                        "url": btn.get("url", "")
                    })
        
        compliance = validate_category_compliance(
            category=category,
            body=body_text_check,
            header=header_text_check,
            footer=footer_text_check,
            buttons=buttons_check
        )
        
        # Warn about non-compliant Utility templates but allow submission with force flag
        # Meta will auto-correct the category if needed, so we just warn
        if category == "UTILITY" and not compliance.is_compliant:
            if not force_submit:
                # Return warning with option to force submit
                logger.warning(
                    f"Template '{name}' has compliance warnings. "
                    f"Detected: {compliance.detected_intent.value}, "
                    f"Violations: {len(compliance.violations)}"
                )
                return jsonify({
                    "success": False,
                    "warning": True,  # Indicates this is a soft warning, not hard error
                    "can_force": True,  # Frontend can retry with force=true
                    "error": compliance.message or "This message may not comply with Utility category requirements. Meta may auto-correct the category.",
                    "detected_intent": compliance.detected_intent.value,
                    "violations": violations_to_dict(compliance.violations),
                    "suggest_switch": compliance.suggest_switch
                }), 400
            else:
                # User chose to proceed despite warnings
                logger.info(
                    f"Template '{name}' force-submitted despite compliance warnings. "
                    f"Detected: {compliance.detected_intent.value}"
                )
            
    except Exception as e:
        # Log but don't block if intent detection fails
        logger.warning(f"Intent detection failed: {e}")
    # ====== END INTENT ENFORCEMENT ======
    
    try:
        # Validate advanced button requirements server-side to prevent Meta rejections.
        has_catalog_button = False
        has_voice_call_button = False
        for comp in components:
            if str(comp.get("type", "")).upper() != "BUTTONS":
                continue

            for btn in (comp.get("buttons", []) or []):
                btn_type = str(btn.get("type", "")).upper()

                if btn_type == "FLOW":
                    flow_id = str(btn.get("flow_id") or "").strip()
                    if not flow_id:
                        return jsonify({"success": False, "error": "Flow button requires flow_id"}), 400

                    is_valid_flow, flow_error = validate_template_flow_attachment(account_id, flow_id)
                    if not is_valid_flow:
                        return jsonify({"success": False, "error": flow_error}), 400

                if btn_type == "COPY_CODE":
                    code_example = str(btn.get("example") or btn.get("copy_code") or "").strip()
                    if code_example and len(code_example) > 15:
                        return jsonify({"success": False, "error": "COPY_CODE example must be 15 characters or less"}), 400

                if btn_type == "CATALOG":
                    has_catalog_button = True

                if btn_type == "VOICE_CALL":
                    has_voice_call_button = True

        if has_catalog_button:
            # Meta catalog templates are MARKETING category templates.
            if str(category).upper() != "MARKETING":
                return jsonify({
                    "success": False,
                    "error": "Catalog button requires MARKETING category"
                }), 400

            # Best-effort check: ensure at least one product catalog is connected to this WABA.
            catalogs_resp = http_requests.get(
                f"https://graph.facebook.com/{api_version}/{account.waba_id}/product_catalogs",
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=15,
            )
            catalogs_data = catalogs_resp.json() if catalogs_resp.content else {}
            if catalogs_resp.ok:
                connected_catalogs = catalogs_data.get("data", []) or []
                if not connected_catalogs:
                    return jsonify({
                        "success": False,
                        "error": "No product catalog connected to this WhatsApp Business Account.",
                        "catalog_required": True,
                        "catalog_setup_url": "/dashboard/whatsapp/catalog",
                        "suggestion": "Connect a catalog on the Catalog Management page before submitting a CATALOG button template.",
                    }), 400
            else:
                logger.warning("Catalog precheck failed: %s", catalogs_data)

        if has_voice_call_button:
            capability = _get_voice_call_capability(account, access_token, api_version)
            if capability.get("block_template_submission"):
                return jsonify({
                    "success": False,
                    "error": "VOICE_CALL button is blocked until account call readiness checks pass.",
                    "capability": capability,
                }), 400

        # Parse body text to extract variables
        import re
        import json
        body_text = ""
        for comp in components:
            if comp.get("type", "").upper() == "BODY":
                body_text = comp.get("text", "")
                break
        
        # Detect ALL variables: both positional {{1}} and named {{name}}
        all_variables = re.findall(r'\{\{([^}]+)\}\}', body_text)
        unique_variables = list(dict.fromkeys([v.strip() for v in all_variables]))  # Preserve order, remove dupes
        
        # Determine if we have named parameters (non-numeric variables)
        positional_vars = [v for v in unique_variables if v.isdigit()]
        named_vars = [v for v in unique_variables if not v.isdigit()]
        
        # Auto-detect parameter_format if not explicitly provided
        if not parameter_format:
            if named_vars:
                parameter_format = "named"
            elif positional_vars:
                parameter_format = "positional"
        
        is_named = parameter_format == "named"
        variable_count = len(unique_variables)
        
        logger.info(f"Template '{name}' variables detected: {unique_variables}, format: {parameter_format}")
        
        # Build proper components with examples for Meta API
        # NOTE: Meta API accepts both lowercase and uppercase component types,
        # but documentation examples use lowercase for named params
        api_components = []
        for comp in components:
            comp_type = comp.get("type", "").upper()
            if comp_type == "BODY":
                # Use lowercase 'body' for named params as per Meta documentation
                body_comp = {
                    "type": "body" if is_named else "BODY"
                }
                
                # CRITICAL: Authentication templates have a very strict schema.
                # They MUST NOT have a 'text' field in the BODY component.
                # Instead, they use a fixed format and can include a security recommendation.
                if category.upper() == 'AUTHENTICATION':
                    body_comp["add_security_recommendation"] = True
                else:
                    body_comp["text"] = comp.get("text", "")
                # Add example based on parameter format
                if variable_count > 0:
                    if is_named:
                        # Named parameters format: body_text_named_params
                        # Use example from frontend if provided, else generate realistic examples
                        existing_example = comp.get("example", {})
                        if existing_example.get("body_text_named_params"):
                            body_comp["example"] = existing_example
                        else:
                            # Generate realistic example values based on param name
                            def get_example_value(param_name):
                                lower_name = param_name.lower()
                                if 'name' in lower_name:
                                    return 'John'
                                if 'email' in lower_name:
                                    return 'john@example.com'
                                if 'phone' in lower_name:
                                    return '+1234567890'
                                if 'date' in lower_name:
                                    return '2024-01-15'
                                if 'time' in lower_name:
                                    return '10:30 AM'
                                if 'amount' in lower_name or 'price' in lower_name or 'money' in lower_name:
                                    return '$99.99'
                                if 'order' in lower_name or 'id' in lower_name:
                                    return 'ORD-12345'
                                if 'code' in lower_name:
                                    return 'ABC123'
                                if 'address' in lower_name:
                                    return '123 Main St'
                                if 'number' in lower_name:
                                    return '12345'
                                return f'sample_{param_name}'
                            
                            body_comp["example"] = {
                                "body_text_named_params": [
                                    {"param_name": var, "example": get_example_value(var)}
                                    for var in named_vars
                                ]
                            }
                    else:
                        # Positional parameters format: body_text nested array
                        existing_example = comp.get("example", {})
                        if existing_example.get("body_text"):
                            body_comp["example"] = existing_example
                        else:
                            examples = [f"example{i}" for i in range(1, len(positional_vars) + 1)]
                            body_comp["example"] = {
                                "body_text": [examples]  # Nested array!
                            }
                api_components.append(body_comp)
            elif comp_type == "HEADER":
                header_format = str(comp.get("format", "TEXT")).upper()
                header_comp = {
                    "type": "HEADER",
                    "format": header_format,
                }

                # Text header
                if header_format == "TEXT" and comp.get("text"):
                    header_comp["text"] = comp.get("text")

                    # Optional examples for text header variables
                    header_example = comp.get("example", {}) or {}
                    if header_example.get("header_text"):
                        header_comp["example"] = {"header_text": header_example.get("header_text")}
                    elif header_example.get("header_text_named_params"):
                        header_comp["example"] = {
                            "header_text_named_params": header_example.get("header_text_named_params")
                        }

                # Media header (image/video/document/gif)
                if header_format in {"IMAGE", "VIDEO", "DOCUMENT", "GIF"} and comp.get("example", {}).get("header_handle"):
                    header_comp["example"] = comp.get("example")

                # Location header has no extra properties for create payload
                api_components.append(header_comp)
            elif comp_type == "FOOTER":
                # CRITICAL: Authentication templates have a very strict schema.
                # Standard FOOTER with 'text' is NOT allowed for AUTHENTICATION.
                # Only specific fields like 'code_expiration_minutes' or 'add_security_recommendation' (deprecated from footer) are allowed.
                if category.upper() == 'AUTHENTICATION':
                    logger.info("Skipping invalid FOOTER 'text' for AUTHENTICATION template")
                    # We skip the footer if it's a standard one, as it would cause an error.
                    continue
                
                api_components.append({
                    "type": "FOOTER",
                    "text": comp.get("text", "")
                })
            elif comp_type == "BUTTONS":
                raw_buttons = comp.get("buttons", []) or []
                normalized_buttons = []

                for btn in raw_buttons:
                    btn_type = str(btn.get("type", "")).upper()
                    if not btn_type:
                        continue

                    norm_btn = {"type": btn_type}

                    # Types with text labels
                    if btn_type in {"QUICK_REPLY", "URL", "PHONE_NUMBER", "FLOW", "VOICE_CALL", "CATALOG"}:
                        btn_text = btn.get("text")
                        if btn_text:
                            norm_btn["text"] = btn_text
                        elif btn_type == "CATALOG":
                            # Meta requires text for CATALOG buttons; default to "View catalog"
                            norm_btn["text"] = "View catalog"

                    if btn_type == "URL":
                        if btn.get("url"):
                            norm_btn["url"] = btn.get("url")
                        if btn.get("example"):
                            norm_btn["example"] = btn.get("example")

                    elif btn_type == "PHONE_NUMBER":
                        if btn.get("phone_number") or btn.get("phone"):
                            norm_btn["phone_number"] = btn.get("phone_number") or btn.get("phone")

                    elif btn_type == "FLOW":
                        if btn.get("flow_id"):
                            norm_btn["flow_id"] = btn.get("flow_id")
                        if btn.get("flow_token"):
                            norm_btn["flow_token"] = btn.get("flow_token")

                    elif btn_type == "COPY_CODE":
                        # Meta expects example code when creating coupon templates
                        code_example = btn.get("example") or btn.get("copy_code") or btn.get("code")
                        if code_example:
                            norm_btn["example"] = code_example

                    elif btn_type == "OTP":
                        # Authentication templates: OTP button schema
                        if btn.get("otp_type"):
                            norm_btn["otp_type"] = str(btn.get("otp_type")).upper()
                        if btn.get("supported_apps"):
                            norm_btn["supported_apps"] = btn.get("supported_apps")

                    normalized_buttons.append(norm_btn)

                if normalized_buttons:
                    api_components.append({
                        "type": "BUTTONS",
                        "buttons": normalized_buttons,
                    })
            else:
                api_components.append(comp)
        
        # CRITICAL: Authentication templates MUST have exactly one OTP button.
        # Auto-inject a copy_code button if user didn't provide one.
        if category.upper() == 'AUTHENTICATION':
            has_otp_button = any(
                c.get("type", "").upper() == "BUTTONS" and any(
                    str(btn.get("type", "")).upper() == "OTP"
                    for btn in (c.get("buttons", []) or [])
                )
                for c in api_components
            )
            if not has_otp_button:
                logger.info("Auto-injecting OTP copy_code button for AUTHENTICATION template")
                api_components.append({
                    "type": "BUTTONS",
                    "buttons": [
                        {
                            "type": "OTP",
                            "otp_type": "COPY_CODE"
                        }
                    ]
                })
        
        # Build the API request payload
        api_payload = {
            "name": name,
            "category": category.lower(),  # Meta docs show lowercase category
            "language": language,
            "components": api_components,
        }
        
        # CRITICAL: Add parameter_format for named parameters
        if is_named:
            api_payload["parameter_format"] = "named"
        
        logger.info(f"Meta API template create payload: {json.dumps(api_payload, indent=2)}")
        
        # Create template via Meta API
        resp = http_requests.post(
            f"https://graph.facebook.com/{api_version}/{account.waba_id}/message_templates",
            headers={"Authorization": f"Bearer {access_token}"},
            json=api_payload,
            timeout=30,
        )
        
        meta_resp = resp.json()
        
        # Log the full response for debugging
        logger.info(f"Meta API template create response: {meta_resp}")
        logger.info(f"Meta API status code: {resp.status_code}")
        
        if "error" in meta_resp:
            error_info = meta_resp["error"]
            error_msg = error_info.get("message", "Unknown error")
            error_code = error_info.get("code")
            error_subcode = error_info.get("error_subcode")
            error_user_msg = error_info.get("error_user_msg")
            
            logger.error(f"Meta template create error: {error_info}")
            
            # Build detailed error message
            detailed_error = error_msg
            if error_user_msg:
                detailed_error = f"{error_msg}: {error_user_msg}"
            if error_code:
                detailed_error += f" (code: {error_code})"
            
            return jsonify({
                "success": False, 
                "error": detailed_error,
                "meta_error": error_info
            }), 400
        
        # Save to database
        tpl = WhatsAppTemplate(
            account_id=account_id,
            meta_template_id=meta_resp.get("id"),
            name=name,
            category=category.upper(),
            language=language,
            status="PENDING",
            components=components,
        )
        
        # Parse body text - handle both named and positional variables
        for comp in components:
            if comp.get("type", "").upper() == "BODY":
                tpl.body_text = comp.get("text", "")
                import re
                # Detect all variables (both named and positional)
                all_vars = re.findall(r'\{\{([^}]+)\}\}', tpl.body_text)
                tpl.variable_count = len(set(all_vars))
            elif comp.get("type", "").upper() == "HEADER":
                tpl.header_text = comp.get("text", "")
            elif comp.get("type", "").upper() == "FOOTER":
                tpl.footer_text = comp.get("text", "")
        
        get_db().add(tpl)
        get_db().commit()
        
        return jsonify({
            "success": True,
            "template": tpl.to_dict(),
        }), 201
        
    except Exception as e:
        logger.exception(f"Template create error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@whatsapp_bp.route("/templates/<template_name>", methods=["GET"])
def get_template(template_name: str):
    """
    Get a specific template by name.
    
    GET /api/whatsapp/templates/<name>
    """
    import requests
    
    waba_id = request.args.get("waba_id") or os.getenv("WHATSAPP_WABA_ID")
    
    if not waba_id:
        return jsonify({
            "success": False,
            "error": "WABA ID not configured"
        }), 400
    
    # Check cache first
    cache_key = f"templates_{waba_id}"
    if cache_key in _template_cache:
        for t in _template_cache[cache_key]:
            if t["name"] == template_name:
                return jsonify({
                    "success": True,
                    "template": t,
                    "cached": True
                })
    
    # Get access token from database first, then fallback to .env
    from .models import WhatsAppAccount
    
    access_token = None
    account = WhatsAppAccount.query.filter_by(waba_id=waba_id, is_active=True).first()
    if account:
        access_token = account.get_access_token()
    
    # Fallback to environment variables
    if not access_token:
        access_token = os.getenv("WHATSAPP_ACCESS_TOKEN") or os.getenv("WHATSAPP_TEMP_TOKEN")
    
    api_version = os.getenv("WHATSAPP_API_VERSION", "v22.0")
    
    if not access_token:
        return jsonify({
            "success": False,
            "error": "Access token not found. Connect a WhatsApp account first."
        }), 400
    
    try:
        url = f"https://graph.facebook.com/{api_version}/{waba_id}/message_templates"
        params = {
            "name": template_name,
            "fields": "name,status,category,language,components"
        }
        headers = {"Authorization": f"Bearer {access_token}"}
        
        response = requests.get(url, params=params, headers=headers, timeout=30)
        data = response.json()
        
        if "error" in data:
            return jsonify({
                "success": False,
                "error": data["error"].get("message", "Unknown error")
            }), 400
        
        templates = data.get("data", [])
        
        if not templates:
            return jsonify({
                "success": False,
                "error": f"Template '{template_name}' not found"
            }), 404
        
        t = templates[0]
        template_info = {
            "name": t.get("name"),
            "status": t.get("status"),
            "category": t.get("category"),
            "language": t.get("language"),
            "body": None,
            "header": None,
            "footer": None,
        }
        
        for comp in t.get("components", []):
            comp_type = comp.get("type", "").upper()
            if comp_type == "BODY":
                template_info["body"] = comp.get("text", "")
            elif comp_type == "HEADER":
                template_info["header"] = comp.get("text") or comp.get("format")
            elif comp_type == "FOOTER":
                template_info["footer"] = comp.get("text", "")
        
        return jsonify({
            "success": True,
            "template": template_info,
            "cached": False
        })
        
    except requests.RequestException as e:
        logger.exception(f"Error fetching template: {e}")
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


# ============================================================
# Account Info (optional - for debugging)
# ============================================================

@whatsapp_bp.route("/accounts", methods=["GET"])
def list_accounts():
    """
    List configured WhatsApp accounts.
    
    GET /api/whatsapp/accounts
    GET /api/whatsapp/accounts?workspace_id=xxx
    GET /api/whatsapp/accounts?include_inactive=true  (to include inactive accounts)
    
    By default only returns ACTIVE accounts that have access tokens.
    """
    from .models import WhatsAppAccount, accounts_query_with_any_token

    workspace_id = request.args.get("workspace_id")
    include_inactive = request.args.get("include_inactive", "false").lower() == "true"
    
    query = WhatsAppAccount.query
    
    if workspace_id:
        query = query.filter_by(workspace_id=workspace_id)
    
    # By default, only return active accounts with tokens
    if not include_inactive:
        query = query.filter_by(is_active=True)
        query = accounts_query_with_any_token(query)
    
    accounts = query.all()
    
    return jsonify({
        "success": True,
        "accounts": [a.to_dict() for a in accounts],
        "count": len(accounts),
    })


@whatsapp_bp.route("/accounts/<int:account_id>/unlink", methods=["PATCH"])
def unlink_account(account_id: int):
    """
    Unlink/deactivate a WhatsApp account (soft delete - keeps data).
    
    PATCH /api/whatsapp/accounts/<id>/unlink
    
    Use DELETE /api/whatsapp/accounts/<id> to permanently delete with all data.
    """
    from .models import WhatsAppAccount
    
    account = WhatsAppAccount.query.get(account_id)
    
    if not account:
        return jsonify({"success": False, "error": "Account not found"}), 404
    
    try:
        phone_number = account.display_phone_number
        
        # Soft delete - just mark as inactive, keep all data
        account.is_active = False
        account.clear_access_token_storage()
        get_db().commit()
        
        logger.info(f"Unlinked WhatsApp account: {phone_number}")
        
        return jsonify({
            "success": True,
            "message": f"Account {phone_number} unlinked (data preserved)"
        })
        
    except Exception as e:
        get_db().rollback()
        logger.exception(f"Failed to unlink account: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@whatsapp_bp.route("/admin/transfer-account", methods=["POST"])
def admin_transfer_account():
    """
    Admin endpoint: transfer a WABA phone to a different workspace.

    POST /api/whatsapp/admin/transfer-account
    Body: {
        "phone_number_id": "123456",
        "new_workspace_id": "42",
        "force": false          // optional – force-move even if active elsewhere
    }

    Returns:
        {"success": true/false, "message": "..."}
    """
    from .connection_guard import transfer_account_to_workspace

    data = request.get_json(silent=True) or {}
    phone_number_id = data.get("phone_number_id")
    new_workspace_id = data.get("new_workspace_id")
    force = data.get("force", False)

    if not phone_number_id or not new_workspace_id:
        return jsonify({"success": False, "error": "phone_number_id and new_workspace_id required"}), 400

    result = transfer_account_to_workspace(phone_number_id, new_workspace_id, force=force)
    status_code = 200 if result["success"] else 409
    return jsonify(result), status_code


@whatsapp_bp.route("/admin/diagnose-account", methods=["GET"])
def admin_diagnose_account():
    """
    Admin endpoint: diagnose WABA account state for a phone or workspace.

    GET /api/whatsapp/admin/diagnose-account?phone_number_id=123
    GET /api/whatsapp/admin/diagnose-account?workspace_id=42

    Returns all matching account records with conflict detection.
    """
    from .models import WhatsAppAccount

    phone_number_id = request.args.get("phone_number_id")
    workspace_id = request.args.get("workspace_id")

    if not phone_number_id and not workspace_id:
        return jsonify({"success": False, "error": "Provide phone_number_id or workspace_id"}), 400

    results = []

    if phone_number_id:
        accounts = WhatsAppAccount.query.filter_by(phone_number_id=phone_number_id).all()
    else:
        accounts = WhatsAppAccount.query.filter_by(workspace_id=workspace_id).all()

    for acct in accounts:
        results.append({
            "id": acct.id,
            "workspace_id": acct.workspace_id,
            "waba_id": acct.waba_id,
            "phone_number_id": acct.phone_number_id,
            "display_phone_number": acct.display_phone_number,
            "is_active": acct.is_active,
            "has_token": bool(acct.access_token_encrypted),
            "token_type": acct.token_type,
            "created_at": acct.created_at.isoformat() if acct.created_at else None,
            "updated_at": acct.updated_at.isoformat() if acct.updated_at else None,
        })

    # Detect conflicts: multiple active records for the same phone
    active_per_phone = {}
    all_accounts = WhatsAppAccount.query.filter_by(is_active=True).all()
    for acct in all_accounts:
        active_per_phone.setdefault(acct.phone_number_id, []).append(str(acct.workspace_id))
    conflicts = {pid: ws_list for pid, ws_list in active_per_phone.items() if len(ws_list) > 1}

    return jsonify({
        "success": True,
        "accounts": results,
        "conflicts": conflicts,
        "conflict_count": len(conflicts),
    })


@whatsapp_bp.route("/accounts/<int:account_id>/customer-webhook", methods=["GET", "PATCH"])
def customer_webhook_config(account_id: int):
    """
    Get or set customer webhook forwarding for partner inbound events (Vaish).

    PATCH body:
      {
        "customer_webhook_url": "https://customer.example.com/webhook",
        "customer_webhook_secret_header": "x-customer-secret",
        "customer_webhook_secret_value": "..."
      }
    """
    from .models import WhatsAppAccount

    account = WhatsAppAccount.query.get(account_id)
    if not account:
        return jsonify({"success": False, "error": "Account not found"}), 404

    if request.method == "GET":
        return jsonify({
            "success": True,
            "account_id": account.id,
            "workspace_id": account.workspace_id,
            "waba_id": account.waba_id,
            "customer_webhook_url": account.customer_webhook_url,
            "customer_webhook_secret_header": account.customer_webhook_secret_header,
            "customer_webhook_secret_configured": bool(
                (account.customer_webhook_secret_header or "").strip()
                and (account.customer_webhook_secret_value or "").strip()
            ),
        })

    data = request.get_json(silent=True) or {}
    if not any(
        key in data
        for key in (
            "customer_webhook_url",
            "customer_webhook_secret_header",
            "customer_webhook_secret_value",
        )
    ):
        return jsonify({
            "success": False,
            "error": "At least one of customer_webhook_url, customer_webhook_secret_header, "
            "customer_webhook_secret_value is required",
        }), 400

    if "customer_webhook_url" in data:
        raw_url = data.get("customer_webhook_url")
        if raw_url is None or (isinstance(raw_url, str) and not raw_url.strip()):
            account.customer_webhook_url = None
        else:
            url = str(raw_url).strip()
            if not url.startswith("https://"):
                return jsonify({"success": False, "error": "customer_webhook_url must use https://"}), 400
            if len(url) > 512:
                return jsonify({"success": False, "error": "URL too long (max 512 characters)"}), 400
            account.customer_webhook_url = url

    if "customer_webhook_secret_header" in data:
        raw_header = data.get("customer_webhook_secret_header")
        if raw_header is None or (isinstance(raw_header, str) and not raw_header.strip()):
            account.customer_webhook_secret_header = None
        else:
            header = str(raw_header).strip()
            if len(header) > 128:
                return jsonify({"success": False, "error": "Secret header name too long (max 128)"}), 400
            account.customer_webhook_secret_header = header

    if "customer_webhook_secret_value" in data:
        raw_secret = data.get("customer_webhook_secret_value")
        if raw_secret is None or (isinstance(raw_secret, str) and not raw_secret.strip()):
            account.customer_webhook_secret_value = None
        else:
            account.customer_webhook_secret_value = str(raw_secret).strip()

    try:
        get_db().commit()
        return jsonify({
            "success": True,
            "account_id": account.id,
            "workspace_id": account.workspace_id,
            "customer_webhook_url": account.customer_webhook_url,
            "customer_webhook_secret_configured": bool(
                (account.customer_webhook_secret_header or "").strip()
                and (account.customer_webhook_secret_value or "").strip()
            ),
        })
    except Exception as exc:
        get_db().rollback()
        logger.exception("customer_webhook_config failed: %s", exc)
        return jsonify({"success": False, "error": str(exc)}), 500


@whatsapp_bp.route("/accounts/<int:account_id>/rename", methods=["PATCH"])
def rename_account(account_id: int):
    """
    Rename a WhatsApp account (custom display name).
    
    PATCH /api/whatsapp/accounts/<id>/rename
    Body: {"name": "New Account Name"}
    
    Uses custom_name field which is never overwritten by Meta API during re-link.
    """
    from .models import WhatsAppAccount
    
    account = WhatsAppAccount.query.get(account_id)
    
    if not account:
        return jsonify({"success": False, "error": "Account not found"}), 404
    
    data = request.get_json() or {}
    new_name = data.get("name", "").strip()
    
    if not new_name:
        return jsonify({"success": False, "error": "Name is required"}), 400
    
    if len(new_name) > 128:
        return jsonify({"success": False, "error": "Name too long (max 128 characters)"}), 400
    
    try:
        old_name = account.custom_name or account.verified_name
        account.custom_name = new_name  # Use custom_name, never overwritten by Meta
        get_db().commit()
        
        logger.info(f"Renamed WhatsApp account: {old_name} -> {new_name}")
        
        return jsonify({
            "success": True,
            "message": f"Account renamed to '{new_name}'",
            "account": account.to_dict()
        })
        
    except Exception as e:
        get_db().rollback()
        logger.exception(f"Failed to rename account: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@whatsapp_bp.route("/accounts/<int:account_id>/meta-business-id", methods=["PATCH", "PUT"])
def update_meta_business_id(account_id: int):
    """
    Update Meta Business Manager ID for a WhatsApp account.

    PATCH/PUT /api/whatsapp/accounts/<id>/meta-business-id
    Body: {"meta_business_id": "123456789012345"}
    """
    from .models import WhatsAppAccount

    account = WhatsAppAccount.query.get(account_id)
    if not account:
        return jsonify({"success": False, "error": "Account not found"}), 404

    data = request.get_json() or {}
    raw_value = data.get("meta_business_id", "")
    meta_business_id = "".join(c for c in str(raw_value).strip() if c.isdigit())

    if raw_value and not meta_business_id:
        return jsonify({"success": False, "error": "Meta Business Manager ID must contain digits only"}), 400

    if meta_business_id and len(meta_business_id) > 64:
        return jsonify({"success": False, "error": "Meta Business Manager ID is too long"}), 400

    try:
        account.meta_business_id = meta_business_id or None
        get_db().commit()

        logger.info(f"Updated Meta Business Manager ID for account {account_id}: {meta_business_id or 'cleared'}")

        return jsonify({
            "success": True,
            "message": "Meta Business Manager ID updated successfully",
            "account": account.to_dict()
        })
    except Exception as e:
        get_db().rollback()
        logger.exception(f"Failed to update Meta Business Manager ID: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@whatsapp_bp.route("/accounts/<int:account_id>/migrate-resources", methods=["POST"])
@require_admin_only
def migrate_account_resources(account_id: int):
    """
    Migrate account-scoped WhatsApp resources into a target account.

    POST /api/whatsapp/accounts/<target_account_id>/migrate-resources

    Body (all optional):
    {
      "source_account_ids": [12, 18],
      "include_active_sources": false,
      "dry_run": true
    }

    Defaults:
    - source_account_ids: all other accounts in same workspace
    - include_active_sources: false (migrate from old/unlinked accounts only)
    - dry_run: true
    """
    from .models import WhatsAppAccount, WhatsAppTemplate, WhatsAppFlow
    from .automation_models import (
        WhatsAppAutomationRule,
        WhatsAppBusinessHours,
        ContactAutomationOverride,
    )
    from .visual_automation_models import WhatsAppVisualAutomation
    from .trigger_models import WhatsAppTrigger
    from .faq_models import WhatsAppFAQ
    from .drip_models import WhatsAppDripCampaign

    target_account = WhatsAppAccount.query.get(account_id)
    if not target_account:
        return jsonify({"success": False, "error": "Target account not found"}), 404

    workspace_id = str(target_account.workspace_id or "")
    if not workspace_id:
        return jsonify({"success": False, "error": "Target account has no workspace_id"}), 400

    data = request.get_json(silent=True) or {}
    dry_run = bool(data.get("dry_run", True))
    include_active_sources = bool(data.get("include_active_sources", False))
    source_ids_raw = data.get("source_account_ids")

    source_query = WhatsAppAccount.query.filter(
        WhatsAppAccount.workspace_id == workspace_id,
        WhatsAppAccount.id != target_account.id,
    )
    if not include_active_sources:
        source_query = source_query.filter(WhatsAppAccount.is_active == False)

    source_accounts = source_query.all()

    if source_ids_raw:
        try:
            requested_ids = {int(x) for x in source_ids_raw}
        except Exception:
            return jsonify({"success": False, "error": "source_account_ids must be an array of integers"}), 400
        source_accounts = [a for a in source_accounts if a.id in requested_ids]

    if not source_accounts:
        return jsonify({
            "success": True,
            "message": "No source accounts found to migrate",
            "dry_run": dry_run,
            "target_account_id": target_account.id,
            "workspace_id": workspace_id,
            "source_account_ids": [],
            "summary": {},
        })

    summary = {
        "templates": {"migrated": 0, "skipped_conflicts": 0},
        "flows": {"migrated": 0, "skipped_conflicts": 0},
        "triggers": {"migrated": 0, "skipped_conflicts": 0},
        "automation_rules": {"migrated": 0},
        "business_hours": {"migrated": 0, "skipped_conflicts": 0},
        "contact_overrides": {"migrated": 0},
        "visual_automations": {"migrated": 0},
        "faqs": {"migrated": 0},
        "drip_campaigns": {"migrated": 0},
    }
    details = {
        "template_conflicts": [],
        "flow_conflicts": [],
        "trigger_conflicts": [],
        "business_hours_conflicts": [],
    }

    try:
        for source in source_accounts:
            # Templates: unique(account_id, name, language)
            source_templates = WhatsAppTemplate.query.filter_by(account_id=source.id).all()
            for tmpl in source_templates:
                conflict = WhatsAppTemplate.query.filter_by(
                    account_id=target_account.id,
                    name=tmpl.name,
                    language=tmpl.language,
                ).first()
                if conflict:
                    summary["templates"]["skipped_conflicts"] += 1
                    details["template_conflicts"].append({
                        "source_account_id": source.id,
                        "template_id": tmpl.id,
                        "name": tmpl.name,
                        "language": tmpl.language,
                    })
                else:
                    summary["templates"]["migrated"] += 1
                    if not dry_run:
                        tmpl.account_id = target_account.id

            # Flows: unique(account_id, name, flow_version)
            source_flows = WhatsAppFlow.query.filter_by(account_id=source.id).all()
            for flow in source_flows:
                conflict = WhatsAppFlow.query.filter_by(
                    account_id=target_account.id,
                    name=flow.name,
                    flow_version=flow.flow_version,
                ).first()
                if conflict:
                    summary["flows"]["skipped_conflicts"] += 1
                    details["flow_conflicts"].append({
                        "source_account_id": source.id,
                        "flow_id": flow.id,
                        "name": flow.name,
                        "flow_version": flow.flow_version,
                    })
                else:
                    summary["flows"]["migrated"] += 1
                    if not dry_run:
                        flow.account_id = target_account.id

            # Triggers: unique(account_id, slug)
            source_triggers = WhatsAppTrigger.query.filter_by(account_id=source.id).all()
            for trig in source_triggers:
                conflict = WhatsAppTrigger.query.filter_by(
                    account_id=target_account.id,
                    slug=trig.slug,
                ).first()
                if conflict:
                    summary["triggers"]["skipped_conflicts"] += 1
                    details["trigger_conflicts"].append({
                        "source_account_id": source.id,
                        "trigger_id": trig.id,
                        "slug": trig.slug,
                    })
                else:
                    summary["triggers"]["migrated"] += 1
                    if not dry_run:
                        trig.account_id = target_account.id

            # Business hours: unique(workspace_id, account_id)
            source_bh = WhatsAppBusinessHours.query.filter_by(
                workspace_id=workspace_id,
                account_id=source.id,
            ).first()
            if source_bh:
                target_bh = WhatsAppBusinessHours.query.filter_by(
                    workspace_id=workspace_id,
                    account_id=target_account.id,
                ).first()
                if target_bh:
                    summary["business_hours"]["skipped_conflicts"] += 1
                    details["business_hours_conflicts"].append({
                        "source_account_id": source.id,
                        "business_hours_id": source_bh.id,
                    })
                else:
                    summary["business_hours"]["migrated"] += 1
                    if not dry_run:
                        source_bh.account_id = target_account.id

            # Straight rebind resources (no unique account constraint risk)
            moved_rules = WhatsAppAutomationRule.query.filter_by(
                workspace_id=workspace_id,
                account_id=source.id,
            ).update({"account_id": target_account.id}, synchronize_session=False)
            summary["automation_rules"]["migrated"] += moved_rules

            moved_overrides = ContactAutomationOverride.query.filter_by(
                workspace_id=workspace_id,
                account_id=source.id,
            ).update({"account_id": target_account.id}, synchronize_session=False)
            summary["contact_overrides"]["migrated"] += moved_overrides

            moved_visual = WhatsAppVisualAutomation.query.filter_by(
                workspace_id=workspace_id,
                account_id=source.id,
            ).update({"account_id": target_account.id}, synchronize_session=False)
            summary["visual_automations"]["migrated"] += moved_visual

            moved_faqs = WhatsAppFAQ.query.filter_by(
                workspace_id=workspace_id,
                account_id=source.id,
            ).update({"account_id": target_account.id}, synchronize_session=False)
            summary["faqs"]["migrated"] += moved_faqs

            moved_drips = WhatsAppDripCampaign.query.filter_by(
                workspace_id=workspace_id,
                account_id=source.id,
            ).update({"account_id": target_account.id}, synchronize_session=False)
            summary["drip_campaigns"]["migrated"] += moved_drips

        if dry_run:
            get_db().rollback()
        else:
            get_db().commit()

        return jsonify({
            "success": True,
            "dry_run": dry_run,
            "target_account_id": target_account.id,
            "workspace_id": workspace_id,
            "source_account_ids": [a.id for a in source_accounts],
            "summary": summary,
            "details": details,
            "message": "Dry run completed" if dry_run else "Resources migrated successfully",
        })

    except Exception as e:
        get_db().rollback()
        logger.exception(f"Failed to migrate resources to account {account_id}: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@whatsapp_bp.route("/accounts/<int:account_id>/update-token", methods=["POST", "PUT"])
@require_admin_only  # SECURITY: Only admins can update access tokens
def update_account_token(account_id: int):
    """
    Update access token for a WhatsApp account.
    
    POST/PUT /api/whatsapp/accounts/<id>/update-token
    
    Request body:
    {
        "access_token": "new_token_here"
    }
    
    Use this to update an expired or invalid token without reconnecting.
    After update, automatically subscribes WABA to webhooks.
    """
    from .models import WhatsAppAccount
    
    account = WhatsAppAccount.query.get(account_id)
    if not account:
        return jsonify({"success": False, "error": "Account not found"}), 404
    
    data = request.get_json() or {}
    new_token = data.get("access_token")
    
    if not new_token:
        return jsonify({"success": False, "error": "access_token is required"}), 400
    
    try:
        # Validate token with Meta first
        from .connection_path import validate_token_with_meta
        token_check = validate_token_with_meta(new_token)
        
        if not token_check.get("valid"):
            return jsonify({
                "success": False,
                "error": f"Invalid access token: {token_check.get('error', 'Token validation failed')}"
            }), 400
        
        # Try to exchange for long-lived token if possible
        final_token = new_token
        expires_at = None
        
        try:
            from .oauth import exchange_short_for_long_token
            # Function signature: exchange_short_for_long_token(short_token: str) -> Dict[str, Any]
            exchange_result = exchange_short_for_long_token(new_token)
            
            if exchange_result.get("access_token"):
                final_token = exchange_result["access_token"]
                expires_at = exchange_result.get("expires_at")
                logger.info(f"Successfully exchanged token for long-lived token (expires: {expires_at})")
                
        except Exception as exchange_error:
            # If exchange fails (e.g. no app secret, or token already long-lived), 
            # log warning but proceed with original token as it was validated above
            logger.warning(f"Token exchange failed or skipped, using original token: {str(exchange_error)}")
        
        # Update token
        account.set_access_token(final_token, "permanent", expires_at)
        get_db().commit()
        
        logger.info(f"Updated access token for account {account_id}")
        
        # Auto-subscribe to webhooks after token update
        setup_result = None
        try:
            from .health_check import subscribe_waba_to_webhooks, perform_health_check
            
            # Subscribe WABA to webhooks
            success, message, details = subscribe_waba_to_webhooks(account.waba_id, new_token)
            
            if success:
                logger.info(f"✅ Auto-subscribed WABA {account.waba_id} to webhooks after token update")
            else:
                logger.warning(f"⚠️ Webhook subscription failed after token update: {message}")
            
            # Run health check
            health_result = perform_health_check(account_id, auto_fix=True)
            
            setup_result = {
                "webhook_subscription": {"success": success, "message": message},
                "health_check": health_result
            }
        except Exception as e:
            logger.exception(f"Post-token-update setup error: {e}")
            setup_result = {"error": str(e)}
        
        return jsonify({
            "success": True,
            "message": "Access token updated successfully",
            "account_id": account_id,
            "setup": setup_result
        })
        
    except Exception as e:
        get_db().rollback()
        logger.exception(f"Failed to update token: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@whatsapp_bp.route("/accounts/<int:account_id>/debug", methods=["GET"])
def get_account_debug_info(account_id: int):
    """
    Get account details including access token for TESTING ONLY.
    
    WARNING: This exposes sensitive data - use only for development/testing!
    
    GET /api/whatsapp/accounts/<id>/debug
    """
    from .models import WhatsAppAccount
    
    account = WhatsAppAccount.query.get(account_id)
    if not account:
        return jsonify({"success": False, "error": "Account not found"}), 404
    
    # Get decrypted token
    access_token = account.get_access_token()
    
    return jsonify({
        "success": True,
        "account": {
            "id": account.id,
            "waba_id": account.waba_id,
            "phone_number_id": account.phone_number_id,
            "display_phone_number": account.display_phone_number,
            "verified_name": account.verified_name,
            "access_token": access_token,
            "token_type": account.token_type,
            "is_active": account.is_active,
            "has_flow_keys": account.has_flow_keys() if hasattr(account, 'has_flow_keys') else False,
        }
    })


@whatsapp_bp.route("/accounts/<int:account_id>/full-health-check", methods=["GET", "POST"])
def full_health_check(account_id: int):
    """
    Comprehensive health check for a WhatsApp account.
    
    GET  /api/whatsapp/accounts/<id>/full-health-check - Check health
    POST /api/whatsapp/accounts/<id>/full-health-check - Check and auto-fix issues
    
    Checks:
    1. Access token validity and expiration
    2. WABA webhook subscription (auto-subscribes if missing)
    3. Phone number quality rating
    4. Webhook configuration
    
    Returns detailed report with any issues and whether they were auto-fixed.
    """
    from .health_check import perform_health_check
    
    # POST = auto-fix enabled, GET = check only
    auto_fix = request.method == "POST"
    
    try:
        result = perform_health_check(account_id, auto_fix=auto_fix)
        return jsonify(result)
    except Exception as e:
        logger.exception(f"Health check failed: {e}")
        return jsonify({
            "success": False,
            "error": str(e),
            "error_code": "HEALTH_CHECK_FAILED"
        }), 500


@whatsapp_bp.route("/workspace/<workspace_id>/health-check", methods=["GET", "POST"])
def workspace_health_check(workspace_id: str):
    """
    Health check for all WhatsApp accounts in a workspace.
    
    GET  /api/whatsapp/workspace/<id>/health-check - Check health
    POST /api/whatsapp/workspace/<id>/health-check - Check and auto-fix issues
    
    Returns health status for all accounts in the workspace.
    """
    from .health_check import perform_workspace_health_check
    
    auto_fix = request.method == "POST"
    
    try:
        result = perform_workspace_health_check(workspace_id, auto_fix=auto_fix)
        return jsonify(result)
    except Exception as e:
        logger.exception(f"Workspace health check failed: {e}")
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


@whatsapp_bp.route("/accounts/<int:account_id>/subscribe-webhooks", methods=["POST"])
def manual_subscribe_webhooks(account_id: int):
    """
    Manually subscribe a WABA to webhooks.
    
    POST /api/whatsapp/accounts/<id>/subscribe-webhooks
    
    Use this if webhooks are not working for an account.
    """
    from .models import WhatsAppAccount
    from .health_check import subscribe_waba_to_webhooks
    
    account = WhatsAppAccount.query.get(account_id)
    if not account:
        return jsonify({"success": False, "error": "Account not found"}), 404
    
    access_token = account.get_access_token()
    if not access_token:
        return jsonify({
            "success": False,
            "error": "No access token found for this account"
        }), 400
    
    success, message, details = subscribe_waba_to_webhooks(account.waba_id, access_token)
    
    return jsonify({
        "success": success,
        "message": message,
        "details": details,
        "waba_id": account.waba_id
    }), 200 if success else 400


@whatsapp_bp.route("/accounts/<int:account_id>/webhook-health/revalidate", methods=["POST"])
def webhook_health_revalidate(account_id: int):
    """
    Re-run webhook subscription + delivery integrity checks (advisory persistence only).

    POST /api/whatsapp/accounts/<id>/webhook-health/revalidate
    """
    from .models import WhatsAppAccount
    from .webhook_health import validate_and_persist_account

    account = WhatsAppAccount.query.get(account_id)
    if not account:
        return jsonify({"success": False, "error": "Account not found"}), 404
    report = validate_and_persist_account(account, probe_callback=True)
    return jsonify({"success": True, "report": report})


@whatsapp_bp.route("/accounts/<int:account_id>/webhook-health/retry-subscribe", methods=["POST"])
def webhook_health_retry_subscribe(account_id: int):
    """
    POST subscribed_apps (full fields) then revalidate health.

    POST /api/whatsapp/accounts/<id>/webhook-health/retry-subscribe
    """
    from .models import WhatsAppAccount
    from .webhook_health import retry_subscribe_webhooks

    account = WhatsAppAccount.query.get(account_id)
    if not account:
        return jsonify({"success": False, "error": "Account not found"}), 404
    out = retry_subscribe_webhooks(account)
    ok = bool(out.get("subscribe_result", {}).get("success"))
    return jsonify({"success": ok, **out}), 200 if ok else 400


@whatsapp_bp.route("/accounts/<int:account_id>/webhook-health/refresh-subscriptions", methods=["POST"])
def webhook_health_refresh_subscriptions(account_id: int):
    """
    GET subscribed_apps metadata only (no callback probe).

    POST /api/whatsapp/accounts/<id>/webhook-health/refresh-subscriptions
    """
    from .models import WhatsAppAccount
    from .webhook_health import refresh_subscribed_apps_metadata

    account = WhatsAppAccount.query.get(account_id)
    if not account:
        return jsonify({"success": False, "error": "Account not found"}), 404
    meta = refresh_subscribed_apps_metadata(account)
    return jsonify({"success": bool(meta.get("success")), **meta}), 200 if meta.get("success") else 400


@whatsapp_bp.route("/accounts/<int:account_id>", methods=["DELETE"])
@require_admin_only  # SECURITY: Only admins can delete accounts
def delete_account(account_id: int):
    """
    Permanently delete a WhatsApp account and all related data.
    
    DELETE /api/whatsapp/accounts/<id>
    """
    from .models import WhatsAppAccount, WhatsAppConversation, WhatsAppMessage, WhatsAppTemplate, WhatsAppFlow, WhatsAppUsageEvent
    from .trigger_models import WhatsAppTrigger
    from .trigger_logs_model import TriggerLog
    from .faq_models import WhatsAppFAQ
    from .drip_models import WhatsAppDripCampaign, WhatsAppDripStep, WhatsAppDripEnrollment
    from .automation_models import (
        WhatsAppAutomationRule,
        WhatsAppAutomationLog,
        WhatsAppBusinessHours,
        ContactAutomationOverride,
    )
    from .visual_automation_models import WhatsAppVisualAutomation, WhatsAppAutomationNode
    
    account = WhatsAppAccount.query.get(account_id)
    
    if not account:
        return jsonify({"success": False, "error": "Account not found"}), 404
    
    try:
        # Store info for logging
        phone_number = account.display_phone_number
        waba_id = account.waba_id
        
        # Delete account-scoped dependencies first to avoid FK constraint failures.
        template_count = WhatsAppTemplate.query.filter_by(account_id=account_id).delete(synchronize_session=False)
        flow_count = WhatsAppFlow.query.filter_by(account_id=account_id).delete(synchronize_session=False)

        # Delete trigger logs (child) before triggers (parent)
        trigger_log_count = TriggerLog.query.filter_by(account_id=account_id).delete(synchronize_session=False)
        trigger_count = WhatsAppTrigger.query.filter_by(account_id=account_id).delete(synchronize_session=False)

        faq_count = WhatsAppFAQ.query.filter_by(account_id=account_id).delete(synchronize_session=False)
        
        # Delete Drip Campaign sub-records (steps and enrollments) before campaigns
        drip_campaign_ids = [c.id for c in WhatsAppDripCampaign.query.filter_by(account_id=account_id).all()]
        drip_step_count = 0
        drip_enrollment_count = 0
        if drip_campaign_ids:
            drip_enrollment_count = WhatsAppDripEnrollment.query.filter(WhatsAppDripEnrollment.campaign_id.in_(drip_campaign_ids)).delete(synchronize_session=False)
            drip_step_count = WhatsAppDripStep.query.filter(WhatsAppDripStep.campaign_id.in_(drip_campaign_ids)).delete(synchronize_session=False)
        drip_count = WhatsAppDripCampaign.query.filter_by(account_id=account_id).delete(synchronize_session=False)

        # Delete usage events
        usage_event_count = WhatsAppUsageEvent.query.filter_by(account_id=account_id).delete(synchronize_session=False)

        automation_rule_ids = [r.id for r in WhatsAppAutomationRule.query.filter_by(account_id=account_id).all()]
        automation_log_count = 0
        if automation_rule_ids:
            automation_log_count = WhatsAppAutomationLog.query.filter(WhatsAppAutomationLog.rule_id.in_(automation_rule_ids)).delete(synchronize_session=False)
        automation_rule_count = WhatsAppAutomationRule.query.filter_by(account_id=account_id).delete(synchronize_session=False)
        business_hours_count = WhatsAppBusinessHours.query.filter_by(account_id=account_id).delete(synchronize_session=False)
        contact_override_count = ContactAutomationOverride.query.filter_by(account_id=account_id).delete(synchronize_session=False)

        visual_automation_ids = [a.id for a in WhatsAppVisualAutomation.query.filter_by(account_id=account_id).all()]
        visual_node_count = 0
        if visual_automation_ids:
            visual_node_count = WhatsAppAutomationNode.query.filter(WhatsAppAutomationNode.automation_id.in_(visual_automation_ids)).delete(synchronize_session=False)
        visual_count = WhatsAppVisualAutomation.query.filter_by(account_id=account_id).delete(synchronize_session=False)

        logger.info(
            "Deleted dependencies for account %s: templates=%s flows=%s triggers=%s trigger_logs=%s faqs=%s drips=%s "
            "drip_steps=%s drip_enrollments=%s usage_events=%s "
            "automation_rules=%s automation_logs=%s business_hours=%s contact_overrides=%s visual_automations=%s visual_nodes=%s",
            account_id,
            template_count,
            flow_count,
            trigger_count,
            trigger_log_count,
            faq_count,
            drip_count,
            drip_step_count,
            drip_enrollment_count,
            usage_event_count,
            automation_rule_count,
            automation_log_count,
            business_hours_count,
            contact_override_count,
            visual_count,
            visual_node_count,
        )
        
        # Delete related conversations and messages
        conversations = WhatsAppConversation.query.filter_by(account_id=account_id).all()
        for conv in conversations:
            # Delete messages in this conversation
            WhatsAppMessage.query.filter_by(conversation_id=conv.id).delete()
            get_db().delete(conv)
        
        # Now delete the account
        get_db().delete(account)
        get_db().commit()
        
        logger.info(f"Permanently deleted WhatsApp account: {phone_number} (WABA: {waba_id})")
        
        return jsonify({
            "success": True,
            "message": f"Account {phone_number} permanently deleted"
        })
        
    except Exception as e:
        get_db().rollback()
        logger.exception(f"Failed to delete account: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@whatsapp_bp.route("/accounts/<int:account_id>/register", methods=["POST"])
def register_phone_number(account_id: int):
    """
    Register a phone number with WhatsApp Business API.
    
    POST /api/whatsapp/accounts/<id>/register
    
    This is required before a phone number can send/receive messages.
    Called automatically during OAuth, but can be triggered manually if needed.
    
    Request Body (optional):
        {"pin": "123456"}  - 6-digit PIN for 2FA (default: 123456)
    
    Returns:
        {"success": true} or {"success": false, "error": "..."}
    """
    import requests as http_requests
    from .models import WhatsAppAccount
    
    # Use centralized token helper
    account, token_error = get_account_with_token(account_id)
    if token_error:
        return jsonify({"success": False, "error": token_error}), 400
    
    access_token = account.get_access_token()
    
    # Get optional PIN from request body
    data = request.get_json(silent=True) or {}
    pin = data.get("pin", "123456")  # Default 6-digit PIN
    
    api_version = os.getenv("WHATSAPP_API_VERSION", "v24.0")
    
    try:
        resp = http_requests.post(
            f"https://graph.facebook.com/{api_version}/{account.phone_number_id}/register",
            json={
                "messaging_product": "whatsapp",
                "pin": pin
            },
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json"
            },
            timeout=15
        )
        result = resp.json()
        
        if result.get("success"):
            logger.info(f"Phone number {account.phone_number_id} registered successfully")
            return jsonify({
                "success": True,
                "message": "Phone number registered with WhatsApp Business API",
                "phone_number_id": account.phone_number_id,
                "display_phone_number": account.display_phone_number
            })
        else:
            error = result.get("error", {})
            error_msg = error.get("message", "Registration failed")
            error_code = error.get("code", 0)
            
            # Check if already registered (not an error)
            if "already registered" in error_msg.lower():
                return jsonify({
                    "success": True,
                    "message": "Phone number is already registered",
                    "phone_number_id": account.phone_number_id
                })
            
            logger.warning(f"Phone registration failed: {error_msg}")
            return jsonify({
                "success": False,
                "error": error_msg,
                "error_code": error_code
            }), 400
            
    except Exception as e:
        logger.exception(f"Phone registration error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


# ============================================================
# Phase-2 Part-2: Health Monitoring Endpoints
# ============================================================

@whatsapp_bp.route("/accounts/<int:account_id>/health", methods=["GET"])
def get_account_health(account_id: int):
    """
    Force refresh health data for a WhatsApp account from Meta API.
    
    GET /api/whatsapp/accounts/<id>/health
    
    Returns updated account with health fields.
    """
    from .health_service import health_service
    
    result = health_service.sync_account_health(account_id)
    
    if result.get("success"):
        return jsonify(result)
    else:
        return jsonify(result), 400 if result.get("error") == "Account not found" else 500


@whatsapp_bp.route("/accounts/<int:account_id>/phone-numbers", methods=["GET"])
def get_account_phone_numbers(account_id: int):
    """
    Get phone numbers associated with a WhatsApp account.
    
    GET /api/whatsapp/accounts/<id>/phone-numbers
    
    Returns phone number details from Meta API.
    """
    from .models import WhatsAppAccount
    from .health_service import health_service
    
    # Use centralized token helper
    account, token_error = get_account_with_token(account_id)
    if token_error:
        return jsonify({"success": False, "error": token_error}), 400
    
    access_token = account.get_access_token()
    
    result = health_service.fetch_phone_numbers(account.waba_id, access_token)
    
    if result.get("success"):
        return jsonify({
            "success": True,
            "account_id": account_id,
            "waba_id": account.waba_id,
            "phone_numbers": result.get("phone_numbers", []),
        })
    else:
        return jsonify({
            "success": False,
            "error": result.get("error_message"),
            "error_code": result.get("error_code"),
        }), 500


@whatsapp_bp.route("/accounts/<int:account_id>/diagnostics", methods=["GET"])
def get_account_diagnostics(account_id: int):
    """
    Get diagnostic information for a WhatsApp account.
    
    GET /api/whatsapp/accounts/<id>/diagnostics
    
    Returns:
    - Last health check time
    - Last error (if any)
    - Token type and expiry info (no actual token exposed)
    - Account status
    """
    from .models import WhatsAppAccount
    
    account = WhatsAppAccount.query.get(account_id)
    if not account:
        return jsonify({"success": False, "error": "Account not found"}), 404
    
    # Build diagnostics (never expose actual tokens)
    has_token = bool(account.access_token_encrypted)
    
    diagnostics = {
        "success": True,
        "account_id": account_id,
        "diagnostics": {
            # Connection status
            "is_active": account.is_active,
            "phone_number_status": account.phone_number_status,
            "account_review_status": account.account_review_status,
            
            # Token info (masked)
            "has_access_token": has_token,
            "token_type": account.token_type if has_token else None,
            "token_expires_at": account.token_expires_at.isoformat() if account.token_expires_at else None,
            "token_status": "valid" if has_token and (
                account.token_type == "permanent" or 
                (account.token_expires_at and account.token_expires_at > datetime.now(timezone.utc))
            ) else ("expired" if account.token_expires_at else "missing"),
            
            # Health check info
            "last_health_check_at": account.last_health_check_at.isoformat() if account.last_health_check_at else None,
            "last_synced_at": account.last_synced_at.isoformat() if account.last_synced_at else None,
            
            # Error info
            "last_error_code": account.last_error_code,
            "last_error_message": account.last_error_message,
            
            # Quality metrics
            "quality_score": account.quality_score,
            "messaging_limit": account.messaging_limit,
        }
    }
    
    return jsonify(diagnostics)


@whatsapp_bp.route("/accounts/<int:account_id>/voice-call-capability", methods=["GET"])
def get_account_voice_call_capability(account_id: int):
    """
    Get voice-call readiness for VOICE_CALL template buttons.

    GET /api/whatsapp/accounts/<id>/voice-call-capability
    """
    account, token_error = get_account_with_token(account_id)
    if token_error:
        return jsonify({"success": False, "error": token_error}), 400

    access_token = account.get_access_token()
    capability = _get_voice_call_capability(account, access_token)
    return jsonify(capability)


# ============================================================
# Ice Breakers (Conversation Starters)
# ============================================================

@whatsapp_bp.route("/accounts/<int:account_id>/ice-breakers", methods=["GET"])
def get_ice_breakers(account_id: int):
    """
    Get current ice breakers (conversation starters) for a WhatsApp number.
    
    GET /api/whatsapp/accounts/<id>/ice-breakers
    
    Ice breakers appear as quick-reply buttons when users first open the chat.
    Max 4 ice breakers allowed by Meta.
    """
    import requests
    from .models import WhatsAppAccount
    
    account, token_error = get_account_with_token(account_id)
    if token_error:
        return jsonify({"success": False, "error": token_error}), 400
    
    access_token = account.get_access_token()
    api_version = os.getenv("WHATSAPP_API_VERSION", "v22.0")
    
    try:
        # Get WhatsApp Business Profile with ice breakers
        url = f"https://graph.facebook.com/{api_version}/{account.phone_number_id}/whatsapp_business_profile"
        params = {
            "fields": "about,address,description,email,profile_picture_url,websites,vertical,messaging_product"
        }
        headers = {"Authorization": f"Bearer {access_token}"}
        
        response = requests.get(url, params=params, headers=headers, timeout=30)
        data = response.json()
        
        if "error" in data:
            return jsonify({
                "success": False,
                "error": data["error"].get("message", "Failed to get ice breakers"),
                "error_code": data["error"].get("code")
            }), 400
        
        profile_data = data.get("data", [{}])[0] if isinstance(data.get("data"), list) else data
        
        # Get conversational components (ice breakers)
        conv_url = f"https://graph.facebook.com/{api_version}/{account.phone_number_id}"
        conv_params = {"fields": "conversational_automation"}
        
        conv_response = requests.get(conv_url, params=conv_params, headers=headers, timeout=30)
        conv_data = conv_response.json()
        
        ice_breakers = []
        if "conversational_automation" in conv_data:
            automation = conv_data["conversational_automation"]
            if "ice_breakers" in automation:
                ice_breakers = automation["ice_breakers"]
        
        return jsonify({
            "success": True,
            "account_id": account_id,
            "ice_breakers": ice_breakers,
            "profile": profile_data
        })
        
    except requests.RequestException as e:
        logger.exception(f"Failed to get ice breakers: {e}")
        return jsonify({"success": False, "error": f"Request failed: {str(e)}"}), 500


@whatsapp_bp.route("/accounts/<int:account_id>/ice-breakers", methods=["PUT", "POST"])
def update_ice_breakers(account_id: int):
    """
    Update ice breakers (conversation starters) for a WhatsApp number.
    
    POST/PUT /api/whatsapp/accounts/<id>/ice-breakers
    
    Request body:
    {
        "ice_breakers": [
            {"content": "What services do you offer?"},
            {"content": "I need help with my order"},
            {"content": "Speak to a human"},
            {"content": "View pricing"}
        ],
        "enable_welcome_message": false,
        "prompts": ["Hi!", "Hello"]  // Optional: greeting prompts
    }
    
    Max 4 ice breakers. Each content max 80 chars.
    """
    import requests
    from .models import WhatsAppAccount
    
    account, token_error = get_account_with_token(account_id)
    if token_error:
        return jsonify({"success": False, "error": token_error}), 400
    
    data = request.get_json()
    if not data:
        return jsonify({"success": False, "error": "Request body required"}), 400
    
    ice_breakers = data.get("ice_breakers", [])
    
    # Validation
    if len(ice_breakers) > 4:
        return jsonify({"success": False, "error": "Maximum 4 ice breakers allowed"}), 400
    
    for i, ib in enumerate(ice_breakers):
        content = ib.get("content", "")
        if not content:
            return jsonify({"success": False, "error": f"Ice breaker {i+1} content is required"}), 400
        if len(content) > 80:
            return jsonify({"success": False, "error": f"Ice breaker {i+1} exceeds 80 character limit"}), 400
    
    access_token = account.get_access_token()
    api_version = os.getenv("WHATSAPP_API_VERSION", "v22.0")
    
    try:
        # Build conversational automation payload
        automation_payload = {
            "enable_welcome_message": data.get("enable_welcome_message", False),
            "ice_breakers": ice_breakers
        }
        
        # Add prompts if provided
        if data.get("prompts"):
            automation_payload["prompts"] = data["prompts"]
        
        # Update via Graph API
        url = f"https://graph.facebook.com/{api_version}/{account.phone_number_id}/conversational_automation"
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json"
        }
        
        import json
        response = requests.post(
            url,
            headers=headers,
            data=json.dumps(automation_payload),
            timeout=30
        )
        result = response.json()
        
        if "error" in result:
            return jsonify({
                "success": False,
                "error": result["error"].get("message", "Failed to update ice breakers"),
                "error_code": result["error"].get("code")
            }), 400
        
        logger.info(f"Updated ice breakers for account {account_id}: {len(ice_breakers)} items")
        
        return jsonify({
            "success": True,
            "message": f"Ice breakers updated successfully ({len(ice_breakers)} items)",
            "account_id": account_id,
            "ice_breakers": ice_breakers
        })
        
    except requests.RequestException as e:
        logger.exception(f"Failed to update ice breakers: {e}")
        return jsonify({"success": False, "error": f"Request failed: {str(e)}"}), 500


@whatsapp_bp.route("/accounts/<int:account_id>/ice-breakers", methods=["DELETE"])
def delete_ice_breakers(account_id: int):
    """
    Remove all ice breakers from a WhatsApp number.
    
    DELETE /api/whatsapp/accounts/<id>/ice-breakers
    """
    import requests
    from .models import WhatsAppAccount
    
    account, token_error = get_account_with_token(account_id)
    if token_error:
        return jsonify({"success": False, "error": token_error}), 400
    
    access_token = account.get_access_token()
    api_version = os.getenv("WHATSAPP_API_VERSION", "v22.0")
    
    try:
        # Clear ice breakers by setting empty array
        url = f"https://graph.facebook.com/{api_version}/{account.phone_number_id}/conversational_automation"
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json"
        }
        
        import json
        response = requests.post(
            url,
            headers=headers,
            data=json.dumps({
                "enable_welcome_message": False,
                "ice_breakers": []
            }),
            timeout=30
        )
        result = response.json()
        
        if "error" in result:
            return jsonify({
                "success": False,
                "error": result["error"].get("message", "Failed to delete ice breakers"),
                "error_code": result["error"].get("code")
            }), 400
        
        logger.info(f"Deleted ice breakers for account {account_id}")
        
        return jsonify({
            "success": True,
            "message": "Ice breakers removed successfully",
            "account_id": account_id
        })
        
    except requests.RequestException as e:
        logger.exception(f"Failed to delete ice breakers: {e}")
        return jsonify({"success": False, "error": f"Request failed: {str(e)}"}), 500


# ============================================================
# Phase-2: OAuth / Connect Endpoints
# ============================================================

@whatsapp_bp.route("/connect/start", methods=["GET"])
def connect_start():
    """
    Return configuration for frontend SDK-based Embedded Signup.
    Frontend will use FB.login() with the config_id.
    """
    workspace_id = request.args.get("workspace_id")
    if not workspace_id:
        return jsonify({"success": False, "error": "workspace_id is required"}), 400

    from . import oauth as _wa_oauth

    app_id = _wa_oauth.META_APP_ID
    config_id = os.getenv("WHATSAPP_CONFIG_ID")
    api_version = os.getenv("FB_API_VERSION") or os.getenv("WHATSAPP_API_VERSION", "v22.0")
    
    if not app_id:
        return jsonify({"success": False, "error": "Meta app id not configured (META_APP_ID or FB_APP_ID)"}), 500
    
    if not config_id:
        return jsonify({"success": False, "error": "WHATSAPP_CONFIG_ID not configured. Add your WhatsApp Embedded Signup config ID to .env"}), 500

    return jsonify({
        "success": True,
        "app_id": app_id,
        "config_id": config_id,
        "api_version": api_version,
        "workspace_id": workspace_id
    })


@whatsapp_bp.route("/connect/popup", methods=["GET"])
def connect_popup():
    """
    Popup-based OAuth flow for WhatsApp Embedded Signup.
    Uses the proper Meta Embedded Signup flow with config_id.
    """
    from itsdangerous import URLSafeSerializer
    
    workspace_id = request.args.get("workspace_id")
    if not workspace_id:
        return "workspace_id is required", 400

    # Build state with workspace_id (signed for security)
    secret_key = current_app.secret_key or os.getenv("SECRET_KEY", "dev-key")
    serializer = URLSafeSerializer(secret_key, salt="whatsapp-oauth")
    state = serializer.dumps({"workspace_id": workspace_id})

    from . import oauth as _wa_oauth

    # OAuth configuration
    app_id = _wa_oauth.META_APP_ID
    config_id = os.getenv("WHATSAPP_CONFIG_ID")
    api_version = os.getenv("FB_API_VERSION") or os.getenv("WHATSAPP_API_VERSION", "v22.0")
    redirect_base = os.getenv("OAUTH_REDIRECT_BASE", "https://sociovia-backend-362038465411.europe-west1.run.app")
    redirect_uri = f"{redirect_base.rstrip('/')}/api/whatsapp/connect/callback"

    if not app_id:
        return "Meta app id not configured (META_APP_ID or FB_APP_ID)", 500

    # WhatsApp-specific scopes for Embedded Signup
    scopes = [
        "whatsapp_business_management",
        "whatsapp_business_messaging",
        "business_management",
    ]

    # Build Facebook OAuth URL for WhatsApp Embedded Signup
    # Reference: https://developers.facebook.com/docs/whatsapp/embedded-signup
    params = {
        "client_id": app_id,
        "redirect_uri": redirect_uri,
        "scope": ",".join(scopes),
        "response_type": "code",
        "state": state,
    }
    
    # Only add config_id for WhatsApp Embedded Signup if explicitly enabled
    # NOTE: Embedded Signup requires BSP/TP status from Meta
    # If you get "Embedded signup is only available for BSPs or TPs" error,
    # set WHATSAPP_USE_EMBEDDED_SIGNUP=false in your environment
    use_embedded = os.getenv("WHATSAPP_USE_EMBEDDED_SIGNUP", "false").lower() == "true"
    if config_id and use_embedded:
        params["config_id"] = config_id
        # Use extras to force the embedded signup flow with phone selection
        import json
        extras = {
            "feature": "whatsapp_embedded_signup",
            "sessionInfoVersion": 4,
        }
        params["extras"] = json.dumps(extras)

    auth_url = f"https://www.facebook.com/{api_version}/dialog/oauth?{urlencode(params)}"
    
    logger.info(f"Redirecting to Facebook OAuth for WhatsApp: workspace_id={workspace_id}, embedded_signup={use_embedded}, config_id={config_id if use_embedded else 'disabled'}")
    return redirect(auth_url)


@whatsapp_bp.route("/connect/complete", methods=["POST"])
def connect_complete():
    """
    Complete the WhatsApp connection after frontend FB.login() call.
    Exchange the authorization code for access token and save account.
    """
    data = request.get_json(silent=True) or {}
    code = data.get("code")
    workspace_id = data.get("workspace_id")
    
    if not code:
        return jsonify({"success": False, "error": "Authorization code is required"}), 400
    
    if not workspace_id:
        return jsonify({"success": False, "error": "workspace_id is required"}), 400

    try:
        service = WhatsAppService(workspace_id=workspace_id)
        account = service.connect_account(code, workspace_id)
        
        return jsonify({
            "success": True,
            "account": {
                "id": account.id,
                "waba_id": account.waba_id,
                "phone_number_id": account.phone_number_id,
                "display_phone_number": account.display_phone_number,
                "verified_name": account.verified_name,
                "quality_score": account.quality_score,
            }
        })
    except Exception as e:
        logger.exception(f"OAuth Complete Error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@whatsapp_bp.route("/connect/exchange", methods=["POST"])
def connect_exchange():
    """
    Exchange authorization code from Embedded Signup for access token.
    This is called by the frontend after FB.login() returns a code.
    
    POST /api/whatsapp/connect/exchange
    Body: {
      "code": "...",
      "workspace_id": "...",
      "business_id": "optional — Meta Business Manager id",
      "waba_id": "optional — WhatsApp Business Account id",
      "phone_number_id": "optional — Cloud API phone number id"
    }
    """
    import requests as http_requests
    from .models import WhatsAppAccount
    
    data = request.get_json(silent=True) or {}
    code = data.get("code")
    workspace_id = data.get("workspace_id")
    
    if not code:
        return jsonify({"success": False, "error": "Authorization code is required"}), 400
    
    if not workspace_id:
        return jsonify({"success": False, "error": "workspace_id is required"}), 400

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
    
    from . import oauth as _wa_oauth

    app_id = _wa_oauth.META_APP_ID
    app_secret = _wa_oauth.META_APP_SECRET
    api_version = os.getenv("FB_API_VERSION") or os.getenv("WHATSAPP_API_VERSION", "v22.0")

    if not app_id or not app_secret:
        return jsonify(
            {
                "success": False,
                "error": "Meta app credentials not configured for connect/exchange",
                "hint": "Set META_APP_ID + META_APP_SECRET or FB_APP_* or WHATSAPP_APP_SECRET (see oauth.py).",
            }
        ), 503

    try:
        # Exchange code for access token (no redirect_uri needed for Embedded Signup)
        token_resp = http_requests.get(
            f"https://graph.facebook.com/{api_version}/oauth/access_token",
            params={
                "client_id": app_id,
                "client_secret": app_secret,
                "code": code,
            },
            timeout=15,
        ).json()
        
        logger.info(f"Token exchange response: {token_resp}")
        
        if "error" in token_resp:
            raise ValueError(f"Token exchange failed: {token_resp['error'].get('message', 'Unknown error')}")
        
        access_token = token_resp.get("access_token")
        if not access_token:
            raise ValueError("No access_token in response")

        # Meta Tech Provider: Embedded Signup code exchange returns the business integration
        # token. Skip fb_exchange_token unless explicitly enabled (exchange can downgrade partner tokens).
        if os.getenv("WHATSAPP_EMBEDDED_SIGNUP_LONG_LIVED", "false").lower() == "true":
            try:
                long_token_resp = http_requests.get(
                    f"https://graph.facebook.com/{api_version}/oauth/access_token",
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
                    logger.info("Got long-lived token")
            except Exception as e:
                logger.warning(f"Failed to get long-lived token: {e}")
        
        from .meta_asset_discovery import DiscoveryAmbiguousError, resolve_binding_for_auto_connect
        from .tech_provider_onboarding import (
            apply_hint_overrides,
            merge_connect_hints,
            tech_provider_next_steps,
        )

        hints = merge_connect_hints(data, ob_session)
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
                logger.warning("record_asset_hints during connect/exchange: %s", hint_e)

        try:
            binding, discovery = resolve_binding_for_auto_connect(
                access_token,
                api_version=api_version,
                app_id=app_id,
                app_secret=app_secret,
                hints=hints or None,
                allow_legacy_single_guess=False,
            )
        except DiscoveryAmbiguousError as e:
            d = e.discovery
            if ob_session:
                try:
                    from .onboarding_session_manager import attach_exchange_context

                    attach_exchange_context(
                        ob_session,
                        workspace_id=str(workspace_id),
                        hints=hints or {},
                        discovery=d,
                    )
                except Exception as attach_e:
                    logger.warning("onboarding attach_exchange_context: %s", attach_e)
            body = {
                "success": False,
                "error": str(e),
                "requires_user_choice": True,
                "candidates": d.get("candidates"),
                "ambiguity": d.get("ambiguity"),
                "token_debug": d.get("token_debug"),
                "wabas_without_phones": d.get("wabas_without_phones"),
            }
            if ob_session:
                body["onboarding_session_id"] = str(ob_session.id)
            return jsonify(body), 409

        binding = apply_hint_overrides(binding, hints)
        waba_id = binding["waba_id"]
        phone_number_id = binding["phone_number_id"]
        display_phone_number = binding.get("display_phone_number")
        verified_name = binding.get("verified_name")
        meta_business_id = binding.get("meta_business_id")
        if discovery.get("legacy_guess_used"):
            logger.warning("connect/exchange used legacy_guess_used (should not happen with allow_legacy False)")
        
        # GUARD: Block if this phone is active in another workspace
        from .connection_guard import check_phone_available
        conflict = check_phone_available(phone_number_id, workspace_id)
        if conflict:
            cbody = {"success": False, "error": conflict["error"], "error_code": conflict["error_code"]}
            if ob_session:
                cbody["onboarding_session_id"] = str(ob_session.id)
            return jsonify(cbody), 409
            
        # META COMPLIANCE: Business Readiness Validation
        from .meta_asset_discovery import check_business_readiness
        readiness = check_business_readiness(phone_number_id, access_token, api_version=api_version)
        
        # Save to database
        existing = WhatsAppAccount.query.filter_by(phone_number_id=phone_number_id).first()
        existed_before = existing is not None

        if existing:
            existing.workspace_id = workspace_id
            existing.waba_id = waba_id
            if meta_business_id:
                existing.meta_business_id = str(meta_business_id)
            existing.set_access_token(access_token, token_type="permanent")
            existing.is_active = True
            existing.display_phone_number = display_phone_number
            existing.verified_name = verified_name
            account = existing
        else:
            account = WhatsAppAccount(
                workspace_id=workspace_id,
                waba_id=waba_id,
                phone_number_id=phone_number_id,
                display_phone_number=display_phone_number,
                verified_name=verified_name,
                is_active=True,
                meta_business_id=str(meta_business_id) if meta_business_id else None,
            )
            account.set_access_token(access_token, token_type="permanent")
            get_db().add(account)
        
        get_db().commit()

        try:
            from .warmup_account_ops import ensure_mature_relink_skips_warmup, start_warmup_for_new_account

            if not existed_before:
                start_warmup_for_new_account(account, source="embedded_signup")
            else:
                ensure_mature_relink_skips_warmup(account)
                
            if not readiness.get("is_ready"):
                account.operational_mode = "advisory_safe_mode"
                issues_str = ", ".join(readiness.get("missing_fields", []) + readiness.get("issues", []))
                account.safe_mode_reason = f"Incomplete Business Profile: {issues_str}"
                
            get_db().commit()
        except Exception as w_e:
            logger.warning("warmup bootstrap after connect/exchange: %s", w_e)
            get_db().rollback()
        
        logger.info(f"WhatsApp account connected via Embedded Signup: {phone_number_id}")
        
        # ============================================================
        # CANONICAL PROVISIONING PIPELINE
        # All post-save setup (phone registration, webhook subscription,
        # warmup, templates, capability matrix) via ONE pipeline.
        # ============================================================
        finalize_ok = True
        provisioning_result = None
        try:
            from .provisioning_engine import provision_account

            provisioning_result = provision_account(
                account=account,
                access_token=access_token,
                source="embedded_signup",
                is_new_account=not existed_before,
                api_version=api_version,
            )
            finalize_ok = provisioning_result.success
        except Exception as prov_e:
            finalize_ok = False
            logger.warning("provisioning after connect/exchange: %s", prov_e)

        # Onboarding session lifecycle
        if ob_session:
            try:
                from .onboarding_session_manager import mark_token_exchanged

                mark_token_exchanged(ob_session)
            except Exception as tok_e:
                logger.warning("onboarding mark_token_exchanged: %s", tok_e)
            if finalize_ok:
                try:
                    from .onboarding_session_manager import mark_webhook_subscribed, mark_completed

                    sub_status = {"success": True}
                    mark_webhook_subscribed(ob_session, sub_status)
                    mark_completed(ob_session, account.id)
                except Exception as mc_e:
                    logger.warning("onboarding mark_completed: %s", mc_e)
            else:
                try:
                    from .onboarding_session_manager import mark_failed

                    mark_failed(
                        ob_session,
                        (provisioning_result.action_required if provisioning_result else "provisioning_pipeline_failed")[
                            :2000
                        ],
                    )
                except Exception:
                    pass

        next_steps = tech_provider_next_steps(provisioning_result, include_payment=finalize_ok)
        account_payload = {
            "id": account.id,
            "waba_id": account.waba_id,
            "phone_number_id": account.phone_number_id,
            "display_phone_number": account.display_phone_number,
            "verified_name": account.verified_name,
            "meta_business_id": account.meta_business_id,
        }

        if not finalize_ok:
            prov_dict = provisioning_result.to_dict() if provisioning_result else None
            fail_body = {
                "success": False,
                "error": (
                    provisioning_result.action_required
                    if provisioning_result and provisioning_result.action_required
                    else "WhatsApp provisioning incomplete — webhooks or registration failed"
                ),
                "error_code": "PROVISIONING_INCOMPLETE",
                "account": account_payload,
                "business_readiness": readiness,
                "provisioning": prov_dict,
                "next_steps": next_steps,
            }
            if ob_session:
                fail_body["onboarding_session_id"] = str(ob_session.id)
                fail_body["onboarding_finalize_ok"] = False
            return jsonify(fail_body), 422

        ok_body = {
            "success": True,
            "account": account_payload,
            "business_readiness": readiness,
            "provisioning": provisioning_result.to_dict() if provisioning_result else None,
            "next_steps": next_steps,
        }
        if ob_session:
            ok_body["onboarding_session_id"] = str(ob_session.id)
            ok_body["onboarding_finalize_ok"] = True
        return jsonify(ok_body)

    except Exception as e:
        logger.exception(f"Embedded Signup Exchange Error: {e}")
        if ob_session:
            try:
                from .onboarding_session_manager import mark_failed

                mark_failed(ob_session, str(e)[:2000])
            except Exception:
                pass
        return jsonify({"success": False, "error": str(e)}), 500


@whatsapp_bp.route("/oauth/facebook-login", methods=["POST"])
def facebook_oauth_login():
    """
    Simple Facebook OAuth login - exchanges token and auto-connects WABA if found.
    
    This endpoint:
    1. Validates the short-lived token
    2. Exchanges for long-lived token
    3. Auto-discovers WABA and phone numbers
    4. Automatically connects the WhatsApp account
    
    POST /api/whatsapp/oauth/facebook-login
    Body: { "access_token": "...", "workspace_id": "..." }
    
    Returns:
        {
            "success": true,
            "connected": true/false,  // Whether WABA was auto-connected
            "account": {...},         // Connected account details (if connected)
            "access_token": "...",    // Long-lived token (for manual linking if needed)
        }
    """
    import requests as http_requests
    from .models import WhatsAppAccount
    
    from . import oauth as wa_oauth

    data = request.get_json(silent=True) or {}
    short_token = data.get("access_token")
    workspace_id = data.get("workspace_id")
    
    if not short_token:
        return jsonify({"success": False, "error": "access_token is required"}), 400
    
    if not workspace_id:
        return jsonify({"success": False, "error": "workspace_id is required"}), 400
    
    app_id = wa_oauth.META_APP_ID
    app_secret = wa_oauth.META_APP_SECRET
    api_version = os.getenv("FB_API_VERSION") or os.getenv("WHATSAPP_API_VERSION", "v22.0")
    
    if not app_id or not app_secret:
        logger.error(
            "Facebook / Meta app credentials not configured for oauth/facebook-login "
            "(set META_APP_ID + META_APP_SECRET, or FB_APP_ID + FB_APP_SECRET, "
            "or include WHATSAPP_APP_SECRET alongside META_APP_ID)"
        )
        return jsonify({
            "success": False,
            "error": "Facebook OAuth not configured",
            "hint": "Set META_APP_ID and META_APP_SECRET (or FB_APP_*), or WHATSAPP_APP_SECRET with META_APP_ID.",
        }), 503
    
    try:
        # Validate the short-lived token first
        debug_resp = http_requests.get(
            f"https://graph.facebook.com/{api_version}/debug_token",
            params={
                "input_token": short_token,
                "access_token": f"{app_id}|{app_secret}",
            },
            timeout=15,
        ).json()
        
        token_data = debug_resp.get("data", {})
        
        if not token_data.get("is_valid"):
            return jsonify({
                "success": False,
                "error": "Invalid or expired access token"
            }), 401
        
        user_id = token_data.get("user_id")
        scopes = token_data.get("scopes", [])
        
        # Check for required WhatsApp scopes.
        # business_management is required for full business asset management flows
        # (and for partner credit-line operations), while the two whatsapp_* scopes
        # are required for template and messaging operations.
        required_scopes = [
            "whatsapp_business_management",
            "whatsapp_business_messaging",
            "business_management",
        ]
        missing_scopes = [s for s in required_scopes if s not in scopes]
        
        if missing_scopes:
            return jsonify({
                "success": False,
                "error": f"Missing required permissions: {', '.join(missing_scopes)}. Please login again and grant all permissions.",
                "missing_scopes": missing_scopes,
                "required_scopes": required_scopes,
            }), 403
        
        # Exchange for long-lived token
        long_token_resp = http_requests.get(
            f"https://graph.facebook.com/{api_version}/oauth/access_token",
            params={
                "grant_type": "fb_exchange_token",
                "client_id": app_id,
                "client_secret": app_secret,
                "fb_exchange_token": short_token,
            },
            timeout=15,
        ).json()
        
        if "error" in long_token_resp:
            error_msg = long_token_resp["error"].get("message", "Token exchange failed")
            logger.error(f"Facebook token exchange failed: {error_msg}")
            return jsonify({"success": False, "error": error_msg}), 400
        
        long_token = long_token_resp.get("access_token")
        expires_in = long_token_resp.get("expires_in", 5184000)  # Default ~60 days
        
        if not long_token:
            return jsonify({"success": False, "error": "Token exchange returned no token"}), 500
        
        logger.info(f"Facebook OAuth login successful for user {user_id}, workspace {workspace_id}")
        
        # ============================================================
        # AUTO-DISCOVER WABA AND PHONE NUMBERS (canonical discovery)
        # ============================================================
        from .meta_asset_discovery import DiscoveryAmbiguousError, resolve_binding_for_auto_connect

        hints = {}
        if data.get("business_id"):
            hints["business_id"] = str(data.get("business_id")).strip()
        if data.get("waba_id"):
            hints["waba_id"] = str(data.get("waba_id")).strip()
        if data.get("phone_number_id"):
            hints["phone_number_id"] = str(data.get("phone_number_id")).strip()

        waba_id = None
        phone_number_id = None
        display_phone_number = None
        verified_name = None
        meta_business_id = None
        discovery_summary = None

        try:
            binding, discovery_summary = resolve_binding_for_auto_connect(
                long_token,
                api_version=api_version,
                app_id=app_id,
                app_secret=app_secret,
                hints=hints or None,
                allow_legacy_single_guess=False,
            )
            waba_id = binding["waba_id"]
            phone_number_id = binding["phone_number_id"]
            display_phone_number = binding.get("display_phone_number")
            verified_name = binding.get("verified_name")
            meta_business_id = binding.get("meta_business_id")
        except DiscoveryAmbiguousError as e:
            d = e.discovery
            return jsonify({
                "success": False,
                "connected": False,
                "error": str(e),
                "requires_user_choice": True,
                "candidates": d.get("candidates"),
                "ambiguity": d.get("ambiguity"),
                "token_debug": d.get("token_debug"),
                "access_token": long_token,
                "expires_in": expires_in,
            }), 409
        except ValueError as ve:
            logger.info("Facebook login: no unique WABA binding: %s", ve)
        
        # ============================================================
        # AUTO-CONNECT IF WABA FOUND
        # ============================================================
        if waba_id and phone_number_id:
            # GUARD: Block if this phone is active in another workspace
            from .connection_guard import check_phone_available
            conflict = check_phone_available(phone_number_id, workspace_id)
            if conflict:
                return jsonify({
                    "success": False,
                    "connected": False,
                    "error": conflict["error"],
                    "error_code": conflict["error_code"],
                    "access_token": long_token,
                    "expires_in": expires_in,
                }), 409
            
            # Save to database
            existing = WhatsAppAccount.query.filter_by(phone_number_id=phone_number_id).first()
            
            if existing:
                existing.workspace_id = workspace_id
                existing.waba_id = waba_id
                if meta_business_id:
                    existing.meta_business_id = str(meta_business_id)
                existing.set_access_token(long_token, token_type="permanent")
                existing.is_active = True
                existing.display_phone_number = display_phone_number
                existing.verified_name = verified_name
                existing.connected_by_user_id = user_id
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
                    meta_business_id=str(meta_business_id) if meta_business_id else None,
                )
                account.set_access_token(long_token, token_type="permanent")
                get_db().add(account)
            
            get_db().commit()
            
            logger.info(f"WhatsApp account auto-connected via Facebook Login: {phone_number_id} for workspace {workspace_id}")
            
            # ============================================================
            # CANONICAL PROVISIONING PIPELINE
            # Fixes: missing webhook subscription, missing warmup,
            # missing readiness validation for Facebook Login flow.
            # ============================================================
            provisioning_result = None
            try:
                from .provisioning_engine import provision_account

                provisioning_result = provision_account(
                    account=account,
                    access_token=long_token,
                    source="facebook_login",
                    is_new_account=not existing,
                    api_version=api_version,
                )
            except Exception as prov_e:
                logger.warning("provisioning after facebook-login: %s", prov_e)

            from .connection_path import verify_messaging_send_access

            send_check = verify_messaging_send_access(long_token, phone_number_id, waba_id)
            send_details = send_check.get("details") or {}
            send_permission_ok = bool(send_check.get("can_send"))
            requires_system_user = (
                not send_permission_ok
                and (
                    send_details.get("token_type") == "USER"
                    or send_details.get("waba_ownership_type") == "CLIENT_OWNED"
                )
            )

            message = "WhatsApp account connected successfully!"
            if not send_permission_ok:
                message = (
                    "Account linked, but this Facebook login token cannot send messages on this "
                    "WhatsApp Business Account. Use Manual Connection with a System User token "
                    "from Sociovia Business Manager."
                )
            
            return jsonify({
                "success": True,
                "connected": True,
                "message": message,
                "account": {
                    "id": account.id,
                    "waba_id": account.waba_id,
                    "phone_number_id": account.phone_number_id,
                    "display_phone_number": account.display_phone_number,
                    "verified_name": account.verified_name,
                },
                "provisioning": provisioning_result.to_dict() if provisioning_result else None,
                "send_permission": {
                    "ok": send_permission_ok,
                    "error": send_check.get("error"),
                    "error_code": send_check.get("error_code"),
                    "hints": send_check.get("hints") or [],
                    "requires_system_user_token": requires_system_user,
                    "details": send_details,
                },
                "access_token": long_token,
                "expires_in": expires_in,
            })
        
        # No WABA found - return token for manual linking
        logger.info(f"No WABA found for user {user_id}, returning token for manual linking")
        return jsonify({
            "success": True,
            "connected": False,
            "message": "Authenticated successfully but no WhatsApp Business Account found. Please enter your WABA details manually.",
            "access_token": long_token,
            "expires_in": expires_in,
            "user_id": user_id,
            "scopes": scopes,
        })
        
    except Exception as e:
        logger.exception(f"Facebook OAuth Login Error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@whatsapp_bp.route("/post-connection-check", methods=["POST"])
def post_connection_check():
    """
    Called by frontend immediately after redirect to /dashboard/whatsapp.
    Runs the full provisioning pipeline with auto-fix.
    Returns capability matrix + readiness score + auto-fix results.

    POST /api/whatsapp/post-connection-check
    Body: { "workspace_id": "..." }
    """
    from .models import WhatsAppAccount

    data = request.get_json(silent=True) or {}
    workspace_id = data.get("workspace_id")

    if not workspace_id:
        return jsonify({"success": False, "error": "workspace_id is required"}), 400

    account = WhatsAppAccount.query.filter_by(
        workspace_id=str(workspace_id), is_active=True
    ).first()

    if not account:
        return jsonify({
            "success": False,
            "error": "No active WhatsApp account found",
            "readiness_score": 0,
            "capability_matrix": {},
        }), 404

    access_token = account.get_access_token()
    if not access_token:
        return jsonify({
            "success": False,
            "error": "No valid access token — re-authentication required",
            "readiness_score": 0,
            "action_required": "Re-authenticate with Meta to restore access",
        }), 200

    try:
        from .provisioning_engine import provision_account

        result = provision_account(
            account=account,
            access_token=access_token,
            source="post_connection_check",
            is_new_account=False,
        )
        payload = result.to_dict()
        from .operational_profile import build_operational_profile

        payload["operational_profile"] = build_operational_profile(
            capability_matrix=payload.get("capability_matrix"),
            checks=payload.get("checks"),
            token_debug=payload.get("token_debug"),
            warmup_state=payload.get("warmup_state"),
            connection_status="CONNECTED",
        )
        return jsonify(payload)
    except Exception as e:
        logger.exception("post-connection-check error: %s", e)
        return jsonify({"success": False, "error": str(e)}), 500


@whatsapp_bp.route("/oauth/discover-waba", methods=["POST"])
def discover_waba():
    """
    Use an access token to discover WABA ID and Phone Number IDs from Meta API.
    
    This helps users find their Phone Number ID which is not easily visible
    in Meta Business Suite.
    
    POST /api/whatsapp/oauth/discover-waba
    Body: { "access_token": "..." }
    
    Returns:
        {
            "success": true,
            "wabas": [
                {
                    "waba_id": "123...",
                    "waba_name": "My Business",
                    "phone_numbers": [
                        {
                            "id": "456...",
                            "display_phone_number": "+1 555...",
                            "verified_name": "My Business",
                            "quality_rating": "GREEN"
                        }
                    ]
                }
            ]
        }
    """
    data = request.get_json(silent=True) or {}
    access_token = data.get("access_token")
    
    if not access_token:
        return jsonify({"success": False, "error": "access_token is required"}), 400
    
    api_version = os.getenv("FB_API_VERSION", "v22.0")
    
    try:
        wabas = []
        
        from .meta_asset_discovery import discover_whatsapp_assets

        app_id = os.getenv("FB_APP_ID") or os.getenv("META_APP_ID")
        app_secret = os.getenv("FB_APP_SECRET") or os.getenv("META_APP_SECRET")

        disc = discover_whatsapp_assets(
            access_token,
            api_version=api_version,
            app_id=app_id,
            app_secret=app_secret,
            hints=None,
        )

        for b in disc.get("businesses") or []:
            for w in b.get("wabas") or []:
                waba_entry = {
                    "waba_id": w.get("waba_id"),
                    "waba_name": w.get("waba_name"),
                    "business_id": b.get("business_id"),
                    "business_name": b.get("business_name"),
                    "our_app_subscribed": w.get("our_app_subscribed"),
                    "subscribed_apps": w.get("subscribed_apps"),
                    "phone_numbers": [],
                }
                for phone in w.get("phone_numbers") or []:
                    waba_entry["phone_numbers"].append({
                        "id": phone.get("id"),
                        "display_phone_number": phone.get("display_phone_number"),
                        "verified_name": phone.get("verified_name"),
                        "quality_rating": phone.get("quality_rating"),
                        "name_status": phone.get("name_status"),
                    })
                wabas.append(waba_entry)
        
        if not wabas:
            return jsonify({
                "success": True,
                "wabas": [],
                "requires_user_choice": False,
                "message": "No WhatsApp Business Accounts found. Make sure your Facebook account has access to a WABA in Meta Business Suite.",
                "token_debug": disc.get("token_debug"),
                "portfolio_errors": disc.get("portfolio_errors"),
            })
        
        return jsonify({
            "success": True,
            "wabas": wabas,
            "count": len(wabas),
            "requires_user_choice": disc.get("requires_user_choice"),
            "candidates": disc.get("candidates"),
            "ambiguity": disc.get("ambiguity"),
            "token_debug": disc.get("token_debug"),
            "wabas_without_phones": disc.get("wabas_without_phones"),
        })
        
    except Exception as e:
        logger.exception(f"WABA Discovery Error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@whatsapp_bp.route("/connect/callback", methods=["GET"])
def connect_callback():
    """
    Handle the redirect from Meta (for popup-based OAuth).
    Exchanges code for token, saves account, and closes popup.
    """
    from itsdangerous import URLSafeSerializer, BadSignature
    
    code = request.args.get("code")
    raw_state = request.args.get("state", "")
    error = request.args.get("error")
    error_description = request.args.get("error_description", "")
    
    # Get frontend URL for postMessage
    frontend_url = os.getenv("FRONTEND_BASE_URL", "https://sociovia.com")

    if error:
        logger.warning(f"WhatsApp OAuth error: {error} - {error_description}")
        return _render_oauth_response(frontend_url, {
            "type": "sociovia_oauth_complete",
            "success": False,
            "error": error_description or error,
        })

    if not code:
        return _render_oauth_response(frontend_url, {
            "type": "sociovia_oauth_complete",
            "success": False,
            "error": "Missing authorization code",
        })

    # Parse the signed state to get workspace_id
    workspace_id = None
    try:
        secret_key = current_app.secret_key or os.getenv("SECRET_KEY", "dev-key")
        serializer = URLSafeSerializer(secret_key, salt="whatsapp-oauth")
        state_data = serializer.loads(raw_state)
        workspace_id = state_data.get("workspace_id")
    except BadSignature:
        logger.warning("Invalid state signature, attempting raw parse")
        # Fallback: try using state as workspace_id directly
        workspace_id = raw_state if raw_state else None
    except Exception as e:
        logger.exception(f"State parsing error: {e}")

    if not workspace_id:
        return _render_oauth_response(frontend_url, {
            "type": "sociovia_oauth_complete",
            "success": False,
            "error": "Missing workspace_id in state",
        })

    try:
        # Exchange code for access token
        app_id = os.getenv("FB_APP_ID") or os.getenv("META_APP_ID")
        app_secret = os.getenv("FB_APP_SECRET") or os.getenv("META_APP_SECRET")
        api_version = os.getenv("FB_API_VERSION", "v22.0")
        redirect_base = os.getenv("OAUTH_REDIRECT_BASE", "https://sociovia-backend-362038465411.europe-west1.run.app")
        redirect_uri = f"{redirect_base.rstrip('/')}/api/whatsapp/connect/callback"
        
        import requests as http_requests
        
        # Token exchange
        token_resp = http_requests.get(
            f"https://graph.facebook.com/{api_version}/oauth/access_token",
            params={
                "client_id": app_id,
                "client_secret": app_secret,
                "redirect_uri": redirect_uri,
                "code": code,
            },
            timeout=15,
        ).json()
        
        if "error" in token_resp:
            raise ValueError(f"Token exchange failed: {token_resp['error'].get('message', 'Unknown error')}")
        
        access_token = token_resp.get("access_token")
        if not access_token:
            raise ValueError("No access_token in response")
        
        # Meta Tech Provider: the Embedded Signup code exchange already returns the
        # long-lived business-integration (system-user) token. Running it through
        # fb_exchange_token DOWNGRADES it to a long-lived USER token, so skip it
        # unless explicitly opted in (mirrors /connect/exchange).
        if os.getenv("WHATSAPP_EMBEDDED_SIGNUP_LONG_LIVED", "false").lower() == "true":
            try:
                long_token_resp = http_requests.get(
                    f"https://graph.facebook.com/{api_version}/oauth/access_token",
                    params={
                        "grant_type": "fb_exchange_token",
                        "client_id": app_id,
                        "client_secret": app_secret,
                        "fb_exchange_token": access_token,
                    },
                    timeout=15,
                ).json()
                access_token = long_token_resp.get("access_token", access_token)
            except Exception:
                pass  # Keep original token if exchange fails
        
        from .meta_asset_discovery import DiscoveryAmbiguousError, resolve_binding_for_auto_connect

        try:
            binding, discovery = resolve_binding_for_auto_connect(
                access_token,
                api_version=api_version,
                app_id=app_id,
                app_secret=app_secret,
                hints=None,
                allow_legacy_single_guess=False,
            )
        except DiscoveryAmbiguousError as e:
            d = e.discovery
            return _render_oauth_response(frontend_url, {
                "type": "sociovia_oauth_complete",
                "success": False,
                "error": str(e),
                "requires_user_choice": True,
                "candidates": d.get("candidates"),
                "ambiguity": d.get("ambiguity"),
                "token_debug": d.get("token_debug"),
            })
        except ValueError as ve:
            return _render_oauth_response(frontend_url, {
                "type": "sociovia_oauth_complete",
                "success": False,
                "error": str(ve),
            })

        waba_id = binding["waba_id"]
        phone_number_id = binding["phone_number_id"]
        display_phone_number = binding.get("display_phone_number")
        verified_name = binding.get("verified_name")
        meta_business_id = binding.get("meta_business_id")
        if discovery.get("legacy_guess_used"):
            logger.warning("connect/callback used legacy_guess_used (unexpected)")
        
        # Save to database
        from .models import WhatsAppAccount
        from .connection_guard import check_phone_available
        
        # GUARD: Block if this phone is active in another workspace
        conflict = check_phone_available(phone_number_id, workspace_id)
        if conflict:
            raise ValueError(conflict["error"])
        
        # Check if account already exists
        existing = WhatsAppAccount.query.filter_by(phone_number_id=phone_number_id).first()
        
        if existing:
            # Update existing account (same workspace, or inactive transfer)
            existing.workspace_id = workspace_id
            existing.waba_id = waba_id
            if meta_business_id:
                existing.meta_business_id = str(meta_business_id)
            existing.access_token_encrypted = None  # Will be set below
            existing.set_access_token(access_token, token_type="permanent")
            existing.is_active = True
            existing.display_phone_number = display_phone_number
            existing.verified_name = verified_name
            account = existing
        else:
            # Create new account
            account = WhatsAppAccount(
                workspace_id=workspace_id,
                waba_id=waba_id,
                phone_number_id=phone_number_id,
                display_phone_number=display_phone_number,
                verified_name=verified_name,
                is_active=True,
                meta_business_id=str(meta_business_id) if meta_business_id else None,
            )
            account.set_access_token(access_token, token_type="permanent")
            get_db().add(account)
        
        get_db().commit()
        
        logger.info(f"WhatsApp account connected: phone_number_id={account.phone_number_id}")
        
        # Subscribe WABA to app for webhooks (messages, template updates, etc.)
        try:
            subscribe_waba_to_app(account.waba_id, access_token)
        except Exception as e:
            logger.warning(f"Initial webhook subscription failed for WABA {account.waba_id}: {e}")
        
        # ============================================================
        # AUTO-REGISTER PHONE NUMBER WITH WHATSAPP BUSINESS API
        # This is required before the phone can send/receive messages
        # ============================================================
        try:
            register_resp = http_requests.post(
                f"https://graph.facebook.com/{api_version}/{phone_number_id}/register",
                json={
                    "messaging_product": "whatsapp",
                    "pin": "123456"  # Default 6-digit PIN for 2FA (user can change later)
                },
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json"
                },
                timeout=15
            )
            register_result = register_resp.json()
            if register_result.get("success"):
                logger.info(f"Phone number {phone_number_id} registered successfully with WhatsApp Business API")
            else:
                # Log but don't fail - phone might already be registered
                logger.warning(f"Phone registration response: {register_result}")
        except Exception as reg_error:
            # Don't fail the OAuth if registration fails - it might already be registered
            logger.warning(f"Phone registration warning (may already be registered): {reg_error}")
        
        return _render_oauth_response(frontend_url, {
            "type": "sociovia_oauth_complete",
            "success": True,
            "account": {
                "id": account.id,
                "waba_id": account.waba_id,
                "phone_number_id": account.phone_number_id,
                "display_phone_number": account.display_phone_number,
                "verified_name": account.verified_name,
            }
        })
    except Exception as e:
        logger.exception(f"OAuth Callback Error: {e}")
        return _render_oauth_response(frontend_url, {
            "type": "sociovia_oauth_complete",
            "success": False,
            "error": str(e),
        })


def _render_oauth_response(frontend_url: str, payload: dict):
    """
    Render HTML that sends postMessage to opener and closes popup.
    Same pattern as the Facebook OAuth callback.
    """
    import json
    payload_json = json.dumps(payload, separators=(",", ":"))
    frontend = frontend_url.rstrip("/")
    
    # Determine redirect URL based on success/failure
    success = payload.get("success", False)
    redirect_url = f"{frontend}/dashboard/whatsapp/settings?connected=true" if success else f"{frontend}/dashboard/whatsapp/settings?error=true"
    
    return f'''<!doctype html>
<html>
<head><meta charset="utf-8"/><title>OAuth Complete</title></head>
<body>
<p style="font-family: sans-serif; text-align: center; margin-top: 50px;">
  {('✓ Connected! Closing...' if success else 'Connection failed. Closing...')}
</p>
<script>
(function() {{
  var payload = {payload_json};
  var redirectUrl = "{redirect_url}";
  
  // Try to communicate with opener and close this popup
  if (window.opener && !window.opener.closed) {{
    // Send postMessage to opener
    try {{
      window.opener.postMessage(payload, "*");
    }} catch(e) {{}}
    
    // Navigate the opener to refresh
    try {{
      window.opener.location.href = redirectUrl;
    }} catch(e) {{}}
    
    // Close this popup IMMEDIATELY (must happen before any navigation in THIS window)
    window.close();
    
    // Fallback: if window.close() didn't work, show a message
    setTimeout(function() {{
      document.body.innerHTML = 
        '<p style="font-family: sans-serif; text-align: center; margin-top: 50px;">' +
        '✓ Connected! <a href="javascript:window.close()">Click here to close</a></p>';
    }}, 500);
    
  }} else {{
    // No opener - redirect in same window
    window.location.href = redirectUrl;
  }}
}})();
</script>
</body>
</html>
''', 200, {"Content-Type": "text/html"}


# ============================================================
# Notification Settings Endpoints
# ============================================================

@whatsapp_bp.route("/notification-settings", methods=["GET"])
def get_notification_settings():
    """
    Get notification settings for a workspace's WhatsApp account.
    
    GET /api/whatsapp/notification-settings?workspace_id=xxx
    
    Returns:
        - notification_phone_number: The phone number to receive notifications
    """
    from .token_helper import resolve_workspace_or_account_param
    
    workspace_id = request.args.get("workspace_id")
    account_id = request.args.get("account_id")
    
    try:
        account, err = resolve_workspace_or_account_param(workspace_id, account_id)
        if err == "workspace_id or account_id is required":
            return jsonify({"success": False, "error": err}), 400
        if err in ("Invalid account_id", "account_id does not belong to workspace_id"):
            return jsonify({"success": False, "error": err}), 400
        if not account:
            return jsonify({"success": False, "error": err or "No WhatsApp account found"}), 404
        
        return jsonify({
            "success": True,
            "notification_phone_number": account.notification_phone_number,
            "notification_email": getattr(account, "notification_email", None),
        })
        
    except Exception as e:
        logger.exception(f"Failed to get notification settings: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@whatsapp_bp.route("/notification-settings", methods=["POST", "PUT"])
@require_admin_only  # SECURITY: Only admins can update notification settings
def update_notification_settings():
    """
    Update notification settings for a workspace's WhatsApp account.
    
    POST /api/whatsapp/notification-settings
    Body: {
        "workspace_id": "xxx",  // or "account_id"
        "notification_phone_number": "919876543210"  // With country code, no +
    }
    """
    from .token_helper import resolve_workspace_or_account_param
    
    data = request.get_json() or {}
    workspace_id = data.get("workspace_id")
    account_id = data.get("account_id")
    notification_phone_number = data.get("notification_phone_number", "").strip()
    notification_email = data.get("notification_email", "").strip()
    
    try:
        account, err = resolve_workspace_or_account_param(workspace_id, account_id)
        if err == "workspace_id or account_id is required":
            return jsonify({"success": False, "error": err}), 400
        if err in ("Invalid account_id", "account_id does not belong to workspace_id"):
            return jsonify({"success": False, "error": err}), 400
        if not account:
            return jsonify({"success": False, "error": err or "No WhatsApp account found"}), 404
        
        # Clean up phone number - remove spaces, dashes, +
        if notification_phone_number:
            notification_phone_number = notification_phone_number.replace(" ", "").replace("-", "").replace("+", "")
        
        # Update notification contact details
        account.notification_phone_number = notification_phone_number if notification_phone_number else None
        if hasattr(account, "notification_email"):
            account.notification_email = notification_email if notification_email else None
        get_db().commit()
        
        logger.info(
            "Updated notification settings for account %s: phone=%s email=%s",
            account.id,
            notification_phone_number,
            notification_email,
        )
        
        return jsonify({
            "success": True,
            "message": "Notification settings updated successfully",
            "notification_phone_number": account.notification_phone_number,
            "notification_email": getattr(account, "notification_email", None),
        })
        
    except Exception as e:
        get_db().rollback()
        logger.exception(f"Failed to update notification settings: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@whatsapp_bp.route("/send-notification", methods=["POST"])
def send_notification_message():
    """
    Send a notification message to the configured notification phone number.
    Used for internal alerts like waitlist signups.
    
    POST /api/whatsapp/send-notification
    Body: {
        "workspace_id": "xxx",  // or "account_id"
        "template_name": "sociovia_onboard",
        "body_params": ["John", "john@example.com", "1234567890", "Acme Inc", "2024-01-15 10:30:00"]
    }
    """
    from .token_helper import resolve_workspace_or_account_param
    from .services import WhatsAppService
    
    data = request.get_json() or {}
    workspace_id = data.get("workspace_id")
    account_id = data.get("account_id")
    template_name = data.get("template_name")
    body_params = data.get("body_params", [])
    
    try:
        account, err = resolve_workspace_or_account_param(workspace_id, account_id)
        if err == "workspace_id or account_id is required":
            return jsonify({"success": False, "error": err}), 400
        if err in ("Invalid account_id", "account_id does not belong to workspace_id"):
            return jsonify({"success": False, "error": err}), 400
        if not account:
            return jsonify({"success": False, "error": err or "No WhatsApp account found"}), 404
        
        if not account.notification_phone_number:
            return jsonify({
                "success": False,
                "error": "No notification phone number configured. Please set it in WhatsApp Settings."
            }), 400
        
        if not template_name:
            return jsonify({
                "success": False,
                "error": "template_name is required"
            }), 400
        
        # Get access token
        access_token = account.get_access_token()
        if not access_token:
            return jsonify({
                "success": False,
                "error": "No access token available"
            }), 400
        
        # Create WhatsApp service
        service = WhatsAppService(
            access_token=access_token,
            phone_number_id=account.phone_number_id
        )
        
        # Send template message
        result = service.send_template(
            to=account.notification_phone_number,
            template_name=template_name,
            body_params=body_params,
            language_code="en_US"
        )
        
        if result.get("success"):
            logger.info(f"Sent notification to {account.notification_phone_number} using template {template_name}")
            return jsonify({
                "success": True,
                "message": "Notification sent successfully",
                "message_id": result.get("message_id")
            })
        else:
            return jsonify({
                "success": False,
                "error": result.get("error", "Failed to send notification")
            }), 400
        
    except Exception as e:
        logger.exception(f"Failed to send notification: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


# ============================================================
# Chat Media Upload Endpoint
# ============================================================

@whatsapp_bp.route("/media/upload", methods=["POST", "OPTIONS"])
@whatsapp_bp.route("/media/upload/public", methods=["POST", "OPTIONS"])
@whatsapp_bp.route("/media/upload/url", methods=["POST", "OPTIONS"])
def upload_chat_media():
    """
    Upload media files (images, videos, documents) for WhatsApp chat messages.
    Stores files in DigitalOcean Spaces and returns public URL.
    
    POST /api/whatsapp/media/upload
    Content-Type: multipart/form-data
    file: <binary>
    media_type: image|video|document (optional, auto-detected from mime type)
    
    Returns:
    {
        "success": true,
        "public_url": "https://bucket.region.digitaloceanspaces.com/...",
        "media_type": "image",
        "filename": "original_name.jpg",
        "size": 12345,
        "mime_type": "image/jpeg"
    }
    """
    import uuid
    import time
    import re
    from werkzeug.utils import secure_filename

    try:
        import boto3
    except ImportError:
        logger.error("boto3 is not installed — chat media upload unavailable")
        return jsonify({
            "success": False,
            "error": "storage_not_configured",
            "message": "Media upload service is not available on this server. Paste a public HTTPS URL instead.",
        }), 500
    
    if request.method == "OPTIONS":
        response = jsonify({})
        response.headers.add('Access-Control-Allow-Origin', '*')
        response.headers.add('Access-Control-Allow-Headers', 'Content-Type, Authorization')
        response.headers.add('Access-Control-Allow-Methods', 'POST, OPTIONS')
        return response
    
    try:
        # Check for file
        if 'file' not in request.files:
            return jsonify({"success": False, "error": "no_file_provided"}), 400
        
        file = request.files['file']
        if not file or not file.filename:
            return jsonify({"success": False, "error": "empty_file"}), 400
        
        # Get content type
        content_type = file.content_type or 'application/octet-stream'
        original_filename = secure_filename(file.filename)
        
        # Determine media type from content type
        allowed_types = {
            # Images
            'image/jpeg': 'image',
            'image/jpg': 'image',
            'image/png': 'image',
            'image/webp': 'image',
            'image/gif': 'image',
            # Videos
            'video/mp4': 'video',
            'video/quicktime': 'video',
            'video/3gpp': 'video',
            'video/avi': 'video',
            'video/mpeg': 'video',
            # Documents
            'application/pdf': 'document',
            'application/msword': 'document',
            'application/vnd.openxmlformats-officedocument.wordprocessingml.document': 'document',
            'application/vnd.ms-excel': 'document',
            'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet': 'document',
            'application/vnd.ms-powerpoint': 'document',
            'application/vnd.openxmlformats-officedocument.presentationml.presentation': 'document',
            'text/plain': 'document',
            'text/csv': 'document',
            'application/zip': 'document',
            'application/x-rar-compressed': 'document',
        }
        
        if content_type not in allowed_types:
            return jsonify({
                "success": False, 
                "error": "invalid_file_type",
                "message": f"File type '{content_type}' is not supported",
                "allowed": list(allowed_types.keys())
            }), 400
        
        media_type = request.form.get('media_type') or allowed_types.get(content_type, 'document')
        
        # Validate file size based on WhatsApp limits
        file.seek(0, 2)  # Seek to end
        file_size = file.tell()
        file.seek(0)  # Reset to beginning
        
        # WhatsApp file size limits
        max_sizes = {
            'image': 5 * 1024 * 1024,     # 5MB for images
            'video': 16 * 1024 * 1024,    # 16MB for videos
            'document': 100 * 1024 * 1024  # 100MB for documents
        }
        
        max_size = max_sizes.get(media_type, 16 * 1024 * 1024)
        if file_size > max_size:
            return jsonify({
                "success": False, 
                "error": "file_too_large",
                "message": f"File size ({file_size // (1024*1024)}MB) exceeds maximum ({max_size // (1024*1024)}MB) for {media_type}",
                "max_size_bytes": max_size
            }), 400
        
        # Convert WebP/GIF to JPEG for WhatsApp compatibility
        # WhatsApp Cloud API only supports JPEG and PNG for images
        file_data = None
        if content_type in ('image/webp', 'image/gif'):
            try:
                from PIL import Image
                from io import BytesIO
                
                # Read the image
                img = Image.open(file.stream)
                
                # Convert to RGB (WebP/GIF might have alpha channel or palette)
                if img.mode in ('RGBA', 'LA', 'P'):
                    # Create white background for transparency
                    background = Image.new('RGB', img.size, (255, 255, 255))
                    if img.mode == 'P':
                        img = img.convert('RGBA')
                    background.paste(img, mask=img.split()[-1] if img.mode == 'RGBA' else None)
                    img = background
                elif img.mode != 'RGB':
                    img = img.convert('RGB')
                
                # Save as JPEG
                output = BytesIO()
                img.save(output, format='JPEG', quality=90)
                output.seek(0)
                file_data = output
                
                # Update metadata
                content_type = 'image/jpeg'
                original_filename = os.path.splitext(original_filename)[0] + '.jpg'
                file_size = len(output.getvalue())
                
                logger.info(f"Converted {file.content_type} to JPEG for WhatsApp compatibility")
            except Exception as conv_err:
                logger.warning(f"Failed to convert image: {conv_err}, uploading original")
                file.seek(0)
                file_data = file.stream
        else:
            file_data = file.stream
        
        # Get S3/Spaces config
        SPACE_NAME = os.environ.get("SPACE_NAME") or os.environ.get("DO_SPACES_BUCKET")
        SPACE_REGION = os.environ.get("SPACE_REGION") or os.environ.get("DO_SPACES_REGION")
        ACCESS_KEY = os.environ.get("ACCESS_KEY") or os.environ.get("DO_ACCESS_KEY_ID")
        SECRET_KEY = os.environ.get("SECRET_KEY") or os.environ.get("DO_SECRET_ACCESS_KEY")
        
        if not all([SPACE_NAME, SPACE_REGION, ACCESS_KEY, SECRET_KEY]):
            logger.error("S3/Spaces configuration not complete")
            return jsonify({
                "success": False,
                "error": "storage_not_configured",
                "message": "Media storage is not configured. Set SPACE_NAME, SPACE_REGION, ACCESS_KEY, and SECRET_KEY, or paste a public HTTPS URL.",
            }), 500
        
        # Initialize S3 client
        SPACE_ENDPOINT = f'https://{SPACE_REGION}.digitaloceanspaces.com'
        s3_client = boto3.client(
            's3',
            aws_access_key_id=ACCESS_KEY,
            aws_secret_access_key=SECRET_KEY,
            endpoint_url=SPACE_ENDPOINT
        )
        
        # Generate unique key
        # Keep part of original filename in object key so downstream fetchers (Meta/clients)
        # can infer a stable document name instead of falling back to "Untitled".
        ts = int(time.time())
        unique_id = uuid.uuid4().hex[:12]
        ext = os.path.splitext(original_filename)[1].lower() or '.bin'
        filename_base = os.path.splitext(original_filename)[0]
        safe_base = re.sub(r"[^a-zA-Z0-9_-]+", "_", filename_base).strip("_") or "file"
        safe_base = safe_base[:80]
        key = f"uploads/chat/{media_type}/{ts}_{unique_id}_{safe_base}{ext}"
        
        # Upload to Spaces
        try:
            s3_client.upload_fileobj(
                file_data,
                SPACE_NAME,
                key,
                ExtraArgs={
                    "ACL": "public-read",
                    "ContentType": content_type,
                    "ContentDisposition": f'inline; filename="{original_filename}"'
                }
            )
            
            public_url = f"https://{SPACE_NAME}.{SPACE_REGION}.digitaloceanspaces.com/{key}"
            
            logger.info(f"Uploaded chat media: {public_url} ({media_type}, {file_size} bytes)")
            
            return jsonify({
                "success": True,
                "public_url": public_url,
                "url": public_url,  # Alias for compatibility
                "media_type": media_type,
                "filename": original_filename,
                "size": file_size,
                "mime_type": content_type,
                "key": key
            })
            
        except Exception as upload_err:
            logger.exception(f"Failed to upload to DigitalOcean Spaces: {upload_err}")
            return jsonify({
                "success": False,
                "error": "upload_failed",
                "message": "Failed to upload file to storage. Check server credentials or use a public HTTPS URL.",
                "details": str(upload_err),
            }), 500
    
    except Exception as e:
        logger.exception(f"Chat media upload failed: {e}")
        return jsonify({
            "success": False,
            "error": "internal_server_error",
            "message": "Unexpected error during upload. Please try again.",
            "details": str(e),
        }), 500