# agent_auth/decorators.py
"""
Route decorators for the agent auth module.
"""

from functools import wraps

from flask import jsonify

from .security import get_current_agent


def require_agent_auth(f):
    """Require a valid, active agent token. Injects nothing; use get_current_agent()."""
    @wraps(f)
    def decorated(*args, **kwargs):
        agent = get_current_agent()
        if not agent:
            return jsonify({
                "success": False,
                "error": "agent_authentication_required",
                "message": "Valid agent token required",
            }), 401
        return f(*args, **kwargs)
    return decorated


def require_owner_user(f):
    """Require an authenticated account owner (a normal user) for agent-management
    endpoints. Passes the user as the first positional arg. Agents themselves are
    NOT owners — an agent token resolves to the owner user id, so we additionally
    reject requests that carry an agent token."""
    @wraps(f)
    def decorated(*args, **kwargs):
        from tenant.context import get_current_user
        from auth_core import authenticated_agent_id

        if authenticated_agent_id() is not None:
            return jsonify({
                "success": False,
                "error": "owner_only",
                "message": "Agents cannot manage agents.",
            }), 403

        user = get_current_user()
        if not user:
            return jsonify({
                "success": False,
                "error": "authentication_required",
            }), 401
        return f(user, *args, **kwargs)
    return decorated
