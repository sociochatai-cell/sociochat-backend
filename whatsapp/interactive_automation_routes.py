"""
Interactive Automation API Routes
=================================

REST API for managing Interactive WhatsApp automation flows (visual builder).

Endpoints:
    GET    /api/whatsapp/interactive-automations          → List all automations
    POST   /api/whatsapp/interactive-automations          → Create new automation
    GET    /api/whatsapp/interactive-automations/<id>     → Get automation by ID
    PUT    /api/whatsapp/interactive-automations/<id>     → Update automation
    DELETE /api/whatsapp/interactive-automations/<id>     → Delete automation
    POST   /api/whatsapp/interactive-automations/<id>/publish → Publish automation
    POST   /api/whatsapp/interactive-automations/<id>/pause   → Pause automation
"""

import logging
from datetime import datetime, timezone
from functools import wraps
from flask import Blueprint, request, jsonify, g
from models import db
from .visual_automation_models import WhatsAppVisualAutomation
from .models import WhatsAppAccount
from . import api_node_executor
from . import interactive_flow_ai
from subscription.decorators import require_feature
from . import flow_variables

logger = logging.getLogger(__name__)

interactive_automation_bp = Blueprint("interactive_automation", __name__)


# ============================================================
# Helper Functions (additive)
# ============================================================

def _merge_variables(existing: dict, incoming: dict, *, replace: bool = False) -> dict:
    """Merge flow variables; preserve existing secrets when a masked placeholder is sent."""
    if replace or not isinstance(existing, dict):
        base = {}
    else:
        base = dict(existing)
    if not isinstance(incoming, dict):
        return base
    for key, value in incoming.items():
        if value == "***" and key in base:
            continue
        if (
            flow_variables.is_sensitive_variable_key(key)
            and (value is None or (isinstance(value, str) and not value.strip()))
            and key in base
            and base.get(key) not in (None, "")
        ):
            continue
        base[key] = value
    return base


def _apply_automation_metadata(automation: WhatsAppVisualAutomation, data: dict) -> None:
    """Apply variables / flow_config from an API payload (additive, no migration)."""
    if "variables" in data:
        incoming = data.get("variables") or {}
        replace = bool(data.get("replaceVariables"))
        automation.variables = _merge_variables(
            automation.variables if isinstance(automation.variables, dict) else {},
            incoming,
            replace=replace,
        )
    if "flow_config" in data or "flowConfig" in data:
        cfg = data.get("flow_config") if "flow_config" in data else data.get("flowConfig")
        automation.flow_config = cfg if isinstance(cfg, dict) else {}


# ============================================================
# Decorators
# ============================================================

def require_workspace(f):
    """
    Decorator to verify workspace_id is provided.
    """
    @wraps(f)
    def decorated_function(*args, **kwargs):
        workspace_id = request.args.get("workspace_id") or request.json.get("workspace_id")
        if not workspace_id:
            return jsonify({"success": False, "error": "workspace_id is required"}), 400
        kwargs["workspace_id"] = str(workspace_id)
        return f(*args, **kwargs)
    return decorated_function


# ============================================================
# Interactive Automation CRUD
# ============================================================

