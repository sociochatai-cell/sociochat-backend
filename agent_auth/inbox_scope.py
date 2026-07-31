# agent_auth/inbox_scope.py
"""
Level 3 — inbox/number scoping helpers + auto-assign engine.
============================================================
Single source of truth consumed by:
- whatsapp/routes.py + services.py  (narrow the inbox for agent principals)
- whatsapp/webhook.py               (auto-assign new inbound numbers, Phase 3)
- agent_auth/assignment_routes.py   (owner assignment management)

Phone normalization MUST match what the webhook stores on conversations —
always go through normalize_customer_phone() below.
"""

import logging
from datetime import datetime, timezone

from models import db
from .models import (
    WorkspaceAgent,
    AgentWorkspace,
    AgentNumberAssignment,
    WorkspaceAutoAssignState,
)

logger = logging.getLogger(__name__)


def normalize_customer_phone(phone: str) -> str:
    """Normalize a customer phone the SAME way the webhook does before storing
    conversation.user_phone, so assignment rows always match conversations."""
    raw = (phone or "").strip()
    try:
        from whatsapp.utils import normalize_phone
        return normalize_phone(raw) or raw
    except Exception:
        # Fallback: digits only (defensive; whatsapp.utils should always import)
        return "".join(ch for ch in raw if ch.isdigit() or ch == "+")


# ---------------------------------------------------------------------------
# Read-side scoping (what may this agent see?)
# ---------------------------------------------------------------------------
def get_agent_workspace_grant(agent_id: int, workspace_id: int):
    """The AgentWorkspace row for (agent, workspace), or None if not granted."""
    try:
        wid = int(workspace_id)
    except (TypeError, ValueError):
        return None
    return AgentWorkspace.query.filter_by(agent_id=agent_id, workspace_id=wid).first()


def get_agent_inbox_scope(agent_id: int, workspace_id: int) -> str | None:
    """'all' | 'by_chat' for a granted workspace, None if the workspace is not
    granted at all (caller must treat None as NO ACCESS)."""
    grant = get_agent_workspace_grant(agent_id, workspace_id)
    if grant is None:
        return None
    return grant.inbox_scope if grant.inbox_scope in AgentWorkspace.INBOX_SCOPES else "all"


def get_assigned_phones(agent_id: int, workspace_id: int) -> list:
    """All customer phones assigned to this agent in this workspace."""
    rows = AgentNumberAssignment.query.filter_by(
        agent_id=agent_id, workspace_id=int(workspace_id)
    ).all()
    return [r.customer_phone for r in rows]


def agent_allowed_phones(agent_id: int, workspace_id: int):
    """The read-side contract for inbox narrowing.

    Returns:
        None  -> no phone filter needed (scope 'all')
        list  -> ONLY these phones are visible (may be EMPTY = sees nothing)

    A non-granted workspace also returns [] (fail closed) — though the Level-2
    gate should have rejected the request before this point.
    """
    scope = get_agent_inbox_scope(agent_id, workspace_id)
    if scope is None:
        return []
    if scope == "all":
        return None
    return get_assigned_phones(agent_id, workspace_id)


def phone_visible_to_agent(agent_id: int, workspace_id: int, customer_phone: str) -> bool:
    """May this agent open/act on a conversation with this customer phone?"""
    allowed = agent_allowed_phones(agent_id, workspace_id)
    if allowed is None:
        return True
    normalized = normalize_customer_phone(customer_phone)
    allowed_set = set(allowed)
    return customer_phone in allowed_set or normalized in allowed_set


