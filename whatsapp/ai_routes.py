"""
WhatsApp AI Routes
==================

API endpoints for AI chatbot configuration and testing.

Endpoints:
- GET /api/whatsapp/accounts/{id}/ai/config - Get AI configuration
- POST /api/whatsapp/accounts/{id}/ai/config - Update AI configuration  
- POST /api/whatsapp/accounts/{id}/ai/test - Test AI response
- POST /api/whatsapp/accounts/{id}/ai/enable - Enable AI chatbot
- POST /api/whatsapp/accounts/{id}/ai/disable - Disable AI chatbot
"""

import logging
import os
from functools import wraps
from flask import Blueprint, jsonify, request
from .http_rate_limit import rate_limit

from shared_models import db
from .automation_models import WhatsAppAutomationRule
from .models import WhatsAppAccount, upsert_whatsapp_bot_settings_shadow
from .ai_chatbot import (
    DEFAULT_SYSTEM_PROMPT,
    DEFAULT_MAX_OUTPUT_TOKENS,
    get_bot_config,
    prepare_automation_ai,
    create_ai_chatbot,
)

logger = logging.getLogger(__name__)

# Blueprint for AI routes
ai_bp = Blueprint('whatsapp_ai', __name__, url_prefix='/api/whatsapp/accounts')


def _get_ai_rule_for_account(workspace_id: str, account_id: int) -> WhatsAppAutomationRule | None:
    """
    Return the newest AI chat rule and auto-disable older duplicates.
    """
    rules = (
        WhatsAppAutomationRule.query.filter_by(
            workspace_id=workspace_id,
            account_id=account_id,
            rule_type="ai_chat",
        )
        .order_by(WhatsAppAutomationRule.id.desc())
        .all()
    )
    if not rules:
        return None

    primary = rules[0]
    if len(rules) > 1:
        for duplicate in rules[1:]:
            if duplicate.is_active or duplicate.status != "paused":
                duplicate.is_active = False
                duplicate.status = "paused"
        logger.warning(
            "Found %s duplicate ai_chat rules for account %s; kept rule %s active",
            len(rules) - 1,
            account_id,
            primary.id,
        )
    return primary


# ============================================================
# Decorators
# ============================================================

def require_account_access(f):
    """
    Decorator to verify account access and inject account + workspace_id.
    
    Matches the pattern used in automation_routes.py.
    Expects `account_id` in the URL parameters.
    """
    @wraps(f)
    def decorated_function(*args, **kwargs):
        account_id = kwargs.get("account_id")
        
        if not account_id:
            return jsonify({"error": "Account ID required"}), 400
        
        # Get account
        account = WhatsAppAccount.query.get(account_id)
        
        if not account:
            return jsonify({"error": "Account not found"}), 404
        
        # Use workspace from account (same as automation_routes.py)
        workspace_id = account.workspace_id
        
        kwargs["account"] = account
        kwargs["workspace_id"] = workspace_id
        
        return f(*args, **kwargs)
    
    return decorated_function


# ============================================================
# AI Configuration Endpoints
# ============================================================

@ai_bp.route('/<int:account_id>/ai/config', methods=['GET'])
@require_account_access
def get_ai_config(account_id: int, account: WhatsAppAccount, workspace_id: str):
    """
    Get AI chatbot configuration for an account.
    
    Returns:
        AI configuration including enabled status, system prompt, etc.
    """
    try:
        # Find the AI_CHAT rule for this account
        ai_rule = _get_ai_rule_for_account(workspace_id, account.id)
        
        if not ai_rule:
            # Return default config if no AI rule exists
            bot = get_bot_config(account)
            return jsonify({
                "enabled": False,
                "system_prompt": DEFAULT_SYSTEM_PROMPT,
                "fallback_message": "I'm sorry, I couldn't process your request. A team member will assist you soon.",
                "max_tokens": DEFAULT_MAX_OUTPUT_TOKENS,
                "temperature": 0.7,
                "context_messages": 5,
                "priority": 999,  # Low priority (AI is usually fallback)
                "ai_model": bot.get("ai_model"),
                "knowledge_base_id": bot.get("kb_id"),
            }), 200
        
        # Extract config from the rule
        response_config = ai_rule.response_config or {}
        bot = get_bot_config(account)
        
        return jsonify({
            "enabled": ai_rule.is_active,
            "rule_id": ai_rule.id,
            "system_prompt": response_config.get("system_prompt", DEFAULT_SYSTEM_PROMPT),
            "fallback_message": response_config.get("fallback_message", "I'm sorry, I couldn't process your request."),
            "max_tokens": response_config.get("max_tokens", DEFAULT_MAX_OUTPUT_TOKENS),
            "temperature": response_config.get("temperature", 0.7),
            "context_messages": response_config.get("context_messages", 5),
            "priority": ai_rule.priority,
            "trigger_count": ai_rule.trigger_count,
            "last_triggered_at": ai_rule.last_triggered_at.isoformat() + "Z" if ai_rule.last_triggered_at else None,
            "ai_model": bot.get("ai_model") or response_config.get("model"),
            "knowledge_base_id": bot.get("kb_id") or response_config.get("knowledge_base_id"),
        }), 200
        
    except Exception as e:
        logger.exception(f"Error getting AI config: {e}")
        return jsonify({"error": "Failed to get AI configuration"}), 500


