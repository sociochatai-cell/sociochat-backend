"""
Fast Router — Priority-Based Message Routing
=============================================

Routes incoming messages by priority BEFORE the full automation engine:

    1. Interactive flow (mid-conversation state) → instant
    2. FAQ / cached keyword → instant reply (~100ms)
    3. Template match → fast reply (~300ms)
    4. AI fallback → placeholder + async Gemini (~2-5s)

This replaces the old synchronous _process_automation() call inside the
webhook handler, turning it into a non-blocking background task.

Usage (called from background_processor):
    from whatsapp.fast_router import route_message
    route_message(account_id, conversation_id, message_text, ...)
"""

import logging
import os
import random
import time
import threading
from typing import Optional, Dict, Any

from flask import current_app
from shared_models import db
from .trace_debug import trace_event
from .debug_logger import bind_context, wa_debug

logger = logging.getLogger(__name__)

# Default placeholder variants sent before AI processing
DEFAULT_PLACEHOLDERS = (
    "Got it, checking that now...",
    "One moment, let me look into that.",
    "Sure, I am checking this for you.",
    "Thanks, I will help you with that right away.",
    "Understood. Let me verify this for you.",
)


def _placeholder_sync_first_enabled() -> bool:
    value = os.getenv("WHATSAPP_PLACEHOLDER_SYNC_FIRST", "true")
    return str(value).strip().lower() in {"1", "true", "yes", "on"}

