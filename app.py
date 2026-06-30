"""
SocioChat Backend - Standalone WhatsApp Business Platform
=========================================================
"""

import os
import json
import logging
from datetime import datetime
from flask import Flask, request, jsonify, Response, stream_with_context
from flask_cors import CORS
from werkzeug.middleware.proxy_fix import ProxyFix
from dotenv import load_dotenv

from config import Config
from models import db, User, Workspace
from notifications import notification_manager, init_notification_engine
from auth_routes import auth_bp, get_current_user
from auth_core import authenticated_user_id, authenticated_admin_id

# Set up logging
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

# Load environment variables
load_dotenv()

# Create Flask app
app = Flask(__name__)
app.config.from_object(Config)
app.secret_key = os.environ.get("SESSION_SECRET", app.config['SECRET_KEY'])
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)
# ---------- CORS ----------
# Set CORS_ALLOW_ALL=0 to restore origin whitelist (production-safe default after debugging).
CORS_ALLOW_ALL = os.getenv("CORS_ALLOW_ALL", "1").strip().lower() in ("1", "true", "yes", "on")

_CORS_ALLOW_HEADERS = [
    "Content-Type",
    "Authorization",
    "X-Requested-With",
    "X-User-Id",
    "X-User-Email",
    "X-Admin-Id",
    "X-Workspace-ID",
]
_CORS_ALLOW_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]
_CORS_HEADERS_VALUE = "Content-Type, Authorization, X-Requested-With, X-User-Id, X-User-Email, X-Admin-Id, X-Workspace-ID"
_CORS_METHODS_VALUE = "GET, POST, PUT, PATCH, DELETE, OPTIONS"

_extra_origins = [o.strip().rstrip("/") for o in os.getenv("EXTRA_CORS_ORIGINS", "").split(",") if o.strip()]
FRONTEND_ORIGINS = [
    os.getenv("FRONTEND_ORIGIN", "http://localhost:5173").rstrip("/"),
    "http://localhost:5173",
    "http://127.0.0.1:5173",
] + _extra_origins
# Also allow all devtunnels subdomains via regex pattern
FRONTEND_ORIGINS.append(r"https://.*\.devtunnels\.ms")
# Production white-label primary domain + any subdomain (www./app./tenant-*.).
FRONTEND_ORIGINS.append(r"https://([a-z0-9-]+\.)*sociochat\.ai")

import re as _re
import time as _time
from urllib.parse import urlparse as _urlparse

# --- Dynamic tenant-domain CORS ---------------------------------------------
# White-label tenants add custom domains at runtime. Allow each tenant's domain
# for CORS automatically — no code change or redeploy per tenant. Cached briefly
# so we don't hit the DB on every response.
_TENANT_HOSTS_CACHE = {"hosts": frozenset(), "ts": 0.0}
_TENANT_HOSTS_TTL = float(os.getenv("CORS_TENANT_CACHE_TTL", "60"))

def _tenant_cors_hosts():
    """Cached set of registered tenant custom-domain hostnames (lowercased, bare
    host, no scheme). Refreshed at most once per TTL. Never raises — on error it
    returns the last known set so a DB blip can't break CORS for everyone."""
    now = _time.time()
    if now - _TENANT_HOSTS_CACHE["ts"] < _TENANT_HOSTS_TTL:
        return _TENANT_HOSTS_CACHE["hosts"]
    try:
        from tenant.models import Tenant
        rows = (
            Tenant.query
            .with_entities(Tenant.custom_domain)
            .filter(
                Tenant.custom_domain.isnot(None),
                Tenant.custom_domain != "",
                Tenant.domain_status != "disabled",  # matches the app's own host resolver
            )
            .all()
        )
        _TENANT_HOSTS_CACHE["hosts"] = frozenset(r[0].strip().lower() for r in rows if r[0])
    except Exception as e:
        logger.warning(f"[CORS] tenant domain lookup failed: {e}")
    finally:
        _TENANT_HOSTS_CACHE["ts"] = now
    return _TENANT_HOSTS_CACHE["hosts"]

