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
import secrets
from datetime import datetime, timezone
from typing import Optional

from flask import Blueprint, request, session, redirect, jsonify, make_response
from sqlalchemy import create_engine, text
from werkzeug.security import check_password_hash, generate_password_hash

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


def _shared_id_master_enabled() -> bool:
    """Phase-1 flag (DEFAULT OFF). When ON, a newly auto-created SocioChat user
    adopts the SAME id as the Sociovia master user (true master/slave shared id)
    instead of SocioChat minting its own 1,000,000+ id.

    Safety: forward-only — this only affects brand-new auto-creates. It never
    re-keys an existing row, and if the target id is already taken in SocioChat
    (by any email) it falls back to normal autoincrement. So flipping this flag
    cannot break any existing account, webhook, send-URL, automation, or inbox.
    """
    return os.environ.get("SHARED_ID_MASTER", "").strip().lower() in ("1", "true", "yes", "on")


def _ws_copy_at_sso_enabled() -> bool:
    """Phase-2 flag (DEFAULT OFF). When ON, the SSO handoff copies the user's
    Sociovia workspace(s) into SocioChat on entry (via enable_sync_for_user), so a
    first-time user lands on their exact workspace instead of a blank one.
    Best-effort: any copy failure is swallowed and never blocks the SSO login.
    """
    return os.environ.get("WS_COPY_AT_SSO", "").strip().lower() in ("1", "true", "yes", "on")


def _plan_push_at_sso_enabled() -> bool:
    """Phase-3 flag (DEFAULT OFF). When ON, the SSO handoff copies the user's
    Sociovia (master) plan + status onto the SocioChat user row, so SocioChat's own
    plan gate reflects what the user actually bought on Sociovia. Forward-only
    (Sociovia→SocioChat), best-effort — never blocks login.
    """
    return os.environ.get("PLAN_PUSH_AT_SSO", "").strip().lower() in ("1", "true", "yes", "on")


def _push_plan_from_sociovia(sociochat_uid, sociovia_user_id) -> None:
    """Copy plan/status/subscription_expires_at from the Sociovia master user onto
    the SocioChat user row (READ-ONLY on Sociovia). Best-effort; never raises."""
    if not sociovia_user_id:
        return
    try:
        from models import db, User
        eng = _get_sociovia_engine()
        with eng.connect() as c:
            row = c.execute(
                text("SELECT plan, status, subscription_expires_at FROM users WHERE id=:id"),
                {"id": sociovia_user_id},
            ).fetchone()
        if not row:
            return
        plan, status, sub_exp = row
        u = User.query.get(sociochat_uid)
        if u is None:
            return
        changed = False
        if plan and getattr(u, "plan", None) != plan:
            u.plan = plan; changed = True
        if status and getattr(u, "status", None) != status:
            u.status = status; changed = True
        if hasattr(u, "subscription_expires_at") and getattr(u, "subscription_expires_at", None) != sub_exp:
            u.subscription_expires_at = sub_exp; changed = True
        if changed:
            db.session.commit()
            logger.info(
                "[wa-entry] plan push uid=%s -> plan=%s status=%s", sociochat_uid, plan, status,
            )
            # Best-effort: rebuild the capability matrix so feature gates refresh.
            try:
                from subscription.service import schedule_capabilities_resync_for_user
                schedule_capabilities_resync_for_user(int(sociochat_uid), reason="sso_plan_push")
            except Exception:
                logger.debug("[wa-entry] capability resync unavailable (non-fatal)")
    except Exception:
        logger.exception("[wa-entry] plan push failed (non-fatal) for uid=%s", sociochat_uid)


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