# In-process conversation lock map (Gunicorn worker-local).
# Prevents same conversation from being processed concurrently in one worker.
_CONVERSATION_LOCKS: Dict[int, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def _get_conversation_lock(conversation_id: int) -> threading.Lock:
    with _LOCKS_GUARD:
        lock = _CONVERSATION_LOCKS.get(conversation_id)
        if lock is None:
            lock = threading.Lock()
            _CONVERSATION_LOCKS[conversation_id] = lock
        return lock


def _cached_rule_type(rule: Any) -> Optional[str]:
    if isinstance(rule, dict):
        return rule.get("rule_type")
    return getattr(rule, "rule_type", None)


def _has_welcome_rule(rules: Any) -> bool:
    return any(_cached_rule_type(rule) == "welcome" for rule in (rules or []))


def _interactive_pre_route_enabled() -> bool:
    value = os.getenv("WHATSAPP_ENABLE_INTERACTIVE_PRE_ROUTE", "true")
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _ai_use_dedicated_queue() -> bool:
    """When false (default), generate and send AI in the webhook worker job."""
    value = os.getenv("WHATSAPP_AI_DEDICATED_QUEUE", "false")
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _conversation_needs_interactive_route(
    conversation_id: int,
    workspace_id: str,
    account_id: int,
    message_text: str,
    *,
    is_button_reply: bool,
    is_first_inbound: Optional[bool],
) -> bool:
    """Skip expensive visual-flow scans only when no trigger context applies."""
    if is_button_reply:
        return True
    if is_first_inbound and _interactive_pre_route_enabled():
        return True
    try:
        from .interactive_automation_engine import (
            conversation_has_interactive_routing_context,
            message_might_match_interactive_keyword,
        )

        if conversation_has_interactive_routing_context(conversation_id, workspace_id):
            return True
        if message_might_match_interactive_keyword(account_id, workspace_id, message_text):
            return True
    except Exception:
        return False
    return False


def route_message(
    account_id: int,
    workspace_id: str,
    conversation_id: int,
    message_text: str,
    message_id: int,
    from_phone: str,
    msg_type: str = "text",
    is_button_reply: bool = False,
    button_payload: Optional[str] = None,
    is_flow_reply: bool = False,
    is_first_inbound: Optional[bool] = None,
    raw_message: Optional[Dict[str, Any]] = None,
    account_obj=None,
    conversation_obj=None,
    automation_replay: bool = False,
):
    """
    Priority-based message routing (runs in background thread).

    Order:
      1. Interactive automation (visual flow builder — has state)
      2. Regular automation (keyword/welcome/FAQ → instant)
      3. AI automation → placeholder first, then async Gemini
      4. Lead keyword check (CAPI events)
    """
    start = time.time()
    conv_lock = _get_conversation_lock(int(conversation_id))
    if not conv_lock.acquire(blocking=False):
        logger.info(f"[fast_router] Conversation {conversation_id} already in progress; skipping concurrent route")
        wa_debug.after(
            "fast_router",
            acct_id=account_id,
            conv_id=conversation_id,
            wp_id=workspace_id,
            status="skipped",
            error="conversation_lock_held",
        )
        return

    route_trace = wa_debug.before(
        "fast_router",
        acct_id=account_id,
        conv_id=conversation_id,
        wp_id=workspace_id,
        details={
            "message_id": message_id,
            "msg_type": msg_type,
            "button_reply": is_button_reply,
            "text_preview": (message_text or "")[:120],
        },
    )

    try:
        inbound_wamid = None
        if isinstance(raw_message, dict):
            inbound_wamid = raw_message.get("id")
        if inbound_wamid:
            bind_context(wamid=inbound_wamid)
        trace_event(
            stage="router.start",
            status="ok",
            wamid=inbound_wamid,
            conversation_id=conversation_id,
            account_id=account_id,
            details={"msg_type": msg_type, "button_reply": is_button_reply},
        )
        from .models import WhatsAppAccount, WhatsAppConversation

        account = account_obj or WhatsAppAccount.query.get(account_id)
        if not account:
            logger.warning(f"[fast_router] Account {account_id} not found")
            wa_debug.after(
                "fast_router",
                trace_id=route_trace,
                acct_id=account_id,
                conv_id=conversation_id,
                status="fail",
                error="account_not_found",
            )
            return

        conversation = conversation_obj or WhatsAppConversation.query.get(conversation_id)
        if not conversation:
            logger.warning(f"[fast_router] Conversation {conversation_id} not found")
            wa_debug.after(
                "fast_router",
                trace_id=route_trace,
                acct_id=account_id,
                conv_id=conversation_id,
                status="fail",
                error="conversation_not_found",
            )
            return

        if bool((conversation.attribution_data or {}).get("opted_out")):
            logger.info(f"[fast_router] Contact opted-out for conversation {conversation_id}; skipping automation")
            wa_debug.after(
                "fast_router",
                trace_id=route_trace,
                acct_id=account_id,
                conv_id=conversation_id,
                wp_id=account.workspace_id,
                status="skipped",
                error="contact_opted_out",
            )
            return

        # ── Full lead auto-discovery (best-effort, gated per-workspace) ──
        # Classify EVERY inbound message and auto-create/update a CRM lead when it
        # shows high purchase intent — independent of any flow leadAction node. Runs
        # here, after the conversation is resolved and BEFORE the routing branches, so
        # a lead it creates is already present when (and if) a downstream leadAction
        # node fires for the same inbound (that node then updates, not re-creates).
        # The toggle, threshold and dedup all live in maybe_discover_lead; this call
        # never raises into routing.
        # Only run discovery on genuine inbound FREE TEXT — never on button taps or
        # flow-form submissions (those are structured replies, not purchase-intent
        # prose, and classifying them would create spurious leads).
        if (
            msg_type == "text"
            and not is_button_reply
            and not is_flow_reply
            and message_text
            and str(message_text).strip()
        ):
            try:
                from .lead_action_service import LeadActionService

                LeadActionService.maybe_discover_lead(
                    account.workspace_id,
                    conversation,
                    message_text,
                )
            except Exception as e:
                logger.exception(f"[fast_router] lead auto-discovery error (non-fatal): {e}")
                _safe_rollback()

        is_first_message = is_first_inbound
        if is_first_message is None and not is_button_reply:
            from .automation_engine import check_is_first_message
            is_first_message = check_is_first_message(conversation_id)

        if not is_flow_reply and isinstance(raw_message, dict):
            interactive = raw_message.get("interactive", {})
            if raw_message.get("type") == "interactive" and interactive.get("type") == "nfm_reply":
                is_flow_reply = True

        needs_interactive = (
            is_flow_reply
            or is_button_reply
            or _conversation_needs_interactive_route(
                conversation_id,
                str(account.workspace_id or ""),
                account.id,
                message_text,
                is_button_reply=is_button_reply,
                is_first_inbound=is_first_message,
            )
        )

        # ────────────────────────────────────────────────────────
        # 1. INTERACTIVE AUTOMATIONS (mid-flow, button, flow form, keyword)
        # ────────────────────────────────────────────────────────
        if needs_interactive:
            try:
                from .interactive_automation_engine import process_interactive_automation

                inbound_wamid = None
                if isinstance(raw_message, dict):
                    inbound_wamid = raw_message.get("id")

                interactive_result = process_interactive_automation(
                    account=account,
                    conversation=conversation,
                    message_text=message_text,
                    from_phone=from_phone,
                    is_button_reply=is_button_reply,
                    button_payload=button_payload,
                    is_first_inbound=is_first_message,
                    inbound_wamid=inbound_wamid,
                    is_flow_reply=is_flow_reply,
                )

                # Interactive engine can explicitly allow fallback routing (AI/rules)
                # when it pauses flow due to an unrelated user query.
                if interactive_result is not None and not bool(
                    (interactive_result or {}).get("allow_ai_takeover")
                ):
                    elapsed = int((time.time() - start) * 1000)
                    logger.info(f"[fast_router] Interactive automation handled in {elapsed}ms")
                    wa_debug.after(
                        "fast_router",
                        trace_id=route_trace,
                        acct_id=account.id,
                        conv_id=conversation_id,
                        wp_id=account.workspace_id,
                        wamid=inbound_wamid,
                        status="ok",
                        elapsed_ms=elapsed,
                        details={"handler": "interactive", "result": interactive_result},
                    )
                    return
                if interactive_result is not None:
                    logger.info(
                        "[fast_router] Interactive automation yielded to fallback routing "
                        "(reason=%s)",
                        (interactive_result or {}).get("reason"),
                    )
            except Exception as e:
                logger.exception(f"[fast_router] Interactive automation error (non-fatal): {e}")
                _safe_rollback()

        # Button replies can fall through to regular keyword automations if not handled by interactive flows

        # Only process text messages from here (flow replies are handled above)
        if is_flow_reply or msg_type != "text" or not message_text:
            return

        # ────────────────────────────────────────────────────────
        # 2. REGULAR AUTOMATIONS (keyword / welcome / FAQ / etc.)
        # ────────────────────────────────────────────────────────
        try:
            from .automation_engine import (
                AutomationEngine,
                send_automation_response,
            )

            engine = AutomationEngine(
                account_id=account.id,
                workspace_id=account.workspace_id,
            )

            result = engine.process_incoming_message(
                message_text=message_text,
                conversation_id=conversation_id,
                is_first_message=is_first_message,
                message_id=message_id,
            )

            if result:
                response_type = result.get("response_type")
                response_config = result.get("response_config", {}).copy()
                trace_event(
                    stage="router.rule_matched",
                    status="ok",
                    wamid=inbound_wamid,
                    conversation_id=conversation_id,
                    account_id=account.id,
                    details={
                        "rule_name": result.get("rule_name"),
                        "response_type": response_type,
                    },
                )

                # ── FAST PATH: non-AI responses (FAQ, text, template) ──
                if response_type != "ai":
                    if response_type == "faq":
                        response_config["faq_id"] = result.get("faq_id")
                        response_config["faq_answer"] = result.get("faq_answer")

                    success, sent_msg_id, error = send_automation_response(
                        account_id=account.id,
                        conversation_id=conversation_id,
                        response_type=response_type,
                        response_config=response_config,
                        to_phone=from_phone,
                    )
                    elapsed = int((time.time() - start) * 1000)
                    logger.info(
                        f"[fast_router] {response_type.upper()} response in {elapsed}ms "
                        f"(rule={result.get('rule_name')}, ok={success})"
                    )
                    wa_debug.after(
                        "fast_router",
                        trace_id=route_trace,
                        acct_id=account.id,
                        conv_id=conversation_id,
                        wp_id=account.workspace_id,
                        wamid=inbound_wamid,
                        status="ok" if success else "fail",
                        elapsed_ms=elapsed,
                        error=error,
                        details={
                            "handler": response_type,
                            "rule_name": result.get("rule_name"),
                        },
                    )
                    return

                # ── SLOW PATH: AI response ──────────────────────────
                # Queue AI in the background so webhook workers stay free.
                from .ai_chatbot import _build_fast_greeting_reply

                fast_greeting_reply = _build_fast_greeting_reply(message_text)
                if fast_greeting_reply:
                    success, sent_msg_id, error = send_automation_response(
                        account_id=account.id,
                        conversation_id=conversation_id,
                        response_type="text",
                        response_config={"message": fast_greeting_reply},
                        to_phone=from_phone,
                    )
                    elapsed = int((time.time() - start) * 1000)
                    logger.info(
                        f"[fast_router] AI greeting shortcut in {elapsed}ms "
                        f"(rule={result.get('rule_name')}, ok={success})"
                    )
                    return

                response_config["incoming_message"] = message_text
                response_config["inbound_wamid"] = inbound_wamid
                if automation_replay:
                    response_config["skip_placeholder"] = True

                placeholder_enabled = _ai_placeholder_enabled(response_config)
                placeholder_ms = 0
                if placeholder_enabled:
                    try:
                        from .ai_chatbot import get_genai_runtime_status

                        ai_status = get_genai_runtime_status()
                        if ai_status.get("available"):
                            placeholder_start = time.time()
                            if _placeholder_sync_first_enabled():
                                _send_placeholder_fast(
                                    current_app._get_current_object(),
                                    _account_snapshot(account),
                                    from_phone,
                                    conversation_id,
                                    response_config,
                                    message_text,
                                )
                            else:
                                _dispatch_placeholder_priority(
                                    account,
                                    from_phone,
                                    conversation_id,
                                    response_config,
                                    message_text,
                                )
                            placeholder_ms = int((time.time() - placeholder_start) * 1000)
                            logger.debug(
                                "[fast_router] Placeholder dispatch: %sms",
                                placeholder_ms,
                            )
                        else:
                            logger.warning(
                                "[fast_router] GenAI not configured — skipping placeholder"
                            )
                    except Exception as placeholder_err:
                        logger.warning(
                            "[fast_router] Placeholder skipped (non-fatal): %s",
                            placeholder_err,
                        )

                from datetime import datetime, timezone
                from core.queue.manager import enqueue_job, get_queue_backend

                backend = get_queue_backend()
                use_ai_queue = backend == "redis" and _ai_use_dedicated_queue()
                ai_payload = {
                    "account_id": account.id,
                    "conversation_id": conversation_id,
                    "response_config": response_config,
                    "to_phone": from_phone,
                    "rule_name": result.get("rule_name"),
                    "started_at": start,
                    "enqueued_at": datetime.now(timezone.utc).isoformat(),
                    "idempotency_key": f"ai:{conversation_id}:{message_id}",
                }

                logger.info(
                    "[fast_router] AI dispatch decision conv=%s msg_id=%s backend=%s "
                    "dedicated_queue=%s rule=%s",
                    conversation_id,
                    message_id,
                    backend,
                    use_ai_queue,
                    result.get("rule_name"),
                )
                trace_event(
                    stage="router.ai_dispatch",
                    status="attempt",
                    wamid=inbound_wamid,
                    conversation_id=conversation_id,
                    account_id=account.id,
                    details={
                        "backend": backend,
                        "dedicated_queue": use_ai_queue,
                        "message_id": message_id,
                        "rule_name": result.get("rule_name"),
                    },
                )

                if use_ai_queue:
                    queued = enqueue_job(
                        "whatsapp-ai-generate",
                        ai_payload,
                        source="fast_router",
                    )
                    elapsed = int((time.time() - start) * 1000)
                    logger.info(
                        "[fast_router] AI enqueued to %s after %sms "
                        "(rule=%s, placeholder_ms=%s, depth=%s)",
                        queued.get("queue_name"),
                        elapsed,
                        result.get("rule_name"),
                        placeholder_ms,
                        queued.get("depth"),
                    )
                    trace_event(
                        stage="router.ai_dispatch",
                        status="queued",
                        wamid=inbound_wamid,
                        conversation_id=conversation_id,
                        account_id=account.id,
                        details={
                            "queue": queued.get("queue_name"),
                            "depth": queued.get("depth"),
                            "job": "whatsapp-ai-generate",
                        },
                    )
                    return

                # Default: inline Gemini + RAG in the webhook worker (reliable path).
                try:
                    _send_ai_response(
                        account_id=account.id,
                        conversation_id=conversation_id,
                        response_config=response_config,
                        to_phone=from_phone,
                        rule_name=result.get("rule_name"),
                        started_at=start,
                    )
                    elapsed = int((time.time() - start) * 1000)
                    logger.info(
                        "[fast_router] AI inline send completed in %sms (rule=%s)",
                        elapsed,
                        result.get("rule_name"),
                    )
                    trace_event(
                        stage="router.ai_dispatch",
                        status="inline_ok",
                        wamid=inbound_wamid,
                        conversation_id=conversation_id,
                        account_id=account.id,
                        details={"elapsed_ms": elapsed, "rule_name": result.get("rule_name")},
                    )
                except Exception as inline_err:
                    logger.warning(
                        "[fast_router] Inline AI send failed, using fallback: %s",
                        inline_err,
                    )
                    _send_ai_fallback(
                        account_id=account.id,
                        conversation_id=conversation_id,
                        response_config=response_config,
                        to_phone=from_phone,
                    )
                    trace_event(
                        stage="router.ai_dispatch",
                        status="inline_fallback",
                        wamid=inbound_wamid,
                        conversation_id=conversation_id,
                        account_id=account.id,
                        details={"error": str(inline_err)},
                    )
                return

        except Exception as e:
            logger.exception(f"[fast_router] Automation error (non-fatal): {e}")
            _safe_rollback()

        # ────────────────────────────────────────────────────────
        # 3. LEAD KEYWORD CHECK (CAPI events, off critical path)
        # ────────────────────────────────────────────────────────
        try:
            from .background_processor import bg_processor

            submitted = bg_processor.submit(_check_lead_keywords, account, conversation, message_text)
            if submitted is None:
                _check_lead_keywords(account, conversation, message_text)
        except Exception as e:
            logger.exception(f"[fast_router] Lead keyword check error (non-fatal): {e}")
            _safe_rollback()

        elapsed = int((time.time() - start) * 1000)
        logger.info(f"[fast_router] No automation matched — completed in {elapsed}ms")
        wa_debug.after(
            "fast_router",
            trace_id=route_trace,
            acct_id=account_id,
            conv_id=conversation_id,
            wp_id=workspace_id,
            status="ok",
            elapsed_ms=elapsed,
            details={"handler": "none", "matched": False},
        )

    except Exception as e:
        logger.exception(f"[fast_router] Fatal routing error: {e}")
        wa_debug.after(
            "fast_router",
            trace_id=route_trace,
            acct_id=account_id,
            conv_id=conversation_id,
            wp_id=workspace_id,
            status="fail",
            error=str(e),
        )
        _safe_rollback()
    finally:
        try:
            conv_lock.release()
        except Exception:
            pass


# ── Helpers ──────────────────────────────────────────────────


def _resolve_placeholder_message(response_config: Optional[Dict[str, Any]], incoming_message: str) -> str:
    if isinstance(response_config, dict):
        custom = response_config.get("placeholder_message")
        if isinstance(custom, str) and custom.strip():
            return custom.strip()
        custom_list = response_config.get("placeholder_messages")
        if isinstance(custom_list, list):
            options = [str(item).strip() for item in custom_list if str(item).strip()]
            if options:
                return random.choice(options)

    text = (incoming_message or "").lower()
    if "connect" in text or "setup" in text or "account" in text:
        return "Great question. Let me check the setup steps for you."
    return random.choice(DEFAULT_PLACEHOLDERS)


def _account_snapshot(account) -> Dict[str, Any]:
    return {
        "access_token": account.get_access_token(),
        "phone_number_id": account.phone_number_id,
        "waba_id": account.waba_id,
        "workspace_id": account.workspace_id,
    }


def _dispatch_placeholder_priority(
    account,
    to_phone: str,
    conversation_id: int,
    response_config: Optional[Dict[str, Any]] = None,
    incoming_message: str = "",
) -> bool:
    """
    PRIORITY placeholder dispatch - higher precedence than regular queue.
    Uses high-priority daemon thread to minimize delay.
    """
    try:
        snapshot = _account_snapshot(account)
        app_obj = current_app._get_current_object()
        
        # Create high-priority daemon thread
        thread = threading.Thread(
            target=_send_placeholder_fast,
            args=(app_obj, snapshot, to_phone, conversation_id, response_config, incoming_message),
            daemon=True,
            name="wa-placeholder-priority",
        )
        # Set thread priority if possible (Unix-like systems)
        thread.start()
        # Don't join - just fire and forget to keep dispatch fast
        return True
    except Exception as e:
        logger.warning(f"[fast_router] Placeholder priority dispatch failed: {e}")
        return False


def _dispatch_placeholder_quick(
    account,
    to_phone: str,
    conversation_id: int,
    response_config: Optional[Dict[str, Any]] = None,
    incoming_message: str = "",
) -> bool:
    """
    DEPRECATED: Use _dispatch_placeholder_priority instead.
    This is kept for backwards compatibility.
    """
    return _dispatch_placeholder_priority(account, to_phone, conversation_id, response_config, incoming_message)


def _send_placeholder_fast(
    app_obj,
    account_snapshot: Dict[str, Any],
    to_phone: str,
    conversation_id: int,
    response_config: Optional[Dict[str, Any]] = None,
    incoming_message: str = "",
):
    """
    Send instant placeholder message before AI processing.
    Runs in high-priority daemon thread with app context.
    """
    try:
        with app_obj.app_context():
            from .services import WhatsAppService

            # Create service with account credentials
            service = WhatsAppService(
                access_token=account_snapshot.get("access_token"),
                phone_number_id=account_snapshot.get("phone_number_id"),
                waba_id=account_snapshot.get("waba_id"),
                workspace_id=account_snapshot.get("workspace_id"),
            )
            
            # Resolve which placeholder message to send
            placeholder_message = _resolve_placeholder_message(response_config, incoming_message)
            
            # Send with minimal overhead: defer post-send, broadcast on success
            result = service.send_text(
                to_phone,
                placeholder_message,
                conversation_id=conversation_id,
                defer_post_send=True,
                broadcast_on_success=True,
            )
            
            if result.get("success"):
                logger.info(f"[fast_router] Placeholder sent to {to_phone} (wamid={result.get('message_id', 'unknown')})")
                return True
            else:
                logger.warning(f"[fast_router] Placeholder send failed: {result.get('error', 'unknown error')}")
                return False
    except Exception as e:
        # Placeholder failure is non-fatal — AI response will still be sent
        logger.warning(f"[fast_router] Placeholder exception: {type(e).__name__}: {e}", exc_info=False)


def _ai_placeholder_enabled(response_config: Optional[Dict[str, Any]] = None) -> bool:
    if response_config and response_config.get("skip_placeholder"):
        return False

    # Pre-AI placeholder fillers ("One moment…", "Let me verify this for you.") are OFF by
    # default: they read as misleading robotic filler and, being stored as outbound messages,
    # pollute the AI conversation context (e.g. a "let me verify" filler made the model answer a
    # later question about email verification). This env is the GLOBAL kill switch — when it is
    # not explicitly enabled, no per-automation opt-in (send_placeholder/send_typing_indicator)
    # can turn placeholders back on.
    env_value = os.getenv("WHATSAPP_AI_PLACEHOLDER_ENABLED", "false")
    if str(env_value).strip().lower() not in {"1", "true", "yes", "on"}:
        return False

    if response_config and "send_placeholder" in response_config:
        value = response_config.get("send_placeholder")
    elif response_config and "send_typing_indicator" in response_config:
        # Alias for UI/automation payloads that refer to typing indicator behavior.
        value = response_config.get("send_typing_indicator")
    else:
        value = env_value

    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _send_ai_fallback(
    account_id: int,
    conversation_id: int,
    response_config: Dict[str, Any],
    to_phone: str,
):
    from .automation_engine import send_automation_response, set_conversation_needs_attention
    from datetime import datetime, timedelta, timezone
    from .models import WhatsAppMessage
    from .human_escalation import apply_human_handoff

    fallback_message = (
        response_config.get("fallback_message")
        or "I'm sorry, I couldn't process your request. A team member will assist you soon."
    )
    inbound_wamid = (response_config or {}).get("inbound_wamid")
    
    # Check if agent has recently replied (within last 30 seconds).
    # If so, don't set needs_attention - let the agent handle it.
    recent_outbound = WhatsAppMessage.query.filter(
        WhatsAppMessage.conversation_id == conversation_id,
        WhatsAppMessage.direction.in_(["echo", "outgoing", "out", "sent"]),
        WhatsAppMessage.created_at >= datetime.now(timezone.utc) - timedelta(seconds=90)
    ).first()
    
    if not recent_outbound:
        # Use the shared handoff flow so fallback also triggers email notifications
        # and inbox attention markers consistently.
        ok, err = apply_human_handoff(
            account_id=account_id,
            conversation_id=conversation_id,
            to_phone=to_phone,
            reason="ai_queue_fallback",
            incoming_message=str(response_config.get("incoming_message") or ""),
        )
        trace_event(
            stage="router.ai_fallback",
            status="handoff_ok" if ok else "handoff_failed",
            wamid=inbound_wamid,
            conversation_id=conversation_id,
            account_id=account_id,
            details={"reason": "ai_queue_fallback", "error": err},
        )
        if ok:
            return True, None, None
        logger.warning("[fast_router] apply_human_handoff failed during ai_queue_fallback: %s", err)
    else:
        trace_event(
            stage="router.ai_fallback",
            status="recent_agent_reply_skip_handoff",
            wamid=inbound_wamid,
            conversation_id=conversation_id,
            account_id=account_id,
            details={"reason": "ai_queue_fallback"},
        )
        # The AI/agent already replied to the customer. Do NOT also send the
        # "I'm sorry … a team member will assist" text — that is what produced
        # the odd double message. Just flag the inbox for a human and stop.
        set_conversation_needs_attention(
            conversation_id, needs_attention=True, reason="ai_queue_fallback",
        )
        return True, None, None

    set_conversation_needs_attention(
        conversation_id,
        needs_attention=True,
        reason="ai_queue_fallback",
    )
    success, sent_msg_id, error = send_automation_response(
        account_id=account_id,
        conversation_id=conversation_id,
        response_type="text",
        response_config={"message": fallback_message},
        to_phone=to_phone,
    )
    trace_event(
        stage="router.ai_fallback",
        status="text_sent" if success else "text_send_failed",
        wamid=inbound_wamid,
        conversation_id=conversation_id,
        account_id=account_id,
        details={"reason": "ai_queue_fallback", "error": error},
    )
    return success, sent_msg_id, error


def _send_ai_response(
    account_id: int,
    conversation_id: int,
    response_config: Dict[str, Any],
    to_phone: str,
    rule_name: Optional[str],
    started_at: float,
):
    """Background AI send phase after the placeholder has been returned."""
    try:
        from .automation_engine import send_automation_response
        inbound_wamid = (response_config or {}).get("inbound_wamid")

        success, sent_msg_id, error = send_automation_response(
            account_id=account_id,
            conversation_id=conversation_id,
            response_type="ai",
            response_config=response_config,
            to_phone=to_phone,
        )
        elapsed = int((time.time() - started_at) * 1000)
        logger.info(
            f"[fast_router] AI response in {elapsed}ms "
            f"(rule={rule_name}, ok={success}, msg_id={sent_msg_id})"
        )
        trace_event(
            stage="router.ai_send_result",
            status="ok" if success else "error",
            wamid=inbound_wamid,
            conversation_id=conversation_id,
            account_id=account_id,
            details={
                "error": error,
                "elapsed_ms": elapsed,
                "rule_name": rule_name,
                "sent_msg_id": sent_msg_id,
            },
        )
        if not success:
            logger.warning(
                "[fast_router] AI response failed (rule=%s): %s — sending fallback",
                rule_name,
                error,
            )
            _send_ai_fallback(
                account_id=account_id,
                conversation_id=conversation_id,
                response_config=response_config,
                to_phone=to_phone,
            )
    except Exception as e:
        logger.exception(f"[fast_router] AI background send failed: {e}")
        trace_event(
            stage="router.ai_send_result",
            status="error",
            conversation_id=conversation_id,
            account_id=account_id,
            details={"error": str(e), "rule_name": rule_name},
        )
        _safe_rollback()


def _check_lead_keywords(account, conversation, text: str):
    """Check for lead-identifying keywords and trigger CAPI event."""
    import re

    if not text:
        return

    lead_patterns = [
        r"Interested_in_Lead",
        r"Campaign_ID_(\w+)",
        r"Ref:\s*(\S+)",
        r"Ad_ID:\s*(\S+)",
    ]

    ad_id = None
    matched = False

    for pattern in lead_patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            matched = True
            if match.lastindex and match.lastindex >= 1:
                ad_id = match.group(1)
            break

    if not matched:
        return

    # Tag conversation
    if not conversation.entry_source:
        conversation.entry_source = "keyword"
        if ad_id:
            conversation.ad_id = ad_id

    # Trigger CAPI Lead event
    try:
        from integrations.capi_service import send_capi_event

        user_data = {"phone": conversation.user_phone, "name": conversation.user_name}
        custom_data = {"lead_type": "keyword", "source": "whatsapp"}
        if ad_id:
            custom_data["ad_id"] = ad_id
        elif conversation.ad_id:
            custom_data["ad_id"] = conversation.ad_id
        if conversation.ctwa_clid:
            user_data["ctwa_clid"] = conversation.ctwa_clid

        send_capi_event(
            event_name="Lead",
            user_data_dict=user_data,
            custom_data_dict=custom_data,
            workspace_id=account.workspace_id,
            action_source="business_messaging",
        )
        logger.info(f"[fast_router] CAPI Lead event for conv {conversation.id}")
    except Exception as e:
        logger.error(f"[fast_router] CAPI Lead event failed: {e}")

    try:
        db.session.commit()
    except Exception:
        _safe_rollback()


def _safe_rollback():
    """Rollback DB session safely."""
    try:
        db.session.rollback()
    except Exception:
        pass
