"""
Template Acceleration Routes
============================

API endpoints for the WhatsApp Template Approval Acceleration System.

Endpoints:
- POST /templates/validate   - Pre-submit validation + confidence score
- POST /templates/rewrite    - AI rewrite with explain-why
- POST /templates/create     - Submit to Meta with acceleration
- GET  /templates/<id>/status - Poll status with confidence drift
- POST /templates/<id>/sync   - Sync single template from Meta
- GET  /templates/analytics   - Approval time analytics
"""

import logging
import os
from datetime import datetime, timezone
from flask import Blueprint, request, jsonify
from sqlalchemy import func

from shared_models import db
from .models import WhatsAppTemplate, WhatsAppAccount
from .services import WhatsAppService
from .template_validator import validate_template, TemplateValidator, ApprovalPath
from .template_rewriter import rewrite_template, RewriteMode
from .http_rate_limit import rate_limit
from tenant.context import (
    get_current_user,
    resolve_owned_account,
    resolve_owned_workspace,
    user_owns_workspace,
)

logger = logging.getLogger(__name__)


def _resolve_owned_template(template_id):
    """Load a template and verify the authenticated user owns its account's
    workspace. Returns (template, error_response). Fails CLOSED."""
    template = WhatsAppTemplate.query.get(template_id)
    if not template:
        return None, (jsonify({"success": False, "error": "Template not found"}), 404)
    account, err = resolve_owned_account(template.account_id)
    if err:
        return None, err
    return template, None

# Blueprint for template routes
template_bp = Blueprint("template_routes", __name__, url_prefix="/api/whatsapp/templates")


@template_bp.before_request
def _enforce_template_ownership():
    """Blueprint-wide IDOR guard. A template belongs to an account (account_id ->
    workspace). Every ``/<template_id>`` route must be owned by the caller, and any
    account_id/workspace_id supplied to list/create must be owned too. Fails closed.
    """
    if request.method == "OPTIONS":
        return None
    va = request.view_args or {}
    if va.get("template_id") is not None:
        _tpl, err = _resolve_owned_template(va["template_id"])
        if err:
            return err
        return None
    user = get_current_user()
    if not user:
        return jsonify({"success": False, "error": "authentication_required"}), 401
    body = request.get_json(silent=True) if request.is_json else None
    body = body if isinstance(body, dict) else {}
    account_id = request.args.get("account_id") or body.get("account_id")
    workspace_id = request.args.get("workspace_id") or body.get("workspace_id")
    if account_id:
        _acc, err = resolve_owned_account(account_id)
        if err:
            return err
    elif workspace_id:
        if not user_owns_workspace(user, workspace_id):
            return jsonify({"success": False, "error": "forbidden"}), 403
    return None


# ==============================================================
# POST /templates/validate
# ==============================================================

@template_bp.route("/validate", methods=["POST"])
def validate_template_endpoint():
    """
    Pre-submit validation with confidence score.
    
    Request Body:
    {
        "body": "Your order {{1}} has shipped...",
        "category": "UTILITY",
        "header": "Order Update",
        "footer": "Thanks for shopping!",
        "buttons": [],
        "urls": [],
        "language": "en_US",
        "account_id": 12  # Optional - to check if new WABA
    }
    
    Response:
    {
        "confidence_score": 87,
        "risk_flags": ["CTA_IN_UTILITY"],
        "approval_path": "AUTOMATED_SLOW",
        "detected_intent": "UTILITY",
        "suggestions": ["Remove call-to-action verbs"],
        "show_fast_path_badge": false,
        "intent_mismatch": false,
        "intent_mismatch_message": null
    }
    """
    try:
        data = request.get_json() or {}
        
        body = data.get("body", "")
        category = data.get("category", "UTILITY")
        header = data.get("header")
        footer = data.get("footer")
        buttons = data.get("buttons", [])
        urls = data.get("urls", [])
        language = data.get("language", "en_US")
        account_id = data.get("account_id")
        
        # Check if new WABA (< 30 days old). account_id is optional; when supplied
        # it must belong to the authenticated user's workspace (fail closed).
        is_new_waba = False
        if account_id:
            account, err = resolve_owned_account(account_id)
            if err:
                return err
            if account.created_at:
                days_old = (datetime.now(timezone.utc) - account.created_at).days
                is_new_waba = days_old < 30
        
        # Validate
        result = validate_template(
            body=body,
            category=category,
            header=header,
            footer=footer,
            buttons=buttons,
            urls=urls,
            language=language,
            is_new_waba=is_new_waba,
        )
        
        return jsonify(result), 200
        
    except Exception as e:
        logger.exception("Template validation error")
        return jsonify({"error": str(e)}), 500


