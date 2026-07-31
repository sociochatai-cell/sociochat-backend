"""
WhatsApp Automation API Routes
==============================

RESTful API for managing WhatsApp automation rules.

Multi-tenant: All endpoints scope data to the authenticated user's workspace.

Endpoints:
    GET    /api/whatsapp/accounts/<id>/automation/rules       - List rules
    POST   /api/whatsapp/accounts/<id>/automation/rules       - Create rule
    GET    /api/whatsapp/accounts/<id>/automation/rules/<rid> - Get rule
    PUT    /api/whatsapp/accounts/<id>/automation/rules/<rid> - Update rule
    DELETE /api/whatsapp/accounts/<id>/automation/rules/<rid> - Delete rule
    
    GET    /api/whatsapp/accounts/<id>/automation/logs        - Get logs
    
    GET    /api/whatsapp/accounts/<id>/automation/business-hours    - Get hours
    PUT    /api/whatsapp/accounts/<id>/automation/business-hours    - Update hours
"""

import logging
from datetime import datetime, timezone
from flask import Blueprint, request, jsonify
from functools import wraps

from shared_models import db
from .models import WhatsAppAccount, WhatsAppConversation
from .automation_models import (
    WhatsAppAutomationRule,
    WhatsAppAutomationLog,
    WhatsAppBusinessHours,
    get_contact_overrides,
    set_contact_override,
)

logger = logging.getLogger(__name__)

automation_bp = Blueprint("whatsapp_automation", __name__)


# ============================================================
# Decorators
# ============================================================

def require_account_access(f):
    """
    Decorator to verify account access and inject account + workspace_id.
    
    Expects `account_id` in the URL parameters.
    Injects `account` and `workspace_id` into kwargs.
    """
    @wraps(f)
    def decorated_function(*args, **kwargs):
        account_id = kwargs.get("account_id")

        if not account_id:
            return jsonify({"error": "Account ID required"}), 400

        # Enforce that the authenticated user owns this account's workspace.
        # Fails closed (401/404/403) — closes the prior no-auth IDOR.
        from tenant.context import resolve_owned_account
        account, err = resolve_owned_account(account_id)
        if err:
            return err

        kwargs["account"] = account
        kwargs["workspace_id"] = account.workspace_id

        return f(*args, **kwargs)

    return decorated_function


# ============================================================
# Automation Rules CRUD
# ============================================================

@automation_bp.route("/accounts/<int:account_id>/automation/rules", methods=["GET"])
@require_account_access
def list_automation_rules(account_id: int, account: WhatsAppAccount, workspace_id: str):
    """
    List all automation rules for an account.
    
    Query params:
        - rule_type: Filter by rule type (welcome, away, keyword, command)
        - is_active: Filter by active status (true/false)
    """
    try:
        query = WhatsAppAutomationRule.query.filter_by(
            workspace_id=workspace_id,
            account_id=account_id,
            status="active"  # Don't show soft-deleted
        )
        
        # Apply filters
        rule_type = request.args.get("rule_type")
        if rule_type:
            query = query.filter_by(rule_type=rule_type)
        
        is_active = request.args.get("is_active")
        if is_active is not None:
            query = query.filter_by(is_active=is_active.lower() == "true")
        
        # Order by priority
        rules = query.order_by(WhatsAppAutomationRule.priority.asc()).all()
        
        return jsonify({
            "success": True,
            "rules": [rule.to_dict() for rule in rules],
            "count": len(rules)
        })
        
    except Exception as e:
        logger.exception(f"Error listing automation rules: {e}")
        return jsonify({"error": "Failed to list automation rules"}), 500