def _origin_host_allowed_for_tenant(origin):
    """True if the origin's host is a registered tenant custom domain
    (apex/www-insensitive)."""
    try:
        host = (_urlparse(origin).hostname or "").lower()
    except Exception:
        return False
    if not host:
        return False
    hosts = _tenant_cors_hosts()
    if host in hosts:
        return True
    bare = host[4:] if host.startswith("www.") else host
    return bare in hosts or ("www." + bare) in hosts

def _is_allowed_origin(origin):
    """Allowed if it matches the static list (exact or regex) OR is a registered
    tenant custom domain (dynamic, looked up from the DB)."""
    if not origin:
        return False
    cleaned = origin.rstrip("/")
    for allowed in FRONTEND_ORIGINS:
        if cleaned == allowed:
            return True
        try:
            if _re.fullmatch(allowed, cleaned):
                return True
        except _re.error:
            pass
    return _origin_host_allowed_for_tenant(cleaned)

if CORS_ALLOW_ALL:
    logger.warning(
        "[CORS] Dev mode: localhost + *.devtunnels.ms with credentials "
        "(no wildcard * — required for credentials: include)."
    )
else:
    logger.info("[CORS] Whitelist mode: FRONTEND_ORIGIN + EXTRA_CORS_ORIGINS + devtunnels.")

# Never use origins="*" with supports_credentials=True — browsers block it.
CORS(
    app,
    origins=FRONTEND_ORIGINS,
    supports_credentials=True,
    allow_headers=_CORS_ALLOW_HEADERS,
    expose_headers=["Content-Type"],
    methods=_CORS_ALLOW_METHODS,
)

@app.after_request
def add_cors_headers(resp):
    origin = request.headers.get("Origin")
    if origin and _is_allowed_origin(origin):
        resp.headers["Access-Control-Allow-Origin"] = origin
        resp.headers["Access-Control-Allow-Credentials"] = "true"
        resp.headers["Access-Control-Allow-Headers"] = _CORS_HEADERS_VALUE
        resp.headers["Access-Control-Allow-Methods"] = _CORS_METHODS_VALUE
    resp.headers["Vary"] = "Origin"
    return resp

# Session and Database Config
app.config.update({
    "SESSION_TYPE": os.getenv("SESSION_TYPE", "sqlalchemy"),
    "SESSION_PERMANENT": True,
    "SESSION_COOKIE_HTTPONLY": True,
    "SESSION_COOKIE_SECURE": True,
    "SESSION_COOKIE_SAMESITE": "None",
    "SESSION_KEY_PREFIX": "sv_session:",
})

app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
    # Keep the per-PROCESS footprint small. Total DB connections =
    # (pool_size + max_overflow) × gunicorn workers × running instances, and the
    # Postgres instance has a hard `max_connections` cap. A big pool here, times
    # multiple workers + a local dev server hitting the same DB, exhausts it
    # ("remaining connection slots are reserved…"). 5+5 = 10 max per process.
    "pool_size": int(os.getenv("DB_POOL_SIZE", 5)),
    "max_overflow": int(os.getenv("DB_MAX_OVERFLOW", 5)),
    "pool_timeout": int(os.getenv("DB_POOL_TIMEOUT", 30)),
    "pool_pre_ping": True,
    # Recycle connections well under typical server idle timeouts so idle ones
    # are returned to the DB instead of lingering and hogging a slot.
    "pool_recycle": int(os.getenv("DB_POOL_RECYCLE", 280)),
}

# Initialize database
db.init_app(app)

# Session graceful connection teardown handling
from sqlalchemy.exc import OperationalError

@app.teardown_appcontext
def shutdown_session(exception=None):
    try:
        db.session.remove()
    except Exception:
        pass

@app.errorhandler(OperationalError)
def handle_db_operational_error(e):
    logger.exception("Database operational error: %s", e)
    return jsonify({
        "error": "database_unavailable",
        "message": "Database connections are temporarily exhausted. Please try again shortly."
    }), 503