@ai_bp.route('/<int:account_id>/ai/config', methods=['POST'])
@require_account_access
def update_ai_config(account_id: int, account: WhatsAppAccount, workspace_id: str):
    """
    Update AI chatbot configuration.
    
    Request body:
        {
            "enabled": true,
            "system_prompt": "...",
            "fallback_message": "...",
            "max_tokens": DEFAULT_MAX_OUTPUT_TOKENS,
            "temperature": 0.7,
            "context_messages": 5
        }
    """
    try:
        data = request.get_json() or {}
        
        # Find or create AI rule
        ai_rule = _get_ai_rule_for_account(workspace_id, account.id)
        
        if not ai_rule:
            # Create new AI rule
            ai_rule = WhatsAppAutomationRule(
                workspace_id=workspace_id,
                account_id=account.id,
                name="AI Chatbot",
                description="AI-powered conversational responses using Gemini",
                rule_type="ai_chat",
                status="active",
                is_active=data.get("enabled", False),
                trigger_config={},  # AI_CHAT always matches
                response_type="ai",
                response_config={
                    "system_prompt": data.get("system_prompt", DEFAULT_SYSTEM_PROMPT),
                    "fallback_message": data.get("fallback_message", "I'm sorry, I couldn't process your request."),
                    "max_tokens": data.get("max_tokens", DEFAULT_MAX_OUTPUT_TOKENS),
                    "temperature": data.get("temperature", 0.7),
                    "context_messages": data.get("context_messages", 5),
                },
                priority=999,  # Low priority - AI is fallback
                cooldown_seconds=0,
                max_triggers_per_day=0,
            )
            db.session.add(ai_rule)
            logger.info(f"Created AI chatbot rule for account {account.id}")
        else:
            # Update existing rule
            if "enabled" in data:
                ai_rule.is_active = data["enabled"]
                ai_rule.status = "active" if data["enabled"] else "paused"
            
            # Update response config
            response_config = ai_rule.response_config or {}
            
            if "system_prompt" in data:
                response_config["system_prompt"] = data["system_prompt"]
            if "fallback_message" in data:
                response_config["fallback_message"] = data["fallback_message"]
            if "max_tokens" in data:
                response_config["max_tokens"] = data["max_tokens"]
            if "temperature" in data:
                response_config["temperature"] = data["temperature"]
            if "context_messages" in data:
                response_config["context_messages"] = data["context_messages"]
            
            ai_rule.response_config = response_config
            
            logger.info(f"Updated AI chatbot config for account {account.id}")
        
        rc = ai_rule.response_config or {}
        upsert_whatsapp_bot_settings_shadow(
            account,
            ai_enabled=ai_rule.is_active,
            ai_model=rc.get("model"),
            temperature=rc.get("temperature"),
            max_tokens=rc.get("max_tokens"),
            prompt_override=rc.get("system_prompt"),
            knowledge_base_id=rc.get("knowledge_base_id"),
        )

        db.session.commit()
        
        return jsonify({
            "success": True,
            "message": "AI configuration updated",
            "rule_id": ai_rule.id,
            "enabled": ai_rule.is_active,
        }), 200
        
    except Exception as e:
        db.session.rollback()
        logger.exception(f"Error updating AI config: {e}")
        return jsonify({"error": "Failed to update AI configuration"}), 500