# ==============================================================
# POST /templates/rewrite
# ==============================================================

@template_bp.route("/rewrite", methods=["POST"])
@rate_limit("whatsapp.template.rewrite")
def rewrite_template_endpoint():
    """
    AI rewrite with explain-why mode.
    
    Request Body:
    {
        "body": "Don't miss our HUGE sale! {{1}}",
        "target_category": "UTILITY",
        "mode": "neutral_utility",  # or "clear_marketing", "strict_authentication"
        "current_category": "MARKETING"  # Optional - for intent warnings
    }
    
    Response:
    {
        "rewritten_body": "Your order update: {{1}}",
        "changes_made": [
            "Removed promotional language ('HUGE sale')",
            "Neutralized tone for transactional use"
        ],
        "preserved_variables": ["{{1}}"],
        "intent_warning": null,
        "success": true,
        "error": null
    }
    """
    try:
        data = request.get_json() or {}
        
        body = data.get("body", "")
        target_category = data.get("target_category", "UTILITY")
        mode = data.get("mode", RewriteMode.NEUTRAL_UTILITY)
        current_category = data.get("current_category")
        
        if not body:
            return jsonify({"error": "Body is required"}), 400
        
        result = rewrite_template(
            body=body,
            target_category=target_category,
            mode=mode,
            current_category=current_category,
        )
        
        return jsonify(result), 200
        
    except Exception as e:
        logger.exception("Template rewrite error")
        return jsonify({"error": str(e)}), 500


# ==============================================================
# POST /templates/create
# ==============================================================

@template_bp.route("/create", methods=["POST"])
@rate_limit("whatsapp.template.create")
def create_template_endpoint():
    """
    Submit template to Meta with acceleration tracking.
    
    - Validates before submission
    - Blocks if intent mismatch detected
    - Tracks submission time and confidence
    - Returns optimistic response immediately
    
    Request Body:
    {
        "account_id": 12,
        "name": "order_shipped",
        "category": "UTILITY",
        "language": "en_US",
        "components": [
            {"type": "BODY", "text": "Your order {{1}} has shipped!"}
        ]
    }
    
    Response:
    {
        "success": true,
        "template": {...},
        "validation": {...},
        "message": "Template submitted for approval"
    }
    """
    try:
        data = request.get_json() or {}
        
        account_id = data.get("account_id")
        name = data.get("name", "").strip()
        category = data.get("category", "UTILITY")
        language = data.get("language", "en_US")
        components = data.get("components", [])
        
        # Validate required fields
        if not account_id:
            return jsonify({"error": "account_id is required"}), 400
        if not name:
            return jsonify({"error": "name is required"}), 400
        if not components:
            return jsonify({"error": "components are required"}), 400

        # Get account (ownership-checked: fails closed on missing user / cross-tenant)
        account, err = resolve_owned_account(account_id)
        if err:
            return err
        
        # Extract body for validation
        body = ""
        header = ""
        footer = ""
        buttons = []
        urls = []
        
        for comp in components:
            comp_type = comp.get("type", "").upper()
            if comp_type == "BODY":
                body = comp.get("text", "")
            elif comp_type == "HEADER":
                header = comp.get("text", "")
            elif comp_type == "FOOTER":
                footer = comp.get("text", "")
            elif comp_type == "BUTTONS":
                buttons = comp.get("buttons", [])
        
        # Pre-validate
        # account.created_at may be tz-naive (stored without tzinfo); treat it as
        # UTC so we don't subtract a naive from an aware datetime (crashes).
        _created = account.created_at
        if _created is not None and _created.tzinfo is None:
            _created = _created.replace(tzinfo=timezone.utc)
        days_old = (datetime.now(timezone.utc) - _created).days if _created else 0
        is_new_waba = days_old < 30
        
        validator = TemplateValidator(is_new_waba=is_new_waba)
        validation = validator.validate(
            body=body,
            category=category,
            header=header,
            footer=footer,
            buttons=buttons,
            urls=urls,
            language=language,
        )
        
        # Block if intent mismatch
        if validation.intent_mismatch:
            return jsonify({
                "success": False,
                "error": "intent_mismatch",
                "message": validation.intent_mismatch_message,
                "validation": validation.to_dict(),
            }), 400
        
        # Block if confidence too low
        if validation.confidence_score < 60:
            return jsonify({
                "success": False,
                "error": "low_confidence",
                "message": "Template confidence too low. Please address the suggestions before submitting.",
                "validation": validation.to_dict(),
            }), 400
        
        # Prepare Data for Service
        import re
        variables = re.findall(r'\{\{(\d+)\}\}', body)
        
        template_data = {
            "name": name,
            "category": category,
            "language": language,
            "components": components,
            "confidence_initial": validation.confidence_score,
            "validation_flags": validation.risk_flags,
            "detected_intent": validation.detected_intent,
            "approval_path": validation.approval_path,
        }
        
        # Use Service to Create Draft
        service = WhatsAppService()
        result, success = service.create_draft_template(account_id, template_data)
        
        if not success:
             return jsonify({"error": result.get("error", "Failed to create draft")}), 400
        
        return jsonify({
            "success": True,
            "template": result,
            "validation": validation.to_dict(),
            "message": "Template draft created. Ready for submission.",
            "show_fast_path_badge": validation.show_fast_path_badge,
        }), 201
        
    except Exception as e:
        logger.exception("Template creation error")
        db.session.rollback()
        return jsonify({"error": str(e)}), 500


