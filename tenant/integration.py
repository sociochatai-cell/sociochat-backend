"""
Tenant Module - Per-Tenant Meta / WhatsApp Integration Credentials
==================================================================

Each tenant can use THEIR OWN Meta app (App ID, App Secret, OAuth redirect URL,
webhook URL + verify token, Embedded-Signup config_id). The internal SocioChat
tenant (T0000) and any tenant that hasn't configured a field fall back to the
global ``.env`` values — so existing behavior is byte-identical for T0000.

This module is purely additive:
* ``TenantIntegration`` model (one row per tenant; secrets stored encrypted).
* ``get_tenant_meta_config(...)`` — the single resolver every WhatsApp/Meta code
  path uses instead of ``os.getenv(...)``. It resolves the tenant (by workspace
  id, tenant id, or phone_number_id), returns the tenant's values with PER-FIELD
  env fallback, and ``is_custom`` telling callers whether a tenant override exists.
* ``integration_bp`` — Super-Admin GET/PUT to manage a tenant's credentials, plus
  a logged-in ``GET /api/tenant/meta-config`` that returns only the PUBLIC bits
  (app_id, config_id, redirect/webhook URLs — never the secret) for the Connect UI.

Secrets are never returned by any endpoint; only ``has_*`` flags are exposed.
"""

import os
import logging

from flask import Blueprint, jsonify, request

