"""
Payments Module - Database Models
=================================

``PaymentTransaction`` is the single source of truth for every payment attempt.
A row is created (status="initiated") before redirecting to PayU and is the only
thing that flips a user/tenant onto a paid plan — and only after the PayU
callback is hash-verified. It also gives idempotency (one activation per txn) and
an audit/reconciliation trail.
"""

from datetime import datetime
from models import db


# layer values
LAYER_USER_PLAN = "user_plan"        # end-user (SocioChat or a tenant's user) buys a plan
LAYER_TENANT_LICENSE = "tenant_license"  # tenant buys a white-label license from SocioChat

# status values
STATUS_INITIATED = "initiated"   # row created, redirected to PayU, no result yet
STATUS_SUCCESS = "success"       # verified paid + plan activated
STATUS_FAILED = "failed"         # PayU reported failure / verification failed


class PaymentTransaction(db.Model):
    __tablename__ = "payment_transactions"

    id = db.Column(db.BigInteger, primary_key=True)

    # Our own merchant transaction id (sent to PayU as ``txnid``). Unique.
    txnid = db.Column(db.String(64), unique=True, nullable=False, index=True)

    # Who is paying / who receives.
    tenant_id = db.Column(db.Integer, nullable=True, index=True)      # payer's tenant
    user_id = db.Column(db.Integer, nullable=True, index=True)        # paying user (also tenant-admin for license)
    payee_tenant_id = db.Column(db.Integer, nullable=True)           # whose PayU account collects (None = platform)

    layer = db.Column(db.String(16), nullable=False)                 # user_plan | tenant_license
    plan_slug = db.Column(db.String(48), nullable=False)
    billing_scope = db.Column(db.String(16), nullable=True)          # to restore User.pending_billing on success

    amount = db.Column(db.Integer, nullable=False)                   # INR rupees (matches plan price fields)
    currency = db.Column(db.String(8), nullable=False, default="INR")

    status = db.Column(db.String(16), nullable=False, default=STATUS_INITIATED, index=True)
    provider = db.Column(db.String(16), nullable=False, default="payu")
    payu_mode = db.Column(db.String(12), nullable=True)              # test | production

    payu_payment_id = db.Column(db.String(64), nullable=True)        # PayU's mihpayid
    payu_status = db.Column(db.String(32), nullable=True)            # raw PayU status string
    error = db.Column(db.Text, nullable=True)
    raw_response = db.Column(db.Text, nullable=True)                 # JSON dump of PayU callback for audit

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    completed_at = db.Column(db.DateTime, nullable=True)

    def serialize(self) -> dict:
        return {
            "txnid": self.txnid,
            "tenant_id": self.tenant_id,
            "user_id": self.user_id,
            "layer": self.layer,
            "plan_slug": self.plan_slug,
            "amount": self.amount,
            "currency": self.currency,
            "status": self.status,
            "payu_status": self.payu_status,
            "payu_payment_id": self.payu_payment_id,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
        }
