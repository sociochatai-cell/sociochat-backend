"""
Tenant Module - White-Label TENANT Subscription Plans (the license catalog)
===========================================================================

This is a SEPARATE concept from ``subscription.plan_models.SubscriptionPlan``:

* ``SubscriptionPlan`` (the existing model) = plans for END USERS — either
  SocioChat's own users (global) or a tenant's users (tenant-scoped). It governs
  per-user feature access.

* ``TenantPlan`` (this model) = the WHITE-LABEL LICENSE a tenant buys from
  SocioChat to run the platform under their own brand. It is chosen by the Super
  Admin at tenant creation/edit and governs tenant-level entitlements (price,
  term, how many end-users / workspaces the tenant may have). Assigning a
  TenantPlan does NOT touch any user's ``user.plan`` — the two layers are
  isolated on purpose.

``tenant_id`` semantics mirror SubscriptionPlan:
  * NULL  -> a UNIVERSAL license plan in the shared catalog (every tenant can be
            put on it).
  * set   -> a CUSTOM license plan negotiated for exactly ONE tenant.
"""

from datetime import datetime

from models import db


# Universal license slugs seeded by ``seed_default_tenant_plans`` at startup.
UNLIMITED = -1


class TenantPlan(db.Model):
    """A white-label license tier a tenant subscribes to (NOT an end-user plan)."""

    __tablename__ = "tenant_plans"

    id = db.Column(db.Integer, primary_key=True)
    slug = db.Column(db.String(48), unique=True, nullable=False, index=True)
    name = db.Column(db.String(80), nullable=False)
    description = db.Column(db.Text, nullable=True)

    # Pricing (placeholder for the future payment gateway — no charge happens yet).
    price_inr = db.Column(db.Integer, nullable=True)
    billing_period = db.Column(db.String(16), nullable=False, default="monthly")  # monthly|yearly|custom

    # Tenant-level entitlements (-1 = unlimited).
    max_end_users = db.Column(db.Integer, nullable=False, default=UNLIMITED)
    max_workspaces = db.Column(db.Integer, nullable=False, default=UNLIMITED)

    is_active = db.Column(db.Boolean, nullable=False, default=True)
    is_public = db.Column(db.Boolean, nullable=False, default=True)

    # Feature/limit matrix: { "<feature_key>": { "enabled": bool, "limit_value": int|null } }.
    features = db.Column(db.JSON, nullable=True)

    # NULL = universal shared catalog; set = custom plan for one tenant.
    tenant_id = db.Column(db.Integer, nullable=True, index=True)
    sort_order = db.Column(db.Integer, nullable=False, default=0)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def serialize(self) -> dict:
        return {
            "id": self.id,
            "slug": self.slug,
            "name": self.name,
            "description": self.description,
            "price_inr": self.price_inr,
            "billing_period": self.billing_period,
            "max_end_users": self.max_end_users,
            "max_workspaces": self.max_workspaces,
            "is_active": bool(self.is_active),
            "is_public": bool(self.is_public),
            "tenant_id": self.tenant_id,
            "sort_order": self.sort_order,
            "features": self.features or {},
        }


# Universal white-label license catalog seeded once at startup. (name, desc,
# price_inr, billing_period, max_end_users, max_workspaces, sort_order)
DEFAULT_TENANT_PLANS = {
    "wl_basic": (
        "White-Label Basic", "Run SocioChat under your brand for a small team.",
        9999, "monthly", 50, 50, 1,
    ),
    "wl_pro": (
        "White-Label Pro", "Higher limits and full feature set for growing resellers.",
        24999, "monthly", 500, 500, 2,
    ),
    "wl_enterprise": (
        "White-Label Enterprise", "Unlimited end-users and workspaces; custom terms.",
        None, "custom", UNLIMITED, UNLIMITED, 3,
    ),
}


def seed_default_tenant_plans() -> None:
    """Idempotently seed the universal white-label license catalog."""
    import logging
    logger = logging.getLogger(__name__)
    try:
        for slug, (name, desc, price, period, max_users, max_ws, order) in DEFAULT_TENANT_PLANS.items():
            row = TenantPlan.query.filter_by(slug=slug, tenant_id=None).first()
            if not row:
                db.session.add(TenantPlan(
                    slug=slug, name=name, description=desc, price_inr=price,
                    billing_period=period, max_end_users=max_users,
                    max_workspaces=max_ws, sort_order=order,
                    is_active=True, is_public=True, tenant_id=None,
                ))
        db.session.commit()
        logger.info("Tenant-plan (white-label license) catalog seeded")
    except Exception:
        db.session.rollback()
        logger.exception("seed_default_tenant_plans failed")