@automation_bp.route("/accounts/<int:account_id>/automation/rules", methods=["POST"])
@require_account_access
def create_automation_rule(account_id: int, account: WhatsAppAccount, workspace_id: str):
    """
    Create a new automation rule.
    
    Request body:
    {
        "name": "Welcome Message",
        "rule_type": "welcome",
        "trigger_config": {},
        "response_type": "text",
        "response_config": {"message": "Hello! Thanks for reaching out."},
        "is_active": true,
        "priority": 1,
        "cooldown_seconds": 3600,
        "max_triggers_per_day": 1
    }
    """
    try:
        data = request.get_json()
        
        if not data:
            return jsonify({"error": "Request body required"}), 400
        
        # Validate required fields
        required = ["name", "rule_type", "response_type", "response_config"]
        for field in required:
            if field not in data:
                return jsonify({"error": f"Missing required field: {field}"}), 400
        
        # Validate rule_type
        valid_types = ["welcome", "away", "keyword", "command", "default"]
        if data["rule_type"] not in valid_types:
            return jsonify({"error": f"Invalid rule_type. Must be one of: {valid_types}"}), 400
        
        # Validate response_type
        valid_response_types = ["text", "template", "interactive"]
        if data["response_type"] not in valid_response_types:
            return jsonify({"error": f"Invalid response_type. Must be one of: {valid_response_types}"}), 400
        
        # Create rule
        rule = WhatsAppAutomationRule(
            workspace_id=workspace_id,
            account_id=account_id,
            name=data["name"],
            description=data.get("description"),
            rule_type=data["rule_type"],
            trigger_config=data.get("trigger_config", {}),
            response_type=data["response_type"],
            response_config=data["response_config"],
            is_active=data.get("is_active", True),
            priority=data.get("priority", 100),
            cooldown_seconds=data.get("cooldown_seconds"),
            max_triggers_per_day=data.get("max_triggers_per_day"),
        )
        
        db.session.add(rule)
        db.session.commit()
        
        logger.info(f"Created automation rule: {rule.id} ({rule.name})")
        
        return jsonify({
            "success": True,
            "rule": rule.to_dict(),
            "message": "Automation rule created successfully"
        }), 201
        
    except Exception as e:
        db.session.rollback()
        logger.exception(f"Error creating automation rule: {e}")
        return jsonify({"error": "Failed to create automation rule"}), 500


@automation_bp.route("/accounts/<int:account_id>/automation/rules/<int:rule_id>", methods=["GET"])
@require_account_access
def get_automation_rule(account_id: int, rule_id: int, account: WhatsAppAccount, workspace_id: str):
    """Get a specific automation rule."""
    try:
        rule = WhatsAppAutomationRule.query.filter_by(
            id=rule_id,
            workspace_id=workspace_id,
            account_id=account_id
        ).first()
        
        if not rule:
            return jsonify({"error": "Rule not found"}), 404
        
        return jsonify({
            "success": True,
            "rule": rule.to_dict()
        })
        
    except Exception as e:
        logger.exception(f"Error getting automation rule: {e}")
        return jsonify({"error": "Failed to get automation rule"}), 500


@automation_bp.route("/accounts/<int:account_id>/automation/rules/<int:rule_id>", methods=["PUT"])
@require_account_access
def update_automation_rule(account_id: int, rule_id: int, account: WhatsAppAccount, workspace_id: str):
    """
    Update an automation rule.
    
    Only provided fields are updated.
    """
    try:
        rule = WhatsAppAutomationRule.query.filter_by(
            id=rule_id,
            workspace_id=workspace_id,
            account_id=account_id
        ).first()
        
        if not rule:
            return jsonify({"error": "Rule not found"}), 404
        
        data = request.get_json()
        if not data:
            return jsonify({"error": "Request body required"}), 400
        
        # Update allowed fields
        allowed_fields = [
            "name", "description", "trigger_config", "response_type",
            "response_config", "is_active", "priority", "cooldown_seconds",
            "max_triggers_per_day"
        ]
        
        for field in allowed_fields:
            if field in data:
                setattr(rule, field, data[field])
        
        rule.updated_at = datetime.now(timezone.utc)
        db.session.commit()
        
        logger.info(f"Updated automation rule: {rule.id}")
        
        return jsonify({
            "success": True,
            "rule": rule.to_dict(),
            "message": "Automation rule updated successfully"
        })
        
    except Exception as e:
        db.session.rollback()
        logger.exception(f"Error updating automation rule: {e}")
        return jsonify({"error": "Failed to update automation rule"}), 500


