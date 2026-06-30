"""
Tenant Module - Service Layer
=============================

Business logic for creating and managing tenants: tenant CRUD, branding,
per-tenant subscription + feature overrides, and the automatic creation of a
Tenant Admin + Demo User (each with their own auto-provisioned workspace, since
1 user = 1 workspace).
"""

import logging
from datetime import datetime

from werkzeug.security import generate_password_hash

from models import db, User, Workspace
from tenant.models import (
    Tenant, TenantSubscription, TenantFeatureOverride, TenantPlanChangeHistory,
)
from tenant.branding import INTERNAL_TENANT_CODE
from tenant.context import (
    generate_password, generate_tenant_code, normalize_tenant_code,
)

logger = logging.getLogger(__name__)

# Branding fields that map 1:1 onto Tenant columns (company_name handled apart).
_BRANDING_COLUMNS = [
    "short_name", "name_suffix", "tagline",
    "logo_url", "logo_dark_url", "favicon_url",
    "primary_color", "secondary_color", "accent_color",
    "font_family", "button_style", "theme",
    "background_color", "surface_color", "text_color", "border_color",
    "heading_font_family", "corner_radius",
    "support_email", "login_background",
    "landing_video_url", "landing_image_url", "landing_headline",
    "landing_subheadline", "landing_cta_text",
]


class TenantError(Exception):
    """Raised for tenant operations that should surface a 4xx to the client."""

    def __init__(self, code: str, status: int = 400):
        super().__init__(code)
        self.code = code
        self.status = status


