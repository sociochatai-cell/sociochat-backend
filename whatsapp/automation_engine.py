"""
WhatsApp Automation Engine
==========================

Core automation processing logic for a multi-tenant SaaS environment.

Design Principles:
- Fail-safe: Automation errors never break message reception
- Multi-tenant: All queries scoped by workspace_id
- Rate-limited: Prevents spam with cooldowns
- Audited: All automation triggers are logged
- Async-ready: Designed for future async processing

Usage:
    engine = AutomationEngine(account_id, workspace_id)
    response = engine.process_incoming_message(message, conversation)
"""

import logging
import os
import re
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, Optional, List, Tuple

from models import db
from notifications import notification_manager
from .automation_models import (
    WhatsAppAutomationRule,
    WhatsAppAutomationLog,
    WhatsAppBusinessHours,
    is_automation_disabled_for_contact,
)

logger = logging.getLogger(__name__)


class AutomationEngine:
    """
    Processes incoming messages against automation rules.
    
    Multi-tenant safe: All operations scoped to workspace_id.
    """
    
    def __init__(self, account_id: int, workspace_id: str):
        """
        Initialize automation engine for a specific account.
        
        Args:
            account_id: WhatsApp account ID
            workspace_id: Workspace ID for multi-tenant isolation
        """
        self.account_id = account_id
        self.workspace_id = workspace_id
        self._rules_cache: Optional[List[WhatsAppAutomationRule]] = None
        self._business_hours: Optional[WhatsAppBusinessHours] = None
    
    def process_incoming_message(
        self,
        message_text: str,
        conversation_id: int,
        is_first_message: bool,
        message_id: Optional[int] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Process incoming message and return automation response if any rule matches.
        
        Args:
            message_text: The text content of the incoming message
            conversation_id: Conversation ID for logging
            is_first_message: Whether this is first message from this contact
            message_id: ID of the trigger message (for logging)
            
        Returns:
            Response dict if rule matched, None otherwise
            {
                "rule_id": 123,
                "response_type": "text",
                "response_config": {...},
                "matched_keyword": "hello"
            }
        """
        try:
            # Get active rules for this account, ordered by priority
            rules = self._get_active_rules()
            
            if not rules:
                logger.debug(f"No automation rules for account {self.account_id}")
                return None
            
            # Check rules in priority order
            for rule in rules:
                match_result = self._check_rule_match(
                    rule, 
                    message_text, 
                    is_first_message,
                    conversation_id
                )
                
                if match_result["matched"]:
                    # Check per-contact override (allow disabling automations for specific contacts)
                    try:
                        if is_automation_disabled_for_contact(self.workspace_id, conversation_id, rule.rule_type):
                            logger.debug(f"Rule {rule.id} ({rule.rule_type}) disabled for conversation {conversation_id}")
                            continue
                    except Exception as e:
                        logger.warning(f"Failed to check contact override: {e}")
                        db.session.rollback()
                    
                    # Check rate limiting
                    if not self._check_rate_limit(rule, conversation_id):
                        logger.debug(f"Rule {rule.id} rate limited for conversation {conversation_id}")
                        continue
                    
                    # Log the trigger
                    self._log_trigger(
                        rule=rule,
                        conversation_id=conversation_id,
                        message_id=message_id,
                        trigger_text=message_text[:500],  # Truncate for storage
                        matched_keyword=match_result.get("matched_keyword")
                    )
                    
                    # Update rule statistics
                    rule.trigger_count = (rule.trigger_count or 0) + 1
                    rule.last_triggered_at = datetime.now(timezone.utc)
                    
                    try:
                        db.session.commit()
                    except Exception as e:
                        logger.warning(f"Failed to update rule stats: {e}")
                        db.session.rollback()
                    
                    logger.info(f"[Automation Source: {rule.rule_type.upper()}] Rule {rule.id} '{rule.name}' triggered for conversation {conversation_id}")
                    
                    return {
                        "rule_id": rule.id,
                        "rule_name": rule.name,
                        "response_type": rule.response_type,
                        "response_config": rule.response_config,
                        "matched_keyword": match_result.get("matched_keyword"),
                        # FAQ-specific info (if FAQ rule matched)
                        "faq_id": match_result.get("faq_id"),
                        "faq_answer": match_result.get("faq_answer"),
                    }
            
            return None
            
        except Exception as e:
            # CRITICAL: Never let automation errors break message processing
            logger.exception(f"Automation engine error: {e}")
            try:
                db.session.rollback()
            except Exception:
                pass
            return None
    
    def _get_active_rules(self) -> List[WhatsAppAutomationRule]:
        """
        Get active automation rules for this account.
        
        Returns rules ordered by priority (lower number = higher priority).
        """
        if self._rules_cache is not None:
            return self._rules_cache
        
        try:
            rules = WhatsAppAutomationRule.query.filter_by(
                workspace_id=self.workspace_id,
                account_id=self.account_id,
                is_active=True,
                status="active"
            ).order_by(
                WhatsAppAutomationRule.priority.asc()
            ).all()
            
            self._rules_cache = rules
            return rules
        except Exception as e:
            logger.exception(f"Failed to fetch automation rules: {e}")
            try:
                db.session.rollback()
            except Exception:
                pass
            return []
    
    def _check_rule_match(
        self,
        rule: WhatsAppAutomationRule,
        message_text: str,
        is_first_message: bool,
        conversation_id: int
    ) -> Dict[str, Any]:
        """
        Check if a rule matches the incoming message.
        
        Returns:
            {"matched": bool, "matched_keyword": str or None}
        """
        rule_type = rule.rule_type
        trigger_config = rule.trigger_config or {}
        
        # WELCOME: Triggers on first message from new contact
        if rule_type == "welcome":
            if is_first_message:
                return {"matched": True, "matched_keyword": None}
            return {"matched": False}
        
        # AWAY: Triggers outside business hours
        if rule_type == "away":
            if not self._is_within_business_hours():
                return {"matched": True, "matched_keyword": None}
            return {"matched": False}
        
        # COMMAND: Triggers on slash commands
        if rule_type == "command":
            command = trigger_config.get("command", "").lower().strip()
            aliases = trigger_config.get("aliases", [])
            
            text_lower = message_text.lower().strip()
            
            # Check main command
            if command and (text_lower == command or text_lower.startswith(command + " ")):
                return {"matched": True, "matched_keyword": command}
            
            # Check aliases
            for alias in aliases:
                alias_lower = alias.lower().strip()
                if text_lower == alias_lower or text_lower.startswith(alias_lower + " "):
                    return {"matched": True, "matched_keyword": alias}
            
            return {"matched": False}
        
        # KEYWORD: Triggers on keyword match
        if rule_type == "keyword":
            keywords = trigger_config.get("keywords", [])
            match_type = trigger_config.get("match_type", "contains")  # contains, exact, starts_with
            case_sensitive = trigger_config.get("case_sensitive", False)
            
            text_to_check = message_text if case_sensitive else message_text.lower()
            
            for keyword in keywords:
                kw = keyword if case_sensitive else keyword.lower()
                
                if match_type == "exact":
                    if text_to_check.strip() == kw.strip():
                        return {"matched": True, "matched_keyword": keyword}
                elif match_type == "starts_with":
                    if text_to_check.strip().startswith(kw.strip()):
                        return {"matched": True, "matched_keyword": keyword}
                else:  # contains
                    if kw in text_to_check:
                        return {"matched": True, "matched_keyword": keyword}
            
            return {"matched": False}
        
        # DEFAULT: Always matches (used as fallback)
        if rule_type == "default":
            return {"matched": True, "matched_keyword": None}
        
        # FAQ: Check FAQ database for matching entries
        if rule_type == "faq":
            try:
                from .faq_models import find_matching_faq
                
                matched_faq = find_matching_faq(
                    workspace_id=self.workspace_id,
                    account_id=self.account_id,
                    message=message_text
                )
                
                if matched_faq:
                    # Store matched FAQ info for response handler
                    return {
                        "matched": True,
                        "matched_keyword": matched_faq.question[:50],
                        "faq_id": matched_faq.id,
                        "faq_answer": matched_faq.answer
                    }
            except Exception as e:
                logger.exception(f"FAQ matching error: {e}")
                try:
                    db.session.rollback()
                except Exception:
                    pass
            
            return {"matched": False}
        
        # AI_CHAT: Always matches, generates AI response
        # Typically used as low-priority fallback with response_type="ai"
        if rule_type == "ai_chat":
            return {"matched": True, "matched_keyword": None}
        
        return {"matched": False}
    
    def _is_within_business_hours(self) -> bool:
        """
        Check if current time is within business hours.
        
        Returns True if:
        - Business hours not configured
        - Business hours disabled
        - Current time is within configured hours
        """
        try:
            if self._business_hours is None:
                self._business_hours = WhatsAppBusinessHours.query.filter_by(
                    workspace_id=self.workspace_id,
                    account_id=self.account_id
                ).first()
            
            if self._business_hours is None:
                return True  # No config = always "within hours"
            
            return self._business_hours.is_within_business_hours()
            
        except Exception as e:
            logger.exception(f"Business hours check error: {e}")
            try:
                db.session.rollback()
            except Exception:
                pass
            return True  # On error, assume within hours
    
    def _check_rate_limit(self, rule: WhatsAppAutomationRule, conversation_id: int) -> bool:
        """
        Check if rule can be triggered (rate limiting).
        
        Returns:
            True if rule can be triggered, False if rate limited
        """
        try:
            # Check cooldown
            cooldown = rule.cooldown_seconds or 0
            if cooldown > 0:
                cutoff = datetime.now(timezone.utc) - timedelta(seconds=cooldown)
                
                recent_trigger = WhatsAppAutomationLog.query.filter(
                    WhatsAppAutomationLog.rule_id == rule.id,
                    WhatsAppAutomationLog.conversation_id == conversation_id,
                    WhatsAppAutomationLog.created_at > cutoff
                ).first()
                
                if recent_trigger:
                    return False
            
            # Check daily limit
            max_per_day = rule.max_triggers_per_day or 0
            if max_per_day > 0:
                today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
                
                today_count = WhatsAppAutomationLog.query.filter(
                    WhatsAppAutomationLog.rule_id == rule.id,
                    WhatsAppAutomationLog.conversation_id == conversation_id,
                    WhatsAppAutomationLog.created_at >= today_start
                ).count()
                
                if today_count >= max_per_day:
                    return False
            
            return True
        except Exception as e:
            logger.warning(f"Rate limit check error: {e}")
            try:
                db.session.rollback()
            except Exception:
                pass
            return True  # On error, allow the trigger
    
    def _log_trigger(
        self,
        rule: WhatsAppAutomationRule,
        conversation_id: int,
        message_id: Optional[int],
        trigger_text: str,
        matched_keyword: Optional[str]
    ):
        """
        Log automation trigger for audit and analytics.
        """
        try:
            log = WhatsAppAutomationLog(
                workspace_id=self.workspace_id,
                rule_id=rule.id,
                conversation_id=conversation_id,
                trigger_message_id=message_id,
                trigger_text=trigger_text,
                matched_keyword=matched_keyword,
                response_success=True,  # Will be updated after response sent
            )
            db.session.add(log)
            # Don't commit here - let caller handle transaction
        except Exception as e:
            logger.exception(f"Failed to log automation trigger: {e}")
            try:
                db.session.rollback()
            except Exception:
                pass


def check_is_first_message(conversation_id: int) -> bool:
    """
    Check if this is the first message in a conversation.
    
    Uses message count to determine.
    """
    from .models import WhatsAppMessage
    
    try:
        # Count incoming messages in this conversation
        incoming_count = WhatsAppMessage.query.filter_by(
            conversation_id=conversation_id,
            direction="incoming"
        ).count()
        
        # If 0 or 1 (the current one), it's first message
        return incoming_count <= 1
    except Exception as e:
        logger.exception(f"Error checking first message: {e}")
        try:
            db.session.rollback()
        except Exception:
            pass
        return False


def _get_conversation_context(conversation_id: int, max_messages: int = 5) -> List[Dict[str, str]]:
    """
    Get recent conversation messages for AI context.
    
    Args:
        conversation_id: Conversation ID
        max_messages: Maximum number of previous messages to retrieve
        
    Returns:
        List of message dicts: [{"role": "user"|"model", "text": "..."}]
    """
    from .models import WhatsAppMessage
    
    try:
        # Get recent messages, ordered by created_at descending
        messages = WhatsAppMessage.query.filter_by(
            conversation_id=conversation_id
        ).order_by(
            WhatsAppMessage.created_at.desc()
        ).limit(max_messages + 1).all()  # +1 to skip current message
        
        # Reverse to get chronological order (oldest first)
        messages = list(reversed(messages))
        
        # Convert to context format, skip the latest (current) message
        context = []
        for msg in messages[:-1]:  # Exclude the last (current) message
            role = "user" if msg.direction == "incoming" else "model"
            text = ""
            
            # Extract text from content
            if isinstance(msg.content, dict):
                text = msg.content.get("text", "") or msg.content.get("body", "")
            elif isinstance(msg.content, str):
                text = msg.content
            
            if text:
                context.append({"role": role, "text": text})
        
        return context
        
    except Exception as e:
        logger.exception(f"Error getting conversation context: {e}")
        return []


def send_automation_response(
    account_id: int,
    conversation_id: int,
    response_type: str,
    response_config: Dict[str, Any],
    to_phone: str
) -> Tuple[bool, Optional[int], Optional[str]]:
    """
    Send the automated response.
    
    Args:
        account_id: WhatsApp account ID
        conversation_id: Conversation to respond to
        response_type: Type of response (text, template, interactive)
        response_config: Response configuration
        to_phone: Recipient phone number
        
    Returns:
        (success, message_id, error_message)
    """
    from .models import WhatsAppAccount, WhatsAppMessage
    from .services import WhatsAppService
    
    try:
        account = WhatsAppAccount.query.get(account_id)
        if not account:
            return False, None, "Account not found"
        
        access_token = account.get_access_token()
        if not access_token:
            return False, None, "No access token"
        
        service = WhatsAppService(
            access_token=access_token,
            phone_number_id=account.phone_number_id,
            waba_id=account.waba_id,
            workspace_id=account.workspace_id
        )
        
        result = None
        
        if response_type == "text":
            message_text = response_config.get("message", "")
            if message_text:
                result = service.send_text(to_phone, message_text)
        
        elif response_type == "template":
            template_name = response_config.get("template_name")
            language = response_config.get("language", "en_US")
            components = response_config.get("components")
            
            if template_name:
                result = service.send_template(
                    to=to_phone,
                    template_name=template_name,
                    language_code=language,
                    components=components
                )
        
        elif response_type == "interactive":
            # Interactive buttons or list
            interactive_type = response_config.get("type", "button")
            body_text = response_config.get("body", "")
            
            if interactive_type == "button":
                buttons = response_config.get("buttons", [])
                result = service.send_interactive_buttons(
                    to=to_phone,
                    body_text=body_text,
                    buttons=buttons,
                    header=response_config.get("header"),
                    footer=response_config.get("footer")
                )
            elif interactive_type == "list":
                sections = response_config.get("sections", [])
                button_text = response_config.get("button_text", "Select")
                result = service.send_interactive_list(
                    to=to_phone,
                    body_text=body_text,
                    button_text=button_text,
                    sections=sections,
                    header=response_config.get("header"),
                    footer=response_config.get("footer")
                )
        
        elif response_type == "faq":
            # FAQ response - just send the pre-matched FAQ answer
            faq_answer = response_config.get("faq_answer", "")
            faq_id = response_config.get("faq_id")
            
            if faq_answer:
                result = service.send_text(to_phone, faq_answer)
                
                # Update FAQ match count
                if faq_id:
                    try:
                        from .faq_models import WhatsAppFAQ
                        faq = WhatsAppFAQ.query.get(faq_id)
                        if faq:
                            faq.increment_match_count()
                            db.session.commit()
                    except Exception as faq_err:
                        logger.warning(f"Failed to update FAQ match count: {faq_err}")
                        try:
                            db.session.rollback()
                        except Exception:
                            pass
        
        elif response_type == "ai":
            # AI-powered response using Gemini with RAG integration
            from .ai_chatbot import create_ai_chatbot, DEFAULT_HANDOFF_MESSAGE
            
            business_name = account.custom_name or account.verified_name or "our business"
            
            # Get AI configuration from response_config, including workspace_id for RAG
            ai_config_dict = {
                "enabled": True,
                "business_name": business_name,
                "system_prompt": response_config.get("system_prompt", ""),
                "fallback_message": response_config.get(
                    "fallback_message", DEFAULT_HANDOFF_MESSAGE
                ),
                "model": response_config.get("model") or os.getenv("TEXT_MODEL", "gemini-3.1-flash-lite"),
                "max_tokens": response_config.get("max_tokens", 1024),
                "temperature": response_config.get("temperature", 0.3),
                "context_messages": response_config.get("context_messages", 5),
                # RAG configuration - enable knowledge base retrieval
                "use_rag": response_config.get("use_rag", True),
                "rag_top_k": response_config.get("rag_top_k", 5),
                "rag_confidence_threshold": response_config.get("rag_confidence_threshold", 0.5),
                "workspace_id": account.workspace_id,  # CRITICAL: Pass workspace_id for RAG
            }
            
            chatbot = create_ai_chatbot(ai_config_dict)
            
            # Get conversation context for better responses
            context = _get_conversation_context(conversation_id, ai_config_dict["context_messages"])
            
            # Get the incoming message from response_config
            incoming_message = response_config.get("incoming_message", "")
            
            # Generate AI response (now with RAG integration)
            ai_response = chatbot.generate_response(
                message=incoming_message,
                context=context
            )
            
            if ai_response.success and ai_response.message:
                result = service.send_text(to_phone, ai_response.message)
                logger.info(f"[Automation Source: AI CHATBOT + RAG] AI response sent: tokens={ai_response.tokens_used}, time={ai_response.response_time_ms}ms")
            else:
                # Use fallback message
                result = service.send_text(to_phone, ai_config_dict["fallback_message"])
                logger.warning(f"AI failed, using fallback: {ai_response.error}")
        
        if result and result.get("success"):
            # Get the stored message ID
            message_id = result.get("message_id")
            conversation_id = result.get("conversation_id")
            logger.info(f"Automation response sent successfully: {result.get('wamid')}")
            
            # Broadcast via SSE for real-time inbox update
            try:
                message_data = result.get("message")
                if message_data and conversation_id:
                    notification_manager.broadcast("whatsapp_message_received", {
                        "message": message_data,
                        "conversation_id": conversation_id,
                        "account_id": account_id,
                        "workspace_id": account.workspace_id
                    })
                    logger.info(f"Broadcasted automation response message: {message_id}")
            except Exception as e:
                logger.error(f"Failed to broadcast automation response event: {e}")
            
            return True, message_id, None
        else:
            error = result.get("error", "Unknown error") if result else "No response"
            logger.warning(f"Automation response failed: {error}")
            return False, None, error
            
    except Exception as e:
        logger.exception(f"Failed to send automation response: {e}")
        try:
            db.session.rollback()
        except Exception:
            pass
        return False, None, str(e)
