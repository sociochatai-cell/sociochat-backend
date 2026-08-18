"""
SocioChat /wa-entry — POST-based SSO receiver from Sociovia.
============================================================

Accepts a short-lived token issued by Sociovia's /api/whatsapp/sso-redirect,
validates it against Sociovia's postgres.users table (same RDS instance,
different DB), finds/creates the matching SocioChat user by EMAIL (because
user IDs do NOT align between the two DBs), sets a Flask session cookie, and
redirects to the requested next path.

Security properties:
  - Token arrives in POST body (never in URL / logs / history / Referer)
  - Token is single-use: cleared from Sociovia DB after successful exchange
  - Token expiry enforced (2-min TTL set by Sociovia)
  - CSRF: no origin check needed here because the token itself is the credential
    (unpredictable, unguessable, single-use, short-lived)
  - Fingerprint-based lookup (SHA-256) — raw token never stored

Deployment (at cutover time — NOT NOW):
  1. Add this file to sociochat-backend/ as `wa_entry_routes.py`
  2. Register in app.py:  app.register_blueprint(wa_entry_bp)
  3. Set env var  SOCIOVIA_DB_URL=postgresql://dbuser:socioviaDB@sociovia-db.cnmc0e0wuizu.ap-south-1.rds.amazonaws.com:5432/postgres
  4. Restart sociochat-backend
"""
from __future__ import annotations

import hashlib
import logging
import os
from datetime import datetime, timezone
from typing import Optional

from flask import Blueprint, request, session, redirect, jsonify, make_response
from sqlalchemy import create_engine, text
from werkzeug.security import check_password_hash

logger = logging.getLogger(__name__)

wa_entry_bp = Blueprint("wa_entry", __name__)

# Sociovia DB connection — same RDS instance, different DB name.
# Kept lazy-init'd so import doesn't fail if env is missing at boot.
_sociovia_engine = None
_ALLOWED_NEXT_PREFIXES = (
    "/dashboard/whatsapp",
    "/dashboard/settings",
    "/dashboard",
)
_DEFAULT_NEXT = "/dashboard/whatsapp/inbox"


def _get_sociovia_engine():
    global _sociovia_engine
    if _sociovia_engine is None:
        url = os.environ.get("SOCIOVIA_DB_URL")
        if not url:
            raise RuntimeError(
                "SOCIOVIA_DB_URL not set — cannot validate SSO tokens from Sociovia"
            )
        _sociovia_engine = create_engine(url, pool_pre_ping=True, pool_recycle=300)
    return _sociovia_engine


def _safe_next_path(raw: Optional[str]) -> str:
    """Only accept whitelisted internal paths — prevents open-redirect."""
    if not raw:
        return _DEFAULT_NEXT
    if not raw.startswith("/"):
        return _DEFAULT_NEXT
    if any(raw.startswith(p) for p in _ALLOWED_NEXT_PREFIXES):
        return raw
    return _DEFAULT_NEXT


def _exchange_token_with_sociovia(token: str) -> Optional[dict]:
    """Validate the token against Sociovia's users table.

    Returns dict of {user_id, email, name} on success, None on failure.
    Clears the used token (single-use) on success.
    """
    if not token or len(token) < 32:
        logger.warning("[wa-entry] token too short — rejected")
        return None

    fingerprint = hashlib.sha256(token.encode()).hexdigest()
    now = datetime.now(timezone.utc).replace(tzinfo=None)  # Sociovia uses naive UTC

    eng = _get_sociovia_engine()
    with eng.begin() as conn:
        row = conn.execute(
            text(
                """
                SELECT id, email, name, auto_login_token_hash
                FROM users
                WHERE auto_login_token_fingerprint = :fp
                  AND auto_login_token_expires_at > :now
                LIMIT 1
                """
            ),
            {"fp": fingerprint, "now": now},
        ).fetchone()

        if row is None:
            logger.warning("[wa-entry] no matching Sociovia user for token (fp=%s)", fingerprint[:8])
            return None

        sociovia_uid, email, name, tok_hash = row
        # Verify the raw token against the bcrypt hash (defence in depth)
        if not tok_hash or not check_password_hash(tok_hash, token):
            logger.warning("[wa-entry] token hash mismatch for user %s", sociovia_uid)
            return None

        # SINGLE-USE: clear the token immediately so replay fails.
        conn.execute(
            text(
                """
                UPDATE users
                SET auto_login_token_hash = NULL,
                    auto_login_token_fingerprint = NULL,
                    auto_login_token_expires_at = NULL
                WHERE id = :uid
                """
            ),
            {"uid": sociovia_uid},
        )

    return {
        "sociovia_user_id": sociovia_uid,
        "email": (email or "").strip().lower(),
        "name": name,
    }


