# agent_auth/__init__.py
"""
Agent (sub-login) authentication + management for SocioChat.
============================================================
A workspace-owner (a `users` row) creates restricted "agent" sub-logins scoped
to specific features (Level 1) and specific workspaces (Level 2). Inbox/number
scoping (Level 3) is layered on in later phases.

NOTE: This is distinct from the `agent_backend` package, which is the AI
intent/action system (natural language -> action). They are unrelated.

Public surface:
- agent_auth_bp        : /api/agent-auth        (agent login/logout/me/refresh)
- agent_admin_bp       : /api/agent-admin       (owner-facing agent CRUD)
- assignment_bp        : /api/agent-admin       (owner-facing inbox/number assignment — Level 3)
- agent_superadmin_bp  : /api/admin/agent-mgmt  (platform super-admin agent management for ANY account)
- enforce_agent_restrictions : before_request gate (register in app.py)
"""

from .routes import agent_auth_bp, agent_admin_bp
from .assignment_routes import assignment_bp
from .superadmin_routes import agent_superadmin_bp
from .gate import enforce_agent_restrictions

__all__ = [
    "agent_auth_bp",
    "agent_admin_bp",
    "assignment_bp",
    "agent_superadmin_bp",
    "enforce_agent_restrictions",
]
