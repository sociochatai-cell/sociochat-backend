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
# Manual status change — a human (owner/agent) can override the automation for one
# lead: mark booked/closed, pull someone back to active, correct a wrong status.
# Enrols/stops the drip correctly via the lifecycle helpers so the state stays coherent.
# --------------------------------------------------------------------------- #
_MANUAL_STATUSES = {"new", "contacted", "qualified", "assigned", "active", "ghosted", "dormant", "closed"}


# Demo leads use a distinctive phone prefix so they can never collide with a real
# customer and can be wiped in one call before go-live.
_DEMO_PREFIX = "DEMO"


@jom_bp.post("/leads/demo-seed")
@require_owner_user
def demo_seed(user):
    body = request.get_json(silent=True) or {}
    err = _guard(user, body.get("workspace_id"))
    if err:
        return err
    wsid = int(body["workspace_id"])
    from datetime import datetime, timezone, timedelta
    from .models import JomLifecycleState, JomBehaviorScore, JomDepartment
    now = datetime.now(timezone.utc)
    depmap = {d.name: d.id for d in JomDepartment.query.filter_by(workspace_id=wsid).all()}

    def dep(name):
        return depmap.get(name)

    rows = [
        # name, phone, destination, budget, status, dept, days_ago, drip_stage, close_reason
        ("Rahul Sharma", "DEMO0001", "Bali", "~1.2L", "qualified", "Bali", 0, None, None),
        ("Anita Verma", "DEMO0002", "Kashmir", "~90k", "assigned", "Kashmir", 1, None, None),
        ("Vikram Singh", "DEMO0003", "Vietnam", "~1.5L", "ghosted", "Vietnam", 7, "0", None),
        ("Sara Khan", "DEMO0004", "Thailand", "~1.1L", "dormant", "Thailand", 14, "1", None),
        ("Deepa Nair", "DEMO0005", "Bali", "~2L", "closed", "Bali", 20, None, "booked_elsewhere"),
        ("Arjun Rao", "DEMO0006", "Ladakh", "~80k", "new", "Ladakh", 0, None, None),
    ]
    created = 0
    for name, phone, dest, budget, status, dname, days_ago, stage, close_reason in rows:
        JomLifecycleState.query.filter_by(workspace_id=wsid, customer_phone=phone).delete()
        JomBehaviorScore.query.filter_by(workspace_id=wsid, customer_phone=phone).delete()
        r = JomLifecycleState(
            workspace_id=wsid, customer_phone=phone, status=status,
            department_id=dep(dname), customer_name=name, destination=dest, budget=budget,
            status_since=now - timedelta(days=days_ago),
            last_inbound_at=now - timedelta(days=days_ago),
            drip_stage=stage,
            next_followup_at=(now + timedelta(days=3)) if status in ("ghosted", "dormant") else None,
            close_reason=close_reason,
            last_summary=f"Was planning a {dest} trip.",
        )
        db.session.add(r)
        db.session.add(JomBehaviorScore(workspace_id=wsid, customer_phone=phone,
                                        total_inbound=3, engagement_score=3))
        created += 1
    db.session.commit()
    return jsonify({"success": True, "seeded": created, "note": "demo leads (phone prefix DEMO) — wipe via /leads/demo-clear"})


@jom_bp.post("/leads/demo-clear")
@require_owner_user
def demo_clear(user):
    body = request.get_json(silent=True) or {}
    err = _guard(user, body.get("workspace_id"))
    if err:
        return err
    wsid = int(body["workspace_id"])
    from .models import JomLifecycleState, JomBehaviorScore
    n = JomLifecycleState.query.filter(
        JomLifecycleState.workspace_id == wsid,
        JomLifecycleState.customer_phone.like(f"{_DEMO_PREFIX}%")).delete(synchronize_session=False)
    JomBehaviorScore.query.filter(
        JomBehaviorScore.workspace_id == wsid,
        JomBehaviorScore.customer_phone.like(f"{_DEMO_PREFIX}%")).delete(synchronize_session=False)
    db.session.commit()
    return jsonify({"success": True, "cleared": n})