# ==============================================================
# PUT /templates/<id> - Update Draft
# ==============================================================


# ==============================================================
# GET /templates/<id> - Get Template Details
# ==============================================================

@template_bp.route("/<int:template_id>", methods=["GET"])
def get_template_details(template_id):
    """Get template details by ID (Local DB)."""
    try:
        template, err = _resolve_owned_template(template_id)
        if err:
            return err

        return jsonify({"success": True, "template": template.to_dict()}), 200
        
    except Exception as e:
        logger.exception("Error fetching template details")
        return jsonify({"error": str(e)}), 500


@template_bp.route("/<int:template_id>", methods=["PUT"])

def update_template_draft(template_id):
    """Update a draft template before submission."""
    try:
        _, err = _resolve_owned_template(template_id)
        if err:
            return err

        data = request.get_json() or {}

        service = WhatsAppService()
        result, success = service.update_draft_template(template_id, data)
        
        if not success:
            return jsonify(result), 400
            
        return jsonify({"success": True, "template": result}), 200
        
    except Exception as e:
        logger.exception("Template update error")
        return jsonify({"error": str(e)}), 500


# ==============================================================
# POST /templates/<id>/submit - Submit to Meta
# ==============================================================

@template_bp.route("/<int:template_id>/submit", methods=["POST"])
@rate_limit("whatsapp.template.submit")
def submit_template_endpoint(template_id):
    """Submit a local draft to Meta."""
    try:
        _, err = _resolve_owned_template(template_id)
        if err:
            return err

        service = WhatsAppService()
        result, success = service.submit_template_to_meta(template_id)
        
        if not success:
            return jsonify(result), 400
            
        return jsonify({"success": True, "template": result, "message": "Submitted to Meta"}), 200
        
    except Exception as e:
        logger.exception("Template submission error")
        return jsonify({"error": str(e)}), 500


# ==============================================================
# POST /templates/<id>/archive - Soft Delete
# ==============================================================

@template_bp.route("/<int:template_id>/archive", methods=["POST"])
def archive_template_endpoint(template_id):
    """Archive a template (Soft delete)."""
    try:
        _, err = _resolve_owned_template(template_id)
        if err:
            return err

        service = WhatsAppService()
        result, success = service.archive_template(template_id)
        
        if not success:
            return jsonify(result), 400
            
        return jsonify({"success": True, "template": result}), 200
        
    except Exception as e:
        logger.exception("Template archive error")
        return jsonify({"error": str(e)}), 500


