# agent_auth/routes.py
"""
Agent auth + management API — SocioChat.

Blueprints:
- agent_auth_bp   (/api/agent-auth)  : agent-facing login/logout/me/refresh
- agent_admin_bp  (/api/agent-admin) : owner-facing agent CRUD

The agent logs in with username + password only (usernames are globally unique).
"""

from datetime import datetime, timedelta, timezone

from flask import Blueprint, request, jsonify, current_app

from sqlalchemy.exc import IntegrityError

from models import db, User, Workspace
from auth_core import create_agent_token
from tenant.context import get_current_user
from .models import WorkspaceAgent, AgentWorkspace, AgentAuditLog
from .security import get_current_agent, request_metadata
from .decorators import require_agent_auth, require_owner_user
from .inbox_scope import preview_claim_for_agent
from .config import (
    APP_PAGES,
    get_assignable_page_keys,
    validate_page_permissions,
)

agent_auth_bp = Blueprint("agent_auth", __name__, url_prefix="/api/agent-auth")
agent_admin_bp = Blueprint("agent_admin", __name__, url_prefix="/api/agent-admin")


# ============================================================================
# Rate limiting (in-memory; swap for Redis in multi-worker prod)
# ============================================================================
_login_attempts = {}  # {username: [(ts, success), ...]}
MAX_LOGIN_ATTEMPTS = 5
LOCKOUT_DURATION = timedelta(minutes=15)


def _check_rate_limit(username):
    key = username
    now = datetime.now(timezone.utc)
    cutoff = now - LOCKOUT_DURATION
    if key in _login_attempts:
        _login_attempts[key] = [(ts, s) for ts, s in _login_attempts[key] if ts > cutoff]
    failed = [a for a in _login_attempts.get(key, []) if not a[1]]
    if len(failed) >= MAX_LOGIN_ATTEMPTS:
        oldest = min(a[0] for a in failed)
        retry_after = int((oldest + LOCKOUT_DURATION - now).total_seconds())
        if retry_after > 0:
            return (False, retry_after)
    return (True, None)


def _record_attempt(username, success):
    key = username
    now = datetime.now(timezone.utc)
    _login_attempts.setdefault(key, []).append((now, success))


def _serialize_workspaces(workspace_ids):
    """id + business_name for the agent's allowed workspaces (for the switcher)."""
    if not workspace_ids:
        return []
    rows = Workspace.query.filter(Workspace.id.in_(workspace_ids)).all()
    return [
        {"id": w.id, "business_name": w.business_name or f"Workspace {w.id}"}
        for w in rows
    ]


# ============================================================================
# Username availability (usernames are GLOBALLY unique — login is username-only)
# ============================================================================
def username_available(username: str) -> bool:
    """True if no agent anywhere already uses this username (global uniqueness)."""
    username = (username or "").strip()
    if not username:
        return False
    return WorkspaceAgent.query.filter_by(username=username).first() is None


def suggest_username(base: str):
    """Return an AVAILABLE username derived from ``base`` (base itself if free,
    else base_2 / base_3 / …), or None if nothing free was found."""
    base = (base or "").strip()
    if not base:
        return None
    if username_available(base):
        return base
    for i in range(2, 100):
        cand = f"{base}_{i}"
        if len(cand) <= 100 and username_available(cand):
            return cand
    return None


def username_availability_payload(username: str) -> dict:
    """Shared response body for the availability endpoints."""
    username = (username or "").strip()
    if not username:
        return {"success": True, "username": username, "available": False, "suggestion": None}
    avail = username_available(username)
    return {
        "success": True,
        "username": username,
        "available": avail,
        "suggestion": None if avail else suggest_username(username),
    }


