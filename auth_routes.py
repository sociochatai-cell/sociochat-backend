"""
Auth Routes for SocioChat
REST API authentication endpoints (signup, login, JWT tokens).
"""

import os
import jwt
import logging
from datetime import datetime, timedelta, timezone
from flask import Blueprint, request, jsonify, session, make_response
from werkzeug.security import generate_password_hash, check_password_hash
from email_validator import validate_email, EmailNotValidError
from models import db, User, Workspace
from utils import valid_password, generate_code, load_email_template, log_action
from mailer import send_mail
from auth_core import create_user_token, authenticated_user_id

auth_bp = Blueprint("auth", __name__, url_prefix="/api/auth")
logger = logging.getLogger(__name__)

JWT_SECRET = os.getenv("SECRET_KEY", "dev-secret-change-in-production")
JWT_EXPIRY_HOURS = int(os.getenv("JWT_EXPIRY_HOURS", 168))  # 7 days


def create_jwt(user_id: int, email: str) -> str:
    """Create a JWT token for the given user."""
    payload = {
        "user_id": user_id,
        "email": email,
        "exp": datetime.now(timezone.utc) + timedelta(hours=JWT_EXPIRY_HOURS),
        "iat": datetime.now(timezone.utc),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm="HS256")


def decode_jwt(token: str) -> dict:
    """Decode and verify a JWT token."""
    return jwt.decode(token, JWT_SECRET, algorithms=["HS256"])


def get_current_user():
    """Current user from the server session OR a SIGNED Bearer JWT (lets the SPA
    authenticate when third-party cookies are blocked). See auth_core."""
    uid = authenticated_user_id()
    if uid is None:
        return None
    try:
        return User.query.get(uid)
    except Exception:
        return None


@auth_bp.route("/signup", methods=["POST"])
def signup():
    """Register a new user and trigger email verification."""
    data = request.get_json() or {}
    name = data.get("name", "").strip()
    email = data.get("email", "").strip().lower()
    password = data.get("password", "")
    business_name = data.get("business_name", "").strip()
    phone = data.get("phone", "").strip()
    industry = data.get("industry", "").strip()

    errors = []
    if not name:
        errors.append("Name is required")
    if not email:
        errors.append("Email is required")
    else:
        try:
            validate_email(email)
        except EmailNotValidError:
            errors.append("Invalid email format")
            
    if not valid_password(password):
        errors.append("Password must be at least 8 characters")
    if not business_name:
        errors.append("Business name is required")
    if not phone:
        errors.append("Phone number is required")

    # --- Tenant resolution (public signup defaults to the internal tenant) ---
    from tenant.models import Tenant
    from tenant.branding import INTERNAL_TENANT_CODE

    tenant_code = (data.get("tenant_code") or "").strip().upper()
    if tenant_code:
        tenant = Tenant.query.filter_by(tenant_code=tenant_code).first()
        if not tenant:
            errors.append("Invalid tenant code")
    else:
        tenant = Tenant.query.filter_by(tenant_code=INTERNAL_TENANT_CODE).first()

    tenant_id = tenant.id if tenant else None

    # Email is unique PER TENANT, so scope the duplicate check to the tenant.
    if email and tenant_id and User.query.filter_by(email=email, tenant_id=tenant_id).first():
        errors.append("Email already registered")

    if errors:
        return jsonify({"success": False, "errors": errors}), 400

    # Per-tenant SMS gateway is REQUIRED (no global fallback for sub-tenants). The
    # internal SocioChat tenant uses the platform default and is always configured.
    from tenant.integration import get_tenant_sms_config
    if not get_tenant_sms_config(tenant_id=tenant_id).configured:
        return jsonify({
            "success": False,
            "error": "sms_not_configured",
            "errors": ["SMS is not configured for this account. Ask your administrator to add the SMS provider, sender ID and API key in the Integration settings."],
            "message": "SMS is not configured for this account.",
        }), 400

    verification_code = generate_code()

    user = User(
        tenant_id=tenant_id,
        name=name,
        email=email,
        phone=phone,
        business_name=business_name,
        industry=industry,
        password_hash=generate_password_hash(password),
        verification_code_hash=generate_password_hash(verification_code),
        verification_expires_at=datetime.utcnow() + timedelta(minutes=15),
        email_verified=False,
        status="pending_verification",
    )
    db.session.add(user)
    db.session.commit()

    log_action("system", "user_signup", user.id, {"email": email})

    # Auto-create initial workspace placeholder
    workspace = Workspace(
        user_id=user.id,
        business_name=business_name,
    )
    db.session.add(workspace)
    db.session.commit()

    email_sent = False
    sms_sent = False

    logger.info(f"VERIFICATION_CODE for {email}: {verification_code}")

    # 1. Try to send email verification
    try:
        email_body = load_email_template("user_verify.txt", {"name": name, "code": verification_code})
        send_mail(email, "Verify your SocioChat account", email_body, tenant_id=user.tenant_id)
        email_sent = True
    except Exception:
        logger.exception("Failed to send verification email")

    # 2. Also send SAME code via SMS using the tenant's own gateway (no fallback).
    if phone:
        try:
            from sms_routes import _normalize_phone
            from tenant.sms_otp import send_sms
            normalized = _normalize_phone(phone)
            if normalized:
                user.phone = normalized
                db.session.commit()

            message_text = (
                " Dear Customer,\n"
                f"Your One-Time Password (OTP) is {verification_code}.\n"
                "Please do not share this code with anyone for security reasons.\n\n"
                "Regards,\nProfes"
            )
            ok, err = send_sms(user.tenant_id, normalized or phone, message_text)
            if ok:
                sms_sent = True
                logger.info(f"SMS OTP sent to {phone}")
            else:
                logger.warning(f"SMS send failed during signup: {err}")
        except Exception:
            logger.exception("Failed to send SMS OTP during signup")

    return jsonify({
        "success": True,
        "message": "Signup successful. Check your email/SMS for verification code.",
        "user_id": user.id,
        "email_sent": email_sent,
        "sms_sent": sms_sent,
    }), 201


