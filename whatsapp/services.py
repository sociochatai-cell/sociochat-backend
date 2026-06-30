"""
WhatsApp Services — Facade
===========================

This module is the backward-compatible entrypoint for all WhatsApp operations.

ARCHITECTURE (Post-Decomposition):
    WhatsAppService  ←  thin facade (this file)
        ├── WhatsAppMessagingService  (messaging_service.py)
        ├── WhatsAppAccountService   (account_service.py)
        └── WhatsAppTemplateService  (template_service.py)

All existing imports continue to work:
    from whatsapp.services import WhatsAppService           ✔
    from whatsapp.services import send_text_message          ✔
    from whatsapp.services import ConversationService        ✔

NEW code should prefer specific imports:
    from whatsapp.messaging_service import WhatsAppMessagingService
    from whatsapp.template_service import WhatsAppTemplateService
    from whatsapp.account_service import WhatsAppAccountService
"""

import os
import json
import logging
import time
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List

import requests
from sqlalchemy import and_, cast, func, or_
from sqlalchemy.dialects.postgresql import JSONB

from shared_models import db
from .models import (
    WhatsAppAccount,
    WhatsAppConversation,
    WhatsAppMessage,
    WhatsAppTemplate,
    update_account_heartbeat,
)
from .utils import normalize_phone as _normalize_phone_util
from notifications import notification_manager
from .usage_events import emit_message_sent_usage_event, broadcast_usage_event
from .debug_logger import wa_debug

# ── Sub-service imports (the decomposed pieces) ─────────────────────
from .messaging_service import WhatsAppMessagingService
from .account_service import WhatsAppAccountService
from .template_service import WhatsAppTemplateService

logger = logging.getLogger(__name__)

# WhatsApp Cloud API base URL
WHATSAPP_API_BASE = "https://graph.facebook.com"


# ============================================================
# Main Service — Facade via MRO
# ============================================================

