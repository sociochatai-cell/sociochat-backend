# agent_auth/security.py
"""
Agent identity resolution for request handlers + the before_request gate.
"""

from flask import g, request

from models import db
from auth_core import authenticated_agent_id


def get_current_agent():
    """Load the authenticated WorkspaceAgent for this request, or None.

    Resolves from the signed agent token (via auth_core), loads the row, and
    verifies it is still active. Caches on `g` for the request. A disabled or
    deleted agent resolves to None (fails closed)."""
    cached = getattr(g, "_current_agent", None)
    if cached is not None:
        return cached

    agent_id = authenticated_agent_id()
    if agent_id is None:
        return None

    from .models import WorkspaceAgent

    agent = db.session.get(WorkspaceAgent, agent_id)
    if not agent or not agent.is_active:
        return None

    g._current_agent = agent
    return agent


def request_metadata() -> dict:
    """IP + user-agent for audit logging."""
    return {
        "ip_address": request.remote_addr,
        "user_agent": (request.headers.get("User-Agent") or "")[:512],
    }