@auth_bp.route("/login", methods=["POST"])
def login():
    """Login with Tenant Code + Email + Password.

    Tenant identification uses the tenant_code (NOT a subdomain), so the same
    email can exist in multiple tenants. The tenant_code is optional for
    backward compatibility: if omitted and the email resolves to exactly one
    account, that account is used; if the email is ambiguous across tenants we
    ask for the tenant code.
    """
    data = request.get_json(force=True, silent=True) or {}
    email = data.get("email", "").strip().lower()
    password = data.get("password", "")
    tenant_code = (data.get("tenant_code") or "").strip().upper()

    if not email or not password:
        return jsonify({"success": False, "error": "email_password_required"}), 400

    from tenant.models import Tenant

    tenant = None
    user = None
    if tenant_code:
        # Step 1: find tenant by code.  Step 2: find user inside that tenant.
        tenant = Tenant.query.filter_by(tenant_code=tenant_code).first()
        if not tenant:
            logger.info("login_failed_tenant code=%s ip=%s", tenant_code, request.remote_addr)
            return jsonify({"success": False, "error": "invalid_credentials"}), 401
        user = User.query.filter_by(tenant_id=tenant.id, email=email).first()
    else:
        matches = User.query.filter_by(email=email).all()
        if len(matches) > 1:
            return jsonify({
                "success": False,
                "error": "tenant_code_required",
                "message": "Multiple accounts use this email. Please enter your Tenant Code.",
            }), 409
        user = matches[0] if matches else None
        if user and getattr(user, "tenant_id", None):
            tenant = db.session.get(Tenant, user.tenant_id)

    # Step 3: validate password.
    if not user or not check_password_hash(user.password_hash, password):
        logger.info("login_failed email=%s ip=%s", email, request.remote_addr)
        return jsonify({"success": False, "error": "invalid_credentials"}), 401

    # Tenant must be active.
    if tenant and tenant.status == "suspended":
        logger.info("login_blocked_tenant code=%s", tenant.tenant_code)
        return jsonify({"success": False, "error": "tenant_suspended"}), 403

    # Check Block / Verification states
    if user.status in ["pending_verification", "under_review", "rejected"]:
        logger.info("login_blocked email=%s status=%s", email, user.status)
        return jsonify({"success": False, "status": user.status, "error": "not_approved"}), 403

    # Step 4: login — hydrate server-side session mapping.
    session.permanent = True
    session["user_id"] = user.id
    session.modified = True

    workspaces = Workspace.query.filter_by(user_id=user.id).all()

    tenant_payload = None
    if tenant:
        tenant_payload = {
            "tenant_code": tenant.tenant_code,
            "company_name": tenant.company_name,
            "branding": tenant.branding_dict(),
        }

    resp = make_response(jsonify({
        "success": True,
        "message": "Login successful",
        "token": create_user_token(user.id, user.email),
        "user": {
            "id": user.id,
            "name": user.name,
            "email": user.email,
            "business_name": user.business_name,
            "status": user.status,
            "role": user.role,
            "tenant_id": getattr(user, "tenant_id", None),
        },
        "tenant": tenant_payload,
        "workspaces": [
            {"id": ws.id, "business_name": ws.business_name}
            for ws in workspaces
        ]
    }), 200)

    return resp


