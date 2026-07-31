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
from shared_models import db
from .visual_automation_models import WhatsAppVisualAutomation
from .models import WhatsAppAccount, WhatsAppConversation
from .visual_automation_models import WhatsAppConversationState
from .interactive_automation_engine import (
    InteractiveAutomationEngine,
    invalidate_trigger_cache,
    invalidate_flow_cache,
)
from . import api_node_executor
from . import interactive_flow_ai
from . import flow_variables

logger = logging.getLogger(__name__)

interactive_automation_bp = Blueprint("interactive_automation", __name__)


# ============================================================
# Helper Functions
# ============================================================

def _extract_trigger_toggles_to_config(automation, nodes):
    """
    Extract trigger toggles from trigger node and store in trigger_config.
    
    This ensures trigger restrictions are accessible both in nodes (for UI)
    and in trigger_config (for backend matching logic).
    """
    if not nodes:
        return
    
    # Find trigger node
    trigger_node = None
    for node in nodes:
        if node.get("type") == "trigger":
            trigger_node = node
            break
    
    if not trigger_node:
        return
    
    trigger_data = trigger_node.get("data", {}) or {}

    # Extract toggles from trigger node data.
    first_message_only = trigger_data.get("firstMessageOnly", False)
    one_time_only = trigger_data.get("oneTimeOnly", False)
    node_trigger_type = trigger_data.get("triggerType")
    node_keywords = trigger_data.get("keywords") or []

    # Reassign whole dict so SQLAlchemy tracks JSON change reliably
    current_trigger_config = automation.trigger_config or {}
    merged = {
        **current_trigger_config,
        "firstMessageOnly": bool(first_message_only),
        "oneTimeOnly": bool(one_time_only),
    }
    if node_trigger_type:
        merged["type"] = node_trigger_type
        automation.trigger_type = node_trigger_type
    if node_keywords:
        merged["keywords"] = node_keywords
    automation.trigger_config = merged
    logger.debug(
        "Extracted trigger toggles for automation %s: firstMessageOnly=%s, oneTimeOnly=%s",
        automation.id,
        bool(first_message_only),
        bool(one_time_only),
    )


def _validate_automation_nodes(nodes: list) -> list:
    """Return list of validation error strings for flow nodes."""
    errors = []
    for node in nodes or []:
        if not isinstance(node, dict):
            continue
        if node.get("type") != "api":
            continue
        node_errors = api_node_executor.validate_api_node_data(node.get("data") or {})
        label = (node.get("data") or {}).get("label") or node.get("id") or "api"
        for err in node_errors:
            errors.append(f"API node '{label}': {err}")
    return errors


def _merge_variables(existing: dict, incoming: dict, *, replace: bool = False) -> dict:
    """Merge flow variables; preserve existing secrets when masked placeholder sent."""
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
    """Apply variables / flow_config from API payload."""
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
        workspace_id = request.args.get("workspace_id") or (request.get_json(silent=True) or {}).get("workspace_id")
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
            variables=data.get("variables") if isinstance(data.get("variables"), dict) else {},
            flow_config=(
                data.get("flow_config")
                if isinstance(data.get("flow_config"), dict)
                else (data.get("flowConfig") if isinstance(data.get("flowConfig"), dict) else {})
            ),
            status="draft",
            is_active=False,
        )
        
        # Extract trigger toggles from trigger node and add to trigger_config
        _extract_trigger_toggles_to_config(automation, data.get("nodes", []))
        
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
            automation.trigger_config = trigger

        # Always derive trigger toggles from trigger node after applying updates,
        # so trigger payloads cannot overwrite it.
        source_nodes = data.get("nodes") or automation.nodes or []
        _extract_trigger_toggles_to_config(automation, source_nodes)

        if "viewport" in data:
            automation.viewport = data["viewport"]

        _apply_automation_metadata(automation, data)

        if "nodes" in data:
            node_errors = _validate_automation_nodes(source_nodes)
            if node_errors:
                return jsonify({"success": False, "error": "Invalid flow nodes", "details": node_errors}), 400
            
        automation.version = (automation.version or 1) + 1
        automation.updated_at = datetime.now(timezone.utc)
        
        db.session.commit()
        
        # Invalidate caches so next match uses fresh trigger config
        invalidate_trigger_cache(automation.workspace_id, automation.account_id)
        invalidate_flow_cache(automation_id)
        
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
        
        workspace_id = automation.workspace_id
        account_id = automation.account_id
            
        db.session.delete(automation)
        db.session.commit()
        
        # Invalidate caches
        invalidate_trigger_cache(workspace_id, account_id)
        invalidate_flow_cache(automation_id)
        
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
        
        # Invalidate caches so trigger index includes this automation
        invalidate_trigger_cache(automation.workspace_id, automation.account_id)
        invalidate_flow_cache(automation_id)
        
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
        
        # Invalidate caches
        invalidate_trigger_cache(automation.workspace_id, automation.account_id)
        invalidate_flow_cache(automation_id)
        
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