# ============================================================================
# Agent-facing auth
# ============================================================================
@agent_auth_bp.route("/login", methods=["POST"])
def agent_login():
    """POST /api/agent-auth/login  { username, password }

    Usernames are globally unique, so the agent logs in with username + password
    only (no Account ID). The owner_user_id/tenant_id are read off the resolved
    agent row for token minting."""
    try:
        data = request.get_json(silent=True) or {}
        username = (data.get("username") or "").strip()
        password = data.get("password") or ""

        if not username or not password:
            return jsonify({"success": False, "error": "missing_credentials",
                            "message": "Username and password are required"}), 400

        allowed, retry_after = _check_rate_limit(username)
        if not allowed:
            return jsonify({"success": False, "error": "too_many_attempts",
                            "message": f"Too many failed attempts. Try again in {retry_after}s.",
                            "retry_after": retry_after}), 429

        meta = request_metadata()
        agent = WorkspaceAgent.query.filter_by(username=username).first()

        if not agent or not agent.check_password(password):
            _record_attempt(username, False)
            AgentAuditLog.log(action="login_failed",
                              owner_user_id=agent.owner_user_id if agent else None,
                              resource=username, meta={"reason": "invalid_credentials"},
                              **meta)
            db.session.commit()
            return jsonify({"success": False, "error": "invalid_credentials",
                            "message": "Invalid username or password"}), 401

        if not agent.is_active:
            _record_attempt(username, False)
            AgentAuditLog.log(action="login_failed", agent_id=agent.id,
                              owner_user_id=agent.owner_user_id, resource=username,
                              meta={"reason": "disabled"}, **meta)
            db.session.commit()
            return jsonify({"success": False, "error": "account_disabled",
                            "message": "Your account has been disabled. Contact your administrator."}), 403

        _record_attempt(username, True)
        agent.register_login()
        token = create_agent_token(agent.id, agent.owner_user_id, agent.tenant_id)
        AgentAuditLog.log(action="login_success", agent_id=agent.id,
                          owner_user_id=agent.owner_user_id, **meta)
        db.session.commit()

        return jsonify({
            "success": True,
            "token": token,
            "agent": agent.serialize(),
            "workspaces": _serialize_workspaces(agent.get_allowed_workspace_ids()),
        })
    except Exception as e:
        db.session.rollback()
        current_app.logger.exception("agent_login error: %s", e)
        return jsonify({"success": False, "error": "server_error"}), 500


@agent_auth_bp.route("/logout", methods=["POST"])
@require_agent_auth
def agent_logout():
    agent = get_current_agent()
    try:
        AgentAuditLog.log(action="logout", agent_id=agent.id,
                          owner_user_id=agent.owner_user_id, **request_metadata())
        db.session.commit()
    except Exception:
        db.session.rollback()
    return jsonify({"success": True, "message": "Logged out"})


@agent_auth_bp.route("/me", methods=["GET"])
@require_agent_auth
def agent_me():
    agent = get_current_agent()
    return jsonify({
        "success": True,
        "agent": agent.serialize(),
        "workspaces": _serialize_workspaces(agent.get_allowed_workspace_ids()),
    })


@agent_auth_bp.route("/refresh-token", methods=["POST"])
@require_agent_auth
def agent_refresh_token():
    agent = get_current_agent()
    token = create_agent_token(agent.id, agent.owner_user_id, agent.tenant_id)
    return jsonify({"success": True, "token": token})


@agent_auth_bp.route("/can-claim", methods=["POST"])
@require_agent_auth
def agent_can_claim():
    """POST /api/agent-auth/can-claim  { workspace_id, phone }

    Read-only preview of whether the current agent may start a new chat with a
    number (would it be auto-claimed, already theirs, or blocked). Writes nothing.
    """
    agent = get_current_agent()
    data = request.get_json(silent=True) or {}
    workspace_id = data.get("workspace_id")
    phone = data.get("phone") or ""

    allowed, reason = preview_claim_for_agent(agent.id, workspace_id, phone)
    if allowed:
        message = "You can message this number."
    elif reason == "assigned_to_other":
        message = "This number is already assigned to another agent."
    elif reason == "workspace_not_granted":
        message = "You do not have access to this workspace."
    else:
        message = "You cannot message this number."

    return jsonify({
        "success": True,
        "allowed": allowed,
        "reason": reason,
        "message": message,
    })


# ============================================================================
# Owner-facing management CRUD
# ============================================================================
def _owned_workspace_ids(user) -> set:
    return {w.id for w in Workspace.query.filter_by(user_id=user.id).all()}


def _validate_workspace_ids(user, workspace_ids):
    """Return (ok, error, cleaned_ids). workspace_ids must all belong to user."""
    if workspace_ids is None:
        return True, None, []
    if not isinstance(workspace_ids, list):
        return False, "workspace_ids must be a list", []
    owned = _owned_workspace_ids(user)
    cleaned = []
    for wid in workspace_ids:
        try:
            wid = int(wid)
        except (ValueError, TypeError):
            return False, f"Invalid workspace id: {wid!r}", []
        if wid not in owned:
            return False, f"Workspace {wid} does not belong to you", []
        if wid not in cleaned:
            cleaned.append(wid)
    return True, None, cleaned