# ---------------------------------------------------------------------------
# Write-side "New Chat" claim (agent starts a brand-new conversation)
# ---------------------------------------------------------------------------
def resolve_agent_claim(agent_id: int, workspace_id: int, phone: str, *, create: bool):
    """Decide (and optionally record) whether ``agent_id`` may message ``phone``.

    Used when an agent sends the FIRST message to a number (starting a new chat).
    Returns a tuple ``(allowed: bool, reason: str | None)``:

    - workspace not granted to the agent      -> (False, "workspace_not_granted")
    - inbox scope 'all'                        -> (True, None)
    - scope 'by_chat', number is THIS agent's  -> (True, None)
    - scope 'by_chat', number is ANOTHER agent's -> (False, "assigned_to_other")
    - scope 'by_chat', number is unassigned:
        * create=True  -> add a 'manual' assignment to THIS agent (NOT committed
                          here — the caller's transaction commits it) -> (True, None)
        * create=False -> (True, None) preview only, no row written

    Never raises: any unexpected error returns (False, "error").
    """
    try:
        phone = normalize_customer_phone(phone)
        scope = get_agent_inbox_scope(agent_id, workspace_id)
        if scope is None:
            return (False, "workspace_not_granted")
        if scope == "all":
            return (True, None)

        # scope == "by_chat": exclusive per (workspace, phone) assignment.
        existing = AgentNumberAssignment.query.filter_by(
            workspace_id=int(workspace_id), customer_phone=phone
        ).first()
        if existing is not None:
            if existing.agent_id == agent_id:
                return (True, None)
            return (False, "assigned_to_other")

        if create:
            db.session.add(AgentNumberAssignment(
                agent_id=agent_id,
                workspace_id=int(workspace_id),
                customer_phone=phone,
                assignment_type="manual",
                assigned_by_user_id=None,
            ))
        return (True, None)
    except Exception:
        logger.exception(
            "resolve_agent_claim failed agent=%s ws=%s", agent_id, workspace_id
        )
        return (False, "error")


def claim_number_for_agent(agent_id: int, workspace_id: int, phone: str):
    """Claim ``phone`` for ``agent_id`` (writes the assignment on first contact)."""
    return resolve_agent_claim(agent_id, workspace_id, phone, create=True)


def preview_claim_for_agent(agent_id: int, workspace_id: int, phone: str):
    """Read-only preview of a claim — no assignment row is written."""
    return resolve_agent_claim(agent_id, workspace_id, phone, create=False)


# ---------------------------------------------------------------------------
# Phase 3 — auto-assign engine (round-robin)
# ---------------------------------------------------------------------------
def auto_assign_new_number(workspace_id: int, customer_phone: str):
    """Round-robin assign a brand-new inbound customer number, if enabled.

    Call ONLY from the genuine-inbound path when a NEW conversation was just
    created. Safe by design:
    - no-op when the workspace's auto-assign is off or no agent participates
    - no-op when the number already has an assignment (advance/manual wins)
    - unique constraint absorbs concurrent duplicates; the row lock on the
      state pointer serializes the rotation.
    - NEVER raises: any failure logs and returns None (webhook must not break).

    Returns the AgentNumberAssignment created, or None. Does NOT commit — the
    caller's transaction (webhook message handling) commits it atomically.
    """
    try:
        wid = int(workspace_id)
        phone = normalize_customer_phone(customer_phone)
        if not phone:
            return None

        # Lock the rotation pointer row (serializes concurrent inbound messages).
        state = (
            db.session.query(WorkspaceAutoAssignState)
            .filter_by(workspace_id=wid)
            .with_for_update()
            .first()
        )
        if state is None or not state.enabled:
            return None

        # Advance/manual assignment wins; also absorbs racing duplicates.
        existing = AgentNumberAssignment.query.filter_by(
            workspace_id=wid, customer_phone=phone
        ).first()
        if existing is not None:
            return None

        # Participants: ACTIVE agents granted this workspace with auto_assign on
        # and chat-scoped inbox (an 'all'-scope agent sees everything anyway).
        participants = (
            db.session.query(AgentWorkspace)
            .join(WorkspaceAgent, WorkspaceAgent.id == AgentWorkspace.agent_id)
            .filter(
                AgentWorkspace.workspace_id == wid,
                AgentWorkspace.auto_assign.is_(True),
                AgentWorkspace.inbox_scope == "by_chat",
                WorkspaceAgent.is_active.is_(True),
            )
            .order_by(AgentWorkspace.agent_id.asc())
            .all()
        )
        if not participants:
            return None

        ids = [p.agent_id for p in participants]
        # Strict round-robin: next id after the last assigned one (wraps).
        if state.last_agent_id in ids:
            next_agent_id = ids[(ids.index(state.last_agent_id) + 1) % len(ids)]
        else:
            next_agent_id = ids[0]

        assignment = AgentNumberAssignment(
            agent_id=next_agent_id,
            workspace_id=wid,
            customer_phone=phone,
            assignment_type="auto",
            assigned_by_user_id=None,
        )
        db.session.add(assignment)
        state.last_agent_id = next_agent_id
        state.updated_at = datetime.now(timezone.utc)

        logger.info(
            "auto_assign ws=%s phone=%s -> agent=%s", wid, phone, next_agent_id
        )
        return assignment
    except Exception:
        logger.exception("auto_assign_new_number failed ws=%s", workspace_id)
        return None