@ai_bp.route('/<int:account_id>/ai/test', methods=['POST'])
@rate_limit("whatsapp.ai.test")
@require_account_access
def test_ai_response(account_id: int, account: WhatsAppAccount, workspace_id: str):
    """
    Test AI response without sending to WhatsApp.
    
    This test endpoint uses RAG integration if the knowledge base is indexed.
    
    Request body:
        {
            "message": "Hello, what services do you offer?",
            "context": [{"role": "user", "text": "..."}],  // Optional
            "use_rag": true  // Optional, default true
        }
    """
    try:
        data = request.get_json() or {}
        message = data.get("message", "")
        context = data.get("context", [])
        use_rag = data.get("use_rag", True)  # Allow disabling RAG for comparison
        
        if not message:
            return jsonify({"error": "Message is required"}), 400
        
        # Get AI config for this account (automation rule + bot_settings slice)
        ai_rule = _get_ai_rule_for_account(workspace_id, account.id)
        
        rc = dict(ai_rule.response_config) if ai_rule and ai_rule.response_config else {}
        rc["use_rag"] = use_rag
        prep = prepare_automation_ai(account, rc)
        if not prep["run"]:
            return jsonify({
                "success": False,
                "error": "AI is disabled at account/bot_settings level",
                "message": prep["fallback_message"],
            }), 200
        
        chatbot = create_ai_chatbot(prep["config"])
        result = chatbot.generate_response(message=message, context=context)
        
        return jsonify({
            "success": result.success,
            "message": result.message,
            "tokens_used": result.tokens_used,
            "response_time_ms": result.response_time_ms,
            "error": result.error,
            "rag_enabled": use_rag,
            "workspace_id": workspace_id,
        }), 200
        
    except Exception as e:
        logger.exception(f"Error testing AI response: {e}")
        return jsonify({"error": "Failed to test AI response"}), 500


@ai_bp.route('/<int:account_id>/ai/enable', methods=['POST'])
@require_account_access
def enable_ai(account_id: int, account: WhatsAppAccount, workspace_id: str):
    """Quick endpoint to enable AI chatbot."""
    try:
        ai_rule = _get_ai_rule_for_account(workspace_id, account.id)
        
        if not ai_rule:
            # Create default AI rule
            ai_rule = WhatsAppAutomationRule(
                workspace_id=workspace_id,
                account_id=account.id,
                name="AI Chatbot",
                description="AI-powered conversational responses",
                rule_type="ai_chat",
                status="active",
                is_active=True,
                trigger_config={},
                response_type="ai",
                response_config={
                    "system_prompt": DEFAULT_SYSTEM_PROMPT,
                    "fallback_message": "I'm sorry, I couldn't process your request. A team member will assist you soon.",
                    "max_tokens": DEFAULT_MAX_OUTPUT_TOKENS,
                    "temperature": 0.7,
                    "context_messages": 5,
                },
                priority=999,
            )
            db.session.add(ai_rule)
        else:
            ai_rule.is_active = True
            ai_rule.status = "active"

        rc = ai_rule.response_config or {}
        upsert_whatsapp_bot_settings_shadow(
            account,
            ai_enabled=True,
            ai_model=rc.get("model"),
            temperature=rc.get("temperature"),
            max_tokens=rc.get("max_tokens"),
            prompt_override=rc.get("system_prompt"),
            knowledge_base_id=rc.get("knowledge_base_id"),
        )
        
        db.session.commit()
        
        return jsonify({
            "success": True,
            "message": "AI chatbot enabled",
            "rule_id": ai_rule.id,
        }), 200
        
    except Exception as e:
        db.session.rollback()
        logger.exception(f"Error enabling AI: {e}")
        return jsonify({"error": "Failed to enable AI"}), 500


@ai_bp.route('/<int:account_id>/ai/disable', methods=['POST'])
@require_account_access
def disable_ai(account_id: int, account: WhatsAppAccount, workspace_id: str):
    """Quick endpoint to disable AI chatbot."""
    try:
        ai_rule = _get_ai_rule_for_account(workspace_id, account.id)
        
        if ai_rule:
            ai_rule.is_active = False
            ai_rule.status = "paused"
            upsert_whatsapp_bot_settings_shadow(account, ai_enabled=False)
            db.session.commit()
        
        return jsonify({
            "success": True,
            "message": "AI chatbot disabled",
        }), 200
        
    except Exception as e:
        db.session.rollback()
        logger.exception(f"Error disabling AI: {e}")
        return jsonify({"error": "Failed to disable AI"}), 500


# ============================================================
# Intent Detection Endpoints
# ============================================================

@ai_bp.route('/<int:account_id>/ai/intent', methods=['POST'])
@rate_limit("whatsapp.ai.intent")
@require_account_access
def classify_message_intent(account_id: int, account: WhatsAppAccount, workspace_id: str):
    """
    Classify the intent of a message.
    
    Request body:
        {
            "message": "Hello, I need help with my order"
        }
    """
    try:
        from .ai_chatbot import classify_intent, INTENT_TYPES
        
        data = request.get_json() or {}
        message = data.get("message", "")
        
        if not message:
            return jsonify({"error": "Message is required"}), 400
        
        # Classify intent (workspace-scoped so a tenant's own Gemini key is used)
        result = classify_intent(message, workspace_id=workspace_id)
        
        return jsonify({
            "success": result.success,
            "intent": result.intent,
            "confidence": result.confidence,
            "response_time_ms": result.response_time_ms,
            "available_intents": INTENT_TYPES,
            "error": result.error,
        }), 200
        
    except Exception as e:
        logger.exception(f"Error classifying intent: {e}")
        return jsonify({"error": "Failed to classify intent"}), 500