def _set_agent_workspaces(agent, workspace_ids):
    """Replace the agent's workspace grants with workspace_ids."""
    AgentWorkspace.query.filter_by(agent_id=agent.id).delete()
    for wid in workspace_ids:
        db.session.add(AgentWorkspace(agent_id=agent.id, workspace_id=wid))


@agent_admin_bp.route("/agents", methods=["GET"])
@require_owner_user
def list_agents(user):
    agents = (WorkspaceAgent.query
              .filter_by(owner_user_id=user.id)
              .order_by(WorkspaceAgent.created_at.desc())
              .all())
    return jsonify({
        "success": True,
        "agents": [a.serialize(include_sensitive=True) for a in agents],
        "account_id": user.id,
        "total": len(agents),
    })


@agent_admin_bp.route("/agents", methods=["POST"])
@require_owner_user
def create_agent(user):
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    display_name = (data.get("display_name") or "").strip()
    allowed_pages = data.get("allowed_pages", [])
    workspace_ids = data.get("workspace_ids", [])

    errors = []
    if not username:
        errors.append("Username is required")
    elif len(username) < 3:
        errors.append("Username must be at least 3 characters")
    elif len(username) > 100:
        errors.append("Username must be under 100 characters")
    elif not username.replace("_", "").replace("-", "").replace(".", "").isalnum():
        errors.append("Username may contain only letters, numbers, underscore, hyphen, dot")

    if not password or len(password) < 8:
        errors.append("Password must be at least 8 characters")

    ok, page_errors, pages = validate_page_permissions(allowed_pages)
    if not ok:
        errors.extend(page_errors)
    if not pages:
        errors.append("Select at least one page permission")

    ws_ok, ws_err, ws_ids = _validate_workspace_ids(user, workspace_ids)
    if not ws_ok:
        errors.append(ws_err)
    if not ws_ids:
        errors.append("Assign at least one workspace")

    if errors:
        return jsonify({"success": False, "error": "validation_error",
                        "message": "; ".join(errors), "errors": errors}), 400

    # Usernames are globally unique — reject if ANY account already uses it.
    if WorkspaceAgent.query.filter_by(username=username).first():
        return jsonify({"success": False, "error": "username_exists",
                        "message": f'An agent named "{username}" already exists'}), 409

    try:
        agent = WorkspaceAgent(
            owner_user_id=user.id,
            tenant_id=getattr(user, "tenant_id", None),
            username=username,
            display_name=display_name or username,
            is_active=True,
        )
        agent.set_password(password)
        agent.set_allowed_pages(pages)
        db.session.add(agent)
        db.session.flush()  # assign agent.id
        _set_agent_workspaces(agent, ws_ids)

        AgentAuditLog.log(action="agent_created", agent_id=agent.id,
                          owner_user_id=user.id, resource=username,
                          meta={"allowed_pages": pages, "workspace_ids": ws_ids},
                          **request_metadata())
        db.session.commit()
        return jsonify({"success": True, "agent": agent.serialize(include_sensitive=True),
                        "account_id": user.id,
                        "message": "Agent created successfully"}), 201
    except IntegrityError:
        # Concurrent create hit the global username unique constraint.
        db.session.rollback()
        return jsonify({"success": False, "error": "username_exists",
                        "message": f'An agent named "{username}" already exists'}), 409
    except Exception as e:
        db.session.rollback()
        current_app.logger.exception("create_agent error: %s", e)
        return jsonify({"success": False, "error": "server_error"}), 500


def _get_owned_agent(user, agent_id):
    return WorkspaceAgent.query.filter_by(id=agent_id, owner_user_id=user.id).first()


@agent_admin_bp.route("/agents/<int:agent_id>", methods=["GET"])
@require_owner_user
def get_agent(user, agent_id):
    agent = _get_owned_agent(user, agent_id)
    if not agent:
        return jsonify({"success": False, "error": "agent_not_found"}), 404
    return jsonify({"success": True, "agent": agent.serialize(include_sensitive=True)})


