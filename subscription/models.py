"""
Subscription Module - Database Models
======================================

Models for tracking subscription usage and limits.
"""

from datetime import datetime, date
from shared_models import db


class SubscriptionUsage(db.Model):
    """
    Daily usage tracking per user/workspace.
    
    Tracks messages sent, image credits used per day.
    """
    __tablename__ = "subscription_usage"
    __table_args__ = (
        db.UniqueConstraint("user_id", "workspace_id", "usage_date", name="uq_subscription_usage_daily"),
    )

    id = db.Column(db.BigInteger, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    workspace_id = db.Column(db.Integer, db.ForeignKey("workspaces2.id"), nullable=True, index=True)
    usage_date = db.Column(db.Date, nullable=False, default=date.today, index=True)

    # Daily counters
    messages_sent = db.Column(db.Integer, nullable=False, default=0)
    image_credits_used = db.Column(db.Integer, nullable=False, default=0)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def to_dict(self):
        return {
            "id": self.id,
            "user_id": self.user_id,
            "workspace_id": self.workspace_id,
            "usage_date": self.usage_date.isoformat() if self.usage_date else None,
            "messages_sent": self.messages_sent,
            "image_credits_used": self.image_credits_used,
        }


class AdSpendTracking(db.Model):
    """
    Track cumulative ad spend per workspace.
    
    Resets based on billing cycle (monthly).
    """
    __tablename__ = "ad_spend_tracking"
    __table_args__ = (
        db.UniqueConstraint("workspace_id", "billing_period_start", name="uq_ad_spend_billing_period"),
    )

    id = db.Column(db.BigInteger, primary_key=True)
    workspace_id = db.Column(db.Integer, db.ForeignKey("workspaces2.id"), nullable=False, index=True)
    
    # Billing period
    billing_period_start = db.Column(db.Date, nullable=False, index=True)
    billing_period_end = db.Column(db.Date, nullable=False)

    # Cumulative spend in INR (paise for precision)
    total_spend_paise = db.Column(db.BigInteger, nullable=False, default=0)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    @property
    def total_spend_inr(self):
        """Get spend in INR (rupees)."""
        return self.total_spend_paise / 100

    def to_dict(self):
        return {
            "id": self.id,
            "workspace_id": self.workspace_id,
            "billing_period_start": self.billing_period_start.isoformat() if self.billing_period_start else None,
            "billing_period_end": self.billing_period_end.isoformat() if self.billing_period_end else None,
            "total_spend_paise": self.total_spend_paise,
            "total_spend_inr": self.total_spend_inr,
        }


class PlanChangeHistory(db.Model):
    """
    Track when admin changes user plans.
    """
    __tablename__ = "plan_change_history"

    id = db.Column(db.BigInteger, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    
    old_plan = db.Column(db.String(32), nullable=False)
    new_plan = db.Column(db.String(32), nullable=False)
    
    changed_by_admin_id = db.Column(db.Integer, db.ForeignKey("admins.id"), nullable=True)
    reason = db.Column(db.Text, nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            "id": self.id,
            "user_id": self.user_id,
            "old_plan": self.old_plan,
            "new_plan": self.new_plan,
            "changed_by_admin_id": self.changed_by_admin_id,
            "reason": self.reason,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
