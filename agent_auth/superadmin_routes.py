# agent_auth/superadmin_routes.py
"""
Platform super-admin agent management API — SocioChat.

Blueprint:
- agent_superadmin_bp (/api/admin/agent-mgmt) : platform-admin management of
  ANY account's agents. An "account" is a `users` row (the owner). This mirrors
  the OWNER-facing endpoints in routes.py / assignment_routes.py, but is
  super-admin-authenticated (tenant.context.require_super_admin) and explicitly
  parameterized by an owner_user_id instead of binding to the authed user.

Every route is @require_super_admin, so each handler receives the resolved
platform admin as its first positional argument (`admin`). All create/update
operations validate that workspace_ids are a subset of the OWNER's workspaces
and stamp owner_user_id + tenant_id from the owner User.
"""

from flask import Blueprint, request, jsonify, current_app

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from models import db, User, Workspace
from tenant.context import require_super_admin
from whatsapp.models import WhatsAppConversation
from .models import (
    WorkspaceAgent,
    AgentWorkspace,
    AgentNumberAssignment,
    WorkspaceAutoAssignState,
    AgentAuditLog,
)
from .security import request_metadata
from .inbox_scope import normalize_customer_phone
from .config import (
    APP_PAGES,
    get_assignable_page_keys,
    validate_page_permissions,
)
from .routes import _set_agent_workspaces, _serialize_workspaces, username_availability_payload
from .assignment_routes import (
    _get_grant,
    _grant_payload,
    _normalized_phones,
    _parse_workspace_id,
    _workspace_account_ids,
    _workspace_conversation_phones,
    _is_newer_conversation,
)

agent_superadmin_bp = Blueprint(
    "agent_superadmin", __name__, url_prefix="/api/admin/agent-mgmt"
)


# ============================================================================
# Owner (account) resolution + ownership helpers
# ============================================================================
def _load_owner(owner_user_id):
    """Return the owner User row, or None if it does not exist."""
    try:
        return db.session.get(User, int(owner_user_id))
    except (TypeError, ValueError):
        return None


def _account_not_found(owner_user_id):
    return jsonify({
        "success": False,
        "error": "account_not_found",
        "message": f"Account {owner_user_id} not found",
    }), 404


def _owner_workspace_ids(owner_user_id) -> set:
    """The set of workspace ids owned by this account."""
    return {w.id for w in Workspace.query.filter_by(user_id=owner_user_id).all()}


def _owner_owns_workspace(owner_user_id, workspace_id) -> bool:
    """True if workspace_id exists and is owned by owner_user_id."""
    try:
        wid = int(workspace_id)
    except (TypeError, ValueError):
        return False
    ws = Workspace.query.get(wid)
    return bool(ws and ws.user_id == owner_user_id)


def _get_owner_agent(owner_user_id, agent_id):
    """The WorkspaceAgent belonging to this owner, or None."""
    return WorkspaceAgent.query.filter_by(
        id=agent_id, owner_user_id=owner_user_id
    ).first()


def _owner_workspace_access_error(owner_user_id, workspace_id):
    """None if workspace belongs to the owner, else a (response, status) tuple."""
    if not _owner_owns_workspace(owner_user_id, workspace_id):
        return (
            jsonify({
                "success": False,
                "error": "workspace_not_found",
                "message": f"Workspace {workspace_id} does not belong to this account",
            }),
            404,
        )
    return None


def _validate_owner_workspace_ids(owner_user_id, workspace_ids):
    """Return (ok, error, cleaned_ids). workspace_ids must ⊆ owner's workspaces."""
    if workspace_ids is None:
        return True, None, []
    if not isinstance(workspace_ids, list):
        return False, "workspace_ids must be a list", []
    owned = _owner_workspace_ids(owner_user_id)
    cleaned = []
    for wid in workspace_ids:
        try:
            wid = int(wid)
        except (ValueError, TypeError):
            return False, f"Invalid workspace id: {wid!r}", []
        if wid not in owned:
            return False, f"Workspace {wid} does not belong to this account", []
        if wid not in cleaned:
            cleaned.append(wid)
    return True, None, cleaned