from models import db

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Model
# --------------------------------------------------------------------------- #
class TenantIntegration(db.Model):
    """A tenant's own Meta/WhatsApp app credentials (one row per tenant)."""

    __tablename__ = "tenant_integration"

    id = db.Column(db.Integer, primary_key=True)
    tenant_id = db.Column(
        db.Integer, db.ForeignKey("tenants.id", ondelete="CASCADE"),
        unique=True, nullable=False, index=True,
    )

    meta_app_id = db.Column(db.String(64), nullable=True)
    meta_app_secret_enc = db.Column(db.Text, nullable=True)        # encrypted
    oauth_redirect_url = db.Column(db.String(500), nullable=True)
    webhook_url = db.Column(db.String(500), nullable=True)
    webhook_verify_token_enc = db.Column(db.Text, nullable=True)   # encrypted
    whatsapp_config_id = db.Column(db.String(64), nullable=True)

    # ── WhatsApp / Meta Graph API versions (per-tenant override) ──
    whatsapp_api_version = db.Column(db.String(16), nullable=True)
    fb_api_version = db.Column(db.String(16), nullable=True)

    # ── Email / SMTP (so a tenant sends mail from THEIR own domain) ──
    smtp_host = db.Column(db.String(255), nullable=True)
    smtp_port = db.Column(db.Integer, nullable=True)
    smtp_user = db.Column(db.String(255), nullable=True)
    smtp_pass_enc = db.Column(db.Text, nullable=True)              # encrypted
    mail_from = db.Column(db.String(255), nullable=True)

    # ── AI keys (tenant brings their own AI billing/quota) ──
    gemini_api_key_enc = db.Column(db.Text, nullable=True)         # encrypted
    google_sa_json_enc = db.Column(db.Text, nullable=True)         # encrypted (Sheets service account JSON)

    # ── SMS gateway (for per-tenant SMS OTP password reset) ──
    sms_provider = db.Column(db.String(32), nullable=True)         # e.g. "msg91" | "twilio"
    sms_sender_id = db.Column(db.String(32), nullable=True)        # sender id / from number / template id
    sms_api_key_enc = db.Column(db.Text, nullable=True)           # encrypted

    # ── PayU payment gateway (tenant brings their OWN PayU account so end-user
    #    subscription payments settle DIRECTLY into the tenant's bank) ──
    payu_key = db.Column(db.String(128), nullable=True)            # merchant key (semi-public)
    payu_salt_enc = db.Column(db.Text, nullable=True)             # merchant salt (SECRET, encrypted)
    payu_mode = db.Column(db.String(12), nullable=True)           # "test" | "production"
    payu_salt_version = db.Column(db.String(4), nullable=True)    # "v1" | "v2"

    created_at = db.Column(db.DateTime, default=db.func.now())
    updated_at = db.Column(db.DateTime, default=db.func.now(), onupdate=db.func.now())

    # ---- encrypted-field helpers (lazy import to avoid whatsapp package load) ----
    def set_app_secret(self, plain: str) -> None:
        from whatsapp.encryption import encrypt_token
        self.meta_app_secret_enc = encrypt_token(plain) if plain else None

    def get_app_secret(self) -> str:
        if not self.meta_app_secret_enc:
            return ""
        try:
            from whatsapp.encryption import decrypt_token
            return decrypt_token(self.meta_app_secret_enc)
        except Exception:
            logger.exception("Failed to decrypt tenant app secret")
            return ""

    def set_verify_token(self, plain: str) -> None:
        from whatsapp.encryption import encrypt_token
        self.webhook_verify_token_enc = encrypt_token(plain) if plain else None

    def get_verify_token(self) -> str:
        if not self.webhook_verify_token_enc:
            return ""
        try:
            from whatsapp.encryption import decrypt_token
            return decrypt_token(self.webhook_verify_token_enc)
        except Exception:
            logger.exception("Failed to decrypt tenant verify token")
            return ""

    # ---- generic encrypted-field helpers for the newer secret columns ----
    def _set_enc(self, attr: str, plain: str) -> None:
        from whatsapp.encryption import encrypt_token
        setattr(self, attr, encrypt_token(plain) if plain else None)

    def _get_enc(self, attr: str) -> str:
        raw = getattr(self, attr, None)
        if not raw:
            return ""
        try:
            from whatsapp.encryption import decrypt_token
            return decrypt_token(raw)
        except Exception:
            logger.exception("Failed to decrypt %s", attr)
            return ""

    def set_smtp_pass(self, plain: str) -> None:
        self._set_enc("smtp_pass_enc", plain)

    def get_smtp_pass(self) -> str:
        return self._get_enc("smtp_pass_enc")

    def set_gemini_api_key(self, plain: str) -> None:
        self._set_enc("gemini_api_key_enc", plain)

    def get_gemini_api_key(self) -> str:
        return self._get_enc("gemini_api_key_enc")

    def set_google_sa_json(self, plain: str) -> None:
        self._set_enc("google_sa_json_enc", plain)

    def get_google_sa_json(self) -> str:
        return self._get_enc("google_sa_json_enc")

    def set_sms_api_key(self, plain: str) -> None:
        self._set_enc("sms_api_key_enc", plain)

    def get_sms_api_key(self) -> str:
        return self._get_enc("sms_api_key_enc")

    def set_payu_salt(self, plain: str) -> None:
        self._set_enc("payu_salt_enc", plain)

    def get_payu_salt(self) -> str:
        return self._get_enc("payu_salt_enc")

    def serialize(self) -> dict:
        """Masked — never exposes the actual secret/verify-token values."""
        return {
            "tenant_id": self.tenant_id,
            "meta_app_id": self.meta_app_id or "",
            "oauth_redirect_url": self.oauth_redirect_url or "",
            "webhook_url": self.webhook_url or "",
            "whatsapp_config_id": self.whatsapp_config_id or "",
            "whatsapp_api_version": self.whatsapp_api_version or "",
            "fb_api_version": self.fb_api_version or "",
            "smtp_host": self.smtp_host or "",
            "smtp_port": self.smtp_port,
            "smtp_user": self.smtp_user or "",
            "mail_from": self.mail_from or "",
            "sms_provider": self.sms_provider or "",
            "sms_sender_id": self.sms_sender_id or "",
            "payu_key": self.payu_key or "",
            "payu_mode": self.payu_mode or "",
            "payu_salt_version": self.payu_salt_version or "",
            "has_app_secret": bool(self.meta_app_secret_enc),
            "has_verify_token": bool(self.webhook_verify_token_enc),
            "has_smtp_pass": bool(self.smtp_pass_enc),
            "has_gemini_api_key": bool(self.gemini_api_key_enc),
            "has_google_sa_json": bool(self.google_sa_json_enc),
            "has_sms_api_key": bool(self.sms_api_key_enc),
            "has_payu_salt": bool(self.payu_salt_enc),
        }


