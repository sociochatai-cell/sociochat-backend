"""
Tenant Module - SMS OTP password reset (per-tenant SMS gateway)
===============================================================

Lets a tenant's user reset their password via a one-time code sent over SMS to
the TENANT's recovery phone (``tenants.phone_number``). The SMS gateway is
resolved PER TENANT via ``get_tenant_sms_config`` (each tenant can plug in their
own provider key in the Integration tab; falls back to the global ``.env``).

If NO gateway key is configured anywhere, the code is logged server-side (a dev
fallback) so the flow is testable without signing up for a provider — real texts
go out the moment a key is set.

Endpoints (blueprint ``sms_auth_bp``):
  * POST /api/auth/sms/forgot/request  {tenant_code, email}            -> sends OTP
  * POST /api/auth/sms/forgot/verify   {tenant_code, email, code, new_password}
"""

import os
import logging
import hashlib
import secrets
from datetime import datetime, timedelta

from flask import Blueprint, jsonify, request
from werkzeug.security import generate_password_hash

from models import db

logger = logging.getLogger(__name__)

try:
    OTP_TTL_MINUTES = int(os.getenv("SMS_OTP_TTL_MIN", "10") or 10)
except (TypeError, ValueError):
    OTP_TTL_MINUTES = 10
OTP_MAX_ATTEMPTS = 5


# --------------------------------------------------------------------------- #
# Model
# --------------------------------------------------------------------------- #
class PhoneOTP(db.Model):
    """A one-time SMS code (hashed at rest), scoped to a tenant + phone."""

    __tablename__ = "phone_otps"

    id = db.Column(db.Integer, primary_key=True)
    tenant_id = db.Column(db.Integer, index=True, nullable=True)
    phone = db.Column(db.String(32), nullable=False, index=True)
    purpose = db.Column(db.String(32), nullable=False, default="password_reset")
    code_hash = db.Column(db.String(128), nullable=False)
    expires_at = db.Column(db.DateTime, nullable=False)
    attempts = db.Column(db.Integer, default=0)
    used = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)


def _hash_code(tenant_id, phone, code) -> str:
    raw = f"{tenant_id}|{phone}|{str(code).strip()}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def generate_and_store_otp(tenant_id, phone, purpose="password_reset") -> str:
    """Create + persist a fresh 6-digit OTP; returns the plaintext code (to send)."""
    code = f"{secrets.randbelow(900000) + 100000}"  # 6 digits, no leading-zero loss
    otp = PhoneOTP(
        tenant_id=tenant_id,
        phone=str(phone),
        purpose=purpose,
        code_hash=_hash_code(tenant_id, phone, code),
        expires_at=datetime.utcnow() + timedelta(minutes=OTP_TTL_MINUTES),
    )
    db.session.add(otp)
    db.session.commit()
    return code


def verify_otp(tenant_id, phone, code, purpose="password_reset"):
    """Return (ok, reason). Marks the code used on success; counts attempts."""
    otp = (
        PhoneOTP.query
        .filter_by(tenant_id=tenant_id, phone=str(phone), purpose=purpose, used=False)
        .order_by(PhoneOTP.created_at.desc())
        .first()
    )
    if not otp:
        return False, "no_otp"
    if otp.expires_at < datetime.utcnow():
        return False, "expired"
    if (otp.attempts or 0) >= OTP_MAX_ATTEMPTS:
        return False, "too_many_attempts"
    otp.attempts = (otp.attempts or 0) + 1
    if otp.code_hash != _hash_code(tenant_id, phone, code):
        db.session.commit()
        return False, "invalid_code"
    otp.used = True
    db.session.commit()
    return True, ""


# --------------------------------------------------------------------------- #
# Sending (per-tenant gateway; MSG91 implemented, Twilio stub, dev fallback)
# --------------------------------------------------------------------------- #
def send_sms(tenant_id, phone, text):
    """Send an SMS via the tenant's resolved gateway.

    Returns (ok, error). error is "" on success, "sms_not_configured" when the
    tenant has no usable SMS gateway (sub-tenant that hasn't configured one), or
    "send_failed" when the provider call failed. Never raises.
    """
    from tenant.integration import get_tenant_sms_config

    cfg = get_tenant_sms_config(tenant_id=tenant_id)
    if not cfg.configured:
        logger.warning("send_sms: SMS not configured for tenant_id=%s (provider=%r)", tenant_id, cfg.provider)
        return False, "sms_not_configured"

    provider = (cfg.provider or "smshorizon").lower()
    try:
        import requests as http
        if provider in ("smshorizon", "horizon", ""):
            # Platform default account (DLT-approved sender/template live in sms_routes).
            from sms_routes import send_sms_horizon, _normalize_phone
            ok, resp = send_sms_horizon(_normalize_phone(str(phone)), text)
            if not ok:
                logger.warning("smshorizon send failed: %s", resp)
            return (ok, "" if ok else "send_failed")
        if provider == "msg91":
            resp = http.get(
                "https://api.msg91.com/api/sendhttp.php",
                params={
                    "authkey": cfg.api_key,
                    "mobiles": str(phone).lstrip("+"),
                    "message": text,
                    "sender": cfg.sender_id or "SOCIOC",
                    "route": "4",
                    "country": "91",
                },
                timeout=15,
            )
            ok = resp.status_code == 200 and "error" not in (resp.text or "").lower()
            if not ok:
                logger.warning("MSG91 send failed: %s %s", resp.status_code, (resp.text or "")[:200])
            return (ok, "" if ok else "send_failed")
        if provider == "twilio":
            sid_from = (cfg.sender_id or "").split("|", 1)
            sid = sid_from[0] if sid_from else ""
            from_num = sid_from[1] if len(sid_from) > 1 else (cfg.sender_id or "")
            if not sid:
                logger.warning("Twilio needs Account SID in sms_sender_id ('SID|FROM'); skipping send")
                return False, "send_failed"
            resp = http.post(
                f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json",
                data={"To": phone, "From": from_num, "Body": text},
                auth=(sid, cfg.api_key),
                timeout=15,
            )
            ok = resp.status_code in (200, 201)
            return (ok, "" if ok else "send_failed")
        logger.warning("SMS provider %r not implemented; OTP not sent", provider)
        return False, "send_failed"
    except Exception:
        logger.exception("send_sms failed (provider=%s)", provider)
        return False, "send_failed"


