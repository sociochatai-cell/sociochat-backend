"""
Tenant Module - Custom-Domain Layer
===================================

Additive backend for white-label custom domains. This module is purely
additive: it does NOT touch tenant isolation, auth, workspace, WhatsApp, or the
existing branding pipeline.

Responsibilities
----------------
* ``ensure_domain_schema()`` — idempotent ALTER + backfill (mirrors
  ``tenant/migrations.py`` / ``subscription/schema_migrations.py``). Adds the
  three lifecycle columns to ``tenants`` and backfills the internal tenant
  (T0000) to an always-on ``active`` domain.
* Public domain resolution (``/api/tenant/by-domain``) used by the frontend to
  theme + gate by the request host. The platform host(s) (sociochat.ai,
  localhost, devtunnels) ALWAYS resolve to the internal tenant T0000. Any
  unknown / disabled / suspended domain returns 404 ``domain_not_configured``
  with NO fallback to another tenant.
* Tenant-self DNS instructions (``/api/tenant/domain-info``).
* Super-admin domain management (set / verify / ssl / disable).

The module is import-safe: no DB access happens at import time.
"""

import os
import re
import socket
import logging

from flask import Blueprint, jsonify, request
from sqlalchemy import func, inspect, text

from models import db
from tenant.models import Tenant
from tenant.branding import INTERNAL_TENANT_CODE
from tenant.context import require_super_admin, require_tenant_user

logger = logging.getLogger(__name__)

# No url_prefix — every route declares its full path.
domain_bp = Blueprint("domain", __name__)


# --------------------------------------------------------------------------- #
# Schema migration / backfill (idempotent; safe on every boot)
# --------------------------------------------------------------------------- #
def _column_names(table: str) -> set:
    try:
        insp = inspect(db.engine)
        return {c["name"] for c in insp.get_columns(table)}
    except Exception:
        return set()


def _table_exists(table: str) -> bool:
    try:
        return inspect(db.engine).has_table(table)
    except Exception:
        return False


def _add_column(table: str, column: str, ddl_type: str) -> None:
    """Add ``column`` to ``table`` (no DEFAULT) if missing; idempotent."""
    cols = _column_names(table)
    if not cols or column in cols:
        return
    try:
        db.session.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl_type}"))
        db.session.commit()
        logger.info("Added %s.%s column", table, column)
    except Exception:
        db.session.rollback()
        logger.exception("Failed adding %s.%s", table, column)


def ensure_domain_schema() -> None:
    """Add custom-domain lifecycle columns to ``tenants`` + backfill T0000.

    Columns (added without DEFAULT, then seeded via UPDATE so the boolean
    literals work on both SQLite and Postgres):
      * domain_verified  BOOLEAN
      * ssl_enabled      BOOLEAN
      * domain_status    VARCHAR(32)
    """
    if not _table_exists("tenants"):
        return

    _add_column("tenants", "domain_verified", "BOOLEAN")
    _add_column("tenants", "ssl_enabled", "BOOLEAN")
    _add_column("tenants", "domain_status", "VARCHAR(32)")

    cols = _column_names("tenants")

    # Seed sane defaults on any pre-existing rows where the new columns are NULL.
    if "domain_verified" in cols:
        try:
            db.session.execute(text(
                "UPDATE tenants SET domain_verified = FALSE WHERE domain_verified IS NULL"
            ))
            db.session.commit()
        except Exception:
            db.session.rollback()
            logger.exception("Backfill tenants.domain_verified failed")

    if "ssl_enabled" in cols:
        try:
            db.session.execute(text(
                "UPDATE tenants SET ssl_enabled = FALSE WHERE ssl_enabled IS NULL"
            ))
            db.session.commit()
        except Exception:
            db.session.rollback()
            logger.exception("Backfill tenants.ssl_enabled failed")

    if "domain_status" in cols:
        try:
            db.session.execute(text(
                "UPDATE tenants SET domain_status = 'none' "
                "WHERE domain_status IS NULL OR domain_status = ''"
            ))
            db.session.commit()
        except Exception:
            db.session.rollback()
            logger.exception("Backfill tenants.domain_status failed")

    # Internal tenant T0000 — its domain (the platform host) just works.
    if {"domain_status", "domain_verified"} <= cols:
        try:
            db.session.execute(
                text(
                    "UPDATE tenants SET domain_status = 'active', domain_verified = TRUE "
                    "WHERE tenant_code = :code"
                ),
                {"code": INTERNAL_TENANT_CODE},
            )
            db.session.commit()
            logger.info("Backfilled internal tenant %s domain to active", INTERNAL_TENANT_CODE)
        except Exception:
            db.session.rollback()
            logger.exception("Backfill internal-tenant domain failed")

    # One-time cleanup: a non-internal tenant whose custom_domain is actually a
    # platform / localhost / dev-tunnel host (e.g. the app URL pasted by
    # mistake) was never a valid tenant domain — it resolves to T0000 — so null
    # it out. Idempotent and safe to run on every boot.
    if "custom_domain" in cols:
        try:
            rows = db.session.execute(text(
                "SELECT id, tenant_code, custom_domain FROM tenants "
                "WHERE custom_domain IS NOT NULL AND custom_domain != ''"
            )).fetchall()
            for row in rows:
                if row.tenant_code == INTERNAL_TENANT_CODE:
                    continue
                if is_platform_host(_normalize_domain(row.custom_domain or "")):
                    db.session.execute(text(
                        "UPDATE tenants SET custom_domain = NULL, domain_status = 'none', "
                        "domain_verified = FALSE, ssl_enabled = FALSE WHERE id = :id"
                    ), {"id": row.id})
                    logger.info(
                        "Cleared invalid platform-host custom_domain on tenant %s",
                        row.tenant_code,
                    )
            db.session.commit()
        except Exception:
            db.session.rollback()
            logger.exception("custom_domain platform-host cleanup failed")


