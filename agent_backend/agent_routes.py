"""
Agent API Routes — Flask blueprint for the agent chat system.
=============================================================

Endpoints:
    POST   /api/agent/chat          – Main chat endpoint
    GET    /api/agent/capabilities   – List all registered actions
    DELETE /api/agent/session/<id>   – Clear a session
"""

import logging
from flask import Blueprint, request, jsonify

from auth_routes import get_current_user
from .action_executor import action_executor
from .action_registry import action_registry
from .session_manager import session_manager

logger = logging.getLogger(__name__)

agent_bp = Blueprint("agent", __name__, url_prefix="/api/agent")


def _get_workspace_id() -> str | None:
    """Extract workspace_id from header or current user's default workspace."""
    wid = request.headers.get("X-Workspace-ID")
    if wid:
        return wid

    user = get_current_user()
    if user:
        from models import Workspace
        ws = Workspace.query.filter_by(user_id=user.id).first()
        if ws:
            return str(ws.id)
    return None


def _get_account_id(workspace_id: str) -> int | None:
    """Get the first active WhatsApp account for the workspace."""
    try:
        from whatsapp.models import WhatsAppAccount
        acct = WhatsAppAccount.query.filter_by(
            workspace_id=workspace_id, is_active=True
        ).first()
        return acct.id if acct else None
    except Exception:
        return None


# ============================================================
# POST /api/agent/chat
# ============================================================

@agent_bp.route("/chat", methods=["POST"])
def agent_chat():
    """
    Main agent chat endpoint.

    Body JSON:
        message       – (required) natural language prompt
        session_id    – (optional) existing session
        workspace_id  – (optional) override header

    Returns structured agent response.
    """
    data = request.get_json(silent=True) or {}
    message = (data.get("message") or "").strip()

    if not message:
        return jsonify({"status": "error", "message": "Message is required."}), 400

    # Require a real (session/JWT) identity — the agent can send messages and run
    # automations, so it must never run for an anonymous or impersonated caller.
    user = get_current_user()
    if not user:
        return jsonify({"status": "error", "message": "authentication_required"}), 401

    # The workspace the agent acts on MUST belong to the caller. resolve_owned_workspace
    # verifies ownership (or falls back to the user's own workspace when none supplied).
    from tenant.context import resolve_owned_workspace
    ws, err = resolve_owned_workspace(user, data.get("workspace_id") or request.headers.get("X-Workspace-ID"))
    if err:
        return err
    workspace_id = str(ws.id)

    session_id = data.get("session_id")
    account_id = _get_account_id(workspace_id)

    user_id = str(user.id)

    result = action_executor.process_message(
        message=message,
        workspace_id=workspace_id,
        session_id=session_id,
        account_id=account_id,
        user_id=user_id,
    )

    return jsonify(result), 200


# ============================================================
# GET /api/agent/capabilities
# ============================================================

@agent_bp.route("/capabilities", methods=["GET"])
def agent_capabilities():
    """Return all registered actions (for frontend hints / autocomplete)."""
    domain = request.args.get("domain")
    actions = action_registry.list_actions(domain=domain)
    return jsonify({"actions": actions, "domains": action_registry.list_domains()}), 200


# ============================================================
# DELETE /api/agent/session/<id>
# ============================================================

@agent_bp.route("/session/<session_id>", methods=["DELETE"])
def agent_clear_session(session_id: str):
    """Clear a session."""
    deleted = session_manager.delete(session_id)
    if deleted:
        return jsonify({"status": "success", "message": "Session cleared."}), 200
    return jsonify({"status": "error", "message": "Session not found."}), 404