# ==============================================================
# POST /templates/<id>/duplicate - Clone
# ==============================================================

@template_bp.route("/<int:template_id>/duplicate", methods=["POST"])
def duplicate_template_endpoint(template_id):
    """Duplicate a template to a new draft."""
    try:
        _, err = _resolve_owned_template(template_id)
        if err:
            return err

        data = request.get_json() or {}
        new_name = data.get("name")

        service = WhatsAppService()
        result, success = service.duplicate_template(template_id, new_name)
        
        if not success:
            return jsonify(result), 400
            
        return jsonify({"success": True, "template": result}), 201
        
    except Exception as e:
        logger.exception("Template duplicate error")
        return jsonify({"error": str(e)}), 500


# ==============================================================
# DELETE /templates/<id> - Permanent Delete
# ==============================================================

@template_bp.route("/<int:template_id>", methods=["DELETE"])
def delete_template_endpoint(template_id):
    """Permanently delete a template."""
    try:
        _, err = _resolve_owned_template(template_id)
        if err:
            return err

        logger.info("[template_delete] Deleting template %s", template_id)
        service = WhatsAppService()
        result, success = service.delete_template(template_id)

        if not success:
            logger.warning("[template_delete] Failed: %s", result)
            if "success" not in result:
                result["success"] = False
            return jsonify(result), 400

        if "success" not in result:
            result["success"] = True
        logger.info("[template_delete] Deleted template %s", template_id)
        return jsonify(result), 200

    except Exception as e:
        logger.exception("Template delete error")
        return jsonify({"success": False, "error": str(e)}), 500

# ==============================================================
# POST /templates/<id>/resubmit - Update Meta Template
# ==============================================================

@template_bp.route("/<int:template_id>/resubmit", methods=["POST"])
def resubmit_template_endpoint(template_id):
    """Resubmit an existing template to Meta (Edit)."""
    try:
        _, err = _resolve_owned_template(template_id)
        if err:
            return err

        service = WhatsAppService()
        result, success = service.edit_meta_template(template_id)
        
        if not success:
            return jsonify(result), 400
            
        return jsonify({"success": True, "template": result, "message": "Resubmitted to Meta"}), 200
        
    except Exception as e:
        logger.exception("Template resubmission error")
        return jsonify({"error": str(e)}), 500


# ==============================================================
# POST /templates/upload_media - Resumable Upload
# ==============================================================

@template_bp.route("/upload_media", methods=["POST"])
def upload_template_media():
    """Upload media for template header (creates handle)."""
    try:
        if 'file' not in request.files:
            return jsonify({"error": "No file part"}), 400
            
        file = request.files['file']
        if file.filename == '':
            return jsonify({"error": "No selected file"}), 400

        # Get account-specific access token (ownership-checked)
        account_id = request.form.get('account_id')
        access_token = None
        if account_id:
            account, err = resolve_owned_account(account_id)
            if err:
                return err
            access_token = account.get_access_token()

        if not access_token:
            access_token = os.getenv("WHATSAPP_ACCESS_TOKEN")

        if not access_token:
            return jsonify({"success": False, "error": "No access token available. Please reconnect your WhatsApp account."}), 400

        # Save to temp file strictly for upload processing
        import tempfile
        from werkzeug.utils import secure_filename
        
        filename = secure_filename(file.filename)
        temp_dir = tempfile.gettempdir()
        temp_path = os.path.join(temp_dir, filename)
        file.save(temp_path)
        
        try:
            # Determine mime type
            import mimetypes
            mime_type, _ = mimetypes.guess_type(temp_path)
            if not mime_type:
                mime_type = 'application/octet-stream'
                
            service = WhatsAppService()
            logger.info("Starting resumable upload: file=%s, mime=%s, account_id=%s, token_set=%s",
                        filename, mime_type, account_id, bool(access_token))
            handle = service.resumable_media_upload(temp_path, mime_type, access_token=access_token)
            
            if not handle:
                return jsonify({"success": False, "error": "Upload failed. Meta did not return a media handle."}), 500

            return jsonify({"success": True, "handle": handle}), 200
            
        finally:
            # Clean up temp file
            if os.path.exists(temp_path):
                os.remove(temp_path)
        
    except Exception as e:
        logger.exception("Media upload error")
        return jsonify({"error": str(e)}), 500