@jom_bp.patch("/leads/<path:customer_phone>/status")
@require_owner_user
def set_lead_status(user, customer_phone):
    body = request.get_json(silent=True) or {}
    err = _guard(user, body.get("workspace_id"))
    if err:
        return err
    wsid = int(body["workspace_id"])
    new_status = str(body.get("status") or "").strip().lower()
    reason = body.get("reason")
    if new_status not in _MANUAL_STATUSES:
        return jsonify({"success": False, "error": "invalid_status",
                        "allowed": sorted(_MANUAL_STATUSES)}), 400

    from .models import JomLifecycleState
    from . import lifecycle
    row = JomLifecycleState.query.filter_by(workspace_id=wsid, customer_phone=str(customer_phone)).first()
    if not row:
        return jsonify({"success": False, "error": "lead_not_found"}), 404
    old = row.status

    # Route through the lifecycle helpers so drips/close reasons stay consistent, rather
    # than blindly overwriting the column.
    if new_status == "ghosted":
        lifecycle.mark_ghosted(wsid, customer_phone)
    elif new_status == "dormant":
        lifecycle.mark_dormant(wsid, customer_phone)
    elif new_status == "closed":
        lifecycle.mark_closed(wsid, customer_phone, reason=reason or "manual")
    elif new_status == "active":
        # bring them back into conversation, keep the sticky agent, stop any drip
        row.status = "active"
        row.next_followup_at = None
        row.drip_stage = None
        row.close_reason = None
        row.updated_at = db.func.now()
        db.session.commit()
        lifecycle._fire_status_change(wsid, customer_phone, old, "active")
    else:
        # new / contacted / qualified / assigned — set directly, clear any pending drip
        row.status = new_status
        if new_status in ("new", "contacted", "qualified"):
            row.next_followup_at = None
            row.drip_stage = None
        row.updated_at = db.func.now()
        db.session.commit()
        lifecycle._fire_status_change(wsid, customer_phone, old, new_status)

    row = JomLifecycleState.query.filter_by(workspace_id=wsid, customer_phone=str(customer_phone)).first()
    logger.info("[jom] manual status change %s: %s -> %s (by user %s)",
                customer_phone, old, row.status, getattr(user, "id", None))
    return jsonify({"success": True, "customer_phone": str(customer_phone),
                    "old_status": old, "status": row.status})


# --------------------------------------------------------------------------- #
# Settings — owner-editable cadence / qualify threshold / template mapping.
# GET returns the merged live view (defaults + overrides) plus the workspace's
# Meta-approved templates so the UI can offer them in dropdowns. PATCH upserts.
# --------------------------------------------------------------------------- #
def _approved_templates(wsid):
    """Best-effort list of the workspace's approved WhatsApp template names, for the
    template-picker dropdowns. Fail-safe: returns [] if the account/table isn't there."""
    try:
        from whatsapp.models import WhatsAppAccount, WhatsAppTemplate  # type: ignore
        acc = WhatsAppAccount.query.filter_by(workspace_id=str(wsid)).first()
        if not acc:
            return []
        q = WhatsAppTemplate.query.filter_by(account_id=acc.id)
        names = []
        for t in q.all():
            if getattr(t, "is_archived", False):
                continue
            status = (getattr(t, "meta_status", None) or getattr(t, "status", "") or "").upper()
            if status in ("", "APPROVED"):
                nm = getattr(t, "name", None)
                if nm:
                    names.append(nm)
        return sorted(set(names))
    except Exception as exc:  # noqa: BLE001
        logger.debug("[jom] approved-template lookup unavailable: %s", exc)
        return []