# --------------------------------------------------------------------------- #
# Platform-host helpers
# --------------------------------------------------------------------------- #
def _platform_domains() -> set:
    raw = os.getenv("PLATFORM_DOMAINS", "sociochat.ai,www.sociochat.ai")
    return {d.strip().lower() for d in raw.split(",") if d.strip()}


def is_platform_host(host: str) -> bool:
    """True for the platform's own host(s) and local/dev hosts -> T0000."""
    host = (host or "").strip().lower()
    if host in _platform_domains():
        return True
    if host in {"localhost", "127.0.0.1", ""}:
        return True
    if host.endswith(".devtunnels.ms") or host.endswith(".localhost"):
        return True
    return False


def _host_from_request() -> str:
    return (request.args.get("host") or request.host or "").split(":")[0].strip().lower()


def _normalize_domain(value: str) -> str:
    """Lowercase + strip scheme and any path/query from a domain string."""
    d = (value or "").strip().lower()
    if not d:
        return ""
    if "://" in d:
        d = d.split("://", 1)[1]
    # Drop any path / query / port that snuck in.
    d = d.split("/", 1)[0]
    d = d.split("?", 1)[0]
    d = d.split(":", 1)[0]
    return d.strip().strip(".")


# A real public domain: dot-separated labels of [a-z0-9-], valid length.
_DOMAIN_RE = re.compile(
    r"^(?=.{1,253}$)(?!-)[a-z0-9-]{1,63}(?<!-)(\.(?!-)[a-z0-9-]{1,63}(?<!-))+$"
)


def _is_assignable_custom_domain(domain: str) -> tuple:
    """A tenant custom domain must be a real public domain the tenant owns —
    NOT the platform's own host, localhost, a dev tunnel, or a bare IP (those
    resolve to the platform / are meaningless as a tenant domain).

    Returns ``(ok, reason)``.
    """
    if not domain:
        return False, "empty"
    if is_platform_host(domain):
        return False, "platform_domain"
    if all(part.isdigit() for part in domain.split(".")):
        return False, "ip_not_allowed"
    if not _DOMAIN_RE.match(domain):
        return False, "invalid_format"
    return True, ""


def _cname_host_for(domain: str) -> str:
    """Derive the CNAME host label from a domain.

    ``portal.abccompany.com`` -> ``portal``; an apex domain -> ``@``.
    Heuristic: a domain with more than two labels is treated as a subdomain.
    """
    domain = (domain or "").strip().lower().strip(".")
    if not domain:
        return "@"
    labels = domain.split(".")
    if len(labels) > 2:
        return ".".join(labels[:-2])
    return "@"


