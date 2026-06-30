"""
Flow Routes - WhatsApp Flows API Endpoints
==========================================

Provides REST API for managing WhatsApp Flows.

Endpoints:
    POST   /api/whatsapp/flows              - Create draft flow
    GET    /api/whatsapp/flows              - List flows for account
    GET    /api/whatsapp/flows/{id}         - Get flow details
    PUT    /api/whatsapp/flows/{id}         - Update draft flow
    DELETE /api/whatsapp/flows/{id}         - Delete draft flow
    POST   /api/whatsapp/flows/{id}/publish - Publish to Meta
    POST   /api/whatsapp/flows/{id}/clone   - Clone published flow
    POST   /api/whatsapp/flows/validate     - Validate flow JSON
"""

import logging
import os
from datetime import datetime, timezone
from flask import Blueprint, request, jsonify, g
import requests as http_requests

logger = logging.getLogger(__name__)

from shared_models import db, Workspace, User
from .models import WhatsAppFlow, WhatsAppAccount
from .flow_validator import validate_flow_json, generate_sample_flow
from .flow_access import (
    require_flow_access,
    require_account_access,
    validate_flow_account_match,
    normalize_flow_category,
)
from .token_helper import get_account_with_token
from subscription.service import check_flow_limit

# Create blueprint
flow_bp = Blueprint("flows", __name__, url_prefix="/api/whatsapp/flows")


def _flow_uses_data_endpoint(flow_json: dict) -> bool:
    """Return True when the flow JSON expects Meta to call our data endpoint."""
    if not isinstance(flow_json, dict):
        return False

    if flow_json.get("data_api_version"):
        return True

    for screen in flow_json.get("screens", []) or []:
        if isinstance(screen, dict) and screen.get("data_api_version"):
            return True

    return False


def _meta_graph_error(error_data: dict) -> dict:
    """Return the nested ``error`` object from a Graph API JSON body."""
    return (error_data or {}).get("error") or {}


def _safe_response_json(response: http_requests.Response) -> dict:
    """Parse a Graph API response body without raising on empty/invalid JSON."""
    try:
        return response.json()
    except Exception:
        return {}


def _response_for_meta_flow_create_error(error_data: dict):
    """
    Map Meta ``POST /{waba-id}/flows`` failures to Sociovia error codes.

    Meta often uses ``code`` 100 for unrelated validation issues; do **not**
    treat every 100 as missing Flows capability.
    """
    err = _meta_graph_error(error_data)
    msg_l = (err.get("message") or "").lower()
    user_msg = err.get("error_user_msg") or err.get("message") or "Unknown error"
    user_title_l = (err.get("error_user_title") or "").lower()
    user_l = (err.get("error_user_msg") or "").lower()
    code = err.get("code")
    sub = err.get("error_subcode")

    # Flow name collides with another flow on the same WABA (very common).
    if (
        sub == 4016019
        or ("unique" in user_l and "name" in user_l)
        or "not unique" in user_title_l
    ):
        return (
            {
                "success": False,
                "error": "flow_name_not_unique",
                "message": err.get("error_user_msg")
                or (
                    "A flow with this name already exists on this WhatsApp Business Account. "
                    "Rename the flow in Sociovia or delete/rename the duplicate in WhatsApp Manager."
                ),
                "meta_error": err,
            },
            400,
        )

    if code == 139000 or "integrity" in msg_l:
        return (
            {
                "success": False,
                "error": "business_not_verified",
                "message": "Business verification required. Complete verification in Meta Business Manager to publish flows.",
                "action_url": "https://business.facebook.com/settings/whatsapp-business-accounts",
                "meta_error": err,
            },
            400,
        )

    # Actual Flows product / permission issues (keep narrow to avoid false positives).
    if (
        "flow" in msg_l
        and (
            "capability" in msg_l
            or "does not have access" in msg_l
            or ("permission" in msg_l and "manage" in msg_l)
        )
    ) or "flows feature" in msg_l:
        return (
            {
                "success": False,
                "error": "flows_not_enabled",
                "message": "Flows may not be enabled for this WhatsApp Business Account, or the token lacks required Flow permissions. Check Meta Business Manager and reconnect the app.",
                "meta_error": err,
            },
            400,
        )

    if "token" in msg_l or "session has expired" in msg_l:
        return (
            {
                "success": False,
                "error": "token_invalid",
                "message": "Access token is invalid or expired. Please reconnect your WhatsApp Business Account.",
                "meta_error": err,
            },
            400,
        )

    return (
        {
            "success": False,
            "error": "meta_api_error",
            "message": f"Failed to create flow on Meta: {err.get('message', user_msg)}",
            "meta_error": err,
        },
        400,
    )