@jom_bp.get("/settings")
@require_owner_user
def get_settings_route(user):
    err = _guard(user, _wsid())
    if err:
        return err
    wsid = int(_wsid())
    from . import settings as jom_settings
    data = jom_settings.get_settings(wsid)
    return jsonify({
        "success": True,
        "settings": data,
        "template_keys": list(jom_settings.DEFAULT_TEMPLATES.keys()),
        "approved_templates": _approved_templates(wsid),
    })


@jom_bp.patch("/settings")
@require_owner_user
def patch_settings_route(user):
    body = request.get_json(silent=True) or {}
    err = _guard(user, body.get("workspace_id"))
    if err:
        return err
    wsid = int(body["workspace_id"])
    from . import settings as jom_settings

    def _days(val):
        if val is None:
            return None
        if not isinstance(val, list) or not val:
            raise ValueError("cadence must be a non-empty list of day offsets")
        out = sorted({int(x) for x in val if int(x) >= 0})
        if not out:
            raise ValueError("cadence must contain at least one day offset")
        return out

    try:
        ghosted = _days(body.get("ghosted_days"))
        dormant = _days(body.get("dormant_days"))
        qmt = body.get("qualify_max_turns")
        if qmt is not None:
            qmt = int(qmt)
            if qmt < 1 or qmt > 50:
                raise ValueError("qualify_max_turns must be between 1 and 50")
        templates = body.get("templates")
        if templates is not None and not isinstance(templates, dict):
            raise ValueError("templates must be an object")
    except (TypeError, ValueError) as exc:
        return jsonify({"success": False, "error": str(exc)}), 400

    data = jom_settings.save(
        wsid, ghosted_days=ghosted, dormant_days=dormant,
        qualify_max_turns=qmt, templates=templates,
    )
    logger.info("[jom] settings updated for ws %s by user %s", wsid, getattr(user, "id", None))
    return jsonify({"success": True, "settings": data})


# --------------------------------------------------------------------------- #
# Google Sheet lead intake — paste a share link, we poll it and import new leads
# (firing the opening template). GET returns config + health; POST saves; the
# /sync endpoint runs it on demand and returns the result immediately.
# --------------------------------------------------------------------------- #
@jom_bp.get("/sheet")
@require_owner_user
def get_sheet(user):
    err = _guard(user, _wsid())
    if err:
        return err
    from . import sheet as jom_sheet
    return jsonify({"success": True, "config": jom_sheet.config_json(int(_wsid()))})


@jom_bp.post("/sheet")
@require_owner_user
def save_sheet(user):
    body = request.get_json(silent=True) or {}
    err = _guard(user, body.get("workspace_id"))
    if err:
        return err
    wsid = int(body["workspace_id"])
    from . import sheet as jom_sheet
    sheet_url = body.get("sheet_url")
    if sheet_url:
        # validate it looks like a Google Sheet before saving, so the health badge
        # isn't the first place the owner learns the link is wrong.
        if jom_sheet.csv_export_url(sheet_url) is None:
            return jsonify({"success": False, "error": "Not a valid Google Sheets link."}), 400
    cfg_json = jom_sheet.save_config(
        wsid,
        sheet_url=sheet_url,
        enabled=body.get("enabled"),
        header_map=body.get("header_map"),
    )
    logger.info("[jom] sheet config saved for ws %s by user %s", wsid, getattr(user, "id", None))
    return jsonify({"success": True, "config": cfg_json})


@jom_bp.post("/sheet/sync")
@require_owner_user
def sync_sheet(user):
    body = request.get_json(silent=True) or {}
    err = _guard(user, body.get("workspace_id"))
    if err:
        return err
    wsid = int(body["workspace_id"])
    from . import sheet as jom_sheet
    # A manual sync can optionally skip messaging (dry preview of what would import).
    send_opening = body.get("send_opening", True)
    result = jom_sheet.sync_workspace(wsid, send_opening=bool(send_opening))
    status = 200 if result.get("success") else 400
    return jsonify({**result, "config": jom_sheet.config_json(wsid)}), status