# ============================================================================
# 1) Accounts directory
# ============================================================================
@agent_superadmin_bp.route("/accounts", methods=["GET"])
@require_super_admin
def list_accounts(admin):
    """GET /api/admin/agent-mgmt/accounts?q=<optional search>

    Every account (users row) with its workspace + agent counts. Optional q
    filters on name/email (case-insensitive). Capped at 200, newest first.
    """
    q = (request.args.get("q") or "").strip()
    try:
        query = User.query
        if q:
            like = f"%{q}%"
            query = query.filter(db.or_(User.name.ilike(like), User.email.ilike(like)))
        users = query.order_by(User.id.desc()).limit(200).all()
        user_ids = [u.id for u in users]

        ws_counts = {}
        agent_counts = {}
        if user_ids:
            for uid, cnt in (
                db.session.query(Workspace.user_id, func.count(Workspace.id))
                .filter(Workspace.user_id.in_(user_ids))
                .group_by(Workspace.user_id)
                .all()
            ):
                ws_counts[uid] = cnt
            for oid, cnt in (
                db.session.query(
                    WorkspaceAgent.owner_user_id, func.count(WorkspaceAgent.id)
                )
                .filter(WorkspaceAgent.owner_user_id.in_(user_ids))
                .group_by(WorkspaceAgent.owner_user_id)
                .all()
            ):
                agent_counts[oid] = cnt

        accounts = [
            {
                "id": u.id,
                "name": u.name,
                "email": u.email,
                "tenant_id": u.tenant_id,
                "workspace_count": ws_counts.get(u.id, 0),
                "agent_count": agent_counts.get(u.id, 0),
            }
            for u in users
        ]
        return jsonify({"success": True, "accounts": accounts, "total": len(accounts)})
    except Exception as e:
        db.session.rollback()
        current_app.logger.exception("list_accounts error: %s", e)
        return jsonify({"success": False, "error": "server_error"}), 500


# ============================================================================
# 2) An account's workspaces
# ============================================================================
@agent_superadmin_bp.route("/accounts/<int:owner_user_id>/workspaces", methods=["GET"])
@require_super_admin
def list_account_workspaces(admin, owner_user_id):
    owner = _load_owner(owner_user_id)
    if not owner:
        return _account_not_found(owner_user_id)
    rows = (
        Workspace.query.filter_by(user_id=owner_user_id)
        .order_by(Workspace.id.desc())
        .all()
    )
    return jsonify({
        "success": True,
        "workspaces": [
            {"id": w.id, "business_name": w.business_name or f"Workspace {w.id}"}
            for w in rows
        ],
    })


# ============================================================================
# 3) List an account's agents
# ============================================================================
@agent_superadmin_bp.route("/accounts/<int:owner_user_id>/agents", methods=["GET"])
@require_super_admin
def list_account_agents(admin, owner_user_id):
    owner = _load_owner(owner_user_id)
    if not owner:
        return _account_not_found(owner_user_id)
    agents = (
        WorkspaceAgent.query.filter_by(owner_user_id=owner_user_id)
        .order_by(WorkspaceAgent.created_at.desc())
        .all()
    )
    return jsonify({
        "success": True,
        "agents": [a.serialize(include_sensitive=True) for a in agents],
        "account_id": owner_user_id,
        "total": len(agents),
    })


# ============================================================================
# 4) Create an agent for an account
# ============================================================================
@agent_superadmin_bp.route("/accounts/<int:owner_user_id>/agents", methods=["POST"])
@require_super_admin
def create_account_agent(admin, owner_user_id):
    owner = _load_owner(owner_user_id)
    if not owner:
        return _account_not_found(owner_user_id)

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

    ws_ok, ws_err, ws_ids = _validate_owner_workspace_ids(owner_user_id, workspace_ids)
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
            owner_user_id=owner_user_id,
            tenant_id=getattr(owner, "tenant_id", None),
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
                          owner_user_id=owner_user_id, resource=username,
                          meta={"allowed_pages": pages, "workspace_ids": ws_ids,
                                "admin_id": admin.id},
                          **request_metadata())
        db.session.commit()
        return jsonify({"success": True,
                        "agent": agent.serialize(include_sensitive=True),
                        "account_id": owner_user_id,
                        "message": "Agent created successfully"}), 201
    except IntegrityError:
        # Concurrent create hit the global username unique constraint.
        db.session.rollback()
        return jsonify({"success": False, "error": "username_exists",
                        "message": f'An agent named "{username}" already exists'}), 409
    except Exception as e:
        db.session.rollback()
        current_app.logger.exception("create_account_agent error: %s", e)
        return jsonify({"success": False, "error": "server_error"}), 500


