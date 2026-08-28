"""
WhatsApp Webhook Handler - Phase 1
==================================

Webhook processing for WhatsApp Cloud API.

Handles:
    - Webhook verification (GET)
    - Message received events
    - Delivery status updates
    - Read receipts
    - Error events
    - Template status updates (APPROVED, REJECTED, PAUSED, DISABLED)
    - Template quality updates (GREEN, YELLOW, RED)
    - account_update (e.g. phone number quality) → account status slice + legacy mirror

Features:
    ✔ Verify signature header (X-Hub-Signature-256)
    ✔ Deduplicate processing using wamid
    ✔ Store raw webhook JSON into whatsapp_webhook_logs
    ✔ Store incoming messages in whatsapp_messages
    ✔ Auto-open/close conversations based on events
    ✔ Multi-tenant: Routes webhooks to correct account by WABA ID
"""

import os
import json
import logging
import time
from datetime import datetime, timezone
from typing import Optional, Dict, Any, Tuple, List
from sqlalchemy.exc import IntegrityError

from notifications import notification_manager
from shared_models import db
from .models import (
    WhatsAppAccount,
    WhatsAppConversation,
    WhatsAppMessage,
    WhatsAppWebhookLog,
    MessageStatusEvent,
    WhatsAppTemplate,
    update_account_heartbeat,
    shadow_sync_account_quality_rating,
    shadow_sync_account_restriction_state,
)
from .utils import (
    verify_signature,
    normalize_phone,
    parse_whatsapp_timestamp,
    extract_message_text,
    get_message_type,
    extract_media_info,
)
from .webhook_health import map_entry_changes_to_signal_keys, touch_inbound_signals
from .trace_debug import trace_event

logger = logging.getLogger(__name__)


def send_capi_event(*args, **kwargs):
    """Lazy import so webhook module loads even if integrations package is optional.

    CRM pipeline: events route through ``SocioviaCrm.capi_service`` (the multi-tenant
    CRM Conversions API service). Falls back to the legacy ``integrations`` package
    only if the CRM service is unavailable, so message processing never breaks.
    """
    try:
        from SocioviaCrm.capi_service import send_capi_event as _send
    except Exception:
        from integrations.capi_service import send_capi_event as _send

    return _send(*args, **kwargs)


# ============================================================
# Webhook Verification
# ============================================================

def _webhook_app_secrets() -> List[str]:
    """Distinct Meta app secrets used to verify X-Hub-Signature-256."""
    secrets: List[str] = []
    for key in ("FB_APP_SECRET", "META_APP_SECRET", "WHATSAPP_APP_SECRET"):
        val = (os.getenv(key) or "").strip()
        if val and val not in secrets:
            secrets.append(val)
    return secrets


def verify_webhook_signature(payload: bytes, signature: str, app_secret: str) -> bool:
    """
    Verify the webhook signature from Meta.
    
    Args:
        payload: Raw request body
        signature: X-Hub-Signature-256 header value
        app_secret: App secret from environment
        
    Returns:
        True if signature is valid
    """
    if not app_secret:
        logger.warning("WHATSAPP_APP_SECRET not configured - skipping signature verification")
        return True  # Allow if no secret configured (dev mode)
    
    return verify_signature(payload, signature, app_secret)


def _extract_phone_number_id_from_payload(payload: bytes) -> Optional[str]:
    """Best-effort parse of metadata.phone_number_id from a raw webhook body.

    Used to resolve the PER-TENANT Meta app secret for signature verification.
    Never raises — returns None when the body can't be parsed or has no pid.
    """
    try:
        data = json.loads(payload.decode("utf-8") if isinstance(payload, bytes) else payload)
    except Exception:
        return None
    try:
        for entry in (data.get("entry") or []):
            for change in (entry.get("changes") or []):
                pid = ((change.get("value") or {}).get("metadata") or {}).get("phone_number_id")
                if pid:
                    return str(pid)
    except Exception:
        return None
    return None


def _tenant_app_secret_for_payload(payload: bytes) -> Optional[str]:
    """Resolve the tenant's Meta app secret (env-fallback) for this webhook.

    Re-applies the multi-tenant pattern: a tenant using its OWN Meta app signs
    webhooks with ITS app secret. We resolve it via
    ``get_tenant_meta_config(phone_number_id=pid).app_secret``. Never raises.
    """
    pid = _extract_phone_number_id_from_payload(payload)
    if not pid:
        return None
    try:
        from tenant.integration import get_tenant_meta_config
        cfg = get_tenant_meta_config(phone_number_id=pid)
        return (getattr(cfg, "app_secret", None) or "").strip() or None
    except Exception:
        logger.exception("get_tenant_meta_config(phone_number_id=%s) failed; using env secrets", pid)
        return None


def verify_webhook_signature_any(payload: bytes, signature: str) -> bool:
    """
    Verify webhook signature against the per-tenant app secret, then env secrets.

    Multi-tenant: a tenant using its own Meta app signs with ITS app secret, so we
    FIRST try ``get_tenant_meta_config(phone_number_id=pid).app_secret`` (resolved
    from the payload's metadata). We then fall back to the global env secrets —
    Meta signs SocioChat/T0000 with the Facebook App Secret (``FB_APP_SECRET``);
    some deployments also set ``WHATSAPP_APP_SECRET`` to a different value.
    """
    secrets: List[str] = []

    # 1) Per-tenant secret (own Meta app) — resolved from the payload's pid.
    tenant_secret = _tenant_app_secret_for_payload(payload)
    if tenant_secret:
        secrets.append(tenant_secret)

    # 2) Global env secret(s) as fallback (T0000 / unconfigured tenants).
    for secret in _webhook_app_secrets():
        if secret not in secrets:
            secrets.append(secret)

    if not secrets:
        logger.warning("No tenant/env app secret configured - skipping verification")
        return True
    if not signature:
        return False
    for secret in secrets:
        if verify_signature(payload, signature, secret):
            return True
    return False


def verify_webhook_challenge(mode: str, token: str, challenge: str) -> Optional[str]:
    """
    Verify webhook subscription challenge from Meta.

    Multi-tenant: the GET-verify request carries no tenant context, so we accept
    the global env verify token OR ANY configured tenant's token via
    ``tenant.integration.verify_token_matches_any`` (instead of comparing only to
    the single ``WHATSAPP_VERIFY_TOKEN`` env value). Falls back to the env-token
    comparison if the tenant resolver is unavailable.

    Args:
        mode: hub.mode parameter
        token: hub.verify_token parameter
        challenge: hub.challenge parameter

    Returns:
        Challenge string if valid, None otherwise
    """
    if mode != "subscribe":
        logger.warning(f"Webhook verification failed: mode={mode}")
        return None

    try:
        from tenant.integration import verify_token_matches_any
        if verify_token_matches_any(token):
            logger.info("Webhook verification successful")
            return challenge
        logger.warning("Webhook verification failed: token did not match env or any tenant")
        return None
    except Exception:
        logger.exception("verify_token_matches_any unavailable; falling back to env token")

    # Fallback: compare against the single global env token.
    verify_token = os.getenv("WHATSAPP_VERIFY_TOKEN", "")
    if not verify_token:
        logger.error("WHATSAPP_VERIFY_TOKEN not configured!")
        return None
    if token == verify_token:
        logger.info("Webhook verification successful (env fallback)")
        return challenge

    logger.warning(f"Webhook verification failed: token_match={token == verify_token}")
    return None


# ============================================================
# Webhook Processor
# ============================================================

