"""
PayU SI (autopay) mandate models — recurring subscription auto-renew.
=====================================================================

A *mandate* is a customer-approved standing instruction to auto-debit the plan
price every billing period. The customer approves ONCE at checkout (SI
registration); after that WE initiate each recurring charge via PayU's
``si_transaction`` API. Ported from the reference implementation.

At most ONE live mandate per user (enforced by a partial unique index in the
schema self-heal). Lifecycle is driven through ``mandate_service.transition()``.
"""

from datetime import datetime, timezone
from models import db


# Mandate lifecycle statuses (see mandate_service.ALLOWED_TRANSITIONS).
MANDATE_PENDING = "pending_registration"  # SI checkout started, not yet confirmed
MANDATE_ACTIVE = "active"                 # registered; eligible for recurring charges
MANDATE_PAUSED = "paused"                 # temporarily halted (e.g. payment retry window)
MANDATE_CANCELLED = "cancelled"           # user/admin cancelled — terminal
MANDATE_FAILED = "failed"                 # registration failed or 3+ consecutive charge failures
MANDATE_EXPIRED = "expired"               # validity window ended
MANDATE_STATUSES = {
    MANDATE_PENDING, MANDATE_ACTIVE, MANDATE_PAUSED,
    MANDATE_CANCELLED, MANDATE_FAILED, MANDATE_EXPIRED,
}


class PayuMandate(db.Model):
    __tablename__ = "payu_mandates"
    __table_args__ = ({"extend_existing": True},)

    id = db.Column(db.BigInteger, primary_key=True, autoincrement=True)
    user_id = db.Column(db.Integer, nullable=False, index=True)
    tenant_id = db.Column(db.Integer, nullable=True)

    plan_slug = db.Column(db.String(64), nullable=False)
    billing_period = db.Column(db.String(16), nullable=False, default="monthly")
    amount = db.Column(db.Numeric(12, 2), nullable=False)      # per-period total
    currency = db.Column(db.String(8), nullable=False, default="INR")

    status = db.Column(db.String(24), nullable=False, default=MANDATE_PENDING, index=True)
    # PayU SI reference used to auto-debit (mihpayid / authPayuId of the registration).
    si_token = db.Column(db.String(128), nullable=True)
    registration_txnid = db.Column(db.String(64), nullable=True, index=True)
    payu_mode = db.Column(db.String(8), nullable=False, default="test")

    next_charge_at = db.Column(db.DateTime(timezone=True), nullable=True)
    last_charge_at = db.Column(db.DateTime(timezone=True), nullable=True)
    last_charge_status = db.Column(db.String(24), nullable=True)
    consecutive_failures = db.Column(db.Integer, nullable=False, default=0)
    notified_at = db.Column(db.DateTime(timezone=True), nullable=True)  # RBI pre-debit notice

    created_at = db.Column(db.DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = db.Column(
        db.DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    def serialize(self) -> dict:
        return {
            "id": self.id,
            "plan_slug": self.plan_slug,
            "billing_period": self.billing_period,
            "amount": float(self.amount) if self.amount is not None else None,
            "currency": self.currency,
            "status": self.status,
            "mode": self.payu_mode,
            "next_charge_at": self.next_charge_at.isoformat() if self.next_charge_at else None,
            "last_charge_at": self.last_charge_at.isoformat() if self.last_charge_at else None,
            "last_charge_status": self.last_charge_status,
            "active": self.status == MANDATE_ACTIVE,
        }


class PayuMandateCharge(db.Model):
    __tablename__ = "payu_mandate_charges"
    __table_args__ = ({"extend_existing": True},)

    id = db.Column(db.BigInteger, primary_key=True, autoincrement=True)
    mandate_id = db.Column(db.BigInteger, nullable=False, index=True)
    txnid = db.Column(db.String(64), nullable=False, unique=True, index=True)
    amount = db.Column(db.Numeric(12, 2), nullable=True)
    status = db.Column(db.String(24), nullable=False, default="created")  # created|success|failed
    payu_id = db.Column(db.String(64), nullable=True)
    error = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)