def _resolve_public_base_url() -> str:
    """Prefer the active request host so live tunnel publishes use the reachable URL."""
    candidates = [
        (request.host_url or "").strip(),
        (os.getenv("APP_BASE_URL") or "").strip(),
        (os.getenv("PUBLIC_APP_BASE_URL") or "").strip(),
    ]

    for candidate in candidates:
        if candidate.lower().startswith(("http://", "https://")):
            return candidate.rstrip("/")

    return ""


_FIELD_COMPONENT_TYPES = {
    "TextInput",
    "TextArea",
    "Dropdown",
    "RadioButtonsGroup",
    "CheckboxGroup",
    "DatePicker",
}


def _valid_flow_field_name(name: object) -> bool:
    if not isinstance(name, str) or not name:
        return False
    first = name[0]
    return (first.isalpha() or first == "_") and all(
        ch.isalnum() or ch == "_" for ch in name
    )


def _field_data_type(component: dict) -> str:
    return "array" if component.get("type") == "CheckboxGroup" else "string"


def _collect_screen_fields(screen: dict) -> dict:
    fields = {}
    children = ((screen.get("layout") or {}).get("children") or [])
    for component in children:
        if not isinstance(component, dict):
            continue
        if component.get("type") in _FIELD_COMPONENT_TYPES:
            name = component.get("name")
            if _valid_flow_field_name(name):
                fields[name] = _field_data_type(component)
    return fields


def normalize_flow_submission_payloads(flow_json: dict) -> dict:
    """
    Ensure Flow navigation/complete actions carry collected form values forward.

    Without a Footer payload, Meta can send an nfm_reply that only contains
    {"flow_token": "unused"}, leaving the inbox with no submitted fields.
    """
    if not isinstance(flow_json, dict):
        return flow_json

    screens = flow_json.get("screens")
    if not isinstance(screens, list):
        return flow_json

    prior_fields = {}
    for screen in screens:
        if not isinstance(screen, dict):
            continue

        if prior_fields:
            existing_data = screen.get("data") if isinstance(screen.get("data"), dict) else {}
            data = dict(existing_data)
            for field_name, field_type in prior_fields.items():
                data.setdefault(
                    field_name,
                    {"type": field_type, "__example__": "Example"},
                )
            screen["data"] = data

        current_fields = _collect_screen_fields(screen)
        children = ((screen.get("layout") or {}).get("children") or [])

        for component in children:
            if not isinstance(component, dict) or component.get("type") != "Footer":
                continue

            action = component.get("on-click-action")
            if not isinstance(action, dict):
                continue

            action_name = str(action.get("name") or "").lower()
            if action_name not in {"navigate", "complete"}:
                continue

            payload = action.get("payload") if isinstance(action.get("payload"), dict) else {}
            payload = dict(payload)

            for field_name in prior_fields:
                payload.setdefault(field_name, f"${{data.{field_name}}}")
            for field_name in current_fields:
                payload.setdefault(field_name, f"${{form.{field_name}}}")

            if payload:
                action["payload"] = payload

        prior_fields.update(current_fields)

    return flow_json


# ============================================================
# POST /api/whatsapp/flows - Create Draft Flow
# ============================================================