# ---------- Tenant workspace-ownership enforcement ----------
# Central chokepoint for the "Hybrid" isolation model: if an authenticated
# tenant user supplies an explicit workspace_id (header or query) they do NOT
# own, reject it. This closes the forged X-Workspace-ID cross-tenant hole
# without having to edit every WhatsApp/CRM/automation route. Webhooks, admin,
# internal and auth paths are skipped (no session user / different realm).
_ENFORCE_WS = os.getenv("TENANT_ENFORCE_WORKSPACE", "1").strip().lower() in ("1", "true", "yes", "on")
_WS_ENFORCE_SKIP_PREFIXES = (
    "/api/auth", "/api/admin", "/api/superadmin", "/api/tenant",
    "/api/internal", "/api/whatsapp/webhook", "/api/whatsapp/tracking",
    "/api/notifications",
)


@app.before_request
def _enforce_workspace_ownership():
    if not _ENFORCE_WS or request.method == "OPTIONS":
        return
    path = request.path or ""
    if not path.startswith("/api/"):
        return
    if any(path.startswith(p) for p in _WS_ENFORCE_SKIP_PREFIXES):
        return
    # Only enforce for authenticated tenant users; webhooks have no session user.
    from tenant.context import get_current_user
    user = get_current_user()
    if not user:
        return
    wid = request.headers.get("X-Workspace-ID") or request.args.get("workspace_id")
    if not wid:
        return
    try:
        wid_int = int(wid)
    except (ValueError, TypeError):
        return
    ws = db.session.get(Workspace, wid_int)
    if ws is None:
        return  # let the route return its own 404
    if ws.user_id != user.id:
        logger.warning("blocked_cross_workspace user=%s ws=%s owner=%s path=%s",
                       user.id, wid_int, ws.user_id, path)
        return jsonify({"success": False, "error": "forbidden_workspace"}), 403


# ---------- Fail-closed authentication gate ----------
# Default-DENY for the whole API: every /api/* route requires a logged-in
# principal (session cookie OR a SIGNED Bearer JWT — see auth_core) UNLESS its
# path is on the explicit public allowlist below. This single chokepoint closes
# the unauthenticated-access holes across all blueprints at once; per-route
# role/ownership checks still run on top for authorization.
# Emergency escape hatch: set AUTH_GATE=0 to disable (do NOT in production).
_AUTH_GATE = os.getenv("AUTH_GATE", "1").strip().lower() in ("1", "true", "yes", "on")

# (method, exact-path) pairs reachable WITHOUT logging in.
_PUBLIC_EXACT = {
    ("GET", "/api/status"),
    # Auth + onboarding
    ("POST", "/api/auth/login"), ("POST", "/api/auth/signup"),
    ("POST", "/api/auth/verify-email"), ("POST", "/api/auth/resend-code"),
    ("POST", "/api/auth/logout"), ("POST", "/api/auth/forgot-password"),
    ("GET", "/api/auth/reset-password/validate"), ("POST", "/api/auth/reset-password"),
    ("POST", "/api/verify-email"), ("POST", "/api/resend-code"),
    ("POST", "/api/logout"), ("POST", "/api/forgot-password"),
    ("GET", "/api/password/reset/validate"), ("POST", "/api/password/reset"),
    ("POST", "/api/sms/send-otp"), ("POST", "/api/sms/verify-otp"),
    ("POST", "/api/auth/sms/forgot/request"), ("POST", "/api/auth/sms/forgot/verify"),
    ("POST", "/api/admin/login"),
    # Public catalog / branding (loaded before login)
    ("GET", "/api/subscription/plans"),
    ("GET", "/api/tenant/by-domain"), ("GET", "/api/tenant/domain-allowed"),
    # Payment provider callbacks (verified by signed hash inside the handler)
    ("POST", "/api/payments/payu/return"), ("GET", "/api/payments/payu/return"),
    ("POST", "/api/payments/payu/webhook"),
    # Meta WhatsApp webhook (hub.verify_token / HMAC verified inside the handler)
    ("GET", "/api/whatsapp/webhook"), ("POST", "/api/whatsapp/webhook"),
    # External CRM lead webhooks (each verifies its own per-workspace X-Webhook-Key)
    ("POST", "/api/webhook/zapier"), ("POST", "/api/webhook/meta-lead"),
    ("POST", "/api/webhook/sheets"), ("POST", "/api/webhook/typeform"),
    ("POST", "/api/webhook/hubspot"), ("POST", "/api/webhook/pipedrive"),
    ("GET", "/api/webhook/meta/leadgen"), ("POST", "/api/webhook/meta/leadgen"),
    # Public click-tracking write (anonymous visitor clicks)
    ("POST", "/api/v1/tracking/click-to-chat"),
}
# Public path PREFIXES (families that protect themselves or are public by design).
_PUBLIC_PREFIXES = (
    "/api/tenant/branding/by-code/",   # public per-tenant branding lookup
    "/api/webhook/provider/",          # keyed CRM provider webhooks
    "/api/internal/",                  # scheduler + usage events (own secret token)
    "/api/whatsapp/flows/endpoint",    # Meta-signed WhatsApp Flow data endpoint
)
# Sensitive routes that sit under a public prefix / shared dynamic path — force auth.
_FORCE_AUTH_EXACT = {
    ("GET", "/api/v1/tracking/all"),
    ("GET", "/api/v1/tracking/debug"),
}