class WebhookProcessor:
    """
    Process incoming webhook events from WhatsApp Cloud API.
    """
    
    def __init__(self, db_session=None):
        """
        Initialize webhook processor.
        
        Args:
            db_session: SQLAlchemy session (optional)
        """
        self.db_session = db_session or db.session
        self._processed_wamids = set()  # In-memory dedup cache
    
    def process_webhook(self, payload: Dict[str, Any]) -> Tuple[bool, str]:
        """
        Process incoming webhook payload.
        
        Args:
            payload: Webhook JSON payload
            
        Returns:
            Tuple of (success, message)
        """
        try:
            # Log raw webhook
            raw_json = json.dumps(payload)
            
            # Validate structure
            if payload.get("object") != "whatsapp_business_account":
                self._log_webhook(raw_json, "unknown", None, "Invalid object type")
                return False, "Invalid webhook object type"
            
            entries = payload.get("entry", [])
            if not entries:
                self._log_webhook(raw_json, "empty", None, "No entries")
                return False, "No entries in webhook"
            
            # Process each entry
            for entry in entries:
                self._process_entry(entry, raw_json)
            
            return True, "Webhook processed successfully"
            
        except Exception as e:
            logger.exception(f"Webhook processing error: {e}")
            return False, str(e)
    
    def _process_entry(self, entry: Dict[str, Any], raw_json: str):
        """
        Process a single entry from the webhook.
        
        Args:
            entry: Entry object from webhook
            raw_json: Original raw JSON for logging
        """
        # WABA ID is in entry.id - used for multi-tenant routing
        waba_id = entry.get("id") or ""
        changes = entry.get("changes", [])
        
        for change in changes:
            value = change.get("value", {})
            field = change.get("field", "")
            
            # ============================================================
            # Handle Template Status Updates (Multi-tenant)
            # ============================================================
            if field == "message_template_status_update":
                self._process_template_status_update(waba_id, value, raw_json)
                continue
            
            # ============================================================
            # Handle Template Quality Updates (Multi-tenant)
            # ============================================================
            if field == "message_template_quality_update":
                self._process_template_quality_update(waba_id, value, raw_json)
                continue
            
            # ============================================================
            # Handle Template Category Updates (Multi-tenant)
            # ============================================================
            if field == "template_category_update":
                self._process_template_category_update(waba_id, value, raw_json)
                continue
            
            # ============================================================
            # Account updates (e.g. phone number quality from Meta)
            # ============================================================
            if field == "account_update":
                self._process_account_update(waba_id, value, raw_json)
                continue
            
            # ============================================================
            # Handle Message Echoes (Coexistence: messages sent from mobile)
            # ============================================================
            if field in ("message_echoes", "smb_message_echoes"):
                phone_number_id = value.get("metadata", {}).get("phone_number_id")
                echo_messages = value.get("messages", value.get("message_echoes", []))
                for echo_msg in echo_messages:
                    self._process_echo(echo_msg, phone_number_id)
                self._log_webhook(raw_json, "echo", phone_number_id)
                continue
            
            # ============================================================
            # Handle History Sync (Coexistence: 180-day history after QR)
            # ============================================================
            if field in ("history_sync", "history"):
                phone_number_id = value.get("metadata", {}).get("phone_number_id")
                self._process_history_sync(value, phone_number_id)
                self._log_webhook(raw_json, "history_sync", phone_number_id)
                continue
            
            # ============================================================
            # Handle Contacts Sync (Coexistence: contacts from WA Business app)
            # ============================================================
            if field == "smb_app_state_sync":
                phone_number_id = value.get("metadata", {}).get("phone_number_id")
                self._process_smb_app_state_sync(value, phone_number_id)
                self._log_webhook(raw_json, "contacts_sync", phone_number_id)
                continue
            
            # ============================================================
            # Handle Messages (existing logic)
            # ============================================================
            if field != "messages":
                # Unknown field, skip
                logger.debug(f"Skipping unknown webhook field: {field}")
                continue
            
            phone_number_id = value.get("metadata", {}).get("phone_number_id")
            
            # ============================================================
            # Coexistence: History sync can arrive under field="messages"
            # with "history" key in value instead of "messages"/"statuses"
            # ============================================================
            if "history" in value:
                logger.info(f"History sync data detected under messages field for {phone_number_id}")
                self._process_history_sync(value, phone_number_id)
                self._log_webhook(raw_json, "history_sync", phone_number_id)
                continue
            
            # ============================================================
            # Coexistence: Contacts sync can arrive under field="messages"
            # with "contacts" key but no "messages" key
            # ============================================================
            if "smb_app_state_sync" in value or ("contacts" in value and "messages" not in value and "statuses" not in value):
                logger.info(f"Contacts sync data detected under messages field for {phone_number_id}")
                self._process_smb_app_state_sync(value, phone_number_id)
                self._log_webhook(raw_json, "contacts_sync", phone_number_id)
                continue
            
            # Process statuses (delivery receipts)
            statuses = value.get("statuses", [])
            for status in statuses:
                self._process_status(status, phone_number_id)
                self._log_webhook(raw_json, "status", phone_number_id)
            
            # Process messages
            messages = value.get("messages", [])
            contacts = value.get("contacts", [])
            
            for message in messages:
                contact = self._find_contact(message.get("from"), contacts)
                self._process_message(message, contact, phone_number_id)
                self._log_webhook(raw_json, "message", phone_number_id)
            
            # Process errors
            errors = value.get("errors", [])
            for error in errors:
                self._process_error(error, phone_number_id)
                self._log_webhook(raw_json, "error", phone_number_id)

        keys = map_entry_changes_to_signal_keys(changes)
        if keys and waba_id:
            touch_inbound_signals(str(waba_id), keys)

    def _process_message(
        self,
        message: Dict[str, Any],
        contact: Optional[Dict[str, Any]],
        phone_number_id: str,
    ):
        """
        Process an incoming message.
        
        Args:
            message: Message object from webhook
            contact: Contact info (name, etc.)
            phone_number_id: Our phone number ID
        """
        wamid = message.get("id")
        from_phone = message.get("from")
        msg_type = get_message_type(message)
        timestamp = message.get("timestamp")
        
        debug_message_logs = str(os.getenv("WHATSAPP_WEBHOOK_DEBUG_MESSAGE", "")).strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        if debug_message_logs:
            print(f"📩 Processing message: wamid={wamid}, from={from_phone}, type={msg_type}")
            print(f"   phone_number_id: {phone_number_id}")
        
        if not wamid or not from_phone:
            if debug_message_logs:
                print("⚠️ Message missing wamid or from")
            logger.warning("Message missing wamid or from")
            return

        trace_event(
            stage="worker.inbound.received",
            status="ok",
            wamid=wamid,
            details={
                "from_phone": from_phone,
                "msg_type": msg_type,
                "phone_number_id": phone_number_id,
            },
        )
        
        # Deduplicate by wamid (database is source of truth).
        # NOTE: We intentionally avoid process-memory "already seen" caches here
        # because they can drop valid retries when an earlier attempt fails
        # before commit.
        existing = WhatsAppMessage.query.filter_by(wamid=wamid).first()
        if existing:
            trace_event(
                stage="worker.inbound.duplicate",
                status="skipped",
                wamid=wamid,
                conversation_id=existing.conversation_id,
                details={"existing_message_id": existing.id},
            )
            logger.info(
                "Duplicate inbound message skipped (db precheck): wamid=%s existing_id=%s conversation_id=%s created_at=%s",
                wamid,
                existing.id,
                existing.conversation_id,
                existing.created_at,
            )
            self._maybe_replay_automation_on_duplicate(
                existing=existing,
                message=message,
                contact=contact,
                phone_number_id=phone_number_id,
                inbound_wamid=wamid,
            )
            return
        
        # Normalize phone
        from_phone = normalize_phone(from_phone)
        
        # Get account - returns None if not found or inactive
        account = self._get_or_create_account(phone_number_id)
        
        if not account:
            if debug_message_logs:
                print(f"⚠️ Skipping message - no active account for phone_number_id: {phone_number_id}")
            return
        
        # Get or create conversation
        contact_name = contact.get("profile", {}).get("name") if contact else None
        conversation = self._get_or_create_conversation(account.id, from_phone, contact_name)

        # Phase 3 — round-robin auto-assign a brand-new inbound customer number
        # to an agent. ONLY this genuine-inbound path triggers it (send-echo,
        # history-sync and contacts-import must not). Runs BEFORE the commit
        # below so the assignment lands atomically with the conversation +
        # message. Fully failure-isolated: it must NEVER break the webhook.
        if getattr(conversation, "_is_new", False):
            try:
                if account.workspace_id is not None:
                    from agent_auth.inbox_scope import auto_assign_new_number
                    auto_assign_new_number(int(account.workspace_id), from_phone)
            except Exception:
                logger.exception(
                    "auto_assign hook failed (conversation %s, account %s)",
                    conversation.id, account.id,
                )

        # Build message content
        content = self._extract_content(message, msg_type)
        if msg_type == "order":
            try:
                from .order_enrichment import enrich_order_content
                content = enrich_order_content(content, account)
            except Exception as enrich_err:
                logger.warning(f"Order enrichment skipped: {enrich_err}")
        
        # Parse message timestamp
        from datetime import timedelta
        msg_timestamp = parse_whatsapp_timestamp(timestamp) or datetime.now(timezone.utc)
        
        # Create message record
        msg_record = WhatsAppMessage(
            conversation_id=conversation.id,
            direction="incoming",
            type=msg_type,
            content=content,
            wamid=wamid,
            status="received",
            created_at=msg_timestamp,
        )
        self.db_session.add(msg_record)
        # Flush early so duplicate wamid races are handled before any lazy-load
        # query (which can otherwise trigger autoflush at unpredictable points).
        try:
            self.db_session.flush()
        except IntegrityError as ie:
            self.db_session.rollback()
            duplicate = WhatsAppMessage.query.filter_by(wamid=wamid).first()
            if duplicate:
                trace_event(
                    stage="worker.inbound.duplicate",
                    status="skipped",
                    wamid=wamid,
                    conversation_id=duplicate.conversation_id,
                    details={"existing_message_id": duplicate.id, "source": "flush_race"},
                )
                logger.info(
                    "Duplicate inbound message race skipped (flush): wamid=%s existing_id=%s conversation_id=%s created_at=%s",
                    wamid,
                    duplicate.id,
                    duplicate.conversation_id,
                    duplicate.created_at,
                )
                return
            logger.exception("Incoming message flush failed for wamid=%s: %s", wamid, ie)
            return
        
        # Update conversation with session tracking
        conversation.last_message_at = msg_timestamp
        conversation.last_inbound_at = msg_timestamp
        conversation.session_expires_at = msg_timestamp + timedelta(hours=24)  # 24h window
        conversation.status = "open"  # Open on new message
        conversation.unread_count = (conversation.unread_count or 0) + 1
        
        # Process CTWA attribution if this is from an ad
        self._process_attribution(message, conversation, account=account)
        
        update_account_heartbeat(account, db_session=self.db_session, inbound=True)
        
        try:
            self.db_session.commit()
        except Exception as commit_err:
            logger.exception(f"Failed to commit incoming message: {commit_err}")
            self.db_session.rollback()
            return
        if debug_message_logs:
            print(f"✅ Stored message: id={msg_record.id}, type={msg_type}, content={content}")
        logger.info(f"Stored incoming message: {wamid} from {from_phone}")
        trace_event(
            stage="worker.inbound.stored",
            status="ok",
            wamid=wamid,
            conversation_id=conversation.id,
            account_id=account.id,
            details={"message_id": msg_record.id, "msg_type": msg_type},
        )

        # 1. Trigger Booking Notification check
        booking_text = ""
        if msg_type == "text":
            booking_text = extract_message_text(message) or ""
        elif msg_type == "interactive":
            interactive = message.get("interactive", {})
            int_type = interactive.get("type", "")
            if int_type == "button_reply":
                booking_text = interactive.get("button_reply", {}).get("title") or ""
            elif int_type == "list_reply":
                booking_text = interactive.get("list_reply", {}).get("title") or ""
        elif msg_type == "button":
            button_data = message.get("button", {}) or {}
            booking_text = button_data.get("text") or button_data.get("payload") or ""

        if booking_text:
            try:
                from .human_escalation import check_and_trigger_booking_notification
                check_and_trigger_booking_notification(account, conversation, booking_text, msg_record.id)
            except Exception as e:
                logger.error(f"Failed to run booking check: {e}")

        # 2. Trigger Catalog Cart Notification
        if msg_type == "order":
            try:
                from .human_escalation import send_cart_notification_email_bg
                print(f"📧 Catalog cart email flow start: message_id={msg_record.id} conversation_id={conversation.id}")
                logger.info(
                    "Running catalog cart email notification for message %s (conversation=%s)",
                    msg_record.id,
                    conversation.id,
                )
                # Run inline in worker job for deterministic delivery.
                # (Background thread submission could be skipped/saturated silently.)
                send_cart_notification_email_bg(
                    account_id=account.id,
                    conversation_id=conversation.id,
                    message_id=msg_record.id,
                    order_data=content,
                )
                print(f"📧 Catalog cart email flow done: message_id={msg_record.id}")
                logger.info("Catalog cart email flow completed for message %s", msg_record.id)
            except Exception as email_err:
                logger.exception(f"Failed to run catalog cart email flow: {email_err}")

            # Fire a CAPI InitiateCheckout (a cart submission — NOT a confirmed
            # payment) to the workspace's pixel for attribution. Fire-and-forget;
            # never breaks order processing.
            try:
                _items = (content or {}).get("product_items", []) if isinstance(content, dict) else []
                _total = sum(float(it.get("item_price") or 0) * int(it.get("quantity") or 1) for it in _items)
                _cur = (_items[0].get("currency") if _items else None) or "INR"
                if _total > 0:
                    from integrations.capi_service import send_capi_event
                    send_capi_event(
                        event_name="InitiateCheckout",
                        user_data_dict={"phone": conversation.user_phone, "name": conversation.user_name},
                        custom_data_dict={
                            "value": _total,
                            "currency": _cur,
                            "num_items": len(_items),
                            "content_ids": [it.get("product_retailer_id") for it in _items if it.get("product_retailer_id")],
                        },
                        workspace_id=account.workspace_id,
                        action_source="business_messaging",
                    )
            except Exception as _ic_err:
                logger.error(f"CAPI InitiateCheckout from WA order failed: {_ic_err}")

            # Auto-send a PayU payment link if enabled (SocioChat-only; removable).
            try:
                from flask import request as _flask_request
                _host = ""
                try:
                    _host = _flask_request.host_url
                except Exception:
                    _host = ""
                from whatsapp.commerce_pay.auto import maybe_auto_request_payment
                maybe_auto_request_payment(
                    account=account, conversation=conversation,
                    message_id=msg_record.id, order_content=content, host_url=_host,
                )
            except Exception as _auto_err:
                logger.error(f"commerce auto-pay hook failed: {_auto_err}")

        # --- Bulk-campaign reply attribution -------------------------------------
        # Resolve which bulk campaign (if any) this inbound message is replying to,
        # stamp the inbound message with that campaign_id, and persist replied state
        # on the matching drip enrollment so /campaigns/<id>/intelligence is
        # authoritative on reload. Previously the inbound message carried no
        # campaign_id and enrollment.replied was never written, so the bulk UI
        # matched replies to recipients by phone only and showed them against the
        # wrong campaign whenever a number was enrolled in more than one.
        reply_campaign_id = None
        reply_preview = None
        try:
            from datetime import timedelta as _td
            from .drip_models import WhatsAppDripEnrollment

            window_start = msg_timestamp - _td(hours=24)
            last_outbound = (
                WhatsAppMessage.query
                .filter(
                    WhatsAppMessage.conversation_id == conversation.id,
                    WhatsAppMessage.campaign_id.isnot(None),
                    WhatsAppMessage.direction.in_(["outgoing", "echo"]),
                    WhatsAppMessage.created_at >= window_start,
                    WhatsAppMessage.created_at <= msg_timestamp,
                )
                .order_by(WhatsAppMessage.created_at.desc())
                .first()
            )
            if last_outbound is not None and last_outbound.campaign_id:
                # Only treat THIS inbound as the campaign reply if it is the FIRST inbound
                # after the campaign send — otherwise an unrelated message later in the
                # thread (or hours after an ignored campaign) would inflate reply metrics
                # (enrollment.replied is a one-way latch).
                intervening_inbound = (
                    WhatsAppMessage.query
                    .filter(
                        WhatsAppMessage.conversation_id == conversation.id,
                        WhatsAppMessage.direction == "incoming",
                        WhatsAppMessage.id != msg_record.id,
                        WhatsAppMessage.created_at > last_outbound.created_at,
                        WhatsAppMessage.created_at < msg_timestamp,
                    )
                    .first()
                )
                if intervening_inbound is None:
                    reply_campaign_id = last_outbound.campaign_id
                    msg_record.campaign_id = reply_campaign_id

                    _preview = (booking_text or "").strip()
                    reply_preview = _preview[:280] or None

                    # Prefer an exact link via the outbound wamid; fall back to phone forms.
                    enrollment = None
                    if last_outbound.wamid:
                        enrollment = WhatsAppDripEnrollment.query.filter_by(
                            campaign_id=reply_campaign_id, message_id=last_outbound.wamid
                        ).first()
                    if enrollment is None:
                        _phones = []
                        try:
                            from .utils import normalize_phone_robust as _npr
                            _n = _npr(from_phone)
                            if _n:
                                _phones.append(_n)
                        except Exception:
                            pass
                        for _cand in (from_phone, getattr(conversation, "user_phone", None)):
                            if _cand and _cand not in _phones:
                                _phones.append(_cand)
                        for _p in _phones:
                            enrollment = WhatsAppDripEnrollment.query.filter_by(
                                campaign_id=reply_campaign_id, phone_number=_p
                            ).first()
                            if enrollment is not None:
                                break
                    if enrollment is not None and not enrollment.replied:
                        enrollment.replied = True
                        enrollment.replied_at = msg_timestamp

                    self.db_session.commit()
        except Exception as _attr_err:
            self.db_session.rollback()
            logger.warning("Bulk reply attribution failed (conversation %s): %s", conversation.id, _attr_err)
            reply_campaign_id = None
            reply_preview = None

        # Broadcast real-time event
        try:
            notification_manager.broadcast("whatsapp_message_received", {
                "message": msg_record.to_dict(),
                "conversation": conversation.to_dict(),  # Include full conversation state
                "conversation_id": conversation.id,
                "account_id": account.id,
                "workspace_id": account.workspace_id,
                "user_phone": conversation.user_phone,
                "user_name": conversation.user_name,
                # Bulk reply correlation so the campaign UI patches the RIGHT campaign
                # row (the FE guard keys off campaign_id; was previously absent/dead).
                "campaign_id": reply_campaign_id,
                "reply_preview": reply_preview,
            })
        except Exception as e:
            logger.error(f"Failed to broadcast message received event: {e}")

        # ============================================================
        # Mobile PUSH notification (additive, best-effort).
        # The message is already saved+committed above, so this can NEVER affect
        # message processing. Fully isolated in try/except like the CRM block.
        # Sends only to devices registered for this workspace (mobile users);
        # the web registers none, so this is a no-op for web.
        # ============================================================
        try:
            from push.expo_push import send_push_to_workspace
            _preview = None
            try:
                _md = msg_record.to_dict()
                _content = _md.get("content")
                if isinstance(_content, dict):
                    _preview = _content.get("text") or _content.get("body") or _content.get("caption")
                elif isinstance(_content, str):
                    _preview = _content
                _preview = _preview or _md.get("body")
            except Exception:
                _preview = None
            _title = conversation.user_name or conversation.user_phone or "New message"
            _body = _preview or "New WhatsApp message"
            send_push_to_workspace(
                account.workspace_id,
                title=_title,
                body=_body,
                data={"conversation_id": conversation.id, "type": "whatsapp_message"},
            )
        except Exception as e:
            logger.warning(f"Push notify skipped (non-fatal): {e}")

        # ============================================================
        # CRM: Auto-capture conversation into the CRM as a Lead.
        # Lazy import to avoid circular imports (SocioviaCrm <-> whatsapp).
        # New conversations create a Lead; existing ones refresh
        # last_interaction_at (dedupe-safe) and advance status -> "contacted".
        # ALL CRM calls are isolated in try/except so a CRM failure NEVER breaks
        # message processing (chat is the critical path).
        # ============================================================
        try:
            from SocioviaCrm.lead_ingest import upsert_lead_from_conversation
            is_new_conversation = getattr(conversation, "_is_new", False)
            if is_new_conversation:
                upsert_lead_from_conversation(
                    conversation, account, db_session=self.db_session
                )
                logger.info(
                    f"Auto-captured CRM lead for new conversation {conversation.id}"
                )
            else:
                # Cheap refresh of last_interaction_at on an already-known lead.
                upsert_lead_from_conversation(
                    conversation, account, db_session=self.db_session
                )
                # CRM: auto-advance lead status -> contacted (a reply on an
                # existing conversation). Forward-only/idempotent; new
                # conversations stay "new" so this lives only in the else branch.
                try:
                    from SocioviaCrm.lead_ingest import advance_lead_status_from_conversation
                    advance_lead_status_from_conversation(
                        conversation, account, "contacted",
                        reason="Replied on WhatsApp", db_session=self.db_session
                    )
                except Exception as e:
                    logger.exception(f"Failed to advance CRM lead status to contacted: {e}")
        except Exception as e:
            logger.exception(f"Failed to auto-capture CRM lead from conversation: {e}")

        # Process automation rules (after commit to avoid blocking)
        # Handle both text messages and interactive button replies
        if msg_type == "text":
            text_content = extract_message_text(message)
            if text_content:
                self._process_automation(
                    account=account,
                    conversation=conversation,
                    message_text=text_content,
                    message_id=msg_record.id,
                    from_phone=from_phone,
                    is_button_reply=False,
                    button_payload=None,
                    inbound_wamid=wamid,
                )
            # Check for lead keywords in text messages
            self._check_for_lead_keywords(account, conversation, text_content)
            # CRM Phase 2: buying-intent keyword rule -> advance lead to rule's
            # status (per-workspace configurable). Forward-only/idempotent; isolated
            # so CRM logic never breaks message processing. Runs AFTER the
            # reply->contacted hook so cheap signals win first.
            rule = None
            try:
                from SocioviaCrm.qualify_keywords import match_qualify_rule
                from SocioviaCrm.lead_ingest import advance_lead_status_from_conversation
                rule = match_qualify_rule(text_content, account.workspace_id)
                if rule:
                    advance_lead_status_from_conversation(
                        conversation, account, rule["status"],
                        reason=f"Keyword '{rule['keyword']}' -> {rule['status']}",
                        db_session=self.db_session)
            except Exception as e:
                logger.exception(f"interest-keyword qualify hook failed: {e}")
            # CRM Phase 3: AI fallback classifier. ONLY runs when the keyword rule
            # did NOT match (rule is None). classify_lead_status returns None when
            # AI is disabled / chit-chat / error, so this is cheap when off.
            # Fail-safe — never breaks message processing.
            if rule is None:
                try:
                    from SocioviaCrm.ai_status_classifier import classify_lead_status
                    from SocioviaCrm.lead_ingest import advance_lead_status_from_conversation
                    _st = classify_lead_status(text_content, account.workspace_id)
                    if _st:
                        advance_lead_status_from_conversation(
                            conversation, account, _st,
                            reason=f"AI classified -> {_st}",
                            db_session=self.db_session)
                except Exception as e:
                    logger.exception(f"AI status classify hook failed: {e}")
        elif msg_type == "interactive":
            # Handle button replies from interactive messages
            interactive = message.get("interactive", {})
            int_type = interactive.get("type", "")
            
            button_payload = None
            button_title = ""
            
            if int_type == "button_reply":
                reply = interactive.get("button_reply", {})
                button_payload = reply.get("id")
                button_title = reply.get("title", "")
            elif int_type == "list_reply":
                reply = interactive.get("list_reply", {})
                button_payload = reply.get("id")
                button_title = reply.get("title", "")
            elif int_type == "nfm_reply":
                reply = interactive.get("nfm_reply", {})
                button_title = reply.get("body") or "Flow completed"
                # Appointment booking: a submitted Flow form carries its answers in
                # `response_json`. If it contains date+time fields, create the booking
                # row (status=confirmed) and stamp/schedule its reminder. Without this
                # the whole booking pipeline (Bookings tab + reminders) never fires.
                try:
                    from .flow_os_routes import maybe_create_booking_from_submission
                    response_json = reply.get("response_json")
                    if isinstance(response_json, str):
                        import json as _json
                        try:
                            response_json = _json.loads(response_json)
                        except _json.JSONDecodeError:
                            response_json = {}
                    if isinstance(response_json, dict) and response_json:
                        maybe_create_booking_from_submission(
                            account_id=account.id,
                            conversation_id=conversation.id,
                            wa_id=from_phone,
                            response_json=response_json,
                            flow_id=reply.get("flow_id"),
                        )
                        self.db_session.commit()
                except Exception as booking_err:
                    logger.warning("Could not create booking from flow submission: %s", booking_err)
                    try:
                        self.db_session.rollback()
                    except Exception:
                        pass
                self._process_automation(
                    account=account,
                    conversation=conversation,
                    message_text=button_title,
                    message_id=msg_record.id,
                    from_phone=from_phone,
                    is_button_reply=False,
                    button_payload=None,
                    inbound_wamid=wamid,
                    is_flow_reply=True,
                    raw_message=message,
                )
                # CRM: auto-advance lead status -> qualified (completed a flow).
                # Forward-only/idempotent; isolated so CRM logic never breaks msg
                # processing.
                try:
                    from SocioviaCrm.lead_ingest import advance_lead_status_from_conversation
                    advance_lead_status_from_conversation(
                        conversation, account, "qualified",
                        reason="Completed WhatsApp flow", db_session=self.db_session
                    )
                except Exception as e:
                    logger.exception(f"Failed to advance CRM lead status to qualified (flow): {e}")

            if button_payload:
                self._process_automation(
                    account=account,
                    conversation=conversation,
                    message_text=button_title,
                    message_id=msg_record.id,
                    from_phone=from_phone,
                    is_button_reply=True,
                    button_payload=button_payload,
                    inbound_wamid=wamid,
                )
        elif msg_type == "button":
            # Template quick-reply buttons (not flow-builder interactive buttons)
            button_data = message.get("button", {}) or {}
            button_payload = button_data.get("payload") or button_data.get("text") or ""
            button_title = button_data.get("text") or button_payload
            if button_payload or button_title:
                self._process_automation(
                    account=account,
                    conversation=conversation,
                    message_text=button_title or button_payload,
                    message_id=msg_record.id,
                    from_phone=from_phone,
                    is_button_reply=True,
                    button_payload=str(button_payload),
                    inbound_wamid=wamid,
                )
        
        # Check for Lead Keywords / Ref Tags in text messages
        if msg_type == "text":
            text_content = extract_message_text(message)
            self._check_for_lead_keywords(account, conversation, text_content)
    
    def _check_for_lead_keywords(self, account, conversation, text):
        """
        Check message text for lead identifiers (Ref tags, campaign IDs).
        Suggested by user: "Interested_in_Lead", "Campaign_ID_987".
        """
        if not text:
            return
            
        text_lower = text.lower()
        lead_triggers = ["interested_in_lead", "campaign_id_", "ref:", "ad_id:"]
        
        matched = False
        for trigger in lead_triggers:
            if trigger in text_lower:
                matched = True
                break
        
        if matched:
            logger.info(f"Lead trigger matched in text: '{text}' for conversation {conversation.id}")
            # Try to extract a specific ID if present (e.g. Campaign_ID_987 -> 987)
            ad_id = None
            import re
            match = re.search(r"(?:campaign_id_|ref:|ad_id:)\s*(\w+)", text, re.I)
            if match:
                ad_id = match.group(1)
            
            self._trigger_capi_lead_event(account, conversation, ad_id=ad_id)

    def _trigger_capi_lead_event(self, account, conversation, ad_id=None):
        """
        Trigger a 'Lead' event to Meta Conversions API.
        """
        try:
            # Avoid duplicate Lead events in short succession (e.g. within 1 hour)
            # You might want to implement a more robust check based on ad_id
            
            user_data = {
                "phone": conversation.user_phone,
                "name": conversation.user_name,
            }
            
            custom_data = {
                "lead_type": "whatsapp_inquiry",
                "source": "whatsapp_wehook"
            }
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
                action_source="business_messaging"
            )
            logger.info(f"Triggered CAPI Lead event for workspace {account.workspace_id}, phone {conversation.user_phone}")
            
        except Exception as e:
            logger.error(f"Failed to trigger CAPI Lead event: {e}")
    
    def _process_status(self, status: Dict[str, Any], phone_number_id: str):
        """
        Process a delivery status update.
        
        Args:
            status: Status object from webhook
            phone_number_id: Our phone number ID
        """
        from datetime import timedelta
        
        wamid = status.get("id")
        status_value = status.get("status")  # sent, delivered, read, failed
        timestamp = status.get("timestamp")
        recipient = status.get("recipient_id")
        
        if not wamid or not status_value:
            return
        
        ts = parse_whatsapp_timestamp(timestamp) if timestamp else datetime.now(timezone.utc)
        
        # Log status event for debugging/history (always log for audit)
        error_code = None
        error_message = None
        error_href = None
        if status_value == "failed":
            errors = status.get("errors", [])
            if errors:
                error = errors[0] or {}
                error_code = str(error.get("code", "")) if error.get("code") is not None else None
                title = error.get("title") or error.get("message") or "Unknown error"
                details = (error.get("error_data") or {}).get("details")
                error_href = error.get("href")

                # Keep the primary reason first, then include actionable details/link when present.
                message_parts = [str(title).strip()]
                if details:
                    message_parts.append(str(details).strip())
                if error_href:
                    message_parts.append(f"More info: {error_href}")
                error_message = "\n".join([part for part in message_parts if part])

        status_event = MessageStatusEvent(
            wamid=wamid,
            status=status_value,
            timestamp=ts,
            error_code=error_code,
            error_message=error_message,
            raw_event=status,  # Store full event for debugging
        )
        self.db_session.add(status_event)
        
        # Find the message by wamid
        message = WhatsAppMessage.query.filter_by(wamid=wamid).first()
        
        if not message:
            logger.debug(f"Status update for unknown message: {wamid}")
            try:
                self.db_session.commit()  # Still commit the status event
            except Exception as e:
                logger.warning(f"Failed to commit status event: {e}")
                self.db_session.rollback()
            return
        
        # Update status
        old_status = message.status
        message.status = status_value
        
        if status_value == "sent":
            message.sent_at = ts
            # Update conversation outbound tracking
            if message.conversation:
                message.conversation.last_outbound_at = ts
        elif status_value == "delivered":
            message.delivered_at = ts
        elif status_value == "read":
            message.read_at = ts
        elif status_value == "failed":
            message.error_code = error_code
            message.error_message = error_message
            # Log campaign-level failure for visibility
            if message.campaign_id:
                logger.warning(
                    f"Campaign {message.campaign_id} message delivery failed: "
                    f"recipient={recipient}, wamid={wamid}, "
                    f"error_code={error_code}, error={error_message}"
                )
        
        acc = message.conversation.account if message.conversation else None
        if acc:
            if status_value == "sent":
                update_account_heartbeat(acc, db_session=self.db_session, outbound=True)
            elif status_value == "failed" and (error_message or error_code):
                update_account_heartbeat(
                    acc,
                    db_session=self.db_session,
                    inbound=True,
                    error={"code": error_code, "message": error_message or "delivery_failed"},
                )
        
        try:
            self.db_session.commit()
        except Exception as e:
            logger.warning(f"Failed to commit status update: {e}")
            self.db_session.rollback()
            return
        logger.debug(f"Updated message status: {wamid} {old_status} -> {status_value}")

        # Broadcast real-time status update
        try:
            if message and message.conversation:
                account = message.conversation.account
                # If account relationship isn't loaded, access via ID might be safer if lazy loading issues, 
                # but explicit access usually triggers load. 
                # fallback to None if not available to avoid error
                workspace_id = account.workspace_id if account else None
                account_id = message.conversation.account_id

                notification_manager.broadcast("whatsapp_message_status", {
                    "wamid": wamid,
                    "status": status_value,
                    "timestamp": ts.isoformat(),
                    "conversation_id": message.conversation_id,
                    "account_id": account_id,
                    "workspace_id": workspace_id,
                    "campaign_id": message.campaign_id,
                    "recipient_phone": message.conversation.user_phone if message.conversation else recipient,
                    "error_code": message.error_code,
                    "error_message": message.error_message,
                })
        except Exception as e:
            logger.error(f"Failed to broadcast status update event: {e}")
    
    def _process_error(self, error: Dict[str, Any], phone_number_id: str):
        """
        Process an error event.
        
        Args:
            error: Error object from webhook
            phone_number_id: Our phone number ID
        """
        error_code = error.get("code")
        error_title = error.get("title")
        error_message = error.get("message")
        error_details = error.get("error_data", {})
        
        logger.error(
            f"WhatsApp error: code={error_code}, title={error_title}, "
            f"message={error_message}, details={error_details}"
        )
        account = self._get_or_create_account(phone_number_id)
        if account:
            update_account_heartbeat(
                account,
                db_session=self.db_session,
                inbound=True,
                error={
                    "code": error_code,
                    "message": error_message or error_title,
                    "title": error_title,
                },
            )
            try:
                self.db_session.commit()
            except Exception as ce:
                logger.warning(f"Failed to commit error heartbeat: {ce}")
                try:
                    self.db_session.rollback()
                except Exception:
                    pass
    
    def _process_account_update(self, waba_id: str, value: Dict[str, Any], raw_json: str):
        """
        Handle account_update webhooks (e.g. PHONE_NUMBER_QUALITY_UPDATE from Meta).

        Ref: https://developers.facebook.com/docs/graph-api/webhooks/reference/whatsapp_business_account
        """
        try:
            event = (value.get("event") or "").strip().upper()
            phone_number_id = value.get("phone_number_id")
            if not phone_number_id and isinstance(value.get("phone_number"), dict):
                phone_number_id = value["phone_number"].get("id")
            if phone_number_id is not None:
                phone_number_id = str(phone_number_id)

            account = None
            if phone_number_id:
                account = self.db_session.query(WhatsAppAccount).filter_by(
                    phone_number_id=phone_number_id
                ).first()
            if not account and waba_id:
                account = self._get_account_by_waba_id(waba_id)

            if not account:
                self._log_webhook(raw_json, "account_update", phone_number_id, "Account not found for account_update")
                return

            update_account_heartbeat(account, db_session=self.db_session, inbound=True)

            if event in ("PARTNER_REMOVED", "ACCOUNT_OFFBOARDED"):
                account.is_active = False
                account.sync_status = "disconnected"
                if event == "PARTNER_REMOVED":
                    account.is_coexistence = False
                    disconnection = value.get("disconnection_info") or {}
                    account.webhook_last_error = (
                        f"PARTNER_REMOVED: {disconnection.get('reason') or event}"
                    )[:2000]
                logger.warning(
                    "Coexistence/account disconnect event=%s account=%s waba=%s",
                    event,
                    account.id,
                    waba_id,
                )

            elif event == "ACCOUNT_RECONNECTED":
                account.is_active = True
                account.is_coexistence = True
                account.sync_status = "syncing"
                account.coexistence_paired_at = datetime.now(timezone.utc)
                logger.info("Coexistence ACCOUNT_RECONNECTED account=%s", account.id)

            if "QUALITY" in event or event == "PHONE_NUMBER_QUALITY_UPDATE":
                rating = (
                    value.get("quality_rating")
                    or value.get("current_quality_rating")
                    or value.get("new_quality_rating")
                    or value.get("quality_score")
                )
                if rating is not None:
                    shadow_sync_account_quality_rating(
                        account,
                        str(rating).strip(),
                        db_session=self.db_session,
                    )
                    
            if "RESTRICTION" in event or event in ("ACCOUNT_UPDATE", "ACCOUNT_RESTRICTION", "PHONE_NUMBER_RESTRICTION"):
                restriction = value.get("restriction_type") or value.get("new_restriction_type")
                if restriction is not None:
                    shadow_sync_account_restriction_state(
                        account,
                        str(restriction).strip(),
                        db_session=self.db_session,
                    )

            self.db_session.commit()
            self._log_webhook(raw_json, "account_update", account.phone_number_id)
        except Exception as e:
            logger.exception(f"Error processing account_update: {e}")
            self._log_webhook(raw_json, "account_update", None, str(e))
            try:
                self.db_session.rollback()
            except Exception:
                pass
    
    def _extract_content(self, message: Dict[str, Any], msg_type: str) -> Dict[str, Any]:
        """
        Extract content from message based on type.
        
        Args:
            message: Message object
            msg_type: Message type
            
        Returns:
            Content dict
        """
        content: Dict[str, Any] = {"type": msg_type}
        
        if msg_type == "text":
            content["text"] = message.get("text", {}).get("body", "")
        
        elif msg_type in ("image", "video", "audio", "document", "sticker"):
            media_info = extract_media_info(message)
            if media_info:
                content.update(media_info)
        
        elif msg_type == "location":
            location = message.get("location", {})
            content.update({
                "latitude": location.get("latitude"),
                "longitude": location.get("longitude"),
                "name": location.get("name"),
                "address": location.get("address"),
            })
        
        elif msg_type == "contacts":
            content["contacts"] = message.get("contacts", [])
        
        elif msg_type == "interactive":
            interactive = message.get("interactive", {})
            int_type = interactive.get("type", "")
            content["interactive_type"] = int_type
            
            if int_type == "button_reply":
                reply = interactive.get("button_reply", {})
                content["button_id"] = reply.get("id")
                content["button_title"] = reply.get("title")
            elif int_type == "list_reply":
                reply = interactive.get("list_reply", {})
                content["list_id"] = reply.get("id")
                content["list_title"] = reply.get("title")
                content["list_description"] = reply.get("description")
            elif int_type == "nfm_reply":
                # Flow form submission — persist the answers so the Submissions tab
                # (which reads content.response_json) can list it. Without this the
                # submission is stored but shows nothing.
                nfm = interactive.get("nfm_reply", {})
                content["response_json"] = nfm.get("response_json")
                content["flow_id"] = nfm.get("flow_id") or interactive.get("flow_id")
                content["submission_status"] = "received"

        elif msg_type == "button":
            content["button_text"] = message.get("button", {}).get("text", "")
            content["button_payload"] = message.get("button", {}).get("payload", "")
        
        elif msg_type == "order":
            order = message.get("order", {}) or {}
            content.update({
                "catalog_id": order.get("catalog_id"),
                "text": order.get("text"),
                "product_items": order.get("product_items") or order.get("items") or [],
                "order": order,
                "raw": message,
            })
        
        else:
            # Store raw for unknown types
            content["raw"] = message
        
        return content
    
    def _get_or_create_account(self, phone_number_id: str) -> Optional[WhatsAppAccount]:
        """
        Get WhatsApp account by phone_number_id.
        
        Returns None if:
        - Account doesn't exist (don't auto-create for incoming webhooks)
        - Account exists but is inactive (unlinked)
        """
        account = WhatsAppAccount.query.filter_by(phone_number_id=phone_number_id).first()
        
        if not account:
            # Don't auto-create accounts for incoming webhooks
            # Accounts should be created via OAuth flow
            logger.warning(f"No account found for phone_number_id: {phone_number_id}")
            return None
        
        if not account.is_active:
            # Skip inactive (unlinked) accounts
            logger.info(f"Skipping inactive account: {phone_number_id}")
            return None
        
        return account
    
    def _get_or_create_conversation(
        self,
        account_id: int,
        user_phone: str,
        user_name: Optional[str] = None,
    ) -> WhatsAppConversation:
        """Get or create conversation, with phone normalization and dedup."""
        # Canonical form (e.g. "919999320932")
        canonical = normalize_phone(user_phone)

        # 1. Exact match on canonical phone
        conversation = WhatsAppConversation.query.filter_by(
            account_id=account_id,
            user_phone=canonical,
        ).first()

        # 2. Fallback: find by last 10 digits (catches old un-normalized rows)
        if not conversation and len(canonical) > 10:
            last10 = canonical[-10:]
            conversation = WhatsAppConversation.query.filter(
                WhatsAppConversation.account_id == account_id,
                WhatsAppConversation.user_phone.in_([last10, canonical]),
            ).order_by(WhatsAppConversation.last_message_at.desc().nullslast()).first()
            # Migrate the old row to canonical form so future lookups are instant
            if conversation and conversation.user_phone != canonical:
                logger.info(f"Migrating conversation {conversation.id} phone {conversation.user_phone} → {canonical}")
                conversation.user_phone = canonical
        
        if not conversation:
            conversation = WhatsAppConversation(
                account_id=account_id,
                user_phone=canonical,
                user_name=user_name,
                status="open",
                unread_count=0,
            )
            self.db_session.add(conversation)
            self.db_session.flush()
            # Transient marker so the CRM auto-capture hook can tell a brand-new
            # conversation (create a Lead) from a returning one (refresh + advance).
            conversation._is_new = True
            logger.info(f"Created conversation with: {canonical}")
        else:
            conversation._is_new = False
            if user_name and conversation.user_name != user_name:
                # Update name if it changed or was missing
                conversation.user_name = user_name

        return conversation
    
    def _process_attribution(
        self,
        message: Dict[str, Any],
        conversation: WhatsAppConversation,
        account: Optional[WhatsAppAccount] = None,
    ):
        """
        Process CTWA attribution if this message came from an ad click.
        
        Args:
            message: Message object from webhook
            conversation: WhatsAppConversation to update
        """
        referral = message.get("referral")
        if not referral:
            return  # Not from an ad
        
        source_type = referral.get("source_type")
        if source_type != "ad":
            return  # Not an ad referral
        
        # Only attribute on first message (if not already attributed)
        if conversation.entry_source == "ctwa" and conversation.ad_id:
            logger.debug(f"Conversation {conversation.id} already attributed")
            return
        
        try:
            from ctwa.attribution import parse_referral
            
            attribution = parse_referral(message)
            if attribution:
                conversation.entry_source = "ctwa"
                conversation.ctwa_clid = attribution.ctwa_clid
                conversation.ad_id = attribution.ad_id
                conversation.attribution_data = attribution.to_dict()
                conversation.attributed_at = datetime.now(timezone.utc)
                
                logger.info(
                    f"Attributed conversation {conversation.id} to ad {attribution.ad_id}"
                )
                
                # Trigger CAPI Lead event for official ad click
                self._trigger_capi_lead_event(account, conversation, ad_id=attribution.ad_id)

                # Auto-create a CRM lead from this ad conversation IF the ad's campaign
                # has "create leads" enabled (per-campaign opt-in set in the ad wizard).
                try:
                    from ctwa.models import CTWACampaign
                    _camp = None
                    if getattr(attribution, "ad_id", None):
                        _camp = CTWACampaign.query.filter(CTWACampaign.meta_ad_id == attribution.ad_id).first()
                    if _camp is None and getattr(attribution, "campaign_id", None):
                        _camp = CTWACampaign.query.filter(CTWACampaign.meta_campaign_id == attribution.campaign_id).first()
                    # Default TRUE when we can't find our campaign row (external/legacy ad) so leads aren't silently dropped.
                    if _camp is None or getattr(_camp, "create_leads", True):
                        from SocioviaCrm.lead_ingest import upsert_lead_from_conversation
                        upsert_lead_from_conversation(conversation, account)
                        logger.info(f"CTWA lead upserted for conv {conversation.id} (campaign flag on)")
                    else:
                        logger.info(f"CTWA lead skipped for conv {conversation.id}: create_leads disabled")
                except Exception:
                    logger.exception("CTWA lead upsert failed (non-fatal)")
        except Exception as e:
            logger.exception(f"Failed to process attribution: {e}")
    
    def _find_contact(self, phone: str, contacts: List[Dict]) -> Optional[Dict]:
        """Find contact info by phone number."""
        for contact in contacts:
            if contact.get("wa_id") == phone:
                return contact
        return None
    
    def _trigger_capi_lead_event(
        self,
        account: WhatsAppAccount,
        conversation: WhatsAppConversation,
        ad_id: Optional[str] = None,
        lead_type: str = "ctwa",
        ctwa_clid: Optional[str] = None,
    ):
        """
        Send a 'Lead' event to Meta CAPI for attribution.
        """
        try:
            # Fall back to the click id stored on the conversation when the caller
            # didn't pass one — otherwise CTWA attribution is silently lost.
            ctwa_clid = ctwa_clid or getattr(conversation, "ctwa_clid", None)
            user_data = {
                "phone": conversation.user_phone,
            }
            if conversation.user_name:
                user_data["name"] = conversation.user_name
            if ctwa_clid:
                user_data["ctwa_clid"] = ctwa_clid

            custom_data = {
                "lead_type": lead_type,
                "source": "whatsapp",
            }
            if ad_id:
                custom_data["ad_id"] = ad_id

            result = send_capi_event(
                event_name="Lead",
                user_data_dict=user_data,
                custom_data_dict=custom_data,
                workspace_id=account.workspace_id,
                action_source="business_messaging",
            )
            logger.info(f"CAPI Lead event sent for conv {conversation.id}: {result}")
        except Exception as e:
            logger.exception(f"Failed to send CAPI Lead event: {e}")
    
    def _check_for_lead_keywords(
        self,
        account: WhatsAppAccount,
        conversation: WhatsAppConversation,
        text: str,
    ):
        """
        Check incoming text for lead-identifying keywords/ref params.
        If found, trigger a CAPI Lead event.
        """
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
                last_idx = match.lastindex
                if last_idx is not None and last_idx >= 1:
                    ad_id = match.group(1)
                break

        if matched:
            # Tag conversation as keyword-sourced lead if not already attributed
            if not conversation.entry_source:
                conversation.entry_source = "keyword"
                if ad_id:
                    conversation.ad_id = ad_id
            self._trigger_capi_lead_event(
                account, conversation, ad_id=ad_id, lead_type="keyword"
            )
            logger.info(f"Keyword lead detected in conv {conversation.id}, ad_id={ad_id}")
            # CRM: auto-advance lead status -> qualified (matched interest keyword).
            # Forward-only/idempotent; isolated so CRM logic never breaks msg
            # processing.
            try:
                from SocioviaCrm.lead_ingest import advance_lead_status_from_conversation
                advance_lead_status_from_conversation(
                    conversation, account, "qualified",
                    reason="Matched interest keyword", db_session=self.db_session
                )
            except Exception as e:
                logger.exception(f"Failed to advance CRM lead status to qualified (keyword): {e}")

    @staticmethod
    def _outbound_text(msg: WhatsAppMessage) -> str:
        content = msg.content or {}
        if isinstance(content, dict):
            return str(content.get("text") or content.get("body") or "").strip()
        return str(content).strip()

    @staticmethod
    def _automation_replay_delay_sec() -> float:
        try:
            return max(0.0, float(os.getenv("WHATSAPP_AUTOMATION_REPLAY_DELAY_SEC", "2")))
        except (TypeError, ValueError):
            return 2.0

    @staticmethod
    def _try_acquire_automation_lock(wamid: str, *, ttl_seconds: int = 180) -> bool:
        if not wamid:
            return True
        try:
            from core.cache import get_redis_client
            from core.cache.redis_client import WA_KEY_PREFIX

            client = get_redis_client()
            key = f"{WA_KEY_PREFIX}automation:lock:{wamid}"
            return bool(client.set(key, "1", nx=True, ex=max(30, int(ttl_seconds))))
        except Exception as exc:
            logger.warning("[webhook] automation lock unavailable (%s); proceeding", exc)
            return True

    @staticmethod
    def _automation_claim_key(wamid: str, role: str = "primary") -> str:
        return f"automation:{role}:{wamid}"

    @classmethod
    def _has_db_automation_claim(cls, wamid: str, *, role: str = "primary") -> bool:
        if not wamid:
            return False
        from whatsapp.drip_models import WhatsAppSendIdempotency

        return (
            WhatsAppSendIdempotency.query.filter_by(
                dedup_key=cls._automation_claim_key(wamid, role)
            ).first()
            is not None
        )

    @classmethod
    def _try_acquire_db_automation_claim(cls, wamid: str, *, role: str = "primary") -> bool:
        """
        Cross-environment automation mutex (shared Postgres).

        Unlike Redis locks, this works when local Docker and production Cloud Run
        both process the same WABA against one database.
        """
        if not wamid:
            return True
        from whatsapp.drip_models import WhatsAppSendIdempotency

        key = cls._automation_claim_key(wamid, role)
        try:
            db.session.add(WhatsAppSendIdempotency(dedup_key=key))
            db.session.commit()
            return True
        except IntegrityError:
            db.session.rollback()
            return False
        except Exception as exc:
            db.session.rollback()
            logger.warning("[webhook] DB automation claim unavailable (%s); proceeding", exc)
            return True

    @staticmethod
    def _duplicate_replay_mode_for_account(account_id: Optional[int]) -> str:
        """
        fallback_recovery (default): re-run AI when only the generic fallback was sent.
        only_if_no_reply: never replay once any outbound exists (avoids fallback+AI double
        sends while production and local Docker share one database).

        Per-account override: WHATSAPP_DUPLICATE_REPLAY_ONLY_IF_NO_REPLY_ACCOUNTS=13,55
        """
        if account_id is not None:
            raw_accounts = os.getenv(
                "WHATSAPP_DUPLICATE_REPLAY_ONLY_IF_NO_REPLY_ACCOUNTS", ""
            )
            account_ids = {
                int(part.strip())
                for part in raw_accounts.split(",")
                if part.strip().isdigit()
            }
            if account_id in account_ids:
                return "only_if_no_reply"

        mode = (os.getenv("WHATSAPP_DUPLICATE_REPLAY_MODE") or "fallback_recovery").strip().lower()
        if mode in {"only_if_no_reply", "no_reply_only", "none"}:
            return "only_if_no_reply"
        return "fallback_recovery"

    @staticmethod
    def _duplicate_peer_wait_sec() -> float:
        try:
            return max(0.0, float(os.getenv("WHATSAPP_DUPLICATE_PEER_WAIT_SEC", "28")))
        except (TypeError, ValueError):
            return 28.0

    def _wait_for_peer_automation_outcome(
        self,
        inbound: WhatsAppMessage,
        *,
        max_wait_sec: float,
    ) -> str:
        """
        Poll for another worker/environment to finish automation.

        Returns: substantive | fallback_only | none
        """
        from .ai_chatbot import DEFAULT_FALLBACK_MESSAGE

        marker = "couldn't process your request"
        fallback_snippet = (
            marker if marker in DEFAULT_FALLBACK_MESSAGE else DEFAULT_FALLBACK_MESSAGE[:40]
        )
        deadline = time.time() + max_wait_sec
        interval = 2.0
        while True:
            outbounds = (
                WhatsAppMessage.query.filter(
                    WhatsAppMessage.conversation_id == inbound.conversation_id,
                    WhatsAppMessage.id > inbound.id,
                    WhatsAppMessage.direction == "outgoing",
                )
                .order_by(WhatsAppMessage.id.asc())
                .all()
            )
            if not outbounds:
                if time.time() >= deadline:
                    return "none"
            else:
                has_substantive = False
                for msg in outbounds:
                    text = self._outbound_text(msg)
                    if not text:
                        continue
                    if fallback_snippet not in text.lower():
                        has_substantive = True
                        break
                if has_substantive:
                    return "substantive"
                if time.time() >= deadline:
                    return "fallback_only"

            if time.time() >= deadline:
                return "none"
            time.sleep(min(interval, max(0.1, deadline - time.time())))

    def _inbound_needs_automation_replay(self, inbound: WhatsAppMessage) -> bool:
        """
        True when a duplicate webhook should re-run automation because the
        first handler only sent the generic fallback (or nothing).
        """
        from .ai_chatbot import DEFAULT_FALLBACK_MESSAGE

        marker = "couldn't process your request"
        fallback_snippet = marker if marker in DEFAULT_FALLBACK_MESSAGE else DEFAULT_FALLBACK_MESSAGE[:40]

        outbounds = (
            WhatsAppMessage.query.filter(
                WhatsAppMessage.conversation_id == inbound.conversation_id,
                WhatsAppMessage.id > inbound.id,
                WhatsAppMessage.direction == "outgoing",
            )
            .order_by(WhatsAppMessage.id.asc())
            .all()
        )
        if not outbounds:
            return True

        for msg in outbounds:
            text = self._outbound_text(msg)
            if not text:
                continue
            if fallback_snippet not in text.lower():
                return False
        return True

    def _maybe_replay_automation_on_duplicate(
        self,
        *,
        existing: WhatsAppMessage,
        message: Dict[str, Any],
        contact: Optional[Dict[str, Any]],
        phone_number_id: str,
        inbound_wamid: str,
    ) -> None:
        """Re-run AI routing when Meta retries after a failed/partial first pass."""
        try:
            if not self._inbound_needs_automation_replay(existing):
                trace_event(
                    stage="worker.automation.replay",
                    status="skipped",
                    wamid=inbound_wamid,
                    conversation_id=existing.conversation_id,
                    details={"reason": "substantive_reply_exists"},
                )
                return

            account = self._get_or_create_account(phone_number_id)
            if not account:
                return

            conversation = WhatsAppConversation.query.get(existing.conversation_id)
            if not conversation:
                return

            msg_type = get_message_type(message)
            from_phone = normalize_phone(message.get("from") or conversation.user_phone or "")
            if not from_phone:
                return

            message_text = ""
            is_button_reply = False
            button_payload = None

            if msg_type == "text":
                message_text = extract_message_text(message) or ""
                if not message_text and existing.type == "text":
                    stored = existing.content or {}
                    if isinstance(stored, dict):
                        message_text = str(stored.get("text") or "").strip()
            elif msg_type == "interactive":
                interactive = message.get("interactive", {}) or {}
                int_type = interactive.get("type", "")
                is_button_reply = True
                if int_type == "button_reply":
                    reply = interactive.get("button_reply", {}) or {}
                    button_payload = reply.get("id")
                    message_text = reply.get("title", "")
                elif int_type == "list_reply":
                    reply = interactive.get("list_reply", {}) or {}
                    button_payload = reply.get("id")
                    message_text = reply.get("title", "")
            elif msg_type == "button":
                button_data = message.get("button", {}) or {}
                is_button_reply = True
                button_payload = button_data.get("payload") or button_data.get("text") or ""
                message_text = button_data.get("text") or button_payload

            if not message_text and existing.type == "text":
                stored = existing.content or {}
                if isinstance(stored, dict):
                    message_text = str(stored.get("text") or "").strip()

            if not message_text:
                return

            replay_mode = self._duplicate_replay_mode_for_account(account.id)
            peer_wait = self._duplicate_peer_wait_sec()
            if peer_wait > 0:
                peer_outcome = self._wait_for_peer_automation_outcome(
                    existing, max_wait_sec=peer_wait
                )
                trace_event(
                    stage="worker.automation.replay",
                    status="peer_wait",
                    wamid=inbound_wamid,
                    conversation_id=existing.conversation_id,
                    details={
                        "peer_outcome": peer_outcome,
                        "peer_wait_sec": peer_wait,
                        "replay_mode": replay_mode,
                    },
                )
                if peer_outcome == "substantive":
                    trace_event(
                        stage="worker.automation.replay",
                        status="skipped",
                        wamid=inbound_wamid,
                        conversation_id=existing.conversation_id,
                        details={"reason": "peer_substantive_reply"},
                    )
                    return
                if replay_mode == "only_if_no_reply" and peer_outcome in {
                    "fallback_only",
                    "substantive",
                }:
                    logger.info(
                        "[webhook] Duplicate replay skipped (%s): peer already replied conv=%s wamid=%s",
                        replay_mode,
                        existing.conversation_id,
                        inbound_wamid,
                    )
                    trace_event(
                        stage="worker.automation.replay",
                        status="skipped",
                        wamid=inbound_wamid,
                        conversation_id=existing.conversation_id,
                        details={
                            "reason": "peer_reply_exists",
                            "peer_outcome": peer_outcome,
                            "replay_mode": replay_mode,
                        },
                    )
                    return

            if self._has_db_automation_claim(inbound_wamid, role="primary"):
                if not self._inbound_needs_automation_replay(existing):
                    trace_event(
                        stage="worker.automation.replay",
                        status="skipped",
                        wamid=inbound_wamid,
                        conversation_id=existing.conversation_id,
                        details={"reason": "primary_claim_substantive_reply"},
                    )
                    return
                if replay_mode == "only_if_no_reply":
                    trace_event(
                        stage="worker.automation.replay",
                        status="skipped",
                        wamid=inbound_wamid,
                        conversation_id=existing.conversation_id,
                        details={"reason": "primary_claim_peer_fallback"},
                    )
                    return
            elif peer_outcome == "none" and self._try_acquire_db_automation_claim(
                inbound_wamid, role="primary"
            ):
                if not self._try_acquire_automation_lock(inbound_wamid):
                    trace_event(
                        stage="worker.automation.replay",
                        status="skipped",
                        wamid=inbound_wamid,
                        conversation_id=existing.conversation_id,
                        details={"reason": "automation_lock_held_primary_takeover"},
                    )
                    return
                logger.info(
                    "[webhook] Duplicate inbound — running primary automation (no peer claim): conv=%s wamid=%s",
                    conversation.id,
                    inbound_wamid,
                )
                trace_event(
                    stage="worker.automation.replay",
                    status="primary_takeover",
                    wamid=inbound_wamid,
                    conversation_id=conversation.id,
                    account_id=account.id,
                    details={"message_id": existing.id},
                )
                self._process_automation(
                    account=account,
                    conversation=conversation,
                    message_text=message_text,
                    message_id=existing.id,
                    from_phone=from_phone,
                    is_button_reply=is_button_reply,
                    button_payload=button_payload,
                    inbound_wamid=inbound_wamid,
                    automation_replay=False,
                )
                return

            delay = self._automation_replay_delay_sec()
            if delay > 0:
                time.sleep(delay)
                if not self._inbound_needs_automation_replay(existing):
                    trace_event(
                        stage="worker.automation.replay",
                        status="skipped",
                        wamid=inbound_wamid,
                        conversation_id=existing.conversation_id,
                        details={"reason": "substantive_reply_after_wait", "delay_sec": delay},
                    )
                    return

            if not self._try_acquire_db_automation_claim(inbound_wamid, role="replay"):
                trace_event(
                    stage="worker.automation.replay",
                    status="skipped",
                    wamid=inbound_wamid,
                    conversation_id=existing.conversation_id,
                    details={"reason": "db_replay_claim_held"},
                )
                return

            if not self._try_acquire_automation_lock(inbound_wamid):
                trace_event(
                    stage="worker.automation.replay",
                    status="skipped",
                    wamid=inbound_wamid,
                    conversation_id=existing.conversation_id,
                    details={"reason": "automation_lock_held"},
                )
                return

            logger.info(
                "[webhook] Replaying automation for duplicate inbound: conv=%s msg_id=%s wamid=%s",
                conversation.id,
                existing.id,
                inbound_wamid,
            )
            trace_event(
                stage="worker.automation.replay",
                status="running",
                wamid=inbound_wamid,
                conversation_id=conversation.id,
                account_id=account.id,
                details={"message_id": existing.id},
            )
            self._process_automation(
                account=account,
                conversation=conversation,
                message_text=message_text,
                message_id=existing.id,
                from_phone=from_phone,
                is_button_reply=is_button_reply,
                button_payload=button_payload,
                inbound_wamid=inbound_wamid,
                automation_replay=True,
            )
        except Exception as exc:
            logger.exception(
                "Automation replay on duplicate failed (non-fatal): wamid=%s err=%s",
                inbound_wamid,
                exc,
            )
            trace_event(
                stage="worker.automation.replay",
                status="error",
                wamid=inbound_wamid,
                conversation_id=existing.conversation_id,
                details={"error": str(exc)},
            )

    def _process_automation(
        self,
        account: WhatsAppAccount,
        conversation: WhatsAppConversation,
        message_text: str,
        message_id: int,
        from_phone: str,
        is_button_reply: bool = False,
        button_payload: Optional[str] = None,
        inbound_wamid: Optional[str] = None,
        automation_replay: bool = False,
        is_flow_reply: bool = False,
        raw_message: Optional[dict] = None,
    ):
        """
        Process automation rules for incoming message.
        
        CRITICAL: This must be fail-safe. Automation errors should NEVER
        prevent normal message processing or break the webhook handler.
        """
        try:
            from .automation_engine import check_is_first_message
            from .fast_router import route_message

            if inbound_wamid and not automation_replay:
                if not self._try_acquire_db_automation_claim(inbound_wamid, role="primary"):
                    logger.info(
                        "[webhook] Automation skipped — DB claim held for wamid=%s",
                        inbound_wamid,
                    )
                    trace_event(
                        stage="worker.automation.submitted",
                        status="skipped",
                        wamid=inbound_wamid,
                        conversation_id=conversation.id,
                        account_id=account.id,
                        details={"reason": "db_automation_claim_held"},
                    )
                    return
                if not self._try_acquire_automation_lock(inbound_wamid):
                    logger.info(
                        "[webhook] Automation skipped — another handler owns wamid=%s",
                        inbound_wamid,
                    )
                    trace_event(
                        stage="worker.automation.submitted",
                        status="skipped",
                        wamid=inbound_wamid,
                        conversation_id=conversation.id,
                        account_id=account.id,
                        details={"reason": "automation_lock_held"},
                    )
                    return

            is_first = None
            if not is_button_reply:
                is_first = check_is_first_message(conversation.id)

            if raw_message is None:
                raw_message = {"id": inbound_wamid} if inbound_wamid else None
            elif inbound_wamid and isinstance(raw_message, dict) and not raw_message.get("id"):
                raw_message = {**raw_message, "id": inbound_wamid}
            # Run routing in the webhook worker job (already async via Redis queue).
            # Background threads + a separate AI queue were dropping AI replies silently.
            logger.info(
                "[webhook] automation routing inline: conv=%s msg_id=%s wamid=%s button=%s",
                conversation.id,
                message_id,
                inbound_wamid,
                is_button_reply,
            )
            trace_event(
                stage="worker.automation.submitted",
                status="inline",
                wamid=inbound_wamid,
                conversation_id=conversation.id,
                account_id=account.id,
                details={"message_id": message_id, "button_reply": is_button_reply},
            )
            route_message(
                account_id=account.id,
                workspace_id=str(account.workspace_id or ""),
                conversation_id=conversation.id,
                message_text=message_text,
                message_id=message_id,
                from_phone=from_phone,
                msg_type="interactive" if is_flow_reply else "text",
                is_button_reply=is_button_reply,
                button_payload=button_payload,
                is_first_inbound=is_first,
                raw_message=raw_message,
                account_obj=account,
                conversation_obj=conversation,
                automation_replay=automation_replay,
                is_flow_reply=is_flow_reply,
            )
        except Exception as e:
            logger.exception(f"Automation processing error (non-fatal): {e}")
            print(f"⚠️ Automation error (non-fatal): {e}")
            trace_event(
                stage="worker.automation.error",
                status="error",
                wamid=inbound_wamid,
                conversation_id=conversation.id,
                account_id=account.id,
                details={"message": str(e), "message_id": message_id},
            )
            try:
                self.db_session.rollback()
            except Exception:
                pass

    def _process_automation_sync(
        self,
        account: WhatsAppAccount,
        conversation: WhatsAppConversation,
        message_text: str,
        message_id: int,
        from_phone: str,
        is_button_reply: bool = False,
        button_payload: Optional[str] = None,
        inbound_wamid: Optional[str] = None,
        is_first_message: Optional[bool] = None,
    ):
        """Synchronous automation fallback when the background queue is saturated."""
        try:
            from .interactive_automation_engine import process_interactive_automation
            from .automation_engine import (
                AutomationEngine,
                send_automation_response,
                check_is_first_message,
            )

            interactive_result = process_interactive_automation(
                account=account,
                conversation=conversation,
                message_text=message_text,
                from_phone=from_phone,
                is_button_reply=is_button_reply,
                button_payload=button_payload,
                inbound_wamid=inbound_wamid,
            )

            if interactive_result is not None:
                logger.info(f"Interactive automation triggered: {interactive_result}")
                print(f"🔄 Interactive automation triggered")
                if not bool((interactive_result or {}).get("allow_ai_takeover")):
                    return
                logger.info(
                    "Interactive automation yielded to AI/rules fallback (reason=%s)",
                    (interactive_result or {}).get("reason"),
                )

            # Button replies can fall through to regular keyword automations if not handled by interactive flows

            if is_first_message is None:
                is_first_message = check_is_first_message(conversation.id)

            engine = AutomationEngine(
                account_id=account.id,
                workspace_id=account.workspace_id,
            )
            result = engine.process_incoming_message(
                message_text=message_text,
                conversation_id=conversation.id,
                is_first_message=is_first_message,
                message_id=message_id,
            )

            if not result:
                return

            logger.info(f"Automation rule matched: {result.get('rule_name')}")
            print(f"🤖 Automation triggered: {result.get('rule_name')}")

            response_config = result.get("response_config", {}).copy()
            response_type = result.get("response_type")

            if response_type == "ai":
                response_config["incoming_message"] = message_text
                if inbound_wamid:
                    response_config["inbound_wamid"] = inbound_wamid
            if response_type == "faq":
                response_config["faq_id"] = result.get("faq_id")
                response_config["faq_answer"] = result.get("faq_answer")

            success, sent_msg_id, error = send_automation_response(
                account_id=account.id,
                conversation_id=conversation.id,
                response_type=response_type,
                response_config=response_config,
                to_phone=from_phone,
            )

            if success:
                logger.info("Automation response sent successfully")
                print("✅ Automation response sent")
            else:
                logger.warning(f"Automation response failed: {error}")
                print(f"⚠️ Automation response failed: {error}")
        except Exception as e:
            logger.exception(f"Sync automation processing error (non-fatal): {e}")
            try:
                self.db_session.rollback()
            except Exception:
                pass

    # ============================================================
    # Template Webhook Handlers (Multi-tenant)
    # ============================================================
    
    def _get_account_by_waba_id(self, waba_id: str) -> Optional[WhatsAppAccount]:
        """
        Find WhatsAppAccount by WABA ID for multi-tenant routing.
        
        Args:
            waba_id: WhatsApp Business Account ID from webhook entry.id
            
        Returns:
            WhatsAppAccount if found, None otherwise
        """
        if not waba_id:
            logger.warning("No WABA ID provided in webhook - cannot route to account")
            return None
        
        try:
            # Look up account by waba_id field
            account = self.db_session.query(WhatsAppAccount).filter(
                WhatsAppAccount.waba_id == waba_id
            ).first()
            
            if not account:
                logger.warning(f"No WhatsApp account found for WABA ID: {waba_id}")
                return None
            
            return account
        except Exception as e:
            logger.error(f"Error looking up account by WABA ID {waba_id}: {e}")
            return None
    
    def _process_template_status_update(self, waba_id: str, value: Dict[str, Any], raw_json: str):
        """
        Handle message_template_status_update webhook.
        Updates template status when Meta approves, rejects, or pauses a template.
        
        Webhook payload structure:
        {
            "event": "APPROVED" | "REJECTED" | "PENDING" | "PAUSED" | "DISABLED",
            "message_template_id": "123456789",
            "message_template_name": "template_name",
            "message_template_language": "en_US",
            "reason": "Optional rejection reason"
        }
        
        Args:
            waba_id: WABA ID for multi-tenant routing
            value: The value object from the webhook change
            raw_json: Raw JSON for logging
        """
        try:
            # Extract template info from webhook
            event = value.get("event", "").upper()
            template_id = value.get("message_template_id")
            template_name = value.get("message_template_name")
            template_language = value.get("message_template_language")
            reason = value.get("reason")
            
            # CRITICAL: Convert template_id to string for database comparison
            # Meta sends it as an integer, but our DB column is VARCHAR
            if template_id is not None:
                template_id = str(template_id)
            
            logger.info(f"📋 Template status update: {template_name} ({template_language}) -> {event}")
            print(f"📋 Template status update webhook: {template_name} -> {event} (WABA: {waba_id})")
            
            # Find the account by WABA ID (multi-tenant routing)
            account = self._get_account_by_waba_id(waba_id)
            if not account:
                self._log_webhook(raw_json, "template_status_update", None, 
                    f"Account not found for WABA ID: {waba_id}")
                return
            
            # Map Meta status events to our status values
            status_map = {
                "APPROVED": "APPROVED",
                "REJECTED": "REJECTED",
                "PENDING": "PENDING",
                "PAUSED": "PAUSED",
                "DISABLED": "DISABLED",
                "IN_APPEAL": "IN_APPEAL",
                "PENDING_DELETION": "PENDING_DELETION",
                "DELETED": "DELETED",
                "LIMIT_EXCEEDED": "LIMIT_EXCEEDED",
            }
            new_status = status_map.get(event, event)
            
            # Find the template by meta_template_id OR by name+language+account
            template = None
            
            if template_id:
                template = self.db_session.query(WhatsAppTemplate).filter(
                    WhatsAppTemplate.account_id == account.id,
                    WhatsAppTemplate.meta_template_id == template_id
                ).first()
            
            if not template and template_name and template_language:
                template = self.db_session.query(WhatsAppTemplate).filter(
                    WhatsAppTemplate.account_id == account.id,
                    WhatsAppTemplate.name == template_name,
                    WhatsAppTemplate.language == template_language
                ).first()
            
            if not template:
                logger.warning(f"Template not found: {template_name} ({template_language}) for account {account.id}")
                self._log_webhook(raw_json, "template_status_update", account.phone_number_id,
                    f"Template not found: {template_name}")
                return
            
            # Update template status
            old_status = template.status
            template.status = new_status
            template.last_synced_at = datetime.now(timezone.utc)
            
            if reason:
                template.rejection_reason = reason
            elif new_status == "APPROVED":
                template.rejection_reason = None  # Clear rejection reason on approval
            
            # Update meta_template_id if we didn't have it
            if template_id and not template.meta_template_id:
                template.meta_template_id = template_id
            
            self.db_session.commit()
            
            logger.info(f"✅ Template {template_name} status updated: {old_status} -> {new_status}")
            print(f"✅ Template {template_name} status updated: {old_status} -> {new_status}")

            if old_status != new_status and new_status in ("APPROVED", "REJECTED", "PAUSED", "DISABLED"):
                try:
                    from .background_processor import bg_processor
                    from .human_escalation import send_template_status_notification_email_bg
                    
                    bg_processor.submit(
                        send_template_status_notification_email_bg,
                        account_id=account.id,
                        template_id=template.id,
                        old_status=old_status,
                        new_status=new_status,
                        reason=reason,
                    )
                    logger.info(f"Scheduled template status update email notification: {template_name} ({old_status} -> {new_status})")
                except Exception as email_err:
                    logger.exception(f"Failed to submit background task to send template status update email: {email_err}")
            
            # Log successful webhook processing
            self._log_webhook(raw_json, "template_status_update", account.phone_number_id)
            
            # Broadcast real-time notification
            notification_manager.broadcast("template_update", {
                "template_id": template.id,
                "name": template.name,
                "status": new_status,
                "language": template.language,
                "reason": reason
            })
            
        except Exception as e:
            logger.exception(f"Error processing template status update: {e}")
            self._log_webhook(raw_json, "template_status_update", None, str(e))
    
    def _process_template_quality_update(self, waba_id: str, value: Dict[str, Any], raw_json: str):
        """
        Handle message_template_quality_update webhook.
        Updates template quality score when Meta changes it.
        
        Webhook payload structure:
        {
            "message_template_id": "123456789",
            "message_template_name": "template_name",
            "message_template_language": "en_US",
            "previous_quality_score": "GREEN",
            "new_quality_score": "YELLOW"
        }
        
        Args:
            waba_id: WABA ID for multi-tenant routing
            value: The value object from the webhook change
            raw_json: Raw JSON for logging
        """
        try:
            # Extract template info from webhook
            template_id = value.get("message_template_id")
            template_name = value.get("message_template_name")
            template_language = value.get("message_template_language")
            previous_quality = value.get("previous_quality_score")
            new_quality = value.get("new_quality_score")
            
            # CRITICAL: Convert template_id to string for database comparison
            if template_id is not None:
                template_id = str(template_id)
            
            logger.info(f"📊 Template quality update: {template_name} ({template_language}) {previous_quality} -> {new_quality}")
            print(f"📊 Template quality update webhook: {template_name} {previous_quality} -> {new_quality} (WABA: {waba_id})")
            
            # Find the account by WABA ID (multi-tenant routing)
            account = self._get_account_by_waba_id(waba_id)
            if not account:
                self._log_webhook(raw_json, "template_quality_update", None,
                    f"Account not found for WABA ID: {waba_id}")
                return
            
            # Find the template
            template = None
            
            if template_id:
                template = self.db_session.query(WhatsAppTemplate).filter(
                    WhatsAppTemplate.account_id == account.id,
                    WhatsAppTemplate.meta_template_id == template_id
                ).first()
            
            if not template and template_name and template_language:
                template = self.db_session.query(WhatsAppTemplate).filter(
                    WhatsAppTemplate.account_id == account.id,
                    WhatsAppTemplate.name == template_name,
                    WhatsAppTemplate.language == template_language
                ).first()
            
            if not template:
                logger.warning(f"Template not found for quality update: {template_name} ({template_language})")
                self._log_webhook(raw_json, "template_quality_update", account.phone_number_id,
                    f"Template not found: {template_name}")
                return
            
            # Update quality score
            old_quality = template.quality_score
            template.quality_score = new_quality
            template.last_synced_at = datetime.now(timezone.utc)
            
            self.db_session.commit()
            
            logger.info(f"✅ Template {template_name} quality updated: {old_quality} -> {new_quality}")
            print(f"✅ Template {template_name} quality updated: {old_quality} -> {new_quality}")
            
            # Log successful webhook processing
            self._log_webhook(raw_json, "template_quality_update", account.phone_number_id)
            
            # Broadcast real-time notification for quality change
            notification_manager.broadcast("template_update", {
                "template_id": template.id,
                "name": template.name,
                "status": f"QUALITY_{new_quality}",
                "language": template.language,
                "reason": f"Quality changed from {previous_quality} to {new_quality}",
                "update_type": "quality",
                "previous_quality": previous_quality,
                "new_quality": new_quality,
            })
            
        except Exception as e:
            logger.exception(f"Error processing template quality update: {e}")
            self._log_webhook(raw_json, "template_quality_update", None, str(e))
    
    def _process_template_category_update(self, waba_id: str, value: Dict[str, Any], raw_json: str):
        """
        Handle template_category_update webhook.
        Updates template category when Meta auto-categorizes a template.
        
        Webhook payload structure:
        {
            "message_template_id": "123456789",
            "message_template_name": "template_name",
            "message_template_language": "en_US",
            "previous_category": "UTILITY",
            "new_category": "MARKETING"
        }
        
        Args:
            waba_id: WABA ID for multi-tenant routing
            value: The value object from the webhook change
            raw_json: Raw JSON for logging
        """
        try:
            # Extract template info from webhook
            template_id = value.get("message_template_id")
            template_name = value.get("message_template_name")
            template_language = value.get("message_template_language")
            previous_category = value.get("previous_category")
            new_category = value.get("new_category")
            
            # CRITICAL: Convert template_id to string for database comparison
            if template_id is not None:
                template_id = str(template_id)
            
            logger.info(f"📦 Template category update: {template_name} ({template_language}) {previous_category} -> {new_category}")
            print(f"📦 Template category update webhook: {template_name} {previous_category} -> {new_category} (WABA: {waba_id})")
            
            # Find the account by WABA ID (multi-tenant routing)
            account = self._get_account_by_waba_id(waba_id)
            if not account:
                self._log_webhook(raw_json, "template_category_update", None,
                    f"Account not found for WABA ID: {waba_id}")
                return
            
            # Find the template
            template = None
            
            if template_id:
                template = self.db_session.query(WhatsAppTemplate).filter(
                    WhatsAppTemplate.account_id == account.id,
                    WhatsAppTemplate.meta_template_id == template_id
                ).first()
            
            if not template and template_name and template_language:
                template = self.db_session.query(WhatsAppTemplate).filter(
                    WhatsAppTemplate.account_id == account.id,
                    WhatsAppTemplate.name == template_name,
                    WhatsAppTemplate.language == template_language
                ).first()
            
            if not template:
                logger.warning(f"Template not found for category update: {template_name} ({template_language})")
                self._log_webhook(raw_json, "template_category_update", account.phone_number_id,
                    f"Template not found: {template_name}")
                return
            
            # Update category
            old_category = template.category
            template.category = new_category
            template.last_synced_at = datetime.now(timezone.utc)
            
            self.db_session.commit()
            
            logger.info(f"✅ Template {template_name} category updated: {old_category} -> {new_category}")
            print(f"✅ Template {template_name} category updated: {old_category} -> {new_category}")
            
            # Log successful webhook processing
            self._log_webhook(raw_json, "template_category_update", account.phone_number_id)
            
            # Broadcast real-time notification for category change
            notification_manager.broadcast("template_update", {
                "template_id": template.id,
                "name": template.name,
                "status": f"CATEGORY_{new_category}",
                "language": template.language,
                "reason": f"Category changed from {previous_category} to {new_category}",
                "update_type": "category",
                "previous_category": previous_category,
                "new_category": new_category,
            })
            
        except Exception as e:
            logger.exception(f"Error processing template category update: {e}")
            self._log_webhook(raw_json, "template_category_update", None, str(e))
    
    # ============================================================
    # Coexistence: Echo Message Handler
    # ============================================================
    
    def _process_echo(self, echo_msg: Dict[str, Any], phone_number_id: str):
        """
        Process a message echo from coexistence mode.
        
        Echoes are messages sent from the WhatsApp mobile app by the business.
        We store them as direction='echo' to show in the inbox alongside
        agent-sent and customer messages.
        
        This also updates device_activity tracking (last_echo_at).
        
        Args:
            echo_msg: Echo message object from webhook
            phone_number_id: Our phone number ID
        """
        from .utils import normalize_phone, parse_whatsapp_timestamp, get_message_type
        
        wamid = echo_msg.get("id")
        to_phone = echo_msg.get("to")
        msg_type = get_message_type(echo_msg)
        timestamp = echo_msg.get("timestamp")
        
        logger.info(f"📱 Echo message received: wamid={wamid}, to={to_phone}, type={msg_type}")
        
        if not wamid or not to_phone:
            logger.warning("Echo message missing wamid or to")
            return
        
        # Deduplicate by wamid
        existing = WhatsAppMessage.query.filter_by(wamid=wamid).first()
        if existing:
            logger.debug(f"Duplicate echo skipped: {wamid}")
            return
        
        # Get account
        account = self._get_or_create_account(phone_number_id)
        if not account:
            return
        
        # Update device activity tracking
        account.last_echo_at = datetime.now(timezone.utc)
        
        # Normalize to_phone
        to_phone = normalize_phone(to_phone)
        
        # Get or create conversation with the recipient
        conversation = self._get_or_create_conversation(account.id, to_phone)
        
        # Build content
        content = self._extract_content(echo_msg, msg_type)
        content["echo"] = True  # Mark as echo message
        
        # Parse timestamp
        msg_timestamp = parse_whatsapp_timestamp(timestamp) or datetime.now(timezone.utc)
        
        # Store echo message with direction='echo'
        msg_record = WhatsAppMessage(
            conversation_id=conversation.id,
            direction="echo",  # Key difference: messages from mobile app
            type=msg_type,
            content=content,
            wamid=wamid,
            status="sent",
            created_at=msg_timestamp,
            sent_at=msg_timestamp,
        )
        self.db_session.add(msg_record)
        
        # Update conversation timestamps
        conversation.last_message_at = msg_timestamp
        conversation.last_outbound_at = msg_timestamp
        
        update_account_heartbeat(account, db_session=self.db_session, inbound=True)
        
        self.db_session.commit()
        logger.info(f"✅ Stored echo message: id={msg_record.id}, type={msg_type}")
        
        # Broadcast real-time event
        try:
            from notifications import notification_manager
            # Standardize on 'whatsapp_message_received' so frontend only needs one handler
            # for both incoming and echoes (sent from mobile)
            notification_manager.broadcast("whatsapp_message_received", {
                "message": msg_record.to_dict(),
                "conversation_id": conversation.id,
                "account_id": account.id,
                "workspace_id": account.workspace_id,
                "source": "mobile_app",
                "direction": "echo"
            })
        except Exception as e:
            logger.error(f"Failed to broadcast echo event: {e}")
    
    # ============================================================
    # Coexistence: History Sync Handler
    # ============================================================
    
    def _process_history_sync(self, value: Dict[str, Any], phone_number_id: str):
        """
        Process history sync webhook from coexistence mode.
        
        Meta sends history in phases (0-2) with threads containing messages.
        Payload structure:
        {
          "history": [{
            "metadata": {"phase": 0, "chunk_order": 1, "progress": 55},
            "threads": [{
              "id": "<user_phone>",
              "messages": [{ ... }]
            }]
          }]
        }
        
        Or if history sharing was declined:
        { "history": [{ "errors": [{ "code": 2593109, ... }] }] }
        """
        from .utils import normalize_phone, parse_whatsapp_timestamp, get_message_type
        
        logger.info(f"📚 History sync received for {phone_number_id}")
        
        account = self._get_or_create_account(phone_number_id)
        if not account:
            return
        
        # Update sync status
        if account.sync_status != "synced":
            account.sync_status = "syncing"
        
        # Meta sends data in "history" array, each with "threads"
        # Also support legacy "conversations" format
        history_entries = value.get("history", [])
        conversations_data = value.get("conversations", [])
        
        synced_count: int = 0
        skipped_count: int = 0
        progress: int = 0
        
        for entry in history_entries:
            # Check for errors (e.g., user declined history sharing)
            if "errors" in entry:
                for err in entry["errors"]:
                    logger.warning(f"History sync error: code={err.get('code')} - {err.get('message')}")
                    if err.get("code") == 2593109:
                        logger.info("Business declined to share chat history")
                        account.sync_status = "synced"
                        account.history_sync_completed = True
                        self.db_session.commit()
                        return
                continue
            
            metadata = entry.get("metadata", {})
            phase = metadata.get("phase", 0)
            chunk_order = metadata.get("chunk_order", 0)
            progress = metadata.get("progress", 0)
            logger.info(f"📚 History phase={phase}, chunk={chunk_order}, progress={progress}%")
            
            threads = entry.get("threads", [])
            for thread in threads:
                contact_phone = thread.get("id")
                if not contact_phone:
                    continue
                
                contact_phone = normalize_phone(contact_phone)
                
                conversation = self._get_or_create_conversation(
                    account.id, contact_phone, None
                )
                
                messages = thread.get("messages", [])
                for msg in messages:
                    wamid = msg.get("id")
                    if not wamid:
                        continue
                    
                    existing = WhatsAppMessage.query.filter_by(wamid=wamid).first()
                    if existing:
                        skipped_count += 1
                        continue
                    
                    msg_type = get_message_type(msg)
                    timestamp = msg.get("timestamp")
                    msg_timestamp = parse_whatsapp_timestamp(timestamp) or datetime.now(timezone.utc)
                    
                    from_phone = msg.get("from")
                    to_phone = msg.get("to")
                    is_from_business = (from_phone and from_phone != contact_phone)
                    direction = "echo" if is_from_business else "incoming"
                    
                    content = self._extract_content(msg, msg_type)
                    content["history_sync"] = True
                    content["phase"] = phase
                    
                    msg_record = WhatsAppMessage(
                        conversation_id=conversation.id,
                        direction=direction,
                        type=msg_type,
                        content=content,
                        wamid=wamid,
                        status=msg.get("status", "sent" if direction == "echo" else "received"),
                        created_at=msg_timestamp,
                    )
                    self.db_session.add(msg_record)
                    synced_count += 1
                    
                    if not conversation.last_message_at or msg_timestamp > conversation.last_message_at:
                        conversation.last_message_at = msg_timestamp
                
                try:
                    self.db_session.commit()
                except Exception as e:
                    self.db_session.rollback()
                    logger.error(f"Failed to commit history sync for {contact_phone}: {e}")
        
        # Also handle legacy "conversations" format
        for conv_data in conversations_data:
            contact_phone = conv_data.get("id") or conv_data.get("phone")
            if not contact_phone:
                continue
            contact_phone = normalize_phone(contact_phone)
            contact_name = conv_data.get("name")
            conversation = self._get_or_create_conversation(account.id, contact_phone, contact_name)
            messages = conv_data.get("messages", [])
            for msg in messages:
                wamid = msg.get("id")
                if not wamid:
                    continue
                existing = WhatsAppMessage.query.filter_by(wamid=wamid).first()
                if existing:
                    skipped_count += 1
                    continue
                msg_type = get_message_type(msg)
                timestamp = msg.get("timestamp")
                msg_timestamp = parse_whatsapp_timestamp(timestamp) or datetime.now(timezone.utc)
                from_phone = msg.get("from")
                is_from_business = from_phone == phone_number_id if from_phone else False
                direction = "echo" if is_from_business else "incoming"
                content = self._extract_content(msg, msg_type)
                content["history_sync"] = True
                msg_record = WhatsAppMessage(
                    conversation_id=conversation.id, direction=direction, type=msg_type,
                    content=content, wamid=wamid,
                    status="sent" if direction == "echo" else "received",
                    created_at=msg_timestamp,
                )
                self.db_session.add(msg_record)
                synced_count += 1
                if not conversation.last_message_at or msg_timestamp > conversation.last_message_at:
                    conversation.last_message_at = msg_timestamp
            try:
                self.db_session.commit()
            except Exception as e:
                self.db_session.rollback()
                logger.error(f"Failed to commit history sync for {contact_phone}: {e}")
        
        # Final status update
        try:
            if progress >= 100:
                account.sync_status = "synced"
                account.history_sync_completed = True
            
            self.db_session.commit()
            logger.info(f"📚 History sync: {synced_count} new, {skipped_count} duplicates skipped, progress={progress}%")
        except Exception as e:
            self.db_session.rollback()
            logger.error(f"History sync final commit error: {e}")

    def _process_smb_app_state_sync(self, value: Dict[str, Any], phone_number_id: Optional[str]):
        """Process smb_app_state_sync — update conversation contact names from WA Business app."""
        from .utils import normalize_phone

        if not phone_number_id:
            return

        account = self._get_or_create_account(phone_number_id)
        if not account:
            return

        state_sync = value.get("state_sync") or value.get("smb_app_state_sync") or []
        updated = 0
        for row in state_sync:
            if (row.get("type") or "").lower() != "contact":
                continue
            contact = row.get("contact") or {}
            phone = contact.get("phone_number")
            if not phone:
                continue
            action = (row.get("action") or "add").lower()
            norm_phone = normalize_phone(str(phone))
            conversation = self._get_or_create_conversation(account.id, norm_phone, None)
            if action == "remove":
                continue
            full_name = contact.get("full_name") or contact.get("first_name")
            if full_name and conversation.customer_name != full_name:
                conversation.customer_name = str(full_name)[:255]
                updated += 1

        if updated:
            try:
                self.db_session.commit()
                logger.info("Contacts sync: updated %s conversations for %s", updated, phone_number_id)
            except Exception as e:
                self.db_session.rollback()
                logger.warning("Contacts sync commit failed: %s", e)
    
    def _log_webhook(
        self,
        raw_json: str,
        event_type: str,
        phone_number_id: Optional[str],
        error: Optional[str] = None,
    ):
        """Log webhook to database."""
        try:
            log = WhatsAppWebhookLog(
                raw_json=raw_json,
                event_type=event_type,
                phone_number_id=phone_number_id,
                processed=error is None,
                error_message=error,
                processed_at=datetime.now(timezone.utc) if error is None else None,
            )
            self.db_session.add(log)
            self.db_session.commit()
        except Exception as e:
            logger.error(f"Failed to log webhook: {e}")


