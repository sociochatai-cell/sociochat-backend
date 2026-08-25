"""
WhatsApp Models - Phase 1
=========================

SQLAlchemy models for WhatsApp data persistence.

Tables:
    - WhatsAppAccount         → Connected WhatsApp Business accounts
    - WhatsAppConversation    → Chat conversations with users
    - WhatsAppMessage         → Individual messages (sent/received)
    - WhatsAppWebhookLog      → Raw webhook payloads for debugging
"""

from datetime import datetime, timezone
from typing import Optional, Dict, Any
import enum
import json
import logging
import uuid

from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import Enum, Text, JSON, UniqueConstraint, Index, BigInteger, Numeric
from sqlalchemy.dialects.postgresql import JSONB, UUID

# Import db from main models to share the same instance
from shared_models import db


logger = logging.getLogger(__name__)


def _coerce_utc_expiry(expires_at: Optional[datetime]) -> Optional[datetime]:
    if expires_at is None:
        return None
    if expires_at.tzinfo is None:
        return expires_at.replace(tzinfo=timezone.utc)
    return expires_at


def _log_wa_token_decrypt_failed(
    account_id: Optional[int],
    workspace_id: Optional[str],
    token_source: str,
    exc: BaseException,
) -> None:
    """Decrypt failures only — never log ciphertext or plaintext."""
    logger.warning(
        "WhatsApp access token decrypt failed",
        extra={
            "wa_event": "wa_token_decrypt_failed",
            "account_id": account_id,
            "workspace_id": workspace_id,
            "token_source": token_source,
            "exc_type": type(exc).__name__,
        },
    )


def _log_wa_token_not_stored(account_id: Optional[int], workspace_id: Optional[str]) -> None:
    logger.debug(
        "WhatsApp account has no stored access token ciphertext",
        extra={
            "wa_event": "wa_token_not_stored",
            "account_id": account_id,
            "workspace_id": workspace_id,
        },
    )


def _log_wa_token_stored_expiry_elapsed(
    account_id: Optional[int],
    workspace_id: Optional[str],
    token_source: str,
) -> None:
    """Advisory only: row expiry metadata vs Meta invalidity (handled elsewhere)."""
    logger.info(
        "WhatsApp stored token past token_expires_at (advisory)",
        extra={
            "wa_event": "wa_token_stored_expiry_elapsed",
            "account_id": account_id,
            "workspace_id": workspace_id,
            "token_source": token_source,
        },
    )


def _wa_has_stored_ciphertext(value) -> bool:
    """True if DB holds non-empty token ciphertext (ignores whitespace-only)."""
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    return bool(value)


def _log_wa_token_decrypted_empty(account_id: Optional[int], workspace_id: Optional[str], token_source: str) -> None:
    logger.debug(
        "WhatsApp token decrypted to empty string",
        extra={
            "wa_event": "wa_token_decrypted_empty",
            "account_id": account_id,
            "workspace_id": workspace_id,
            "token_source": token_source,
        },
    )


def _log_wa_token_credential_shadow_healed(account_id: Optional[int], workspace_id: Optional[str]) -> None:
    logger.debug(
        "whatsapp_credentials shadow row synchronized from whatsapp_accounts token column",
        extra={
            "wa_event": "wa_token_credential_shadow_healed",
            "account_id": account_id,
            "workspace_id": workspace_id,
        },
    )

class ConversationStatus(enum.Enum):
    """Conversation status states."""
    OPEN = "open"
    CLOSED = "closed"


class MessageDirection(enum.Enum):
    """Message direction."""
    INCOMING = "incoming"
    OUTGOING = "outgoing"
    ECHO = "echo"  # Messages sent from common mobile app


class MessageType(enum.Enum):
    """WhatsApp message types."""
    TEXT = "text"
    TEMPLATE = "template"
    IMAGE = "image"
    VIDEO = "video"
    AUDIO = "audio"
    DOCUMENT = "document"
    STICKER = "sticker"
    LOCATION = "location"
    CONTACTS = "contacts"
    INTERACTIVE = "interactive"
    REACTION = "reaction"
    UNKNOWN = "unknown"


class MessageStatus(enum.Enum):
    """Message delivery status."""
    PENDING = "pending"
    SENT = "sent"
    DELIVERED = "delivered"
    READ = "read"
    FAILED = "failed"


class TemplateStatus(enum.Enum):
    """WhatsApp template approval status from Meta."""
    APPROVED = "APPROVED"
    PENDING = "PENDING"
    REJECTED = "REJECTED"
    PAUSED = "PAUSED"
    DISABLED = "DISABLED"


class FlowStatus(enum.Enum):
    """WhatsApp Flow lifecycle status."""
    DRAFT = "DRAFT"              # Editable, not yet published
    PUBLISHED = "PUBLISHED"      # Immutable, selectable in templates
    DEPRECATED = "DEPRECATED"    # Read-only, replaced by newer version


class FlowCategory(enum.Enum):
    """WhatsApp Flow category types."""
    LEAD_GEN = "LEAD_GEN"
    SURVEY = "SURVEY"
    BOOKING = "BOOKING"
    FEEDBACK = "FEEDBACK"
    CUSTOM = "CUSTOM"


# ============================================================
# TABLE: WhatsApp Templates (synced from Meta)
# ============================================================