# --------------------------------------------------------------------------- #
# Resolver
# --------------------------------------------------------------------------- #
class MetaConfig:
    """Resolved Meta config for a tenant; every field already env-fallback'd."""

    __slots__ = (
        "app_id", "app_secret", "redirect_url", "webhook_url",
        "verify_token", "config_id", "tenant_id", "is_custom",
        "whatsapp_api_version", "fb_api_version",
    )

    def __init__(self, **kw):
        for k in self.__slots__:
            setattr(self, k, kw.get(k))

    def public_dict(self) -> dict:
        """Safe to send to the browser — NO secret / verify token."""
        return {
            "app_id": self.app_id or "",
            "config_id": self.config_id or "",
            "redirect_url": self.redirect_url or "",
            "webhook_url": self.webhook_url or "",
            "is_custom": bool(self.is_custom),
        }


def _env(*names) -> str:
    for n in names:
        v = os.getenv(n)
        if v:
            return v
    return ""


def _env_config() -> dict:
    """The global (.env) Meta config — the SocioChat / T0000 defaults."""
    app_base = (os.getenv("APP_BASE_URL") or "").rstrip("/")
    return {
        "app_id": _env("META_APP_ID", "FB_APP_ID"),
        "app_secret": _env("META_APP_SECRET", "FB_APP_SECRET", "WHATSAPP_APP_SECRET"),
        "redirect_url": (
            os.getenv("OAUTH_REDIRECT_BASE")
            or (f"{app_base}/api/whatsapp/connect/callback" if app_base else "")
        ),
        "webhook_url": (f"{app_base}/api/whatsapp/webhook" if app_base else ""),
        "verify_token": _env("WHATSAPP_VERIFY_TOKEN"),
        "config_id": _env("WHATSAPP_CONFIG_ID"),
        "whatsapp_api_version": _env("WHATSAPP_API_VERSION"),
        "fb_api_version": _env("FB_API_VERSION"),
    }


def _resolve_tenant_id(workspace_id=None, tenant_id=None, phone_number_id=None):
    if tenant_id:
        try:
            return int(tenant_id)
        except (TypeError, ValueError):
            return None
    from models import User, Workspace

    def _ws_to_tenant(ws_id):
        try:
            ws = db.session.get(Workspace, int(ws_id))
            if not ws:
                return None
            u = db.session.get(User, ws.user_id)
            return getattr(u, "tenant_id", None) if u else None
        except (TypeError, ValueError):
            return None

    if workspace_id:
        return _ws_to_tenant(workspace_id)

    if phone_number_id:
        try:
            from whatsapp.models import WhatsAppAccount
            acct = WhatsAppAccount.query.filter_by(phone_number_id=str(phone_number_id)).first()
            if acct and acct.workspace_id:
                return _ws_to_tenant(acct.workspace_id)
        except Exception:
            return None
    return None