@interactive_automation_bp.route("/interactive-automations/conversation-state", methods=["GET"])
@require_workspace
def get_conversation_flow_state(workspace_id: str):
    """
    Return the collected interactive-flow responses + progress for a conversation so the
    dashboard can show what the customer has answered so far (and where the flow is).

    Query params:
        - workspace_id: required
        - conversation_id: required
        - include_completed: optional ("1"/"true") to also return the most recent
          finished/paused flow when none is active.
    """
    conversation_id = request.args.get("conversation_id")
    if not conversation_id:
        return jsonify({"success": False, "error": "conversation_id is required"}), 400
    try:
        conversation_id = int(conversation_id)
    except (TypeError, ValueError):
        return jsonify({"success": False, "error": "conversation_id must be an integer"}), 400

    include_completed = str(request.args.get("include_completed") or "").lower() in ("1", "true", "yes")

    # Defense in depth: ensure the conversation belongs to this workspace.
    conversation = WhatsAppConversation.query.get(conversation_id)
    if conversation is not None:
        acct = WhatsAppAccount.query.get(conversation.account_id)
        if acct and str(acct.workspace_id or "") != str(workspace_id):
            return jsonify({"success": False, "error": "conversation not in workspace"}), 403

    state = (
        WhatsAppConversationState.query.filter_by(
            conversation_id=conversation_id,
            workspace_id=str(workspace_id),
            is_active=True,
        )
        .order_by(WhatsAppConversationState.updated_at.desc())
        .first()
    )
    if state is None and include_completed:
        state = (
            WhatsAppConversationState.query.filter_by(
                conversation_id=conversation_id,
                workspace_id=str(workspace_id),
            )
            .order_by(WhatsAppConversationState.updated_at.desc())
            .first()
        )

    if state is None:
        return jsonify({"success": True, "flowState": None})

    sd = state.state_data if isinstance(state.state_data, dict) else {}
    collected = sd.get("collected") or {}
    field_order = sd.get("_field_order") or list(collected.keys())

    # Map field -> {label, validationType, required} from the automation's input nodes.
    field_meta = {}
    automation_name = None
    try:
        automation = (
            WhatsAppVisualAutomation.query.get(state.automation_id) if state.automation_id else None
        )
        if automation is not None:
            automation_name = automation.name
            for node in (automation.nodes or []):
                if (node.get("type") or "") != "input":
                    continue
                ndata = node.get("data") or {}
                fkey = ndata.get("field")
                if not fkey:
                    continue
                field_meta[fkey] = {
                    "label": ndata.get("body") or ndata.get("label") or fkey,
                    "validationType": ndata.get("validationType", "text"),
                    "required": bool(ndata.get("required", True)),
                    "enumValues": (
                        ndata.get("enumValues")
                        or ndata.get("options")
                        or ndata.get("enum_values")
                        or None
                    ),
                    "placeholder": ndata.get("placeholder"),
                }
    except Exception as exc:
        logger.warning("flow-state: could not load automation %s meta: %s", state.automation_id, exc)

    responses = []
    seen = set()
    for idx, fkey in enumerate(field_order):
        if fkey in seen or fkey not in collected:
            continue
        seen.add(fkey)
        meta = field_meta.get(fkey, {})
        responses.append({
            "field": fkey,
            "label": meta.get("label", fkey),
            "value": collected.get(fkey),
            "order": idx,
            "validationType": meta.get("validationType", "text"),
            "required": meta.get("required", True),
        })
    # Any collected fields missing from _field_order (defensive).
    for fkey, val in collected.items():
        if fkey in seen:
            continue
        seen.add(fkey)
        meta = field_meta.get(fkey, {})
        responses.append({
            "field": fkey,
            "label": meta.get("label", fkey),
            "value": val,
            "order": len(responses),
            "validationType": meta.get("validationType", "text"),
            "required": meta.get("required", True),
        })

    return jsonify({
        "success": True,
        "flowState": {
            "stateId": state.id,
            "automationId": state.automation_id,
            "automationName": automation_name,
            "currentNodeId": state.current_node_id,
            "isActive": bool(state.is_active),
            "isPaused": bool(sd.get("paused")),
            "pauseReason": sd.get("pause_reason"),
            "waitingForInput": bool(sd.get("waiting_for_input")),
            "currentField": sd.get("current_field"),
            # The awaited input field's metadata (validationType + enum options) so the FE can
            # render the right typed keyboard for the step the customer is on. None unless waiting.
            "currentInput": (
                {
                    "field": sd.get("current_field"),
                    "label": (field_meta.get(sd.get("current_field")) or {}).get("label", sd.get("current_field")),
                    "validationType": (field_meta.get(sd.get("current_field")) or {}).get("validationType", "text"),
                    "enumValues": (field_meta.get(sd.get("current_field")) or {}).get("enumValues"),
                    "placeholder": (field_meta.get(sd.get("current_field")) or {}).get("placeholder"),
                    "required": (field_meta.get(sd.get("current_field")) or {}).get("required", True),
                }
                if sd.get("waiting_for_input") and sd.get("current_field")
                else None
            ),
            "completedAt": state.completed_at.isoformat() if state.completed_at else None,
            "updatedAt": state.updated_at.isoformat() if state.updated_at else None,
            "responses": responses,
        },
    })


