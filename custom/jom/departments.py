"""
Phase 1 — Departments + routing (JOM vertical).

Service layer only (routes call these). All functions are workspace-scoped and must
be invoked behind is_jom_workspace(). Nothing here runs for any other workspace.

Routing model:
  a QUALIFIED lead's first message text → match a department by keyword →
  round-robin an ACTIVE agent within that department → record it on jom_lifecycle_state.
  If no department matches (or the matched one is inactive/empty) → return None so the
  caller applies the fallback (Phase 5: general queue + owner email).
"""
import logging
from datetime import datetime, timezone

from models import db
from .models import JomDepartment, JomDepartmentAgent, JomLifecycleState
from . import config as cfg

logger = logging.getLogger(__name__)


def _now():
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- #
# Department CRUD
# --------------------------------------------------------------------------- #
def create_department(workspace_id, name, keywords=None, routing_mode=None, sort_order=0):
    dep = JomDepartment(
        workspace_id=int(workspace_id),
        name=name.strip(),
        match_keywords=[k.strip().lower() for k in (keywords or []) if k and k.strip()],
        routing_mode=(routing_mode or cfg.ROUTING_ROUND_ROBIN),
        sort_order=int(sort_order or 0),
        is_active=True,
    )
    db.session.add(dep)
    db.session.commit()
    return dep


def list_departments(workspace_id, active_only=False):
    q = JomDepartment.query.filter_by(workspace_id=int(workspace_id))
    if active_only:
        q = q.filter_by(is_active=True)
    return q.order_by(JomDepartment.sort_order.asc(), JomDepartment.id.asc()).all()


def get_department(workspace_id, department_id):
    return JomDepartment.query.filter_by(id=int(department_id), workspace_id=int(workspace_id)).first()


def update_department(workspace_id, department_id, *, name=None, keywords=None,
                      routing_mode=None, is_active=None, sort_order=None):
    dep = get_department(workspace_id, department_id)
    if not dep:
        return None
    if name is not None:
        dep.name = name.strip()
    if keywords is not None:
        dep.match_keywords = [k.strip().lower() for k in keywords if k and k.strip()]
    if routing_mode is not None:
        dep.routing_mode = routing_mode
    if is_active is not None:
        dep.is_active = bool(is_active)
    if sort_order is not None:
        dep.sort_order = int(sort_order)
    dep.updated_at = _now()
    db.session.commit()
    return dep


def set_active(workspace_id, department_id, active):
    """Seasonal on/off toggle."""
    return update_department(workspace_id, department_id, is_active=bool(active))


def delete_department(workspace_id, department_id):
    dep = get_department(workspace_id, department_id)
    if not dep:
        return False
    db.session.delete(dep)   # cascades to jom_department_agents
    db.session.commit()
    return True


# --------------------------------------------------------------------------- #
# Agent membership (which agents staff a department)
# --------------------------------------------------------------------------- #
def add_agent(workspace_id, department_id, agent_id):
    dep = get_department(workspace_id, department_id)
    if not dep:
        return None
    existing = JomDepartmentAgent.query.filter_by(department_id=dep.id, agent_id=int(agent_id)).first()
    if existing:
        return existing
    link = JomDepartmentAgent(department_id=dep.id, agent_id=int(agent_id))
    db.session.add(link)
    db.session.commit()
    return link


def remove_agent(workspace_id, department_id, agent_id):
    dep = get_department(workspace_id, department_id)
    if not dep:
        return False
    link = JomDepartmentAgent.query.filter_by(department_id=dep.id, agent_id=int(agent_id)).first()
    if not link:
        return False
    db.session.delete(link)
    db.session.commit()
    return True


def list_agent_ids(department_id):
    rows = JomDepartmentAgent.query.filter_by(department_id=int(department_id)).all()
    return [r.agent_id for r in rows]


def move_agent(workspace_id, agent_id, from_department_id, to_department_id):
    """Seasonal reshuffle — move an agent between departments."""
    remove_agent(workspace_id, from_department_id, agent_id)
    return add_agent(workspace_id, to_department_id, agent_id)


# --------------------------------------------------------------------------- #
# Routing
# --------------------------------------------------------------------------- #
def match_department(workspace_id, text):
    """Return the first ACTIVE department whose keyword appears in the text, else None.
    Matching is case-insensitive substring on each keyword; departments checked by
    sort_order then id (deterministic)."""
    if not text:
        return None
    low = str(text).lower()
    for dep in list_departments(workspace_id, active_only=True):
        for kw in (dep.match_keywords or []):
            if kw and kw in low:
                return dep
    return None


def pick_agent_round_robin(department):
    """Return the next ACTIVE agent_id in this department after last_agent_id (strict
    rotation). Inactive/deleted agents are skipped so a lead never routes to someone
    who can't answer. Returns None if the department has no active agents. Advances
    the pointer."""
    all_ids = list_agent_ids(department.id)
    agent_ids = sorted(all_ids)
    if agent_ids:
        try:
            from agent_auth.models import WorkspaceAgent
            active = {
                a.id for a in WorkspaceAgent.query.filter(
                    WorkspaceAgent.id.in_(agent_ids), WorkspaceAgent.is_active.is_(True)
                ).all()
            }
            agent_ids = [i for i in agent_ids if i in active]
        except Exception:  # noqa: BLE001
            pass
    if not agent_ids:
        return None
    last = department.last_agent_id
    if last is None or last not in agent_ids:
        nxt = agent_ids[0]
    else:
        i = agent_ids.index(last)
        nxt = agent_ids[(i + 1) % len(agent_ids)]
    department.last_agent_id = nxt
    department.updated_at = _now()
    db.session.commit()
    return nxt