def get_tenant_meta_config(workspace_id=None, tenant_id=None, phone_number_id=None) -> MetaConfig:
    """Resolve a tenant's Meta credentials, per-field env fallback.

    Pass any one of workspace_id / tenant_id / phone_number_id. For the internal
    tenant (T0000), an unknown tenant, or any blank field, the global .env value
    is used — so SocioChat / unconfigured tenants behave exactly as before.
    """
    env = _env_config()
    tid = None
    row = None
    try:
        tid = _resolve_tenant_id(workspace_id, tenant_id, phone_number_id)
        if tid:
            from tenant.models import Tenant
            from tenant.branding import INTERNAL_TENANT_CODE
            t = db.session.get(Tenant, tid)
            if t and t.tenant_code != INTERNAL_TENANT_CODE:
                row = TenantIntegration.query.filter_by(tenant_id=tid).first()
    except Exception:
        logger.exception("get_tenant_meta_config resolution failed; using env")
        row = None

    if not row:
        return MetaConfig(tenant_id=tid, is_custom=False, **env)

    return MetaConfig(
        tenant_id=tid,
        is_custom=True,
        app_id=row.meta_app_id or env["app_id"],
        app_secret=row.get_app_secret() or env["app_secret"],
        redirect_url=row.oauth_redirect_url or env["redirect_url"],
        webhook_url=row.webhook_url or env["webhook_url"],
        verify_token=row.get_verify_token() or env["verify_token"],
        config_id=row.whatsapp_config_id or env["config_id"],
        whatsapp_api_version=row.whatsapp_api_version or env["whatsapp_api_version"],
        fb_api_version=row.fb_api_version or env["fb_api_version"],
    )


# --------------------------------------------------------------------------- #
# Email / SMTP + AI resolvers (same per-field env-fallback pattern as Meta)
# --------------------------------------------------------------------------- #
class SMTPConfig:
    """Resolved per-tenant SMTP/email config; every field already env-fallback'd."""

    __slots__ = ("host", "port", "user", "password", "mail_from", "tenant_id", "is_custom")

    def __init__(self, **kw):
        for k in self.__slots__:
            setattr(self, k, kw.get(k))


def _env_smtp() -> dict:
    """Global (.env) SMTP config — the SocioChat / T0000 defaults."""
    try:
        port = int(os.getenv("SMTP_PORT", "587") or 587)
    except (TypeError, ValueError):
        port = 587
    return {
        "host": _env("SMTP_HOST"),
        "port": port,
        "user": _env("SMTP_USER"),
        "password": _env("SMTP_PASS"),
        "mail_from": _env("MAIL_FROM") or "noreply@sociochat.com",
    }


def get_tenant_smtp_config(workspace_id=None, tenant_id=None, phone_number_id=None) -> SMTPConfig:
    """Resolve a tenant's SMTP/email config with per-field env fallback.

    For T0000 / unknown tenant / any blank field the global .env value is used,
    so existing email sending is byte-identical unless a tenant configures its own.
    """
    env = _env_smtp()
    tid = None
    row = None
    try:
        tid = _resolve_tenant_id(workspace_id, tenant_id, phone_number_id)
        if tid:
            from tenant.models import Tenant
            from tenant.branding import INTERNAL_TENANT_CODE
            t = db.session.get(Tenant, tid)
            if t and t.tenant_code != INTERNAL_TENANT_CODE:
                row = TenantIntegration.query.filter_by(tenant_id=tid).first()
    except Exception:
        logger.exception("get_tenant_smtp_config resolution failed; using env")
        row = None

    if not row:
        return SMTPConfig(tenant_id=tid, is_custom=False, **env)

    return SMTPConfig(
        tenant_id=tid,
        is_custom=bool(row.smtp_host),
        host=row.smtp_host or env["host"],
        port=row.smtp_port or env["port"],
        user=row.smtp_user or env["user"],
        password=row.get_smtp_pass() or env["password"],
        mail_from=row.mail_from or env["mail_from"],
    )


class AIConfig:
    """Resolved per-tenant AI config (Gemini key + Google service-account JSON)."""

    __slots__ = ("gemini_api_key", "google_sa_json", "tenant_id", "is_custom")

    def __init__(self, **kw):
        for k in self.__slots__:
            setattr(self, k, kw.get(k))


def _env_ai() -> dict:
    return {
        "gemini_api_key": _env("GEMINI_API_KEY", "GOOGLE_API_KEY"),
        "google_sa_json": _env("SERVICE_ACCOUNT_JSON", "GOOGLE_SERVICE_ACCOUNT_JSON"),
    }