# --------------------------------------------------------------------------- #
# Agent-scheduled one-off follow-ups. Unlike the admin endpoints above, these are
# usable by the ASSIGNED AGENT as well as the owner (agents schedule from their
# inbox). We therefore authenticate manually: an agent token assigned to this JOM
# workspace, OR the workspace owner. Fired by the existing scheduler tick.
# --------------------------------------------------------------------------- #
def _schedule_actor(workspace_id):
    """(kind, actor_id, None) for an allowed owner/agent, else (None, None, (resp, status))."""
    try:
        wsid = int(workspace_id)
    except (TypeError, ValueError):
        return None, None, (jsonify({"success": False, "error": "workspace_id (int) required"}), 400)
    if not is_jom_workspace(wsid):
        return None, None, (jsonify({"success": False, "error": "not_enabled_for_workspace"}), 403)
    from auth_core import authenticated_agent_id
    if authenticated_agent_id() is not None:
        from agent_auth.security import get_current_agent
        ag = get_current_agent()
        if ag and ag.has_workspace(wsid):
            return "agent", ag.id, None
        return None, None, (jsonify({"success": False, "error": "forbidden"}), 403)
    from tenant.context import get_current_user
    user = get_current_user()
    if not user:
        return None, None, (jsonify({"success": False, "error": "authentication_required"}), 401)
    try:
        from agent_auth.routes import _owned_workspace_ids
        if wsid in set(_owned_workspace_ids(user)):
            return "owner", user.id, None
    except Exception:  # noqa: BLE001
        pass
    return None, None, (jsonify({"success": False, "error": "forbidden"}), 403)


@jom_bp.post("/leads/<path:customer_phone>/schedule")
def create_schedule(customer_phone):
    body = request.get_json(silent=True) or {}
    kind, actor_id, err = _schedule_actor(body.get("workspace_id"))
    if err:
        return err
    wsid = int(body["workspace_id"])
    template_name = (body.get("template_name") or "").strip()
    variables = body.get("variables") or {}
    note = (body.get("note") or "").strip() or None
    when = body.get("scheduled_at")   # ISO-8601 UTC string from the client
    if not template_name:
        return jsonify({"success": False, "error": "template_name required"}), 400
    if template_name not in set(_approved_templates(wsid)) and _approved_templates(wsid):
        return jsonify({"success": False, "error": "template not approved for this account"}), 400
    if not when:
        return jsonify({"success": False, "error": "scheduled_at required"}), 400
    from datetime import datetime, timezone
    try:
        dt = datetime.fromisoformat(str(when).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return jsonify({"success": False, "error": "scheduled_at must be ISO-8601"}), 400

    from . import scheduled as jom_sched
    row, e = jom_sched.create(
        wsid, customer_phone, template_name, dt,
        variables=variables, note=note, created_by_kind=kind, created_by_id=actor_id,
    )
    if e:
        return jsonify({"success": False, "error": e}), 400
    logger.info("[jom] schedule created for %s by %s#%s", customer_phone, kind, actor_id)
    return jsonify({"success": True, "schedule": row}), 201


@jom_bp.get("/leads/<path:customer_phone>/schedules")
def list_schedules(customer_phone):
    kind, actor_id, err = _schedule_actor(_wsid())
    if err:
        return err
    from . import scheduled as jom_sched
    rows = jom_sched.list_for_lead(int(_wsid()), customer_phone)
    return jsonify({"success": True, "schedules": rows})


@jom_bp.post("/schedules/<int:schedule_id>/cancel")
def cancel_schedule(schedule_id):
    body = request.get_json(silent=True) or {}
    kind, actor_id, err = _schedule_actor(body.get("workspace_id"))
    if err:
        return err
    from . import scheduled as jom_sched
    row, e = jom_sched.cancel(int(body["workspace_id"]), schedule_id)
    if e:
        return jsonify({"success": False, "error": e}), (404 if e == "not_found" else 400)
    return jsonify({"success": True, "schedule": row})


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