def _request_is_public() -> bool:
    method = request.method
    path = request.path or ""
    if (method, path) in _FORCE_AUTH_EXACT:
        return False
    if (method, path) in _PUBLIC_EXACT:
        return True
    if any(path.startswith(pre) for pre in _PUBLIC_PREFIXES):
        return True
    # Public click-tracking redirect: GET /api/v1/tracking/<opaque-id>
    if method == "GET" and path.startswith("/api/v1/tracking/"):
        last = path.rsplit("/", 1)[-1]
        if last not in ("all", "debug", "generate"):
            return True
    return False


@app.before_request
def _require_authentication():
    if not _AUTH_GATE or request.method == "OPTIONS":
        return
    path = request.path or ""
    if not path.startswith("/api/"):
        return  # non-API: "/", "/get_test", "/t/<id>" redirect, "/favicon.ico"
    if _request_is_public():
        return
    if authenticated_user_id() is not None or authenticated_admin_id() is not None:
        return
    return jsonify({"success": False, "error": "authentication_required"}), 401


# Register Auth Blueprint
app.register_blueprint(auth_bp)

# Register SMS OTP Blueprint
from sms_routes import sms_bp
app.register_blueprint(sms_bp)

# Register WhatsApp Blueprints
from whatsapp import (
    whatsapp_bp, automation_bp, ai_bp, faq_bp, knowledge_bp,
    template_bp, trigger_bp, drip_bp, interactive_automation_bp,
    bulk_bp, production_trigger_bp,
    flow_bp, flow_testing_bp, flow_endpoint_bp, flow_os_bp, bookings_bp,
    dataset_bp, coexistence_bp,
    catalog_bp, tracking_bp, tracking_redirect_bp, scheduler_bp,
)
from whatsapp.usage_events_routes import usage_events_internal_bp
app.register_blueprint(whatsapp_bp, url_prefix="/api/whatsapp")
app.register_blueprint(automation_bp, url_prefix="/api/whatsapp")
app.register_blueprint(ai_bp)
app.register_blueprint(faq_bp)
app.register_blueprint(knowledge_bp)
app.register_blueprint(template_bp)
app.register_blueprint(trigger_bp)
app.register_blueprint(production_trigger_bp)
app.register_blueprint(drip_bp)
app.register_blueprint(interactive_automation_bp, url_prefix="/api/whatsapp")
app.register_blueprint(bulk_bp)
app.register_blueprint(flow_bp)
app.register_blueprint(flow_testing_bp)
app.register_blueprint(flow_endpoint_bp)
app.register_blueprint(flow_os_bp)
app.register_blueprint(bookings_bp)
app.register_blueprint(dataset_bp, url_prefix="/api/whatsapp")
app.register_blueprint(coexistence_bp)
app.register_blueprint(catalog_bp, url_prefix="/api/whatsapp")
app.register_blueprint(tracking_bp)
app.register_blueprint(tracking_redirect_bp)
app.register_blueprint(scheduler_bp, url_prefix="/api/internal/scheduler")
app.register_blueprint(usage_events_internal_bp, url_prefix="/api/internal/whatsapp")

# Subscription / billing
from subscription.routes import subscription_bp
from admin_routes import admin_bp, ensure_default_admin
app.register_blueprint(subscription_bp)
app.register_blueprint(admin_bp)