@auth_bp.route("/me", methods=["GET"])
def me():
    """Get current authenticated user info."""
    user = get_current_user()
    if not user:
        # Fallback: check X-User-Id header
        user_id = request.headers.get("X-User-Id")
        if user_id:
            try:
                user = User.query.get(int(user_id))
            except Exception:
                pass
    if not user:
        return jsonify({"success": False, "error": "Not authenticated"}), 401

    workspaces = Workspace.query.filter_by(user_id=user.id).all()

    tenant_payload = None
    if getattr(user, "tenant_id", None):
        from tenant.models import Tenant
        tenant = db.session.get(Tenant, user.tenant_id)
        if tenant:
            tenant_payload = {
                "tenant_code": tenant.tenant_code,
                "company_name": tenant.company_name,
                "branding": tenant.branding_dict(),
            }

    return jsonify({
        "success": True,
        "user": {
            "id": user.id,
            "name": user.name,
            "email": user.email,
            "business_name": user.business_name,
            "role": user.role,
            "status": user.status,
            "plan": getattr(user, "plan", "beta"),
            "tenant_id": getattr(user, "tenant_id", None),
        },
        "tenant": tenant_payload,
        "workspaces": [
            {"id": ws.id, "business_name": ws.business_name}
            for ws in workspaces
        ]
    })


# ── Email OTP Verification ──

VERIFY_TTL_MIN = int(os.getenv("VERIFY_TTL_MIN", 15))

@auth_bp.route("/verify-email", methods=["POST"])
def verify_email():
    """Verify email with 6-digit OTP code."""
    data = request.get_json() or {}
    email = (data.get("email") or "").strip().lower()
    code = (data.get("code") or "").strip()

    if not email or not code:
        return jsonify({"success": False, "error": "Email and code required"}), 400

    user = User.query.filter_by(email=email).first()
    if not user:
        return jsonify({"success": False, "error": "User not found"}), 404

    if user.email_verified:
        return jsonify({"success": True, "message": "Already verified", "status": user.status}), 200

    if not user.verification_code_hash or user.verification_expires_at < datetime.utcnow():
        return jsonify({"success": False, "error": "Verification expired"}), 400

    if not check_password_hash(user.verification_code_hash, code):
        return jsonify({"success": False, "error": "Invalid code"}), 400

    user.email_verified = True
    user.status = "active"
    user.verification_code_hash = None
    user.verification_expires_at = None

    # Activate the plan chosen during signup — but NEVER a paid tier here. This
    # endpoint is public/unauthenticated; granting starter/growth/etc. would let
    # anyone self-assign a paid plan with no payment. Paid plans must go through
    # PayU (/api/payments/initiate -> verified callback). So only the free beta
    # is honored at verification; everything else falls back to beta and the user
    # pays via the subscription page afterwards.
    chosen_plan = (data.get("plan") or "").strip().lower()
    if chosen_plan == "beta" or not user.plan or user.plan == "pending_verification":
        user.plan = "beta"
        user.beta_expires_at = datetime.utcnow() + timedelta(days=30)

    db.session.commit()
    log_action("system", "email_verified", user.id)

    # Set session so user is logged in after verification
    session.permanent = True
    session["user_id"] = user.id
    session.modified = True

    return jsonify({
        "success": True,
        "message": "Email verified. Account active.",
        "token": create_user_token(user.id, user.email),
        "status": user.status,
        "plan": user.plan,
        "beta_expires_at": user.beta_expires_at.isoformat() if user.beta_expires_at else None,
        "user": {
            "id": user.id,
            "name": user.name,
            "email": user.email,
            "business_name": user.business_name,
            "role": user.role,
            "status": user.status,
            "plan": user.plan,
        }
    }), 200


