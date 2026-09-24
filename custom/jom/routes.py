"""
JOM admin/status routes.
- Phase 0: read-only GET /api/jom/status.
- Phase 1: department CRUD + agent membership. All mutating endpoints require an
  authenticated OWNER (reusing agent_auth.require_owner_user), the workspace must be
  OWNED by that user, AND the workspace must be JOM-gated. Non-JOM workspaces get 403.
"""
import logging

from flask import Blueprint, request, jsonify
from sqlalchemy import text

from models import db
from .gate import is_jom_workspace, allowed_workspace_ids
from . import departments as dept_svc

logger = logging.getLogger(__name__)

jom_bp = Blueprint("jom", __name__, url_prefix="/api/jom")

_TABLES = ["jom_departments", "jom_department_agents", "jom_lifecycle_state", "jom_behavior_score"]


# --------------------------------------------------------------------------- #
# Status (read-only)
# --------------------------------------------------------------------------- #
@jom_bp.get("/status")
def jom_status():
    ws = request.args.get("workspace_id")
    tables_ok = {}
    try:
        for t in _TABLES:
            tables_ok[t] = bool(db.session.execute(text("SELECT to_regclass(:t) IS NOT NULL"), {"t": t}).scalar())
    except Exception as exc:  # noqa: BLE001
        logger.warning("[jom] status table check failed: %s", exc)
    return jsonify({
        "vertical": "jom", "phase": 1,
        "allowed_workspace_ids": allowed_workspace_ids(),
        "workspace_id": ws,
        "gated_for_workspace": is_jom_workspace(ws) if ws is not None else None,
        "tables": tables_ok,
        "all_tables_present": all(tables_ok.get(t) for t in _TABLES) if tables_ok else False,
    })


# --------------------------------------------------------------------------- #
# Guard shared by all admin endpoints
# --------------------------------------------------------------------------- #
def _guard(user, workspace_id):
    """Return an (error_response, status) tuple or None if allowed."""
    try:
        wsid = int(workspace_id)
    except (TypeError, ValueError):
        return jsonify({"success": False, "error": "workspace_id (int) required"}), 400
    if not is_jom_workspace(wsid):
        return jsonify({"success": False, "error": "not_enabled_for_workspace"}), 403
    try:
        from agent_auth.routes import _owned_workspace_ids
        if wsid not in set(_owned_workspace_ids(user)):
            return jsonify({"success": False, "error": "forbidden"}), 403
    except Exception:  # noqa: BLE001
        return jsonify({"success": False, "error": "forbidden"}), 403
    return None


def _dep_json(d):
    return {
        "id": d.id, "workspace_id": d.workspace_id, "name": d.name,
        "match_keywords": d.match_keywords, "routing_mode": d.routing_mode,
        "is_active": d.is_active, "sort_order": d.sort_order,
        "last_agent_id": d.last_agent_id, "agent_ids": dept_svc.list_agent_ids(d.id),
    }


def _wsid():
    return request.args.get("workspace_id") or (request.get_json(silent=True) or {}).get("workspace_id")


# --------------------------------------------------------------------------- #
# Department CRUD (Phase 1) — lazy import require_owner_user to avoid import cycles
# --------------------------------------------------------------------------- #
from agent_auth.decorators import require_owner_user  # noqa: E402


@jom_bp.get("/departments")
@require_owner_user
def list_departments(user):
    err = _guard(user, _wsid())
    if err:
        return err
    deps = dept_svc.list_departments(int(_wsid()))
    return jsonify({"success": True, "departments": [_dep_json(d) for d in deps]})


@jom_bp.post("/departments")
@require_owner_user
def create_department(user):
    body = request.get_json(silent=True) or {}
    err = _guard(user, body.get("workspace_id"))
    if err:
        return err
    if not (body.get("name") or "").strip():
        return jsonify({"success": False, "error": "name required"}), 400
    d = dept_svc.create_department(
        int(body["workspace_id"]), body["name"],
        keywords=body.get("match_keywords") or body.get("keywords") or [],
        routing_mode=body.get("routing_mode"), sort_order=body.get("sort_order", 0),
    )
    return jsonify({"success": True, "department": _dep_json(d)}), 201