# ============================================================================
# 5) Get a single agent
# ============================================================================
@agent_superadmin_bp.route(
    "/accounts/<int:owner_user_id>/agents/<int:agent_id>", methods=["GET"]
)
@require_super_admin
def get_account_agent(admin, owner_user_id, agent_id):
    owner = _load_owner(owner_user_id)
    if not owner:
        return _account_not_found(owner_user_id)
    agent = _get_owner_agent(owner_user_id, agent_id)
    if not agent:
        return jsonify({"success": False, "error": "agent_not_found"}), 404
    return jsonify({"success": True, "agent": agent.serialize(include_sensitive=True)})


# ============================================================================
# 6) Update an agent
# ============================================================================
@agent_superadmin_bp.route(
    "/accounts/<int:owner_user_id>/agents/<int:agent_id>", methods=["PATCH"]
)
@require_super_admin
def update_account_agent(admin, owner_user_id, agent_id):
    owner = _load_owner(owner_user_id)
    if not owner:
        return _account_not_found(owner_user_id)
    agent = _get_owner_agent(owner_user_id, agent_id)
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
        ws_ok, ws_err, ws_ids = _validate_owner_workspace_ids(
            owner_user_id, data["workspace_ids"]
        )
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
                          owner_user_id=owner_user_id,
                          meta={"admin_id": admin.id}, **request_metadata())
        db.session.commit()
        return jsonify({"success": True,
                        "agent": agent.serialize(include_sensitive=True),
                        "message": "Agent updated"})
    except Exception as e:
        db.session.rollback()
        current_app.logger.exception("update_account_agent error: %s", e)
        return jsonify({"success": False, "error": "server_error"}), 500


# ============================================================================
# 7) Delete an agent
# ============================================================================
@agent_superadmin_bp.route(
    "/accounts/<int:owner_user_id>/agents/<int:agent_id>", methods=["DELETE"]
)
@require_super_admin
def delete_account_agent(admin, owner_user_id, agent_id):
    owner = _load_owner(owner_user_id)
    if not owner:
        return _account_not_found(owner_user_id)
    agent = _get_owner_agent(owner_user_id, agent_id)
    if not agent:
        return jsonify({"success": False, "error": "agent_not_found"}), 404
    username = agent.username
    try:
        AgentAuditLog.log(action="agent_deleted", owner_user_id=owner_user_id,
                          resource=username,
                          meta={"agent_id": agent_id, "admin_id": admin.id},
                          **request_metadata())
        db.session.delete(agent)
        db.session.commit()
        return jsonify({"success": True, "message": "Agent deleted"})
    except Exception as e:
        db.session.rollback()
        current_app.logger.exception("delete_account_agent error: %s", e)
        return jsonify({"success": False, "error": "server_error"}), 500


# ============================================================================
# 8) Reset an agent's password
# ============================================================================
@agent_superadmin_bp.route(
    "/accounts/<int:owner_user_id>/agents/<int:agent_id>/reset-password",
    methods=["POST"],
)
@require_super_admin
def reset_account_agent_password(admin, owner_user_id, agent_id):
    owner = _load_owner(owner_user_id)
    if not owner:
        return _account_not_found(owner_user_id)
    agent = _get_owner_agent(owner_user_id, agent_id)
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
                          owner_user_id=owner_user_id,
                          meta={"admin_id": admin.id}, **request_metadata())
        db.session.commit()
        return jsonify({"success": True, "message": "Password reset"})
    except Exception as e:
        db.session.rollback()
        current_app.logger.exception("reset_account_agent_password error: %s", e)
        return jsonify({"success": False, "error": "server_error"}), 500