# Tenant (multi-tenant white-label) blueprints — Super Admin + Tenant Admin
from tenant import superadmin_bp, tenant_bp
app.register_blueprint(superadmin_bp)
app.register_blueprint(tenant_bp)

# Branding image uploads (logo/favicon) + custom per-tenant subscription plans.
try:
    from tenant.upload_routes import upload_bp
    app.register_blueprint(upload_bp)
except Exception as e:
    logger.warning(f"Tenant upload routes not loaded: {e}")
try:
    from tenant.plan_admin_routes import tenant_plans_bp
    app.register_blueprint(tenant_plans_bp)
except Exception as e:
    logger.warning(f"Tenant custom-plan routes not loaded: {e}")
try:
    # White-label LICENSE catalog (TenantPlan) — distinct from end-user plans.
    from tenant.tenant_plan_routes import tenant_subscription_plans_bp
    app.register_blueprint(tenant_subscription_plans_bp)
except Exception as e:
    logger.warning(f"Tenant license-plan routes not loaded: {e}")
try:
    from tenant.domain_routes import domain_bp
    app.register_blueprint(domain_bp)
except Exception as e:
    logger.warning(f"Tenant custom-domain routes not loaded: {e}")
try:
    from tenant.integration import integration_bp
    app.register_blueprint(integration_bp)
except Exception as e:
    logger.warning(f"Tenant integration routes not loaded: {e}")
try:
    from tenant.private_slot_routes import tenant_private_slot_bp
    app.register_blueprint(tenant_private_slot_bp)
except Exception as e:
    logger.warning(f"Tenant private-slot routes not loaded: {e}")
try:
    from tenant.sms_otp import sms_auth_bp
    app.register_blueprint(sms_auth_bp)
except Exception as e:
    logger.warning(f"Tenant SMS-OTP routes not loaded: {e}")

# PayU payment gateway (per-tenant Bring-Your-Own + platform billing).
try:
    from payments import payments_bp
    app.register_blueprint(payments_bp)
except Exception as e:
    logger.warning(f"Payment routes not loaded: {e}")

# Register Agent Blueprint
from agent_backend import agent_bp
app.register_blueprint(agent_bp)