@flow_bp.route("", methods=["POST"])
def create_flow():
    """
    Create a new draft flow.
    
    Request body:
    {
        "account_id": 1,
        "name": "Lead Capture Form",
        "category": "LEAD_GEN",
        "flow_json": {...},      // Optional, can use template
        "entry_screen_id": "WELCOME"
    }
    """
    data = request.get_json() or {}
    
    # Required fields
    account_id = data.get("account_id")
    name = data.get("name")
    category = normalize_flow_category(data.get("category", "OTHER"))
    
    if not account_id:
        return jsonify({"success": False, "error": "account_id is required"}), 400
    if not name:
        return jsonify({"success": False, "error": "name is required"}), 400
    
    # Validate account exists
    account = WhatsAppAccount.query.get(account_id)
    if not account:
        return jsonify({"success": False, "error": "Account not found"}), 404
        
    # Check Subscription Limit
    workspace_id = getattr(account, "workspace_id", None)
    if workspace_id:
        try:
             wid = int(workspace_id)
             workspace = Workspace.query.get(wid)
             if workspace:
                 user = User.query.get(workspace.user_id)
                 if user:
                     allowed, current, limit = check_flow_limit(user, wid)
                     if not allowed:
                         return jsonify({
                             "success": False, 
                             "error": "flow_limit_exceeded", 
                             "limit": limit,
                             "current": current,
                             "plan": user.plan,
                             "message": "Limit reached. Please upgrade your plan."
                         }), 403
        except Exception:
             pass
    
    # Get or generate flow JSON
    flow_json = data.get("flow_json")
    if not flow_json:
        # Generate sample flow based on category
        flow_json = generate_sample_flow(category)
    flow_json = normalize_flow_submission_payloads(flow_json)
    
    # Determine entry screen
    entry_screen_id = data.get("entry_screen_id")
    if not entry_screen_id:
        screens = flow_json.get("screens", [])
        entry_screen_id = screens[0].get("id") if screens else "WELCOME"
    
    # Check for duplicate name (same account, same version)
    existing = WhatsAppFlow.query.filter_by(
        account_id=account_id,
        name=name,
        flow_version=1
    ).first()
    
    if existing:
        return jsonify({
            "success": False, 
            "error": f"Flow with name '{name}' already exists"
        }), 409
    
    # Create flow
    flow = WhatsAppFlow(
        account_id=account_id,
        name=name,
        category=category.upper(),
        flow_version=1,
        flow_json=flow_json,
        schema_version=str((flow_json or {}).get("version") or "5.0"),
        entry_screen_id=entry_screen_id,
        status="DRAFT",
    )
    
    db.session.add(flow)
    db.session.commit()
    
    return jsonify({
        "success": True,
        "flow": flow.to_dict(),
        "message": "Draft flow created successfully"
    }), 201


# ============================================================
# GET /api/whatsapp/flows - List Flows
# ============================================================

@flow_bp.route("", methods=["GET"])
def list_flows():
    """
    List flows for an account.
    
    Query params:
    - account_id (required)
    - status (optional): DRAFT, PUBLISHED, DEPRECATED
    - category (optional): LEAD_GEN, SURVEY, etc.
    """
    account_id = request.args.get("account_id", type=int)
    status = request.args.get("status")
    category = request.args.get("category")
    
    if not account_id:
        return jsonify({"success": False, "error": "account_id is required"}), 400
    
    query = WhatsAppFlow.query.filter_by(account_id=account_id)
    
    if status:
        query = query.filter_by(status=status.upper())
    if category:
        query = query.filter_by(category=category.upper())
    
    # Order by name, then version descending
    flows = query.order_by(WhatsAppFlow.name, WhatsAppFlow.flow_version.desc()).all()
    
    return jsonify({
        "success": True,
        "flows": [f.to_dict() for f in flows],
        "count": len(flows)
    })


# ============================================================
# GET /api/whatsapp/flows/{id} - Get Flow Details
# ============================================================

@flow_bp.route("/<int:flow_id>", methods=["GET"])
@require_flow_access
def get_flow(flow_id: int):
    """Get flow details by ID."""
    # Flow is attached to g by the decorator
    flow = g.flow
    
    return jsonify({
        "success": True,
        "flow": flow.to_dict()
    })


# ============================================================
# PUT /api/whatsapp/flows/{id} - Update Draft Flow
# ============================================================