# --------------------------------------------------------------------------- #
# Public: resolve tenant by request host
# --------------------------------------------------------------------------- #
@domain_bp.route("/api/tenant/by-domain", methods=["GET"])
def tenant_by_domain():
    """PUBLIC (no auth) — resolve the tenant for the current request host.

    Platform host / localhost / devtunnels -> internal tenant T0000 (always).
    A configured custom domain -> that tenant (unless disabled/suspended).
    Anything else -> 404 domain_not_configured (NO fallback).

    Local-testing override: ``?code=<TENANT_CODE>`` resolves directly to that
    tenant, but ONLY on a platform / local host (localhost, 127.0.0.1,
    *.devtunnels.ms, sociochat.ai). A real tenant custom domain ALWAYS wins and
    ignores ``?code=`` entirely, so production behavior is unchanged.
    """
    code = (request.args.get("code") or "").strip().upper()
    host = _host_from_request()

    if code and is_platform_host(host):
        # Local-testing override: resolve directly by tenant_code (platform host
        # only). Not-found or suspended mirrors the existing 404 rule.
        tenant = Tenant.query.filter_by(tenant_code=code).first()
        if not tenant or (tenant.status or "") == "suspended":
            return jsonify({"success": False, "error": "domain_not_configured"}), 404
    else:
        if is_platform_host(host):
            tenant = Tenant.query.filter_by(tenant_code=INTERNAL_TENANT_CODE).first()
        else:
            tenant = (
                Tenant.query.filter(
                    func.lower(Tenant.custom_domain) == host,
                    Tenant.domain_status != "disabled",
                    Tenant.custom_domain.isnot(None),
                ).first()
            )

        # No tenant, or a non-platform tenant that is suspended -> hard 404.
        if not tenant or (
            not is_platform_host(host) and (tenant.status or "") == "suspended"
        ):
            return jsonify({"success": False, "error": "domain_not_configured"}), 404

    from tenant.service import tenant_feature_matrix

    branding_version = 0
    if tenant.updated_at:
        try:
            branding_version = int(tenant.updated_at.timestamp())
        except Exception:
            branding_version = 0

    return jsonify({
        "success": True,
        "tenant_id": tenant.id,
        "tenant_code": tenant.tenant_code,
        "company_name": tenant.company_name,
        "phone_number": tenant.phone_number,
        "branding": tenant.branding_dict(),
        "features": tenant_feature_matrix(tenant),
        "branding_version": branding_version,
    }), 200


# --------------------------------------------------------------------------- #
# Caddy on-demand TLS gate
# --------------------------------------------------------------------------- #
@domain_bp.route("/api/tenant/domain-allowed", methods=["GET"])
def domain_allowed():
    """Caddy on-demand-TLS ``ask`` endpoint.

    Caddy calls ``GET /api/tenant/domain-allowed?domain=<sni>`` BEFORE issuing a
    TLS certificate for an incoming hostname. We return 200 only for the
    platform's own hosts or a configured (non-disabled) tenant custom domain, so
    a stranger pointing a random domain at our IP can't trigger cert issuance.
    """
    host = _normalize_domain(request.args.get("domain") or "")
    if not host:
        return ("", 400)
    # Platform / localhost / dev-tunnel hosts are always allowed.
    if is_platform_host(host):
        return ("", 200)
    # Any configured, non-disabled tenant custom domain is allowed.
    t = (
        Tenant.query.filter(
            func.lower(Tenant.custom_domain) == host,
            Tenant.custom_domain.isnot(None),
            Tenant.domain_status != "disabled",
        ).first()
    )
    if t:
        return ("", 200)
    return ("", 404)


# --------------------------------------------------------------------------- #
# Tenant-self: domain status + DNS instructions
# --------------------------------------------------------------------------- #
@domain_bp.route("/api/tenant/domain-info", methods=["GET"])
@require_tenant_user
def tenant_domain_info(user):
    """The caller's tenant domain status + DNS setup instructions."""
    tenant = db.session.get(Tenant, int(user.tenant_id)) if getattr(user, "tenant_id", None) else None
    if not tenant:
        return jsonify({"success": False, "error": "no_tenant"}), 403

    cname_target = os.getenv("PLATFORM_CNAME_TARGET", "sociochat.ai")
    a_ip = os.getenv("PLATFORM_A_IP", "")
    custom_domain = tenant.custom_domain or None

    return jsonify({
        "success": True,
        "custom_domain": custom_domain,
        "domain_status": tenant.domain_status or "none",
        "domain_verified": bool(tenant.domain_verified),
        "ssl_enabled": bool(tenant.ssl_enabled),
        "dns_instructions": {
            "cname": {
                "host": _cname_host_for(custom_domain) if custom_domain else "@",
                "target": cname_target,
            },
            "a_record": {
                "host": "@",
                "target": a_ip,
            },
        },
    }), 200