@jom_bp.patch("/departments/<int:department_id>")
@require_owner_user
def update_department(user, department_id):
    body = request.get_json(silent=True) or {}
    err = _guard(user, body.get("workspace_id"))
    if err:
        return err
    d = dept_svc.update_department(
        int(body["workspace_id"]), department_id,
        name=body.get("name"), keywords=body.get("match_keywords"),
        routing_mode=body.get("routing_mode"), is_active=body.get("is_active"),
        sort_order=body.get("sort_order"),
    )
    if not d:
        return jsonify({"success": False, "error": "not_found"}), 404
    return jsonify({"success": True, "department": _dep_json(d)})


@jom_bp.delete("/departments/<int:department_id>")
@require_owner_user
def delete_department(user, department_id):
    err = _guard(user, _wsid())
    if err:
        return err
    ok = dept_svc.delete_department(int(_wsid()), department_id)
    return jsonify({"success": ok}), (200 if ok else 404)


@jom_bp.post("/departments/<int:department_id>/agents")
@require_owner_user
def add_agent(user, department_id):
    body = request.get_json(silent=True) or {}
    err = _guard(user, body.get("workspace_id"))
    if err:
        return err
    if not body.get("agent_id"):
        return jsonify({"success": False, "error": "agent_id required"}), 400
    link = dept_svc.add_agent(int(body["workspace_id"]), department_id, int(body["agent_id"]))
    if link is None:
        return jsonify({"success": False, "error": "department_not_found"}), 404
    return jsonify({"success": True, "agent_ids": dept_svc.list_agent_ids(department_id)}), 201


@jom_bp.delete("/departments/<int:department_id>/agents/<int:agent_id>")
@require_owner_user
def remove_agent(user, department_id, agent_id):
    err = _guard(user, _wsid())
    if err:
        return err
    ok = dept_svc.remove_agent(int(_wsid()), department_id, agent_id)
    return jsonify({"success": ok, "agent_ids": dept_svc.list_agent_ids(department_id)}), (200 if ok else 404)