@ai_bp.route('/<int:account_id>/ai/intents', methods=['GET'])
@require_account_access
def get_available_intents(account_id: int, account: WhatsAppAccount, workspace_id: str):
    """Get list of available intent types."""
    from .ai_chatbot import INTENT_TYPES
    
    return jsonify({
        "success": True,
        "intents": [
            {"id": intent, "label": intent.replace("_", " ").title()}
            for intent in INTENT_TYPES
        ]
    })


# ============================================================
# Message Rewrite Endpoint  
# ============================================================

REWRITE_TONES = {
    "professional": "Rewrite in a professional, business-appropriate tone. Keep it polished and respectful.",
    "friendly": "Rewrite in a warm, friendly, and approachable tone. Add a touch of personality.",
    "casual": "Rewrite in a casual, relaxed tone like chatting with a friend.",
    "formal": "Rewrite in a formal, respectful tone suitable for official communication.",
    "empathetic": "Rewrite with empathy and understanding. Show care and concern for the recipient.",
    "concise": "Rewrite to be brief and to the point. Remove unnecessary words.",
}

@ai_bp.route("/<int:account_id>/ai/rewrite-message", methods=["POST"])
@rate_limit("whatsapp.ai.rewrite")
@require_account_access
def rewrite_message(account_id: int, account: WhatsAppAccount, workspace_id: str):
    """
    Rewrite a chat message in a specific tone using AI.
    
    Request body:
        {
            "message": "hey can u help me with this",
            "tone": "professional"  // professional, friendly, casual, formal, empathetic, concise
        }
    
    Returns:
        {
            "success": true,
            "original": "hey can u help me with this",
            "rewritten": "Hello! Could you please assist me with this?",
            "tone": "professional"
        }
    """
    from .ai_chatbot import get_genai_client
    from google.genai.types import GenerateContentConfig
    
    data = request.get_json() or {}
    message = data.get("message", "").strip()
    tone = data.get("tone", "professional").lower()
    
    if not message:
        return jsonify({"success": False, "error": "Message is required"}), 400
    
    if tone not in REWRITE_TONES:
        return jsonify({
            "success": False, 
            "error": f"Invalid tone. Choose from: {', '.join(REWRITE_TONES.keys())}"
        }), 400
    
    try:
        # Use the same client as the rest of the codebase, scoped to the
        # account's workspace so a tenant with its own Gemini key is billed on
        # their own AI quota (env fallback for T0000 / unconfigured tenants).
        client = get_genai_client(workspace_id=workspace_id)
        if not client:
            return jsonify({"success": False, "error": "AI not configured"}), 500
        
        tone_instruction = REWRITE_TONES[tone]
        
        prompt = f"""Rewrite this WhatsApp message for a customer service agent to send.

{tone_instruction}

RULES:
1. Keep the same meaning and intent
2. Do NOT add greetings unless the original has one
3. Do NOT add unnecessary filler words
4. Keep it concise and natural for WhatsApp
5. Return ONLY the rewritten message, nothing else

Original message: {message}

Rewritten message:"""

        response = client.models.generate_content(
            model=os.environ.get("TEXT_MODEL") or os.environ.get("GEMINI_MODEL") or "gemini-3.5-flash",
            contents=prompt,
            config=GenerateContentConfig(
                max_output_tokens=256,
                temperature=0.7
            )
        )
        
        rewritten = response.text.strip() if response.text else message
        
        # Clean up any quotes if the model added them
        if rewritten.startswith('"') and rewritten.endswith('"'):
            rewritten = rewritten[1:-1]
        if rewritten.startswith("'") and rewritten.endswith("'"):
            rewritten = rewritten[1:-1]
        
        return jsonify({
            "success": True,
            "original": message,
            "rewritten": rewritten,
            "tone": tone
        })
        
    except Exception as e:
        logger.exception("Message rewrite error")
        return jsonify({"success": False, "error": str(e)}), 500


@ai_bp.route("/<int:account_id>/ai/rewrite-tones", methods=["GET"])
@require_account_access
def get_rewrite_tones(account_id: int, account: WhatsAppAccount, workspace_id: str):
    """Get available rewrite tones."""
    return jsonify({
        "success": True,
        "tones": [
            {"id": tone_id, "label": tone_id.title(), "description": desc}
            for tone_id, desc in REWRITE_TONES.items()
        ]
    })


# ============================================================
# Register Blueprint Helper
# ============================================================

def register_ai_routes(app):
    """Register AI routes with Flask app."""
    app.register_blueprint(ai_bp)
    logger.info("Registered WhatsApp AI routes")