@automation_bp.route("/accounts/<int:account_id>/automation/rules/<int:rule_id>", methods=["DELETE"])
@require_account_access
def delete_automation_rule(account_id: int, rule_id: int, account: WhatsAppAccount, workspace_id: str):
    """
    Soft-delete an automation rule.
    
    Sets status to 'deleted' instead of removing from database.
    """
    try:
        rule = WhatsAppAutomationRule.query.filter_by(
            id=rule_id,
            workspace_id=workspace_id,
            account_id=account_id
        ).first()
        
        if not rule:
            return jsonify({"error": "Rule not found"}), 404
        
        # Soft delete
        rule.status = "deleted"
        rule.is_active = False
        rule.updated_at = datetime.now(timezone.utc)
        db.session.commit()
        
        logger.info(f"Deleted automation rule: {rule.id}")
        
        return jsonify({
            "success": True,
            "message": "Automation rule deleted successfully"
        })
        
    except Exception as e:
        db.session.rollback()
        logger.exception(f"Error deleting automation rule: {e}")
        return jsonify({"error": "Failed to delete automation rule"}), 500


# ============================================================
# Automation Logs
# ============================================================

@automation_bp.route("/accounts/<int:account_id>/automation/logs", methods=["GET"])
@require_account_access
def list_automation_logs(account_id: int, account: WhatsAppAccount, workspace_id: str):
    """
    List automation execution logs.
    
    Query params:
        - rule_id: Filter by rule ID
        - conversation_id: Filter by conversation ID
        - limit: Max results (default 50, max 200)
        - offset: Pagination offset
    """
    try:
        query = WhatsAppAutomationLog.query.filter_by(
            workspace_id=workspace_id
        ).join(
            WhatsAppAutomationRule,
            WhatsAppAutomationLog.rule_id == WhatsAppAutomationRule.id
        ).filter(
            WhatsAppAutomationRule.account_id == account_id
        )
        
        # Apply filters
        rule_id = request.args.get("rule_id", type=int)
        if rule_id:
            query = query.filter(WhatsAppAutomationLog.rule_id == rule_id)
        
        conversation_id = request.args.get("conversation_id", type=int)
        if conversation_id:
            query = query.filter(WhatsAppAutomationLog.conversation_id == conversation_id)
        
        # Pagination
        limit = min(request.args.get("limit", 50, type=int), 200)
        offset = request.args.get("offset", 0, type=int)
        
        # Get total count
        total = query.count()
        
        # Order by most recent and paginate
        logs = query.order_by(
            WhatsAppAutomationLog.created_at.desc()
        ).offset(offset).limit(limit).all()
        
        return jsonify({
            "success": True,
            "logs": [log.to_dict() for log in logs],
            "count": len(logs),
            "total": total,
            "limit": limit,
            "offset": offset
        })
        
    except Exception as e:
        logger.exception(f"Error listing automation logs: {e}")
        return jsonify({"error": "Failed to list automation logs"}), 500


# ============================================================
# Business Hours
# ============================================================

@automation_bp.route("/accounts/<int:account_id>/automation/business-hours", methods=["GET"])
@require_account_access
def get_business_hours(account_id: int, account: WhatsAppAccount, workspace_id: str):
    """Get business hours configuration."""
    try:
        hours = WhatsAppBusinessHours.query.filter_by(
            workspace_id=str(workspace_id),
            account_id=account_id
        ).first()
        
        if not hours:
            # Return default empty config
            return jsonify({
                "success": True,
                "business_hours": None,
                "message": "No business hours configured"
            })
        
        return jsonify({
            "success": True,
            "business_hours": hours.to_dict()
        })
        
    except Exception as e:
        logger.exception(f"Error getting business hours: {e}")
        return jsonify({"error": "Failed to get business hours"}), 500