@flow_bp.route("/<int:flow_id>", methods=["PUT"])
@require_flow_access
def update_flow(flow_id: int):
    """
    Update a draft flow.
    
    Note: Published flows cannot be edited. Use clone instead.
    """
    # Flow is attached to g by the decorator
    flow = g.flow
    
    if flow.status != "DRAFT":
        return jsonify({
            "success": False, 
            "error": f"Cannot edit {flow.status} flow. Clone to create a new version."
        }), 400
    
    data = request.get_json() or {}
    
    # Update allowed fields
    if "name" in data:
        flow.name = data["name"]
    if "category" in data:
        flow.category = normalize_flow_category(data["category"])
    if "flow_json" in data:
        normalized_flow_json = normalize_flow_submission_payloads(data["flow_json"])
        flow.flow_json = normalized_flow_json
        flow.schema_version = str((normalized_flow_json or {}).get("version") or flow.schema_version)
    if "entry_screen_id" in data:
        flow.entry_screen_id = data["entry_screen_id"]
    
    flow.updated_at = datetime.now(timezone.utc)
    db.session.commit()
    
    return jsonify({
        "success": True,
        "flow": flow.to_dict(),
        "message": "Flow updated successfully"
    })


# ============================================================
# DELETE /api/whatsapp/flows/{id} - Delete Draft Flow
# ============================================================

@flow_bp.route("/<int:flow_id>", methods=["DELETE"])
@require_flow_access
def delete_flow(flow_id: int):
    """
    Delete a draft flow.
    
    Note: Published flows cannot be deleted, only deprecated.
    """
    # Flow is attached to g by the decorator
    flow = g.flow
    
    if flow.status == "PUBLISHED":
        return jsonify({
            "success": False,
            "error": "Cannot delete published flow. Use deprecate instead."
        }), 400
    
    db.session.delete(flow)
    db.session.commit()
    
    return jsonify({
        "success": True,
        "message": "Flow deleted successfully"
    })


# ============================================================
# POST /api/whatsapp/flows/{id}/publish - Publish to Meta
# ============================================================

