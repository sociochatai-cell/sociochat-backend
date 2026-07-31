# whatsapp/commerce_pay/models.py
"""
Per-workspace PayU payment configuration.

Each business (workspace) stores its OWN PayU merchant credentials so its
customers pay INTO that business's account. The salt (the signing secret) is
Fernet-encrypted at rest; the merchant key is stored plain (it is semi-public,
used in the payment form) but never fully exposed on read (masked).
"""

from datetime import datetime, timezone

from models import db


class WorkspacePaymentConfig(db.Model):
    """One payment-gateway configuration per workspace (PayU for now)."""

    __tablename__ = "workspace_payment_configs"
    __table_args__ = (
        db.UniqueConstraint("workspace_id", name="uq_workspace_payment_config"),
        {"extend_existing": True},
    )

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    workspace_id = db.Column(
        db.Integer,
        db.ForeignKey("workspaces2.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    provider = db.Column(db.String(16), nullable=False, default="payu")
    # PayU merchant credentials. Salt is the secret -> encrypted at rest.
    merchant_key = db.Column(db.String(255), nullable=True)
    merchant_salt_encrypted = db.Column(db.Text, nullable=True)
    mode = db.Column(db.String(8), nullable=False, default="test")  # 'test' | 'live'
    is_active = db.Column(db.Boolean, nullable=False, default=True)

    # Auto-send a payment link the moment a catalog order arrives (workspace default).
    auto_request_payment = db.Column(db.Boolean, nullable=False, default=False)
    # Extra notification recipients (comma-separated) for order/payment emails,
    # in addition to the default owner/account address.
    notify_emails = db.Column(db.Text, nullable=True)

    created_at = db.Column(db.DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = db.Column(
        db.DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    # --- salt encryption helpers ---
    def set_salt(self, salt: str) -> None:
        from whatsapp.encryption import encrypt_token
        self.merchant_salt_encrypted = encrypt_token(salt) if salt else None

    def get_salt(self):
        if not self.merchant_salt_encrypted:
            return None
        try:
            from whatsapp.encryption import decrypt_token
            return decrypt_token(self.merchant_salt_encrypted)
        except Exception:
            return None

    @property
    def is_connected(self) -> bool:
        return bool(self.merchant_key and self.merchant_salt_encrypted and self.is_active)

    @staticmethod
    def _mask(value: str) -> str:
        if not value:
            return ""
        v = str(value)
        if len(v) <= 4:
            return "•" * len(v)
        return "•" * (len(v) - 4) + v[-4:]

    def notify_emails_list(self) -> list:
        raw = self.notify_emails or ""
        return [e.strip() for e in raw.replace(";", ",").split(",") if e.strip()]

    def serialize(self) -> dict:
        """Safe representation for the UI — NEVER returns the salt or full key."""
        return {
            "provider": self.provider,
            "mode": self.mode,
            "is_active": self.is_active,
            "connected": self.is_connected,
            "merchant_key_masked": self._mask(self.merchant_key or ""),
            "auto_request_payment": bool(self.auto_request_payment),
            "notify_emails": self.notify_emails_list(),
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class CommerceOrder(db.Model):
    """A single in-chat payment request generated for a customer.

    One row per "Request Payment" action. Keyed by ``txnid`` (also the PayU
    transaction id), which ties the chat message → PayU checkout → callback →
    lookup together. Self-contained/removable (SocioChat-only feature).
    """

    __tablename__ = "commerce_orders"
    __table_args__ = ({"extend_existing": True},)

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    workspace_id = db.Column(
        db.Integer,
        db.ForeignKey("workspaces2.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # PayU transaction id (our reference). Globally unique.
    txnid = db.Column(db.String(64), nullable=False, unique=True, index=True)

    customer_phone = db.Column(db.String(32), nullable=False)      # E.164 without +
    customer_name = db.Column(db.String(120), nullable=True)
    customer_email = db.Column(db.String(160), nullable=True)
    # Conversation to post the confirmation into (may be null → resolve by phone).
    conversation_id = db.Column(db.Integer, nullable=True)
    # WhatsApp order message id this was auto-generated from (dedup guard). Null for manual.
    source_message_id = db.Column(db.String(128), nullable=True, index=True)
    # 'manual' (owner clicked) | 'auto' (order-received automation)
    origin = db.Column(db.String(8), nullable=False, default="manual")

    amount = db.Column(db.Numeric(12, 2), nullable=False)
    currency = db.Column(db.String(8), nullable=False, default="INR")
    productinfo = db.Column(db.String(160), nullable=False, default="Order")

    # pending | paid | failed
    status = db.Column(db.String(16), nullable=False, default="pending", index=True)
    payu_mihpayid = db.Column(db.String(64), nullable=True)  # PayU's own txn id on success
    mode = db.Column(db.String(8), nullable=False, default="test")

    created_at = db.Column(db.DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = db.Column(
        db.DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    paid_at = db.Column(db.DateTime(timezone=True), nullable=True)

    @property
    def amount_str(self) -> str:
        """PayU wants a 2-dp decimal string, e.g. ``1999.00``."""
        return f"{float(self.amount):.2f}"

    def serialize(self) -> dict:
        return {
            "id": self.id,
            "txnid": self.txnid,
            "customer_phone": self.customer_phone,
            "customer_name": self.customer_name,
            "amount": float(self.amount) if self.amount is not None else None,
            "currency": self.currency,
            "productinfo": self.productinfo,
            "status": self.status,
            "mode": self.mode,
            "origin": self.origin,
            "conversation_id": self.conversation_id,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "paid_at": self.paid_at.isoformat() if self.paid_at else None,
        }


class CommerceChatOverride(db.Model):
    """Per-conversation override of the workspace auto-payment default.

    No row  → use the workspace default (WorkspacePaymentConfig.auto_request_payment).
    mode='on'/'off' → force auto on/off for this one chat.
    """

    __tablename__ = "commerce_chat_overrides"
    __table_args__ = (
        db.UniqueConstraint("conversation_id", name="uq_commerce_chat_override_conv"),
        {"extend_existing": True},
    )

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    workspace_id = db.Column(db.Integer, nullable=False, index=True)
    conversation_id = db.Column(db.Integer, nullable=False, index=True)
    mode = db.Column(db.String(8), nullable=False, default="on")  # 'on' | 'off'
    updated_at = db.Column(
        db.DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