class WhatsAppService(
    WhatsAppMessagingService,
    WhatsAppAccountService,
    WhatsAppTemplateService,
):
    """
    WhatsApp Cloud API service (facade).

    This class composes Messaging + Account + Template services via MRO.
    It owns:
      • __init__ (credential resolution)
      • Core infra: _send_api_request, _store_outgoing_message,
        _get_or_create_conversation, _normalize_phone, get_media_url
      • Properties: api_url, headers

    All messaging, template, and account methods are inherited from
    the dedicated sub-service mixins.
    """

    def __init__(
        self,
        db_session=None,
        phone_number_id: Optional[str] = None,
        access_token: Optional[str] = None,
        waba_id: Optional[str] = None,
        workspace_id: Optional[str] = None,
        account_id: Optional[int] = None,
    ):
        """
        Initialize WhatsApp service.

        Args:
            db_session: SQLAlchemy session (optional, uses default if not provided)
            phone_number_id: WhatsApp phone number ID (falls back to stored account or env var)
            access_token: Access token (falls back to stored account or env var)
            waba_id: WhatsApp Business Account ID (falls back to stored account or env var)
            workspace_id: Workspace ID to look up stored account (Phase-2)
            account_id: Optional account ID to query stored account directly
        """
        self.db_session = db_session or db.session
        self.api_version = os.getenv("WHATSAPP_API_VERSION", "v24.0")

        # Phase-2: Try to get stored account for workspace/id first
        stored_account = None
        if account_id:
            stored_account = WhatsAppAccount.query.get(account_id)

        if not stored_account:
            if workspace_id and not phone_number_id:
                stored_account = self._get_workspace_account(workspace_id)
            elif workspace_id and phone_number_id:
                # Look up the specific account by phone_number_id
                stored_account = WhatsAppAccount.query.filter_by(
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
            self.account_id = stored_account.id
        else:
            # Fall back to provided params or env vars
            self.phone_number_id = phone_number_id or os.getenv("WHATSAPP_PHONE_NUMBER_ID", "")
            self.access_token = access_token or os.getenv("WHATSAPP_ACCESS_TOKEN") or os.getenv("WHATSAPP_TEMP_TOKEN", "")
            self.waba_id = waba_id or os.getenv("WHATSAPP_WABA_ID", "")
            self.account_id = account_id

    # ============================================================
    # Properties
    # ============================================================

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
    # Core Infrastructure (shared by all sub-services)
    # ============================================================

    def _send_api_request(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        Send request to WhatsApp Cloud API.

        Includes rate limiting for coexistence accounts (5 MPS).
        """
        account = None
        try:
            account = WhatsAppAccount.query.filter_by(
                phone_number_id=self.phone_number_id,
                is_active=True,
            ).first()
        except Exception as e:
            logger.warning(f"Account lookup for capability gate failed: {e}")

        if account:
            try:
                from .capabilities import outbound_send_capability_check

                cap = outbound_send_capability_check(account.id, self.db_session, send_kind="unknown")
                if not cap.ok:
                    return {
                        "success": False,
                        "error": cap.message,
                        "error_code": "CAPABILITY_DENIED",
                    }
            except Exception as e:
                logger.warning(f"Capability check failed (non-fatal): {e}")

        # Rate limiting: coexistence MPS
        try:
            from .rate_limiter import get_rate_limiter

            if account:
                limiter = get_rate_limiter()
                mps = account.mps_limit or (5 if account.is_coexistence else 80)

                if not limiter.acquire(self.phone_number_id, mps):
                    # Wait and retry once
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

        trace_id = wa_debug.http_before(
            "POST",
            self.api_url,
            acct_id=getattr(account, "id", None) or self.account_id,
            pn_id=self.phone_number_id,
            waba_id=getattr(account, "waba_id", None) or self.waba_id,
            wp_id=getattr(account, "workspace_id", None) or getattr(self, "workspace_id", None),
            payload=payload,
        )
        started = time.perf_counter()

        try:
            logger.info(f"Sending WhatsApp API request to {self.api_url}")
            logger.debug(f"Payload: {json.dumps(payload, indent=2)}")

            response = requests.post(
                self.api_url,
                headers=self.headers,
                json=payload,
                timeout=30,
            )

            try:
                response_data = response.json()
            except ValueError:
                response_data = {"raw_body": (response.text or "")[:2000]}

            elapsed_ms = (time.perf_counter() - started) * 1000
            wa_debug.http_after(
                "POST",
                self.api_url,
                trace_id=trace_id,
                status_code=response.status_code,
                acct_id=getattr(account, "id", None) or self.account_id,
                pn_id=self.phone_number_id,
                waba_id=getattr(account, "waba_id", None) or self.waba_id,
                wp_id=getattr(account, "workspace_id", None) or getattr(self, "workspace_id", None),
                elapsed_ms=elapsed_ms,
                response_body=response_data,
            )
            logger.debug(f"Response: {json.dumps(response_data, indent=2)}")

            if response.status_code == 200:
                return {
                    "success": True,
                    "response": response_data,
                    "wamid": response_data.get("messages", [{}])[0].get("id"),
                }
            else:
                error = response_data.get("error", {})
                error_code = error.get("code")
                error_type = error.get("type", "")
                logger.error(f"WhatsApp API error: {error}")

                # Self-healing: Permission error (#200 OAuthException)
                # This means the token lacks whatsapp_business_messaging scope
                # or the WABA is not subscribed to our app.
                permission_hints = None
                if error_code == 200 and "OAuthException" in error_type and account:
                    logger.warning(
                        "⚠️ Permission error #200 detected for account %s — "
                        "attempting WABA re-subscription self-heal",
                        account.id,
                    )
                    try:
                        account.token_health = "permission_error"
                        account.token_health_checked_at = datetime.now(timezone.utc)
                        self.db_session.commit()
                    except Exception:
                        pass
                    # Attempt re-subscription (non-blocking)
                    try:
                        from .utils import subscribe_waba_to_app
                        sub_result = subscribe_waba_to_app(
                            account.waba_id, self.access_token
                        )
                        logger.info(
                            "Self-heal webhook re-subscription result: %s",
                            sub_result,
                        )
                    except Exception as heal_e:
                        logger.warning("Self-heal failed: %s", heal_e)
                    permission_hints = [
                        "This is not caused by the template image — upload succeeded, but Meta blocked the send.",
                        "Partner/client-owned WABAs (e.g. Trusthomes) need a System User token from Sociovia Business Manager, not Facebook Login.",
                        "In Sociovia: WhatsApp → Settings → reconnect with Manual Link / System User token.",
                        "In Trusthomes Business Manager → Partners → Sociovia: grant permission to send messages on behalf of the WABA.",
                    ]

                result = {
                    "success": False,
                    "error": error.get("message", "Unknown error"),
                    "error_code": error_code,
                    "response": response_data,
                }
                if error_code == 200:
                    result["error_category"] = "messaging_permission_denied"
                    result["remediation"] = (
                        "Your Meta access token cannot send messages for this WhatsApp Business Account. "
                        "Reconnect using a System User token with whatsapp_business_messaging for this WABA."
                    )
                    if permission_hints:
                        result["hints"] = permission_hints
                return result

        except requests.exceptions.Timeout:
            wa_debug.http_after(
                "POST",
                self.api_url,
                trace_id=trace_id,
                status_code=None,
                acct_id=getattr(account, "id", None) or self.account_id,
                pn_id=self.phone_number_id,
                elapsed_ms=(time.perf_counter() - started) * 1000,
                error="Request timeout",
            )
            logger.error("WhatsApp API timeout")
            return {"success": False, "error": "Request timeout"}
        except requests.exceptions.RequestException as e:
            wa_debug.http_after(
                "POST",
                self.api_url,
                trace_id=trace_id,
                status_code=None,
                acct_id=getattr(account, "id", None) or self.account_id,
                pn_id=self.phone_number_id,
                elapsed_ms=(time.perf_counter() - started) * 1000,
                error=str(e),
            )
            logger.exception(f"WhatsApp API request failed: {e}")
            return {"success": False, "error": str(e)}
        except Exception as e:
            wa_debug.http_after(
                "POST",
                self.api_url,
                trace_id=trace_id,
                status_code=None,
                acct_id=getattr(account, "id", None) or self.account_id,
                pn_id=self.phone_number_id,
                elapsed_ms=(time.perf_counter() - started) * 1000,
                error=str(e),
            )
            logger.exception(f"Unexpected error: {e}")
            return {"success": False, "error": str(e)}

    def get_media_url(self, media_id: str) -> Optional[str]:
        """
        Get temporary media URL from Meta given a media ID.
        """
        try:
            media_info = self.get_media_info(media_id)
            if media_info:
                return media_info.get("url")
            return None
        except Exception as e:
            logger.error(f"Error fetching media URL for {media_id}: {e}")
            return None

    def get_media_info(self, media_id: str) -> Optional[Dict[str, Any]]:
        """
        Get media metadata from Meta given a media ID.
        Returns full JSON payload (includes url, mime_type, file_size, etc.).
        """
        try:
            url = f"{WHATSAPP_API_BASE}/{self.api_version}/{media_id}"
            resp = requests.get(url, headers=self.headers, timeout=20)
            data = resp.json()

            if resp.status_code == 200:
                return data

            logger.error(f"Failed to get media info for {media_id}: {data}")
            return None
        except Exception as e:
            logger.error(f"Error fetching media info for {media_id}: {e}")
            return None

    def _get_or_create_conversation(
        self,
        user_phone: str,
        user_name: Optional[str] = None,
    ) -> WhatsAppConversation:
        """
        Get existing conversation or create a new one.
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
        commit: bool = True,
    ) -> WhatsAppMessage:
        """Store outgoing message in database."""
        usage_event = None
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
        self.db_session.flush()

        # Update conversation timestamp and outbound tracking
        conversation.last_message_at = datetime.now(timezone.utc)
        conversation.last_outbound_at = datetime.now(timezone.utc)

        acc = getattr(conversation, "account", None)
        if acc is None and conversation.account_id:
            acc = self.db_session.query(WhatsAppAccount).get(conversation.account_id)
        if acc:
            update_account_heartbeat(acc, db_session=self.db_session, outbound=True)

        usage_event = emit_message_sent_usage_event(
            db_session=self.db_session,
            account_id=conversation.account_id,
            message=message,
            conversation=conversation,
        )

        if commit:
            self.db_session.commit()
            broadcast_usage_event(usage_event)
        else:
            self.db_session.flush()
        return message

    @staticmethod
    def _normalize_phone(phone: str) -> str:
        """Normalize a phone number to digits-only E.164 for the Meta API.

        Prefers the phonenumbers-backed robust normalizer (validates + handles non-Indian
        numbers, Mexico/Argentina, country-code-embedded values), and falls back to the basic
        heuristic so a number that was already acceptable is never dropped at the send layer.
        """
        try:
            from .utils import normalize_phone_robust
            robust = normalize_phone_robust(phone)
            if robust:
                return robust
        except Exception:
            pass
        return _normalize_phone_util(phone)


# ============================================================
# Conversation Service (already clean — standalone class)
# ============================================================

def _attribution_json_true(column, key: str):
    """Truthy check for a boolean flag stored in attribution_data JSON/JSONB."""
    extracted = column.op("->>")(key)
    jsonb_col = cast(column, JSONB)
    return or_(
        jsonb_col.contains({key: True}),
        jsonb_col.contains({key: "true"}),
        extracted == "true",
        extracted == "True",
        extracted == "1",
    )


def _needs_reply_filter():
    """Unread inbound with no newer outbound (matches inbox UI)."""
    return and_(
        WhatsAppConversation.unread_count > 0,
        WhatsAppConversation.last_inbound_at.isnot(None),
        or_(
            WhatsAppConversation.last_outbound_at.is_(None),
            WhatsAppConversation.last_inbound_at > WhatsAppConversation.last_outbound_at,
        ),
    )


def _apply_inbox_category_filter(query, category: Optional[str]):
    """Filter conversations for inbox category tabs."""
    data = WhatsAppConversation.attribution_data

  # "all" / default — return every conversation (human_required shown with badge in UI)
    if not category or category == "all":
        return query

    if category == "unread":
        return query.filter(WhatsAppConversation.unread_count > 0)
    if category == "active":
        return query.filter(
            WhatsAppConversation.closed_by_agent.is_(False),
            WhatsAppConversation.session_expires_at.isnot(None),
            WhatsAppConversation.session_expires_at > func.now(),
        )
    if category == "expired":
        return query.filter(
            WhatsAppConversation.closed_by_agent.is_(False),
            or_(
                WhatsAppConversation.session_expires_at.is_(None),
                WhatsAppConversation.session_expires_at <= func.now(),
            ),
        )
    if category == "needs_reply":
        return query.filter(_needs_reply_filter())
    if category == "human_required":
        return query.filter(_attribution_json_true(data, "human_required"))
    if category == "opted_out":
        return query.filter(_attribution_json_true(data, "opted_out"))

    return query


class ConversationService:
    """
    Service for managing conversations.
    """

    def __init__(self, db_session=None):
        self.db_session = db_session or db.session

    def _base_conversation_query(
        self,
        phone_number_id: Optional[str] = None,
        status: Optional[str] = None,
        account_ids: Optional[List[int]] = None,
        search: Optional[str] = None,
    ):
        query = WhatsAppConversation.query

        if phone_number_id:
            query = query.join(WhatsAppAccount).filter(
                WhatsAppAccount.phone_number_id == phone_number_id
            )

        if account_ids:
            query = query.filter(WhatsAppConversation.account_id.in_(account_ids))

        if status:
            query = query.filter(WhatsAppConversation.status == status)

        if search:
            term = f"%{search.strip()}%"
            query = query.filter(
                or_(
                    WhatsAppConversation.user_phone.ilike(term),
                    WhatsAppConversation.user_name.ilike(term),
                )
            )

        return query

    def count_conversation_totals(
        self,
        phone_number_id: Optional[str] = None,
        status: Optional[str] = None,
        account_ids: Optional[List[int]] = None,
        search: Optional[str] = None,
    ) -> Dict[str, int]:
        """Count conversations per inbox filter category."""
        categories = (
            "all",
            "unread",
            "active",
            "expired",
            "needs_reply",
            "human_required",
            "opted_out",
        )
        totals: Dict[str, int] = {}
        for cat in categories:
            q = self._base_conversation_query(
                phone_number_id=phone_number_id,
                status=status,
                account_ids=account_ids,
                search=search,
            )
            q = _apply_inbox_category_filter(q, cat)
            totals[cat] = q.count()
        return totals

    def get_conversations(
        self,
        phone_number_id: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
        account_ids: Optional[List[int]] = None,
        category: Optional[str] = None,
        search: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Get list of conversations, optionally filtered by inbox category."""
        query = self._base_conversation_query(
            phone_number_id=phone_number_id,
            status=status,
            account_ids=account_ids,
            search=search,
        )
        query = _apply_inbox_category_filter(query, category)

        conversations = (
            query
            .order_by(WhatsAppConversation.last_message_at.desc().nullslast())
            .offset(offset)
            .limit(limit)
            .all()
        )

        if category == "human_required":
            conversations = [
                c
                for c in conversations
                if isinstance(c.attribution_data, dict)
                and bool(c.attribution_data.get("human_required"))
            ]

        # In-memory dedupe only — never run write-side merges during list (inbox must stay fast).
        seen_phones: Dict[str, WhatsAppConversation] = {}
        unique_convs: List[WhatsAppConversation] = []

        for c in conversations:
            key = c.user_phone[-10:] if c.user_phone and len(c.user_phone) >= 10 else c.user_phone
            if key in seen_phones:
                existing = seen_phones[key]
                existing_ts = existing.last_message_at or existing.created_at
                current_ts = c.last_message_at or c.created_at
                if current_ts and existing_ts and current_ts > existing_ts:
                    seen_phones[key] = c
                    unique_convs = [c if x is existing else x for x in unique_convs]
                continue
            seen_phones[key] = c
            unique_convs.append(c)

        return [c.to_dict() for c in unique_convs]

    def get_conversation(
        self,
        conversation_id: int,
        include_messages: bool = True,
        message_limit: int = 50,
    ) -> Optional[Dict[str, Any]]:
        """Get single conversation with messages."""
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
        """Get messages for a conversation."""
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
        result = []
        for message in reversed(messages):
            payload = message.to_dict()
            if message.type == "order" and isinstance(message.content, dict):
                items = message.content.get("product_items") or []
                order_block = message.content.get("order") if isinstance(message.content.get("order"), dict) else {}
                if not items:
                    items = order_block.get("product_items") or []
                def _order_item_needs_enrichment(item: dict) -> bool:
                    if not item.get("name") or not item.get("image_url"):
                        return True
                    image_url = str(item.get("image_url") or "")
                    if "drive.google.com" in image_url and "googleusercontent.com" not in image_url:
                        return True
                    return False

                needs_enrichment = not items or any(
                    isinstance(i, dict) and _order_item_needs_enrichment(i)
                    for i in items
                )
                if needs_enrichment:
                    try:
                        from .order_enrichment import enrich_order_content
                        conversation = message.conversation
                        account = conversation.account if conversation else None
                        if account:
                            enriched = enrich_order_content(dict(message.content), account)
                            if enriched.get("product_items"):
                                message.content = enriched
                                payload["content"] = enriched
                                try:
                                    self.db_session.commit()
                                except Exception:
                                    self.db_session.rollback()
                    except Exception:
                        pass
            result.append(payload)
        return result

    def mark_conversation_read(self, conversation_id: int) -> bool:
        """Mark conversation as read."""
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
    """Send a text message (standalone function)."""
    service = WhatsAppService()
    return service.send_text(to, text)


def send_template_message(
    to: str,
    template_name: str,
    params: Optional[List] = None,
    language: str = "en",
) -> Dict[str, Any]:
    """Send a template message (standalone function)."""
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
    """Send a media message (standalone function)."""
    service = WhatsAppService()
    return service._send_media(to, media_type, media_url, caption=caption)


def send_interactive_message(
    to: str,
    interactive_type: str,
    body_text: str,
    buttons_or_sections: List[Dict],
    **kwargs,
) -> Dict[str, Any]:
    """Send an interactive message (standalone function)."""
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