@template_bp.route("/upload_media_url", methods=["POST"])
def upload_template_media_url():
    """Download media from a URL, upload to Meta, and return a template media handle."""
    try:
        import mimetypes
        import tempfile
        from urllib.parse import urlparse
        import requests as http_requests

        data = request.get_json() or {}
        media_url = (data.get("url") or "").strip()
        account_id = data.get("account_id")
        media_type = str(data.get("media_type") or "image").lower()

        if not media_url:
            return jsonify({"success": False, "error": "url is required"}), 400
        if not (media_url.startswith("https://") or media_url.startswith("http://")):
            return jsonify({"success": False, "error": "URL must start with http:// or https://"}), 400

        # Get account-specific access token (ownership-checked)
        access_token = None
        if account_id:
            account, err = resolve_owned_account(account_id)
            if err:
                return err
            access_token = account.get_access_token()

        if not access_token:
            access_token = os.getenv("WHATSAPP_ACCESS_TOKEN")

        if not access_token:
            return jsonify({"success": False, "error": "No access token available. Please reconnect your WhatsApp account."}), 400

        # Download remote file
        resp = http_requests.get(media_url, timeout=30, stream=True, allow_redirects=True)
        if not resp.ok:
            return jsonify({"success": False, "error": f"Failed to download media (HTTP {resp.status_code})"}), 400

        content_type = (resp.headers.get("Content-Type") or "application/octet-stream").split(";")[0].strip().lower()

        expected_by_type = {
            "image": {"image/jpeg", "image/jpg", "image/png", "image/webp", "image/gif"},
            "video": {"video/mp4", "video/quicktime", "video/3gpp", "video/avi", "video/mpeg"},
            "document": {
                "application/pdf",
                "text/plain",
                "application/msword",
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            },
        }

        if media_type in expected_by_type and content_type not in expected_by_type[media_type]:
            logger.warning("URL media type mismatch: requested=%s, content_type=%s, url=%s", media_type, content_type, media_url)

        # Create temp file with best-effort extension
        parsed = urlparse(media_url)
        ext_from_url = os.path.splitext(parsed.path)[1]
        ext_from_mime = mimetypes.guess_extension(content_type or "") or ""
        suffix = ext_from_url or ext_from_mime or ".bin"

        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as temp_file:
            temp_path = temp_file.name
            total_written = 0
            max_bytes = 100 * 1024 * 1024

            for chunk in resp.iter_content(chunk_size=1024 * 1024):
                if not chunk:
                    continue
                total_written += len(chunk)
                if total_written > max_bytes:
                    return jsonify({"success": False, "error": "Downloaded file exceeds 100MB limit"}), 400
                temp_file.write(chunk)

        try:
            # Recalculate mime type from file if header is missing/generic
            if content_type in {"", "application/octet-stream"}:
                guessed, _ = mimetypes.guess_type(temp_path)
                if guessed:
                    content_type = guessed

            service = WhatsAppService()
            handle = service.resumable_media_upload(temp_path, content_type or "application/octet-stream", access_token=access_token)

            if not handle:
                return jsonify({"success": False, "error": "Upload failed. Meta did not return a media handle."}), 500

            return jsonify({"success": True, "handle": handle, "content_type": content_type}), 200
        finally:
            try:
                if os.path.exists(temp_path):
                    os.remove(temp_path)
            except Exception:
                pass

    except Exception as e:
        logger.exception("URL media upload error")
        return jsonify({"success": False, "error": str(e)}), 500


# ==============================================================
# GET /templates - List templates with sorting
# ==============================================================

