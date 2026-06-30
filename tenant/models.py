"""
Tenant Module - Database Models
===============================

The multi-tenant white-label layer. A Tenant is an isolated, branded instance
of SocioChat. Every User belongs to exactly one Tenant; SocioChat itself is the
internal tenant T0000 and uses the very same models (no special-casing).

Design notes
------------
* Foreign keys are declared as string table refs ("tenants.id") so there is no
  import cycle with models.py (which holds the shared ``db``).
* Subscriptions and feature overrides live on the TENANT, not the user. All
  users in a tenant inherit the tenant subscription. Override hierarchy:
  Tenant feature override -> Subscription plan -> System default.
* Branding columns are flat for easy ALTER/migration; ``branding_extra`` holds
  any forward-compatible white-label fields without a schema change.
"""

from datetime import datetime

from models import db
from tenant.branding import merge_branding, DEFAULT_BRANDING


class Tenant(db.Model):
    """A white-label tenant (an isolated, branded SocioChat instance)."""

    __tablename__ = "tenants"

    id = db.Column(db.Integer, primary_key=True)

    # Identity — tenant_code is the primary login identifier (NOT a subdomain).
    tenant_code = db.Column(db.String(32), unique=True, nullable=False, index=True)
    company_name = db.Column(db.String(255), nullable=False)

    # active | suspended
    status = db.Column(db.String(32), nullable=False, default="active", index=True)

    # Optional custom domain (stored only; never the primary identifier).
    custom_domain = db.Column(db.String(255), nullable=True, index=True)

    # ---- Custom-domain lifecycle (additive; managed by domain_routes) ----
    # domain_status: none | pending | active | disabled
    domain_verified = db.Column(db.Boolean, default=False)
    ssl_enabled = db.Column(db.Boolean, default=False)
    domain_status = db.Column(db.String(32), default="none")

    # ---- Branding (saved values; draft lives client-side for preview) ----
    logo_url = db.Column(db.String(1000), nullable=True)
    logo_dark_url = db.Column(db.String(1000), nullable=True)
    favicon_url = db.Column(db.String(1000), nullable=True)
    primary_color = db.Column(db.String(32), nullable=True)
    secondary_color = db.Column(db.String(32), nullable=True)
    accent_color = db.Column(db.String(32), nullable=True)
    font_family = db.Column(db.String(120), nullable=True)
    button_style = db.Column(db.String(32), nullable=True)
    theme = db.Column(db.String(32), nullable=True)
    # ---- Appearance: UI chrome colors + typography + shape ----
    background_color = db.Column(db.String(32), nullable=True)
    surface_color = db.Column(db.String(32), nullable=True)
    text_color = db.Column(db.String(32), nullable=True)
    border_color = db.Column(db.String(32), nullable=True)
    heading_font_family = db.Column(db.String(120), nullable=True)
    corner_radius = db.Column(db.String(32), nullable=True)
    support_email = db.Column(db.String(255), nullable=True)
    # Recovery / contact phone for the tenant (shown on the forgot-password page).
    phone_number = db.Column(db.String(32), nullable=True)
    short_name = db.Column(db.String(120), nullable=True)
    tagline = db.Column(db.String(255), nullable=True)
    login_background = db.Column(db.Text, nullable=True)
    name_suffix = db.Column(db.String(32), nullable=True)
    # ---- Landing-page customization (hero section) ----
    landing_video_url = db.Column(db.String(1000), nullable=True)
    landing_image_url = db.Column(db.String(1000), nullable=True)
    landing_headline = db.Column(db.String(255), nullable=True)
    landing_subheadline = db.Column(db.Text, nullable=True)
    landing_cta_text = db.Column(db.String(500), nullable=True)
    # Forward-compatible bag for any extra white-label fields.
    branding_extra = db.Column(db.JSON, nullable=True)

    # Denormalized plan slug (authoritative source is TenantSubscription).
    subscription_plan = db.Column(db.String(32), nullable=True, default="starter")

    created_by = db.Column(db.Integer, db.ForeignKey("admins.id"), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # ------------------------------------------------------------------ #
    def branding_dict(self) -> dict:
        """Full branding payload (tenant overrides merged over defaults)."""
        overrides = {
            "company_name": self.company_name,
            "short_name": self.short_name,
            "name_suffix": self.name_suffix,
            "tagline": self.tagline,
            "logo_url": self.logo_url,
            "logo_dark_url": self.logo_dark_url,
            "favicon_url": self.favicon_url,
            "primary_color": self.primary_color,
            "secondary_color": self.secondary_color,
            "accent_color": self.accent_color,
            "font_family": self.font_family,
            "button_style": self.button_style,
            "theme": self.theme,
            "background_color": self.background_color,
            "surface_color": self.surface_color,
            "text_color": self.text_color,
            "border_color": self.border_color,
            "heading_font_family": self.heading_font_family,
            "corner_radius": self.corner_radius,
            "support_email": self.support_email,
            "login_background": self.login_background,
            "landing_video_url": self.landing_video_url,
            "landing_image_url": self.landing_image_url,
            "landing_headline": self.landing_headline,
            "landing_subheadline": self.landing_subheadline,
            "landing_cta_text": self.landing_cta_text,
        }
        if isinstance(self.branding_extra, dict):
            for k, v in self.branding_extra.items():
                if k not in overrides or overrides[k] in (None, ""):
                    overrides[k] = v
        return merge_branding(overrides)

    def public_branding(self) -> dict:
        """Branding payload safe to expose pre-auth (login page theming)."""
        return {
            "tenant_code": self.tenant_code,
            "company_name": self.company_name or DEFAULT_BRANDING["company_name"],
            "status": self.status,
            "phone_number": self.phone_number,
            "branding": self.branding_dict(),
        }

    def serialize(self, include_branding: bool = True) -> dict:
        data = {
            "id": self.id,
            "tenant_code": self.tenant_code,
            "company_name": self.company_name,
            "status": self.status,
            "phone_number": self.phone_number,
            "custom_domain": self.custom_domain,
            "domain_verified": bool(self.domain_verified),
            "ssl_enabled": bool(self.ssl_enabled),
            "domain_status": self.domain_status or "none",
            "subscription_plan": self.subscription_plan,
            "created_by": self.created_by,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
        if include_branding:
            data["branding"] = self.branding_dict()
        return data


class TenantSubscription(db.Model):
    """One subscription per tenant. All tenant users inherit it."""

    __tablename__ = "tenant_subscriptions"

    id = db.Column(db.Integer, primary_key=True)
    tenant_id = db.Column(
        db.Integer, db.ForeignKey("tenants.id", ondelete="CASCADE"),
        unique=True, nullable=False, index=True,
    )
    # The white-label LICENSE the tenant is on. ``plan_slug`` now references a
    # ``TenantPlan`` slug (the tenant license catalog) — NOT an end-user plan.
    plan_slug = db.Column(db.String(48), nullable=False, default="wl_basic", index=True)
    # global | private (mirrors User.billing_scope semantics)
    billing_scope = db.Column(db.String(16), nullable=False, default="global")
    subscription_expires_at = db.Column(db.DateTime, nullable=True)

    # ---- Payment-gateway placeholder (no real charge happens yet) ----
    # payment_status: none | trial | active | past_due | canceled
    payment_status = db.Column(db.String(16), nullable=False, default="none")
    payment_provider = db.Column(db.String(32), nullable=True)  # e.g. razorpay/stripe (future)
    payment_ref = db.Column(db.String(128), nullable=True)      # external subscription/customer id
    started_at = db.Column(db.DateTime, nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def serialize(self) -> dict:
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "plan_slug": self.plan_slug,
            "billing_scope": self.billing_scope,
            "subscription_expires_at": (
                self.subscription_expires_at.isoformat()
                if self.subscription_expires_at else None
            ),
            "payment_status": self.payment_status,
            "payment_provider": self.payment_provider,
            "payment_ref": self.payment_ref,
            "started_at": self.started_at.isoformat() if self.started_at else None,
        }


class TenantFeatureOverride(db.Model):
    """Super-admin feature override at the tenant level.

    ``enabled`` overrides access-type features; ``limit_value`` overrides
    limit-type features. NULL means "no override -> fall through to plan".
    """

    __tablename__ = "tenant_feature_overrides"
    __table_args__ = (
        db.UniqueConstraint("tenant_id", "feature_key", name="uq_tenant_feature_override"),
    )

    id = db.Column(db.Integer, primary_key=True)
    tenant_id = db.Column(
        db.Integer, db.ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    feature_key = db.Column(db.String(64), nullable=False, index=True)
    enabled = db.Column(db.Boolean, nullable=True)
    limit_value = db.Column(db.Integer, nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def serialize(self) -> dict:
        return {
            "feature_key": self.feature_key,
            "enabled": self.enabled,
            "limit_value": self.limit_value,
        }


class TenantPlanChangeHistory(db.Model):
    """Audit trail for tenant subscription changes."""

    __tablename__ = "tenant_plan_change_history"

    # Integer (not BigInteger) so the PK auto-increments on SQLite dev DBs too.
    id = db.Column(db.Integer, primary_key=True)
    tenant_id = db.Column(db.Integer, db.ForeignKey("tenants.id"), nullable=False, index=True)
    old_plan = db.Column(db.String(32), nullable=True)
    new_plan = db.Column(db.String(32), nullable=False)
    changed_by_admin_id = db.Column(db.Integer, db.ForeignKey("admins.id"), nullable=True)
    reason = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def serialize(self) -> dict:
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "old_plan": self.old_plan,
            "new_plan": self.new_plan,
            "changed_by_admin_id": self.changed_by_admin_id,
            "reason": self.reason,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