# ============================================================================
# 9) Pages config (feature catalog for the create-agent UI)
# ============================================================================
@agent_superadmin_bp.route("/pages-config", methods=["GET"])
@require_super_admin
def pages_config(admin):
    return jsonify({
        "success": True,
        "pages": APP_PAGES,
        "assignable_pages": get_assignable_page_keys(),
    })


@agent_superadmin_bp.route("/username-available", methods=["GET"])
@require_super_admin
def superadmin_username_available(admin):
    """Live username availability check (global uniqueness) for the create form."""
    return jsonify(username_availability_payload(request.args.get("username", "")))


# ============================================================================
# 10) Workspace numbers overview (inbox numbers merged with assignments)
# ============================================================================
@agent_superadmin_bp.route(
    "/accounts/<int:owner_user_id>/workspaces/<int:workspace_id>/numbers",
    methods=["GET"],
)
@require_super_admin
def list_account_workspace_numbers(admin, owner_user_id, workspace_id):
    owner = _load_owner(owner_user_id)
    if not owner:
        return _account_not_found(owner_user_id)
    ws_err = _owner_workspace_access_error(owner_user_id, workspace_id)
    if ws_err:
        return ws_err

    try:
        account_ids = _workspace_account_ids(workspace_id)

        # --- distinct customer numbers from conversations ---
        by_phone = {}
        if account_ids:
            convo_rows = (
                db.session.query(
                    WhatsAppConversation.id,
                    WhatsAppConversation.user_phone,
                    WhatsAppConversation.user_name,
                    WhatsAppConversation.last_message_at,
                )
                .filter(WhatsAppConversation.account_id.in_(account_ids))
                .all()
            )
            for cid, phone, name, last_message_at in convo_rows:
                key = normalize_customer_phone(phone)
                if not key:
                    continue
                entry = by_phone.get(key)
                if entry is None:
                    by_phone[key] = {
                        "customer_phone": key,
                        "user_name": name,
                        "conversation_id": cid,
                        "_lm": last_message_at,
                    }
                    continue
                # Same customer across multiple accounts: keep the LATEST
                # conversation id and the max last_message_at.
                if _is_newer_conversation(
                    last_message_at, cid, entry["_lm"], entry["conversation_id"]
                ):
                    entry["conversation_id"] = cid
                    if name:
                        entry["user_name"] = name
                elif name and not entry["user_name"]:
                    entry["user_name"] = name
                if last_message_at is not None and (
                    entry["_lm"] is None or last_message_at > entry["_lm"]
                ):
                    entry["_lm"] = last_message_at

        # --- merge with assignments (advance rows may have no conversation) ---
        assignments = AgentNumberAssignment.query.filter_by(
            workspace_id=workspace_id
        ).all()
        agent_ids = {a.agent_id for a in assignments}
        agent_names = {}
        if agent_ids:
            agents = WorkspaceAgent.query.filter(
                WorkspaceAgent.id.in_(agent_ids)
            ).all()
            agent_names = {a.id: (a.display_name or a.username) for a in agents}

        for assignment in assignments:
            key = (
                normalize_customer_phone(assignment.customer_phone)
                or assignment.customer_phone
            )
            entry = by_phone.setdefault(
                key,
                {
                    "customer_phone": key,
                    "user_name": None,
                    "conversation_id": None,
                    "_lm": None,
                },
            )
            entry["assigned_agent_id"] = assignment.agent_id
            entry["assigned_agent_name"] = agent_names.get(assignment.agent_id)
            entry["assignment_type"] = assignment.assignment_type

        numbers = []
        for entry in by_phone.values():
            last_message_at = entry.pop("_lm")
            entry["last_message_at"] = (
                last_message_at.isoformat() if last_message_at else None
            )
            entry.setdefault("assigned_agent_id", None)
            entry.setdefault("assigned_agent_name", None)
            entry.setdefault("assignment_type", None)
            numbers.append(entry)

        # Sort by last_message_at desc, nulls last (ISO strings order correctly).
        numbers.sort(
            key=lambda n: (n["last_message_at"] is not None, n["last_message_at"] or ""),
            reverse=True,
        )

        state = WorkspaceAutoAssignState.query.filter_by(
            workspace_id=workspace_id
        ).first()

        return jsonify({
            "success": True,
            "numbers": numbers,
            "autoassign_enabled": bool(state.enabled) if state else False,
            "total": len(numbers),
        })
    except Exception as e:
        db.session.rollback()
        current_app.logger.exception("list_account_workspace_numbers error: %s", e)
        return jsonify({"success": False, "error": "server_error"}), 500