@template_bp.route("/", methods=["GET"])
@template_bp.route("/", methods=["GET"])
def get_templates():
    """
    List templates with sorting and filtering.
    
    Query Params:
    - account_id: Filter by account
    - workspace_id: Filter by workspace (resolves to account)
    - status: Filter by status (e.g. APPROVED, PENDING, REJECTED)
    - sort: date_desc (default), date_asc, name_asc, name_desc, status
    - limit: Max items (optional, no limit by default)
    
    Response:
    [
        {...template...},
        ...
    ]
    """
    try:
        from .models import WhatsAppTemplate, WhatsAppAccount

        account_id = request.args.get("account_id")
        workspace_id = request.args.get("workspace_id")
        status_filter = request.args.get("status")
        sort_option = request.args.get("sort", "date_desc")
        limit = request.args.get("limit", type=int)  # No default cap — load all

        # Fail CLOSED: require an authenticated user and scope to workspaces they own.
        user = get_current_user()
        if not user:
            return jsonify({"success": False, "error": "authentication_required"}), 401

        # If an account_id is supplied, verify ownership of its workspace.
        if account_id:
            _account, err = resolve_owned_account(account_id)
            if err:
                return err
            account_id = _account.id
        else:
            # Resolve/verify the workspace (falls back to the user's own workspace).
            workspace, err = resolve_owned_workspace(user, workspace_id)
            if err:
                return err
            account = WhatsAppAccount.query.filter_by(
                workspace_id=str(workspace.id), is_active=True
            ).first()
            if not account:
                # No account for the owned workspace -> no templates to show.
                return jsonify({"success": True, "templates": []}), 200
            account_id = account.id

        query = WhatsAppTemplate.query

        if account_id:
            query = query.filter_by(account_id=account_id)

        # Filter by status if provided (e.g. APPROVED, PENDING, REJECTED)
        if status_filter:
            query = query.filter(WhatsAppTemplate.status == status_filter.upper())
            
        # Sorting
        if sort_option == "date_asc":
            query = query.order_by(WhatsAppTemplate.created_at.asc())
        elif sort_option == "name_asc":
            query = query.order_by(WhatsAppTemplate.name.asc())
        elif sort_option == "name_desc":
            query = query.order_by(WhatsAppTemplate.name.desc())
        elif sort_option == "status":
            query = query.order_by(WhatsAppTemplate.status.asc())
        else: # date_desc / default
            query = query.order_by(WhatsAppTemplate.created_at.desc())
        
        # Only apply limit if explicitly requested
        if limit and limit > 0:
            templates = query.limit(limit).all()
        else:
            templates = query.all()
        
        return jsonify({"success": True, "templates": [t.to_dict() for t in templates]}), 200
        
    except Exception as e:
        logger.exception("Error listing templates")
        # Check for DB column error
        if "no such column" in str(e).lower():
            return jsonify({
                "error": "Database schema mismatch. Please run migration to add 'quality_score' column.",
                "details": str(e)
            }), 500
        return jsonify({"error": str(e)}), 500


# ==============================================================
# GET /templates/<id>/status
# ==============================================================

@template_bp.route("/<int:template_id>/status", methods=["GET"])
def get_template_status(template_id):
    """
    Poll template status with confidence drift tracking.
    
    Response:
    {
        "status": "PENDING",
        "confidence_post_submit": 85,
        "approval_duration_seconds": null,
        "approval_path": "AUTOMATED_SLOW",
        "message": "Under automated review"
    }
    """
    try:
        template, err = _resolve_owned_template(template_id)
        if err:
            return err

        # Calculate how long it's been pending
        pending_seconds = None
        if template.submitted_at and template.status == "PENDING":
            pending_seconds = (datetime.now(timezone.utc) - template.submitted_at).total_seconds()
        
        # Update confidence_post_submit if still pending
        if template.status == "PENDING" and pending_seconds:
            # Recalculate confidence based on pending duration
            if pending_seconds > 30 and template.confidence_initial:
                # Slightly lower confidence if taking longer than expected
                drift = min(15, int(pending_seconds / 30) * 5)
                template.confidence_post_submit = max(50, template.confidence_initial - drift)
                db.session.commit()
        
        # Build response message based on status and duration
        if template.status == "APPROVED":
            message = "Template approved! Ready to send."
        elif template.status == "REJECTED":
            message = f"Template rejected: {template.rejection_reason or 'Unknown reason'}"
        elif pending_seconds and pending_seconds > 60:
            message = "Meta is performing extended automated checks. This is normal for new or modified templates."
        elif pending_seconds and pending_seconds > 30:
            message = "Under automated review (usually completes shortly)"
        else:
            message = "Running automated approval checks..."
        
        return jsonify({
            "id": template.id,
            "status": template.status,
            "confidence_initial": template.confidence_initial,
            "confidence_post_submit": template.confidence_post_submit,
            "pending_seconds": int(pending_seconds) if pending_seconds else None,
            "approval_duration_seconds": template.approval_duration_seconds,
            "approval_path": template.approval_path,
            "approval_outcome_reason": template.approval_outcome_reason,
            "message": message,
            "submitted_at": template.submitted_at.isoformat() + "Z" if template.submitted_at else None,
            "approved_at": template.approved_at.isoformat() + "Z" if template.approved_at else None,
        }), 200
        
    except Exception as e:
        logger.exception("Template status error")
        return jsonify({"error": str(e)}), 500


