"""
Tenant Module - Server-side context & isolation enforcement
============================================================

The tenant boundary is enforced HERE, server-side, derived from the
authenticated session — never from a client-supplied tenant/workspace header.

Key guarantees:
* ``get_current_tenant()`` is derived from the logged-in user's ``tenant_id``.
* ``resolve_owned_workspace()`` verifies that any workspace a request touches
  belongs to the caller (closing the trusted ``X-Workspace-ID`` hole). Because
  a workspace is owned by a user who belongs to exactly one tenant, ownership
  enforcement *is* tenant isolation for the workspace-scoped majority of data.
* Role decorators gate Super Admin / Tenant Admin / Tenant User surfaces.
"""

import os
import re
import string
import secrets
import logging
from functools import wraps

from flask import request, session, jsonify, g

from models import db, User, Workspace, Admin

logger = logging.getLogger(__name__)

# Preserve existing cross-origin/dev-tunnel behavior (frontend sends X-User-Id
# when session cookies are unavailable). Set TENANT_TRUST_USER_HEADER=0 in
# production to require a real server session and harden against impersonation.
_TRUST_USER_HEADER = os.getenv("TENANT_TRUST_USER_HEADER", "1").strip().lower() in (
    "1", "true", "yes", "on",
)

TENANT_ADMIN_ROLES = ("tenant_admin", "admin", "marketing_admin")


# --------------------------------------------------------------------------- #
# Identity resolution (session-first)
# --------------------------------------------------------------------------- #
def get_current_user():
    """Resolve the authenticated user from the server session or a SIGNED Bearer
    JWT only. The forgeable X-User-Id header is no longer trusted (see auth_core)."""
    from auth_core import authenticated_user_id
    uid = authenticated_user_id()
    if uid is None:
        return None
    try:
        return db.session.get(User, uid)
    except (TypeError, ValueError):
        return None


def get_current_admin():
    """Resolve the authenticated platform admin (super admin realm) from the
    server session or a SIGNED admin JWT only. X-Admin-Id is no longer trusted."""
    from auth_core import authenticated_admin_id
    aid = authenticated_admin_id()
    if aid is None:
        return None
    try:
        return db.session.get(Admin, aid)
    except (TypeError, ValueError):
        return None


def is_super_admin() -> bool:
    admin = get_current_admin()
    return bool(admin and getattr(admin, "is_superadmin", False))


def get_current_tenant():
    """The current user's tenant, derived from the authenticated session only."""
    from tenant.models import Tenant

    user = get_current_user()
    if not user or not getattr(user, "tenant_id", None):
        return None
    try:
        return db.session.get(Tenant, int(user.tenant_id))
    except (TypeError, ValueError):
        return None


def get_current_tenant_id():
    user = get_current_user()
    return getattr(user, "tenant_id", None) if user else None


# --------------------------------------------------------------------------- #
# Workspace ownership enforcement (the real isolation boundary)
# --------------------------------------------------------------------------- #
def user_owns_workspace(user, workspace_id) -> bool:
    if not user or workspace_id is None:
        return False
    try:
        ws = db.session.get(Workspace, int(workspace_id))
    except (TypeError, ValueError):
        return False
    return bool(ws and ws.user_id == user.id)


def resolve_owned_workspace(user, requested_workspace_id=None):
    """Return (workspace, error_response).

    * If a workspace_id is supplied, it MUST belong to ``user`` or a 403 is
      returned — this is what prevents one tenant from reading another's data
      via a forged X-Workspace-ID.
    * If none supplied, fall back to the user's (single) workspace.
    """
    if not user:
        return None, (jsonify({"success": False, "error": "Not authenticated"}), 401)

    if requested_workspace_id:
        try:
            wid = int(requested_workspace_id)
        except (TypeError, ValueError):
            return None, (jsonify({"success": False, "error": "invalid_workspace_id"}), 400)
        ws = db.session.get(Workspace, wid)
        if not ws:
            return None, (jsonify({"success": False, "error": "workspace_not_found"}), 404)
        if ws.user_id != user.id:
            logger.warning(
                "cross_workspace_denied user=%s requested_ws=%s owner=%s",
                user.id, wid, ws.user_id,
            )
            return None, (jsonify({"success": False, "error": "forbidden_workspace"}), 403)
        return ws, None

    ws = Workspace.query.filter_by(user_id=user.id).first()
    if not ws:
        return None, (jsonify({"success": False, "error": "no_workspace"}), 404)
    return ws, None


def request_workspace_id():
    """The workspace_id the client is asking for (header/arg/body), unverified."""
    wid = (
        request.headers.get("X-Workspace-ID")
        or request.args.get("workspace_id")
    )
    if not wid:
        try:
            wid = (request.get_json(silent=True) or {}).get("workspace_id")
        except Exception:
            wid = None
    return wid


# --------------------------------------------------------------------------- #
# Role decorators
# --------------------------------------------------------------------------- #
def require_super_admin(f):
    """Only the platform owner (super admin). Passes the admin as first arg."""
    @wraps(f)
    def decorated(*args, **kwargs):
        admin = get_current_admin()
        if not admin or not getattr(admin, "is_superadmin", False):
            return jsonify({"success": False, "error": "super_admin_required"}), 403
        return f(admin, *args, **kwargs)
    return decorated


def require_tenant_admin(f):
    """Tenant admin (or platform admin). Passes the user as first arg."""
    @wraps(f)
    def decorated(*args, **kwargs):
        user = get_current_user()
        if not user:
            return jsonify({"success": False, "error": "authentication_required"}), 401
        if (user.role or "user") not in TENANT_ADMIN_ROLES:
            return jsonify({"success": False, "error": "tenant_admin_required"}), 403
        if not getattr(user, "tenant_id", None):
            return jsonify({"success": False, "error": "no_tenant"}), 403
        g.current_user = user
        g.current_tenant_id = user.tenant_id
        return f(user, *args, **kwargs)
    return decorated


def require_tenant_user(f):
    """Any authenticated tenant user. Passes the user as first arg."""
    @wraps(f)
    def decorated(*args, **kwargs):
        user = get_current_user()
        if not user:
            return jsonify({"success": False, "error": "authentication_required"}), 401
        g.current_user = user
        g.current_tenant_id = getattr(user, "tenant_id", None)
        return f(user, *args, **kwargs)
    return decorated


# --------------------------------------------------------------------------- #
# Credential / code generators (used by tenant creation)
# --------------------------------------------------------------------------- #
def generate_password(length: int = 12) -> str:
    """Generate a readable strong password (letters + digits + a symbol)."""
    alphabet = string.ascii_letters + string.digits
    core = "".join(secrets.choice(alphabet) for _ in range(max(8, length - 2)))
    return core + secrets.choice("!@#$%&*") + secrets.choice(string.digits)


def generate_tenant_code(company_name: str = "") -> str:
    """Generate a unique tenant code candidate (e.g. ABC + random digits).

    The wizard normally supplies the code; this is the fallback generator.
    """
    from tenant.models import Tenant

    base = re.sub(r"[^A-Za-z]", "", company_name or "").upper()[:3] or "TEN"
    base = base.ljust(3, "X")
    for _ in range(40):
        candidate = f"{base}{secrets.randbelow(9000) + 1000}"
        if not Tenant.query.filter_by(tenant_code=candidate).first():
            return candidate
    # Extremely unlikely fallback.
    return f"{base}{secrets.token_hex(3).upper()}"


def normalize_tenant_code(code: str) -> str:
    return re.sub(r"\s+", "", (code or "")).upper()