# ============================================================================
# 11) Assign numbers to an agent (exclusive — reassign moves the number)
# ============================================================================
@agent_superadmin_bp.route(
    "/accounts/<int:owner_user_id>/agents/<int:agent_id>/numbers", methods=["POST"]
)
@require_super_admin
def assign_account_numbers(admin, owner_user_id, agent_id):
    owner = _load_owner(owner_user_id)
    if not owner:
        return _account_not_found(owner_user_id)
    agent = _get_owner_agent(owner_user_id, agent_id)
    if not agent:
        return jsonify({"success": False, "error": "agent_not_found"}), 404

    data = request.get_json(silent=True) or {}
    workspace_id, err = _parse_workspace_id(data)
    if err:
        return err
    ws_err = _owner_workspace_access_error(owner_user_id, workspace_id)
    if ws_err:
        return ws_err
    if not _get_grant(agent.id, workspace_id):
        return jsonify({
            "success": False,
            "error": "workspace_not_granted",
            "message": f"Workspace {workspace_id} is not granted to this agent",
        }), 400

    raw_phones = data.get("phones")
    if not isinstance(raw_phones, list):
        return jsonify({
            "success": False,
            "error": "validation_error",
            "message": "phones must be a list of phone numbers",
        }), 400
    phones = _normalized_phones(raw_phones)
    if not phones:
        return jsonify({
            "success": False,
            "error": "validation_error",
            "message": "No valid phone numbers provided",
        }), 400

    advance = bool(data.get("advance"))

    try:
        conversation_phones = _workspace_conversation_phones(workspace_id)

        assigned = []
        for phone in phones:
            # 'advance' when forced by the caller OR the customer has never
            # messaged in this workspace yet; otherwise 'manual'.
            assignment_type = (
                "advance" if (advance or phone not in conversation_phones) else "manual"
            )
            # EXCLUSIVE per (workspace, phone): reassign moves the number.
            existing = AgentNumberAssignment.query.filter_by(
                workspace_id=workspace_id, customer_phone=phone
            ).first()
            if existing:
                existing.agent_id = agent.id
                existing.assignment_type = assignment_type
                existing.assigned_by_user_id = owner_user_id
                assigned.append(existing)
            else:
                assignment = AgentNumberAssignment(
                    agent_id=agent.id,
                    workspace_id=workspace_id,
                    customer_phone=phone,
                    assignment_type=assignment_type,
                    assigned_by_user_id=owner_user_id,
                )
                db.session.add(assignment)
                assigned.append(assignment)

        db.session.flush()  # assign ids for serialization
        AgentAuditLog.log(
            action="numbers_assigned",
            agent_id=agent.id,
            owner_user_id=owner_user_id,
            meta={"phones": phones, "agent_id": agent.id,
                  "workspace_id": workspace_id, "admin_id": admin.id},
            **request_metadata(),
        )
        db.session.commit()

        return jsonify({
            "success": True,
            "assigned": [a.serialize() for a in assigned],
            "total": len(assigned),
        })
    except Exception as e:
        db.session.rollback()
        current_app.logger.exception("assign_account_numbers error: %s", e)
        return jsonify({"success": False, "error": "server_error"}), 500