# ==============================================================
# POST /templates/<id>/sync - Sync single template from Meta
# ==============================================================

@template_bp.route("/<int:template_id>/sync", methods=["POST"])
def sync_single_template(template_id):
    """
    Sync a single template from Meta to update its status, quality score, etc.
    
    This is more efficient than syncing all templates when you only need
    to refresh one template's status.
    
    Strategy:
    1. Look up template by meta_template_id (direct API call - fastest)
    2. If no meta_template_id, fall back to name+language search
    
    Response:
    {
        "success": true,
        "template": {...},
        "status_changed": true,
        "old_status": "PENDING",
        "new_status": "APPROVED",
        "quality_score": "GREEN",
        "last_synced_at": "2026-01-27T12:00:00Z"
    }
    
    Error Response (404):
    {
        "success": false,
        "error": "Template not found on Meta. It may have been deleted.",
        "deleted_on_meta": true
    }
    """
    try:
        # Get the template and verify ownership of its account's workspace.
        template, err = _resolve_owned_template(template_id)
        if err:
            return err

        # Get account and token (already verified owned by resolve above).
        account = WhatsAppAccount.query.get(template.account_id)
        if not account:
            return jsonify({"success": False, "error": "Account not found"}), 404
        
        access_token = account.get_access_token()
        if not access_token:
            return jsonify({"success": False, "error": "No access token available"}), 400
        
        # Use the WhatsAppService to sync
        from .services import WhatsAppService
        
        service = WhatsAppService(
            access_token=access_token,
            phone_number_id=account.phone_number_id,
            waba_id=account.waba_id
        )
        
        result = service.sync_single_template(template_id)
        
        if not result.get("success"):
            # Check if it's a "not found on Meta" case
            if result.get("deleted_on_meta"):
                return jsonify(result), 404
            return jsonify(result), 400
        
        return jsonify(result), 200
        
    except Exception as e:
        logger.exception(f"Single template sync error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


# ==============================================================
# GET /templates/analytics
# ==============================================================

@template_bp.route("/analytics", methods=["GET"])
def get_template_analytics():
    """
    Approval time analytics for tuning validation rules.
    
    Query Params:
    - account_id (optional): Filter by account
    - days (optional): Days to look back (default 30)
    
    Response:
    {
        "total_templates": 50,
        "approval_rate": 0.92,
        "avg_approval_seconds": 45,
        "by_category": {...},
        "by_outcome_reason": {...},
        "fast_approvals": 35,
        "slow_approvals": 10
    }
    """
    try:
        account_id = request.args.get("account_id", type=int)
        days = request.args.get("days", 30, type=int)

        # Fail CLOSED: require an authenticated user and scope to owned accounts.
        user = get_current_user()
        if not user:
            return jsonify({"success": False, "error": "authentication_required"}), 401

        # Base query
        query = WhatsAppTemplate.query

        if account_id:
            # Verify ownership of the requested account's workspace.
            _account, err = resolve_owned_account(account_id)
            if err:
                return err
            query = query.filter_by(account_id=account_id)
        else:
            # Scope to the account ids in the workspaces this user owns.
            from shared_models import Workspace
            owned_ws_ids = [
                str(w.id) for w in Workspace.query.filter_by(user_id=user.id).all()
            ]
            owned_account_ids = [
                a.id
                for a in WhatsAppAccount.query.filter(
                    WhatsAppAccount.workspace_id.in_(owned_ws_ids)
                ).all()
            ] if owned_ws_ids else []
            if not owned_account_ids:
                return jsonify({
                    "total_templates": 0,
                    "message": "No templates found in the specified period",
                }), 200
            query = query.filter(WhatsAppTemplate.account_id.in_(owned_account_ids))

        # Date filter
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        query = query.filter(WhatsAppTemplate.created_at >= cutoff)
        
        templates = query.all()
        
        if not templates:
            return jsonify({
                "total_templates": 0,
                "message": "No templates found in the specified period"
            }), 200
        
        # Calculate metrics
        total = len(templates)
        approved = [t for t in templates if t.status == "APPROVED"]
        rejected = [t for t in templates if t.status == "REJECTED"]
        pending = [t for t in templates if t.status == "PENDING"]
        
        approval_rate = len(approved) / total if total > 0 else 0
        
        # Average approval time
        approval_times = [t.approval_duration_seconds for t in approved if t.approval_duration_seconds]
        avg_approval_seconds = sum(approval_times) / len(approval_times) if approval_times else None
        
        # Fast vs slow approvals
        fast_approvals = len([t for t in approved if t.approval_duration_seconds and t.approval_duration_seconds <= 60])
        slow_approvals = len([t for t in approved if t.approval_duration_seconds and t.approval_duration_seconds > 120])
        
        # By category
        by_category = {}
        for cat in ["UTILITY", "MARKETING", "AUTHENTICATION"]:
            cat_templates = [t for t in templates if t.category == cat]
            cat_approved = [t for t in cat_templates if t.status == "APPROVED"]
            by_category[cat] = {
                "total": len(cat_templates),
                "approved": len(cat_approved),
                "approval_rate": len(cat_approved) / len(cat_templates) if cat_templates else 0,
            }
        
        # By outcome reason
        by_outcome_reason = {}
        for t in templates:
            reason = t.approval_outcome_reason or "UNKNOWN"
            if reason not in by_outcome_reason:
                by_outcome_reason[reason] = 0
            by_outcome_reason[reason] += 1
        
        # Median approval time
        median_approval_seconds = None
        if approval_times:
            sorted_times = sorted(approval_times)
            mid = len(sorted_times) // 2
            if len(sorted_times) % 2 == 0:
                median_approval_seconds = (sorted_times[mid - 1] + sorted_times[mid]) / 2
            else:
                median_approval_seconds = sorted_times[mid]
        
        # Percentile breakdown (sales gold)
        percentile_breakdown = {
            "under_10s": 0,
            "under_30s": 0,
            "under_2min": 0,
            "over_2min": 0,
        }
        for t in approved:
            if t.approval_duration_seconds:
                if t.approval_duration_seconds < 10:
                    percentile_breakdown["under_10s"] += 1
                elif t.approval_duration_seconds < 30:
                    percentile_breakdown["under_30s"] += 1
                elif t.approval_duration_seconds < 120:
                    percentile_breakdown["under_2min"] += 1
                else:
                    percentile_breakdown["over_2min"] += 1
        
        # Calculate percentages
        approved_count = len(approved)
        percentile_percent = {}
        for key, count in percentile_breakdown.items():
            percentile_percent[key] = round((count / approved_count * 100), 1) if approved_count > 0 else 0
        
        return jsonify({
            "total_templates": total,
            "approved": len(approved),
            "rejected": len(rejected),
            "pending": len(pending),
            "approval_rate": round(approval_rate, 2),
            "avg_approval_seconds": int(avg_approval_seconds) if avg_approval_seconds else None,
            "median_approval_seconds": int(median_approval_seconds) if median_approval_seconds else None,
            "fast_approvals": fast_approvals,
            "slow_approvals": slow_approvals,
            "percentile_breakdown": percentile_breakdown,
            "percentile_percent": percentile_percent,
            "by_category": by_category,
            "by_outcome_reason": by_outcome_reason,
        }), 200
        
    except Exception as e:
        logger.exception("Template analytics error")
        return jsonify({"error": str(e)}), 500


# Import timedelta for analytics
from datetime import timedelta