@automation_bp.route("/accounts/<int:account_id>/automation/business-hours", methods=["PUT"])
@require_account_access
def update_business_hours(account_id: int, account: WhatsAppAccount, workspace_id: str):
    """
    Update business hours configuration.
    
    Request body:
    {
        "timezone": "Asia/Kolkata",
        "is_enabled": true,
        "schedule": {
            "monday": {"enabled": true, "start": "09:00", "end": "18:00"},
            "tuesday": {"enabled": true, "start": "09:00", "end": "18:00"},
            ...
        },
        "exceptions": [
            {"date": "2024-12-25", "is_closed": true, "reason": "Christmas"}
        ]
    }
    """
    try:
        data = request.get_json()
        if not data:
            return jsonify({"error": "Request body required"}), 400
        
        # Get or create
        hours = WhatsAppBusinessHours.query.filter_by(
            workspace_id=str(workspace_id),
            account_id=account_id
        ).first()
        
        if not hours:
            hours = WhatsAppBusinessHours(
                workspace_id=str(workspace_id),
                account_id=account_id
            )
            db.session.add(hours)
        
        # Update fields
        if "timezone" in data:
            hours.timezone = data["timezone"]
        if "is_enabled" in data:
            hours.is_enabled = data["is_enabled"]
        if "schedule" in data:
            hours.schedule = data["schedule"]
        if "exceptions" in data:
            hours.exceptions = data["exceptions"]
        
        hours.updated_at = datetime.now(timezone.utc)
        db.session.commit()
        
        logger.info(f"Updated business hours for account {account_id}")
        
        return jsonify({
            "success": True,
            "business_hours": hours.to_dict(),
            "message": "Business hours updated successfully"
        })
        
    except Exception as e:
        db.session.rollback()
        logger.exception(f"Error updating business hours: {e}")
        return jsonify({"error": "Failed to update business hours"}), 500


# ============================================================
# Quick Setup Endpoints
# ============================================================

@automation_bp.route("/accounts/<int:account_id>/automation/quick-setup/welcome", methods=["POST"])
@require_account_access
def quick_setup_welcome(account_id: int, account: WhatsAppAccount, workspace_id: str):
    """
    Quick setup for welcome message automation.
    
    Request body:
    {
        "message": "Hello! Thanks for reaching out. How can I help you today?"
    }
    """
    try:
        data = request.get_json()
        message = data.get("message") if data else None
        
        if not message:
            return jsonify({"error": "Message is required"}), 400
        
        # Check if welcome rule already exists
        existing = WhatsAppAutomationRule.query.filter_by(
            workspace_id=workspace_id,
            account_id=account_id,
            rule_type="welcome",
            status="active"
        ).first()
        
        if existing:
            # Update existing
            existing.response_config = {"message": message}
            existing.is_active = True
            existing.updated_at = datetime.now(timezone.utc)
            rule = existing
            created = False
        else:
            # Create new
            rule = WhatsAppAutomationRule(
                workspace_id=workspace_id,
                account_id=account_id,
                name="Welcome Message",
                description="Auto-reply to first message from new contacts",
                rule_type="welcome",
                trigger_config={},
                response_type="text",
                response_config={"message": message},
                is_active=True,
                priority=1,
                cooldown_seconds=86400,  # 24 hours
                max_triggers_per_day=1,
            )
            db.session.add(rule)
            created = True
        
        db.session.commit()
        
        return jsonify({
            "success": True,
            "rule": rule.to_dict(),
            "created": created,
            "message": "Welcome message configured successfully"
        }), 201 if created else 200
        
    except Exception as e:
        db.session.rollback()
        logger.exception(f"Error setting up welcome message: {e}")
        return jsonify({"error": "Failed to setup welcome message"}), 500