# --------------------------------------------------------------------------- #
# Lead tracker (read-only) — lifecycle rows joined with department + agent names
# --------------------------------------------------------------------------- #
@jom_bp.get("/leads")
@require_owner_user
def list_leads(user):
    err = _guard(user, _wsid())
    if err:
        return err
    wsid = int(_wsid())
    from .models import JomLifecycleState, JomBehaviorScore, JomDepartment
    from agent_auth.models import WorkspaceAgent

    status_filter = (request.args.get("status") or "").strip().lower()
    q = JomLifecycleState.query.filter_by(workspace_id=wsid)
    if status_filter:
        q = q.filter(JomLifecycleState.status == status_filter)
    rows = q.order_by(JomLifecycleState.id.desc()).limit(500).all()

    # bulk lookups
    dep_names = {d.id: d.name for d in JomDepartment.query.filter_by(workspace_id=wsid).all()}
    agent_ids = {r.assigned_agent_id for r in rows if r.assigned_agent_id}
    agent_names = {}
    if agent_ids:
        for a in WorkspaceAgent.query.filter(WorkspaceAgent.id.in_(agent_ids)).all():
            agent_names[a.id] = a.display_name or a.username
    scores = {}
    for b in JomBehaviorScore.query.filter_by(workspace_id=wsid).all():
        scores[b.customer_phone] = b

    def _row(r):
        b = scores.get(r.customer_phone)
        return {
            "customer_phone": r.customer_phone,
            "status": r.status,
            "department_id": r.department_id,
            "department": dep_names.get(r.department_id),
            "assigned_agent_id": r.assigned_agent_id,
            "agent": agent_names.get(r.assigned_agent_id),
            "drip_stage": r.drip_stage,
            "status_since": r.status_since.isoformat() if r.status_since else None,
            "next_followup_at": r.next_followup_at.isoformat() if r.next_followup_at else None,
            "last_inbound_at": r.last_inbound_at.isoformat() if r.last_inbound_at else None,
            "last_outbound_at": r.last_outbound_at.isoformat() if r.last_outbound_at else None,
            "reopen_count": r.reopen_count,
            "close_reason": getattr(r, "close_reason", None),
            # memory card — what we know about this traveller
            "customer_name": getattr(r, "customer_name", None),
            "destination": getattr(r, "destination", None),
            "budget": getattr(r, "budget", None),
            "travel_dates": getattr(r, "travel_dates", None),
            "party_size": getattr(r, "party_size", None),
            "meta_lead_id": r.meta_lead_id,
            "email": r.email,
            "total_inbound": getattr(b, "total_inbound", None),
            "total_outbound": getattr(b, "total_outbound", None),
            "consecutive_no_reply": getattr(b, "consecutive_no_reply", None),
            "last_intent": getattr(b, "last_intent", None),
            "last_confidence": getattr(b, "last_confidence", None),
            "engagement_score": getattr(b, "engagement_score", None),
        }

    leads = [_row(r) for r in rows]
    # status counts across the (unfiltered) workspace for the summary strip
    counts = {}
    for (st, c) in db.session.query(JomLifecycleState.status, db.func.count()).filter_by(
        workspace_id=wsid
    ).group_by(JomLifecycleState.status).all():
        counts[st] = c
    return jsonify({"success": True, "leads": leads, "counts": counts, "total": len(leads)})


# --------------------------------------------------------------------------- #
# Daily outcome report — "how many went Closed / Dormant / Ghosted / Qualified
# each day", with the close reason broken out. Drives the CRM date filter.
# --------------------------------------------------------------------------- #
@jom_bp.get("/leads/summary")
@require_owner_user
def leads_summary(user):
    err = _guard(user, _wsid())
    if err:
        return err
    wsid = int(_wsid())
    from .models import JomLifecycleState

    try:
        days = max(1, min(int(request.args.get("days", 30)), 365))
    except (TypeError, ValueError):
        days = 30

    # Per-day x per-status counts, keyed on when the status was set.
    day = db.func.date(JomLifecycleState.status_since)
    rows = (
        db.session.query(day.label("d"), JomLifecycleState.status, db.func.count())
        .filter(
            JomLifecycleState.workspace_id == wsid,
            JomLifecycleState.status_since.isnot(None),
            JomLifecycleState.status_since >= db.func.now() - text(f"INTERVAL '{days} days'"),
        )
        .group_by(day, JomLifecycleState.status)
        .order_by(day.desc())
        .all()
    )
    by_day = {}
    for d, status, count in rows:
        key = str(d)
        by_day.setdefault(key, {})[status] = count

    # Why leads closed (not_interested / booked_elsewhere / drip_exhausted)
    reasons = {}
    for reason, count in (
        db.session.query(JomLifecycleState.close_reason, db.func.count())
        .filter(
            JomLifecycleState.workspace_id == wsid,
            JomLifecycleState.status == "closed",
            JomLifecycleState.close_reason.isnot(None),
        )
        .group_by(JomLifecycleState.close_reason)
        .all()
    ):
        reasons[reason] = count

    totals = {}
    for statuses in by_day.values():
        for st, c in statuses.items():
            totals[st] = totals.get(st, 0) + c

    return jsonify({
        "success": True,
        "days": days,
        "by_day": by_day,        # {"2026-09-25": {"closed": 3, "dormant": 5}, ...}
        "totals": totals,        # rolled up across the window
        "close_reasons": reasons,
    })