with app.app_context():
    # Ensure link tracking + subscription tables exist
    import shared_models  # noqa: F401
    import subscription.models  # noqa: F401
    import subscription.plan_models  # noqa: F401
    import tenant.models  # noqa: F401  (register tenant tables before create_all)
    import tenant.tenant_plan_models  # noqa: F401  (register tenant_plans table)
    import tenant.integration  # noqa: F401  (register tenant_integration table)
    import tenant.sms_otp  # noqa: F401  (register phone_otps table)
    import payments.models  # noqa: F401  (register payment_transactions table)
    from whatsapp import dataset_models  # noqa: F401
    from whatsapp import flow_os_models  # noqa: F401

    db.create_all()

    # Appointment-reminder columns on existing FlowOS tables (idempotent).
    # create_all() won't ALTER existing tables, so add the new columns here.
    try:
        from sqlalchemy import text as _sql_text
        with db.engine.begin() as _conn:
            _conn.execute(_sql_text(
                "ALTER TABLE whatsapp_form_bookings "
                "ADD COLUMN IF NOT EXISTS remind_at TIMESTAMP, "
                "ADD COLUMN IF NOT EXISTS reminder_job_id VARCHAR(64), "
                "ADD COLUMN IF NOT EXISTS reminded BOOLEAN NOT NULL DEFAULT FALSE"
            ))
            _conn.execute(_sql_text(
                "ALTER TABLE whatsapp_form_business_hours "
                "ADD COLUMN IF NOT EXISTS timezone VARCHAR(64) NOT NULL DEFAULT 'Asia/Kolkata'"
            ))
        logger.info("Booking-reminder schema ensured")
    except Exception as e:
        logger.warning(f"Booking-reminder schema migration skipped: {e}")

    try:
        from subscription.schema_migrations import ensure_private_slot_schema
        ensure_private_slot_schema()
    except Exception as e:
        logger.warning(f"Private slot schema migration skipped: {e}")

    # Multi-tenant schema patches + backfill (idempotent; creates tenant T0000).
    try:
        from tenant import run_tenant_migrations
        run_tenant_migrations()
    except Exception as e:
        logger.warning(f"Tenant migrations skipped: {e}")

    # Custom per-tenant subscription plan schema (adds subscription_plans.tenant_id).
    try:
        from tenant.plan_admin_routes import ensure_custom_plan_schema
        ensure_custom_plan_schema()
    except Exception as e:
        logger.warning(f"Custom plan schema migration skipped: {e}")

    # White-label LICENSE schema (tenant_plans table + tenant_subscriptions
    # payment_* columns). Run INDEPENDENTLY of run_tenant_migrations so an early
    # failure there can never skip it — missing columns here cause 503s.
    try:
        from tenant.migrations import ensure_tenant_plan_schema
        ensure_tenant_plan_schema()
    except Exception as e:
        logger.warning(f"Tenant-plan schema migration skipped: {e}")

    # Seed the universal white-label LICENSE catalog (TenantPlan rows).
    try:
        from tenant.tenant_plan_models import seed_default_tenant_plans
        seed_default_tenant_plans()
    except Exception as e:
        logger.warning(f"Tenant-plan catalog seed skipped: {e}")

    # Custom domain schema (adds tenants.domain_verified / ssl_enabled / domain_status).
    try:
        from tenant.domain_routes import ensure_domain_schema
        ensure_domain_schema()
    except Exception as e:
        logger.warning(f"Domain schema migration skipped: {e}")

    try:
        ensure_default_admin()
        from subscription.seed import seed_subscription_catalog
        seed_subscription_catalog()
    except Exception as e:
        logger.warning(f"Admin/subscription seed skipped: {e}")

    # CRM models (leads/contacts) used by drip/bulk audience import
    try:
        app.db = db
        from SocioviaCrm.models import init_models as init_crm_models
        init_crm_models()
        # CRM models register AFTER the initial db.create_all() above, so their
        # tables (leads/contacts/deals/tasks/activities/settings/...) don't exist
        # yet. Create them PER-TABLE with checkfirst so one failing table or a
        # pre-existing Postgres ENUM type can't block the rest, and we get a clear
        # log line per table instead of a single swallowed all-or-nothing error.
        crm_models = getattr(app, "crm_models", {}) or {}
        for _name, _model in crm_models.items():
            _tbl = getattr(_model, "__table__", None)
            if _tbl is None:
                continue
            try:
                _tbl.create(bind=db.engine, checkfirst=True)
                logger.info(f"CRM table ensured: {_tbl.name}")
            except Exception as _te:
                logger.warning(f"CRM table create skipped for {_name} ({_tbl.name}): {_te}")
        logger.info("CRM models initialized and tables ensured")
    except Exception as e:
        logger.warning(f"CRM models init skipped: {e}")

    # Register CRM REST API blueprint (/api/leads, /api/contacts, /api/deals,
    # /api/dashboard, /api/tasks, /api/settings, /api/webhook). Meta-ads
    # sub-blueprints (campaigns/meta_integration) are intentionally excluded.
    try:
        from SocioviaCrm import create_crm_blueprint
        app.register_blueprint(create_crm_blueprint())
        logger.info("CRM blueprint registered")
    except Exception as e:
        logger.warning(f"CRM blueprint registration skipped: {e}")

    # Start APScheduler for drip/bulk campaign jobs
    try:
        from whatsapp.scheduler import init_scheduler
        init_scheduler(app)
        logger.info("WhatsApp APScheduler initialized")
    except Exception as e:
        logger.warning(f"APScheduler init skipped: {e}")

    # Initialize notification engine
    try:
        init_notification_engine(db.engine, app.config.get("SQLALCHEMY_DATABASE_URI"))
    except Exception as e:
        logger.warning(f"Notification engine init skipped: {e}")

    # Auto-subscribe all connected WABAs to webhooks on startup
    # This ensures subscriptions stay active after tunnel URL changes or restarts
    try:
        from whatsapp.models import WhatsAppAccount
        from whatsapp.health_check import subscribe_waba_to_webhooks
        
        accounts = WhatsAppAccount.query.filter(
            WhatsAppAccount.waba_id.isnot(None),
            WhatsAppAccount.is_active == True,
        ).all()
        
        for acct in accounts:
            token = acct.get_access_token()
            if token and acct.waba_id:
                ok, msg, _ = subscribe_waba_to_webhooks(acct.waba_id, token)
                status = "✅" if ok else "⚠️"
                logger.info(f"{status} Webhook subscription for WABA {acct.waba_id}: {msg}")
        
        if accounts:
            logger.info(f"Webhook auto-subscribe complete for {len(accounts)} account(s)")
    except Exception as e:
        logger.warning(f"Webhook auto-subscribe skipped: {e}")


