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
import re
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, Optional, List, Tuple

from shared_models import db
from notifications import notification_manager
from .trace_debug import trace_event
from .debug_logger import wa_debug
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

            # Safety: AI chat should always remain a fallback even if mis-prioritized.
            rules.sort(key=lambda r: (1 if r.rule_type == "ai_chat" else 0, r.priority or 0, r.id or 0))
            
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

            # Check main command (with or without leading slash)
            if command:
                cmd_variants = [command]
                if command.startswith("/"):
                    cmd_variants.append(command[1:])
                else:
                    cmd_variants.append("/" + command)
                for cmd in cmd_variants:
                    if text_lower == cmd or text_lower.startswith(cmd + " "):
                        return {"matched": True, "matched_keyword": command}

            # Check aliases (with or without leading slash)
            for alias in aliases:
                alias_lower = alias.lower().strip()
                alias_variants = [alias_lower]
                if alias_lower.startswith("/"):
                    alias_variants.append(alias_lower[1:])
                else:
                    alias_variants.append("/" + alias_lower)
                for av in alias_variants:
                    if text_lower == av or text_lower.startswith(av + " "):
                        return {"matched": True, "matched_keyword": alias}

            return {"matched": False}
        
        # KEYWORD: Triggers on keyword match
        if rule_type == "keyword":
            keywords = trigger_config.get("keywords", [])
            match_type = trigger_config.get("match_type") or trigger_config.get("matchType") or "contains"
            case_sensitive = bool(trigger_config.get("case_sensitive", False))

            if isinstance(keywords, str):
                keywords = [k.strip() for k in keywords.split(",") if k and k.strip()]
            
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
                # Cast workspace_id to VARCHAR to match column type
                self._business_hours = WhatsAppBusinessHours.query.filter(
                    WhatsAppBusinessHours.workspace_id == str(self.workspace_id),
                    WhatsAppBusinessHours.account_id == self.account_id
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

    auto_trace = wa_debug.before(
        "automation_send",
        acct_id=account_id,
        conv_id=conversation_id,
        details={"response_type": response_type, "to_phone": to_phone},
    )

    def _finish(ok: bool, msg_id: Optional[int], err: Optional[str], **details):
        wa_debug.after(
            "automation_send",
            trace_id=auto_trace,
            acct_id=account_id,
            conv_id=conversation_id,
            status="ok" if ok else "fail",
            error=err,
            details=details or None,
        )
        # Seamless flow continuation: if this answer resolved an off-script question the customer
        # asked mid-flow, re-ask the flow step they were on so the conversation picks back up.
        # No-op unless an interactive flow is paused for an off-script query, so it is safe here.
        if ok and response_type in ("faq", "ai", "text"):
            try:
                from .interactive_automation_engine import reprompt_after_off_script_answer
                reprompt_after_off_script_answer(
                    account_id=account_id,
                    conversation_id=conversation_id,
                    to_phone=to_phone,
                )
            except Exception:
                logger.exception("[automation] flow re-prompt after answer failed (non-fatal)")
        return ok, msg_id, err

    try:
        account = WhatsAppAccount.query.get(account_id)
        if not account:
            return _finish(False, None, "Account not found", gate="account_lookup")

        access_token = account.get_access_token()
        if not access_token:
            return _finish(False, None, "No access token", gate="token")

        from .capabilities import automation_capability_check, ai_capability_check

        auto = automation_capability_check(account_id)
        if not auto.ok:
            return _finish(
                False,
                None,
                auto.message or "automation_capability_denied",
                gate="capability",
            )
        from .warmup_enforcement import warmup_denial_automation

        wauto = warmup_denial_automation(account_id, db.session, response_type)
        if wauto:
            return _finish(False, None, wauto.message, gate="warmup", code=wauto.code)
        if response_type == "ai":
            ai_ent = ai_capability_check(account_id)
            if not ai_ent.ok:
                return _finish(
                    False,
                    None,
                    ai_ent.message or "ai_capability_denied",
                    gate="ai_capability",
                )
                
        # Risk-aware throttling via Safe Mode Engine
        from .safe_mode_engine import is_risk_allowed, RiskClass
        risk_map = {
            "text": RiskClass.LOW,
            "faq": RiskClass.LOW,
            "interactive": RiskClass.MEDIUM,
            "ai": RiskClass.MEDIUM,
            "template": RiskClass.HIGH,
        }
        risk_class = risk_map.get(response_type, RiskClass.MEDIUM)
        
        if not is_risk_allowed(account, risk_class):
            logger.warning(f"Automation response ({response_type}) blocked by Safe Mode Engine ({account.operational_mode})")
            return _finish(
                False,
                None,
                f"Suppressed by Safe Mode: {account.operational_mode}",
                gate="safe_mode",
            )
        
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
            from .ai_chatbot import create_ai_chatbot, prepare_automation_ai
            from .human_escalation import apply_human_handoff, should_escalate_to_human

            prep = prepare_automation_ai(account, response_config)
            incoming_message = response_config.get("incoming_message", "")
            inbound_wamid = response_config.get("inbound_wamid")
            trace_event(
                stage="ai.send.start",
                status="attempt",
                wamid=inbound_wamid,
                conversation_id=conversation_id,
                account_id=account_id,
                details={
                    "prep_run": prep.get("run"),
                    "prep_reason": prep.get("reason"),
                    "to_phone": to_phone,
                },
            )

            if not prep["run"]:
                trace_event(
                    stage="ai.send.prep_blocked",
                    status="blocked",
                    wamid=inbound_wamid,
                    conversation_id=conversation_id,
                    account_id=account_id,
                    details={
                        "prep_reason": prep.get("reason"),
                        "fallback_message": prep.get("fallback_message"),
                    },
                )
                ok, err = apply_human_handoff(
                    account_id=account_id,
                    conversation_id=conversation_id,
                    to_phone=to_phone,
                    reason="ai_disabled_or_fallback",
                    incoming_message=incoming_message,
                )
                result = {"success": ok, "conversation_id": conversation_id}
                if not ok:
                    result = service.send_text(to_phone, prep["fallback_message"])
                    trace_event(
                        stage="ai.reply.send",
                        status="fallback_text_sent"
                        if bool(result and result.get("success"))
                        else "fallback_text_failed",
                        wamid=inbound_wamid,
                        conversation_id=conversation_id,
                        account_id=account_id,
                        details={
                            "reason": "prep_blocked_handoff_failed",
                            "handoff_error": err,
                            "error": (result or {}).get("error")
                            if isinstance(result, dict)
                            else None,
                        },
                    )
                logger.info("AI disabled — human handoff applied (account=%s)", account_id)
            else:
                ai_config_dict = prep["config"]
                incoming_message = response_config.get("incoming_message", "")

                # If the user paused an interactive flow to ask this, give the AI that context.
                _flow_hint = None
                try:
                    from .interactive_automation_engine import paused_flow_hint
                    _flow_hint = paused_flow_hint(account_id, conversation_id)
                except Exception:
                    _flow_hint = None
                ai_config_dict["flow_context"] = _flow_hint  # paused-flow awareness (None when not mid-flow)

                # AgentOS growth intent handoff (before generic AI chat)
                try:
                    from .agentos_handoff import handle_inbound_agentos

                    handled, handoff_result = handle_inbound_agentos(
                        account=account,
                        conversation_id=conversation_id,
                        incoming_message=incoming_message,
                        service=service,
                        to_phone=to_phone,
                    )
                    if handled:
                        result = handoff_result or {"success": True, "conversation_id": conversation_id}
                        trace_event(
                            stage="ai.agentos.handoff",
                            status="ok",
                            wamid=inbound_wamid,
                            conversation_id=conversation_id,
                            account_id=account_id,
                            details={"handled": True},
                        )
                        return result
                except Exception as agentos_exc:
                    logger.warning("AgentOS handoff error (continuing with AI): %s", agentos_exc)

                chatbot = create_ai_chatbot(ai_config_dict)
                # PHASE 4: hand the agent the live conversation + customer phone so
                # transactional tools (payment link, human handoff, etc.) can act.
                # Only used by agent mode (default OFF); harmless for the legacy path.
                try:
                    chatbot.config.conversation_id = conversation_id
                    chatbot.config.customer_phone = to_phone
                except Exception:
                    pass
                context = _get_conversation_context(
                    conversation_id, ai_config_dict["context_messages"]
                )
                ai_response = chatbot.generate_response(
                    message=incoming_message,
                    context=context,
                )
                trace_event(
                    stage="ai.generation.result",
                    status="ok" if ai_response.success else "error",
                    wamid=inbound_wamid,
                    conversation_id=conversation_id,
                    account_id=account_id,
                    details={
                        "error": ai_response.error,
                        "used_rag": ai_response.used_rag,
                        "rag_chunks": ai_response.rag_chunks,
                        "low_rag_confidence": ai_response.low_rag_confidence,
                    },
                )

                # Hard rule: if AI generation succeeded and produced ANY reply — text OR
                # queued interactive messages (buttons / product cards) — send it. The
                # "a team member will assist you soon" handoff fires ONLY when there is
                # genuinely no answer at all (no text AND no interactive messages).
                _interactives = getattr(ai_response, "interactive_messages", None) or []
                if ai_response.success and (ai_response.message or _interactives):
                    if ai_response.message:
                        logger.info(
                            "[automation_engine][ai] sending_ai_reply conv=%s preview=%r",
                            conversation_id,
                            ai_response.message[:160],
                        )
                        result = service.send_text(to_phone, ai_response.message)
                    else:
                        logger.info(
                            "[automation_engine][ai] interactive-only reply conv=%s interactives=%s",
                            conversation_id,
                            len(_interactives),
                        )
                        result = {"success": True, "conversation_id": conversation_id}

                    # Fire-and-forget: record AI conversation insights
                    try:
                        from .conversation_insights_service import record_insights
                        record_insights(
                            workspace_id=account.workspace_id,
                            conversation_id=conversation_id,
                            customer_phone=to_phone,
                            tool_log=chatbot.get_tool_log(),
                            ai_reply_text=ai_response.message,
                        )
                    except Exception as _ins_err:
                        logger.warning("[automation_engine] insights recording failed: %s", _ins_err)

                    trace_event(
                        stage="ai.reply.send",
                        status="ok" if bool(result and result.get("success")) else "error",
                        wamid=inbound_wamid,
                        conversation_id=conversation_id,
                        account_id=account_id,
                        details={
                            "error": (result or {}).get("error") if isinstance(result, dict) else None,
                            "preview": (ai_response.message or "")[:140],
                        },
                    )
                    # PHASE 3 (agent mode): after the text reply, send any interactive
                    # visual messages (product cards / reply buttons) the agent queued.
                    # Behind the ai_agent_mode toggle → legacy bots always have an empty
                    # list, so this is a no-op for them.
                    _interactives = getattr(ai_response, "interactive_messages", None) or []
                    for _inter in _interactives:
                        try:
                            _ir = service.send_interactive_passthrough(to_phone, _inter)
                            trace_event(
                                stage="ai.reply.interactive",
                                status="ok" if bool(_ir and _ir.get("success")) else "error",
                                wamid=inbound_wamid,
                                conversation_id=conversation_id,
                                account_id=account_id,
                                details={
                                    "interactive_type": _inter.get("type"),
                                    "error": (_ir or {}).get("error") if isinstance(_ir, dict) else None,
                                },
                            )
                            logger.info(
                                "[automation_engine][ai] interactive sent type=%s ok=%s",
                                _inter.get("type"),
                                bool(_ir and _ir.get("success")),
                            )
                        except Exception as _inter_exc:  # noqa: BLE001
                            logger.warning(
                                "[automation_engine][ai] interactive send failed type=%s err=%s",
                                _inter.get("type"), _inter_exc,
                            )
                    logger.info(
                        "[Automation Source: AI CHATBOT + RAG] AI response sent: "
                        "tokens=%s, time=%sms",
                        ai_response.tokens_used,
                        ai_response.response_time_ms,
                    )
                else:
                    escalate = ai_response.escalate_to_human
                    escalation_reason = ai_response.escalation_reason
                    if not escalate:
                        escalate, escalation_reason = should_escalate_to_human(
                            success=ai_response.success,
                            reply_text=ai_response.message,
                            low_rag_confidence=ai_response.low_rag_confidence,
                            has_rag_context=ai_response.used_rag,
                        )

                    logger.info(
                        "[automation_engine][ai] decision conv=%s success=%s used_rag=%s rag_chunks=%s "
                        "low_rag_confidence=%s escalate=%s reason=%s",
                        conversation_id,
                        ai_response.success,
                        ai_response.used_rag,
                        ai_response.rag_chunks,
                        ai_response.low_rag_confidence,
                        escalate,
                        escalation_reason or "",
                    )

                    if escalate:
                        trace_event(
                            stage="ai.escalation",
                            status="handoff",
                            wamid=inbound_wamid,
                            conversation_id=conversation_id,
                            account_id=account_id,
                            details={"reason": escalation_reason or "ai_escalation"},
                        )
                        ok, err = apply_human_handoff(
                            account_id=account_id,
                            conversation_id=conversation_id,
                            to_phone=to_phone,
                            reason=escalation_reason or "ai_escalation",
                            incoming_message=incoming_message,
                        )
                        result = {"success": ok, "conversation_id": conversation_id}
                        if not ok:
                            logger.warning("[human_escalation] Handoff failed: %s", err)
                            result = service.send_text(
                                to_phone, ai_config_dict["fallback_message"]
                            )
                            trace_event(
                                stage="ai.reply.send",
                                status="fallback_text_sent"
                                if bool(result and result.get("success"))
                                else "fallback_text_failed",
                                wamid=inbound_wamid,
                                conversation_id=conversation_id,
                                account_id=account_id,
                                details={
                                    "reason": "ai_escalation_handoff_failed",
                                    "handoff_error": err,
                                    "escalation_reason": escalation_reason,
                                    "error": (result or {}).get("error")
                                    if isinstance(result, dict)
                                    else None,
                                },
                            )
                        else:
                            logger.info(
                                "[human_escalation] Handoff applied conv=%s reason=%s",
                                conversation_id,
                                escalation_reason,
                            )
                    else:
                        trace_event(
                            stage="ai.reply.send",
                            status="fallback_handoff",
                            wamid=inbound_wamid,
                            conversation_id=conversation_id,
                            account_id=account_id,
                            details={"reason": "ai_generation_failed", "error": ai_response.error},
                        )
                        ok, err = apply_human_handoff(
                            account_id=account_id,
                            conversation_id=conversation_id,
                            to_phone=to_phone,
                            reason="ai_generation_failed",
                            incoming_message=incoming_message,
                        )
                        result = {"success": ok, "conversation_id": conversation_id}
                        if not ok:
                            result = service.send_text(
                                to_phone, ai_config_dict["fallback_message"]
                            )
                            trace_event(
                                stage="ai.reply.send",
                                status="fallback_text_sent"
                                if bool(result and result.get("success"))
                                else "fallback_text_failed",
                                wamid=inbound_wamid,
                                conversation_id=conversation_id,
                                account_id=account_id,
                                details={
                                    "reason": "ai_generation_failed_handoff_failed",
                                    "handoff_error": err,
                                    "error": ai_response.error,
                                    "send_error": (result or {}).get("error")
                                    if isinstance(result, dict)
                                    else None,
                                },
                            )
                        logger.warning(f"AI failed — human handoff: {ai_response.error}")
        
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
            
            return _finish(True, message_id, None, wamid=result.get("wamid"))
        else:
            error = result.get("error", "Unknown error") if result else "No response"
            error_code = result.get("error_code") if isinstance(result, dict) else None
            logger.warning(f"Automation response failed: {error}")
            return _finish(
                False,
                None,
                error,
                error_code=error_code,
                meta_response=result,
            )

    except Exception as e:
        logger.exception(f"Failed to send automation response: {e}")
        try:
            db.session.rollback()
        except Exception:
            pass
        return _finish(False, None, str(e), gate="exception")


# Re-export for fast_router and other callers
from .human_escalation import set_conversation_needs_attention  # noqa: E402,F401