def _find_or_create_sociochat_user(email: str, name: Optional[str]) -> Optional[int]:
    """Return SocioChat's local user.id matching this email; create if not exists."""
    if not email:
        return None

    # Deferred import to avoid circular
    from models import db, User  # SocioChat's User model

    email = email.strip().lower()
    user = User.query.filter(db.func.lower(User.email) == email).first()
    if user:
        return user.id

    # Auto-create — new SocioChat user record with same email.
    # NB: password_hash intentionally left NULL — this user can only log in
    # via SSO from Sociovia (or by password reset).
    try:
        new_user = User(email=email, name=name or email.split("@")[0])
        db.session.add(new_user)
        db.session.commit()
        logger.info("[wa-entry] auto-created SocioChat user id=%s for %s", new_user.id, email)
        return new_user.id
    except Exception as exc:
        db.session.rollback()
        logger.exception("[wa-entry] failed to create user for %s: %s", email, exc)
        return None


@wa_entry_bp.route("/wa-entry", methods=["POST"])
def wa_entry_post():
    """POST handler — accepts token from Sociovia, sets session, redirects."""
    token = (request.form.get("token") or request.json and request.json.get("token") or "").strip()
    workspace_id = (request.form.get("workspace_id") or "").strip()
    next_path = _safe_next_path(request.form.get("next"))

    if not token:
        logger.info("[wa-entry] missing token in POST")
        return jsonify({"success": False, "error": "Missing token"}), 400

    try:
        exchange = _exchange_token_with_sociovia(token)
    except Exception as exc:
        logger.exception("[wa-entry] exchange failed: %s", exc)
        return jsonify({"success": False, "error": "SSO exchange failed"}), 500

    if not exchange:
        return jsonify({"success": False, "error": "Invalid or expired token"}), 401

    sociochat_uid = _find_or_create_sociochat_user(exchange["email"], exchange.get("name"))
    if not sociochat_uid:
        return jsonify({"success": False, "error": "Could not resolve user"}), 500

    # Set the session cookie (HttpOnly by default via Flask config)
    session.clear()
    session["user_id"] = sociochat_uid
    session["sso_source"] = "sociovia"
    if workspace_id:
        session["last_workspace_id"] = workspace_id
    session.permanent = True  # respects app's PERMANENT_SESSION_LIFETIME

    logger.info(
        "[wa-entry] SSO OK: sociovia_uid=%s → sociochat_uid=%s email=%s ws=%s next=%s",
        exchange["sociovia_user_id"], sociochat_uid, exchange["email"], workspace_id, next_path,
    )

    # 303 See Other so the browser switches to GET after the POST
    resp = make_response(redirect(next_path, code=303))
    return resp


@wa_entry_bp.route("/wa-entry", methods=["GET"])
def wa_entry_get_reject():
    """GET rejection — tokens must never travel in URLs. Returns a helpful error."""
    return (
        jsonify(
            {
                "success": False,
                "error": "SSO tokens must be POSTed, not GETed. "
                         "Query-string tokens are insecure and are rejected here."
            }
        ),
        405,
    )