# ============================================================================
# 12) Release numbers from an agent
# ============================================================================
@agent_superadmin_bp.route(
    "/accounts/<int:owner_user_id>/agents/<int:agent_id>/numbers", methods=["DELETE"]
)
@require_super_admin
def release_account_numbers(admin, owner_user_id, agent_id):
    owner = _load_owner(owner_user_id)
    if not owner:
        return _account_not_found(owner_user_id)
    agent = _get_owner_agent(owner_user_id, agent_id)
    if not agent:
        return jsonify({"success": False, "error": "agent_not_found"}), 404

    data = request.get_json(silent=True) or {}
    workspace_id, err = _parse_workspace_id(data)
    if err:
        return err
    ws_err = _owner_workspace_access_error(owner_user_id, workspace_id)
    if ws_err:
        return ws_err
    if not _get_grant(agent.id, workspace_id):
        return jsonify({
            "success": False,
            "error": "workspace_not_granted",
            "message": f"Workspace {workspace_id} is not granted to this agent",
        }), 400

    raw_phones = data.get("phones")
    if not isinstance(raw_phones, list):
        return jsonify({
            "success": False,
            "error": "validation_error",
            "message": "phones must be a list of phone numbers",
        }), 400
    phones = _normalized_phones(raw_phones)
    if not phones:
        return jsonify({
            "success": False,
            "error": "validation_error",
            "message": "No valid phone numbers provided",
        }), 400

    try:
        released = 0
        skipped = []
        for phone in phones:
            existing = AgentNumberAssignment.query.filter_by(
                workspace_id=workspace_id, customer_phone=phone
            ).first()
            if existing and existing.agent_id == agent.id:
                db.session.delete(existing)
                released += 1
            else:
                # Assigned to another agent, or not assigned at all — untouched.
                skipped.append(phone)

        AgentAuditLog.log(
            action="numbers_released",
            agent_id=agent.id,
            owner_user_id=owner_user_id,
            meta={"phones": phones, "agent_id": agent.id, "workspace_id": workspace_id,
                  "released": released, "skipped": skipped, "admin_id": admin.id},
            **request_metadata(),
        )
        db.session.commit()

        return jsonify({"success": True, "released": released, "skipped": skipped})
    except Exception as e:
        db.session.rollback()
        current_app.logger.exception("release_account_numbers error: %s", e)
        return jsonify({"success": False, "error": "server_error"}), 500


# ============================================================================
# 13/14) Per-workspace grant settings (inbox_scope / auto_assign)
# ============================================================================
@agent_superadmin_bp.route(
    "/accounts/<int:owner_user_id>/agents/<int:agent_id>/workspaces/<int:workspace_id>",
    methods=["GET"],
)
@require_super_admin
def get_account_agent_workspace_grant(admin, owner_user_id, agent_id, workspace_id):
    """GET the agent's grant for a workspace (side-effect-free read)."""
    owner = _load_owner(owner_user_id)
    if not owner:
        return _account_not_found(owner_user_id)
    agent = _get_owner_agent(owner_user_id, agent_id)
    if not agent:
        return jsonify({"success": False, "error": "agent_not_found"}), 404
    ws_err = _owner_workspace_access_error(owner_user_id, workspace_id)
    if ws_err:
        return ws_err
    grant = _get_grant(agent.id, workspace_id)
    if not grant:
        return jsonify({
            "success": False,
            "error": "workspace_not_granted",
            "message": f"Workspace {workspace_id} is not granted to this agent",
        }), 400
    return jsonify({"success": True, "grant": _grant_payload(grant)})