@agent_admin_bp.route("/agents/<int:agent_id>", methods=["PATCH"])
@require_owner_user
def update_agent(user, agent_id):
    agent = _get_owned_agent(user, agent_id)
    if not agent:
        return jsonify({"success": False, "error": "agent_not_found"}), 404

    data = request.get_json(silent=True) or {}

    if "display_name" in data:
        name = (data["display_name"] or "").strip()
        if name:
            agent.display_name = name[:255]

    if "is_active" in data:
        agent.is_active = bool(data["is_active"])

    if "allowed_pages" in data:
        ok, page_errors, pages = validate_page_permissions(data["allowed_pages"])
        if not ok:
            return jsonify({"success": False, "error": "validation_error",
                            "message": "; ".join(page_errors), "errors": page_errors}), 400
        if not pages:
            return jsonify({"success": False, "error": "validation_error",
                            "message": "An agent must keep at least one page permission"}), 400
        agent.set_allowed_pages(pages)

    if "workspace_ids" in data:
        ws_ok, ws_err, ws_ids = _validate_workspace_ids(user, data["workspace_ids"])
        if not ws_ok:
            return jsonify({"success": False, "error": "validation_error",
                            "message": ws_err}), 400
        if not ws_ids:
            return jsonify({"success": False, "error": "validation_error",
                            "message": "An agent must keep at least one workspace"}), 400
        _set_agent_workspaces(agent, ws_ids)

    if data.get("password"):
        if len(data["password"]) < 8:
            return jsonify({"success": False, "error": "validation_error",
                            "message": "Password must be at least 8 characters"}), 400
        agent.set_password(data["password"])

    try:
        AgentAuditLog.log(action="agent_updated", agent_id=agent.id,
                          owner_user_id=user.id, **request_metadata())
        db.session.commit()
        return jsonify({"success": True, "agent": agent.serialize(include_sensitive=True),
                        "message": "Agent updated"})
    except Exception as e:
        db.session.rollback()
        current_app.logger.exception("update_agent error: %s", e)
        return jsonify({"success": False, "error": "server_error"}), 500


@agent_admin_bp.route("/agents/<int:agent_id>", methods=["DELETE"])
@require_owner_user
def delete_agent(user, agent_id):
    agent = _get_owned_agent(user, agent_id)
    if not agent:
        return jsonify({"success": False, "error": "agent_not_found"}), 404
    username = agent.username
    try:
        AgentAuditLog.log(action="agent_deleted", owner_user_id=user.id,
                          resource=username, meta={"agent_id": agent_id},
                          **request_metadata())
        db.session.delete(agent)
        db.session.commit()
        return jsonify({"success": True, "message": "Agent deleted"})
    except Exception as e:
        db.session.rollback()
        current_app.logger.exception("delete_agent error: %s", e)
        return jsonify({"success": False, "error": "server_error"}), 500


@agent_admin_bp.route("/agents/<int:agent_id>/reset-password", methods=["POST"])
@require_owner_user
def reset_agent_password(user, agent_id):
    agent = _get_owned_agent(user, agent_id)
    if not agent:
        return jsonify({"success": False, "error": "agent_not_found"}), 404
    data = request.get_json(silent=True) or {}
    new_password = data.get("new_password") or ""
    if len(new_password) < 8:
        return jsonify({"success": False, "error": "validation_error",
                        "message": "New password must be at least 8 characters"}), 400
    agent.set_password(new_password)
    try:
        AgentAuditLog.log(action="password_reset", agent_id=agent.id,
                          owner_user_id=user.id, **request_metadata())
        db.session.commit()
        return jsonify({"success": True, "message": "Password reset"})
    except Exception as e:
        db.session.rollback()
        current_app.logger.exception("reset_agent_password error: %s", e)
        return jsonify({"success": False, "error": "server_error"}), 500


@agent_admin_bp.route("/pages-config", methods=["GET"])
@require_owner_user
def pages_config(user):
    """Feature catalog for the create-agent UI (assignable pages)."""
    return jsonify({
        "success": True,
        "pages": APP_PAGES,
        "assignable_pages": get_assignable_page_keys(),
    })


@agent_admin_bp.route("/username-available", methods=["GET"])
@require_owner_user
def owner_username_available(user):
    """GET /api/agent-admin/username-available?username=X — live availability
    check for the create-agent form. Usernames are globally unique."""
    return jsonify(username_availability_payload(request.args.get("username", "")))
