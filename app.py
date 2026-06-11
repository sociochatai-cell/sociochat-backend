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

import re as _re
def _is_allowed_origin(origin):
    """Check if origin is in the allowed list (exact or regex)."""
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
    return False

if CORS_ALLOW_ALL:
    logger.warning("[CORS] DEBUG MODE: allowing all origins (*). Set CORS_ALLOW_ALL=0 to disable.")
    CORS(
        app,
        resources={r"/*": {"origins": "*"}},
        supports_credentials=False,
        allow_headers=_CORS_ALLOW_HEADERS,
        expose_headers=["Content-Type"],
        methods=_CORS_ALLOW_METHODS,
    )
else:
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
    if CORS_ALLOW_ALL:
        resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Access-Control-Allow-Headers"] = _CORS_HEADERS_VALUE
        resp.headers["Access-Control-Allow-Methods"] = _CORS_METHODS_VALUE
        resp.headers["Vary"] = "Origin"
        return resp

    logger.debug(f"[CORS] Origin header: {origin!r}, allowed: {_is_allowed_origin(origin) if origin else 'N/A'}")
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
    "pool_size": int(os.getenv("DB_POOL_SIZE", 10)),
    "max_overflow": int(os.getenv("DB_MAX_OVERFLOW", 10)),
    "pool_timeout": int(os.getenv("DB_POOL_TIMEOUT", 30)),
    "pool_pre_ping": True,
    "pool_recycle": 1800,
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
    flow_bp, flow_testing_bp, flow_endpoint_bp,
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
app.register_blueprint(dataset_bp, url_prefix="/api/whatsapp")
app.register_blueprint(coexistence_bp)
app.register_blueprint(catalog_bp, url_prefix="/api/whatsapp")
app.register_blueprint(tracking_bp)
app.register_blueprint(tracking_redirect_bp)
app.register_blueprint(scheduler_bp, url_prefix="/api/internal/scheduler")
app.register_blueprint(usage_events_internal_bp, url_prefix="/api/internal/whatsapp")

# Subscription / billing
from subscription.routes import subscription_bp
app.register_blueprint(subscription_bp)

# Register Agent Blueprint
from agent_backend import agent_bp
app.register_blueprint(agent_bp)


with app.app_context():
    # Ensure link tracking + subscription tables exist
    import shared_models  # noqa: F401
    import subscription.models  # noqa: F401
    from whatsapp import dataset_models  # noqa: F401

    db.create_all()

    # CRM models (leads/contacts) used by drip/bulk audience import
    try:
        app.db = db
        from SocioviaCrm.models import init_models as init_crm_models
        init_crm_models()
        logger.info("CRM models initialized")
    except Exception as e:
        logger.warning(f"CRM models init skipped: {e}")

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
        # Fallback: check X-User-Id header
        user_id = request.headers.get("X-User-Id")
        if user_id:
            user = User.query.get(int(user_id))
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
        user_id = request.headers.get("X-User-Id")
        if user_id:
            user = User.query.get(int(user_id))
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
        user_id = request.headers.get("X-User-Id")
        if user_id:
            user = User.query.get(int(user_id))
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
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Access-Control-Allow-Methods"] = _CORS_METHODS_VALUE
        response.headers["Access-Control-Allow-Headers"] = _CORS_HEADERS_VALUE
        return response

    workspace_id = request.args.get("workspace_id")

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
    app.run(host='0.0.0.0', port=5000, debug=True)