@agent_superadmin_bp.route(
    "/accounts/<int:owner_user_id>/agents/<int:agent_id>/workspaces/<int:workspace_id>",
    methods=["PATCH"],
)
@require_super_admin
def update_account_agent_workspace_grant(admin, owner_user_id, agent_id, workspace_id):
    """PATCH the agent's grant. Body may contain inbox_scope ('all'|'by_chat'),
    auto_assign (bool). A body that changes nothing does NOT write an audit row.
    """
    owner = _load_owner(owner_user_id)
    if not owner:
        return _account_not_found(owner_user_id)
    agent = _get_owner_agent(owner_user_id, agent_id)
    if not agent:
        return jsonify({"success": False, "error": "agent_not_found"}), 404
    ws_err = _owner_workspace_access_error(owner_user_id, workspace_id)
    if ws_err:
        return ws_err
    grant = _get_grant(agent.id, workspace_id)
    if not grant:
        return jsonify({
            "success": False,
            "error": "workspace_not_granted",
            "message": f"Workspace {workspace_id} is not granted to this agent",
        }), 400

    data = request.get_json(silent=True) or {}
    changes = {}

    if "inbox_scope" in data:
        scope = data["inbox_scope"]
        if scope not in AgentWorkspace.INBOX_SCOPES:
            return jsonify({
                "success": False,
                "error": "validation_error",
                "message": "inbox_scope must be one of: "
                           + ", ".join(AgentWorkspace.INBOX_SCOPES),
            }), 400
        if scope != grant.inbox_scope:
            changes["inbox_scope"] = (grant.inbox_scope, scope)
            grant.inbox_scope = scope

    if "auto_assign" in data:
        new_auto = bool(data["auto_assign"])
        if new_auto != grant.auto_assign:
            changes["auto_assign"] = (grant.auto_assign, new_auto)
            grant.auto_assign = new_auto

    # Nothing actually changed (also the read-via-empty-body case): no write.
    if not changes:
        return jsonify({"success": True, "grant": _grant_payload(grant)})

    try:
        AgentAuditLog.log(
            action="agent_workspace_updated",
            agent_id=agent.id,
            owner_user_id=owner_user_id,
            meta={"workspace_id": workspace_id, "admin_id": admin.id,
                  "changes": {k: {"from": v[0], "to": v[1]} for k, v in changes.items()}},
            **request_metadata(),
        )
        db.session.commit()
        return jsonify({"success": True, "grant": _grant_payload(grant)})
    except Exception as e:
        db.session.rollback()
        current_app.logger.exception("update_account_agent_workspace_grant error: %s", e)
        return jsonify({"success": False, "error": "server_error"}), 500


# ============================================================================
# 15) Workspace auto-assign switch
# ============================================================================
@agent_superadmin_bp.route(
    "/accounts/<int:owner_user_id>/workspaces/<int:workspace_id>/autoassign",
    methods=["GET"],
)
@require_super_admin
def get_account_autoassign(admin, owner_user_id, workspace_id):
    owner = _load_owner(owner_user_id)
    if not owner:
        return _account_not_found(owner_user_id)
    ws_err = _owner_workspace_access_error(owner_user_id, workspace_id)
    if ws_err:
        return ws_err
    try:
        state = WorkspaceAutoAssignState.query.filter_by(
            workspace_id=workspace_id
        ).first()
        return jsonify({
            "success": True,
            "enabled": bool(state.enabled) if state else False,
        })
    except Exception as e:
        db.session.rollback()
        current_app.logger.exception("get_account_autoassign error: %s", e)
        return jsonify({"success": False, "error": "server_error"}), 500


@agent_superadmin_bp.route(
    "/accounts/<int:owner_user_id>/workspaces/<int:workspace_id>/autoassign",
    methods=["PATCH"],
)
@require_super_admin
def set_account_autoassign(admin, owner_user_id, workspace_id):
    owner = _load_owner(owner_user_id)
    if not owner:
        return _account_not_found(owner_user_id)
    ws_err = _owner_workspace_access_error(owner_user_id, workspace_id)
    if ws_err:
        return ws_err

    data = request.get_json(silent=True) or {}
    if "enabled" not in data:
        return jsonify({
            "success": False,
            "error": "validation_error",
            "message": "enabled (boolean) is required",
        }), 400
    enabled = bool(data["enabled"])

    try:
        state = WorkspaceAutoAssignState.query.filter_by(
            workspace_id=workspace_id
        ).first()
        if state is None:
            state = WorkspaceAutoAssignState(workspace_id=workspace_id, enabled=enabled)
            db.session.add(state)
        else:
            state.enabled = enabled

        AgentAuditLog.log(
            action="autoassign_toggled",
            owner_user_id=owner_user_id,
            meta={"workspace_id": workspace_id, "enabled": enabled, "admin_id": admin.id},
            **request_metadata(),
        )
        db.session.commit()
        return jsonify({"success": True, "enabled": enabled})
    except Exception as e:
        db.session.rollback()
        current_app.logger.exception("set_account_autoassign error: %s", e)
        return jsonify({"success": False, "error": "server_error"}), 500