@auth_bp.route("/resend-code", methods=["POST"])
def resend_code():
    """Resend email verification OTP."""
    data = request.get_json() or {}
    email = (data.get("email") or "").strip().lower()
    if not email:
        return jsonify({"success": False, "error": "email_required"}), 400

    user = User.query.filter_by(email=email).first()
    if not user:
        return jsonify({"success": False, "error": "user_not_found"}), 404
    if user.email_verified:
        return jsonify({"success": True, "message": "already_verified"}), 200

    verification_code = generate_code()
    user.verification_code_hash = generate_password_hash(verification_code)
    user.verification_expires_at = datetime.utcnow() + timedelta(minutes=VERIFY_TTL_MIN)
    db.session.commit()

    email_sent = False
    sms_sent = False

    logger.info(f"RESEND_VERIFICATION_CODE for {user.email}: {verification_code}")

    # 1. Try email
    try:
        email_body = load_email_template("user_verify.txt", {"name": user.name, "code": verification_code})
        send_mail(user.email, "Verify your SocioChat account", email_body, tenant_id=user.tenant_id)
        email_sent = True
    except Exception:
        logger.exception("Failed to send verification email on resend")

    # 2. Also send via SMS using the tenant's own gateway (no global fallback).
    if user.phone:
        try:
            from tenant.sms_otp import send_sms
            message_text = (
                " Dear Customer,\n"
                f"Your One-Time Password (OTP) is {verification_code}.\n"
                "Please do not share this code with anyone for security reasons.\n\n"
                "Regards,\nProfes"
            )
            ok, err = send_sms(user.tenant_id, user.phone, message_text)
            if ok:
                sms_sent = True
        except Exception:
            logger.exception("Failed to send SMS on resend")

    if not email_sent and not sms_sent:
        return jsonify({"success": False, "error": "delivery_failed"}), 500

    return jsonify({"success": True, "message": "code_sent", "email_sent": email_sent, "sms_sent": sms_sent}), 200


# ── Logout ──

@auth_bp.route("/logout", methods=["POST"])
def logout():
    """Clear server-side session."""
    session.pop("user_id", None)
    return jsonify({"success": True, "message": "Logged out"}), 200


# ── Forgot Password ──