def _parse_iso_dt(val):
    """Best-effort parse of an ISO-8601 string (accepts a trailing ``Z``).

    Returns ``None`` for empty/unparseable input so callers can treat a missing
    expiry as "never expires".
    """
    if not val:
        return None
    try:
        return datetime.fromisoformat(str(val).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


# --------------------------------------------------------------------------- #
# Branding / features helpers
# --------------------------------------------------------------------------- #
def assign_branding(tenant: Tenant, branding: dict, company_name: str | None = None) -> None:
    if company_name:
        tenant.company_name = company_name
    if branding:
        if branding.get("company_name"):
            tenant.company_name = branding["company_name"]
        extra = {}
        for key, val in branding.items():
            if key in _BRANDING_COLUMNS:
                if val is not None:
                    setattr(tenant, key, val)
            elif key not in ("company_name",):
                # Unknown white-label keys go into the forward-compatible bag.
                if val is not None:
                    extra[key] = val
        if extra:
            merged = dict(tenant.branding_extra or {})
            merged.update(extra)
            tenant.branding_extra = merged


def tenant_baseline_user_plan_slug(tenant: Tenant) -> str:
    """The tenant's baseline END-USER plan slug (for feature-matrix display).

    ``tenant.subscription_plan`` now holds a white-label LICENSE slug, so it is
    NOT a valid end-user plan. Prefer it only when it happens to resolve to a
    real ``SubscriptionPlan`` (covers internal T0000 = 'enterprise'); otherwise
    use the tenant's own lowest-order end-user plan, falling back to 'starter'.
    """
    from subscription.plan_models import SubscriptionPlan

    slug = tenant.subscription_plan
    if slug and SubscriptionPlan.query.filter_by(slug=slug, is_active=True).first():
        return slug
    row = (SubscriptionPlan.query
           .filter(SubscriptionPlan.tenant_id == tenant.id, SubscriptionPlan.is_active.is_(True))
           .order_by(SubscriptionPlan.sort_order.asc())
           .first())
    return row.slug if row else "starter"


def resolve_tenant_plan(slug: str | None) -> dict | None:
    """Serialize the white-label LICENSE (TenantPlan) for ``slug``, or None."""
    if not slug:
        return None
    from tenant.tenant_plan_models import TenantPlan
    p = TenantPlan.query.filter_by(slug=slug).first()
    return p.serialize() if p else None


def get_assignable_tenant_plans(tenant_id: int) -> list:
    """White-label LICENSE plans a tenant may be put on: universal active + own custom."""
    from tenant.tenant_plan_models import TenantPlan
    universal = (TenantPlan.query
                 .filter(TenantPlan.tenant_id.is_(None), TenantPlan.is_active.is_(True))
                 .order_by(TenantPlan.sort_order.asc(), TenantPlan.id.asc())
                 .all())
    custom = (TenantPlan.query
              .filter_by(tenant_id=tenant_id)
              .order_by(TenantPlan.created_at.asc())
              .all())
    return [p.serialize() for p in universal] + [p.serialize() for p in custom]


def is_tenant_plan_assignable(tenant_id: int, slug: str) -> bool:
    """True if ``slug`` is a universal active license OR this tenant's own custom license."""
    if not slug:
        return False
    from tenant.tenant_plan_models import TenantPlan
    p = TenantPlan.query.filter_by(slug=slug).first()
    if not p:
        return False
    if p.tenant_id is None:
        return bool(p.is_active)
    return p.tenant_id == tenant_id


def tenant_feature_matrix(tenant: Tenant) -> dict:
    """Resolve a tenant's effective feature/limit matrix.

    Starts from the tenant's subscription-plan matrix, then overlays this
    tenant's ``TenantFeatureOverride`` rows:
      * limit-type keys  -> ``limit_value`` (when set)
      * access-type keys -> bool ``enabled`` (when set)

    Imports are local to avoid an import cycle with the subscription module.
    """
    from subscription.service import load_plan_matrix, LIMIT_KEYS

    # NOTE: tenant.subscription_plan is now a white-label LICENSE slug, NOT an
    # end-user plan — resolve the tenant's baseline END-USER plan for the matrix.
    matrix = dict(load_plan_matrix(tenant_baseline_user_plan_slug(tenant)))

    for ov in TenantFeatureOverride.query.filter_by(tenant_id=tenant.id).all():
        if ov.feature_key in LIMIT_KEYS:
            if ov.limit_value is not None:
                matrix[ov.feature_key] = ov.limit_value
        elif ov.enabled is not None:
            matrix[ov.feature_key] = bool(ov.enabled)

    return matrix


def apply_feature_overrides(tenant: Tenant, features: dict) -> None:
    """Upsert tenant feature overrides. ``features`` maps feature_key -> spec.

    spec may be ``{"enabled": bool|None, "limit_value": int|None}`` or a bare
    bool (treated as ``enabled``).
    """
    for key, spec in (features or {}).items():
        if not isinstance(spec, dict):
            spec = {"enabled": bool(spec)}
        ov = TenantFeatureOverride.query.filter_by(tenant_id=tenant.id, feature_key=key).first()
        if not ov:
            ov = TenantFeatureOverride(tenant_id=tenant.id, feature_key=key)
            db.session.add(ov)
        if "enabled" in spec:
            ov.enabled = None if spec["enabled"] is None else bool(spec["enabled"])
        if "limit_value" in spec:
            ov.limit_value = spec["limit_value"]


# --------------------------------------------------------------------------- #
# Users (1 user = 1 workspace)
# --------------------------------------------------------------------------- #
def create_tenant_user(tenant: Tenant, name: str, email: str, password: str,
                       role: str = "user", status: str = "active",
                       plan: str | None = None) -> User:
    """Create a user inside a tenant with their own auto-provisioned workspace.

    ``plan`` is an END-USER plan slug from the tenant's OWN catalog (NOT the
    tenant's white-label license). Falls back to 'starter' when not supplied.
    """
    email = (email or "").strip().lower()
    if not email:
        raise TenantError("email_required")
    if User.query.filter_by(tenant_id=tenant.id, email=email).first():
        raise TenantError("email_exists_in_tenant", status=409)

    user = User(
        tenant_id=tenant.id,
        name=name or email,
        email=email,
        password_hash=generate_password_hash(password),
        role=role,
        status=status,
        email_verified=True,
        business_name=tenant.company_name,
        # End-user plan from the tenant's own catalog — distinct from the
        # tenant's white-label license (TenantSubscription / TenantPlan).
        plan=(plan or "starter"),
    )
    db.session.add(user)
    db.session.flush()  # need user.id for the workspace

    workspace = Workspace(user_id=user.id, business_name=tenant.company_name)
    db.session.add(workspace)
    return user


# --------------------------------------------------------------------------- #
# Tenant lifecycle
# --------------------------------------------------------------------------- #
def create_tenant(admin, payload: dict):
    """Create a tenant + subscription + overrides + Tenant Admin + Demo User.

    Returns ``(tenant, credentials)`` where credentials contains the one-time
    plaintext passwords for the auto-created accounts.
    """
    company = (payload.get("company_name") or "").strip()
    if not company:
        raise TenantError("company_name_required")

    code = normalize_tenant_code(payload.get("tenant_code") or "")
    if not code:
        code = generate_tenant_code(company)
    if Tenant.query.filter_by(tenant_code=code).first():
        raise TenantError("tenant_code_exists", status=409)

    # The tenant's WHITE-LABEL LICENSE slug (a TenantPlan), not an end-user plan.
    # NO auto-default: if the super admin leaves it blank, the tenant starts with
    # NO license — the tenant owner logs in (with the generated admin credentials)
    # and selects + pays for a plan themselves.
    plan = (payload.get("plan") or payload.get("tenant_plan") or "").strip()
    # Optional subscription expiry chosen at creation time ("" / missing = never).
    expires_at = _parse_iso_dt(payload.get("subscription_expires_at"))

    tenant = Tenant(
        tenant_code=code,
        company_name=company,
        status="active",
        custom_domain=(payload.get("custom_domain") or None),
        phone_number=((payload.get("phone_number") or "").strip() or None),
        subscription_plan=(plan or None),
        created_by=getattr(admin, "id", None),
    )
    assign_branding(tenant, payload.get("branding") or {}, company)
    db.session.add(tenant)
    db.session.flush()  # need tenant.id

    # A license chosen by the super admin at creation counts as granted (active);
    # if left blank, the subscription starts unselected/unpaid ("none").
    db.session.add(TenantSubscription(
        tenant_id=tenant.id, plan_slug=plan, subscription_expires_at=expires_at,
        started_at=(datetime.utcnow() if plan else None),
        payment_status=("active" if plan else "none"),
    ))
    apply_feature_overrides(tenant, payload.get("features") or {})

    # Give the tenant its OWN isolated end-user plan catalog (copies it can edit).
    default_user_plan = seed_tenant_user_plans(tenant)

    # Auto-create Tenant Admin + Demo User with generated credentials.
    admin_email = (payload.get("admin_email") or f"admin@{code.lower()}.com").strip().lower()
    demo_email = (payload.get("demo_email") or f"user@{code.lower()}.com").strip().lower()
    admin_pw = generate_password()
    demo_pw = generate_password()

    create_tenant_user(tenant, f"{company} Admin", admin_email, admin_pw,
                       role="tenant_admin", plan=default_user_plan)
    create_tenant_user(tenant, f"{company} Demo User", demo_email, demo_pw,
                       role="user", plan=default_user_plan)

    db.session.commit()

    credentials = {
        "admin": {"tenant_code": code, "email": admin_email, "password": admin_pw},
        "demo": {"tenant_code": code, "email": demo_email, "password": demo_pw},
    }
    logger.info("tenant_created code=%s by_admin=%s", code, getattr(admin, "id", None))
    return tenant, credentials


def update_tenant(tenant: Tenant, payload: dict) -> Tenant:
    if "company_name" in payload and payload["company_name"]:
        tenant.company_name = payload["company_name"].strip()
    if "custom_domain" in payload:
        tenant.custom_domain = payload["custom_domain"] or None
    if "phone_number" in payload:
        tenant.phone_number = (str(payload.get("phone_number") or "").strip() or None)
    if "status" in payload and payload["status"] in ("active", "suspended"):
        tenant.status = payload["status"]
    if "branding" in payload and isinstance(payload["branding"], dict):
        assign_branding(tenant, payload["branding"])
    if "features" in payload and isinstance(payload["features"], dict):
        apply_feature_overrides(tenant, payload["features"])
    db.session.commit()
    return tenant


def set_tenant_subscription(tenant: Tenant, plan_slug: str,
                            expires_at: datetime | None = None,
                            admin=None, reason: str | None = None,
                            payment_status: str | None = None) -> TenantSubscription:
    """Assign a WHITE-LABEL LICENSE (a ``TenantPlan`` slug) to a tenant.

    This governs the tenant's own license/billing ONLY. It deliberately does
    NOT touch any end-user's ``user.plan`` — the white-label license and the
    tenant's end-user plans are separate, isolated layers.
    """
    sub = TenantSubscription.query.filter_by(tenant_id=tenant.id).first()
    old = sub.plan_slug if sub else None
    if not sub:
        sub = TenantSubscription(tenant_id=tenant.id, plan_slug=plan_slug,
                                 started_at=datetime.utcnow())
        db.session.add(sub)
    sub.plan_slug = plan_slug
    if expires_at is not None:
        sub.subscription_expires_at = expires_at
    else:
        from subscription.service import expiry_from_period
        from tenant.tenant_plan_models import TenantPlan
        _tp = TenantPlan.query.filter_by(slug=plan_slug).first()
        _auto = expiry_from_period(datetime.utcnow(), getattr(_tp, "billing_period", None))
        if _auto is not None:
            sub.subscription_expires_at = _auto
    if payment_status is not None:
        sub.payment_status = payment_status
    if sub.started_at is None:
        sub.started_at = datetime.utcnow()
    tenant.subscription_plan = plan_slug

    db.session.add(TenantPlanChangeHistory(
        tenant_id=tenant.id, old_plan=old, new_plan=plan_slug,
        changed_by_admin_id=getattr(admin, "id", None),
        reason=reason or "super_admin_update",
    ))
    db.session.commit()
    return sub


def seed_tenant_user_plans(tenant: Tenant) -> str | None:
    """Clone SocioChat's public end-user plans into THIS tenant's own catalog.

    A new tenant gets an isolated set of end-user plans (their own rows, scoped
    by ``tenant_id``) that the tenant admin can rename/edit/delete — so the
    tenant's users never see SocioChat's plans. Returns the slug of the tenant's
    default end-user plan (clone of 'starter') when available.
    """
    from subscription.plan_models import SubscriptionPlan, PlanFeatureAccess

    code = (tenant.tenant_code or "").lower()
    default_slug = None
    try:
        globals_ = (
            SubscriptionPlan.query
            .filter(
                SubscriptionPlan.tenant_id.is_(None),
                SubscriptionPlan.is_public.is_(True),
                SubscriptionPlan.is_active.is_(True),
            )
            .order_by(SubscriptionPlan.sort_order.asc())
            .all()
        )
        for g in globals_:
            new_slug = f"t{code}_{g.slug}"[:32]
            existing = SubscriptionPlan.query.filter_by(slug=new_slug).first()
            if existing:
                if g.slug == "starter":
                    default_slug = existing.slug
                continue
            copy = SubscriptionPlan(
                slug=new_slug, name=g.name, description=g.description,
                price_monthly_inr=g.price_monthly_inr, is_public=True,
                is_active=True, plan_scope="private", tenant_id=tenant.id,
                sort_order=g.sort_order,
            )
            db.session.add(copy)
            db.session.flush()  # need copy.id for feature-access rows
            for acc in PlanFeatureAccess.query.filter_by(plan_id=g.id).all():
                db.session.add(PlanFeatureAccess(
                    plan_id=copy.id, feature_key=acc.feature_key,
                    enabled=acc.enabled, limit_value=acc.limit_value,
                ))
            if g.slug == "starter":
                default_slug = copy.slug
    except Exception:
        logger.exception("seed_tenant_user_plans failed tenant=%s", getattr(tenant, "id", None))
    return default_slug


def suspend_tenant(tenant: Tenant) -> None:
    tenant.status = "suspended"
    db.session.commit()


def activate_tenant(tenant: Tenant) -> None:
    tenant.status = "active"
    db.session.commit()


def delete_tenant(tenant: Tenant) -> None:
    """Hard-delete a tenant and its directly-owned accounts.

    Guarded so the internal SocioChat tenant (T0000) can never be deleted.
    Workspace-scoped business data is left in place (orphaned) rather than
    cascade-nuking dozens of tables; suspend is the recommended soft path.
    """
    if tenant.tenant_code == INTERNAL_TENANT_CODE:
        raise TenantError("cannot_delete_internal_tenant", status=403)

    users = User.query.filter_by(tenant_id=tenant.id).all()
    for user in users:
        Workspace.query.filter_by(user_id=user.id).delete(synchronize_session=False)
        db.session.delete(user)

    TenantFeatureOverride.query.filter_by(tenant_id=tenant.id).delete(synchronize_session=False)
    TenantSubscription.query.filter_by(tenant_id=tenant.id).delete(synchronize_session=False)
    TenantPlanChangeHistory.query.filter_by(tenant_id=tenant.id).delete(synchronize_session=False)
    db.session.delete(tenant)
    db.session.commit()
    logger.info("tenant_deleted code=%s", tenant.tenant_code)


# --------------------------------------------------------------------------- #
# Serialization
# --------------------------------------------------------------------------- #
def serialize_tenant_detail(tenant: Tenant) -> dict:
    sub = TenantSubscription.query.filter_by(tenant_id=tenant.id).first()
    overrides = TenantFeatureOverride.query.filter_by(tenant_id=tenant.id).all()
    users_count = User.query.filter_by(tenant_id=tenant.id).count()
    # Resolve the white-label LICENSE (TenantPlan) the subscription points at.
    tenant_plan = None
    if sub:
        try:
            from tenant.tenant_plan_models import TenantPlan
            tp = TenantPlan.query.filter_by(slug=sub.plan_slug).first()
            tenant_plan = tp.serialize() if tp else None
        except Exception:
            tenant_plan = None
    return {
        "tenant": tenant.serialize(),
        "subscription": sub.serialize() if sub else None,
        "tenant_plan": tenant_plan,
        "feature_overrides": [o.serialize() for o in overrides],
        "users_count": users_count,
    }