def _mask_phone(phone: str) -> str:
    p = (phone or "").strip()
    if len(p) <= 4:
        return "••••"
    return f"{p[:3]}••••{p[-2:]}"


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
sms_auth_bp = Blueprint("sms_auth", __name__)


def _resolve_tenant_and_user(data):
    """Resolve (tenant, user) from {tenant_code, email}. Returns (tenant, user)."""
    from tenant.models import Tenant
    from models import User

    code = (data.get("tenant_code") or "").strip().upper()
    email = (data.get("email") or "").strip().lower()
    if not code or not email:
        return None, None
    tenant = Tenant.query.filter_by(tenant_code=code).first()
    if not tenant:
        return None, None
    user = User.query.filter_by(tenant_id=tenant.id, email=email).first()
    return tenant, user


@sms_auth_bp.route("/api/auth/sms/forgot/request", methods=["POST"])
def sms_forgot_request():
    data = request.get_json(silent=True) or {}
    tenant, user = _resolve_tenant_and_user(data)

    # No fallback for sub-tenants: the tenant MUST have its own SMS gateway set.
    if tenant:
        from tenant.integration import get_tenant_sms_config
        if not get_tenant_sms_config(tenant_id=tenant.id).configured:
            return jsonify({
                "success": False,
                "error": "sms_not_configured",
                "message": "SMS is not configured for this account. Please add the SMS provider, sender ID and API key in the Integration settings.",
            }), 400

    # Generic response (don't leak whether the account exists). We can only send
    # when the tenant has a recovery phone configured and the user exists.
    if tenant and user and (tenant.phone_number or "").strip():
        phone = tenant.phone_number.strip()
        try:
            code = generate_and_store_otp(tenant.id, phone, "password_reset")
            ok, err = send_sms(tenant.id, phone, f"Your password reset code is {code}. It expires in {OTP_TTL_MINUTES} minutes.")
            if not ok:
                if err == "sms_not_configured":
                    return jsonify({
                        "success": False,
                        "error": "sms_not_configured",
                        "message": "SMS is not configured for this account. Please add your SMS gateway in the Integration settings.",
                    }), 400
                return jsonify({"success": False, "error": "send_failed"}), 500
            return jsonify({"success": True, "sent": True, "phone_hint": _mask_phone(phone)})
        except Exception:
            logger.exception("sms_forgot_request failed")
            return jsonify({"success": False, "error": "send_failed"}), 500

    # No recovery phone or no such user → tell the UI it can't use SMS here.
    return jsonify({"success": True, "sent": False, "error": "no_recovery_phone"})


@sms_auth_bp.route("/api/auth/sms/forgot/verify", methods=["POST"])
def sms_forgot_verify():
    data = request.get_json(silent=True) or {}
    tenant, user = _resolve_tenant_and_user(data)
    code = (data.get("code") or "").strip()
    new_password = data.get("new_password") or ""

    if not tenant or not user or not (tenant.phone_number or "").strip():
        return jsonify({"success": False, "error": "invalid_request"}), 400
    if not code:
        return jsonify({"success": False, "error": "code_required"}), 400
    if len(new_password) < 8:
        return jsonify({"success": False, "error": "weak_password", "message": "Password must be at least 8 characters."}), 400

    ok, reason = verify_otp(tenant.id, tenant.phone_number.strip(), code, "password_reset")
    if not ok:
        return jsonify({"success": False, "error": reason}), 400

    try:
        user.password_hash = generate_password_hash(new_password)
        db.session.commit()
    except Exception:
        db.session.rollback()
        logger.exception("sms_forgot_verify password update failed")
        return jsonify({"success": False, "error": "update_failed"}), 500

    return jsonify({"success": True, "message": "Password reset successfully."})