@interactive_automation_bp.route("/interactive-automations/trigger-manual", methods=["POST"])
def trigger_interactive_automation_manual():
    """
    Manually trigger an active interactive automation for a specific conversation.

    Request body:
    {
        "workspace_id": "abc",
        "account_id": 123,
        "conversation_id": 456,
        "automation_id": 789
    }
    """
    try:
        data = request.get_json(silent=True) or {}

        workspace_id = str(data.get("workspace_id") or "").strip()
        account_id = data.get("account_id")
        conversation_id = data.get("conversation_id")
        automation_id = data.get("automation_id")

        if not workspace_id:
            return jsonify({"success": False, "error": "workspace_id is required"}), 400
        if not account_id:
            return jsonify({"success": False, "error": "account_id is required"}), 400
        if not conversation_id:
            return jsonify({"success": False, "error": "conversation_id is required"}), 400
        if not automation_id:
            return jsonify({"success": False, "error": "automation_id is required"}), 400

        account = WhatsAppAccount.query.get(int(account_id))
        if not account:
            return jsonify({"success": False, "error": "Account not found"}), 404

        if str(account.workspace_id or "") != workspace_id:
            return jsonify({"success": False, "error": "Account does not belong to workspace"}), 403

        conversation = WhatsAppConversation.query.filter_by(
            id=int(conversation_id),
            account_id=int(account_id),
        ).first()
        if not conversation:
            return jsonify({"success": False, "error": "Conversation not found"}), 404

        automation = WhatsAppVisualAutomation.query.filter_by(
            id=int(automation_id),
            account_id=int(account_id),
            workspace_id=workspace_id,
        ).first()
        if not automation:
            return jsonify({"success": False, "error": "Interactive automation not found"}), 404

        if not automation.is_active or automation.status != "active":
            return jsonify({"success": False, "error": "Only active automations can be triggered"}), 400

        # Clear active state so manual trigger can start from the selected flow.
        active_states = WhatsAppConversationState.query.filter_by(
            workspace_id=workspace_id,
            conversation_id=int(conversation_id),
            is_active=True,
        ).all()
        for state in active_states:
            state.complete()
        db.session.commit()

        engine = InteractiveAutomationEngine(
            account_id=int(account_id),
            workspace_id=workspace_id,
            account=account,
        )

        match_entry = {
            "automation_id": automation.id,
            "name": automation.name,
            "version_token": engine._automation_version_token(automation),
        }

        result = engine._start_automation_flow(
            match_entry,
            int(conversation_id),
            conversation.user_phone,
        )

        if not result or not result.get("success"):
            return jsonify({
                "success": False,
                "error": (result or {}).get("error") or "Failed to trigger interactive automation",
            }), 500

        invalidate_flow_cache(automation.id)

        return jsonify({
            "success": True,
            "message": "Interactive automation triggered successfully",
            "automation": {
                "id": automation.id,
                "name": automation.name,
            },
            "conversation_id": int(conversation_id),
            "result": result,
        })

    except Exception as e:
        db.session.rollback()
        logger.exception("Error triggering interactive automation manually: %s", e)
        return jsonify({"success": False, "error": str(e)}), 500


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


@interactive_automation_bp.route("/interactive-automations/<int:automation_id>/variables", methods=["PUT", "PATCH"])
def update_interactive_automation_variables(automation_id: int):
    """
    Update per-flow variables (API tokens, URLs) stored in the database.

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

        invalidate_trigger_cache(automation.workspace_id, automation.account_id)
        invalidate_flow_cache(automation_id)

        return jsonify({"success": True, "automation": automation.to_dict()})
    except Exception as e:
        db.session.rollback()
        logger.error(f"Error updating automation variables: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@interactive_automation_bp.route("/interactive-automations/test-api-node", methods=["POST"])
def test_api_node():
    """
    Dry-run an API node configuration (builder preview / connectivity test).

    Body:
    {
      "node": { "data": { ... api node config ... } },
      "variables": { "phone": "9198...", "name": "Test" }
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
        # Record AI usage for billing/quota (fail-soft).
        try:
            from subscription.service import record_ai_usage
            from tenant.context import get_current_user
            _acting_user = get_current_user()
            record_ai_usage(
                getattr(_acting_user, "id", None),
                int(workspace_id) if workspace_id else None,
                "ai_flow_generation",
                "gemini",
                _commit=True,
            )
        except Exception:
            pass
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
