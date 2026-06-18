"""DB-driven subscription plans and feature access matrix."""

from datetime import datetime, timezone
from typing import Any, Dict, Optional

from shared_models import db


class SubscriptionPlan(db.Model):
    __tablename__ = "subscription_plans"

    id = db.Column(db.Integer, primary_key=True)
    slug = db.Column(db.String(32), unique=True, nullable=False, index=True)
    name = db.Column(db.String(64), nullable=False)
    description = db.Column(db.Text, nullable=True)
    price_monthly_inr = db.Column(db.Integer, nullable=True)
    is_public = db.Column(db.Boolean, nullable=False, default=True)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    # global = public catalog; private = private-slot-only plans
    plan_scope = db.Column(db.String(16), nullable=False, default="global", index=True)
    sort_order = db.Column(db.Integer, nullable=False, default=0)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(
        db.DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "slug": self.slug,
            "name": self.name,
            "description": self.description,
            "price_monthly_inr": self.price_monthly_inr,
            "is_public": self.is_public,
            "is_active": self.is_active,
            "plan_scope": self.plan_scope,
            "sort_order": self.sort_order,
        }


class SubscriptionFeature(db.Model):
    __tablename__ = "subscription_features"

    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(64), unique=True, nullable=False, index=True)
    label = db.Column(db.String(128), nullable=False)
    category = db.Column(db.String(64), nullable=False, default="whatsapp")
    route_path = db.Column(db.String(255), nullable=True)
    feature_type = db.Column(db.String(16), nullable=False, default="access")  # access | limit
    sort_order = db.Column(db.Integer, nullable=False, default=0)
    is_active = db.Column(db.Boolean, nullable=False, default=True)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "key": self.key,
            "label": self.label,
            "category": self.category,
            "route_path": self.route_path,
            "feature_type": self.feature_type,
            "sort_order": self.sort_order,
            "is_active": self.is_active,
        }


class PlanFeatureAccess(db.Model):
    __tablename__ = "plan_feature_access"
    __table_args__ = (
        db.UniqueConstraint("plan_id", "feature_key", name="uq_plan_feature"),
    )

    id = db.Column(db.Integer, primary_key=True)
    plan_id = db.Column(db.Integer, db.ForeignKey("subscription_plans.id"), nullable=False, index=True)
    feature_key = db.Column(db.String(64), nullable=False, index=True)
    enabled = db.Column(db.Boolean, nullable=False, default=True)
    limit_value = db.Column(db.Integer, nullable=True)  # -1 = unlimited for limit-type features

    plan = db.relationship("SubscriptionPlan", backref=db.backref("feature_access", lazy="dynamic"))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "feature_key": self.feature_key,
            "enabled": self.enabled,
            "limit_value": self.limit_value,
        }


class PlanConfigAuditLog(db.Model):
    __tablename__ = "plan_config_audit_logs"

    id = db.Column(db.Integer, primary_key=True)
    admin_id = db.Column(db.Integer, db.ForeignKey("admins.id"), nullable=True)
    admin_email = db.Column(db.String(255), nullable=True)
    action = db.Column(db.String(64), nullable=False)
    plan_slug = db.Column(db.String(32), nullable=True)
    feature_key = db.Column(db.String(64), nullable=True)
    old_value = db.Column(db.Text, nullable=True)
    new_value = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "admin_email": self.admin_email,
            "action": self.action,
            "plan_slug": self.plan_slug,
            "feature_key": self.feature_key,
            "old_value": self.old_value,
            "new_value": self.new_value,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