def get_tenant_ai_config(workspace_id=None, tenant_id=None, phone_number_id=None) -> AIConfig:
    """Resolve a tenant's AI keys with env fallback (T0000/unset → global .env)."""
    env = _env_ai()
    tid = None
    row = None
    try:
        tid = _resolve_tenant_id(workspace_id, tenant_id, phone_number_id)
        if tid:
            from tenant.models import Tenant
            from tenant.branding import INTERNAL_TENANT_CODE
            t = db.session.get(Tenant, tid)
            if t and t.tenant_code != INTERNAL_TENANT_CODE:
                row = TenantIntegration.query.filter_by(tenant_id=tid).first()
    except Exception:
        logger.exception("get_tenant_ai_config resolution failed; using env")
        row = None

    if not row:
        return AIConfig(tenant_id=tid, is_custom=False, **env)

    return AIConfig(
        tenant_id=tid,
        is_custom=bool(row.gemini_api_key_enc or row.google_sa_json_enc),
        gemini_api_key=row.get_gemini_api_key() or env["gemini_api_key"],
        google_sa_json=row.get_google_sa_json() or env["google_sa_json"],
    )


class SMSConfig:
    """Resolved per-tenant SMS-gateway config; every field already env-fallback'd."""

    __slots__ = ("provider", "api_key", "sender_id", "tenant_id", "is_custom", "is_internal", "configured")

    def __init__(self, **kw):
        for k in self.__slots__:
            setattr(self, k, kw.get(k))


def _env_sms() -> dict:
    return {
        "provider": (_env("SMS_PROVIDER") or "smshorizon").lower(),
        "api_key": _env("SMS_API_KEY", "MSG91_AUTH_KEY", "TWILIO_AUTH_TOKEN"),
        "sender_id": _env("SMS_SENDER_ID", "MSG91_SENDER_ID", "TWILIO_FROM"),
    }


def get_tenant_sms_config(workspace_id=None, tenant_id=None, phone_number_id=None) -> SMSConfig:
    """Resolve a tenant's SMS-gateway config.

    The INTERNAL SocioChat tenant (and the no-tenant/global case) uses the global
    .env values (the platform default). A real sub-tenant uses ONLY its own
    Integration credentials — there is NO env fallback; if it hasn't configured an
    SMS gateway, ``configured`` is False so callers can raise a clear error.
    """
    env = _env_sms()
    tid = None
    row = None
    is_internal = True
    try:
        tid = _resolve_tenant_id(workspace_id, tenant_id, phone_number_id)
        if tid:
            from tenant.models import Tenant
            from tenant.branding import INTERNAL_TENANT_CODE
            t = db.session.get(Tenant, tid)
            if t and t.tenant_code != INTERNAL_TENANT_CODE:
                is_internal = False
                row = TenantIntegration.query.filter_by(tenant_id=tid).first()
    except Exception:
        logger.exception("get_tenant_sms_config resolution failed; using env")
        row = None
        is_internal = True

    if is_internal:
        provider = (env["provider"] or "smshorizon").lower()
        api_key = env["api_key"]
        configured = bool(api_key) or provider in ("smshorizon", "horizon")
        return SMSConfig(
            tenant_id=tid, is_internal=True, is_custom=False, configured=configured,
            provider=provider, api_key=api_key, sender_id=env["sender_id"],
        )

    # Real sub-tenant: own credentials ONLY, no env fallback.
    if not row:
        return SMSConfig(
            tenant_id=tid, is_internal=False, is_custom=False, configured=False,
            provider="", api_key="", sender_id="",
        )
    api_key = row.get_sms_api_key()
    return SMSConfig(
        tenant_id=tid,
        is_internal=False,
        is_custom=bool(row.sms_api_key_enc),
        configured=bool(api_key),
        provider=(row.sms_provider or "msg91").lower(),
        api_key=api_key,
        sender_id=row.sms_sender_id or "",
    )