# ---------- Health Check ----------

@app.route("/")
def index():
    return jsonify({
        "name": "SocioChat API",
        "version": "1.0.0",
        "status": "running"
    })


@app.route("/api/status")
def api_status():
    return jsonify({"status": "ok", "service": "sociochat"})


# ---------- Auth Convenience Routes ----------
# These mirror /api/auth/* but live at the paths the frontend calls directly

@app.route("/api/me", methods=["GET"])
def api_me():
    """Get current authenticated user (convenience alias for /api/auth/me)."""
    user = get_current_user()
    if not user:
        return jsonify({"success": False, "error": "Not authenticated"}), 401

    workspaces = Workspace.query.filter_by(user_id=user.id).all()
    return jsonify({
        "success": True,
        "id": user.id,
        "user": {
            "id": user.id,
            "name": user.name,
            "email": user.email,
            "business_name": user.business_name,
            "role": getattr(user, 'role', 'user'),
        },
        "workspaces": [
            {"id": ws.id, "business_name": ws.business_name}
            for ws in workspaces
        ]
    })


@app.route("/api/workspaces", methods=["GET"])
def api_workspaces():
    """List workspaces for the authenticated user."""
    user = get_current_user()
    if not user:
        return jsonify({"success": False, "error": "Not authenticated"}), 401

    workspaces = Workspace.query.filter_by(user_id=user.id).all()
    return jsonify({
        "success": True,
        "workspaces": [
            {"id": ws.id, "name": ws.business_name or f"Workspace {ws.id}", "business_name": ws.business_name}
            for ws in workspaces
        ]
    })


@app.route("/api/workspaces/<int:workspace_id>", methods=["PUT"])
def update_workspace(workspace_id):
    """Update workspace details."""
    user = get_current_user()
    if not user:
        return jsonify({"success": False, "error": "Not authenticated"}), 401

    workspace = Workspace.query.get(workspace_id)
    if not workspace:
        return jsonify({"success": False, "error": "Workspace not found"}), 404

    if workspace.user_id != user.id:
        return jsonify({"success": False, "error": "Unauthorized"}), 403

    data = request.json
    if not data:
        return jsonify({"success": False, "error": "No data provided"}), 400

    if "name" in data or "business_name" in data:
        new_name = data.get("name") or data.get("business_name")
        workspace.business_name = new_name
        db.session.commit()

    return jsonify({
        "success": True,
        "workspace": {
            "id": workspace.id,
            "name": workspace.business_name,
            "business_name": workspace.business_name
        }
    })


@app.route("/api/workspaces", methods=["POST"])
def create_workspace():
    """Create an additional workspace for the authenticated user.

    Enforces the effective max-workspaces limit (plan -> per-tenant override ->
    per-user override, plus the white-label tenant license cap) via
    check_workspace_limit().
    """
    user = get_current_user()
    if not user:
        return jsonify({"success": False, "error": "Not authenticated"}), 401

    from subscription.service import check_workspace_limit
    allowed, current, limit = check_workspace_limit(user)
    if not allowed:
        return jsonify({
            "success": False,
            "error": "workspace_limit_exceeded",
            "current": current,
            "limit": limit,
            "message": f"Workspace limit of {limit} reached. Please upgrade your plan.",
        }), 403

    data = request.get_json(silent=True) or {}
    name = (data.get("name") or data.get("business_name") or "").strip()
    workspace = Workspace(user_id=user.id, business_name=name or None)
    db.session.add(workspace)
    db.session.commit()

    return jsonify({
        "success": True,
        "workspace": {
            "id": workspace.id,
            "name": workspace.business_name or f"Workspace {workspace.id}",
            "business_name": workspace.business_name,
        }
    }), 201