@flow_bp.route("/<int:flow_id>/publish", methods=["POST"])
@require_flow_access
def publish_flow(flow_id: int):
    """
    Publish a draft flow to Meta.
    
    Steps:
    1. Validate flow JSON strictly (v7.3 schema)
    2. Check WABA capability and token validity
    3. POST to Meta Graph API (create → upload JSON → publish)
    4. Store meta_flow_id
    5. Mark as PUBLISHED (immutable)
    
    Returns specific error types for UI feedback:
    - token_expired / token_invalid: Token needs refresh
    - business_not_verified: Business verification required
    - flows_not_enabled: Flows product/permission issues (narrow detection)
    - flow_name_not_unique: Meta subcode 4016019 — duplicate flow name on WABA
    - validation_failed: Flow JSON validation errors
    - meta_api_error: Other Meta Graph errors (see ``meta_error``)
    """
    flow = g.flow

    if not flow.can_publish():
        return jsonify({
            "success": False,
            "error": "flow_not_publishable",
            "message": f"Flow cannot be published. Current status: {flow.status}",
            "status": flow.status
        }), 400

    flow.flow_json = normalize_flow_submission_payloads(flow.flow_json)
    flow.schema_version = str((flow.flow_json or {}).get("version") or flow.schema_version)
    db.session.commit()

    # === Step 1: Validate flow JSON (v7.3 schema) ===
    validation = validate_flow_json(flow.flow_json, flow.entry_screen_id)
    if not validation.valid:
        validation_data = validation.to_dict()
        return jsonify({
            "success": False,
            "error": "validation_failed",
            "message": "Flow validation failed",
            "validation": validation_data,
            "errors": validation_data.get("errors", [])
        }), 400

    # === Step 2: Get account with valid token (centralized) ===
    account, token_error = get_account_with_token(flow.account_id)
    if token_error:
        return jsonify({
            "success": False,
            "error": "token_missing",
            "message": token_error
        }), 400

    # Update flow to use the valid account if different
    if account.id != flow.account_id:
        flow.account_id = account.id
        db.session.commit()

    access_token = account.get_access_token()
    
    try:
        import json
        import time
        api_version = os.getenv("WHATSAPP_API_VERSION", "v18.0")
        graph_base = f"https://graph.facebook.com/{api_version}"

        create_payload = {
            "name": flow.name,
            "categories": [normalize_flow_category(flow.category)],
        }

        if _flow_uses_data_endpoint(flow.flow_json):
            if not account.has_flow_keys():
                return jsonify({
                    "success": False,
                    "error": "flow_keys_missing",
                    "message": "Flow encryption keys are not configured for this WhatsApp account.",
                }), 400

            public_base_url = _resolve_public_base_url()
            if not public_base_url:
                return jsonify({
                    "success": False,
                    "error": "endpoint_uri_missing",
                    "message": "Unable to determine a public base URL for the flow endpoint.",
                }), 400

            create_payload["endpoint_uri"] = (
                f"{public_base_url}/api/whatsapp/flows/endpoint/{account.waba_id}"
            )
        
        # === Step 3: Create flow on Meta ===
        create_response = http_requests.post(
            f"{graph_base}/{account.waba_id}/flows",
            headers={"Authorization": f"Bearer {access_token}"},
            json=create_payload,
            timeout=30
        )
        
        if create_response.status_code != 200:
            try:
                error_data = create_response.json()
            except Exception:
                error_data = {}
            payload, status = _response_for_meta_flow_create_error(error_data)
            return jsonify(payload), status
        
        meta_flow_id = create_response.json().get("id")
        
        # === Step 4: Upload flow JSON as asset ===
        asset_response = http_requests.post(
            f"{graph_base}/{meta_flow_id}/assets",
            headers={"Authorization": f"Bearer {access_token}"},
            files={"file": ("flow.json", json.dumps(flow.flow_json), "application/json")},
            data={"name": "flow.json", "asset_type": "FLOW_JSON"},
            timeout=30
        )
        
        if asset_response.status_code != 200:
            error_data = _safe_response_json(asset_response)
            error_msg = error_data.get("error", {}).get("message", "Unknown error")
            return jsonify({
                "success": False,
                "error": "json_upload_failed",
                "message": f"Failed to upload flow JSON: {error_msg}",
                "meta_flow_id": meta_flow_id,
                "meta_error": error_data.get("error", {})
            }), 400
        
        # === Step 5: Publish the flow ===
        publish_response = http_requests.post(
            f"{graph_base}/{meta_flow_id}/publish",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=30
        )
        
        if publish_response.status_code != 200:
            error_data = _safe_response_json(publish_response)
            error_msg = error_data.get("error", {}).get("message", "Unknown error")
            
            # Check for specific blocking reasons
            if "integrity" in error_msg.lower():
                return jsonify({
                    "success": False,
                    "error": "business_not_verified",
                    "message": "Cannot publish: Business verification is required. Complete verification in Meta Business Manager.",
                    "action_url": "https://business.facebook.com/settings/whatsapp-business-accounts",
                    "meta_flow_id": meta_flow_id,
                    "meta_error": error_data.get("error", {})
                }), 400
            elif "quality" in error_msg.lower():
                return jsonify({
                    "success": False,
                    "error": "message_quality_low",
                    "message": "Cannot publish: Message quality is too low. Send more high-quality messages to improve your score.",
                    "meta_flow_id": meta_flow_id,
                    "meta_error": error_data.get("error", {})
                }), 400
            else:
                return jsonify({
                    "success": False,
                    "error": "publish_failed",
                    "message": f"Failed to publish flow: {error_msg}",
                    "meta_flow_id": meta_flow_id,
                    "meta_error": error_data.get("error", {})
                }), 400
        
        # === Success - update flow record ===
        flow.meta_flow_id = meta_flow_id
        flow.status = "PUBLISHED"
        flow.published_at = datetime.now(timezone.utc)
        db.session.commit()
        
        return jsonify({
            "success": True,
            "flow": flow.to_dict(),
            "message": "Flow published successfully!",
            "meta_flow_id": meta_flow_id
        })
        
    except http_requests.Timeout:
        return jsonify({
            "success": False,
            "error": "timeout",
            "message": "Request timed out. Please try again."
        }), 504
    except http_requests.RequestException as e:
        return jsonify({
            "success": False,
            "error": "network_error",
            "message": f"Network error while publishing: {str(e)}"
        }), 500
    except Exception as e:
        logger.exception("Unexpected error publishing flow %s", flow_id)
        return jsonify({
            "success": False,
            "error": "publish_internal_error",
            "message": f"Unexpected error while publishing: {str(e)}"
        }), 500


# ============================================================
# POST /api/whatsapp/flows/{id}/clone - Clone Published Flow
# ============================================================