class PayUConfig:
    """Resolved per-tenant PayU config.

    ``configured`` tells callers whether a usable key+salt is present so they can
    raise a clear error instead of attempting a broken payment.
    """

    __slots__ = (
        "key", "salt", "mode", "salt_version", "tenant_id",
        "is_internal", "is_custom", "configured",
    )

    def __init__(self, **kw):
        for k in self.__slots__:
            setattr(self, k, kw.get(k))

    @property
    def base_url(self) -> str:
        """The PayU hosted-checkout endpoint for this config's mode."""
        return (
            "https://secure.payu.in/_payment"
            if (self.mode or "").lower() in ("prod", "production", "live")
            else "https://test.payu.in/_payment"
        )

    @property
    def verify_url(self) -> str:
        """The PayU server-to-server Verify Payment endpoint for this mode."""
        return (
            "https://info.payu.in/merchant/postservice.php?form=2"
            if (self.mode or "").lower() in ("prod", "production", "live")
            else "https://test.payu.in/merchant/postservice?form=2"
        )

    def public_dict(self) -> dict:
        """Safe to send to the browser — NO salt."""
        return {
            "key": self.key or "",
            "mode": self.mode or "",
            "salt_version": self.salt_version or "",
            "is_custom": bool(self.is_custom),
            "configured": bool(self.configured),
        }


def _env_payu() -> dict:
    """The platform's own (.env) PayU config — used by SocioChat / T0000 to bill
    tenants for their white-label license and to collect from SocioChat's own
    end-users."""
    # Accept either PAYU_MODE (test|production) or PAYU_ENV (TEST|LIVE).
    raw_mode = (_env("PAYU_MODE", "PAYU_ENV") or "test").lower()
    mode = "production" if raw_mode in ("prod", "production", "live") else "test"
    return {
        "key": _env("PAYU_KEY", "PAYU_MERCHANT_KEY"),
        "salt": _env("PAYU_SALT", "PAYU_MERCHANT_SALT"),
        "mode": mode,
        "salt_version": (_env("PAYU_SALT_VERSION") or "v1").lower(),
    }


def get_platform_payu_config() -> PayUConfig:
    """The PLATFORM's PayU account (SocioChat). Money settles to US.

    Used for the white-label LICENSE billing (tenant -> SocioChat) and for
    SocioChat's own end-user plan payments. Reads only the global .env.
    """
    env = _env_payu()
    return PayUConfig(
        tenant_id=None, is_internal=True, is_custom=False,
        configured=bool(env["key"] and env["salt"]),
        key=env["key"], salt=env["salt"],
        mode=env["mode"], salt_version=env["salt_version"],
    )


def get_tenant_payu_config(workspace_id=None, tenant_id=None, phone_number_id=None) -> PayUConfig:
    """Resolve the PayU account that should RECEIVE an end-user's payment.

    The INTERNAL SocioChat tenant (T0000) and the no-tenant case use the platform
    .env PayU account. A real sub-tenant uses ONLY its own PayU credentials —
    there is NO env fallback; if it hasn't configured PayU, ``configured`` is
    False so callers raise a clear "tenant hasn't set up payments" error. This
    mirrors the per-tenant SMS-gateway rule exactly.
    """
    env = _env_payu()
    tid = None
    row = None
    is_internal = True
    try:
        tid = _resolve_tenant_id(workspace_id, tenant_id, phone_number_id)
        if tid:
            from tenant.models import Tenant
            from tenant.branding import INTERNAL_TENANT_CODE
            t = db.session.get(Tenant, tid)
            if t and t.tenant_code != INTERNAL_TENANT_CODE:
                is_internal = False
                row = TenantIntegration.query.filter_by(tenant_id=tid).first()
    except Exception:
        logger.exception("get_tenant_payu_config resolution failed; using env")
        row = None
        is_internal = True

    if is_internal:
        return PayUConfig(
            tenant_id=tid, is_internal=True, is_custom=False,
            configured=bool(env["key"] and env["salt"]),
            key=env["key"], salt=env["salt"],
            mode=env["mode"], salt_version=env["salt_version"],
        )

    # Real sub-tenant: own credentials ONLY, no env fallback.
    if not row:
        return PayUConfig(
            tenant_id=tid, is_internal=False, is_custom=False, configured=False,
            key="", salt="", mode="test", salt_version="v1",
        )
    salt = row.get_payu_salt()
    return PayUConfig(
        tenant_id=tid,
        is_internal=False,
        is_custom=bool(row.payu_key and row.payu_salt_enc),
        configured=bool(row.payu_key and salt),
        key=row.payu_key or "",
        salt=salt,
        mode=(row.payu_mode or "test").lower(),
        salt_version=(row.payu_salt_version or "v1").lower(),
    )


