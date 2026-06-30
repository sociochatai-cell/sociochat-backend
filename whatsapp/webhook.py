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
from datetime import datetime, timezone
from typing import Optional, Dict, Any, Tuple, List

from notifications import notification_manager
from models import db
from .models import (
    WhatsAppAccount,
    WhatsAppConversation,
    WhatsAppMessage,
    WhatsAppWebhookLog,
    MessageStatusEvent,
    WhatsAppTemplate,
)
from .utils import (
    verify_signature,
    normalize_phone,
    parse_whatsapp_timestamp,
    extract_message_text,
    get_message_type,
    extract_media_info,
    is_duplicate_message,
)
from SocioviaCrm.capi_service import send_capi_event

logger = logging.getLogger(__name__)


def _normalize_verify_token(value: Optional[str]) -> str:
    """Strip whitespace and optional surrounding quotes from Meta verify token."""
    if not value:
        return ""
    cleaned = value.strip()
    if len(cleaned) >= 2 and cleaned[0] == cleaned[-1] and cleaned[0] in ('"', "'"):
        cleaned = cleaned[1:-1].strip()
    return cleaned


# ============================================================
# Webhook Verification
# ============================================================

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


def verify_webhook_challenge(mode: str, token: str, challenge: str) -> Optional[str]:
    """
    Verify webhook subscription challenge from Meta.
    
    Args:
        mode: hub.mode parameter
        token: hub.verify_token parameter
        challenge: hub.challenge parameter
        
    Returns:
        Challenge string if valid, None otherwise
    """
    verify_token = _normalize_verify_token(os.getenv("WHATSAPP_VERIFY_TOKEN", ""))
    token = _normalize_verify_token(token)
    
    if not verify_token:
        logger.error("WHATSAPP_VERIFY_TOKEN not configured!")
        return None
    
    if mode == "subscribe" and token == verify_token:
        logger.info("Webhook verification successful")
        return challenge
    
    logger.warning(f"Webhook verification failed: mode={mode}, token_match={token == verify_token}")
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
                logger.info(f"Contacts sync webhook received for {phone_number_id}")
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
        
        # DEBUG: Print message processing info
        print(f"📩 Processing message: wamid={wamid}, from={from_phone}, type={msg_type}")
        print(f"   phone_number_id: {phone_number_id}")
        
        if not wamid or not from_phone:
            print("⚠️ Message missing wamid or from")
            logger.warning("Message missing wamid or from")
            return
        
        # Deduplicate by wamid
        existing = WhatsAppMessage.query.filter_by(wamid=wamid).first()
        if existing:
            logger.debug(f"Duplicate message skipped: {wamid}")
            return
        
        # In-memory dedup check
        if is_duplicate_message(wamid):
            logger.debug(f"Duplicate message (cache): {wamid}")
            return
        
        # Normalize phone
        from_phone = normalize_phone(from_phone)
        
        # Get account - returns None if not found or inactive
        account = self._get_or_create_account(phone_number_id)
        
        if not account:
            print(f"⚠️ Skipping message - no active account for phone_number_id: {phone_number_id}")
            return
        
        # Get or create conversation
        contact_name = contact.get("profile", {}).get("name") if contact else None
        conversation = self._get_or_create_conversation(account.id, from_phone, contact_name)
        
        # Build message content
        content = self._extract_content(message, msg_type)
        
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
        
        # Update conversation with session tracking
        conversation.last_message_at = msg_timestamp
        conversation.last_inbound_at = msg_timestamp
        conversation.session_expires_at = msg_timestamp + timedelta(hours=24)  # 24h window
        conversation.status = "open"  # Open on new message
        conversation.unread_count = (conversation.unread_count or 0) + 1
        
        # Process CTWA attribution if this is from an ad
        self._process_attribution(message, conversation, account=account)
        
        try:
            self.db_session.commit()
        except Exception as commit_err:
            logger.exception(f"Failed to commit incoming message: {commit_err}")
            self.db_session.rollback()
            return
        print(f"✅ Stored message: id={msg_record.id}, type={msg_type}, content={content}")
        logger.info(f"Stored incoming message: {wamid} from {from_phone}")

        if msg_type == "interactive" and content.get("interactive_type") == "nfm_reply":
            try:
                from .flow_os_routes import maybe_create_booking_from_submission
                response_json = content.get("response_json")
                if isinstance(response_json, str):
                    import json as _json
                    try:
                        response_json = _json.loads(response_json)
                    except _json.JSONDecodeError:
                        response_json = {}
                if isinstance(response_json, dict):
                    maybe_create_booking_from_submission(
                        account_id=account.id,
                        conversation_id=conversation.id,
                        wa_id=from_phone,
                        response_json=response_json,
                        flow_id=content.get("flow_id"),
                    )
                    self.db_session.commit()
            except Exception as booking_err:
                logger.warning("Could not create booking from flow submission: %s", booking_err)
                self.db_session.rollback()
            # CRM: auto-advance lead status -> qualified (completed a flow).
            # Forward-only/idempotent; isolated so CRM logic never breaks msg processing.
            try:
                from SocioviaCrm.lead_ingest import advance_lead_status_from_conversation
                advance_lead_status_from_conversation(
                    conversation, account, "qualified",
                    reason="Completed WhatsApp flow", db_session=self.db_session
                )
            except Exception as e:
                logger.warning(f"Failed to advance CRM lead status to qualified (flow): {e}")

        # Broadcast real-time event
        try:
            notification_manager.broadcast("whatsapp_message_received", {
                "message": msg_record.to_dict(),
                "conversation_id": conversation.id,
                "account_id": account.id,
                "workspace_id": account.workspace_id,
                "user_phone": conversation.user_phone,
                "user_name": conversation.user_name,
            })
        except Exception as e:
            logger.error(f"Failed to broadcast message received event: {e}")

        # Auto-capture: mirror this conversation into the CRM as a Lead.
        # Lazy import to avoid circular imports (SocioviaCrm <-> whatsapp).
        # New conversations create a Lead; existing ones just refresh
        # last_interaction_at (dedupe-safe). Never break message processing.
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
                    logger.warning(f"Failed to advance CRM lead status to contacted: {e}")
        except Exception as e:
            logger.error(f"Failed to auto-capture CRM lead from conversation: {e}")

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
                    button_payload=None
                )
            # Check for lead keywords in text messages
            self._check_for_lead_keywords(account, conversation, text_content)
            # CRM Phase 2: buying-intent keyword rule -> advance lead to rule's status
            # (per-workspace configurable). Forward-only/idempotent; isolated so CRM
            # logic never breaks message processing. Runs AFTER the reply->contacted
            # hook so cheap signals win first.
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
                logger.warning(f"interest-keyword qualify hook failed: {e}")
            # CRM Phase 3: AI fallback classifier. ONLY runs when the keyword rule
            # did NOT match (rule is None). classify_lead_status returns None when AI
            # is disabled / chit-chat / error, so this is cheap when off. Fail-safe.
            if rule is None:
                try:
                    from SocioviaCrm.ai_status_classifier import classify_lead_status
                    _st = classify_lead_status(text_content, account.workspace_id)
                    if _st:
                        advance_lead_status_from_conversation(
                            conversation, account, _st,
                            reason=f"AI classified -> {_st}",
                            db_session=self.db_session)
                except Exception as e:
                    logger.warning(f"AI status classify hook failed: {e}")
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
            
            if button_payload:
                self._process_automation(
                    account=account,
                    conversation=conversation,
                    message_text=button_title,
                    message_id=msg_record.id,
                    from_phone=from_phone,
                    is_button_reply=True,
                    button_payload=button_payload
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
        if status_value == "failed":
            errors = status.get("errors", [])
            if errors:
                error = errors[0]
                error_code = str(error.get("code", ""))
                error_message = error.get("title", error.get("message", "Unknown error"))
        
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
                    "workspace_id": workspace_id
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
                nfm = interactive.get("nfm_reply", {})
                content["response_json"] = nfm.get("response_json")
                content["flow_id"] = nfm.get("flow_id") or interactive.get("flow_id")
                content["submission_status"] = "received"
        
        elif msg_type == "button":
            content["button_text"] = message.get("button", {}).get("text", "")
            content["button_payload"] = message.get("button", {}).get("payload", "")
        
        elif msg_type == "reaction":
            reaction = message.get("reaction", {})
            content["emoji"] = reaction.get("emoji")
            content["message_id"] = reaction.get("message_id")
        
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
            conversation._is_new = True  # transient flag for downstream lead capture
            logger.info(f"Created conversation with: {canonical}")
        elif user_name and conversation.user_name != user_name:
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
            user_data = {
                "ph": conversation.user_phone,
            }
            if conversation.user_name:
                name_parts = conversation.user_name.split(" ", 1)
                user_data["fn"] = name_parts[0]
                if len(name_parts) > 1:
                    user_data["ln"] = name_parts[1]
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
            # CRM: auto-advance lead status -> qualified (matched interest keyword).
            # Forward-only/idempotent; isolated so CRM logic never breaks msg processing.
            try:
                from SocioviaCrm.lead_ingest import advance_lead_status_from_conversation
                advance_lead_status_from_conversation(
                    conversation, account, "qualified",
                    reason="Matched interest keyword", db_session=self.db_session
                )
            except Exception as e:
                logger.warning(f"Failed to advance CRM lead status to qualified (keyword): {e}")
            logger.info(f"Keyword lead detected in conv {conversation.id}, ad_id={ad_id}")
    
    def _process_automation(
        self,
        account: WhatsAppAccount,
        conversation: WhatsAppConversation,
        message_text: str,
        message_id: int,
        from_phone: str,
        is_button_reply: bool = False,
        button_payload: Optional[str] = None,
    ):
        """
        Process automation rules for incoming message.
        
        CRITICAL: This must be fail-safe. Automation errors should NEVER
        prevent normal message processing or break the webhook handler.
        
        Args:
            account: WhatsApp account
            conversation: Conversation object
            message_text: Text content of the message
            message_id: ID of the stored message
            from_phone: Sender phone number
            is_button_reply: True if this is a button reply
            button_payload: Button ID/payload if button reply
        """
        try:
            # --- INTERACTIVE AUTOMATIONS (Visual Flow Builder) ---
            # Process these FIRST as they track conversation state
            from .interactive_automation_engine import process_interactive_automation
            
            interactive_result = process_interactive_automation(
                account=account,
                conversation=conversation,
                message_text=message_text,
                from_phone=from_phone,
                is_button_reply=is_button_reply,
                button_payload=button_payload,
            )
            
            if interactive_result:
                logger.info(f"Interactive automation triggered: {interactive_result}")
                print(f"🔄 Interactive automation triggered")
                return  # Don't continue to regular automation if interactive handled it
            
            # --- REGULAR AUTOMATIONS (Rule-based: welcome, keyword, etc.) ---
            # Only process text messages (not button replies) for regular automation
            if is_button_reply:
                return  # Button replies are only for interactive automations
            
            from .automation_engine import AutomationEngine, send_automation_response, check_is_first_message
            
            # Check if this is the first message in the conversation
            is_first_message = check_is_first_message(conversation.id)
            
            # Initialize automation engine
            engine = AutomationEngine(
                account_id=account.id,
                workspace_id=account.workspace_id
            )
            
            # Process message against automation rules
            result = engine.process_incoming_message(
                message_text=message_text,
                conversation_id=conversation.id,
                is_first_message=is_first_message,
                message_id=message_id
            )
            
            if result:
                logger.info(f"Automation rule matched: {result.get('rule_name')}")
                print(f"🤖 Automation triggered: {result.get('rule_name')}")
                
                # Get response config and inject the incoming message for AI responses
                response_config = result.get("response_config", {}).copy()
                response_type = result.get("response_type")
                
                # For AI responses, inject the incoming message text
                if response_type == "ai":
                    response_config["incoming_message"] = message_text
                
                # For FAQ responses, inject faq_id and faq_answer from match result
                if response_type == "faq":
                    response_config["faq_id"] = result.get("faq_id")
                    response_config["faq_answer"] = result.get("faq_answer")
                
                # Send automated response
                success, sent_msg_id, error = send_automation_response(
                    account_id=account.id,
                    conversation_id=conversation.id,
                    response_type=response_type,
                    response_config=response_config,
                    to_phone=from_phone
                )
                
                if success:
                    logger.info(f"Automation response sent successfully")
                    print(f"✅ Automation response sent")
                else:
                    logger.warning(f"Automation response failed: {error}")
                    print(f"⚠️ Automation response failed: {error}")
            
        except Exception as e:
            # CRITICAL: Never let automation errors break message processing
            logger.exception(f"Automation processing error (non-fatal): {e}")
            print(f"⚠️ Automation error (non-fatal): {e}")
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