@app.route("/api/workspaces/<int:workspace_id>", methods=["DELETE"])
def delete_workspace(workspace_id):
    """Delete one of the authenticated user's workspaces.

    Guarded so a user can never delete their last remaining workspace.
    """
    user = get_current_user()
    if not user:
        return jsonify({"success": False, "error": "Not authenticated"}), 401

    workspace = Workspace.query.get(workspace_id)
    if not workspace:
        return jsonify({"success": False, "error": "Workspace not found"}), 404
    if workspace.user_id != user.id:
        return jsonify({"success": False, "error": "Unauthorized"}), 403

    if Workspace.query.filter_by(user_id=user.id).count() <= 1:
        return jsonify({
            "success": False,
            "error": "cannot_delete_last_workspace",
            "message": "You must keep at least one workspace.",
        }), 400

    try:
        db.session.delete(workspace)
        db.session.commit()
    except Exception:
        db.session.rollback()
        logger.exception("delete_workspace failed user=%s ws=%s", user.id, workspace_id)
        return jsonify({
            "success": False,
            "error": "workspace_delete_failed",
            "message": "Could not delete this workspace; it may still have linked data.",
        }), 409

    return jsonify({"success": True})


# ---------- Auth Proxy Routes ----------
# Forward /api/* auth calls to the auth blueprint handlers

from auth_routes import verify_email as _verify_email, resend_code as _resend_code
from auth_routes import logout as _logout, forgot_password as _forgot_password
from auth_routes import reset_password_validate as _reset_validate, reset_password as _reset_password

@app.route("/api/verify-email", methods=["POST"])
def proxy_verify_email():
    return _verify_email()

@app.route("/api/resend-code", methods=["POST"])
def proxy_resend_code():
    return _resend_code()

@app.route("/api/logout", methods=["POST"])
def proxy_logout():
    return _logout()

@app.route("/api/forgot-password", methods=["POST"])
def proxy_forgot_password():
    return _forgot_password()

@app.route("/api/password/reset/validate", methods=["GET"])
def proxy_reset_validate():
    return _reset_validate()

@app.route("/api/password/reset", methods=["POST"])
def proxy_reset_password():
    return _reset_password()


# ---------- Notification Stream ----------

@app.route("/api/notifications/stream", methods=["GET", "OPTIONS"])
def notification_stream():
    """Server-Sent Events (SSE) endpoint for real-time notifications."""
    if request.method == "OPTIONS":
        response = Response()
        origin = request.headers.get("Origin")
        if origin and _is_allowed_origin(origin):
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Access-Control-Allow-Credentials"] = "true"
        response.headers["Access-Control-Allow-Methods"] = _CORS_METHODS_VALUE
        response.headers["Access-Control-Allow-Headers"] = _CORS_HEADERS_VALUE
        return response

    # --- Tenant isolation: require auth + workspace ownership ---
    # Previously this stream was unauthenticated and trusted the client-supplied
    # workspace_id, allowing anyone to listen to any workspace's events.
    from tenant.context import get_current_user, user_owns_workspace
    _stream_user = get_current_user()
    if not _stream_user:
        return jsonify({"success": False, "error": "authentication_required"}), 401

    workspace_id = request.args.get("workspace_id")
    if workspace_id and not user_owns_workspace(_stream_user, workspace_id):
        return jsonify({"success": False, "error": "forbidden_workspace"}), 403

    def event_stream():
        yield f"data: {json.dumps({'type': 'connected'})}\n\n"
        for event_type, payload in notification_manager.listen_loop(workspace_id=workspace_id):
            if event_type == "heartbeat":
                yield f": heartbeat\n\n"
            elif event_type == "reconnect":
                yield f"data: {json.dumps({'type': 'reconnect', 'data': payload})}\n\n"
                return
            else:
                yield f"data: {json.dumps({'type': event_type, 'data': payload})}\n\n"

    return Response(stream_with_context(event_stream()), mimetype="text/event-stream")

@app.route("/get_test",methods=["GET"])
def hello():
     return "hello"

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True,use_reloader=False)