def verify_token_matches_any(token: str) -> bool:
    """True if a GET-webhook verify token matches the global env OR any tenant's.

    The webhook GET-verify request carries no tenant context, so a tenant using
    its own Meta app verifies against ITS stored token. We accept the global env
    token or any configured tenant token.
    """
    token = (token or "").strip().strip('"').strip("'")
    if not token:
        return False
    env_tok = (_env("WHATSAPP_VERIFY_TOKEN") or "").strip()
    if env_tok and token == env_tok:
        return True
    try:
        for row in TenantIntegration.query.filter(
            TenantIntegration.webhook_verify_token_enc.isnot(None)
        ).all():
            if row.get_verify_token().strip() == token:
                return True
    except Exception:
        logger.exception("verify_token_matches_any lookup failed")
    return False


# --------------------------------------------------------------------------- #
# API — Super Admin manage + public (logged-in) read
# --------------------------------------------------------------------------- #
integration_bp = Blueprint("tenant_integration", __name__)

# Fields stored in plain text (non-secret).
_PLAIN_FIELDS = (
    "meta_app_id", "oauth_redirect_url", "webhook_url", "whatsapp_config_id",
    "whatsapp_api_version", "fb_api_version",
    "smtp_host", "smtp_user", "mail_from",
    "sms_provider", "sms_sender_id",
    "payu_key", "payu_mode", "payu_salt_version",
)

from tenant.context import (  # noqa: E402
    require_super_admin, require_tenant_admin, require_tenant_user,
)


@integration_bp.route("/api/superadmin/tenants/<int:tenant_id>/integration", methods=["GET"])
@require_super_admin
def get_integration(admin, tenant_id):
    from tenant.models import Tenant
    if not db.session.get(Tenant, tenant_id):
        return jsonify({"success": False, "error": "tenant_not_found"}), 404
    row = TenantIntegration.query.filter_by(tenant_id=tenant_id).first()
    return jsonify({"success": True, "integration": row.serialize() if row else None})


@integration_bp.route("/api/superadmin/tenants/<int:tenant_id>/integration", methods=["PUT"])
@require_super_admin
def set_integration(admin, tenant_id):
    from tenant.models import Tenant
    if not db.session.get(Tenant, tenant_id):
        return jsonify({"success": False, "error": "tenant_not_found"}), 404

    data = request.get_json(silent=True) or {}
    row = TenantIntegration.query.filter_by(tenant_id=tenant_id).first()
    if not row:
        row = TenantIntegration(tenant_id=tenant_id)
        db.session.add(row)

    for f in _PLAIN_FIELDS:
        if f in data:
            setattr(row, f, (str(data.get(f) or "")).strip() or None)

    # smtp_port is an integer (blank/invalid clears it).
    if "smtp_port" in data:
        raw_port = str(data.get("smtp_port") or "").strip()
        try:
            row.smtp_port = int(raw_port) if raw_port else None
        except (TypeError, ValueError):
            row.smtp_port = None

    # Secrets: only overwrite when a non-empty value is supplied (so leaving the
    # field blank in the UI doesn't wipe an existing secret).
    if data.get("meta_app_secret"):
        row.set_app_secret(data["meta_app_secret"].strip())
    if data.get("webhook_verify_token"):
        row.set_verify_token(data["webhook_verify_token"].strip())
    if data.get("smtp_pass"):
        row.set_smtp_pass(data["smtp_pass"].strip())
    if data.get("gemini_api_key"):
        row.set_gemini_api_key(data["gemini_api_key"].strip())
    if data.get("google_sa_json"):
        row.set_google_sa_json(data["google_sa_json"].strip())
    if data.get("sms_api_key"):
        row.set_sms_api_key(data["sms_api_key"].strip())
    if data.get("payu_salt"):
        row.set_payu_salt(data["payu_salt"].strip())

    db.session.commit()
    return jsonify({"success": True, "integration": row.serialize()})


