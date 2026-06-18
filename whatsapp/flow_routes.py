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

from datetime import datetime, timezone
from flask import Blueprint, request, jsonify, g
import requests as http_requests

from models import db
from .models import WhatsAppFlow, WhatsAppAccount
from .flow_validator import validate_flow_json, generate_sample_flow, sanitize_flow_json
from .flow_access import require_flow_access, require_account_access, validate_flow_account_match
from .token_helper import get_account_with_token
from subscription.service import check_flow_limit
from subscription.decorators import require_feature
from models import Workspace, User

# Create blueprint
flow_bp = Blueprint("flows", __name__, url_prefix="/api/whatsapp/flows")


# ============================================================
# POST /api/whatsapp/flows - Create Draft Flow
# ============================================================

@flow_bp.route("", methods=["POST"])
@require_feature("whatsapp_flows")
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
    category = data.get("category", "CUSTOM")
    
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
    
    # Determine entry screen and sanitize IDs for Meta compatibility
    entry_screen_id = data.get("entry_screen_id")
    if not entry_screen_id:
        screens = flow_json.get("screens", [])
        entry_screen_id = screens[0].get("id") if screens else "WELCOME"
    flow_json, entry_screen_id = sanitize_flow_json(flow_json, entry_screen_id)
    
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
        flow.category = data["category"].upper()
    if "flow_json" in data:
        entry = data.get("entry_screen_id", flow.entry_screen_id)
        flow.flow_json, flow.entry_screen_id = sanitize_flow_json(data["flow_json"], entry)
    elif "entry_screen_id" in data:
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
    - token_expired: Token needs refresh
    - business_not_verified: Business verification required
    - flows_not_enabled: WABA doesn't have Flows capability
    - validation_failed: Flow JSON validation errors
    - meta_api_error: Meta API returned an error
    """
    flow = g.flow
    
    # === Step 1.5: Get account with valid token (centralized) ===
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
    
    if not flow.can_publish():
        return jsonify({
            "success": False,
            "error": "flow_not_publishable",
            "message": f"Flow cannot be published. Current status: {flow.status}",
            "status": flow.status
        }), 400
    
    # === Step 1: Sanitize + validate flow JSON (v7.3 schema) ===
    flow.flow_json, flow.entry_screen_id = sanitize_flow_json(
        flow.flow_json,
        flow.entry_screen_id,
    )
    db.session.commit()

    validation = validate_flow_json(flow.flow_json, flow.entry_screen_id)
    if not validation.valid:
        return jsonify({
            "success": False,
            "error": "validation_failed",
            "message": "Flow validation failed",
            "validation": validation.to_dict(),
            "errors": validation.to_dict().get("errors", [])
        }), 400
    
    # === Step 2: Get access token (already validated by helper) ===
    access_token = account.get_access_token()
    
    try:
        import json
        import time
        
        # === Step 3: Create flow on Meta ===
        create_response = http_requests.post(
            f"https://graph.facebook.com/v18.0/{account.waba_id}/flows",
            headers={"Authorization": f"Bearer {access_token}"},
            json={
                "name": flow.name,
                "categories": [flow.category],
            },
            timeout=30
        )
        
        if create_response.status_code != 200:
            error_data = create_response.json()
            error_msg = error_data.get("error", {}).get("message", "Unknown error")
            error_code = error_data.get("error", {}).get("code", 0)
            
            # Map Meta errors to user-friendly error types
            if "permission" in error_msg.lower() or error_code == 100:
                return jsonify({
                    "success": False,
                    "error": "flows_not_enabled",
                    "message": "Flows capability is not enabled for this WhatsApp Business Account. Contact Meta Business Support to request access.",
                    "meta_error": error_data.get("error", {})
                }), 400
            elif "integrity" in error_msg.lower() or error_code == 139000:
                return jsonify({
                    "success": False,
                    "error": "business_not_verified",
                    "message": "Business verification required. Complete verification in Meta Business Manager to publish flows.",
                    "action_url": "https://business.facebook.com/settings/whatsapp-business-accounts",
                    "meta_error": error_data.get("error", {})
                }), 400
            elif "token" in error_msg.lower():
                return jsonify({
                    "success": False,
                    "error": "token_invalid",
                    "message": "Access token is invalid or expired. Please reconnect your WhatsApp Business Account.",
                    "meta_error": error_data.get("error", {})
                }), 400
            else:
                return jsonify({
                    "success": False,
                    "error": "meta_api_error",
                    "message": f"Failed to create flow on Meta: {error_msg}",
                    "meta_error": error_data.get("error", {})
                }), 400
        
        meta_flow_id = create_response.json().get("id")
        
        # === Step 4: Upload flow JSON as asset ===
        asset_response = http_requests.post(
            f"https://graph.facebook.com/v18.0/{meta_flow_id}/assets",
            headers={"Authorization": f"Bearer {access_token}"},
            files={"file": ("flow.json", json.dumps(flow.flow_json), "application/json")},
            data={"name": "flow.json", "asset_type": "FLOW_JSON"},
            timeout=30
        )
        
        if asset_response.status_code != 200:
            error_data = asset_response.json()
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
            f"https://graph.facebook.com/v18.0/{meta_flow_id}/publish",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=30
        )
        
        if publish_response.status_code != 200:
            error_data = publish_response.json()
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
        from datetime import datetime, timezone
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
                "category": "LEAD_GEN",
                "name": "Lead Capture",
                "description": "Collect name, email, and phone",
                "flow_json": generate_sample_flow("LEAD_GEN")
            },
            {
                "category": "SURVEY",
                "name": "Customer Survey",
                "description": "Rating and feedback form",
                "flow_json": generate_sample_flow("SURVEY")
            }
        ]
    })