@interactive_automation_bp.route("/interactive-automations", methods=["GET"])
@require_workspace
def list_interactive_automations(workspace_id: str):
    """
    List all interactive automations for a workspace.
    
    Query params:
        - workspace_id: Required workspace ID
        - account_id: Optional filter by account
        - status: Optional filter by status (draft, active, paused)
    """
    try:
        account_id = request.args.get("account_id", type=int)
        status = request.args.get("status")
        
        query = WhatsAppVisualAutomation.query.filter_by(workspace_id=workspace_id)
        
        if account_id:
            query = query.filter_by(account_id=account_id)
        if status:
            query = query.filter_by(status=status)
            
        automations = query.order_by(WhatsAppVisualAutomation.updated_at.desc()).all()
        
        return jsonify({
            "success": True,
            "automations": [a.to_dict() for a in automations]
        })
        
    except Exception as e:
        logger.error(f"Error listing interactive automations: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@interactive_automation_bp.route("/interactive-automations", methods=["POST"])
@require_feature("whatsapp_interactive_automation")
def create_interactive_automation():
    """
    Create a new interactive automation.
    
    Request body:
    {
        "account_id": 123,
        "workspace_id": "abc",
        "name": "My Automation",
        "description": "Optional description",
        "nodes": [...],
        "edges": [...],
        "trigger": {"type": "any_reply", "enabled": true}
    }
    """
    try:
        data = request.get_json()
        
        if not data:
            return jsonify({"success": False, "error": "Request body is required"}), 400
            
        workspace_id = data.get("workspace_id")
        account_id = data.get("account_id")
        name = data.get("name", "Untitled Automation")
        
        if not workspace_id:
            return jsonify({"success": False, "error": "workspace_id is required"}), 400
        if not account_id:
            return jsonify({"success": False, "error": "account_id is required"}), 400
            
        # Verify account exists
        account = WhatsAppAccount.query.get(account_id)
        if not account:
            return jsonify({"success": False, "error": "Account not found"}), 404
            
        # Extract trigger info
        trigger = data.get("trigger", {})
        trigger_type = trigger.get("type", "any_reply")
        
        # Create automation
        automation = WhatsAppVisualAutomation(
            workspace_id=str(workspace_id),
            account_id=account_id,
            name=name,
            description=data.get("description"),
            trigger_type=trigger_type,
            trigger_config=trigger,
            nodes=data.get("nodes", []),
            edges=data.get("edges", []),
            status="draft",
            is_active=False,
        )

        # Additive: persist flow variables / flow_config if provided
        _apply_automation_metadata(automation, data)

        db.session.add(automation)
        db.session.commit()

        logger.info(f"Created interactive automation {automation.id} for workspace {workspace_id}")
        
        return jsonify({
            "success": True,
            "automation": automation.to_dict()
        }), 201
        
    except Exception as e:
        db.session.rollback()
        logger.error(f"Error creating interactive automation: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@interactive_automation_bp.route("/interactive-automations/<int:automation_id>", methods=["GET"])
def get_interactive_automation(automation_id: int):
    """Get a specific interactive automation."""
    try:
        automation = WhatsAppVisualAutomation.query.get(automation_id)
        
        if not automation:
            return jsonify({"success": False, "error": "Automation not found"}), 404
            
        return jsonify({
            "success": True,
            "automation": automation.to_dict()
        })
        
    except Exception as e:
        logger.error(f"Error getting interactive automation: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@interactive_automation_bp.route("/interactive-automations/<int:automation_id>", methods=["PUT"])
def update_interactive_automation(automation_id: int):
    """
    Update an interactive automation.
    
    Only provided fields are updated.
    """
    try:
        automation = WhatsAppVisualAutomation.query.get(automation_id)
        
        if not automation:
            return jsonify({"success": False, "error": "Automation not found"}), 404
            
        data = request.get_json()
        
        # Update fields if provided
        if "name" in data:
            automation.name = data["name"]
        if "description" in data:
            automation.description = data["description"]
        if "nodes" in data:
            automation.nodes = data["nodes"]
        if "edges" in data:
            automation.edges = data["edges"]
        if "trigger" in data:
            trigger = data["trigger"]
            automation.trigger_type = trigger.get("type", automation.trigger_type)
            # Preserve reserved flow variable / config keys stored in trigger_config
            existing_cfg = automation.trigger_config if isinstance(automation.trigger_config, dict) else {}
            new_cfg = dict(trigger) if isinstance(trigger, dict) else {}
            for reserved_key in (
                WhatsAppVisualAutomation._VARIABLES_KEY,
                WhatsAppVisualAutomation._FLOW_CONFIG_KEY,
            ):
                if reserved_key in existing_cfg:
                    new_cfg[reserved_key] = existing_cfg[reserved_key]
            automation.trigger_config = new_cfg
        if "viewport" in data:
            automation.viewport = data["viewport"]

        # Additive: persist flow variables / flow_config if provided
        _apply_automation_metadata(automation, data)

        automation.version = (automation.version or 1) + 1
        automation.updated_at = datetime.now(timezone.utc)

        db.session.commit()

        logger.info(f"Updated interactive automation {automation_id}")
        
        return jsonify({
            "success": True,
            "automation": automation.to_dict()
        })
        
    except Exception as e:
        db.session.rollback()
        logger.error(f"Error updating interactive automation: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@interactive_automation_bp.route("/interactive-automations/<int:automation_id>", methods=["DELETE"])
def delete_interactive_automation(automation_id: int):
    """Delete an interactive automation."""
    try:
        automation = WhatsAppVisualAutomation.query.get(automation_id)
        
        if not automation:
            return jsonify({"success": False, "error": "Automation not found"}), 404
            
        db.session.delete(automation)
        db.session.commit()
        
        logger.info(f"Deleted interactive automation {automation_id}")
        
        return jsonify({
            "success": True,
            "message": "Automation deleted successfully"
        })
        
    except Exception as e:
        db.session.rollback()
        logger.error(f"Error deleting interactive automation: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@interactive_automation_bp.route("/interactive-automations/<int:automation_id>/publish", methods=["POST"])
def publish_interactive_automation(automation_id: int):
    """
    Publish an interactive automation.
    
    Sets status to 'active' and is_active to true.
    """
    try:
        automation = WhatsAppVisualAutomation.query.get(automation_id)
        
        if not automation:
            return jsonify({"success": False, "error": "Automation not found"}), 404
            
        automation.activate()
        db.session.commit()
        
        logger.info(f"Published interactive automation {automation_id}")
        
        return jsonify({
            "success": True,
            "automation": automation.to_dict(),
            "message": "Automation published successfully"
        })
        
    except Exception as e:
        db.session.rollback()
        logger.error(f"Error publishing interactive automation: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@interactive_automation_bp.route("/interactive-automations/<int:automation_id>/pause", methods=["POST"])
def pause_interactive_automation(automation_id: int):
    """
    Pause an interactive automation.
    
    Sets status to 'paused' and is_active to false.
    """
    try:
        automation = WhatsAppVisualAutomation.query.get(automation_id)
        
        if not automation:
            return jsonify({"success": False, "error": "Automation not found"}), 404
            
        automation.pause()
        db.session.commit()
        
        logger.info(f"Paused interactive automation {automation_id}")
        
        return jsonify({
            "success": True,
            "automation": automation.to_dict(),
            "message": "Automation paused successfully"
        })
        
    except Exception as e:
        db.session.rollback()
        logger.error(f"Error pausing interactive automation: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


# ============================================================
# Flow variables (additive)
# ============================================================

@interactive_automation_bp.route("/interactive-automations/<int:automation_id>/variables", methods=["PUT", "PATCH"])
def update_interactive_automation_variables(automation_id: int):
    """
    Update per-flow variables (API tokens, URLs) and flow_config.

    Body: { "variables": { "flow_api_token": "..." }, "flow_config": { "variableDefaults": {} } }
    Send "replaceVariables": true to replace the entire variables map.
    """
    try:
        automation = WhatsAppVisualAutomation.query.get(automation_id)
        if not automation:
            return jsonify({"success": False, "error": "Automation not found"}), 404

        data = request.get_json() or {}
        _apply_automation_metadata(automation, data)
        automation.version = (automation.version or 1) + 1
        automation.updated_at = datetime.now(timezone.utc)
        db.session.commit()

        return jsonify({"success": True, "automation": automation.to_dict()})
    except Exception as e:
        db.session.rollback()
        logger.error(f"Error updating automation variables: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


# ============================================================
# API node dry-run (builder preview / connectivity test)
# ============================================================

@interactive_automation_bp.route("/interactive-automations/test-api-node", methods=["POST"])
def test_api_node():
    """
    Dry-run an API node configuration (builder preview / connectivity test).

    Body:
    {
      "node": { "data": { ... api node config ... } },
      "variables": { "phone": "9198...", "name": "Test" },
      "automation_id": 123   // optional - merge stored flow variables
    }
    """
    try:
        data = request.get_json() or {}
        node = data.get("node") or {}
        node_data = node.get("data") if isinstance(node, dict) else {}
        if not node_data and isinstance(data.get("data"), dict):
            node_data = data["data"]
        variables = data.get("variables") or {}
        automation_id = data.get("automation_id") or data.get("automationId")
        if automation_id:
            automation = WhatsAppVisualAutomation.query.get(int(automation_id))
            if automation and isinstance(automation.variables, dict):
                merged = dict(automation.variables)
                merged.update(variables)
                variables = merged
                flow_config = automation.flow_config if isinstance(automation.flow_config, dict) else {}
                defaults = flow_config.get("variableDefaults") or {}
                variables = flow_variables.merge_variable_defaults(variables, defaults)

        errors = api_node_executor.validate_api_node_data(node_data)
        if errors:
            return jsonify({"success": False, "errors": errors}), 400

        result = api_node_executor.execute_api_node(node_data, variables)
        return jsonify({
            "success": result.success,
            "status_code": result.status_code,
            "handle": result.handle,
            "error": result.error,
            "parsed": result.parsed,
            "raw_preview": (result.raw_text or "")[:2000],
            "outbound_messages": result.outbound_messages,
        })
    except Exception as exc:
        logger.exception("test-api-node failed: %s", exc)
        return jsonify({"success": False, "error": str(exc)}), 500


# ============================================================
# AI flow generation
# ============================================================

@interactive_automation_bp.route("/interactive-automations/ai-generate", methods=["POST"])
def ai_generate_interactive_flow():
    """
    Generate a draft interactive automation from a natural-language prompt.

    Body: { "prompt": "...", "workspace_id": "optional" }
    Returns nodes + edges ready for the visual builder (does not persist).
    """
    try:
        data = request.get_json() or {}
        prompt = (data.get("prompt") or data.get("description") or "").strip()
        if not prompt:
            return jsonify({"success": False, "error": "prompt is required"}), 400

        workspace_id = data.get("workspace_id") or request.args.get("workspace_id")
        draft = interactive_flow_ai.generate_interactive_flow_draft(
            prompt=prompt,
            workspace_id=str(workspace_id) if workspace_id else None,
        )
        return jsonify({
            "success": True,
            "draft": draft,
            "hint": (
                "Set draft.variables.flow_api_token in flow settings before publishing. "
                "API nodes include Authorization headers using {{flow_api_token}}."
            ),
        })
    except ValueError as exc:
        return jsonify({"success": False, "error": str(exc)}), 400
    except RuntimeError as exc:
        return jsonify({"success": False, "error": str(exc)}), 503
    except Exception as exc:
        logger.exception("ai-generate interactive flow failed: %s", exc)
        return jsonify({"success": False, "error": str(exc) or "AI generation failed"}), 500


@interactive_automation_bp.route("/interactive-automations/clear-states", methods=["GET", "POST"])
def clear_conversation_states():
    """
    Clear all active conversation states.
    
    Useful for testing and debugging - clears stale flow states.
    
    Query params:
        - workspace_id: Optional workspace ID
        - phone: Optional phone number to clear states for
    """
    try:
        from .visual_automation_models import WhatsAppConversationState
        
        # Get params from query string or JSON body (if present)
        workspace_id = request.args.get("workspace_id")
        phone = request.args.get("phone")
        
        # Try to get from JSON body if not in query params
        if request.is_json:
            data = request.get_json() or {}
            workspace_id = workspace_id or data.get("workspace_id")
            phone = phone or data.get("phone")
        
        if not workspace_id:
            # Clear ALL active states (admin/debug use)
            query = WhatsAppConversationState.query.filter_by(is_active=True)
        else:
            query = WhatsAppConversationState.query.filter_by(
                workspace_id=str(workspace_id),
                is_active=True
            )
        
        if phone:
            query = query.filter_by(phone_number=phone)
        
        states = query.all()
        count = len(states)
        
        for state in states:
            state.is_active = False
        
        db.session.commit()
        
        logger.info(f"Cleared {count} conversation states")
        
        return jsonify({
            "success": True,
            "cleared_count": count,
            "message": f"Cleared {count} active conversation states"
        })
        
    except Exception as e:
        db.session.rollback()
        logger.error(f"Error clearing conversation states: {e}")
        return jsonify({"success": False, "error": str(e)}), 500