@integration_bp.route("/api/tenant/admin/payu", methods=["GET"])
@require_tenant_admin
def tenant_get_payu(admin_user):
    """Tenant admin reads their OWN PayU config (masked — never the salt)."""
    tid = getattr(admin_user, "tenant_id", None)
    row = TenantIntegration.query.filter_by(tenant_id=tid).first() if tid else None
    cfg = get_tenant_payu_config(tenant_id=tid)
    return jsonify({
        "success": True,
        "payu": {
            "payu_key": (row.payu_key if row else "") or "",
            "payu_mode": (row.payu_mode if row else "") or "test",
            "payu_salt_version": (row.payu_salt_version if row else "") or "v1",
            "has_payu_salt": bool(row.payu_salt_enc) if row else False,
            "configured": bool(cfg.configured),
        },
    })


@integration_bp.route("/api/tenant/admin/payu", methods=["PUT"])
@require_tenant_admin
def tenant_set_payu(admin_user):
    """Tenant admin saves their OWN PayU credentials so their end-users' plan
    payments settle directly into the tenant's PayU account. The salt is stored
    encrypted and only overwritten when a non-empty value is supplied."""
    tid = getattr(admin_user, "tenant_id", None)
    if not tid:
        return jsonify({"success": False, "error": "no_tenant"}), 400

    data = request.get_json(silent=True) or {}
    row = TenantIntegration.query.filter_by(tenant_id=tid).first()
    if not row:
        row = TenantIntegration(tenant_id=tid)
        db.session.add(row)

    if "payu_key" in data:
        row.payu_key = (str(data.get("payu_key") or "")).strip() or None
    if "payu_mode" in data:
        mode = (str(data.get("payu_mode") or "")).strip().lower()
        row.payu_mode = mode if mode in ("test", "production") else "test"
    if "payu_salt_version" in data:
        ver = (str(data.get("payu_salt_version") or "")).strip().lower()
        row.payu_salt_version = ver if ver in ("v1", "v2") else "v1"
    if data.get("payu_salt"):
        row.set_payu_salt(data["payu_salt"].strip())

    db.session.commit()
    cfg = get_tenant_payu_config(tenant_id=tid)
    return jsonify({
        "success": True,
        "payu": {
            "payu_key": row.payu_key or "",
            "payu_mode": row.payu_mode or "test",
            "payu_salt_version": row.payu_salt_version or "v1",
            "has_payu_salt": bool(row.payu_salt_enc),
            "configured": bool(cfg.configured),
        },
    })


@integration_bp.route("/api/tenant/meta-config", methods=["GET"])
@require_tenant_user
def tenant_meta_config(user):
    """Logged-in tenant user: the PUBLIC Meta config (app_id + config_id +
    redirect/webhook URLs) for their tenant — used by the Connect button to init
    the Meta SDK with the tenant's own app. NEVER returns the secret."""
    cfg = get_tenant_meta_config(tenant_id=getattr(user, "tenant_id", None))
    return jsonify({"success": True, "meta": cfg.public_dict()})