# --------------------------------------------------------------------------- #
# Super-admin: domain management
# --------------------------------------------------------------------------- #
@domain_bp.route("/api/superadmin/tenants/<int:tenant_id>/domain", methods=["PUT"])
@require_super_admin
def superadmin_set_domain(admin, tenant_id):
    """Set or clear a tenant's custom domain (puts it into 'pending')."""
    tenant = db.session.get(Tenant, tenant_id)
    if not tenant:
        return jsonify({"success": False, "error": "tenant_not_found"}), 404

    body = request.get_json(silent=True) or {}
    domain = _normalize_domain(body.get("custom_domain") or "")

    if not domain:
        # Clear the domain entirely.
        tenant.custom_domain = None
        tenant.domain_status = "none"
        tenant.domain_verified = False
        tenant.ssl_enabled = False
        db.session.commit()
        return jsonify({"success": True, "tenant": tenant.serialize()}), 200

    # Reject the platform's own / localhost / dev-tunnel / IP / malformed hosts —
    # they are not valid tenant domains (and would resolve to T0000 anyway).
    ok, reason = _is_assignable_custom_domain(domain)
    if not ok:
        return jsonify({
            "success": False,
            "error": "invalid_domain",
            "reason": reason,
            "message": (
                "Enter a real domain you own (e.g. portal.yourcompany.com). "
                "The platform / localhost / dev-tunnel URL is not a valid tenant domain."
            ),
        }), 400

    # Reject if another tenant already claims this domain.
    clash = (
        Tenant.query.filter(
            func.lower(Tenant.custom_domain) == domain,
            Tenant.id != tenant.id,
        ).first()
    )
    if clash:
        return jsonify({"success": False, "error": "domain_taken"}), 409

    tenant.custom_domain = domain
    tenant.domain_status = "pending"
    tenant.domain_verified = False
    db.session.commit()
    return jsonify({"success": True, "tenant": tenant.serialize()}), 200


@domain_bp.route(
    "/api/superadmin/tenants/<int:tenant_id>/domain/verify", methods=["POST"]
)
@require_super_admin
def superadmin_verify_domain(admin, tenant_id):
    """Best-effort DNS verification. ``?force=1`` marks active regardless."""
    tenant = db.session.get(Tenant, tenant_id)
    if not tenant:
        return jsonify({"success": False, "error": "tenant_not_found"}), 404

    force = str(request.args.get("force") or "").strip().lower() in ("1", "true", "yes", "on")
    resolved_ip = None

    if tenant.custom_domain:
        try:
            resolved_ip = socket.gethostbyname(tenant.custom_domain)
        except Exception:
            resolved_ip = None

    if resolved_ip or force:
        tenant.domain_verified = True
        tenant.domain_status = "active"
    else:
        tenant.domain_verified = False
        tenant.domain_status = "pending"

    db.session.commit()
    return jsonify({
        "success": True,
        "domain_verified": bool(tenant.domain_verified),
        "domain_status": tenant.domain_status or "none",
        "resolved_ip": resolved_ip,
    }), 200


@domain_bp.route(
    "/api/superadmin/tenants/<int:tenant_id>/domain/ssl", methods=["POST"]
)
@require_super_admin
def superadmin_domain_ssl(admin, tenant_id):
    """Set the SSL status flag (cert issuance is handled by infra/proxy)."""
    tenant = db.session.get(Tenant, tenant_id)
    if not tenant:
        return jsonify({"success": False, "error": "tenant_not_found"}), 404

    body = request.get_json(silent=True) or {}
    enabled = body.get("enabled", True)
    tenant.ssl_enabled = bool(enabled)
    db.session.commit()
    return jsonify({"success": True, "ssl_enabled": bool(tenant.ssl_enabled)}), 200


@domain_bp.route(
    "/api/superadmin/tenants/<int:tenant_id>/domain/disable", methods=["POST"]
)
@require_super_admin
def superadmin_disable_domain(admin, tenant_id):
    """Disable a tenant's domain (kept stored; by-domain will 404 for it)."""
    tenant = db.session.get(Tenant, tenant_id)
    if not tenant:
        return jsonify({"success": False, "error": "tenant_not_found"}), 404

    tenant.domain_status = "disabled"
    db.session.commit()
    return jsonify({"success": True, "domain_status": tenant.domain_status}), 200
