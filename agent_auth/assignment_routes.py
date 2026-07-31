# agent_auth/assignment_routes.py
"""
Number assignment management API — SocioChat (Phase 2).

Blueprint:
- assignment_bp (/api/agent-admin) : owner-facing management of Level-3 inbox
  scoping — which customer numbers each agent handles, per-workspace inbox
  scope ('all' | 'by_chat'), and the workspace auto-assign switch.

All endpoints require an authenticated OWNER user (agents cannot manage
assignments). Every customer phone is normalized via
inbox_scope.normalize_customer_phone() before compare/write so assignment
rows always match conversation.user_phone as stored by the webhook.
"""

from flask import Blueprint, request, jsonify, current_app

from models import db
from whatsapp.models import WhatsAppAccount, WhatsAppConversation
from .models import (
    WorkspaceAgent,
    AgentWorkspace,
    AgentNumberAssignment,
    WorkspaceAutoAssignState,
    AgentAuditLog,
)
from .decorators import require_owner_user
from .security import request_metadata
from .inbox_scope import normalize_customer_phone
from .routes import _owned_workspace_ids, _get_owned_agent

assignment_bp = Blueprint("agent_assignment", __name__, url_prefix="/api/agent-admin")


# ============================================================================
# Local helpers
# ============================================================================
def _workspace_access_error(user, workspace_id):
    """None if the workspace belongs to user, else a (response, status) tuple."""
    if workspace_id not in _owned_workspace_ids(user):
        return (
            jsonify({
                "success": False,
                "error": "workspace_not_found",
                "message": f"Workspace {workspace_id} does not belong to you",
            }),
            404,
        )
    return None


def _get_grant(agent_id, workspace_id):
    """The AgentWorkspace grant row for (agent, workspace), or None."""
    return AgentWorkspace.query.filter_by(
        agent_id=agent_id, workspace_id=workspace_id
    ).first()


def _workspace_account_ids(workspace_id) -> list:
    """ids of ALL WhatsApp accounts (active or not) mapped to this workspace.
    NOTE: WhatsAppAccount.workspace_id is a STRING column."""
    accounts = WhatsAppAccount.query.filter_by(workspace_id=str(workspace_id)).all()
    return [a.id for a in accounts]


def _workspace_conversation_phones(workspace_id) -> set:
    """Normalized customer phones that already have a conversation in this
    workspace (any of its accounts)."""
    account_ids = _workspace_account_ids(workspace_id)
    if not account_ids:
        return set()
    rows = (
        db.session.query(WhatsAppConversation.user_phone)
        .filter(WhatsAppConversation.account_id.in_(account_ids))
        .distinct()
        .all()
    )
    return {normalize_customer_phone(r[0]) for r in rows if r[0]}


def _parse_workspace_id(data):
    """(workspace_id, error_response). Body must carry an int workspace_id."""
    try:
        return int(data.get("workspace_id")), None
    except (TypeError, ValueError):
        return None, (
            jsonify({
                "success": False,
                "error": "validation_error",
                "message": "workspace_id (integer) is required",
            }),
            400,
        )


def _normalized_phones(raw_phones):
    """Normalize a list of phones, dropping empties and duplicates (order kept)."""
    phones = []
    for phone in raw_phones:
        if not isinstance(phone, str):
            phone = "" if phone is None else str(phone)
        normalized = normalize_customer_phone(phone)
        if normalized and normalized not in phones:
            phones.append(normalized)
    return phones


def _is_newer_conversation(lm_a, id_a, lm_b, id_b):
    """Is conversation A (last_message_at, id) more recent than B?"""
    if lm_a is None and lm_b is None:
        return id_a > id_b
    if lm_a is None:
        return False
    if lm_b is None:
        return True
    if lm_a == lm_b:
        return id_a > id_b
    return lm_a > lm_b


# ============================================================================
# 1) Workspace numbers overview (inbox numbers merged with assignments)
# ============================================================================
@assignment_bp.route("/workspaces/<int:workspace_id>/numbers", methods=["GET"])
@require_owner_user
def list_workspace_numbers(user, workspace_id):
    """GET /api/agent-admin/workspaces/<id>/numbers

    Distinct customer numbers seen in the workspace inbox, merged with agent
    number assignments. Advance-assigned numbers with no conversation yet are
    included with conversation_id/user_name null.
    """
    err = _workspace_access_error(user, workspace_id)
    if err:
        return err

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
        current_app.logger.exception("list_workspace_numbers error: %s", e)
        return jsonify({"success": False, "error": "server_error"}), 500