@flow_bp.route("/<int:flow_id>/clone", methods=["POST"])
def clone_flow(flow_id: int):
    """
    Clone a flow to create a new editable version.
    
    Used for:
    - Editing published flows (immutable)
    - Creating variations
    - Version history
    """
    flow = WhatsAppFlow.query.get(flow_id)
    
    if not flow:
        return jsonify({"success": False, "error": "Flow not found"}), 404

    # Check Subscription Limit (Cloning creates new flow)
    account = WhatsAppAccount.query.get(flow.account_id)
    workspace_id = getattr(account, "workspace_id", None)
    if workspace_id:
        try:
             wid = int(workspace_id)
             workspace = Workspace.query.get(wid)
             if workspace:
                 user = User.query.get(workspace.user_id)
                 if user:
                     allowed, current, limit = check_flow_limit(user, wid)
                     if not allowed:
                         return jsonify({
                             "success": False, 
                             "error": "flow_limit_exceeded", 
                             "limit": limit,
                             "current": current,
                             "plan": user.plan,
                             "message": "Limit reached. Please upgrade your plan."
                         }), 403
        except Exception:
             pass
    
    # Clone the flow
    new_flow = flow.clone()
    new_flow.flow_json = normalize_flow_submission_payloads(new_flow.flow_json)
    new_flow.schema_version = str((new_flow.flow_json or {}).get("version") or new_flow.schema_version)
    
    # Optionally customize from request
    data = request.get_json() or {}
    if data.get("name"):
        new_flow.name = data["name"]
    
    db.session.add(new_flow)
    
    # Deprecate old flow if it was published
    if flow.status == "PUBLISHED":
        flow.status = "DEPRECATED"
    
    db.session.commit()
    
    return jsonify({
        "success": True,
        "flow": new_flow.to_dict(),
        "message": f"Created new version v{new_flow.flow_version}",
        "original_flow_id": flow_id
    })


# ============================================================
# POST /api/whatsapp/flows/validate - Validate Flow JSON
# ============================================================

@flow_bp.route("/validate", methods=["POST"])
def validate_flow():
    """
    Validate flow JSON without saving.
    
    Request body:
    {
        "flow_json": {...},
        "entry_screen_id": "WELCOME"
    }
    """
    data = request.get_json() or {}
    
    flow_json = data.get("flow_json")
    entry_screen_id = data.get("entry_screen_id", "WELCOME")
    
    if not flow_json:
        return jsonify({
            "success": False,
            "error": "flow_json is required"
        }), 400
    
    flow_json = normalize_flow_submission_payloads(flow_json)
    validation = validate_flow_json(flow_json, entry_screen_id)
    
    return jsonify({
        "success": True,
        "valid": validation.valid,
        "validation": validation.to_dict()
    })


# ============================================================
# POST /api/whatsapp/flows/{id}/deprecate - Deprecate Flow
# ============================================================

@flow_bp.route("/<int:flow_id>/deprecate", methods=["POST"])
def deprecate_flow(flow_id: int):
    """Mark a published flow as deprecated."""
    flow = WhatsAppFlow.query.get(flow_id)
    
    if not flow:
        return jsonify({"success": False, "error": "Flow not found"}), 404
    
    if flow.status != "PUBLISHED":
        return jsonify({
            "success": False,
            "error": "Only published flows can be deprecated"
        }), 400
    
    flow.status = "DEPRECATED"
    db.session.commit()
    
    return jsonify({
        "success": True,
        "flow": flow.to_dict(),
        "message": "Flow deprecated successfully"
    })


# ============================================================
# GET /api/whatsapp/flows/templates - Get Sample Templates
# ============================================================

@flow_bp.route("/templates", methods=["GET"])
def get_flow_templates():
    """Get sample flow templates for quick start."""
    return jsonify({
        "success": True,
        "templates": [
            {
                "category": "LEAD_GENERATION",
                "name": "Lead Capture",
                "description": "Collect name, email, and phone",
                "flow_json": generate_sample_flow("LEAD_GENERATION")
            },
            {
                "category": "SURVEY",
                "name": "Customer Survey",
                "description": "Rating and feedback form",
                "flow_json": generate_sample_flow("SURVEY")
            }
        ]
    })