def _find_or_create_sociochat_user(
    email: str, name: Optional[str], sociovia_user_id: Optional[int] = None
) -> Optional[int]:
    """Return SocioChat's local user.id matching this email; create if not exists.

    When SHARED_ID_MASTER is enabled and a Sociovia user id is supplied, a newly
    created user adopts that id (master/slave same-id). See _shared_id_master_enabled.
    """
    if not email:
        return None

    # Deferred import to avoid circular
    from models import db, User  # SocioChat's User model

    email = email.strip().lower()
    user = User.query.filter(db.func.lower(User.email) == email).first()
    if user:
        return user.id

    # Decide whether this new user should adopt the Sociovia master id.
    adopt_id: Optional[int] = None
    if _shared_id_master_enabled() and sociovia_user_id is not None:
        try:
            sv_id = int(sociovia_user_id)
        except (TypeError, ValueError):
            sv_id = None
        if sv_id is not None:
            # Collision guard: only adopt the id if it is FREE in SocioChat.
            # If some other row already owns it, fall back to autoincrement so we
            # never clobber an existing account.
            clash = User.query.filter(User.id == sv_id).first()
            if clash is None:
                adopt_id = sv_id
            else:
                logger.warning(
                    "[wa-entry] shared-id: Sociovia id %s already used in SocioChat "
                    "(email=%s) — falling back to autoincrement for %s",
                    sv_id, getattr(clash, "email", "?"), email,
                )

    # Resolve the internal SocioChat tenant (T0000). SocioChat's users table
    # treats a NULL tenant_id as unsafe (sits outside tenant isolation), so an
    # SSO-provisioned user is placed in the internal tenant — same default the
    # public signup uses. This is SocioChat-only; no other tenant is touched.
    internal_tenant_id = None
    try:
        from tenant.branding import INTERNAL_TENANT_CODE
        from tenant.models import Tenant
        _t = Tenant.query.filter_by(tenant_code=INTERNAL_TENANT_CODE).first()
        internal_tenant_id = _t.id if _t else None
    except Exception:
        logger.exception("[wa-entry] could not resolve internal tenant; leaving tenant_id NULL")

    # Auto-create — new SocioChat user record with same email.
    # password_hash is set to an UNUSABLE random hash (the column is NOT NULL):
    # this user can still only log in via SSO from Sociovia, never with a password.
    try:
        fields = dict(
            email=email,
            name=name or email.split("@")[0],
            password_hash=generate_password_hash(secrets.token_hex(32)),
            tenant_id=internal_tenant_id,
        )
        if adopt_id is not None:
            fields["id"] = adopt_id
        new_user = User(**fields)
        db.session.add(new_user)
        db.session.commit()
        logger.info(
            "[wa-entry] auto-created SocioChat user id=%s for %s (shared_id=%s, tenant=%s)",
            new_user.id, email, adopt_id is not None, internal_tenant_id,
        )
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

    sociochat_uid = _find_or_create_sociochat_user(
        exchange["email"], exchange.get("name"), exchange.get("sociovia_user_id")
    )
    if not sociochat_uid:
        return jsonify({"success": False, "error": "Could not resolve user"}), 500

    # Phase 2 (flag WS_COPY_AT_SSO, DEFAULT OFF): bring the user's Sociovia
    # workspace(s) across on entry so a first-time user lands on their exact
    # workspace. Best-effort — wrapped so a sync failure can NEVER block login.
    if _ws_copy_at_sso_enabled():
        try:
            from models import User
            from sync_routes import enable_sync_for_user
            _u = User.query.get(sociochat_uid)
            if _u is not None:
                _summary = enable_sync_for_user(_u)
                logger.info("[wa-entry] workspace copy-at-SSO for %s: %s", exchange["email"], _summary)
        except Exception:
            logger.exception(
                "[wa-entry] workspace copy-at-SSO failed (non-fatal) for %s", exchange["email"]
            )

    # Phase 3 (flag PLAN_PUSH_AT_SSO, DEFAULT OFF): push the Sociovia master plan
    # onto the SocioChat user so SocioChat's plan gate matches what they bought.
    if _plan_push_at_sso_enabled():
        _push_plan_from_sociovia(sociochat_uid, exchange.get("sociovia_user_id"))

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

    # Build redirect with sso=1 so the SPA runs SSO bootstrap.
    sep = chr(38) if chr(63) in next_path else chr(63)
    dest = next_path + sep + 'sso=1'
    if workspace_id:
        dest += chr(38) + 'ws=' + workspace_id
    resp = make_response(redirect(dest, code=303))
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
