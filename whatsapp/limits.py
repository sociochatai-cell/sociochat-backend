"""
WhatsApp reactive-flow plan gate (whatsapp-service).
====================================================

The interactive visual-flow engine (``interactive_automation_engine``) sends
replies that bypass the manual-send capability gate in ``routes.py`` — they go
straight through ``WhatsAppService._send_api_request``. This leaf module provides
a check-only, fail-open plan gate applied ONCE per inbound flow turn in
``process_interactive_automation`` so a paid tier's monthly WhatsApp cap
(``whatsapp_messages_total``) and the plan-level ``whatsapp_automation`` feature
flag are enforced for flow sends too.

Usage is recorded elsewhere in this service (the ``message_sent`` accounting
event + ``record_interactive_flow_trigger``), so this gate NEVER records — it
only decides allow/deny.

Fail policy mirrors the monolith reactive gate:

  * resolution failures fail OPEN (never break a live flow on a lookup miss);
  * check errors fail CLOSED for capped ai_* plans (never silently exceed a paid
    cap), OPEN otherwise.

The ``whatsapp_automation`` feature is checked by PLAN VALUE only (never via
``check_feature_access``/account status) so a customer's status can never switch
their flows off as a side effect. The per-day ``check_message_limit`` is
intentionally NOT applied: it is a no-op for paid ai_* plans and would otherwise
newly daily-cap legacy .com plans' flows — a behaviour change we avoid.

SCOPE — what this gate covers in whatsapp-service:
  * interactive/visual flows  — gated once per turn at process_interactive_automation.
  * rule-based / keyword / FAQ / AI customer auto-replies  — gated via
    WhatsAppService(meter_as_reactive=True) in automation_engine.send_automation_response.
  * the AI "placeholder" reply  — gated via fast_router's placeholder sender.
INTENTIONALLY NOT gated (documented exemptions, not oversights):
  * the OWNER agentic assistant (agentos_handoff / fast_router agentic service) —
    owner-facing tooling, not a customer automation reply;
  * transactional human-escalation notices ("a team member will assist you") —
    service messages the customer should still receive;
  * the flow-resume "reprompt" nudge sent after an off-script answer — it reaches
    the interactive engine WITHOUT re-entering process_interactive_automation, so
    one resume nudge may go out even when over-cap (benign; the flow itself is
    gated at entry and the primary reply is gated at send).
  Revisit these if the product decides owner-assistant / escalation / resume
  traffic must also count against the plan's WhatsApp entitlement.
"""

from __future__ import annotations

import logging
from typing import Optional, Tuple

logger = logging.getLogger(__name__)


def _safe_rollback():
    """Roll back the shared session so a DB fault inside the gate can't leave it
    aborted (which would stall the caller's post-send persistence). Never raises."""
    try:
        from shared_models import db

        db.session.rollback()
    except Exception:
        pass


def gate_reactive_send(account, db_session=None) -> Tuple[bool, Optional[str], object, Optional[int]]:
    """Decide whether a reactive interactive-flow turn may send.

    CHECK ONLY. Returns ``(allowed, reason, user, wid)``. NEVER raises.
    """
    if account is None:
        return True, None, None, None

    from shared_models import db, User, Workspace
    from subscription.constants import AI_PLANS, UNLIMITED, get_plan_features, is_unlimited
    from subscription.service import get_user_plan, check_whatsapp_total_for_user

    # ── Resolution phase — always fail OPEN ──
    try:
        wid_raw = getattr(account, "workspace_id", None)
        if not wid_raw:
            return True, None, None, None
        try:
            wid = int(str(wid_raw).strip())
        except (TypeError, ValueError):
            wid = None
        workspace = db.session.get(Workspace, wid) if wid is not None else None
        if not workspace:
            return True, None, None, None
        user = db.session.get(User, workspace.user_id)
        if not user:
            return True, None, None, None
    except Exception:
        logger.warning("reactive flow gate resolution failed (fail-open)", exc_info=True)
        _safe_rollback()
        return True, None, None, None

    plan = get_user_plan(user)
    features = get_plan_features(plan)
    is_ai_capped = plan in AI_PLANS and (
        not is_unlimited(features.get("whatsapp_messages_total", UNLIMITED))
        or not is_unlimited(features.get("messages_per_day", UNLIMITED))
    )

    # ── Check phase — fail CLOSED for capped ai_* plans, OPEN otherwise ──
    try:
        if not features.get("whatsapp_automation", True):
            return (
                False,
                "WhatsApp automation isn't included on your current plan. Please upgrade to enable it.",
                user,
                wid,
            )

        ok_total, _cur_t, lim_t = check_whatsapp_total_for_user(user)
        if not ok_total:
            return (
                False,
                f"WhatsApp message limit of {lim_t} reached for this billing period. Please upgrade your plan.",
                user,
                wid,
            )

        return True, None, user, wid
    except Exception:
        logger.warning("reactive flow gate check failed", exc_info=True)
        _safe_rollback()
        if is_ai_capped:
            return (
                False,
                "Could not verify your plan quota right now. Please try again shortly.",
                user,
                wid,
            )
        return True, None, user, wid