# ============================================================================
# 2) Assign numbers to an agent (exclusive — reassign moves the number)
# ============================================================================
@assignment_bp.route("/agents/<int:agent_id>/numbers", methods=["POST"])
@require_owner_user
def assign_numbers(user, agent_id):
    """POST /api/agent-admin/agents/<id>/numbers
    Body: { "workspace_id": int, "phones": [str], "advance": bool? }
    """
    agent = _get_owned_agent(user, agent_id)
    if not agent:
        return jsonify({"success": False, "error": "agent_not_found"}), 404

    data = request.get_json(silent=True) or {}
    workspace_id, err = _parse_workspace_id(data)
    if err:
        return err
    ws_err = _workspace_access_error(user, workspace_id)
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
                existing.assigned_by_user_id = user.id
                assigned.append(existing)
            else:
                assignment = AgentNumberAssignment(
                    agent_id=agent.id,
                    workspace_id=workspace_id,
                    customer_phone=phone,
                    assignment_type=assignment_type,
                    assigned_by_user_id=user.id,
                )
                db.session.add(assignment)
                assigned.append(assignment)

        db.session.flush()  # assign ids for serialization
        AgentAuditLog.log(
            action="numbers_assigned",
            agent_id=agent.id,
            owner_user_id=user.id,
            meta={"phones": phones, "agent_id": agent.id, "workspace_id": workspace_id},
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
        current_app.logger.exception("assign_numbers error: %s", e)
        return jsonify({"success": False, "error": "server_error"}), 500


# ============================================================================
# 3) Release numbers from an agent
# ============================================================================
@assignment_bp.route("/agents/<int:agent_id>/numbers", methods=["DELETE"])
@require_owner_user
def release_numbers(user, agent_id):
    """DELETE /api/agent-admin/agents/<id>/numbers
    Body: { "workspace_id": int, "phones": [str] }

    Only rows belonging to THIS agent are deleted; numbers assigned to other
    agents (or not assigned at all) are reported in `skipped`.
    """
    agent = _get_owned_agent(user, agent_id)
    if not agent:
        return jsonify({"success": False, "error": "agent_not_found"}), 404

    data = request.get_json(silent=True) or {}
    workspace_id, err = _parse_workspace_id(data)
    if err:
        return err
    ws_err = _workspace_access_error(user, workspace_id)
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
            owner_user_id=user.id,
            meta={"phones": phones, "agent_id": agent.id, "workspace_id": workspace_id,
                  "released": released, "skipped": skipped},
            **request_metadata(),
        )
        db.session.commit()

        return jsonify({"success": True, "released": released, "skipped": skipped})
    except Exception as e:
        db.session.rollback()
        current_app.logger.exception("release_numbers error: %s", e)
        return jsonify({"success": False, "error": "server_error"}), 500


# ============================================================================
# 4) Per-workspace grant settings (inbox_scope / auto_assign)
# ============================================================================
def _grant_payload(grant):
    return {
        "workspace_id": grant.workspace_id,
        "inbox_scope": grant.inbox_scope,
        "auto_assign": grant.auto_assign,
    }


@assignment_bp.route(
    "/agents/<int:agent_id>/workspaces/<int:workspace_id>", methods=["GET"]
)
@require_owner_user
def get_agent_workspace_grant_route(user, agent_id, workspace_id):
    """GET the agent's grant for a workspace (side-effect-free read)."""
    agent = _get_owned_agent(user, agent_id)
    if not agent:
        return jsonify({"success": False, "error": "agent_not_found"}), 404
    ws_err = _workspace_access_error(user, workspace_id)
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


@assignment_bp.route(
    "/agents/<int:agent_id>/workspaces/<int:workspace_id>", methods=["PATCH"]
)
@require_owner_user
def update_agent_workspace_grant(user, agent_id, workspace_id):
    """PATCH /api/agent-admin/agents/<id>/workspaces/<wid>
    Body may contain: inbox_scope ('all' | 'by_chat'), auto_assign (bool).
    A no-op body (or a body that changes nothing) does NOT write an audit row.
    """
    agent = _get_owned_agent(user, agent_id)
    if not agent:
        return jsonify({"success": False, "error": "agent_not_found"}), 404

    ws_err = _workspace_access_error(user, workspace_id)
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
            owner_user_id=user.id,
            meta={"workspace_id": workspace_id,
                  "changes": {k: {"from": v[0], "to": v[1]} for k, v in changes.items()}},
            **request_metadata(),
        )
        db.session.commit()
        return jsonify({"success": True, "grant": _grant_payload(grant)})
    except Exception as e:
        db.session.rollback()
        current_app.logger.exception("update_agent_workspace_grant error: %s", e)
        return jsonify({"success": False, "error": "server_error"}), 500


# ============================================================================
# 5) Workspace auto-assign switch
# ============================================================================
@assignment_bp.route("/workspaces/<int:workspace_id>/autoassign", methods=["GET"])
@require_owner_user
def get_autoassign(user, workspace_id):
    """GET /api/agent-admin/workspaces/<id>/autoassign"""
    err = _workspace_access_error(user, workspace_id)
    if err:
        return err
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
        current_app.logger.exception("get_autoassign error: %s", e)
        return jsonify({"success": False, "error": "server_error"}), 500


@assignment_bp.route("/workspaces/<int:workspace_id>/autoassign", methods=["PATCH"])
@require_owner_user
def set_autoassign(user, workspace_id):
    """PATCH /api/agent-admin/workspaces/<id>/autoassign
    Body: { "enabled": bool } — upserts WorkspaceAutoAssignState.
    """
    err = _workspace_access_error(user, workspace_id)
    if err:
        return err

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
            owner_user_id=user.id,
            meta={"workspace_id": workspace_id, "enabled": enabled},
            **request_metadata(),
        )
        db.session.commit()
        return jsonify({"success": True, "enabled": enabled})
    except Exception as e:
        db.session.rollback()
        current_app.logger.exception("set_autoassign error: %s", e)
        return jsonify({"success": False, "error": "server_error"}), 500
