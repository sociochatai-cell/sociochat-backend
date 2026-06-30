"""
WhatsApp Services - Phase 1
===========================

Core WhatsApp Cloud API messaging functions.
All messaging operations go through this service layer.

Functions:
    - send_text_message(to, text)
    - send_template_message(to, template_name, params)
    - send_media_message(to, type, media_url)
    - send_interactive_message(to, buttons_or_list)

All functions:
    ✔ Use WhatsApp Cloud API
    ✔ Get token from environment (WHATSAPP_ACCESS_TOKEN or WHATSAPP_TEMP_TOKEN)
    ✔ Use phone_number_id from environment
    ✔ Return full API response
    ✔ Store outgoing message in DB
    ✔ Create conversation automatically if not exists
"""

import os
import logging
import json
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List, Tuple

import requests

from models import db
from .models import (
    WhatsAppAccount,
    WhatsAppConversation,
    WhatsAppMessage,
    WhatsAppTemplate,
)
from .utils import normalize_phone as _normalize_phone_util, subscribe_waba_to_app
from notifications import notification_manager

logger = logging.getLogger(__name__)

# WhatsApp Cloud API base URL
WHATSAPP_API_BASE = "https://graph.facebook.com"


class WhatsAppService:
    """
    WhatsApp Cloud API messaging service.
    
    Handles all outgoing messages and database persistence.
    """
    
    def __init__(
        self,
        db_session=None,
        phone_number_id: Optional[str] = None,
        access_token: Optional[str] = None,
        waba_id: Optional[str] = None,
        workspace_id: Optional[str] = None,
    ):
        """
        Initialize WhatsApp service.
        
        Args:
            db_session: SQLAlchemy session (optional, uses default if not provided)
            phone_number_id: WhatsApp phone number ID (falls back to stored account or env var)
            access_token: Access token (falls back to stored account or env var)
            waba_id: WhatsApp Business Account ID (falls back to stored account or env var)
            workspace_id: Workspace ID to look up stored account (Phase-2)
        """
        self.db_session = db_session or db.session
        from tenant.integration import get_tenant_meta_config
        self.api_version = (
            get_tenant_meta_config(workspace_id=workspace_id).whatsapp_api_version
            or "v22.0"
        )
        
        # Phase-2: Try to get stored account for workspace first
        # BUT: if phone_number_id is explicitly provided, honour it
        stored_account = None
        if workspace_id and not phone_number_id:
            stored_account = self._get_workspace_account(workspace_id)
        elif workspace_id and phone_number_id:
            # Look up the specific account by phone_number_id
            from .models import WhatsAppAccount as _WA
            stored_account = _WA.query.filter_by(
                phone_number_id=phone_number_id,
                workspace_id=workspace_id,
                is_active=True,
            ).first()
            if not stored_account:
                # Fallback: any active account in the workspace
                stored_account = self._get_workspace_account(workspace_id)
        
        if stored_account:
            # Use stored account credentials
            self.access_token = stored_account.get_access_token()
            self.phone_number_id = stored_account.phone_number_id
            self.waba_id = stored_account.waba_id
        else:
            # Fall back to provided params or env vars
            self.phone_number_id = phone_number_id or os.getenv("WHATSAPP_PHONE_NUMBER_ID", "")
            self.access_token = access_token or os.getenv("WHATSAPP_ACCESS_TOKEN") or os.getenv("WHATSAPP_TEMP_TOKEN", "")
            self.waba_id = waba_id or os.getenv("WHATSAPP_WABA_ID", "")
    
    def _get_workspace_account(self, workspace_id: str) -> Optional["WhatsAppAccount"]:
        """Get active WhatsApp account for workspace."""
        from .models import WhatsAppAccount
        return WhatsAppAccount.query.filter_by(
            workspace_id=workspace_id,
            is_active=True,
        ).order_by(WhatsAppAccount.created_at.desc()).first()
        
    @property
    def api_url(self) -> str:
        """Get API URL for sending messages."""
        return f"{WHATSAPP_API_BASE}/{self.api_version}/{self.phone_number_id}/messages"
    
    @property
    def headers(self) -> Dict[str, str]:
        """Get request headers with authorization."""
        return {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
        }
    
    # ============================================================
    # Core API Methods
    # ============================================================
    
    def _send_api_request(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        Send request to WhatsApp Cloud API.
        
        Includes rate limiting for coexistence accounts (5 MPS).
        
        Args:
            payload: Message payload
            
        Returns:
            API response dict with success status
        """
        # Rate limiting: check if this account has rate limits
        try:
            from .rate_limiter import get_rate_limiter
            from .models import WhatsAppAccount as _WA
            
            account = _WA.query.filter_by(
                phone_number_id=self.phone_number_id,
                is_active=True,
            ).first()
            
            if account:
                limiter = get_rate_limiter()
                mps = account.mps_limit or (5 if account.is_coexistence else 80)
                
                if not limiter.acquire(self.phone_number_id, mps):
                    # Wait and retry once
                    import time
                    wait = limiter.wait_time(self.phone_number_id, mps)
                    if wait > 0 and wait < 5:  # Don't wait more than 5 seconds
                        time.sleep(wait)
                        if not limiter.acquire(self.phone_number_id, mps):
                            logger.warning(f"Rate limited: {self.phone_number_id} (mps={mps})")
                            return {
                                "success": False,
                                "error": "Rate limit exceeded. Please try again shortly.",
                                "error_code": "130429",
                                "retry_after": limiter.wait_time(self.phone_number_id, mps),
                            }
                    else:
                        return {
                            "success": False,
                            "error": "Rate limit exceeded. Please try again shortly.",
                            "error_code": "130429",
                            "retry_after": wait,
                        }
        except ImportError:
            pass  # Rate limiter not available, proceed without
        except Exception as e:
            logger.warning(f"Rate limiter check failed (non-fatal): {e}")
        
        try:
            logger.info(f"Sending WhatsApp API request to {self.api_url}")
            logger.debug(f"Payload: {json.dumps(payload, indent=2)}")
            
            response = requests.post(
                self.api_url,
                headers=self.headers,
                json=payload,
                timeout=30,
            )
            
            response_data = response.json()
            logger.debug(f"Response: {json.dumps(response_data, indent=2)}")
            
            # Print full response for debugging
            print(f"\n{'='*60}")
            print(f"=== META API RESPONSE ===")
            print(f"Status Code: {response.status_code}")
            print(f"Response: {json.dumps(response_data, indent=2)}")
            print(f"{'='*60}\n")
            
            if response.status_code == 200:
                return {
                    "success": True,
                    "response": response_data,
                    "wamid": response_data.get("messages", [{}])[0].get("id"),
                }
            else:
                error = response_data.get("error", {})
                logger.error(f"WhatsApp API error: {error}")
                return {
                    "success": False,
                    "error": error.get("message", "Unknown error"),
                    "error_code": error.get("code"),
                    "response": response_data,
                }
                
        except requests.exceptions.Timeout:
            logger.error("WhatsApp API timeout")
            return {"success": False, "error": "Request timeout"}
        except requests.exceptions.RequestException as e:
            logger.exception(f"WhatsApp API request failed: {e}")
            return {"success": False, "error": str(e)}
        except Exception as e:
            logger.exception(f"Unexpected error: {e}")
            return {"success": False, "error": str(e)}
    
    def get_media_url(self, media_id: str) -> Optional[str]:
        """
        Get temporary media URL from Meta given a media ID.
        
        Args:
            media_id: WhatsApp media ID
            
        Returns:
            Temporary URL string or None
        """
        try:
            url = f"{WHATSAPP_API_BASE}/{self.api_version}/{media_id}"
            resp = requests.get(url, headers=self.headers, timeout=20)
            data = resp.json()
            
            if resp.status_code == 200:
                return data.get("url")
            else:
                logger.error(f"Failed to get media URL for {media_id}: {data}")
                return None
        except Exception as e:
            logger.error(f"Error fetching media URL for {media_id}: {e}")
            return None

    def send_sticker(self, to: str, sticker: str) -> Dict[str, Any]:
        """
        Send a sticker message.
        
        Args:
            to: Recipient phone number
            sticker: Sticker ID or URL
            
        Returns:
            API response dict with message details
        """
        to = self._normalize_phone(to)
        
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to,
            "type": "sticker",
            "sticker": {}
        }
        
        sticker_url = None
        sticker_id = None
        if sticker.startswith("http"):
            payload["sticker"]["link"] = sticker
            sticker_url = sticker
        else:
            payload["sticker"]["id"] = sticker
            sticker_id = sticker
        
        # Get or create conversation
        conversation = self._get_or_create_conversation(to)
        
        # Send via API
        result = self._send_api_request(payload)
        
        # Store message in DB
        message = self._store_outgoing_message(
            conversation=conversation,
            message_type="sticker",
            content={
                "media_type": "sticker",
                "url": sticker_url,
                "id": sticker_id,
            },
            wamid=result.get("wamid"),
            status="sent" if result.get("success") else "failed",
            error_code=str(result.get("error_code")) if result.get("error_code") else None,
            error_message=result.get("error") if not result.get("success") else None,
        )
        
        result["message_id"] = message.id
        result["conversation_id"] = conversation.id
        result["message"] = message.to_dict()
        
        return result

    def _get_or_create_conversation(
        self,
        user_phone: str,
        user_name: Optional[str] = None,
    ) -> WhatsAppConversation:
        """
        Get existing conversation or create a new one.
        
        Args:
            user_phone: User's phone number
            user_name: User's name (optional)
            
        Returns:
            WhatsAppConversation instance
        """
        # Normalize phone number
        user_phone = self._normalize_phone(user_phone)
        
        # Get or create account
        account = self._get_or_create_account()
        
        # Find existing conversation — try canonical phone first
        conversation = WhatsAppConversation.query.filter_by(
            account_id=account.id,
            user_phone=user_phone,
        ).first()

        # Fallback: find by last 10 digits (catches old un-normalized rows)
        if not conversation and len(user_phone) > 10:
            last10 = user_phone[-10:]
            conversation = WhatsAppConversation.query.filter(
                WhatsAppConversation.account_id == account.id,
                WhatsAppConversation.user_phone.in_([last10, user_phone]),
            ).order_by(WhatsAppConversation.last_message_at.desc().nullslast()).first()
            # Migrate old row to canonical form
            if conversation and conversation.user_phone != user_phone:
                logger.info(f"Migrating conversation {conversation.id} phone {conversation.user_phone} → {user_phone}")
                conversation.user_phone = user_phone
        
        if not conversation:
            conversation = WhatsAppConversation(
                account_id=account.id,
                user_phone=user_phone,
                user_name=user_name,
                status="open",
            )
            self.db_session.add(conversation)
            self.db_session.flush()  # Get ID without committing
            logger.info(f"Created new conversation with {user_phone}")
        
        return conversation
    
    def _get_or_create_account(self) -> WhatsAppAccount:
        """Get or create WhatsApp account record."""
        account = WhatsAppAccount.query.filter_by(
            phone_number_id=self.phone_number_id
        ).first()
        
        if not account:
            account = WhatsAppAccount(
                waba_id=self.waba_id or "unknown",
                phone_number_id=self.phone_number_id,
                display_phone_number=self.phone_number_id,  # Will be updated from API
                is_active=True,
            )
            self.db_session.add(account)
            self.db_session.flush()
            logger.info(f"Created new WhatsApp account: {self.phone_number_id}")
        
        return account

    @staticmethod
    def ensure_all_waba_subscriptions():
        """
        Startup task to ensure ALL active WhatsApp accounts are subscribed to the app.
        This fixes stale or missing subscriptions in production without manual intervention.
        """
        from .models import WhatsAppAccount
        from .utils import subscribe_waba_to_app
        
        try:
            active_accounts = WhatsAppAccount.query.filter_by(is_active=True).all()
            logger.info(f"Startup: Ensuring webhook subscriptions for {len(active_accounts)} active WABAs.")
            
            for account in active_accounts:
                token = account.get_access_token()
                if not token:
                    continue
                
                # We skip if this was already done very recently (optional optimization)
                # For now, we perform it to be sure.
                subscribe_waba_to_app(account.waba_id, token)
                
            return True
        except Exception as e:
            logger.error(f"Failed to ensure all WABA subscriptions: {e}")
            return False
    
    
    # ============================================================
    # Template Management
    # ============================================================
    
    def sync_templates(self) -> Dict[str, Any]:
        """
        Sync all templates from Meta for the current account.
        
        Handles:
        1. Pagination (fetches ALL templates)
        2. Updates existing templates
        3. Creates new templates
        4. Deletes local templates that no longer exist on Meta
        
        Returns:
            Sync stats dict
        """
        from .models import WhatsAppAccount, WhatsAppTemplate
        
        account = self._get_or_create_account()
        
        if not self.access_token:
            return {"success": False, "error": "No access token available"}
            
        try:
            logger.info(f"Starting template sync for account {account.id} (WABA: {account.waba_id})")
            
            # 1. Fetch ALL templates from Meta with pagination
            all_meta_templates = []
            # CRITICAL FIX: message_templates endpoint MUST use WABA ID, not Phone Number ID
            base_url = f"{WHATSAPP_API_BASE}/{self.api_version}/{account.waba_id}/message_templates"
            next_url = base_url
            params = {"limit": 100}  # Max allowed by Meta
            
            while next_url:
                resp = requests.get(
                    next_url,
                    headers={"Authorization": f"Bearer {self.access_token}"},
                    params=params,
                    timeout=30
                )
                
                if resp.status_code != 200:
                    logger.error(f"Meta template fetch failed: {resp.text}")
                    return {"success": False, "error": f"Meta API error: {resp.text}"}
                    
                data = resp.json()
                templates_page = data.get("data", [])
                all_meta_templates.extend(templates_page)
                
                # Check for next page
                paging = data.get("paging", {})
                next_url = paging.get("next")
                params = {}  # Params are encoded in next_url for subsequent requests
                
            logger.info(f"Fetched {len(all_meta_templates)} templates from Meta")
            
            # 2. Process Templates
            meta_template_ids = set()
            synced_count = 0
            created_count = 0
            updated_count = 0
            
            for tpl in all_meta_templates:
                meta_id = tpl.get("id")
                name = tpl.get("name")
                language = tpl.get("language")
                status = tpl.get("status")
                category = tpl.get("category")
                
                # Track for deletion check later
                # Use a composite key of name+language since Meta IDs can sometimes change/rotate
                # But we definitely try to track by ID first if possible
                meta_template_ids.add((name, language))
                
                # Find existing in DB
                existing = WhatsAppTemplate.query.filter_by(
                    account_id=account.id,
                    name=name,
                    language=language
                ).first()
                
                if existing:
                    # Update existing
                    existing.meta_template_id = meta_id
                    existing.status = status
                    existing.category = category
                    existing.components = tpl.get("components", [])
                    existing.rejection_reason = tpl.get("rejected_reason")
                    existing.quality_score = tpl.get("quality_score")
                    
                    # Update body/header/footer text content
                    # Parse body - handle both named and positional variables
                    for comp in tpl.get("components", []):
                        comp_type = comp.get("type", "").upper()
                        if comp_type == "BODY":
                            existing.body_text = comp.get("text", "")
                            import re
                            # Detect all variables
                            all_vars = re.findall(r'\{\{([^}]+)\}\}', existing.body_text)
                            existing.variable_count = len(set(all_vars))
                        elif comp_type == "HEADER":
                            existing.header_text = comp.get("text", "")
                        elif comp_type == "FOOTER":
                            existing.footer_text = comp.get("text", "")
                            
                    existing.last_synced_at = datetime.now(timezone.utc)
                    updated_count += 1
                else:
                    # Create new using the helper method if available, or manual construction
                    try:
                        new_tpl = WhatsAppTemplate.from_meta_template(account.id, tpl)
                    except AttributeError:
                        # Fallback if method doesn't exist
                        new_tpl = WhatsAppTemplate(
                            account_id=account.id,
                            meta_template_id=meta_id,
                            name=name,
                            language=language,
                            status=status,
                            category=category,
                            components=tpl.get("components", []),
                            rejection_reason=tpl.get("rejected_reason"),
                            quality_score=tpl.get("quality_score"),
                        )
                        # Extract text content
                        for comp in tpl.get("components", []):
                            comp_type = comp.get("type", "").upper()
                            if comp_type == "BODY":
                                new_tpl.body_text = comp.get("text", "")
                            elif comp_type == "HEADER":
                                new_tpl.header_text = comp.get("text", "")
                            elif comp_type == "FOOTER":
                                new_tpl.footer_text = comp.get("text", "")
                    
                    new_tpl.last_synced_at = datetime.now(timezone.utc)
                    self.db_session.add(new_tpl)
                    created_count += 1
                    
                synced_count += 1
                
            # 3. Handle Deletions (Templates in DB but not in Meta)
            # Fetch all local templates for this account
            all_local_templates = WhatsAppTemplate.query.filter_by(account_id=account.id).all()
            deleted_count = 0
            
            for local_tpl in all_local_templates:
                # Check if this local template exists in the Set of Meta Templates
                # We use name+language as uniqueness constraint
                if (local_tpl.name, local_tpl.language) not in meta_template_ids:
                    # Template deleted on Meta -> Delete locally
                    logger.info(f"Deleting local template {local_tpl.name} ({local_tpl.language}) - absent from Meta")
                    self.db_session.delete(local_tpl)
                    deleted_count += 1
            
            self.db_session.commit()
            
            return {
                "success": True,
                "total_fetched": len(all_meta_templates),
                "synced": synced_count,
                "created": created_count,
                "updated": updated_count,
                "deleted": deleted_count
            }
            
        except Exception as e:
            logger.exception(f"Template sync failed: {e}")
            self.db_session.rollback()
            return {"success": False, "error": str(e)}
    
    def sync_single_template(self, template_id: int) -> Dict[str, Any]:
        """
        Sync a single template from Meta by its local database ID.
        
        Strategy:
        1. Look up the template in local DB to get meta_template_id, name, and language
        2. Try to fetch by meta_template_id directly (most efficient)
        3. If no meta_template_id, fall back to name+language search
        4. Update local record with latest status, quality_score, rejection_reason
        
        Args:
            template_id: Local database template ID
            
        Returns:
            Dict with success status and updated template data
        """
        from .models import WhatsAppAccount, WhatsAppTemplate
        import re
        
        # Get the template from local DB
        template = WhatsAppTemplate.query.get(template_id)
        if not template:
            return {"success": False, "error": "Template not found in database"}
        
        # Get the account
        account = WhatsAppAccount.query.get(template.account_id)
        if not account:
            return {"success": False, "error": "Account not found"}
        
        if not self.access_token:
            return {"success": False, "error": "No access token available"}
        
        try:
            logger.info(f"Syncing single template {template_id} (name: {template.name}, meta_id: {template.meta_template_id})")
            
            meta_template = None
            api_version = self.api_version
            
            # Strategy 1: Direct fetch by meta_template_id (most efficient)
            if template.meta_template_id:
                url = f"{WHATSAPP_API_BASE}/{api_version}/{template.meta_template_id}"
                params = {"fields": "id,name,status,category,language,components,rejected_reason,quality_score"}
                
                resp = requests.get(
                    url,
                    headers={"Authorization": f"Bearer {self.access_token}"},
                    params=params,
                    timeout=30
                )
                
                if resp.status_code == 200:
                    meta_template = resp.json()
                elif resp.status_code == 404:
                    logger.warning(f"Template {template.meta_template_id} not found on Meta, trying name search")
                else:
                    logger.warning(f"Meta API error for direct fetch: {resp.status_code} - {resp.text}")
            
            # Strategy 2: Search by name+language if direct fetch failed
            if not meta_template:
                url = f"{WHATSAPP_API_BASE}/{api_version}/{account.waba_id}/message_templates"
                params = {
                    "name": template.name,
                    "language": template.language,
                    "fields": "id,name,status,category,language,components,rejected_reason,quality_score"
                }
                
                resp = requests.get(
                    url,
                    headers={"Authorization": f"Bearer {self.access_token}"},
                    params=params,
                    timeout=30
                )
                
                if resp.status_code == 200:
                    data = resp.json()
                    templates = data.get("data", [])
                    # Find exact match
                    for tpl in templates:
                        if tpl.get("name") == template.name and tpl.get("language") == template.language:
                            meta_template = tpl
                            break
                else:
                    logger.error(f"Meta name search failed: {resp.status_code} - {resp.text}")
                    return {"success": False, "error": f"Meta API error: {resp.text}"}
            
            if not meta_template:
                # Template no longer exists on Meta
                logger.warning(f"Template {template.name} not found on Meta - may have been deleted")
                return {
                    "success": False,
                    "error": "Template not found on Meta. It may have been deleted.",
                    "deleted_on_meta": True
                }
            
            # Update local record with Meta data
            old_status = template.status
            template.meta_template_id = meta_template.get("id")
            template.status = meta_template.get("status")
            template.category = meta_template.get("category")
            template.rejection_reason = meta_template.get("rejected_reason")
            template.quality_score = meta_template.get("quality_score")
            template.components = meta_template.get("components", [])
            template.last_synced_at = datetime.now(timezone.utc)
            
            # Update text fields from components
            for comp in meta_template.get("components", []):
                comp_type = comp.get("type", "").upper()
                if comp_type == "BODY":
                    template.body_text = comp.get("text", "")
                    # Detect variables
                    all_vars = re.findall(r'\{\{([^}]+)\}\}', template.body_text)
                    template.variable_count = len(set(all_vars))
                elif comp_type == "HEADER":
                    template.header_text = comp.get("text", "")
                elif comp_type == "FOOTER":
                    template.footer_text = comp.get("text", "")
            
            # Track approval time if status changed from PENDING to APPROVED
            if old_status == "PENDING" and template.status == "APPROVED" and template.submitted_at:
                template.approved_at = datetime.now(timezone.utc)
                template.approval_duration_seconds = int(
                    (template.approved_at - template.submitted_at).total_seconds()
                )
            
            self.db_session.commit()
            
            status_changed = old_status != template.status
            
            return {
                "success": True,
                "template": template.to_dict(),
                "status_changed": status_changed,
                "old_status": old_status,
                "new_status": template.status,
                "quality_score": meta_template.get("quality_score"),
                "last_synced_at": template.last_synced_at.isoformat() + "Z"
            }
            
        except Exception as e:
            logger.exception(f"Single template sync failed: {e}")
            self.db_session.rollback()
            return {"success": False, "error": str(e)}
    
    def _store_outgoing_message(
        self,
        conversation: WhatsAppConversation,
        message_type: str,
        content: Dict[str, Any],
        wamid: Optional[str] = None,
        status: str = "pending",
        error_code: Optional[str] = None,
        error_message: Optional[str] = None,
        template_name: Optional[str] = None,
        template_category: Optional[str] = None,
        campaign_id: Optional[int] = None,
    ) -> WhatsAppMessage:
        """
        Store outgoing message in database.
        
        Args:
            conversation: Conversation instance
            message_type: Type of message (text, template, etc.)
            content: Message content
            wamid: WhatsApp message ID (if sent successfully)
            status: Message status
            error_code: Error code (if failed)
            error_message: Error message (if failed)
            template_name: Template name for analytics
            template_category: Template category (UTILITY, MARKETING, AUTHENTICATION)
            campaign_id: ID of the bulk campaign (if applicable)
            
        Returns:
            WhatsAppMessage instance
        """
        message = WhatsAppMessage(
            conversation_id=conversation.id,
            direction="outgoing",
            type=message_type,
            content=content,
            wamid=wamid,
            status=status,
            error_code=error_code,
            error_message=error_message,
            template_name=template_name,
            template_category=template_category,
            campaign_id=campaign_id,
        )
        self.db_session.add(message)
        
        # Update conversation timestamp and outbound tracking
        conversation.last_message_at = datetime.now(timezone.utc)
        conversation.last_outbound_at = datetime.now(timezone.utc)
        
        self.db_session.commit()

        if status == "sent":
            try:
                from .usage_events import emit_message_sent_usage_event, broadcast_usage_event
                event = emit_message_sent_usage_event(
                    db_session=self.db_session,
                    account_id=conversation.account_id,
                    message=message,
                    conversation=conversation,
                )
                broadcast_usage_event(event)
            except Exception as exc:
                logger.warning("Usage event emit failed (non-fatal): %s", exc)

        return message
    
    @staticmethod
    def _normalize_phone(phone: str) -> str:
        """Normalize phone number using the canonical util (E.164 digits, with country code)."""
        return _normalize_phone_util(phone)
    
    # ============================================================
    # Send Text Message
    # ============================================================
    
    def send_text(
        self,
        to: str,
        text: str,
        waba_id: Optional[str] = None,
        preview_url: bool = False,
    ) -> Dict[str, Any]:
        """
        Send a text message.
        
        Args:
            to: Recipient phone number (E.164 format without +)
            text: Message text
            waba_id: Optional WABA ID override
            preview_url: Whether to show URL preview
            
        Returns:
            Response dict with success status
        """
        to = self._normalize_phone(to)
        
        # Get or create conversation first to check if closed
        conversation = self._get_or_create_conversation(to)
        
        # Block sending if conversation is closed by agent
        if conversation.closed_by_agent:
            return {
                "success": False,
                "error": "Cannot send message. This conversation was closed by an agent. Reopen it first or use a template message.",
                "error_code": "CONVERSATION_CLOSED"
            }
        
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to,
            "type": "text",
            "text": {
                "preview_url": preview_url,
                "body": text,
            },
        }
        
        # Send via API
        result = self._send_api_request(payload)
        
        # Store message in DB
        message = self._store_outgoing_message(
            conversation=conversation,
            message_type="text",
            content={"text": text, "preview_url": preview_url},
            wamid=result.get("wamid"),
            status="sent" if result.get("success") else "failed",
            error_code=str(result.get("error_code")) if result.get("error_code") else None,
            error_message=result.get("error") if not result.get("success") else None,
        )
        
        result["message_id"] = message.id
        result["conversation_id"] = conversation.id
        result["message"] = message.to_dict()
        
        return result
    
    # ============================================================
    # Send Template Message
    # ============================================================
    
    def send_template(
        self,
        to: str,
        template_name: str,
        language_code: str = "en",
            components: Optional[List[Dict]] = None,
        waba_id: Optional[str] = None,
        campaign_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Send a template message.
        
        IMPORTANT: Meta Cloud API is strict about template payloads:
        - Templates without variables (like hello_world) must NOT include components
        - Never send empty components array - omit the field entirely
        - recipient_type is NOT required for template messages
        
        Args:
            to: Recipient phone number
            template_name: Template name (must be approved)
            language_code: Language code (default: en)
            components: Template components (header, body, button params) - ONLY if template has variables
            waba_id: Optional WABA ID override
            campaign_id: Optional bulk campaign ID for tracking
            
        Returns:
            Response dict with success status
        """
        to = self._normalize_phone(to)
        
        # Look up template body text, category, and structure from database
        template_body_text = None
        template_header_text = None
        template_footer_text = None
        template_category = None
        template_components_schema = None
        try:
            from .models import WhatsAppTemplate
            template_record = WhatsAppTemplate.query.filter_by(
                name=template_name,
                language=language_code
            ).first()
            if template_record:
                template_body_text = template_record.body_text
                template_header_text = template_record.header_text
                template_footer_text = template_record.footer_text
                template_category = template_record.category  # UTILITY, MARKETING, AUTHENTICATION
                template_components_schema = template_record.components  # Full template structure from Meta
        except Exception as e:
            logger.warning(f"Could not look up template body: {e}")
        
        # Build template object - EXACTLY matching Meta API format
        template_obj = {
            "name": template_name,
            "language": {"code": language_code},
        }
        
        # CRITICAL: Build components based on template structure
        # An empty array [] or None should NOT add the components field
        final_components = []
        
        if components and len(components) > 0:
            # Validate components have actual parameters
            final_components = [c for c in components if c.get("parameters")]
        
        # Auto-detect and add button components for authentication templates
        # If template has buttons with URL type that require parameters, we need to add them
        if template_components_schema and final_components:
            # Extract body params from the provided components
            body_params = []
            for comp in final_components:
                if comp.get("type", "").lower() == "body" and comp.get("parameters"):
                    body_params = [p.get("text", "") for p in comp.get("parameters", [])]
                    break
            
            # Check if template has BUTTONS that need parameters
            has_button_component = any(
                comp.get("type", "").lower() == "button" 
                for comp in final_components
            )
            
            if not has_button_component:
                # Look for button definitions in template schema that need URL params
                for idx, schema_comp in enumerate(template_components_schema):
                    comp_type = schema_comp.get("type", "").upper()
                    if comp_type == "BUTTONS":
                        buttons = schema_comp.get("buttons", [])
                        for btn_idx, button in enumerate(buttons):
                            btn_type = button.get("type", "").upper()
                            # URL buttons with {{1}} parameter, OTP buttons, COPY_CODE buttons
                            if btn_type in ["URL", "OTP", "COPY_CODE"]:
                                # Check if URL has a variable like {{1}}
                                url = button.get("url", "")
                                example = button.get("example", [])
                                otp_type = button.get("otp_type", "")
                                
                                # For OTP/COPY_CODE buttons, use the first body param (OTP code)
                                if btn_type in ["OTP", "COPY_CODE"] or otp_type == "COPY_CODE":
                                    if body_params:
                                        final_components.append({
                                            "type": "button",
                                            "sub_type": "url",
                                            "index": str(btn_idx),
                                            "parameters": [{
                                                "type": "text",
                                                "text": body_params[0]  # OTP code
                                            }]
                                        })
                                # For URL buttons with {{1}} variable
                                elif "{{" in url:
                                    if body_params:
                                        final_components.append({
                                            "type": "button",
                                            "sub_type": "url",
                                            "index": str(btn_idx),
                                            "parameters": [{
                                                "type": "text",
                                                "text": body_params[0] if body_params else ""
                                            }]
                                        })
                            # FLOW buttons REQUIRE a button component at send time, or
                            # Meta rejects with (#131009) "Components sub_type invalid".
                            # An empty action is valid for NAVIGATE flows (Meta auto-
                            # generates the flow_token).
                            elif btn_type == "FLOW":
                                final_components.append({
                                    "type": "button",
                                    "sub_type": "flow",
                                    "index": str(btn_idx),
                                    "parameters": [{
                                        "type": "action",
                                        "action": {}
                                    }]
                                })
        
        # ── Auto-inject parameter_name from template schema (named params) ──
        # If the template schema uses body_text_named_params (e.g. {{name}}, {{email}}),
        # Meta API requires each parameter to include "parameter_name".
        # Auto-detect this from the schema and inject it so callers don't need to know.
        if template_components_schema and final_components:
            # Find named param definitions in the schema
            schema_named_params = None
            for schema_comp in template_components_schema:
                if schema_comp.get("type", "").lower() == "body":
                    example = schema_comp.get("example", {})
                    if example.get("body_text_named_params"):
                        schema_named_params = example["body_text_named_params"]
                    break
            
            # If schema has named params, inject parameter_name into final_components
            if schema_named_params:
                for comp in final_components:
                    if comp.get("type", "").lower() == "body" and comp.get("parameters"):
                        params = comp["parameters"]
                        for i, param in enumerate(params):
                            if not param.get("parameter_name") and i < len(schema_named_params):
                                param["parameter_name"] = schema_named_params[i]["param_name"]
                        break
        
        if final_components:
            template_obj["components"] = final_components
            
        # Helper: Check if we have named params wrapper from validator or direct call
        # Structure: [{"type": "body", "named_params": {"name": "John"}}, {"type": "header", "parameters": [...]}]
        named_params = None
        other_components = []
        has_parameter_name_in_params = False
        
        if components:
            for comp in components:
                if comp.get("named_params"):
                    named_params = comp.get("named_params")
                elif comp.get("parameters"):
                    other_components.append(comp)
        
        # Also check if components already have parameter_name in their parameters
        # This happens when drip engine passes components with named params
        if not named_params and final_components:
            for comp in final_components:
                if comp.get("type", "").lower() == "body" and comp.get("parameters"):
                    for param in comp.get("parameters", []):
                        if param.get("parameter_name"):
                            has_parameter_name_in_params = True
                            break
                    break
            
        # If named params exist, switch to TemplateBuilder strategy
        if named_params:
            from .template_builder import TemplateBuilder
            builder = TemplateBuilder(template_name, language_code)
            
            # Apply named body params
            builder.add_named_body_params(named_params)
            
            # Apply other components (headers, buttons, etc.)
            for comp in other_components:
                comp_type = comp.get("type", "").lower()
                params = comp.get("parameters", [])
                
                if comp_type == "header" and params:
                    p = params[0]
                    p_type = p.get("type", "").lower()
                    if p_type == "image":
                        builder.add_header_image(p.get("image", {}).get("link", ""))
                    elif p_type == "video":
                        builder.add_header_video(p.get("video", {}).get("link", ""))
                    elif p_type == "document":
                        builder.add_header_document(
                            p.get("document", {}).get("link", ""),
                            filename=p.get("document", {}).get("filename")
                        )
                    elif p_type == "text":
                        builder.add_header_text(p.get("text", ""))
                
                # Note: Buttons are usually auto-detected from body_params in TemplateBuilder
                # or manually added. Support for manual button components can be added if needed.
            
            # Re-build payload using builder
            payload = builder.build_with_recipient(to)
            
            # Update template_obj for storage logging consistency
            template_obj = payload.get("template", template_obj)
        elif has_parameter_name_in_params:
            # Components already have parameter_name in them - use them directly
            # This is the proper named parameters format for Meta API
            payload = {
                "messaging_product": "whatsapp",
                "to": to,
                "type": "template",
                "template": template_obj,
            }
        else:
            # Standard positional path
            payload = {
                "messaging_product": "whatsapp",
                "to": to,
                "type": "template",
                "template": template_obj,
            }
        
        # Print the exact payload being sent for debugging
        print(f"\n{'='*60}")
        print(f"=== TEMPLATE PAYLOAD ===")
        print(f"Template Name: {template_name}")
        print(f"Language Code: {language_code}")
        print(f"Template Schema: {template_components_schema}")
        print(f"Input Components: {components}")
        print(f"Final Components: {final_components}")
        print(f"Full Payload: {json.dumps(payload, indent=2)}")
        print(f"{'='*60}\n")
        
        # Get or create conversation
        conversation = self._get_or_create_conversation(to)
        
        # Send via API
        result = self._send_api_request(payload)
        
        # Enhanced logging for template delivery issues
        if result.get("success"):
            logger.info(f"Template '{template_name}' sent successfully, wamid: {result.get('wamid')}")
        else:
            logger.error(f"Template '{template_name}' failed: {result.get('error')}")
        
        # Extract body_params for frontend preview display
        body_params = []
        if components:
            for comp in components:
                if comp.get("type", "").lower() == "body" and comp.get("parameters"):
                    body_params = [p.get("text", "") for p in comp.get("parameters", [])]
                    break
        
        # Store message in DB
        message = self._store_outgoing_message(
            conversation=conversation,
            message_type="template",
            content={
                "template_name": template_name,
                "language": language_code,
                "components": components if components else None,
                "body_params": body_params,  # Store for frontend preview
                "body": template_body_text,  # Template body text pattern for substitution
                "header": template_header_text,
                "footer": template_footer_text,
                "payload_sent": payload,  # Store actual payload for debugging
            },
            wamid=result.get("wamid"),
            status="sent" if result.get("success") else "failed",
            error_code=str(result.get("error_code")) if result.get("error_code") else None,
            error_message=result.get("error") if not result.get("success") else None,
            template_name=template_name,  # Store for analytics tracking
            template_category=template_category,  # Store for category-wise analytics
            campaign_id=campaign_id,
        )
        
        result["message_id"] = message.id
        result["conversation_id"] = conversation.id
        result["message"] = message.to_dict()
        result["payload_sent"] = payload  # Return payload for debugging
        
        # Broadcast via SSE for real-time inbox update
        if result.get("success"):
            try:
                # Get account for workspace_id
                account = WhatsAppAccount.query.filter_by(phone_number_id=self.phone_number_id).first()
                workspace_id = account.workspace_id if account else None
                
                notification_manager.broadcast("whatsapp_message_received", {
                    "message": message.to_dict(),
                    "conversation_id": conversation.id,
                    "account_id": account.id if account else None,
                    "workspace_id": workspace_id
                })
                logger.info(f"Broadcasted template message: {message.id}")
            except Exception as e:
                logger.error(f"Failed to broadcast template message event: {e}")
        
        return result
    
    def send_template_with_builder(
        self,
        to: str,
        template_name: str,
        language_code: str = "en",
        body_params: Optional[List[str]] = None,
        header_image_url: Optional[str] = None,
        header_video_url: Optional[str] = None,
        header_document_url: Optional[str] = None,
        header_text: Optional[str] = None,
        button_payloads: Optional[List[Dict]] = None,
        waba_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Send a template message using the TemplateBuilder for proper payload generation.
        
        This method is recommended over send_template() as it properly handles:
        - Templates without variables (no components field)
        - Image/video/document headers
        - Body parameters
        - Button parameters
        
        Args:
            to: Recipient phone number
            template_name: Template name (must be approved)
            language_code: Language code (default: en)
            body_params: List of body parameter values (for {{1}}, {{2}}, etc.)
            header_image_url: URL for image header
            header_video_url: URL for video header
            header_document_url: URL for document header
            header_text: Text for header variable
            button_payloads: List of button configs [{"index": 0, "type": "url", "value": "..."}]
            waba_id: Optional WABA ID override
            
        Returns:
            Response dict with success status
        """
        from .template_builder import TemplateBuilder
        
        to = self._normalize_phone(to)
        
        # Use builder to construct proper payload
        builder = TemplateBuilder(template_name, language_code)
        
        # Add header if provided
        if header_image_url:
            builder.add_header_image(header_image_url)
        elif header_video_url:
            builder.add_header_video(header_video_url)
        elif header_document_url:
            builder.add_header_document(header_document_url)
        elif header_text:
            builder.add_header_text(header_text)
        
        # Add body params if provided
        if body_params:
            builder.add_body_params(body_params)
        
        # Add button params if provided
        if button_payloads:
            for btn in button_payloads:
                btn_index = btn.get("index", 0)
                btn_type = btn.get("type", "url")
                btn_value = btn.get("value", "")
                
                if btn_type == "url":
                    builder.add_url_button(btn_index, btn_value)
                elif btn_type == "quick_reply":
                    builder.add_quick_reply_button(btn_index, btn_value)
                elif btn_type == "copy_code":
                    builder.add_copy_code_button(btn_index, btn_value)
        
        # Build payload
        payload = builder.build_with_recipient(to)
        
        # Log for debugging
        logger.info(f"Sending template '{template_name}' via builder to {to}")
        logger.debug(f"Builder payload: {json.dumps(payload, indent=2)}")
        
        # Get or create conversation
        conversation = self._get_or_create_conversation(to)
        
        # Send via API
        result = self._send_api_request(payload)
        
        # Store message in DB
        message = self._store_outgoing_message(
            conversation=conversation,
            message_type="template",
            content={
                "template_name": template_name,
                "language": language_code,
                "body_params": body_params,
                "header_image_url": header_image_url,
                "header_video_url": header_video_url,
                "header_document_url": header_document_url,
                "header_text": header_text,
                "button_payloads": button_payloads,
                "payload_sent": payload,
            },
            wamid=result.get("wamid"),
            status="sent" if result.get("success") else "failed",
            error_code=str(result.get("error_code")) if result.get("error_code") else None,
            error_message=result.get("error") if not result.get("success") else None,
            template_name=template_name,  # Store for analytics tracking
        )
        
        result["message_id"] = message.id
        result["conversation_id"] = conversation.id
        result["message"] = message.to_dict()
        result["payload_sent"] = payload
        
        # Broadcast via SSE for real-time inbox update
        if result.get("success"):
            try:
                account = WhatsAppAccount.query.filter_by(phone_number_id=self.phone_number_id).first()
                workspace_id = account.workspace_id if account else None
                
                notification_manager.broadcast("whatsapp_message_received", {
                    "message": message.to_dict(),
                    "conversation_id": conversation.id,
                    "account_id": account.id if account else None,
                    "workspace_id": workspace_id
                })
                logger.info(f"Broadcasted template (builder) message: {message.id}")
            except Exception as e:
                logger.error(f"Failed to broadcast template message event: {e}")
        
        return result
    
    # ============================================================
    # Send Media Messages
    # ============================================================
    
    def send_image(
        self,
        to: str,
        image_url: Optional[str] = None,
        image_id: Optional[str] = None,
        caption: Optional[str] = None,
        waba_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Send an image message.
        
        Args:
            to: Recipient phone number
            image_url: URL of the image (either url or id required)
            image_id: Media ID of uploaded image
            caption: Optional caption
            waba_id: Optional WABA ID override
            
        Returns:
            Response dict with success status
        """
        return self._send_media(to, "image", image_url, image_id, caption, waba_id)
    
    def send_video(
        self,
        to: str,
        video_url: Optional[str] = None,
        video_id: Optional[str] = None,
        caption: Optional[str] = None,
        waba_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Send a video message."""
        return self._send_media(to, "video", video_url, video_id, caption, waba_id)
    
    def send_audio(
        self,
        to: str,
        audio_url: Optional[str] = None,
        audio_id: Optional[str] = None,
        waba_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Send an audio message (no caption support)."""
        return self._send_media(to, "audio", audio_url, audio_id, None, waba_id)
    
    def send_document(
        self,
        to: str,
        document_url: Optional[str] = None,
        document_id: Optional[str] = None,
        caption: Optional[str] = None,
        filename: Optional[str] = None,
        waba_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Send a document message."""
        return self._send_media(to, "document", document_url, document_id, caption, waba_id, filename)
    
    def _send_media(
        self,
        to: str,
        media_type: str,
        media_url: Optional[str] = None,
        media_id: Optional[str] = None,
        caption: Optional[str] = None,
        waba_id: Optional[str] = None,
        filename: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Internal method to send any media message.
        
        Args:
            to: Recipient phone number
            media_type: Type of media (image, video, audio, document)
            media_url: URL of media (either url or id required)
            media_id: Media ID if uploaded
            caption: Optional caption (not supported for audio)
            waba_id: Optional WABA ID override
            filename: Optional filename (for documents)
            
        Returns:
            Response dict with success status
        """
        to = self._normalize_phone(to)
        
        if not media_url and not media_id:
            return {"success": False, "error": "Either media_url or media_id is required"}
        
        media_obj = {}
        if media_id:
            media_obj["id"] = media_id
        else:
            media_obj["link"] = media_url
        
        if caption and media_type != "audio":
            media_obj["caption"] = caption
        
        if filename and media_type == "document":
            media_obj["filename"] = filename
        
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to,
            "type": media_type,
            media_type: media_obj,
        }
        
        # Get or create conversation
        conversation = self._get_or_create_conversation(to)
        
        # Send via API
        result = self._send_api_request(payload)
        
        # Store message in DB
        message = self._store_outgoing_message(
            conversation=conversation,
            message_type=media_type,
            content={
                "media_type": media_type,
                "url": media_url,
                "id": media_id,
                "caption": caption,
                "filename": filename,
            },
            wamid=result.get("wamid"),
            status="sent" if result.get("success") else "failed",
            error_code=str(result.get("error_code")) if result.get("error_code") else None,
            error_message=result.get("error") if not result.get("success") else None,
        )
        
        result["message_id"] = message.id
        result["conversation_id"] = conversation.id
        result["message"] = message.to_dict()
        
        return result
    
    # ============================================================
    # Send Interactive Messages
    # ============================================================
    
    def send_interactive_buttons(
        self,
        to: str,
        body_text: str,
        buttons: List[Dict[str, str]],
        header_text: Optional[str] = None,
        header_image_url: Optional[str] = None,
        header_video_url: Optional[str] = None,
        header_document_url: Optional[str] = None,
        header_document_filename: Optional[str] = None,
        footer_text: Optional[str] = None,
        waba_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Send an interactive message with buttons (max 3 buttons).
        
        Args:
            to: Recipient phone number
            body_text: Message body text
            buttons: List of button dicts [{"id": "btn1", "title": "Button 1"}, ...]
            header_text: Optional text header
            header_image_url: Optional image URL for header (takes priority over text)
            header_video_url: Optional video URL for header
            header_document_url: Optional document URL for header
            header_document_filename: Filename for document header
            footer_text: Optional footer
            waba_id: Optional WABA ID override
            
        Returns:
            Response dict with success status
        """
        to = self._normalize_phone(to)
        
        # Format buttons - handle both pre-formatted and simple formats
        formatted_buttons = []
        for btn in buttons[:3]:  # Max 3 buttons
            # Check if button is already in pre-formatted structure (from interactive automation)
            if btn.get("type") == "reply" and isinstance(btn.get("reply"), dict):
                # Already formatted: {'type': 'reply', 'reply': {'id': '...', 'title': '...'}}
                btn_id = btn["reply"].get("id", str(len(formatted_buttons) + 1))
                btn_title = btn["reply"].get("title", "")
            else:
                # Simple format: {'id': '...', 'title': '...'}
                btn_id = btn.get("id", str(len(formatted_buttons) + 1))
                btn_title = btn.get("title", "")
            
            # Ensure title is not empty (Meta requires this)
            if not btn_title:
                btn_title = f"Option {len(formatted_buttons) + 1}"
            
            formatted_buttons.append({
                "type": "reply",
                "reply": {
                    "id": btn_id,
                    "title": btn_title[:20],  # Max 20 chars
                },
            })
        
        # Debug: print the formatted buttons
        print(f"Formatted buttons for Meta API: {formatted_buttons}")
        
        interactive = {
            "type": "button",
            "body": {"text": body_text},
            "action": {"buttons": formatted_buttons},
        }
        
        # Header support: image > video > document > text (priority order)
        header_type = None
        print(f"[DEBUG] send_interactive_buttons header params:")
        print(f"        header_image_url: {header_image_url}")
        print(f"        header_video_url: {header_video_url}")
        print(f"        header_document_url: {header_document_url}")
        print(f"        header_text: {header_text}")
        
        if header_image_url:
            interactive["header"] = {"type": "image", "image": {"link": header_image_url}}
            header_type = "image"
            print(f"        -> Using IMAGE header: {header_image_url}")
        elif header_video_url:
            interactive["header"] = {"type": "video", "video": {"link": header_video_url}}
            header_type = "video"
            print(f"        -> Using VIDEO header: {header_video_url}")
        elif header_document_url:
            doc_header = {"link": header_document_url}
            if header_document_filename:
                doc_header["filename"] = header_document_filename
            interactive["header"] = {"type": "document", "document": doc_header}
            header_type = "document"
            print(f"        -> Using DOCUMENT header: {header_document_url}")
        elif header_text:
            interactive["header"] = {"type": "text", "text": header_text}
            header_type = "text"
            print(f"        -> Using TEXT header: {header_text}")
        else:
            print(f"        -> NO header")
        
        if footer_text:
            interactive["footer"] = {"text": footer_text}
        
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to,
            "type": "interactive",
            "interactive": interactive,
        }
        
        # Get or create conversation
        conversation = self._get_or_create_conversation(to)
        
        # Send via API
        result = self._send_api_request(payload)
        
        # Store message in DB
        message = self._store_outgoing_message(
            conversation=conversation,
            message_type="interactive",
            content={
                "interactive_type": "button",
                "body": body_text,
                "header": header_text,
                "header_type": header_type,
                "header_image_url": header_image_url,
                "header_video_url": header_video_url,
                "header_document_url": header_document_url,
                "header_document_filename": header_document_filename,
                "footer": footer_text,
                "buttons": buttons,
            },
            wamid=result.get("wamid"),
            status="sent" if result.get("success") else "failed",
            error_code=str(result.get("error_code")) if result.get("error_code") else None,
            error_message=result.get("error") if not result.get("success") else None,
        )
        
        result["message_id"] = message.id
        result["conversation_id"] = conversation.id
        result["message"] = message.to_dict()
        
        return result
    
    def send_interactive_list(
        self,
        to: str,
        body_text: str,
        button_text: str,
        sections: List[Dict],
        header_text: Optional[str] = None,
        footer_text: Optional[str] = None,
        waba_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Send an interactive message with a list menu.
        
        Args:
            to: Recipient phone number
            body_text: Message body text
            button_text: Button text to open the list
            sections: List of sections with rows
            header_text: Optional header
            footer_text: Optional footer
            waba_id: Optional WABA ID override
            
        Returns:
            Response dict with success status
        """
        to = self._normalize_phone(to)
        
        interactive = {
            "type": "list",
            "body": {"text": body_text},
            "action": {
                "button": button_text[:20],  # Max 20 chars
                "sections": sections,
            },
        }
        
        if header_text:
            interactive["header"] = {"type": "text", "text": header_text}
        
        if footer_text:
            interactive["footer"] = {"text": footer_text}
        
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to,
            "type": "interactive",
            "interactive": interactive,
        }
        
        # Get or create conversation
        conversation = self._get_or_create_conversation(to)
        
        # Send via API
        result = self._send_api_request(payload)
        
        # Store message in DB
        message = self._store_outgoing_message(
            conversation=conversation,
            message_type="interactive",
            content={
                "interactive_type": "list",
                "body": body_text,
                "header": header_text,
                "footer": footer_text,
                "button": button_text,
                "sections": sections,
            },
            wamid=result.get("wamid"),
            status="sent" if result.get("success") else "failed",
            error_code=str(result.get("error_code")) if result.get("error_code") else None,
            error_message=result.get("error") if not result.get("success") else None,
        )
        
        result["message_id"] = message.id
        result["conversation_id"] = conversation.id
        result["message"] = message.to_dict()
        
        return result
    
    
    # ============================================================
    # Phase-2: Template Sync Methods
    # ============================================================
    
    def sync_single_template(self, template_id: int) -> Dict[str, Any]:
        """
        Sync a single template from Meta to update its status, quality, etc.
        """
        from .models import WhatsAppTemplate
        
        template = WhatsAppTemplate.query.get(template_id)
        if not template:
            return {"success": False, "error": "Template not found"}
            
        if not self.access_token:
            return {"success": False, "error": "Access token required"}
            
        meta_data = None
        old_status = template.status
        deleted_on_meta = False
        
        # 1. Try Direct Fetch by Meta ID
        if template.meta_template_id:
            try:
                url = f"{WHATSAPP_API_BASE}/{self.api_version}/{template.meta_template_id}"
                resp = requests.get(url, headers={"Authorization": f"Bearer {self.access_token}"})
                if resp.status_code == 200:
                    meta_data = resp.json()
                elif resp.status_code == 404:
                    # Might be deleted, but let's double check with search
                    logger.warning(f"Template {template.meta_template_id} not found by ID. Checking by name.")
            except Exception as e:
                logger.error(f"Error fetching template by ID: {e}")

        # 2. Fallback: Search by name/language if not found by ID
        if not meta_data and self.waba_id:
            try:
                url = f"{WHATSAPP_API_BASE}/{self.api_version}/{self.waba_id}/message_templates"
                params = {
                    "name": template.name,
                    "limit": 50 # Retrieve a batch to filter
                }
                resp = requests.get(url, params=params, headers={"Authorization": f"Bearer {self.access_token}"})
                
                if resp.status_code == 200:
                    data = resp.json()
                    # Filter for exact match on name AND language
                    for t in data.get("data", []):
                        if t.get("name") == template.name and t.get("language") == template.language:
                            meta_data = t
                            break
                            
                    if not meta_data:
                         logger.warning(f"Template {template.name} ({template.language}) not found in search.")
                         deleted_on_meta = True
                else:
                    logger.error(f"Failed to search templates: {resp.text}")
            except Exception as e:
                 logger.error(f"Error searching template: {e}")
        
        if not meta_data:
            if deleted_on_meta:
                return {
                    "success": False, 
                    "error": "Template not found on Meta. It may have been deleted.",
                    "deleted_on_meta": True
                }
            return {"success": False, "error": "Could not fetch template from Meta"}

        # 3. Update Local Record
        try:
            template.status = meta_data.get("status", template.status)
            template.category = meta_data.get("category", template.category)
            template.meta_template_id = meta_data.get("id", template.meta_template_id)
            template.components = meta_data.get("components", template.components)
            template.quality_score = meta_data.get("quality_score", template.quality_score)
            template.rejection_reason = meta_data.get("rejected_reason")
            
            # Update timestamps
            template.last_synced_at = datetime.now(timezone.utc)
            if template.status == "APPROVED" and not template.approved_at:
                template.approved_at = datetime.now(timezone.utc)
                if template.submitted_at:
                    delta = (template.approved_at - template.submitted_at).total_seconds()
                    template.approval_duration_seconds = int(delta)

            self.db_session.commit()
            
            return {
                "success": True,
                "template": template.to_dict(),
                "status_changed": old_status != template.status,
                "old_status": old_status,
                "new_status": template.status,
                "quality_score": template.quality_score,
                "last_synced_at": template.last_synced_at.isoformat() + "Z"
            }
            
        except Exception as e:
            logger.exception(f"Error updating template record: {e}")
            return {"success": False, "error": str(e)}

    # ============================================================
    # Template Management (Native - Phase 2)
    # ============================================================
    
    def create_draft_template(self, account_id: int, data: Dict[str, Any]) -> Tuple[Dict[str, Any], bool]:
        """
        Create a new template draft (Local Status: DRAFT, Meta Status: None).
        
        Args:
            account_id: The ID of the WhatsAppAccount.
            data: Template data (name, category, components, language).
            
        Returns:
            (template_dict, success)
        """
        try:
            account = WhatsAppAccount.query.get(account_id)
            if not account:
                return {"error": "Account not found"}, False

            name = data.get("name", "").strip().lower().replace(" ", "_")
            language = data.get("language", "en_US")
            
            # Check for existing name collision in this account
            existing = WhatsAppTemplate.query.filter_by(
                account_id=account_id, 
                name=name, 
                language=language
            ).first()
            
            if existing:
                if existing.is_archived:
                    # Un-archive if it was archived
                    existing.is_archived = False
                    existing.local_status = "DRAFT"
                    existing.meta_status = None # Reset meta status as we are restarting
                    existing.components = data.get("components")
                    existing.body_text = self._extract_text(data, "BODY")
                    existing.header_text = self._extract_text(data, "HEADER")
                    existing.footer_text = self._extract_text(data, "FOOTER")
                    self.db_session.commit()
                    return existing.to_dict(), True
                else:
                    return {"error": f"Template '{name}' ({language}) already exists."}, False

            # Create new Draft
            template = WhatsAppTemplate(
                account_id=account_id,
                name=name,
                category=data.get("category", "UTILITY"),
                language=language,
                components=data.get("components", []),
                body_text=self._extract_text(data, "BODY"),
                header_text=self._extract_text(data, "HEADER"),
                footer_text=self._extract_text(data, "FOOTER"),
                variable_count=self._count_variables(self._extract_text(data, "BODY")),
                
                # Dual Status
                local_status="DRAFT",
                meta_status=None,
                status="PENDING", # Backward compat, or use DRAFT if we add it to enum
                
                # Approval Acceleration defaults - read from data or default
                confidence_initial=data.get("confidence_initial", 0),
                validation_flags=data.get("validation_flags", []),
                detected_intent=data.get("detected_intent"),
                approval_path=data.get("approval_path"),
            )
            
            self.db_session.add(template)
            self.db_session.commit()
            
            return template.to_dict(), True
            
        except Exception as e:
            logger.exception(f"Error creating draft template: {e}")
            self.db_session.rollback()
            return {"error": str(e)}, False

    def update_draft_template(self, template_id: int, data: Dict[str, Any]) -> Tuple[Dict[str, Any], bool]:
        """
        Update an existing template draft.
        """
        try:
            template = WhatsAppTemplate.query.get(template_id)
            if not template:
                return {"error": "Template not found"}, False
                
            if template.is_archived:
                 return {"error": "Cannot edit archived template"}, False
            
            # If template is already SUBMITTED or APPROVED, simple edit is risky
            # But the requirement allows update_draft to just update local state before re-submission
            # We assume UI warns the user.
            
            if "components" in data:
                template.components = data["components"]
                template.body_text = self._extract_text(data, "BODY")
                template.header_text = self._extract_text(data, "HEADER")
                template.footer_text = self._extract_text(data, "FOOTER")
                template.variable_count = self._count_variables(template.body_text)

            if "category" in data:
                template.category = data["category"]
            
            # Updating a draft always resets local_status to DRAFT if it was SUBMITTED?
            # Decision: Yes, because it differs from what's on Meta.
            if template.local_status == "SUBMITTED":
                 template.local_status = "DRAFT"
            
            template.updated_at = datetime.now(timezone.utc)
            self.db_session.commit()
            
            return template.to_dict(), True
            
        except Exception as e:
            logger.exception(f"Error updating draft: {e}")
            self.db_session.rollback()
            return {"error": str(e)}, False

    def delete_template(self, template_id: int) -> Tuple[Dict[str, Any], bool]:
        """
        Permanently delete a template from local DB and Meta (if linked).
        """
        try:
            template = WhatsAppTemplate.query.get(template_id)
            if not template:
                return {"error": "Template not found"}, False

            # Delete from Meta if linked and has ID
            # Meta API: DELETE /{template_id} or DELETE /{waba_id}/message_templates?name=name
            meta_success = True
            meta_error = None
            
            if template.meta_template_id or (template.meta_status and template.meta_status != 'DRAFT'):
                try:
                    account = WhatsAppAccount.query.get(template.account_id)
                    access_token = account.get_access_token() if account else os.getenv("WHATSAPP_ACCESS_TOKEN")
                    
                    # Meta API Deletion Strategy
                    # 1. Preferred: DELETE /{waba_id}/message_templates?name=...&hsm_id=...
                    # This targets the specific template ID while satisfying the name requirement
                    
                    if account and account.waba_id:
                        url = f"{WHATSAPP_API_BASE}/{self.api_version}/{account.waba_id}/message_templates"
                        params = {"name": template.name}
                        if template.meta_template_id:
                            params["hsm_id"] = template.meta_template_id
                            
                        logger.info(f"Deleting template from Meta: {url} params={params}")
                        resp = requests.delete(url, params=params, headers={"Authorization": f"Bearer {access_token}"})
                    elif template.meta_template_id:
                        # Fallback: DELETE /{template_id}
                        url = f"{WHATSAPP_API_BASE}/{self.api_version}/{template.meta_template_id}"
                        logger.info(f"Deleting template from Meta by ID: {url}")
                        resp = requests.delete(url, headers={"Authorization": f"Bearer {access_token}"})
                    else:
                        resp = None
                        
                    if resp:
                        if resp.status_code == 200 or resp.status_code == 204:
                            logger.info(f"Deleted template {template.name} from Meta")
                        else:
                            # Log full response for debugging
                            logger.warning(f"Failed to delete from Meta: SC={resp.status_code} Body={resp.text}")
                            meta_success = False
                            try:
                                meta_error = resp.json().get("error", {}).get("message", "Unknown Meta Error")
                            except:
                                meta_error = resp.text
                except Exception as e:
                    logger.error(f"Error deleting from Meta: {e}")
                    meta_success = False
                    meta_error = str(e)

            # Delete from local DB regardless of Meta success (force delete)
            # Or should we block? Usually users want it gone locally even if Meta fails.
            self.db_session.delete(template)
            self.db_session.commit()
            
            result = {"success": True}
            if not meta_success:
                result["meta_warning"] = f"Deleted locally but failed on Meta: {meta_error}"
                
            return result, True

        except Exception as e:
            logger.exception(f"Error deleting template: {e}")
            self.db_session.rollback()
            return {"error": str(e)}, False

    def archive_template(self, template_id: int) -> Tuple[Dict[str, Any], bool]:
        """
        Soft delete (archive) a template.
        """
        try:
            template = WhatsAppTemplate.query.get(template_id)
            if not template:
                return {"error": "Template not found"}, False
            
            template.is_archived = True
            template.archived_at = datetime.now(timezone.utc)
            template.local_status = "ARCHIVED"
            
            self.db_session.commit()
            return template.to_dict(), True
            
        except Exception as e:
            logger.exception(f"Error archiving template: {e}")
            return {"error": str(e)}, False

    def duplicate_template(self, template_id: int, new_name: Optional[str] = None) -> Tuple[Dict[str, Any], bool]:
        """
        Duplicate a template as a new DRAFT.
        """
        try:
            original = WhatsAppTemplate.query.get(template_id)
            if not original:
                return {"error": "Original template not found"}, False
            
            base_name = new_name or f"{original.name}_copy"
            # Ensure uniqueness
            count = 1
            final_name = base_name
            while WhatsAppTemplate.query.filter_by(account_id=original.account_id, name=final_name, language=original.language).first():
                final_name = f"{base_name}_{count}"
                count += 1
            
            new_template = WhatsAppTemplate(
                account_id=original.account_id,
                name=final_name,
                category=original.category,
                language=original.language,
                components=original.components, # Deep copy handled by DB insert usually, but be safe with JSON
                body_text=original.body_text,
                header_text=original.header_text,
                footer_text=original.footer_text,
                variable_count=original.variable_count,
                
                local_status="DRAFT",
                meta_status=None,
                status="DRAFT",
            )
            
            self.db_session.add(new_template)
            self.db_session.commit()
            
            return new_template.to_dict(), True
            
        except Exception as e:
            logger.exception(f"Error duplicating template: {e}")
            self.db_session.rollback()
            return {"error": str(e)}, False

    def _extract_text(self, data: Dict, type_str: str) -> str:
        """Helper to extract text from components list."""
        components = data.get("components", [])
        for comp in components:
            if comp.get("type") == type_str:
                return comp.get("text", "")
        return ""

    def _count_variables(self, text: str) -> int:
        """Helper to count {{x}} variables."""
        if not text: return 0
        import re
        matches = re.findall(r'\{\{([^}]+)\}\}', text)
        return len(set(matches))

    # ============================================================
    # Meta API Integration (Phase 3)
    # ============================================================

    def resumable_media_upload(self, file_path: str, mime_type: str, app_id: Optional[str] = None, access_token: Optional[str] = None) -> Optional[str]:
        """
        Perform a Resumable Upload to get a media handle (h).
        Steps:
        1. Create Upload Session (POST /{app_id}/uploads)
        2. Upload Binary Data (POST /{upload_id})
        3. Return handle 'h'
        """
        if not os.path.exists(file_path):
             logger.error(f"File not found for upload: {file_path}")
             return None

        # Config
        target_app_id = app_id or os.getenv("META_APP_ID") or os.getenv("FB_APP_ID")
        if not access_token:
            access_token = os.getenv("WHATSAPP_ACCESS_TOKEN")
        file_size = os.path.getsize(file_path)
        
        if not target_app_id or not access_token:
            logger.error("Missing App ID or Access Token for upload. META_APP_ID/FB_APP_ID=%s, token=%s", target_app_id, bool(access_token))
            return None

        # Step 1: Create Session
        session_url = f"{WHATSAPP_API_BASE}/v22.0/{target_app_id}/uploads"
        params = {
            "file_length": file_size,
            "file_type": mime_type,
            "access_token": access_token
        }
        
        upload_id = None
        try:
            resp = requests.post(session_url, params=params, timeout=10)
            data = resp.json()
            if "id" not in data:
                logger.error(f"Failed to create upload session: {data}")
                return None
            upload_id = data["id"]
        except Exception as e:
            logger.exception(f"Exception creating upload session: {e}")
            return None

        # Step 2: Upload Binary (with retries)
        upload_url = f"{WHATSAPP_API_BASE}/v22.0/{upload_id}"
        auth_header = {"Authorization": f"OAuth {access_token}"}
        # Note: 'file_offset' header is 0 for initial upload

        for attempt in range(3):
            try:
                with open(file_path, 'rb') as f:
                    # binary upload
                    headers = {
                        "Authorization": f"OAuth {access_token}",
                        "file_offset": "0"
                    }
                    resp = requests.post(upload_url, data=f, headers=headers, timeout=60) # Longer timeout for upload
                    result = resp.json()
                    
                    if "h" in result:
                        return result["h"]
                    else:
                        logger.warning(f"Upload attempt {attempt+1} failed: {result}")
                        
            except Exception as e:
                logger.warning(f"Upload attempt {attempt+1} exception: {e}")
                
        logger.error("All media upload attempts failed.")
        return None

    def submit_template_to_meta(self, template_id: int) -> Tuple[Dict[str, Any], bool]:
        """
        Submit a local draft to Meta API.
        Handles media keys automatically if file path is present in components (custom logic required in frontend/route to stage file).
        """
        try:
            template = WhatsAppTemplate.query.get(template_id)
            if not template:
                return {"error": "Template not found"}, False
            
            # 1. Prepare Payload
            # We need to construct the exact JSON Meta expects
            # AND handle potential media uploads if the HEADER has a file handle pending
            
            # NOTE: For this implementation, we assume the frontend or previous step has staged the file 
            # and stored the local path in a temp field or we check components for a local file placeholder.
            # Real-world: Use a specific 'example' structure or separate media staging table. 
            # Simplified: Check if header implies media and if we have a file handle.
            
            # For this phase, we assume template.components HAS the correct structure, 
            # possibly EXCEPT for the 'example' media handle which we might need to generate.
            
            # TODO: Logic to detect local file path in components -> upload -> replace with handle
            # This is complex. We will assume for now components are mostly ready or text-only.
            
            payload = {
                "name": template.name,
                "category": template.category,
                "components": template.components,
                "language": template.language,
                "allow_category_change": True
            }

            # 2. Get Account Credentials
            account = WhatsAppAccount.query.get(template.account_id)
            if not account or not account.waba_id:
                return {"error": "Invalid account WABA configuration"}, False
            
            access_token = account.get_access_token() or os.getenv("WHATSAPP_ACCESS_TOKEN")
            
            # 3. Post to Meta
            url = f"{WHATSAPP_API_BASE}/v22.0/{account.waba_id}/message_templates"
            resp = requests.post(url, json=payload, headers={"Authorization": f"Bearer {access_token}"}, timeout=20)
            data = resp.json()
            
            if "id" in data:
                # Success
                template.meta_template_id = data["id"]
                template.meta_status = data.get("status", "PENDING") # Usually PENDING immediately
                template.local_status = "SUBMITTED"
                template.submitted_at = datetime.now(timezone.utc)
                template.status = template.meta_status 
                
                self.db_session.commit()
                return template.to_dict(), True
            else:
                # API Error
                err_msg = data.get("error", {}).get("message", "Unknown Meta Error")
                # Don't update local status, keep as draft so user can fix
                return {"error": err_msg, "details": data}, False

        except Exception as e:
            logger.exception(f"Error submitting template: {e}")
            return {"error": str(e)}, False

    def edit_meta_template(self, template_id: int, remove_media: bool = False) -> Tuple[Dict[str, Any], bool]:
        """
        Update an EXISTING Meta template (Re-submission).
        """
        try:
            template = WhatsAppTemplate.query.get(template_id)
            if not template or not template.meta_template_id:
                return {"error": "Template not found or not linked to Meta"}, False
                
            # Payload similar to creation
            payload = {
                "components": template.components,
                "category": template.category
            }
            
            # Meta API for edit is POST /{template_id}
            # Note: Name and Language cannot be changed. Category can sometimes be changed? 
            # Official docs say Category change requires deletion/recreation usually, but edits to components are allowed.
            
            account = WhatsAppAccount.query.get(template.account_id)
            access_token = account.get_access_token() or os.getenv("WHATSAPP_ACCESS_TOKEN")
            
            url = f"{WHATSAPP_API_BASE}/v22.0/{template.meta_template_id}"
            resp = requests.post(url, json=payload, headers={"Authorization": f"Bearer {access_token}"}, timeout=20)
            data = resp.json()
            
            if "success" in data and data["success"]:
                # Success - Status usually resets to PENDING for review
                template.meta_status = "PENDING"
                template.local_status = "SUBMITTED"
                template.submitted_at = datetime.now(timezone.utc)
                template.status = "PENDING"
                
                self.db_session.commit()
                return template.to_dict(), True
            else:
                err_msg = data.get("error", {}).get("message", "Unknown Meta Update Error")
                return {"error": err_msg, "details": data}, False
                
        except Exception as e:
            logger.exception(f"Error editing Meta template: {e}")
            return {"error": str(e)}, False



# ============================================================
# Conversation Service
# ============================================================

class ConversationService:
    """
    Service for managing conversations.
    """
    
    def __init__(self, db_session=None):
        self.db_session = db_session or db.session
    
    def get_conversations(
        self,
        phone_number_id: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
        account_ids: Optional[List[int]] = None,
    ) -> List[Dict[str, Any]]:
        """
        Get list of conversations.
        
        Args:
            phone_number_id: Filter by phone number ID
            status: Filter by status (open, closed)
            limit: Max results
            offset: Pagination offset
            account_ids: Filter by account IDs (for workspace filtering)
            
        Returns:
            List of conversation dicts
        """
        query = WhatsAppConversation.query
        
        if phone_number_id:
            query = query.join(WhatsAppAccount).filter(
                WhatsAppAccount.phone_number_id == phone_number_id
            )
        
        if account_ids:
            query = query.filter(WhatsAppConversation.account_id.in_(account_ids))
        
        if status:
            query = query.filter(WhatsAppConversation.status == status)
        
        conversations = (
            query
            .order_by(WhatsAppConversation.last_message_at.desc().nullslast())
            .offset(offset)
            .limit(limit)
            .all()
        )

        # ── Deduplicate by last-10-digit phone key ──
        # Historical data may contain both "9999320932" and "919999320932"
        # for the same person.  Keep the one with the most recent message.
        seen_phones: Dict[str, WhatsAppConversation] = {}
        unique_convs: List[WhatsAppConversation] = []
        merge_ids: List[tuple] = []  # (winner_id, loser_id) for async merge

        for c in conversations:
            key = c.user_phone[-10:] if c.user_phone and len(c.user_phone) >= 10 else c.user_phone
            if key in seen_phones:
                # Keep whichever has the more recent message
                existing = seen_phones[key]
                existing_ts = existing.last_message_at or existing.created_at
                current_ts = c.last_message_at or c.created_at
                if current_ts and existing_ts and current_ts > existing_ts:
                    # Current is newer — swap
                    merge_ids.append((c.id, existing.id))
                    unique_convs = [c if x is existing else x for x in unique_convs]
                    seen_phones[key] = c
                else:
                    merge_ids.append((existing.id, c.id))
                continue
            seen_phones[key] = c
            unique_convs.append(c)

        # Lazy merge: migrate loser conversation phones so future queries are clean
        for winner_id, loser_id in merge_ids:
            try:
                winner = WhatsAppConversation.query.get(winner_id)
                loser = WhatsAppConversation.query.get(loser_id)
                if winner and loser:
                    canonical = _normalize_phone_util(winner.user_phone)
                    # Move messages from loser to winner
                    WhatsAppMessage.query.filter_by(conversation_id=loser.id).update(
                        {"conversation_id": winner.id}, synchronize_session=False
                    )
                    # Ensure winner has canonical phone
                    if winner.user_phone != canonical:
                        winner.user_phone = canonical
                    # Delete the duplicate conversation
                    self.db_session.delete(loser)
                    logger.info(f"[ConversationService] Merged duplicate conversation {loser.id} → {winner.id} (phone: {canonical})")
                self.db_session.commit()
            except Exception as e:
                self.db_session.rollback()
                # If merge failed due to UniqueViolation (winner already owns the phone),
                # just delete the loser conversation to stop the infinite error loop
                try:
                    loser = WhatsAppConversation.query.get(loser_id)
                    if loser:
                        # Move any remaining messages first
                        WhatsAppMessage.query.filter_by(conversation_id=loser.id).update(
                            {"conversation_id": winner_id}, synchronize_session=False
                        )
                        self.db_session.delete(loser)
                        self.db_session.commit()
                        logger.info(f"[ConversationService] Deleted duplicate conversation {loser_id} (winner: {winner_id})")
                    else:
                        logger.debug(f"[ConversationService] Duplicate conversation {loser_id} already deleted")
                except Exception as cleanup_err:
                    self.db_session.rollback()
                    logger.debug(f"[ConversationService] Cleanup of {loser_id} skipped: {cleanup_err}")

        return [c.to_dict() for c in unique_convs]
    
    def get_conversation(
        self,
        conversation_id: int,
        include_messages: bool = True,
        message_limit: int = 50,
    ) -> Optional[Dict[str, Any]]:
        """
        Get single conversation with messages.
        
        Args:
            conversation_id: Conversation ID
            include_messages: Whether to include messages
            message_limit: Max messages to include
            
        Returns:
            Conversation dict or None
        """
        conversation = WhatsAppConversation.query.get(conversation_id)
        
        if not conversation:
            return None
        
        return conversation.to_dict(
            include_messages=include_messages,
            message_limit=message_limit,
        )
    
    def get_messages(
        self,
        conversation_id: int,
        limit: int = 100,
        before_id: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """
        Get messages for a conversation.
        
        Args:
            conversation_id: Conversation ID
            limit: Max messages
            before_id: Get messages before this ID (for pagination)
            
        Returns:
            List of message dicts
        """
        query = WhatsAppMessage.query.filter_by(conversation_id=conversation_id)
        
        if before_id:
            query = query.filter(WhatsAppMessage.id < before_id)
        
        messages = (
            query
            .order_by(WhatsAppMessage.created_at.desc())
            .limit(limit)
            .all()
        )
        
        # Return oldest first
        return [m.to_dict() for m in reversed(messages)]
    
    def mark_conversation_read(self, conversation_id: int) -> bool:
        """
        Mark conversation as read.
        
        Args:
            conversation_id: Conversation ID
            
        Returns:
            True if successful
        """
        conversation = WhatsAppConversation.query.get(conversation_id)
        
        if not conversation:
            return False
        
        conversation.mark_read()
        self.db_session.commit()
        
        return True


# ============================================================
# Standalone Helper Functions (for backward compatibility)
# ============================================================

def send_text_message(to: str, text: str) -> Dict[str, Any]:
    """
    Send a text message (standalone function).
    
    Args:
        to: Recipient phone number
        text: Message text
        
    Returns:
        Response dict
    """
    service = WhatsAppService()
    return service.send_text(to, text)


def send_template_message(
    to: str,
    template_name: str,
    params: Optional[List] = None,
    language: str = "en",
) -> Dict[str, Any]:
    """
    Send a template message (standalone function).
    
    Args:
        to: Recipient phone number
        template_name: Template name
        params: Template parameters (will be converted to components)
        language: Language code
        
    Returns:
        Response dict
    """
    # Convert params to components format if provided
    components = None
    if params:
        components = [{
            "type": "body",
            "parameters": [{"type": "text", "text": str(p)} for p in params],
        }]
    
    service = WhatsAppService()
    return service.send_template(to, template_name, language, components)


def send_media_message(
    to: str,
    media_type: str,
    media_url: str,
    caption: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Send a media message (standalone function).
    
    Args:
        to: Recipient phone number
        media_type: Type of media (image, video, audio, document)
        media_url: URL of the media
        caption: Optional caption
        
    Returns:
        Response dict
    """
    service = WhatsAppService()
    return service._send_media(to, media_type, media_url, caption=caption)


def send_interactive_message(
    to: str,
    interactive_type: str,
    body_text: str,
    buttons_or_sections: List[Dict],
    **kwargs,
) -> Dict[str, Any]:
    """
    Send an interactive message (standalone function).
    
    Args:
        to: Recipient phone number
        interactive_type: "button" or "list"
        body_text: Message body text
        buttons_or_sections: Buttons (for button type) or sections (for list type)
        **kwargs: Additional options (header_text, footer_text, button_text)
        
    Returns:
        Response dict
    """
    service = WhatsAppService()
    
    if interactive_type == "button":
        return service.send_interactive_buttons(
            to=to,
            body_text=body_text,
            buttons=buttons_or_sections,
            header_text=kwargs.get("header_text"),
            footer_text=kwargs.get("footer_text"),
        )
    elif interactive_type == "list":
        return service.send_interactive_list(
            to=to,
            body_text=body_text,
            button_text=kwargs.get("button_text", "Options"),
            sections=buttons_or_sections,
            header_text=kwargs.get("header_text"),
            footer_text=kwargs.get("footer_text"),
        )
    else:
        return {"success": False, "error": f"Unknown interactive type: {interactive_type}"}

# ============================================================
    # Phase-2: OAuth Methods
    # ============================================================

    def get_oauth_url(self, workspace_id: str) -> str:
        """Generate the Meta Embedded Signup URL."""
        app_id = os.getenv("META_APP_ID") or os.getenv("FB_APP_ID")
        # Ensure base URL doesn't have trailing slash
        base_url = os.getenv("APP_BASE_URL", "https://sociovia-backend-362038465411.europe-west1.run.app").rstrip("/")
        redirect_uri = f"{base_url}/api/whatsapp/connect/callback"
        
        if not app_id:
            raise ValueError("META_APP_ID or FB_APP_ID environment variable not set")

        # Scopes required for BSP/Embedded Signup
        # business_management + catalog_management are required for product catalog APIs
        scopes = "whatsapp_business_management,whatsapp_business_messaging,business_management,catalog_management"
        
        return (
            f"https://www.facebook.com/v22.0/dialog/oauth?"
            f"client_id={app_id}&"
            f"redirect_uri={redirect_uri}&"
            f"state={workspace_id}&"
            f"scope={scopes}&"
            f"response_type=code"
        )

    def connect_account(self, code: str, workspace_id: str):
        """Exchange code for token and store account details."""
        # Resolve the tenant's Meta app (env fallback for T0000 / unconfigured).
        from tenant.integration import get_tenant_meta_config
        cfg = get_tenant_meta_config(workspace_id=workspace_id)
        app_id = cfg.app_id or os.getenv("META_APP_ID") or os.getenv("FB_APP_ID")
        app_secret = cfg.app_secret or os.getenv("META_APP_SECRET") or os.getenv("FB_APP_SECRET")
        base_url = os.getenv("APP_BASE_URL", "https://sociovia-backend-362038465411.europe-west1.run.app").rstrip("/")
        redirect_uri = f"{base_url}/api/whatsapp/connect/callback"

        # 1. Exchange code for User Access Token
        token_url = (
            f"https://graph.facebook.com/v22.0/oauth/access_token?"
            f"client_id={app_id}&"
            f"client_secret={app_secret}&"
            f"redirect_uri={redirect_uri}&"
            f"code={code}"
        )
        
        resp = requests.get(token_url)
        data = resp.json()
        
        if "error" in data:
            raise Exception(f"Token exchange failed: {data['error'].get('message')}")
            
        access_token = data["access_token"]
        # expires_in = data.get("expires_in") # Used for logic if needed

        # 2. Get WABA and Phone Number details
        # We query the debug_token endpoint or client_whatsapp_business_accounts
        # For embedded signup, usually we fetch the shared WABA.
        
        # 2a. Fetch WABA IDs accessible by this token
        waba_resp = requests.get(
            f"https://graph.facebook.com/v22.0/me/client_whatsapp_business_accounts",
            headers={"Authorization": f"Bearer {access_token}"}
        )
        waba_data = waba_resp.json()
        
        if "data" not in waba_data or not waba_data["data"]:
            # Fallback: try fetching 'accounts' usually used in standard OAuth
            logger.warning("No client_whatsapp_business_accounts found, trying standard accounts")
            # In real implementation, you might need to handle different granularities
            raise Exception("No WhatsApp Business Accounts found for this user.")

        # For simplicity, pick the first WABA found
        # In a robust app, you might show a UI to select one if multiple exist
        waba_info = waba_data["data"][0]
        waba_id = waba_info["id"]
        waba_name = waba_info.get("name", "Unknown WABA")

        # 3. Get Phone Numbers for this WABA
        phone_resp = requests.get(
            f"https://graph.facebook.com/v22.0/{waba_id}/phone_numbers",
            headers={"Authorization": f"Bearer {access_token}"}
        )
        phone_data = phone_resp.json()
        
        if "data" not in phone_data or not phone_data["data"]:
            raise Exception(f"No phone numbers found for WABA {waba_name}")

        # Pick first verified number
        phone_info = phone_data["data"][0]
        phone_id = phone_info["id"]
        display_number = phone_info.get("display_phone_number")
        quality_rating = phone_info.get("quality_rating")

        # 4. Store in Database — with cross-workspace guard
        from .connection_guard import check_phone_available
        conflict = check_phone_available(phone_id, workspace_id)
        if conflict:
            raise Exception(conflict["error"])
        
        existing = WhatsAppAccount.query.filter_by(
            phone_number_id=phone_id
        ).first()

        if existing:
            account = existing
            account.workspace_id = workspace_id  # Safe: guard passed above
            account.is_active = True
        else:
            account = WhatsAppAccount(phone_number_id=phone_id)
            self.db_session.add(account)

        # Update fields
        account.waba_id = waba_id
        account.workspace_id = workspace_id
        account.display_phone_number = display_number
        account.verified_name = waba_name
        account.quality_score = quality_rating
        
        # Save Encrypted Token
        account.set_access_token(access_token, token_type="permanent")
        account.last_synced_at = datetime.now(timezone.utc)
        
        self.db_session.commit()
        logger.info(f"Connected WhatsApp account {display_number} for workspace {workspace_id}")
        
        # ============================================================
        # AUTO-REGISTER PHONE NUMBER WITH WHATSAPP BUSINESS API
        # This is required before the phone can send/receive messages
        # ============================================================
        try:
            register_resp = requests.post(
                f"https://graph.facebook.com/v22.0/{phone_id}/register",
                json={
                    "messaging_product": "whatsapp",
                    "pin": "123456"  # Default 6-digit PIN for 2FA
                },
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json"
                },
                timeout=15
            )
            register_result = register_resp.json()
            if register_result.get("success"):
                logger.info(f"Phone number {phone_id} auto-registered with WhatsApp Business API")
            else:
                logger.warning(f"Phone registration response: {register_result}")
        except Exception as reg_error:
            # Don't fail if registration fails - phone might already be registered
            logger.warning(f"Phone registration warning (may already be registered): {reg_error}")
        
        return account