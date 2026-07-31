# agent_auth/gate.py
"""
before_request enforcement for AGENT principals.

Registered once in app.py. Runs only when the request carries a valid agent
token; normal users / admins / anonymous fall straight through.

Guarantees (hardened after the Phase-1 security review):
- A disabled/deleted agent token can no longer reach any /api/* route.
- Agents are blocked from owner-only families (admin, tenant, billing,
  subscription, payments, agent management, the AI /api/agent surface) and from
  /api/auth/* (which would leak owner PII), and from workspace mutations.
- Level 1 (features): coarse per-module gate — an agent can only reach an API
  module (whatsapp / crm / analytics) if it has a granted page in that module.
- Level 2 (workspaces): FAIL CLOSED. A workspace_id is read from EVERY source
  (X-Workspace-ID header, ?workspace_id, JSON body, form body, and URL path
  view_args). Any workspace an agent touches must be assigned to it. A request
  to a workspace-scoped DATA family that resolves to NO assigned workspace is
  rejected — closing the "omit workspace_id -> owner-wide data" and
  resource-by-id cross-workspace leaks.

Residual (documented): a request that carries an ASSIGNED workspace_id while
addressing a resource that lives in a DIFFERENT workspace is additionally
guarded at the resource layer (whatsapp _require_owned_account /
_require_owned_conversation add agent workspace+phone checks).
"""

from flask import request, jsonify

from auth_core import authenticated_agent_id
from .security import get_current_agent
from .config import (
    AGENT_DENY_API_PREFIXES,
    AGENT_DENY_WORKSPACE_MUTATIONS,
    agent_module_for_path,
    agent_has_module,
    is_agent_workspace_scoped,
)


def _extract_workspace_id():
    """Read a client-supplied workspace_id from ANY source (fail-closed intent)."""
    wid = request.headers.get("X-Workspace-ID") or request.headers.get("X-Workspace-Id")
    if not wid:
        wid = request.args.get("workspace_id")
    if not wid:
        try:
            body = request.get_json(silent=True) or {}
            wid = body.get("workspace_id")
        except Exception:
            wid = None
    if not wid:
        try:
            wid = request.form.get("workspace_id")
        except Exception:
            wid = None
    if not wid and request.view_args:
        wid = request.view_args.get("workspace_id")
    return wid


def _deny(error, message, status=403):
    return jsonify({"success": False, "error": error, "message": message}), status


def enforce_agent_restrictions():
    """Return a Flask response to BLOCK, or None to allow the request through."""
    if request.method == "OPTIONS":
        return None
    path = request.path or ""
    if not path.startswith("/api/"):
        return None

    # Fast path: not an agent token -> nothing to do.
    if authenticated_agent_id() is None:
        return None

    agent = get_current_agent()
    if agent is None:
        return _deny("agent_inactive", "This agent account is disabled.")

    # The agent's own auth surface is always allowed.
    if path.startswith("/api/agent-auth"):
        return None

    # Owner-only families are never reachable by an agent.
    for prefix in AGENT_DENY_API_PREFIXES:
        if path.startswith(prefix):
            return _deny("agent_forbidden", "This area is not available to agents.")

    # /api/auth/* would resolve/leak the OWNER account (profile, workspaces list,
    # settings). Agents use /api/agent-auth instead. Allow only logout.
    if path.startswith("/api/auth") and not path.startswith("/api/auth/logout"):
        return _deny("agent_forbidden", "This area is not available to agents.")

    # Workspace mutations are owner-only.
    for method, p in AGENT_DENY_WORKSPACE_MUTATIONS:
        if request.method == method and (path == p or path.startswith(p)):
            return _deny("agent_forbidden", "Agents cannot modify workspaces.")

    allowed_pages = agent.get_allowed_pages()

    # Level 1 — coarse per-module feature gate.
    module = agent_module_for_path(path)
    if module is not None and not agent_has_module(allowed_pages, module):
        return _deny("feature_forbidden", "You don't have access to this feature.")

    # Level 2 — workspace narrowing, FAIL CLOSED.
    wid = _extract_workspace_id()
    if wid not in (None, ""):
        if not agent.has_workspace(wid):
            return _deny("forbidden_workspace", "You are not assigned to this workspace.")
    elif is_agent_workspace_scoped(path):
        # A workspace-scoped data request with no resolvable workspace would
        # otherwise return owner-wide data — reject it.
        return _deny("workspace_required", "Select a workspace to continue.", status=400)

    return None
