"""
Centralized SECURE identity resolution for SocioChat.
=====================================================

Single source of truth for "who is this request?". Identity is proven by exactly
two means, both unforgeable once SECRET_KEY is a strong secret:

  1. The server-side session cookie  (session["user_id"] / session["admin_id"])
  2. A SIGNED JWT in the `Authorization: Bearer <token>` header

What this module deliberately does NOT trust (these were the breach):
  * X-User-Id / X-Admin-Id headers   — a plain id is trivially forged
  * ?admin_id=  query parameter       — same
  * UNSIGNED JWTs (verify_signature=False) — anyone can mint one

The Bearer token lets the SPA authenticate even when third-party cookies are
blocked (Safari / Firefox / incognito), which is why the old code leaned on the
forgeable X-User-Id header. Here we replace that with a real signed token.
"""

import os
import jwt
from datetime import datetime, timedelta, timezone

from flask import request, session, current_app, has_request_context

JWT_ALG = "HS256"
JWT_EXPIRY_HOURS = int(os.getenv("JWT_EXPIRY_HOURS", "168"))  # 7 days


def _signing_secret() -> str:
    """The secret used to SIGN new tokens (must match one tried on verify)."""
    return (
        os.environ.get("SECRET_KEY")
        or os.environ.get("SESSION_SECRET")
        or "dev-secret-change-in-production"
    )


def _candidate_secrets():
    """All secrets to TRY when verifying (covers SECRET_KEY/SESSION_SECRET and
    the live app.secret_key)."""
    out = []
    for k in ("SECRET_KEY", "SESSION_SECRET"):
        v = os.environ.get(k)
        if v and v not in out:
            out.append(v)
    if has_request_context():
        sk = getattr(current_app, "secret_key", None)
        if sk and sk not in out:
            out.append(sk)
    if not out:
        out.append("dev-secret-change-in-production")
    return out


def create_user_token(user_id, email=None) -> str:
    """Signed JWT proving a normal-user identity (carries `user_id`)."""
    payload = {
        "user_id": int(user_id),
        "email": email,
        "exp": datetime.now(timezone.utc) + timedelta(hours=JWT_EXPIRY_HOURS),
        "iat": datetime.now(timezone.utc),
    }
    return jwt.encode(payload, _signing_secret(), algorithm=JWT_ALG)


def create_admin_token(admin_id, email=None) -> str:
    """Signed JWT proving a platform super-admin identity (carries `admin_id`)."""
    payload = {
        "admin_id": int(admin_id),
        "email": email,
        "is_admin": True,
        "exp": datetime.now(timezone.utc) + timedelta(hours=JWT_EXPIRY_HOURS),
        "iat": datetime.now(timezone.utc),
    }
    return jwt.encode(payload, _signing_secret(), algorithm=JWT_ALG)


def create_agent_token(agent_id, owner_user_id, tenant_id=None) -> str:
    """Signed JWT proving an AGENT (restricted sub-login) identity.

    Carries `typ='agent'` + `agent_id`, and `owner_user_id` so the agent resolves
    to its owner account for existing data-ownership checks. Agent-specific
    narrowing (allowed workspaces/features) is enforced separately at the
    before_request agent gate — never inferred from this token alone.
    """
    payload = {
        "typ": "agent",
        "agent_id": int(agent_id),
        "owner_user_id": int(owner_user_id),
        "tenant_id": int(tenant_id) if tenant_id is not None else None,
        "exp": datetime.now(timezone.utc) + timedelta(hours=JWT_EXPIRY_HOURS),
        "iat": datetime.now(timezone.utc),
    }
    return jwt.encode(payload, _signing_secret(), algorithm=JWT_ALG)


def _verified_bearer_payload():
    """Return the payload of a SIGNATURE-VERIFIED Bearer JWT, or None.

    NEVER decodes with verify_signature disabled — an unsigned/forged token
    yields None.
    """
    if not has_request_context():
        return None
    auth = (request.headers.get("Authorization") or "").strip()
    if not auth.startswith("Bearer "):
        return None
    token = auth[7:].strip()
    if not token or token.count(".") != 2:
        return None
    for secret in _candidate_secrets():
        try:
            return jwt.decode(token, secret, algorithms=[JWT_ALG])
        except jwt.InvalidTokenError:
            continue
    return None


def _coerce_int(val):
    if val is None or isinstance(val, bool):
        return None
    try:
        return int(val)
    except (TypeError, ValueError):
        return None


def authenticated_user_id():
    """Resolve users.id from the session OR a signed user-token. None if the
    request carries no valid user identity (anonymous)."""
    uid = session.get("user_id")
    if uid is None:
        payload = _verified_bearer_payload()
        if payload is not None:
            if payload.get("typ") == "agent":
                # An agent acts on behalf of its owner account for ownership checks.
                uid = payload.get("owner_user_id")
            else:
                uid = payload.get("user_id") or payload.get("uid") or payload.get("sub")
    return _coerce_int(uid)


def authenticated_admin_id():
    """Resolve admins.id (platform super-admin realm) from the session OR a
    signed admin-token. None if the request is not a proven admin."""
    aid = session.get("admin_id")
    if aid is None:
        payload = _verified_bearer_payload()
        if payload is not None and (payload.get("is_admin") or payload.get("admin_id") is not None):
            aid = payload.get("admin_id")
    return _coerce_int(aid)


def authenticated_agent_id():
    """Resolve workspace_agents.id from a SIGNED agent-token (typ='agent').

    None if the request is not a proven agent. Agents authenticate ONLY via the
    Bearer token (no server session) — the SPA stores it under its own key.
    """
    payload = _verified_bearer_payload()
    if payload is not None and payload.get("typ") == "agent":
        return _coerce_int(payload.get("agent_id"))
    return None


def authenticated_agent_context():
    """Return (agent_id, owner_user_id, tenant_id) for an agent request, or
    (None, None, None). Convenience for the agent gate + profile endpoint."""
    payload = _verified_bearer_payload()
    if payload is not None and payload.get("typ") == "agent":
        return (
            _coerce_int(payload.get("agent_id")),
            _coerce_int(payload.get("owner_user_id")),
            _coerce_int(payload.get("tenant_id")),
        )
    return (None, None, None)