def _upsert_lifecycle(workspace_id, customer_phone, **fields):
    row = JomLifecycleState.query.filter_by(
        workspace_id=int(workspace_id), customer_phone=str(customer_phone)
    ).first()
    if not row:
        row = JomLifecycleState(workspace_id=int(workspace_id), customer_phone=str(customer_phone))
        db.session.add(row)
    for k, v in fields.items():
        setattr(row, k, v)
    row.updated_at = _now()
    db.session.commit()
    return row


def route_qualified_lead(workspace_id, customer_phone, text):
    """Match department by text → round-robin an agent → record on lifecycle state.
    Returns (department, agent_id) or (None, None) if unrouted (caller = fallback)."""
    dep = match_department(workspace_id, text)
    if not dep:
        _upsert_lifecycle(workspace_id, customer_phone, status=cfg.STATUS_QUALIFIED, department_id=None)
        return None, None
    agent_id = None
    if dep.routing_mode == cfg.ROUTING_ROUND_ROBIN:
        agent_id = pick_agent_round_robin(dep)
    _upsert_lifecycle(
        workspace_id, customer_phone,
        status=cfg.STATUS_ASSIGNED, department_id=dep.id, assigned_agent_id=agent_id,
    )
    if agent_id:
        bridge_to_agent_inbox(workspace_id, customer_phone, agent_id)
    logger.info("[jom] routed %s → dept=%s agent=%s", customer_phone, dep.name, agent_id)
    return dep, agent_id


def resolve_agent_with_fallback(workspace_id, customer_phone):
    """Return the agent a returning lead should go to, honouring the client's rule:
      1) the SAME agent they had — if that agent still exists AND is active,
      2) else another ACTIVE agent in the SAME department (round-robin),
      3) else None (general queue — owner picks up).
    Re-points the inbox bridge to whoever is chosen. Fail-safe."""
    try:
        from agent_auth.models import WorkspaceAgent
        row = _get_lifecycle(workspace_id, customer_phone)
        if not row:
            return None
        # 1) sticky agent still valid?
        aid = row.assigned_agent_id
        if aid:
            a = db.session.get(WorkspaceAgent, int(aid))
            if a and a.is_active:
                bridge_to_agent_inbox(workspace_id, customer_phone, aid)
                return aid
        # 2) same department, another active agent (round-robin)
        if row.department_id:
            dep = JomDepartment.query.filter_by(
                id=row.department_id, workspace_id=int(workspace_id)).first()
            if dep:
                new_aid = pick_agent_round_robin(dep)
                if new_aid:
                    row.assigned_agent_id = new_aid
                    row.updated_at = _now()
                    db.session.commit()
                    bridge_to_agent_inbox(workspace_id, customer_phone, new_aid)
                    logger.info("[jom] %s sticky agent %s gone → reassigned to %s (dept %s)",
                                customer_phone, aid, new_aid, dep.name)
                    return new_aid
        # 3) general queue
        logger.info("[jom] %s → general queue (no valid agent/department)", customer_phone)
        return None
    except Exception as exc:  # noqa: BLE001
        logger.warning("[jom] resolve_agent_with_fallback failed for %s: %s", customer_phone, exc)
        db.session.rollback()
        return None


def _get_lifecycle(workspace_id, customer_phone):
    return JomLifecycleState.query.filter_by(
        workspace_id=int(workspace_id), customer_phone=str(customer_phone)).first()


def bridge_to_agent_inbox(workspace_id, customer_phone, agent_id):
    """Mirror a JOM department assignment into the agent's own scoped inbox.

    Without this the lead is assigned in jom_lifecycle_state but the human agent never
    SEES it — JOM skips SocioChat's standard auto-assign, so nothing else creates the
    agent_number_assignments row. Exclusive per (workspace, phone): if the number is
    already held by another agent we re-point it, which is what makes re-engagement to
    the SAME agent actually show up for that agent.
    Fail-safe: a problem here must never break routing."""
    try:
        from agent_auth.models import AgentNumberAssignment
        try:
            from whatsapp.utils import normalize_phone
            phone = normalize_phone(customer_phone) or str(customer_phone)
        except Exception:  # noqa: BLE001
            phone = str(customer_phone)
        row = AgentNumberAssignment.query.filter_by(
            workspace_id=int(workspace_id), customer_phone=phone).first()
        if row:
            if row.agent_id != int(agent_id):
                row.agent_id = int(agent_id)
                db.session.commit()
                logger.info("[jom] inbox bridge re-pointed %s → agent %s", phone, agent_id)
            return row
        row = AgentNumberAssignment(
            workspace_id=int(workspace_id), customer_phone=phone,
            agent_id=int(agent_id), assignment_type="auto",
        )
        db.session.add(row)
        db.session.commit()
        logger.info("[jom] inbox bridge created %s → agent %s", phone, agent_id)
        return row
    except Exception as exc:  # noqa: BLE001
        logger.warning("[jom] agent-inbox bridge failed for %s: %s", customer_phone, exc)
        db.session.rollback()
        return None