@automation_bp.route("/accounts/<int:account_id>/automation/quick-setup/away", methods=["POST"])
@require_account_access
def quick_setup_away(account_id: int, account: WhatsAppAccount, workspace_id: str):
    """
    Quick setup for away message automation.
    
    Request body:
    {
        "message": "Thanks for your message! We're currently away but will get back to you soon.",
        "timezone": "Asia/Kolkata",
        "schedule": {
            "monday": {"enabled": true, "start": "09:00", "end": "18:00"},
            ...
        }
    }
    """
    try:
        data = request.get_json()
        if not data:
            return jsonify({"error": "Request body required"}), 400
        
        message = data.get("message")
        timezone_str = data.get("timezone", "UTC")
        schedule = data.get("schedule")
        
        if not message:
            return jsonify({"error": "Message is required"}), 400
        
        # Setup business hours if schedule provided
        if schedule:
            hours = WhatsAppBusinessHours.query.filter_by(
                workspace_id=str(workspace_id),
                account_id=account_id
            ).first()
            
            if not hours:
                hours = WhatsAppBusinessHours(
                    workspace_id=str(workspace_id),
                    account_id=account_id
                )
                db.session.add(hours)
            
            hours.timezone = timezone_str
            hours.schedule = schedule
            hours.is_enabled = True
            hours.updated_at = datetime.now(timezone.utc)
        
        # Check if away rule already exists
        existing = WhatsAppAutomationRule.query.filter_by(
            workspace_id=workspace_id,
            account_id=account_id,
            rule_type="away",
            status="active"
        ).first()
        
        if existing:
            existing.response_config = {"message": message}
            existing.is_active = True
            existing.updated_at = datetime.now(timezone.utc)
            rule = existing
            created = False
        else:
            rule = WhatsAppAutomationRule(
                workspace_id=workspace_id,
                account_id=account_id,
                name="Away Message",
                description="Auto-reply when outside business hours",
                rule_type="away",
                trigger_config={},
                response_type="text",
                response_config={"message": message},
                is_active=True,
                priority=2,
                cooldown_seconds=3600,  # 1 hour
                max_triggers_per_day=3,
            )
            db.session.add(rule)
            created = True
        
        db.session.commit()
        
        return jsonify({
            "success": True,
            "rule": rule.to_dict(),
            "created": created,
            "message": "Away message configured successfully"
        }), 201 if created else 200
        
    except Exception as e:
        db.session.rollback()
        logger.exception(f"Error setting up away message: {e}")
        return jsonify({"error": "Failed to setup away message"}), 500


@automation_bp.route("/accounts/<int:account_id>/automation/quick-setup/keyword", methods=["POST"])
@require_account_access
def quick_setup_keyword(account_id: int, account: WhatsAppAccount, workspace_id: str):
    """
    Quick setup for keyword-triggered automation.
    
    Request body:
    {
        "name": "Pricing Info",
        "keywords": ["price", "pricing", "cost", "how much"],
        "message": "Our pricing starts at $99/month. Visit example.com/pricing for details.",
        "match_type": "contains"
    }
    """
    try:
        data = request.get_json()
        if not data:
            return jsonify({"error": "Request body required"}), 400
        
        name = data.get("name")
        keywords = data.get("keywords", [])
        message = data.get("message")
        match_type = data.get("match_type", "contains")
        
        if not name or not keywords or not message:
            return jsonify({"error": "name, keywords, and message are required"}), 400
        
        # Create keyword rule
        rule = WhatsAppAutomationRule(
            workspace_id=workspace_id,
            account_id=account_id,
            name=name,
            description=f"Auto-reply when message contains: {', '.join(keywords[:3])}...",
            rule_type="keyword",
            trigger_config={
                "keywords": keywords,
                "match_type": match_type,
                "case_sensitive": False
            },
            response_type="text",
            response_config={"message": message},
            is_active=True,
            priority=50,
            cooldown_seconds=300,  # 5 minutes
        )
        
        db.session.add(rule)
        db.session.commit()
        
        return jsonify({
            "success": True,
            "rule": rule.to_dict(),
            "message": "Keyword automation created successfully"
        }), 201
        
    except Exception as e:
        db.session.rollback()
        logger.exception(f"Error setting up keyword automation: {e}")
        return jsonify({"error": "Failed to setup keyword automation"}), 500


# ============================================================
# Contact Automation Overrides (Per-Contact Toggles)
# ============================================================