RESET_TTL_SECONDS = int(os.getenv("RESET_TTL_SECONDS", 3600))
RESET_TTL_HOURS = max(1, RESET_TTL_SECONDS // 3600)

@auth_bp.route("/forgot-password", methods=["POST"])
def forgot_password():
    """Send a password reset link to the user's email."""
    from tokens import make_action_token

    from tenant.models import Tenant

    data = request.get_json() or {}
    email = (data.get("email") or "").strip().lower()
    tenant_code = (data.get("tenant_code") or "").strip().upper()
    if not email:
        return jsonify({"success": False, "error": "email_required"}), 400

    # Resolve the user WITHIN a tenant — the same email can exist in several
    # tenants, so a tenant_code disambiguates which account to reset.
    user = None
    if tenant_code:
        tenant = Tenant.query.filter_by(tenant_code=tenant_code).first()
        if tenant:
            user = User.query.filter_by(tenant_id=tenant.id, email=email).first()
    else:
        matches = User.query.filter_by(email=email).all()
        if len(matches) == 1:
            user = matches[0]
        elif len(matches) > 1:
            # Ambiguous without a tenant code — ask for it explicitly.
            return jsonify({
                "success": False,
                "error": "tenant_code_required",
                "message": "Multiple accounts use this email. Please enter your Tenant Code.",
            }), 409

    if not user:
        # Don't reveal whether the email exists.
        return jsonify({"success": True, "message": "If the email exists, a reset link has been sent."}), 200

    try:
        token = make_action_token({
            "user_id": user.id,
            "action": "reset_password",
            "issued_at": datetime.utcnow().isoformat()
        })

        frontend_origin = os.getenv("FRONTEND_ORIGIN", "http://localhost:5173")
        reset_url = f"{frontend_origin}/reset-password?token={token}"

        try:
            email_body = load_email_template(
                "password_reset.txt",
                {"name": user.name or user.email, "reset_url": reset_url, "ttl_hours": RESET_TTL_HOURS}
            )
        except Exception:
            email_body = (
                f"Hi {user.name or user.email},\n\n"
                f"Click the link below to reset your password (valid for {RESET_TTL_HOURS} hour(s)):\n\n"
                f"{reset_url}\n\n"
                "If you didn't request this, please ignore this email.\n\n"
                "Thanks,\nSocioChat Team"
            )

        send_mail(user.email, "SocioChat — Password reset instructions", email_body, tenant_id=user.tenant_id)
        log_action("system", "password_reset_requested", user.id)
    except Exception:
        logger.exception("Failed to process forgot-password for %s", email)
        return jsonify({"success": False, "error": "internal_error"}), 500

    return jsonify({"success": True, "message": "If the email exists, a reset link has been sent."}), 200


@auth_bp.route("/reset-password/validate", methods=["GET"])
def reset_password_validate():
    """Validate a password reset token."""
    from tokens import load_action_token

    token = request.args.get("token") or ""
    if not token:
        return jsonify({"valid": False, "error": "token_required"}), 400
    try:
        payload = load_action_token(token, RESET_TTL_SECONDS)
        if payload.get("action") != "reset_password":
            return jsonify({"valid": False, "error": "invalid_action"}), 400
        user = User.query.get(payload.get("user_id"))
        if not user:
            return jsonify({"valid": False, "error": "user_not_found"}), 404
        return jsonify({"valid": True, "user_id": user.id, "email": user.email}), 200
    except Exception as e:
        logger.exception("Reset token validate failed: %s", e)
        return jsonify({"valid": False, "error": "invalid_or_expired_token"}), 400


@auth_bp.route("/reset-password", methods=["POST"])
def reset_password():
    """Reset password using a valid token."""
    from tokens import load_action_token

    data = request.get_json() or {}
    token = (data.get("token") or "").strip()
    new_password = data.get("password") or ""

    if not token or not new_password:
        return jsonify({"success": False, "error": "token_and_password_required"}), 400

    if not valid_password(new_password):
        return jsonify({"success": False, "error": "password_policy_failed"}), 400

    try:
        payload = load_action_token(token, RESET_TTL_SECONDS)
    except Exception:
        return jsonify({"success": False, "error": "invalid_or_expired_token"}), 400

    if payload.get("action") != "reset_password":
        return jsonify({"success": False, "error": "invalid_action"}), 400

    user = User.query.get(payload.get("user_id"))
    if not user:
        return jsonify({"success": False, "error": "user_not_found"}), 404

    try:
        user.password_hash = generate_password_hash(new_password)
        db.session.commit()
        log_action("system", "password_reset_completed", user.id)

        try:
            email_body = load_email_template("password_reset_confirm.txt", {"name": user.name or user.email})
        except Exception:
            email_body = f"Hi {user.name or user.email},\n\nYour password was successfully changed.\n\nSocioChat Team"
        send_mail(user.email, "Your SocioChat password has been changed", email_body, tenant_id=user.tenant_id)

        return jsonify({"success": True, "message": "password_reset_success"}), 200
    except Exception as e:
        logger.exception("Failed to update password for user %s: %s", payload.get("user_id"), e)
        return jsonify({"success": False, "error": "internal_server_error"}), 500