# ============================================================
# WhatsApp Sender (for routes.py compatibility)
# ============================================================

class WhatsAppSender:
    """
    Simple sender class for backward compatibility.
    Wraps WhatsAppService for basic sending operations.
    """
    
    def __init__(self, phone_number_id: str, access_token: str):
        self.phone_number_id = phone_number_id
        self.access_token = access_token
        self._service = None
    
    @property
    def service(self):
        if not self._service:
            from .services import WhatsAppService
            self._service = WhatsAppService(
                phone_number_id=self.phone_number_id,
                access_token=self.access_token,
            )
        return self._service
    
    def send_text(self, to: str, text: str, **kwargs) -> Dict[str, Any]:
        return self.service.send_text(to, text, **kwargs)
    
    def send_template(self, to: str, template_name: str, **kwargs) -> Dict[str, Any]:
        return self.service.send_template(to, template_name, **kwargs)
    
    def send_image(self, to: str, **kwargs) -> Dict[str, Any]:
        return self.service.send_image(to, **kwargs)
    
    def send_video(self, to: str, **kwargs) -> Dict[str, Any]:
        return self.service.send_video(to, **kwargs)
    
    def send_audio(self, to: str, **kwargs) -> Dict[str, Any]:
        return self.service.send_audio(to, **kwargs)
    
    def send_document(self, to: str, **kwargs) -> Dict[str, Any]:
        return self.service.send_document(to, **kwargs)