@automation_bp.route("/accounts/<int:account_id>/automation/contact/<int:conversation_id>/overrides", methods=["GET"])
@require_account_access
def get_contact_automation_overrides(account_id: int, conversation_id: int, account: WhatsAppAccount, workspace_id: str):
    """
    Get automation override settings for a specific contact.
    
    Returns all automation types with their enabled/disabled status.
    Default is True (enabled) for all types.
    
    Response:
    {
        "success": true,
        "overrides": {
            "welcome": true,
            "away": true,
            "command": true,
            "keyword": false,  // disabled for this contact
            "faq": true,
            "ai_chat": true
        }
    }
    """
    try:
        # Verify conversation belongs to this account
        conversation = WhatsAppConversation.query.filter_by(
            id=conversation_id,
            account_id=account_id
        ).first()
        
        if not conversation:
            return jsonify({"error": "Conversation not found"}), 404
        
        overrides = get_contact_overrides(workspace_id, conversation_id)
        attr = conversation.attribution_data if isinstance(conversation.attribution_data, dict) else {}

        return jsonify({
            "success": True,
            "overrides": overrides,
            "conversation_id": conversation_id,
            "ai_paused_by_agent": bool(attr.get("ai_paused_by_agent")),
        })
        
    except Exception as e:
        logger.exception(f"Error getting contact overrides: {e}")
        return jsonify({"error": "Failed to get contact overrides"}), 500


@automation_bp.route("/accounts/<int:account_id>/automation/contact/<int:conversation_id>/overrides", methods=["PUT"])
@require_account_access
def update_contact_automation_overrides(account_id: int, conversation_id: int, account: WhatsAppAccount, workspace_id: str):
    """
    Update automation override settings for a specific contact.
    
    Request body:
    {
        "rule_type": "welcome",
        "is_enabled": false
    }
    
    Or bulk update:
    {
        "overrides": {
            "welcome": false,
            "keyword": false
        }
    }
    """
    try:
        # Verify conversation belongs to this account
        conversation = WhatsAppConversation.query.filter_by(
            id=conversation_id,
            account_id=account_id
        ).first()
        
        if not conversation:
            return jsonify({"error": "Conversation not found"}), 404
        
        data = request.get_json()
        if not data:
            return jsonify({"error": "Request body required"}), 400
        
        # Handle bulk update
        if "overrides" in data:
            for rule_type, is_enabled in data["overrides"].items():
                success = set_contact_override(
                    workspace_id=workspace_id,
                    account_id=account_id,
                    conversation_id=conversation_id,
                    rule_type=rule_type,
                    is_enabled=is_enabled
                )
                if not success:
                    return jsonify({"error": f"Failed to update {rule_type}"}), 500
        
        # Handle single update
        elif "rule_type" in data:
            rule_type = data["rule_type"]
            is_enabled = data.get("is_enabled", True)
            success = set_contact_override(
                workspace_id=workspace_id,
                account_id=account_id,
                conversation_id=conversation_id,
                rule_type=rule_type,
                is_enabled=is_enabled,
            )
            if not success:
                return jsonify({"error": "Failed to update override"}), 500
            if rule_type in ("ai_chat", "interactive_flows") and is_enabled:
                from .automation_models import clear_agent_ai_pause_flag
                clear_agent_ai_pause_flag(conversation_id)
        
        else:
            return jsonify({"error": "Either 'overrides' or 'rule_type' required"}), 400
        
        # Return updated overrides
        overrides = get_contact_overrides(workspace_id, conversation_id)
        attr = conversation.attribution_data if isinstance(conversation.attribution_data, dict) else {}
        if "overrides" in data and (
            data["overrides"].get("ai_chat") is True
            or data["overrides"].get("interactive_flows") is True
        ):
            from .automation_models import clear_agent_ai_pause_flag
            clear_agent_ai_pause_flag(conversation_id)
            conversation = WhatsAppConversation.query.get(conversation_id) or conversation
            attr = conversation.attribution_data if isinstance(conversation.attribution_data, dict) else {}
        
        logger.info(f"Updated contact overrides for conversation {conversation_id}")
        
        return jsonify({
            "success": True,
            "overrides": overrides,
            "ai_paused_by_agent": bool(attr.get("ai_paused_by_agent")),
            "message": "Overrides updated successfully"
        })
        
    except Exception as e:
        logger.exception(f"Error updating contact overrides: {e}")
        return jsonify({"error": "Failed to update contact overrides"}), 500