class WhatsAppTemplate(db.Model):
    """
    WhatsApp message templates synced from Meta API.
    
    Templates must be approved by Meta before they can be sent.
    """
    __tablename__ = "whatsapp_templates"
    __table_args__ = (
        UniqueConstraint("account_id", "name", "language", name="uq_template_name_lang"),
        Index("ix_whatsapp_templates_account", "account_id"),
        Index("ix_whatsapp_templates_status", "status"),
        {"extend_existing": True},
    )

    id = db.Column(db.Integer, primary_key=True)
    account_id = db.Column(db.Integer, db.ForeignKey("whatsapp_accounts.id"), nullable=False)
    meta_template_id = db.Column(db.String(64), nullable=True, index=True)  # ID from Meta API
    name = db.Column(db.String(512), nullable=False, index=True)  # Template name (e.g., "order_confirmation")
    category = db.Column(db.String(32), nullable=False)  # UTILITY, MARKETING, AUTHENTICATION
    language = db.Column(db.String(16), nullable=False, default="en_US")  # Template language code
    
    # Dual-State Status (Phase 2 Refinement)
    status = db.Column(db.String(32), default="PENDING", nullable=False)  # DEPRECATED: Use meta_status
    
    # Internal status (Sociovia lifecycle)
    local_status = db.Column(db.String(32), default="DRAFT", nullable=False)  # DRAFT, SUBMITTED, ARCHIVED
    
    # External status (Meta lifecycle)
    meta_status = db.Column(db.String(32), nullable=True)  # APPROVED, REJECTED, PENDING, PAUSED, DISABLED
    
    # Soft deletion
    is_archived = db.Column(db.Boolean, default=False, nullable=False)
    archived_at = db.Column(db.DateTime, nullable=True)
    rejection_reason = db.Column(db.Text, nullable=True)  # Reason if rejected by Meta
    quality_score = db.Column(db.String(32), nullable=True)  # Template quality: GREEN, YELLOW, RED
    
    # Template components (header, body, footer, buttons) as JSON
    components = db.Column(JSON, nullable=True)
    
    # Parsed body text for quick access
    body_text = db.Column(db.Text, nullable=True)
    header_text = db.Column(db.String(512), nullable=True)
    footer_text = db.Column(db.String(256), nullable=True)
    
    # Variable count for UI hints
    variable_count = db.Column(db.Integer, default=0)
    
    # ============================================================
    # NEW: Approval Acceleration Fields
    # ============================================================
    
    # Confidence tracking
    confidence_initial = db.Column(db.Integer, nullable=True)  # Pre-submit score (0-100)
    confidence_post_submit = db.Column(db.Integer, nullable=True)  # After PENDING response
    
    # Timing analytics
    submitted_at = db.Column(db.DateTime, nullable=True)  # When submitted to Meta
    approved_at = db.Column(db.DateTime, nullable=True)  # When approved by Meta
    approval_duration_seconds = db.Column(db.Integer, nullable=True)  # Time to approval
    
    # Outcome tracking
    approval_outcome_reason = db.Column(db.String(64), nullable=True)  # AUTOMATED_PASS, PROMO_LANGUAGE, etc.
    validation_flags = db.Column(JSON, nullable=True)  # Risk flags at submission
    detected_intent = db.Column(db.String(32), nullable=True)  # UTILITY, MARKETING, AUTHENTICATION
    approval_path = db.Column(db.String(32), nullable=True)  # AUTOMATED_FAST, AUTOMATED_SLOW, etc.
    
    # ============================================================
    
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))
    last_synced_at = db.Column(db.DateTime, nullable=True)
    
    # Relationship
    account = db.relationship("WhatsAppAccount", backref=db.backref("templates", lazy="dynamic", passive_deletes=True))
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for API response."""
        return {
            "id": self.id,
            "account_id": self.account_id,
            "meta_template_id": self.meta_template_id,
            "name": self.name,
            "category": self.category,
            "language": self.language,
            "status": self.status,
            "rejection_reason": self.rejection_reason,
            "quality_score": self.quality_score,
            "components": self.components,
            "body_text": self.body_text,
            "header_text": self.header_text,
            "footer_text": self.footer_text,
            "variable_count": self.variable_count,
            # Approval acceleration fields
            "confidence_initial": self.confidence_initial,
            "confidence_post_submit": self.confidence_post_submit,
            "submitted_at": self.submitted_at.isoformat() + "Z" if self.submitted_at else None,
            "approved_at": self.approved_at.isoformat() + "Z" if self.approved_at else None,
            "approval_duration_seconds": self.approval_duration_seconds,
            "approval_outcome_reason": self.approval_outcome_reason,
            "validation_flags": self.validation_flags,
            "detected_intent": self.detected_intent,
            "approval_path": self.approval_path,
            # Timestamps
            "created_at": self.created_at.isoformat() + "Z" if self.created_at else None,
            "updated_at": self.updated_at.isoformat() + "Z" if self.updated_at else None,
            "last_synced_at": self.last_synced_at.isoformat() + "Z" if self.last_synced_at else None,
            "variable_mapping": self.get_variable_mapping(),
            "is_archived": self.is_archived,
        }

    def get_variable_mapping(self) -> Dict[str, str]:
        """
        Get mapping of position -> variable name.
        e.g. {"1": "name", "2": "order_id"}
        """
        mapping = {}
        
        # 1. Try to get from components example (most reliable for complex templates)
        try:
            if self.components:
                for comp in self.components:
                    if comp.get("type", "").upper() == "BODY":
                        example = comp.get("example", {})
                        named_params = example.get("body_text_named_params", [])
                        if named_params:
                            for idx, param in enumerate(named_params):
                                mapping[str(idx + 1)] = param.get("param_name", "")
                            return mapping
        except Exception:
            pass
            
        # 2. Fallback: Parse body_text for {{name}} pattern
        if self.body_text:
            import re
            # Find all {{...}} patterns
            matches = re.findall(r'\{\{([^}]+)\}\}', self.body_text)
            
            # Filter and deduplicate while preserving order
            unique_vars = []
            seen = set()
            for m in matches:
                clean_m = m.strip()
                # fast skip for numeric positional params
                if clean_m.isdigit(): 
                    continue
                if clean_m not in seen:
                    unique_vars.append(clean_m)
                    seen.add(clean_m)
            
            # Map position to name
            for idx, var_name in enumerate(unique_vars):
                mapping[str(idx + 1)] = var_name
                
        return mapping
    
    @classmethod
    def from_meta_template(cls, account_id: int, meta_data: Dict[str, Any]) -> "WhatsAppTemplate":
        """Create or update template from Meta API response."""
        # Extract body text and variable count
        body_text = ""
        header_text = ""
        footer_text = ""
        variable_count = 0
        
        components = meta_data.get("components", [])
        for comp in components:
            comp_type = comp.get("type", "").upper()
            if comp_type == "BODY":
                body_text = comp.get("text", "")
                # Count ALL variables: both {{1}}, {{2}} and {{name}}, {{order_id}}
                import re
                all_vars = re.findall(r'\{\{([^}]+)\}\}', body_text)
                variable_count = len(set(all_vars))
            elif comp_type == "HEADER":
                header_text = comp.get("text", "")
            elif comp_type == "FOOTER":
                footer_text = comp.get("text", "")
        
        return cls(
            account_id=account_id,
            meta_template_id=meta_data.get("id"),
            name=meta_data.get("name", ""),
            category=meta_data.get("category", "UTILITY"),
            language=meta_data.get("language", "en_US"),
            status=meta_data.get("status", "PENDING"),
            local_status="SUBMITTED", # If coming from Meta, it's submitted
            meta_status=meta_data.get("status", "PENDING"),
            rejection_reason=meta_data.get("rejected_reason"),
            components=components,
            body_text=body_text,
            header_text=header_text,
            footer_text=footer_text,
            variable_count=variable_count,
            last_synced_at=datetime.now(timezone.utc),
        )

# ============================================================
# TABLE 1: WhatsApp Accounts
# ============================================================

class WhatsAppAccount(db.Model):
    """
    Connected WhatsApp Business accounts.
    
    Maps to existing workspace system for multi-tenant support.
    """
    __tablename__ = "whatsapp_accounts"
    __table_args__ = (
        UniqueConstraint("waba_id", "phone_number_id", name="uq_waba_phone"),
        Index("ix_whatsapp_accounts_workspace", "workspace_id"),
        {"extend_existing": True},
    )

    id = db.Column(db.Integer, primary_key=True)
    workspace_id = db.Column(db.String(255), nullable=True, index=True)  # maps to existing workspace system
    waba_id = db.Column(db.String(64), nullable=False, index=True)  # WhatsApp Business Account ID
    phone_number_id = db.Column(db.String(64), nullable=False, unique=True)  # Phone Number ID from Meta
    display_phone_number = db.Column(db.String(32), nullable=True)  # Formatted phone number for display
    verified_name = db.Column(db.String(255), nullable=True)  # Business name from Meta (can be overwritten on re-link)
    custom_name = db.Column(db.String(128), nullable=True)  # User-defined name (never overwritten by Meta)
    quality_score = db.Column(db.String(32), nullable=True)  # GREEN, YELLOW, RED
    messaging_limit = db.Column(db.Integer, nullable=True)  # 1K, 10K, 100K, UNLIMITED
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    
    # Phase-2: Token storage (encrypted)
    access_token_encrypted = db.Column(db.Text, nullable=True)  # Encrypted access token
    token_type = db.Column(db.String(16), default="temporary", nullable=False)  # permanent or temporary
    token_expires_at = db.Column(db.DateTime, nullable=True)  # Expiration for temporary tokens
    connected_by_user_id = db.Column(db.String(64), nullable=True, index=True)  # User who connected this account
    last_synced_at = db.Column(db.DateTime, nullable=True)  # Last time account data was synced from Meta
    
    # Phase-3: Flow encryption keys for dynamic flows
    flow_private_key = db.Column(db.Text, nullable=True)  # RSA private key (PEM format, encrypted)
    flow_public_key = db.Column(db.Text, nullable=True)   # RSA public key (PEM format, uploaded to Meta)
    
    # Notification settings
    notification_phone_number = db.Column(db.String(32), nullable=True)  # Phone number to receive app notifications (e.g., waitlist alerts)
    notification_email = db.Column(db.String(255), nullable=True)  # Email for human-required / AI escalation alerts
    
    # ============================================================
    # Coexistence Fields
    # ============================================================
    is_coexistence = db.Column(db.Boolean, default=False, nullable=False)  # True if connected via coexistence (mobile app stays active)
    mps_limit = db.Column(db.Integer, default=80, nullable=False)  # Messages per second limit (5 for coexistence, 80-1000 for standard)
    meta_business_id = db.Column(db.String(64), nullable=True)  # Meta Business Manager ID
    sync_status = db.Column(db.String(32), default="idle", nullable=False)  # idle, syncing, synced, error
    last_echo_at = db.Column(db.DateTime, nullable=True)  # Last time a mobile echo was received (device activity)
    coexistence_paired_at = db.Column(db.DateTime, nullable=True)  # When QR handshake completed
    history_sync_completed = db.Column(db.Boolean, default=False, nullable=False)  # Whether 180-day history sync is done

    # Bot / AI settings slice (legacy columns + whatsapp_bot_settings; shadow-written from microservice)
    ai_enabled = db.Column(db.Boolean, nullable=True)
    # NEW advanced AI-agent brain toggle (Gemini function-calling). SEPARATE from
    # ai_enabled (which turns the bot on/off). Default off → the bot uses the legacy
    # pure-RAG path; ON → agent brain with tools. Auto-added by WHATSAPP_ORM_COLUMN_SYNC.
    ai_agent_mode = db.Column(db.Boolean, nullable=True, default=False)
    # PHASE 6 guardrails (all auto-added by WHATSAPP_ORM_COLUMN_SYNC):
    #  - ai_agent_disabled_tools: comma-separated tool names the agent may NOT use
    #    for this workspace (e.g. "request_payment,send_template"). Empty/None = all on.
    #  - ai_agent_max_payment: hard ceiling (INR) for a single request_payment link;
    #    None = fall back to the global env cap.
    ai_agent_disabled_tools = db.Column(db.Text, nullable=True)
    ai_agent_max_payment = db.Column(db.Integer, nullable=True)
    ai_model = db.Column(db.String(50), nullable=True)
    temperature = db.Column(db.Float, nullable=True)
    max_tokens = db.Column(db.Integer, nullable=True)
    prompt_override = db.Column(db.Text, nullable=True)
    knowledge_base_id = db.Column(db.String(100), nullable=True)

    # Account status / heartbeat slice (legacy mirror + whatsapp_account_status)
    connection_status = db.Column(db.String(50), nullable=True)
    is_verified = db.Column(db.Boolean, nullable=True)
    last_webhook_received = db.Column(db.DateTime, nullable=True)
    last_outbound_sent = db.Column(db.DateTime, nullable=True)
    last_error_message = db.Column(db.Text, nullable=True)
    last_error_code = db.Column(db.Integer, nullable=True)

    # Platform V2: lifecycle, warmup, operational / safe mode, Meta restrictions (advisory-first)
    onboarding_lifecycle_state = db.Column(db.String(32), nullable=False, default="active")
    warmup_started_at = db.Column(db.DateTime, nullable=True)
    warmup_ends_at = db.Column(db.DateTime, nullable=True)
    warmup_config = db.Column(JSONB, nullable=True)

    operational_mode = db.Column(db.String(24), nullable=False, default="normal")
    safe_mode_advisory_only = db.Column(db.Boolean, nullable=False, default=True)
    safe_mode_reason = db.Column(db.Text, nullable=True)
    safe_mode_reason_code = db.Column(db.String(64), nullable=True)
    safe_mode_override_expires_at = db.Column(db.DateTime, nullable=True)

    restriction_state = db.Column(db.String(40), nullable=False, default="none")
    restriction_reason = db.Column(db.Text, nullable=True)
    restriction_detected_at = db.Column(db.DateTime, nullable=True)
    restriction_source = db.Column(db.String(64), nullable=True)

    webhook_subscription_status = db.Column(db.String(32), nullable=True)
    webhook_last_success_at = db.Column(db.DateTime, nullable=True)
    webhook_last_failure_at = db.Column(db.DateTime, nullable=True)
    webhook_last_error = db.Column(db.Text, nullable=True)
    webhook_health = db.Column(db.String(24), nullable=False, default="unknown")
    webhook_last_checked_at = db.Column(db.DateTime, nullable=True)
    webhook_cooldown_ends_at = db.Column(db.DateTime, nullable=True)
    webhook_subscribed_app_verified = db.Column(db.Boolean, nullable=True)
    last_inbound_webhook_at = db.Column(db.DateTime, nullable=True)
    webhook_echo_validation = db.Column(JSONB, nullable=True)

    webhook_last_validated_at = db.Column(db.DateTime, nullable=True)
    webhook_last_event_at = db.Column(db.DateTime, nullable=True)
    webhook_failure_reason = db.Column(db.Text, nullable=True)
    webhook_failure_count = db.Column(db.Integer, nullable=False, default=0)

    token_health = db.Column(db.String(24), nullable=False, default="unknown")
    token_health_checked_at = db.Column(db.DateTime, nullable=True)
    token_health_detail = db.Column(JSONB, nullable=True)

    embedded_signup_version = db.Column(db.String(32), nullable=True)
    graph_version_used = db.Column(db.String(16), nullable=True)
    fb_sdk_version = db.Column(db.String(32), nullable=True)
    whatsapp_config_id_used = db.Column(db.String(64), nullable=True)

    message_health_metrics = db.Column(JSONB, nullable=True)
    operational_metrics = db.Column(JSONB, nullable=True) # Tracks retry budgets, degradation counts, noise scoring

    trust_score = db.Column(Numeric(6, 2), nullable=True)
    trust_score_computed_at = db.Column(db.DateTime, nullable=True)

    ownership_model_version = db.Column(db.String(32), nullable=False, default="customer_waba_v1")

    # Partner integrations: forward inbound Meta webhooks to customer URL (Vaish / workspace 113)
    customer_webhook_url = db.Column(db.String(512), nullable=True)
    customer_webhook_secret_header = db.Column(db.String(128), nullable=True)
    customer_webhook_secret_value = db.Column(db.String(512), nullable=True)
    
    # Metadata
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    # Relationships
    conversations = db.relationship("WhatsAppConversation", back_populates="account", lazy="dynamic", passive_deletes=True)
    credential_record = db.relationship(
        "WhatsAppCredential",
        back_populates="account",
        uselist=False,
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    bot_settings = db.relationship(
        "WhatsAppBotSettings",
        back_populates="account",
        uselist=False,
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    status_record = db.relationship(
        "WhatsAppAccountStatus",
        back_populates="account",
        uselist=False,
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    capability_record = db.relationship(
        "WhatsAppAccountCapabilities",
        back_populates="account",
        uselist=False,
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    onboarding_sessions = db.relationship(
        "OnboardingSession",
        back_populates="account",
        lazy="dynamic",
        passive_deletes=True,
    )
    trust_snapshots = db.relationship(
        "TrustSnapshot",
        back_populates="account",
        lazy="dynamic",
        passive_deletes=True,
    )
    phone_reputation_snapshots = db.relationship(
        "WhatsAppPhoneReputationSnapshot",
        back_populates="account",
        lazy="dynamic",
        passive_deletes=True,
    )

    def __repr__(self):
        return f"<WhatsAppAccount {self.phone_number_id} ({self.display_phone_number})>"

    def to_dict(self, include_token: bool = False) -> Dict[str, Any]:
        """
        Convert account to dictionary.
        
        Args:
            include_token: If True, include decrypted token (for internal use only)
        """
        result = {
            "id": self.id,
            "workspace_id": self.workspace_id,
            "waba_id": self.waba_id,
            "phone_number_id": self.phone_number_id,
            "display_phone_number": self.display_phone_number,
            "verified_name": self.custom_name or self.verified_name,  # Prefer custom_name if set
            "custom_name": self.custom_name,
            "original_verified_name": self.verified_name,  # Keep original Meta name available
            "messaging_limit": self.messaging_limit,
            "is_active": self.is_active,
            "token_type": self.token_type,
            "token_expires_at": self.token_expires_at.isoformat() if self.token_expires_at else None,
            "connected_by_user_id": self.connected_by_user_id,
            "last_synced_at": self.last_synced_at.isoformat() if self.last_synced_at else None,
            # Coexistence fields
            "is_coexistence": self.is_coexistence,
            "mps_limit": self.mps_limit,
            "meta_business_id": self.meta_business_id,
            "sync_status": self.sync_status,
            "last_echo_at": self.last_echo_at.isoformat() if self.last_echo_at else None,
            "coexistence_paired_at": self.coexistence_paired_at.isoformat() if self.coexistence_paired_at else None,
            "history_sync_completed": self.history_sync_completed,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
        sr = getattr(self, "status_record", None)
        eff_quality = (sr.quality_rating if sr and sr.quality_rating else None) or self.quality_score
        result["quality_score"] = eff_quality
        conn = (sr.status if sr and sr.status else None) or self.connection_status
        if conn:
            result["connection_status"] = conn
        else:
            result["connection_status"] = "connected"
        lwr = (sr.last_webhook_received if sr and sr.last_webhook_received else None) or self.last_webhook_received
        if lwr:
            result["last_webhook_received"] = lwr.isoformat() if hasattr(lwr, "isoformat") else str(lwr)
        los = (sr.last_outbound_sent if sr and sr.last_outbound_sent else None) or self.last_outbound_sent
        if los:
            result["last_outbound_sent"] = los.isoformat() if hasattr(los, "isoformat") else str(los)
        result["is_verified"] = bool(sr.is_verified) if sr else (bool(self.is_verified) if self.is_verified is not None else False)

        result["onboarding_lifecycle_state"] = self.onboarding_lifecycle_state
        result["operational_mode"] = self.operational_mode
        result["safe_mode_advisory_only"] = bool(self.safe_mode_advisory_only)
        result["safe_mode_reason"] = self.safe_mode_reason
        result["safe_mode_reason_code"] = self.safe_mode_reason_code
        result["safe_mode_override_expires_at"] = self.safe_mode_override_expires_at.isoformat() if self.safe_mode_override_expires_at else None
        result["restriction_state"] = self.restriction_state
        result["restriction_reason"] = self.restriction_reason
        result["restriction_detected_at"] = (
            self.restriction_detected_at.isoformat() if self.restriction_detected_at else None
        )
        result["restriction_source"] = self.restriction_source
        result["webhook_health"] = self.webhook_health
        result["webhook_subscription_status"] = self.webhook_subscription_status
        result["webhook_last_checked_at"] = (
            self.webhook_last_checked_at.isoformat() if self.webhook_last_checked_at else None
        )
        result["webhook_cooldown_ends_at"] = (
            self.webhook_cooldown_ends_at.isoformat() if self.webhook_cooldown_ends_at else None
        )
        result["last_inbound_webhook_at"] = (
            self.last_inbound_webhook_at.isoformat() if self.last_inbound_webhook_at else None
        )
        result["warmup_started_at"] = self.warmup_started_at.isoformat() if self.warmup_started_at else None
        result["warmup_ends_at"] = self.warmup_ends_at.isoformat() if self.warmup_ends_at else None
        result["warmup_config"] = self.warmup_config if isinstance(self.warmup_config, dict) else None
        result["ownership_model_version"] = self.ownership_model_version
        result["trust_score"] = float(self.trust_score) if self.trust_score is not None else None
        result["trust_score_computed_at"] = (
            self.trust_score_computed_at.isoformat() if self.trust_score_computed_at else None
        )
        result["message_health_metrics"] = self.message_health_metrics
        result["operational_metrics"] = self.operational_metrics
        result["webhook_echo_validation"] = self.webhook_echo_validation
        result["webhook_last_validated_at"] = (
            self.webhook_last_validated_at.isoformat() if self.webhook_last_validated_at else None
        )
        result["webhook_last_event_at"] = (
            self.webhook_last_event_at.isoformat() if self.webhook_last_event_at else None
        )
        result["webhook_failure_reason"] = self.webhook_failure_reason
        result["webhook_failure_count"] = int(self.webhook_failure_count or 0)
        
        # Only include token for internal backend use, never expose to frontend
        if include_token:
            tok = self.get_access_token()
            if tok:
                result["access_token"] = tok
        
        return result
    
    def get_access_token(self) -> Optional[str]:
        """
        Decrypt Meta access token: prefer ``whatsapp_credentials`` (vault), then legacy
        ``whatsapp_accounts.access_token_encrypted``.

        If the vault row exists but is empty, corrupt, decrypts to empty, or mismatches the
        legacy column (common after introducing ``whatsapp_credentials`` without a full
        backfill), we fall back to legacy and best-effort **shadow-heal** the vault ciphertext
        via ``_upsert_credential_shadow`` + ``flush()`` only (no implicit commit).
        """
        from .encryption import decrypt_token

        rec = None
        try:
            rec = getattr(self, "credential_record", None)
            if rec is None and self.id:
                rec = WhatsAppCredential.query.filter_by(account_id=self.id).first()
        except Exception:
            rec = None

        aid = self.id
        wid = self.workspace_id

        def _finalize_plaintext(plaintext: str, token_source: str) -> str:
            exp = _coerce_utc_expiry(self.token_expires_at)
            if exp is not None and exp < datetime.now(timezone.utc):
                _log_wa_token_stored_expiry_elapsed(aid, wid, token_source)
            return plaintext

        def _heal_shadow_from_legacy_if_needed(plaintext: str, vault_unusable: bool) -> None:
            """
            Mirror legacy ciphertext into whatsapp_credentials when vault is missing, blank,
            or did not yield a usable token. Flush-only; transaction boundary unchanged.
            """
            if not plaintext or not plaintext.strip():
                return
            if not _wa_has_stored_ciphertext(self.access_token_encrypted):
                return
            vault_missing_or_blank = (rec is None) or (
                not _wa_has_stored_ciphertext(getattr(rec, "access_token_encrypted", None))
            )
            if not (vault_unusable or vault_missing_or_blank):
                return
            try:
                self._upsert_credential_shadow(self.access_token_encrypted)
                db.session.flush()
                _log_wa_token_credential_shadow_healed(aid, wid)
            except Exception:
                pass

        vault_cipher = _wa_has_stored_ciphertext(getattr(rec, "access_token_encrypted", None) if rec else None)
        legacy_cipher = _wa_has_stored_ciphertext(self.access_token_encrypted)

        vault_unusable = False

        if vault_cipher and rec is not None:
            try:
                pt = decrypt_token(rec.access_token_encrypted)
            except Exception as e:
                _log_wa_token_decrypt_failed(aid, wid, "vault", e)
                vault_unusable = True
            else:
                if pt and pt.strip():
                    return _finalize_plaintext(pt, "vault")
                _log_wa_token_decrypted_empty(aid, wid, "vault")
                vault_unusable = True

        if legacy_cipher:
            try:
                pt = decrypt_token(self.access_token_encrypted)
            except Exception as e:
                _log_wa_token_decrypt_failed(aid, wid, "legacy", e)
                # Both vault and legacy failed to decrypt.
                if vault_unusable or not vault_cipher:
                    self.token_health = "corrupt"
                    try:
                        db.session.flush()
                    except Exception:
                        pass
            else:
                if not pt or not pt.strip():
                    _log_wa_token_decrypted_empty(aid, wid, "legacy")
                else:
                    out = _finalize_plaintext(pt, "legacy")
                    _heal_shadow_from_legacy_if_needed(out, vault_unusable)
                    return out

        if not vault_cipher and not legacy_cipher:
            _log_wa_token_not_stored(aid, wid)
            
        # If we reach here, we had ciphertexts but none yielded a token
        if vault_cipher or legacy_cipher:
            self.token_health = "corrupt"
            try:
                db.session.flush()
            except Exception:
                pass
                
        return None
    
    def _upsert_credential_shadow(self, encrypted_token: Optional[str]) -> None:
        """Mirror token + Meta IDs into whatsapp_credentials (shadow write; legacy columns unchanged)."""
        if not self.id:
            return
        row = WhatsAppCredential.query.filter_by(account_id=self.id).first()
        if row is None:
            row = WhatsAppCredential(account_id=self.id)
            db.session.add(row)
        row.access_token_encrypted = encrypted_token
        row.phone_number_id = self.phone_number_id
        row.waba_id = self.waba_id

    def set_access_token(self, token: str, token_type: str = "permanent", expires_at: Optional[datetime] = None) -> None:
        """Set encrypted access token."""
        from .encryption import encrypt_token
        self.access_token_encrypted = encrypt_token(token)
        self.token_type = token_type
        self.token_expires_at = expires_at
        try:
            db.session.flush()
        except Exception:
            pass
        self._upsert_credential_shadow(self.access_token_encrypted)

    def clear_access_token_storage(self) -> None:
        """Clear token ciphertext on account row and credential vault (legacy + slice)."""
        self.access_token_encrypted = None
        if self.id:
            row = WhatsAppCredential.query.filter_by(account_id=self.id).first()
            if row is not None:
                row.access_token_encrypted = None
    
    def get_flow_private_key(self) -> Optional[bytes]:
        """Get decrypted flow private key for this account."""
        if not self.flow_private_key:
            return None
        from .encryption import decrypt_token
        try:
            return decrypt_token(self.flow_private_key).encode('utf-8')
        except Exception:
            return None
    
    def set_flow_keys(self, private_key: str, public_key: str) -> None:
        """Set flow encryption keys. Private key is encrypted at rest."""
        from .encryption import encrypt_token
        self.flow_private_key = encrypt_token(private_key)
        self.flow_public_key = public_key  # Public key doesn't need encryption
    
    def has_flow_keys(self) -> bool:
        """Check if this account has flow encryption keys configured."""
        return bool(self.flow_private_key and self.flow_public_key)


# ============================================================
# TABLE 1b: WhatsApp Credentials (vault slice — expand/contract)
# ============================================================


class WhatsAppCredential(db.Model):
    """
    One-to-one encrypted Meta credentials for a WhatsAppAccount.

    Shadow-written alongside whatsapp_accounts legacy columns; no drops on expand phase.
    """

    __tablename__ = "whatsapp_credentials"
    __table_args__ = (
        Index("ix_whatsapp_credentials_account", "account_id"),
        {"extend_existing": True},
    )

    id = db.Column(db.Integer, primary_key=True)
    account_id = db.Column(
        db.Integer,
        db.ForeignKey("whatsapp_accounts.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
        index=True,
    )
    access_token_encrypted = db.Column(db.Text, nullable=True)
    phone_number_id = db.Column(db.String(64), nullable=True)
    waba_id = db.Column(db.String(64), nullable=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = db.Column(
        db.DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    account = db.relationship("WhatsAppAccount", back_populates="credential_record")


# ============================================================
# TABLE 1c: WhatsApp Bot Settings (AI slice — expand/contract)
# ============================================================


class WhatsAppBotSettings(db.Model):
    """
    One-to-one AI / bot configuration for a WhatsAppAccount.

    High-churn fields (prompts, model) update this row to avoid contending with
    webhook-driven updates on whatsapp_accounts. Legacy columns on the account
    row are shadow-written for monolith compatibility.
    """

    __tablename__ = "whatsapp_bot_settings"
    __table_args__ = (
        Index("ix_whatsapp_bot_settings_account", "account_id"),
        {"extend_existing": True},
    )

    id = db.Column(db.Integer, primary_key=True)
    account_id = db.Column(
        db.Integer,
        db.ForeignKey("whatsapp_accounts.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
        index=True,
    )
    ai_enabled = db.Column(db.Boolean, default=False, nullable=False)
    ai_model = db.Column(db.String(50), nullable=True)
    temperature = db.Column(db.Float, nullable=True)
    max_tokens = db.Column(db.Integer, nullable=True)
    prompt_override = db.Column(db.Text, nullable=True)
    knowledge_base_id = db.Column(db.String(100), nullable=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = db.Column(
        db.DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    account = db.relationship("WhatsAppAccount", back_populates="bot_settings")


def upsert_whatsapp_bot_settings_shadow(
    account: "WhatsAppAccount",
    *,
    ai_enabled: Optional[bool] = None,
    ai_model: Optional[str] = None,
    temperature: Optional[float] = None,
    max_tokens: Optional[int] = None,
    prompt_override: Optional[str] = None,
    knowledge_base_id: Optional[str] = None,
) -> None:
    """Mirror AI fields on whatsapp_accounts and whatsapp_bot_settings."""
    if not account.id:
        return
    row = WhatsAppBotSettings.query.filter_by(account_id=account.id).first()
    if row is None:
        row = WhatsAppBotSettings(account_id=account.id)
        db.session.add(row)
    if ai_enabled is not None:
        account.ai_enabled = ai_enabled
        row.ai_enabled = ai_enabled
    if ai_model is not None:
        account.ai_model = ai_model
        row.ai_model = ai_model
    if temperature is not None:
        account.temperature = temperature
        row.temperature = temperature
    if max_tokens is not None:
        account.max_tokens = max_tokens
        row.max_tokens = max_tokens
    if prompt_override is not None:
        account.prompt_override = prompt_override
        row.prompt_override = prompt_override
    if knowledge_base_id is not None:
        account.knowledge_base_id = knowledge_base_id
        row.knowledge_base_id = knowledge_base_id
    try:
        db.session.flush()
    except Exception:
        pass


# ============================================================
# TABLE 1d: WhatsApp Account Status (heartbeat slice — expand/contract)
# ============================================================


class WhatsAppAccountStatus(db.Model):
    """
    One-to-one high-churn connection / health fields for a WhatsAppAccount.

    Webhook and delivery receipts update this row (and legacy mirror columns) so
    token-heavy whatsapp_accounts rows are not locked on every Meta ping.
    """

    __tablename__ = "whatsapp_account_status"
    __table_args__ = (
        Index("ix_whatsapp_account_status_account", "account_id"),
        {"extend_existing": True},
    )

    id = db.Column(db.Integer, primary_key=True)
    account_id = db.Column(
        db.Integer,
        db.ForeignKey("whatsapp_accounts.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
        index=True,
    )
    status = db.Column(db.String(50), nullable=False, default="connected")
    quality_rating = db.Column(db.String(20), nullable=True)
    is_verified = db.Column(db.Boolean, default=False, nullable=False)
    last_webhook_received = db.Column(db.DateTime, nullable=True)
    last_outbound_sent = db.Column(db.DateTime, nullable=True)
    last_error_message = db.Column(db.Text, nullable=True)
    last_error_code = db.Column(db.Integer, nullable=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = db.Column(
        db.DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    account = db.relationship("WhatsAppAccount", back_populates="status_record")


# ============================================================
# TABLE 1e: Account capabilities (entitlement projection — monolith-owned)
# ============================================================


class WhatsAppAccountCapabilities(db.Model):
    """
    Operational snapshot for gates (AI, automation, broadcast, daily send cap).

    Written by the monolith (or its worker) via internal API — not computed here.
    """

    __tablename__ = "whatsapp_account_capabilities"
    __table_args__ = ({"extend_existing": True},)

    account_id = db.Column(
        db.Integer,
        db.ForeignKey("whatsapp_accounts.id", ondelete="CASCADE"),
        primary_key=True,
    )
    subscription_status = db.Column(db.String(32), nullable=False, default="ACTIVE")
    ai_enabled = db.Column(db.Boolean, nullable=False, default=True)
    automation_enabled = db.Column(db.Boolean, nullable=False, default=True)
    broadcast_enabled = db.Column(db.Boolean, nullable=False, default=True)
    daily_message_limit = db.Column(db.Integer, nullable=True)
    monthly_ai_tokens = db.Column(BigInteger, nullable=True)
    projection_version = db.Column(db.Integer, nullable=False, default=1)
    updated_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)

    account = db.relationship("WhatsAppAccount", back_populates="capability_record")


def _get_or_create_account_status_row(account: "WhatsAppAccount", sess) -> Optional["WhatsAppAccountStatus"]:
    if not account.id:
        return None
    row = getattr(account, "status_record", None)
    if row is None:
        row = sess.query(WhatsAppAccountStatus).filter_by(account_id=account.id).first()
    if row is None:
        row = WhatsAppAccountStatus(account_id=account.id, status="connected", is_verified=False)
        sess.add(row)
    return row


def update_account_heartbeat(
    account: "WhatsAppAccount",
    *,
    db_session=None,
    inbound: bool = False,
    outbound: bool = False,
    error: Optional[Dict[str, Any]] = None,
    commit: bool = False,
) -> None:
    """
    Update webhook / outbound heartbeat on the status slice and legacy columns.

    Does not commit unless commit=True (callers usually own the transaction).
    """
    sess = db_session or db.session
    now = datetime.now(timezone.utc)
    row = _get_or_create_account_status_row(account, sess)
    if row is None:
        return

    if inbound:
        account.last_webhook_received = now
        row.last_webhook_received = now
    if outbound:
        account.last_outbound_sent = now
        row.last_outbound_sent = now
    if error is not None:
        msg = error.get("message") or error.get("title") or error.get("error_user_msg")
        if msg is not None:
            account.last_error_message = str(msg)
            row.last_error_message = str(msg)
        code = error.get("code")
        code_i: Optional[int] = None
        if code is not None:
            try:
                code_i = int(code)
            except (TypeError, ValueError):
                code_i = None
        if code_i is not None:
            account.last_error_code = code_i
            row.last_error_code = code_i

    try:
        sess.flush()
    except Exception:
        pass
    if commit:
        try:
            sess.commit()
        except Exception:
            try:
                sess.rollback()
            except Exception:
                pass


def shadow_sync_account_quality_rating(
    account: "WhatsAppAccount",
    quality_rating: Optional[str],
    *,
    db_session=None,
    commit: bool = False,
) -> None:
    """Mirror Meta phone / account quality into status slice and legacy quality_score."""
    if not quality_rating or not account.id:
        return
    q = str(quality_rating).strip().upper()
    if not q:
        return
    sess = db_session or db.session
    account.quality_score = q
    row = _get_or_create_account_status_row(account, sess)
    if row is None:
        return
    row.quality_rating = q
    try:
        sess.flush()
    except Exception:
        pass
    if commit:
        try:
            sess.commit()
        except Exception:
            try:
                sess.rollback()
            except Exception:
                pass


def shadow_sync_account_restriction_state(
    account: "WhatsAppAccount",
    restriction_state: Optional[str],
    *,
    db_session=None,
    commit: bool = False,
) -> None:
    """Mirror Meta phone / account restriction into status slice and legacy restriction_state."""
    if not restriction_state or not account.id:
        return
    r = str(restriction_state).strip().upper()
    if not r:
        return
    sess = db_session or db.session
    account.restriction_state = r
    row = _get_or_create_account_status_row(account, sess)
    if row is not None:
        row.restriction_state = r
    try:
        sess.flush()
    except Exception:
        pass
    if commit:
        try:
            sess.commit()
        except Exception:
            try:
                sess.rollback()
            except Exception:
                pass



def accounts_query_with_any_token(query):
    """Filter WhatsAppAccount rows that have non-empty ciphertext on account or credential vault."""
    from sqlalchemy import or_, and_
    from sqlalchemy.orm import aliased

    Cred = aliased(WhatsAppCredential)
    nonempty_legacy = and_(
        WhatsAppAccount.access_token_encrypted.isnot(None),
        WhatsAppAccount.access_token_encrypted != "",
    )
    nonempty_vault = and_(
        Cred.access_token_encrypted.isnot(None),
        Cred.access_token_encrypted != "",
    )
    return query.outerjoin(Cred, Cred.account_id == WhatsAppAccount.id).filter(
        or_(nonempty_legacy, nonempty_vault)
    )


# ============================================================
# TABLE 2: WhatsApp Conversations
# ============================================================

class WhatsAppConversation(db.Model):
    """
    Chat conversations with WhatsApp users.
    
    A conversation is created when a user messages or when we initiate contact.
    Groups messages by user phone number per account.
    """
    __tablename__ = "whatsapp_conversations"
    __table_args__ = (
        UniqueConstraint("account_id", "user_phone", name="uq_account_user"),
        Index("ix_whatsapp_conversations_status", "status"),
        Index("ix_whatsapp_conversations_last_message", "last_message_at"),
        Index("ix_whatsapp_conversations_account_last", "account_id", "last_message_at"),  # Workspace analytics
        {"extend_existing": True},
    )

    id = db.Column(db.Integer, primary_key=True)
    account_id = db.Column(db.Integer, db.ForeignKey("whatsapp_accounts.id", ondelete="CASCADE"), nullable=False, index=True)
    user_phone = db.Column(db.String(32), nullable=False, index=True)  # User's phone number (E.164 format)
    user_name = db.Column(db.String(255), nullable=True)  # Contact name if available
    status = db.Column(db.String(16), default="open", nullable=False)  # open, closed
    unread_count = db.Column(db.Integer, default=0, nullable=False)  # Unread incoming messages
    
    # Session tracking (24h window based on inbound)
    last_inbound_at = db.Column(db.DateTime, nullable=True)  # Last user message
    last_outbound_at = db.Column(db.DateTime, nullable=True)  # Last business message
    session_expires_at = db.Column(db.DateTime, nullable=True)  # 24h after last inbound
    
    # Manual closing by agent (UI-level only, not WhatsApp-level)
    closed_by_agent = db.Column(db.Boolean, default=False, nullable=False)
    closed_at = db.Column(db.DateTime, nullable=True)  # When agent closed the chat
    
    # Attribution data (CTWA / Keywords)
    entry_source = db.Column(db.String(32), nullable=True)  # ctwa, keyword, organic, flow
    ctwa_clid = db.Column(db.String(255), nullable=True, index=True)
    ad_id = db.Column(db.String(64), nullable=True, index=True)
    campaign_id = db.Column(db.String(64), nullable=True, index=True)
    adset_id = db.Column(db.String(64), nullable=True, index=True)
    attribution_data = db.Column(JSON, nullable=True)  # Full raw attribution dict
    attributed_at = db.Column(db.DateTime, nullable=True)
    
    # Timestamps
    last_message_at = db.Column(db.DateTime, nullable=True)  # When last message was sent/received
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    # Relationships
    account = db.relationship("WhatsAppAccount", back_populates="conversations")
    messages = db.relationship("WhatsAppMessage", back_populates="conversation", lazy="dynamic", order_by="WhatsAppMessage.created_at.desc()", cascade="all, delete-orphan")

    @property
    def is_session_open(self) -> bool:
        """Check if 24h messaging window is open. Respects agent close."""
        # Agent close takes precedence
        if self.closed_by_agent:
            return False
        if not self.session_expires_at:
            return False
        # Handle both timezone-naive (from DB) and timezone-aware datetimes
        now = datetime.now(timezone.utc)
        expires = self.session_expires_at
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        return now < expires
    
    @property
    def session_time_left_seconds(self) -> int:
        """Seconds remaining in session (0 if closed/expired)."""
        if not self.is_session_open:
            return 0
        now = datetime.now(timezone.utc)
        expires = self.session_expires_at
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        return max(0, int((expires - now).total_seconds()))
    
    @property
    def close_reason(self) -> str:
        """Why the session is closed: 'agent', 'expired', or 'never_opened'."""
        if self.closed_by_agent:
            return "agent"
        if self.session_expires_at:
            now = datetime.now(timezone.utc)
            expires = self.session_expires_at
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=timezone.utc)
            if now >= expires:
                return "expired"
        return "never_opened"

    def __repr__(self):
        return f"<WhatsAppConversation {self.id} with {self.user_phone}>"

    def _inbox_attention_flags(self) -> Dict[str, Any]:
        """Expose human_required / needs_attention from attribution_data for inbox filters."""
        data = self.attribution_data if isinstance(self.attribution_data, dict) else {}
        return {
            "human_required": bool(data.get("human_required")),
            "human_required_reason": data.get("human_required_reason"),
            "human_required_at": data.get("human_required_at"),
            "needs_attention": bool(data.get("needs_attention")),
            "needs_attention_reason": data.get("needs_attention_reason"),
            "needs_attention_at": data.get("needs_attention_at"),
            "ai_paused_by_agent": bool(data.get("ai_paused_by_agent")),
            "ai_paused_by_agent_at": data.get("ai_paused_by_agent_at"),
        }

    def to_dict(self, include_messages: bool = False, message_limit: int = 50) -> Dict[str, Any]:
        result = {
            "id": self.id,
            "account_id": self.account_id,
            "user_phone": self.user_phone,
            "user_name": self.user_name,
            "status": self.status,
            "unread_count": self.unread_count,
            "is_session_open": self.is_session_open,
            "session_time_left_seconds": self.session_time_left_seconds,
            "close_reason": self.close_reason if not self.is_session_open else None,
            "closed_by_agent": self.closed_by_agent,
            "closed_at": self.closed_at.isoformat() if self.closed_at else None,
            "last_inbound_at": self.last_inbound_at.isoformat() if self.last_inbound_at else None,
            "last_outbound_at": self.last_outbound_at.isoformat() if self.last_outbound_at else None,
            "session_expires_at": self.session_expires_at.isoformat() if self.session_expires_at else None,
            "last_message_at": self.last_message_at.isoformat() if self.last_message_at else None,
            "entry_source": self.entry_source,
            "ctwa_clid": self.ctwa_clid,
            "ad_id": self.ad_id,
            "campaign_id": self.campaign_id,
            "adset_id": self.adset_id,
            "attributed_at": self.attributed_at.isoformat() if self.attributed_at else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
        result.update(self._inbox_attention_flags())
        
        if include_messages:
            messages = self.messages.limit(message_limit).all()
            result["messages"] = [m.to_dict() for m in reversed(messages)]  # Oldest first
        
        return result

    def mark_read(self) -> None:
        """Mark all messages as read and reset unread count."""
        self.unread_count = 0
        self.updated_at = datetime.now(timezone.utc)


# ============================================================
# TABLE 3: WhatsApp Messages
# ============================================================

class WhatsAppMessage(db.Model):
    """
    Individual WhatsApp messages (sent and received).
    
    Stores message content as JSON to handle all message types.
    """
    __tablename__ = "whatsapp_messages"
    __table_args__ = (
        Index("ix_whatsapp_messages_wamid", "wamid"),
        Index("ix_whatsapp_messages_direction", "direction"),
        Index("ix_whatsapp_messages_created", "created_at"),
        {"extend_existing": True},
    )

    id = db.Column(db.Integer, primary_key=True)
    conversation_id = db.Column(db.Integer, db.ForeignKey("whatsapp_conversations.id", ondelete="CASCADE"), nullable=False, index=True)
    
    # Message metadata
    direction = db.Column(db.String(16), nullable=False)  # incoming, outgoing
    type = db.Column(db.String(32), nullable=False, default="text")  # text, template, image, video, etc.
    
    # Template tracking for analytics (nullable for non-template messages)
    template_name = db.Column(db.String(128), nullable=True, index=True)
    template_category = db.Column(db.String(32), nullable=True, index=True)  # UTILITY, MARKETING, AUTHENTICATION
    
    # Link to bulk campaign (for analytics)
    campaign_id = db.Column(db.Integer, nullable=True, index=True)
    
    # Message content (JSON for flexibility)
    content = db.Column(JSON, nullable=True)  # Stores message body, media info, template params, etc.
    
    # WhatsApp message ID for deduplication and status tracking
    wamid = db.Column(db.String(128), nullable=True, unique=True)  # WhatsApp Message ID
    
    # Delivery status (for outgoing messages)
    status = db.Column(db.String(16), default="pending")  # pending, sent, delivered, read, failed
    error_code = db.Column(db.String(16), nullable=True)  # Error code if failed
    error_message = db.Column(db.Text, nullable=True)  # Error description if failed
    
    # Timestamps (all UTC)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    sent_at = db.Column(db.DateTime, nullable=True)  # When API confirmed sent
    delivered_at = db.Column(db.DateTime, nullable=True)
    read_at = db.Column(db.DateTime, nullable=True)

    # Relationships
    conversation = db.relationship("WhatsAppConversation", back_populates="messages")

    def __repr__(self):
        return f"<WhatsAppMessage {self.id} ({self.direction}/{self.type})>"

    def to_dict(self) -> Dict[str, Any]:
        # Helper to format datetime with timezone for JavaScript
        def format_datetime(dt):
            if not dt:
                return None
            # Ensure datetime has UTC timezone and format with Z suffix
            if dt.tzinfo is None:
                # Assume UTC for naive datetimes
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.strftime('%Y-%m-%dT%H:%M:%S') + 'Z'
        
        return {
            "id": self.id,
            "conversation_id": self.conversation_id,
            "direction": self.direction,
            "type": self.type,
            "template_name": self.template_name,
            "template_category": self.template_category,
            "content": self.content,
            "wamid": self.wamid,
            "status": self.status,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "created_at": format_datetime(self.created_at),
            "sent_at": format_datetime(self.sent_at),
            "delivered_at": format_datetime(self.delivered_at),
            "read_at": format_datetime(self.read_at),
        }

    def update_status(self, status: str, timestamp: Optional[datetime] = None) -> None:
        """Update message delivery status."""
        self.status = status
        if status == "delivered" and timestamp:
            self.delivered_at = timestamp
        elif status == "read" and timestamp:
            self.read_at = timestamp

    def set_error(self, code: str, message: str) -> None:
        """Set error information for failed messages."""
        self.status = "failed"
        self.error_code = code
        self.error_message = message


# ============================================================
# TABLE 4: Message Status Events (for debugging/audit)
# ============================================================

class MessageStatusEvent(db.Model):
    """
    Status history for message delivery tracking.
    
    Stores every status webhook event for a message to help debug
    delivery delays and webhook ordering issues. Keep 14 days max.
    """
    __tablename__ = "whatsapp_message_status_events"
    __table_args__ = (
        Index("ix_status_events_wamid", "wamid"),
        Index("ix_status_events_timestamp", "timestamp"),
        Index("ix_status_events_created", "created_at"),
        {"extend_existing": True},
    )

    id = db.Column(db.Integer, primary_key=True)
    wamid = db.Column(db.String(128), nullable=False, index=True)  # WhatsApp Message ID
    status = db.Column(db.String(16), nullable=False)  # sent, delivered, read, failed
    timestamp = db.Column(db.DateTime, nullable=False)  # Timestamp from webhook
    error_code = db.Column(db.String(16), nullable=True)  # Error code if failed
    error_message = db.Column(db.Text, nullable=True)  # Error description if failed
    raw_event = db.Column(JSON, nullable=True)  # Raw webhook status event for debugging
    
    # Record when we received this event
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)

    def __repr__(self):
        return f"<MessageStatusEvent {self.wamid} -> {self.status}>"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "wamid": self.wamid,
            "status": self.status,
            "timestamp": self.timestamp.isoformat() if self.timestamp else None,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


# ============================================================
# TABLE 4b: Outbound Usage Events (billing consumption outbox)
# ============================================================

class WhatsAppUsageEvent(db.Model):
    """
    Durable usage events emitted by WhatsApp service.

    Billing/monolith consumes this table (or internal API) as the
    single accounting source instead of direct usage mutations.
    """
    __tablename__ = "whatsapp_usage_events"
    __table_args__ = (
        UniqueConstraint("event_key", name="uq_whatsapp_usage_events_event_key"),
        Index("ix_whatsapp_usage_events_type", "event_type"),
        Index("ix_whatsapp_usage_events_account", "account_id"),
        Index("ix_whatsapp_usage_events_created", "created_at"),
        {"extend_existing": True},
    )

    id = db.Column(db.BigInteger, primary_key=True)
    event_type = db.Column(db.String(64), nullable=False)  # message_sent, broadcast_sent, ...
    event_key = db.Column(db.String(191), nullable=False)  # idempotency key
    account_id = db.Column(db.Integer, db.ForeignKey("whatsapp_accounts.id"), nullable=False, index=True)
    message_id = db.Column(db.Integer, db.ForeignKey("whatsapp_messages.id"), nullable=True, index=True)
    wamid = db.Column(db.String(128), nullable=True, index=True)
    occurred_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    payload = db.Column(JSON, nullable=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)

    account = db.relationship("WhatsAppAccount", backref=db.backref("usage_events", lazy="dynamic", passive_deletes=True))
    message = db.relationship("WhatsAppMessage", backref=db.backref("usage_events", lazy="dynamic"))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": int(self.id) if self.id is not None else None,
            "event_type": self.event_type,
            "event_key": self.event_key,
            "account_id": self.account_id,
            "message_id": self.message_id,
            "wamid": self.wamid,
            "occurred_at": self.occurred_at.isoformat() if self.occurred_at else None,
            "payload": self.payload or {},
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


# ============================================================
# TABLE 5: WhatsApp Webhook Logs
# ============================================================

class WhatsAppWebhookLog(db.Model):
    """
    Raw webhook payloads for debugging and audit.
    
    Stores every webhook received from Meta for troubleshooting.
    """
    __tablename__ = "whatsapp_webhook_logs"
    __table_args__ = (
        Index("ix_whatsapp_webhook_logs_event", "event_type"),
        Index("ix_whatsapp_webhook_logs_received", "received_at"),
        {"extend_existing": True},
    )

    id = db.Column(db.Integer, primary_key=True)
    raw_json = db.Column(db.Text, nullable=False)  # Complete raw JSON payload
    event_type = db.Column(db.String(64), nullable=True)  # messages, statuses, errors, etc.
    phone_number_id = db.Column(db.String(64), nullable=True)  # For filtering
    processed = db.Column(db.Boolean, default=False, nullable=False)  # Whether successfully processed
    error_message = db.Column(db.Text, nullable=True)  # Processing error if any
    
    # Timestamps
    received_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    processed_at = db.Column(db.DateTime, nullable=True)

    def __repr__(self):
        return f"<WhatsAppWebhookLog {self.id} ({self.event_type})>"

    def to_dict(self) -> Dict[str, Any]:
        # Parse raw_json safely
        try:
            parsed = json.loads(self.raw_json) if self.raw_json else None
        except (json.JSONDecodeError, TypeError):
            parsed = None

        return {
            "id": self.id,
            "event_type": self.event_type,
            "phone_number_id": self.phone_number_id,
            "processed": self.processed,
            "error_message": self.error_message,
            "received_at": self.received_at.isoformat() if self.received_at else None,
            "processed_at": self.processed_at.isoformat() if self.processed_at else None,
            "payload": parsed,
        }

    @classmethod
    def log_webhook(cls, raw_json: str, event_type: Optional[str] = None, phone_number_id: Optional[str] = None) -> "WhatsAppWebhookLog":
        """Create a webhook log entry."""
        log = cls(
            raw_json=raw_json,
            event_type=event_type,
            phone_number_id=phone_number_id,
        )
        db.session.add(log)
        return log

    def mark_processed(self, error: Optional[str] = None) -> None:
        """Mark webhook as processed."""
        self.processed = error is None
        self.error_message = error
        self.processed_at = datetime.now(timezone.utc)


# ============================================================
# TABLE: WhatsApp Flows
# ============================================================

class WhatsAppFlow(db.Model):
    """
    WhatsApp Flows for interactive data collection.
    
    Flows are interactive mini-apps that users can complete within WhatsApp.
    They must be triggered via template CTA buttons (cannot be sent directly).
    Published flows are IMMUTABLE - must clone to make changes.
    """
    __tablename__ = "whatsapp_flows"
    __table_args__ = (
        UniqueConstraint("account_id", "name", "flow_version", name="uq_flow_name_version"),
        Index("ix_whatsapp_flows_account", "account_id"),
        Index("ix_whatsapp_flows_status", "status"),
        Index("ix_whatsapp_flows_parent", "parent_flow_id"),
        {"extend_existing": True},
    )

    id = db.Column(db.Integer, primary_key=True)
    account_id = db.Column(db.Integer, db.ForeignKey("whatsapp_accounts.id"), nullable=False)
    
    # Flow identity
    name = db.Column(db.String(128), nullable=False, index=True)
    category = db.Column(db.String(32), nullable=False)  # LEAD_GEN, SURVEY, BOOKING, FEEDBACK, CUSTOM
    
    # Versioning (Refinement #1)
    flow_version = db.Column(db.Integer, default=1, nullable=False)
    parent_flow_id = db.Column(db.Integer, db.ForeignKey("whatsapp_flows.id"), nullable=True)
    
    # Flow content
    flow_json = db.Column(JSON, nullable=False)  # Full WhatsApp Flow JSON
    schema_version = db.Column(db.String(16), default="5.0", nullable=False)  # Meta schema version
    entry_screen_id = db.Column(db.String(64), nullable=False)  # Refinement #2: Explicit entry point
    
    # Meta sync
    meta_flow_id = db.Column(db.String(64), nullable=True, index=True)  # ID from Meta after publish
    status = db.Column(db.String(16), default="DRAFT", nullable=False)  # DRAFT, PUBLISHED, DEPRECATED
    
    # Timestamps
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))
    published_at = db.Column(db.DateTime, nullable=True)
    
    # Relationships
    account = db.relationship("WhatsAppAccount", backref=db.backref("flows", lazy="dynamic", passive_deletes=True))
    parent_flow = db.relationship("WhatsAppFlow", remote_side=[id], backref="child_versions")

    def __repr__(self):
        return f"<WhatsAppFlow {self.id}: {self.name} v{self.flow_version} ({self.status})>"

    def to_dict(self) -> Dict[str, Any]:
        """Convert flow to dictionary for API responses."""
        return {
            "id": self.id,
            "account_id": self.account_id,
            "name": self.name,
            "category": self.category,
            "flow_version": self.flow_version,
            "parent_flow_id": self.parent_flow_id,
            "flow_json": self.flow_json,
            "schema_version": self.schema_version,
            "entry_screen_id": self.entry_screen_id,
            "meta_flow_id": self.meta_flow_id,
            "status": self.status,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "published_at": self.published_at.isoformat() if self.published_at else None,
            "screen_count": len(self.flow_json.get("screens", [])) if self.flow_json else 0,
            "is_editable": self.status == "DRAFT",
            "is_selectable": self.status == "PUBLISHED",
        }

    def get_screens(self) -> list:
        """Get list of screens from flow JSON."""
        if not self.flow_json:
            return []
        return self.flow_json.get("screens", [])

    def get_screen_ids(self) -> list:
        """Get list of screen IDs."""
        return [s.get("id") for s in self.get_screens()]

    def can_publish(self) -> bool:
        """Check if flow can be published."""
        return self.status == "DRAFT" and self.flow_json is not None

    def clone(self) -> "WhatsAppFlow":
        """
        Clone this flow for editing (published flows are immutable).
        Returns a new DRAFT flow with incremented version.
        """
        new_flow = WhatsAppFlow(
            account_id=self.account_id,
            name=self.name,
            category=self.category,
            flow_version=self.flow_version + 1,
            parent_flow_id=self.id,
            flow_json=self.flow_json.copy() if self.flow_json else {},
            schema_version=self.schema_version,
            entry_screen_id=self.entry_screen_id,
            status="DRAFT",
        )
        return new_flow


class WhatsAppFavoriteSticker(db.Model):
    """
    User/Workspace favorite stickers for quick reuse.
    """
    __tablename__ = "whatsapp_favorite_stickers"
    __table_args__ = (
        Index("ix_wa_fav_stickers_workspace", "workspace_id"),
        {"extend_existing": True},
    )

    id = db.Column(db.Integer, primary_key=True)
    workspace_id = db.Column(db.Integer, nullable=False, index=True)
    media_id = db.Column(db.String(128), nullable=False)  # Meta Media ID
    mime_type = db.Column(db.String(64), nullable=True)
    sha256 = db.Column(db.String(128), nullable=True)
    
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "workspace_id": self.workspace_id,
            "media_id": self.media_id,
            "mime_type": self.mime_type,
            "sha256": self.sha256,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


# ============================================================
# Platform V2: onboarding recovery, trust snapshots, reputation timeline
# ============================================================


class OnboardingSession(db.Model):
    """
    Embedded Signup / connect flow session for abandonment recovery and audit.
    Customer owns WABA; Sociovia stores delegated session state only.
    """

    __tablename__ = "onboarding_sessions"
    __table_args__ = (
        Index("ix_onboarding_sessions_workspace", "workspace_id"),
        Index("ix_onboarding_sessions_correlation", "correlation_id"),
        Index("ix_onboarding_sessions_status", "status"),
        Index("ix_onboarding_sessions_user", "user_id"),
        {"extend_existing": True},
    )

    id = db.Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id = db.Column(db.String(255), nullable=False)
    user_id = db.Column(db.String(64), nullable=False)
    account_id = db.Column(db.Integer, db.ForeignKey("whatsapp_accounts.id", ondelete="SET NULL"), nullable=True)
    correlation_id = db.Column(db.String(64), nullable=False)
    onboarding_path = db.Column(db.String(40), nullable=False, default="embedded")
    business_manager_id = db.Column(db.String(64), nullable=True)
    waba_id = db.Column(db.String(64), nullable=True)
    phone_number_id = db.Column(db.String(64), nullable=True)
    embedded_signup_version = db.Column(db.String(32), nullable=True)
    graph_version = db.Column(db.String(16), nullable=True)
    sdk_version = db.Column(db.String(32), nullable=True)
    config_id = db.Column(db.String(64), nullable=True)
    status = db.Column(db.String(32), nullable=False, default="started")
    last_step = db.Column(db.String(64), nullable=True)
    session_payload = db.Column(JSONB, nullable=True)
    last_error = db.Column(db.Text, nullable=True)
    resume_token_hash = db.Column(db.String(128), nullable=True)
    abandoned_at = db.Column(db.DateTime, nullable=True)
    completed_at = db.Column(db.DateTime, nullable=True)
    last_event_at = db.Column(db.DateTime, nullable=True)
    expires_at = db.Column(db.DateTime, nullable=True)
    is_coexistence = db.Column(db.Boolean, nullable=False, default=False)
    onboarding_method = db.Column(db.String(40), nullable=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    account = db.relationship("WhatsAppAccount", back_populates="onboarding_sessions")
    events = db.relationship(
        "OnboardingEvent",
        back_populates="session",
        lazy="dynamic",
        cascade="all, delete-orphan",
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": str(self.id) if self.id else None,
            "workspace_id": self.workspace_id,
            "user_id": self.user_id,
            "account_id": self.account_id,
            "correlation_id": self.correlation_id,
            "onboarding_path": self.onboarding_path,
            "business_manager_id": self.business_manager_id,
            "waba_id": self.waba_id,
            "phone_number_id": self.phone_number_id,
            "embedded_signup_version": self.embedded_signup_version,
            "graph_version": self.graph_version,
            "sdk_version": self.sdk_version,
            "config_id": self.config_id,
            "status": self.status,
            "last_step": self.last_step,
            "last_error": self.last_error,
            "abandoned_at": self.abandoned_at.isoformat() if self.abandoned_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "last_event_at": self.last_event_at.isoformat() if self.last_event_at else None,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "is_coexistence": bool(self.is_coexistence),
            "onboarding_method": self.onboarding_method,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class OnboardingEvent(db.Model):
    """Append-only events for an onboarding session."""

    __tablename__ = "onboarding_events"
    __table_args__ = (
        Index("ix_onboarding_events_session", "session_id", "created_at"),
        {"extend_existing": True},
    )

    id = db.Column(db.BigInteger, primary_key=True, autoincrement=True)
    session_id = db.Column(UUID(as_uuid=True), db.ForeignKey("onboarding_sessions.id", ondelete="CASCADE"), nullable=False)
    account_id = db.Column(db.Integer, db.ForeignKey("whatsapp_accounts.id", ondelete="SET NULL"), nullable=True)
    event_type = db.Column(db.String(80), nullable=False)
    correlation_id = db.Column(db.String(64), nullable=True)
    payload = db.Column(JSONB, nullable=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)

    session = db.relationship("OnboardingSession", back_populates="events")
    account = db.relationship("WhatsAppAccount", viewonly=True)


class TrustSnapshot(db.Model):
    """
    Point-in-time trust / health inputs (advisory). Do not use alone for hard enforcement.
    """

    __tablename__ = "trust_snapshots"
    __table_args__ = (
        Index("ix_trust_snapshots_account_time", "account_id", "captured_at"),
        {"extend_existing": True},
    )

    id = db.Column(db.BigInteger, primary_key=True, autoincrement=True)
    account_id = db.Column(db.Integer, db.ForeignKey("whatsapp_accounts.id", ondelete="CASCADE"), nullable=False)
    captured_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    quality_rating = db.Column(db.String(16), nullable=True)
    messaging_tier = db.Column(db.String(32), nullable=True)
    name_status = db.Column(db.String(64), nullable=True)
    verification_status = db.Column(db.String(64), nullable=True)
    webhook_health = db.Column(db.String(24), nullable=True)
    webhook_subscription_status = db.Column(db.String(32), nullable=True)
    restriction_state = db.Column(db.String(40), nullable=True)
    operational_mode = db.Column(db.String(24), nullable=True)
    trust_score = db.Column(Numeric(6, 2), nullable=True)
    inputs = db.Column(JSONB, nullable=True)
    notes = db.Column(db.Text, nullable=True)

    account = db.relationship("WhatsAppAccount", back_populates="trust_snapshots")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "account_id": self.account_id,
            "captured_at": self.captured_at.isoformat() if self.captured_at else None,
            "quality_rating": self.quality_rating,
            "messaging_tier": self.messaging_tier,
            "name_status": self.name_status,
            "verification_status": self.verification_status,
            "webhook_health": self.webhook_health,
            "webhook_subscription_status": self.webhook_subscription_status,
            "restriction_state": self.restriction_state,
            "operational_mode": self.operational_mode,
            "trust_score": float(self.trust_score) if self.trust_score is not None else None,
            "inputs": self.inputs,
            "notes": self.notes,
        }


class WhatsAppPhoneReputationSnapshot(db.Model):
    """Historical quality / tier / display name status for support and timelines."""

    __tablename__ = "whatsapp_phone_reputation_snapshots"
    __table_args__ = (
        Index("ix_phone_rep_account_time", "account_id", "captured_at"),
        Index("ix_phone_rep_phone_time", "phone_number_id", "captured_at"),
        {"extend_existing": True},
    )

    id = db.Column(db.BigInteger, primary_key=True, autoincrement=True)
    account_id = db.Column(db.Integer, db.ForeignKey("whatsapp_accounts.id", ondelete="CASCADE"), nullable=False)
    phone_number_id = db.Column(db.String(64), nullable=False)
    captured_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    quality_rating = db.Column(db.String(16), nullable=True)
    messaging_tier = db.Column(db.String(32), nullable=True)
    name_status = db.Column(db.String(64), nullable=True)
    raw_graph = db.Column(JSONB, nullable=True)

    account = db.relationship("WhatsAppAccount", back_populates="phone_reputation_snapshots")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "account_id": self.account_id,
            "phone_number_id": self.phone_number_id,
            "captured_at": self.captured_at.isoformat() if self.captured_at else None,
            "quality_rating": self.quality_rating,
            "messaging_tier": self.messaging_tier,
            "name_status": self.name_status,
            "raw_graph": self.raw_graph,
        }


class MetaAppHealthSnapshot(db.Model):
    """Aggregated app-level signals (webhook / Graph / rate limits) for outage vs tenant diagnosis."""

    __tablename__ = "meta_app_health_snapshots"
    __table_args__ = (Index("ix_meta_app_health_time", "captured_at"), {"extend_existing": True})

    id = db.Column(db.BigInteger, primary_key=True, autoincrement=True)
    captured_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    window_minutes = db.Column(db.Integer, nullable=False, default=30)
    graph_error_rate = db.Column(Numeric(8, 6), nullable=True)
    webhook_callback_error_count = db.Column(db.Integer, nullable=True)
    rate_limit_hits = db.Column(db.Integer, nullable=True)
    verification_failure_count = db.Column(db.Integer, nullable=True)
    details = db.Column(JSONB, nullable=True)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "captured_at": self.captured_at.isoformat() if self.captured_at else None,
            "window_minutes": self.window_minutes,
            "graph_error_rate": float(self.graph_error_rate) if self.graph_error_rate is not None else None,
            "webhook_callback_error_count": self.webhook_callback_error_count,
            "rate_limit_hits": self.rate_limit_hits,
            "verification_failure_count": self.verification_failure_count,
            "details": self.details,
        }


class WhatsAppOperationalLog(db.Model):
    """
    Log of operational mode transitions, self-healing events, and manual overrides.
    """

    __tablename__ = "whatsapp_operational_logs"
    __table_args__ = (
        Index("ix_whatsapp_op_logs_account_time", "account_id", "created_at"),
        {"extend_existing": True},
    )

    id = db.Column(db.BigInteger, primary_key=True, autoincrement=True)
    account_id = db.Column(db.Integer, db.ForeignKey("whatsapp_accounts.id", ondelete="CASCADE"), nullable=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    
    # Event types: 'transition', 'self_healing', 'override'
    event_type = db.Column(db.String(64), nullable=False)
    
    # Modes
    previous_mode = db.Column(db.String(64), nullable=True)
    new_mode = db.Column(db.String(64), nullable=True)
    
    # Describe the event
    reason = db.Column(db.Text, nullable=True)
    
    # Additional structured context (metrics that caused the transition, risk classes affected, etc.)
    context = db.Column(JSONB, nullable=True)
    
    # Expiration for manual overrides
    expires_at = db.Column(db.DateTime, nullable=True)
    
    # E.g. system, user, or operator
    actor = db.Column(db.String(64), default="system", nullable=False)

    account = db.relationship("WhatsAppAccount", backref=db.backref("operational_logs", lazy="dynamic", cascade="all, delete-orphan", passive_deletes=True))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "account_id": self.account_id,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "event_type": self.event_type,
            "previous_mode": self.previous_mode,
            "new_mode": self.new_mode,
            "reason": self.reason,
            "context": self.context,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "actor": self.actor,
        }
