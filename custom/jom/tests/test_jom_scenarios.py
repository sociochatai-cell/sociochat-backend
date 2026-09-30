"""
JOM END-TO-END SCENARIO SIMULATOR
=================================
Mimics EVERY customer journey of the JOM travel vertical against the REAL code
(lifecycle / qualification / hooks / memory / window / departments), deterministically,
with NO Meta and NO live AI. Time is simulated (we pass `now=` to fire drips), so a
45-day journey runs in milliseconds.

Each scenario narrates every step; on failure it prints the SCENARIO, the STEP, and
expected-vs-actual so you can see exactly where it breaks. Final line is a machine-
readable verdict for CI / the terminal.

Run inside the container:
    docker exec -w /app <c> python -m custom.jom.tests.test_jom_scenarios

WHAT THIS PROVES (≈99%): all flow LOGIC — qualify, route, round-robin, sticky, memory,
drip auto-enrol, ghosted/dormant full cycles, mid-drip cancel, button outcomes,
re-engagement, destination change, 24h window + Continue, template SELECTION + variables.
WHAT IT CANNOT PROVE (the ~1%): real Meta template DELIVERY, exact live-AI wording,
CAPI receipt at Meta — those need the live/sandbox test.
"""
import os
os.environ.setdefault("JOM_WORKSPACE_IDS", "211")

from datetime import datetime, timezone, timedelta

WS = 211
SENT = []           # mock outbound log: (phone, kind, detail)


def _now():
    return datetime.now(timezone.utc)


class Step:
    """Narrated assertion collector for one scenario."""
    def __init__(self, name):
        self.name = name
        self.log = []
        self.failed = None      # (step, expected, actual)

    def ok(self, step, cond, expected="", actual=""):
        mark = "  ✓" if cond else "  ✗"
        self.log.append(f"{mark} {step}")
        if not cond and self.failed is None:
            self.failed = (step, expected, actual)
        return cond


def _drip_send_fn(ws, phone, status, stage):
    # record what the scheduler WOULD send + which template it selects
    from custom.jom import config as cfg
    tpl, uses_tip = cfg.followup_template(status, stage)
    SENT.append((phone, "drip", {"status": status, "stage": stage, "template": tpl, "tip": uses_tip}))


def _continue_send_fn(ws, phone, template, variables):
    SENT.append((phone, "continue", {"template": template, "vars": variables}))


# --------------------------------------------------------------------------- #
# Scenario definitions — each returns a Step
# --------------------------------------------------------------------------- #
def scenario_qualified_and_routed(env):
    from custom.jom import hooks, memory
    from custom.jom.models import JomLifecycleState
    D, agents, WS = env["dept"], env["agents"], 211
    p = "919900000001"
    s = Step("Happy path — qualify → route → round-robin → assign → memory → inbox")
    out = hooks.on_inbound_message(WS, p, "Hi, I want a simdest honeymoon package for 2")
    row = JomLifecycleState.query.filter_by(workspace_id=WS, customer_phone=p).first()
    s.ok("lead qualifies", out.get("qualified") is True, "qualified=True", out)
    s.ok("routed to TEST dept", out.get("department") == D.name, D.name, out.get("department"))
    s.ok("assigned an agent", out.get("agent_id") in agents, f"in {agents}", out.get("agent_id"))
    s.ok("status = assigned", row and row.status == "assigned", "assigned", getattr(row, "status", None))
    # memory capture (simulate AI learning name/destination)
    memory.update_card(WS, p, customer_name="Asha", destination="Bali")
    card = memory.get_card(WS, p)
    s.ok("memory card stores facts", card.get("customer_name") == "Asha", "Asha", card.get("customer_name"))
    # agent-inbox bridge
    from agent_auth.models import AgentNumberAssignment
    try:
        from whatsapp.utils import normalize_phone
        np = normalize_phone(p) or p
    except Exception:
        np = p
    asg = AgentNumberAssignment.query.filter_by(workspace_id=WS, customer_phone=np).first()
    s.ok("lead bridged to agent inbox", asg is not None and asg.agent_id == row.assigned_agent_id,
         "assignment exists", asg)
    return s


def scenario_round_robin(env):
    from custom.jom import hooks
    from custom.jom.models import JomLifecycleState
    D, agents = env["dept"], env["agents"]
    s = Step("Round-robin — two leads → two different agents")
    p1, p2 = "919900000002", "919900000003"
    hooks.on_inbound_message(WS, p1, "simdest trip please")
    hooks.on_inbound_message(WS, p2, "planning simdest for family")
    a1 = JomLifecycleState.query.filter_by(workspace_id=WS, customer_phone=p1).first().assigned_agent_id
    a2 = JomLifecycleState.query.filter_by(workspace_id=WS, customer_phone=p2).first().assigned_agent_id
    s.ok("both got an agent", a1 in agents and a2 in agents, f"both in {agents}", (a1, a2))
    if len(agents) >= 2:
        s.ok("agents differ (round-robin)", a1 != a2, "different", (a1, a2))
    else:
        s.ok("(only 1 agent attached — rotation not observable)", True)
    return s


def scenario_ghosted_full_cycle(env):
    from custom.jom import lifecycle, config as cfg
    from custom.jom.models import JomLifecycleState
    p = "919900000010"
    s = Step("Ghosted FULL cycle — Day 7 → Day 15 → auto-close")
    _seed(p, status=cfg.STATUS_ASSIGNED)
    lifecycle.mark_ghosted(WS, p)
    row = JomLifecycleState.query.filter_by(workspace_id=WS, customer_phone=p).first()
    s.ok("auto-enrolled (stage 0, scheduled)", row.status == "ghosted" and row.drip_stage == "0" and row.next_followup_at, "scheduled", row.status)
    base = row.status_since
    SENT.clear()
    # fire Day 7
    lifecycle.tick(WS, now=base + timedelta(days=7, hours=1), send_fn=_drip_send_fn)
    d7 = [x for x in SENT if x[0] == p]
    s.ok("Day 7 check-in sent (ghosted_checkin template)",
         any(x[2]["template"] == cfg.TPL_GHOSTED_CHECKIN for x in d7), cfg.TPL_GHOSTED_CHECKIN, d7)
    # fire Day 15 → should send exit AND close
    lifecycle.tick(WS, now=base + timedelta(days=15, hours=1), send_fn=_drip_send_fn)
    row = JomLifecycleState.query.filter_by(workspace_id=WS, customer_phone=p).first()
    exits = [x for x in SENT if x[0] == p and x[2]["template"] == cfg.TPL_GHOSTED_EXIT]
    s.ok("Day 15 exit template sent", len(exits) >= 1, "exit sent", SENT)
    s.ok("auto-closed after last step", row.status == "closed", "closed", row.status)
    s.ok("close reason = drip_exhausted", row.close_reason == cfg.CLOSE_REASON_DRIP_EXHAUSTED, "drip_exhausted", row.close_reason)
    return s


def scenario_ghosted_cancelled_midway(env):
    from custom.jom import lifecycle, hooks, config as cfg
    from custom.jom.models import JomLifecycleState
    p = "919900000011"
    s = Step("Ghosted CANCELLED mid-drip — taps 'not planning' after Day 7 → close, Day 15 never fires")
    _seed(p, status=cfg.STATUS_ASSIGNED)
    lifecycle.mark_ghosted(WS, p)
    base = JomLifecycleState.query.filter_by(workspace_id=WS, customer_phone=p).first().status_since
    lifecycle.tick(WS, now=base + timedelta(days=7, hours=1), send_fn=_drip_send_fn)  # Day 7 fires
    # customer taps "not planning"
    out = hooks.on_inbound_message(WS, p, {"button_id": "jom_not_planning", "button_title": "Not planning now"})
    row = JomLifecycleState.query.filter_by(workspace_id=WS, customer_phone=p).first()
    s.ok("closed on the button tap", row.status == "closed", "closed", row.status)
    SENT.clear()
    lifecycle.tick(WS, now=base + timedelta(days=15, hours=1), send_fn=_drip_send_fn)  # Day 15 must NOT fire
    s.ok("Day 15 did NOT fire (cycle cancelled)", not any(x[0] == p for x in SENT), "no send", SENT)
    return s


def scenario_dormant_full_cycle(env):
    from custom.jom import lifecycle, config as cfg
    from custom.jom.models import JomLifecycleState
    p = "919900000020"
    s = Step("Dormant FULL cycle — Day 7,14,24,31 nudges → Day 45 close")
    _seed(p, status=cfg.STATUS_ASSIGNED)
    lifecycle.mark_dormant(WS, p)
    row = JomLifecycleState.query.filter_by(workspace_id=WS, customer_phone=p).first()
    base = row.status_since
    SENT.clear()
    for d in (7, 14, 24, 31):
        lifecycle.tick(WS, now=base + timedelta(days=d, hours=1), send_fn=_drip_send_fn)
    nudges = [x for x in SENT if x[0] == p and x[2]["template"] == cfg.TPL_DORMANT_CHECKIN]
    s.ok("4 dormant check-in nudges sent (reused template)", len(nudges) == 4, "4", len(nudges))
    s.ok("nudges use the tip variable {{3}}", all(x[2]["tip"] for x in nudges), "tip=True", nudges[:1])
    lifecycle.tick(WS, now=base + timedelta(days=45, hours=1), send_fn=_drip_send_fn)  # Day 45 close
    row = JomLifecycleState.query.filter_by(workspace_id=WS, customer_phone=p).first()
    closes = [x for x in SENT if x[0] == p and x[2]["template"] == cfg.TPL_CLOSE]
    s.ok("Day 45 close template sent", len(closes) >= 1, "close sent", SENT)
    s.ok("auto-closed", row.status == "closed", "closed", row.status)
    return s


def scenario_dormant_rework_to_agent(env):
    from custom.jom import lifecycle, hooks, config as cfg
    from custom.jom.models import JomLifecycleState
    p = "919900000021"
    s = Step("Dormant → taps 'Rework my plan' → back to SAME agent, drip stops")
    _seed(p, status=cfg.STATUS_ASSIGNED, agent_id=env["agents"][0])
    lifecycle.mark_dormant(WS, p)
    out = hooks.on_inbound_message(WS, p, {"button_id": "jom_rework", "button_title": "Rework my plan"})
    row = JomLifecycleState.query.filter_by(workspace_id=WS, customer_phone=p).first()
    s.ok("re-activated for the agent", out.get("action") == "button_agent" and row.status == "active", "active", row.status)
    s.ok("drip stopped", row.next_followup_at is None, "no next", row.next_followup_at)
    s.ok("kept the SAME agent (sticky)", row.assigned_agent_id == env["agents"][0], env["agents"][0], row.assigned_agent_id)
    return s


def scenario_reengage_after_close(env):
    from custom.jom import lifecycle, hooks, config as cfg, memory
    from custom.jom.models import JomLifecycleState
    p = "919900000030"
    s = Step("Re-engage after CLOSED — customer returns → active, SAME agent, memory intact")
    _seed(p, status=cfg.STATUS_CLOSED, agent_id=env["agents"][0])
    memory.update_card(WS, p, customer_name="Vikram", destination="Kashmir",
                       last_summary="Loved the Gulmarg stay idea, worried about March snow.")
    out = hooks.on_inbound_message(WS, p, "Hi, I'm back — still thinking about the trip")
    row = JomLifecycleState.query.filter_by(workspace_id=WS, customer_phone=p).first()
    s.ok("re-activated", out.get("action") == "reengaged" and row.status == "active", "active", row.status)
    s.ok("returned to the SAME agent", row.assigned_agent_id == env["agents"][0], env["agents"][0], row.assigned_agent_id)
    block = memory.prompt_block(WS, p)
    s.ok("memory survived (name + where-left-off)", "Vikram" in block and "Gulmarg" in block, "intact", block[:60])
    return s


def scenario_destination_change(env):
    from custom.jom import memory
    p = "919900000040"
    s = Step("Destination change mid-chat — Kashmir → Bali updates memory + steers AI")
    _seed(p)
    memory.update_card(WS, p, customer_name="Neha", destination="Kashmir")
    memory.set_destination(WS, p, "Bali")
    card = memory.get_card(WS, p)
    s.ok("destination updated to Bali", card.get("destination") == "Bali", "Bali", card.get("destination"))
    s.ok("prompt points AI at the NEW destination", "Bali" in memory.prompt_block(WS, p), "Bali in block", "")
    return s


def scenario_not_interested_immediately(env):
    from custom.jom import hooks, config as cfg
    from custom.jom.models import JomLifecycleState
    p = "919900000050"
    s = Step("Not interested immediately — never enters a drip")
    _seed(p, status=cfg.STATUS_NEW)
    out = hooks.on_inbound_message(WS, p, "please stop, not interested")
    row = JomLifecycleState.query.filter_by(workspace_id=WS, customer_phone=p).first()
    s.ok("closed immediately", row.status == "closed", "closed", row.status)
    s.ok("reason = not_interested", row.close_reason == cfg.CLOSE_REASON_NOT_INTERESTED, "not_interested", row.close_reason)
    s.ok("no follow-up scheduled", row.next_followup_at is None, "none", row.next_followup_at)
    return s


def scenario_qualify_soft_stall(env):
    from custom.jom import qualification, config as cfg
    from custom.jom.models import JomLifecycleState
    p = "919900000060"
    s = Step("Qualify-in-5-6 — still chatting, no buyer signal after 5 turns → AI keeps nurturing (soft stall, NOT force-dormant)")
    _seed(p, status=cfg.STATUS_NEW)
    out = None
    for i in range(5):
        out = qualification.qualify_and_route(WS, p, "hmm ok")   # no keyword, weak intent
    row = JomLifecycleState.query.filter_by(workspace_id=WS, customer_phone=p).first()
    turns = qualification.episode_turns(WS, p)
    s.ok(f"counted {turns} episode turns", turns >= 5, ">=5", turns)
    s.ok("soft 'stalled' flag raised (UI can see it)", bool(out and out.get("stalled")), "stalled=True", out)
    s.ok("NOT force-dormant while actively chatting", row.status != "dormant", "not dormant", row.status)
    s.ok("stays in conversation (contacted)", row.status == cfg.STATUS_CONTACTED, "contacted", row.status)
    return s


def scenario_window_continue(env):
    from custom.jom import window, config as cfg, memory
    from custom.jom.models import JomLifecycleState
    p = "919900000070"
    s = Step("24h window — lapses mid-chat → dynamic Continue re-opener")
    now = _now()
    _seed(p, status=cfg.STATUS_ACTIVE)
    row = JomLifecycleState.query.filter_by(workspace_id=WS, customer_phone=p).first()
    row.last_inbound_at = now - timedelta(hours=30)
    from models import db
    db.session.commit()
    memory.update_card(WS, p, customer_name="Ravi", destination="Bali",
                       last_summary="Was comparing Ubud vs Seminyak villas.")
    s.ok("window is CLOSED (30h since last msg)", window.is_window_open(row, now) is False, "closed", "")
    SENT.clear()
    window.run_continue_nudges(WS, now=now, send_fn=_continue_send_fn)
    c = [x for x in SENT if x[0] == p and x[1] == "continue"]
    s.ok("Continue template sent", c and c[0][2]["template"] == cfg.TPL_CONTINUE, cfg.TPL_CONTINUE, c)
    s.ok("Continue is DYNAMIC (real convo in {{2}})", c and "Ubud" in c[0][2]["vars"].get("2", ""), "Ubud in var2", c)
    return s


def scenario_junk_filter(env):
    from custom.jom import qualification, junk_filter
    p = "919900000080"
    s = Step("Junk filter — 3 outbound, 0 reply → flagged junk")
    _seed(p)
    for _ in range(3):
        qualification.record_outbound_no_reply(WS, p)
    s.ok("flagged as junk", junk_filter.is_junk(WS, p) is True, "junk=True", "")
    return s


def scenario_capi_feedback_loop(env):
    """Item 7 — the Conversion-Leads feedback loop. Proves the status→CAPI wiring:
    a QualifiedLead event fires on qualify (with lead_id + email + phone attached),
    a Converted event fires on won, and a lead that closes as 'not interested' sends
    NO positive event (absence = the low-quality signal Meta learns from).
    Meta itself is mocked (spy on send_capi_event) — we prove OUR side calls it right;
    real receipt at Meta is the sandbox test."""
    from custom.jom import hooks, capi_leads, config as cfg
    import SocioviaCrm.capi_service as capi_service
    from custom.jom.models import JomLifecycleState

    s = Step("CAPI feedback loop — QualifiedLead + Converted fire with identity; junk sends nothing")
    calls = []                       # spy log: (event_name, user_data, custom_data, workspace_id)

    def _spy(event_name, user_data_dict=None, custom_data_dict=None, workspace_id=None, **kw):
        calls.append((event_name, user_data_dict or {}, custom_data_dict or {}, workspace_id))
        return {"success": True, "events_received": 1}

    original = getattr(capi_service, "send_capi_event", None)
    capi_service.send_capi_event = _spy
    try:
        # --- qualified lead with a captured Meta Lead ID + email ------------- #
        pq = "919900000095"
        _seed(pq, status=cfg.STATUS_NEW)
        capi_leads.set_lead_identity(WS, pq, meta_lead_id="l_1234567890", email="asha@example.com")
        row = JomLifecycleState.query.filter_by(workspace_id=WS, customer_phone=pq).first()
        s.ok("lead identity stored (lead_id + email)",
             row.meta_lead_id == "l_1234567890" and row.email == "asha@example.com",
             "stored", (row.meta_lead_id, row.email))

        # Piece A: the inbound hook captures the Click-to-WhatsApp click id onto the lead
        hooks.on_inbound_message(WS, pq, "I want a simdest honeymoon package",
                                 meta={"ctwa_clid": "CTWA_ABC123"})  # keyword → qualifies
        row = JomLifecycleState.query.filter_by(workspace_id=WS, customer_phone=pq).first()
        s.ok("ctwa_clid captured onto lead from inbound", row.ctwa_clid == "CTWA_ABC123", "CTWA_ABC123", row.ctwa_clid)
        qcalls = [c for c in calls if c[0] == "QualifiedLead" and c[1].get("phone") == pq]
        s.ok("QualifiedLead event fired on qualify", len(qcalls) >= 1, ">=1 QualifiedLead", len(qcalls))
        if qcalls:
            ud = qcalls[0][1]
            s.ok("event carries the Meta lead_id (ties to the ad)", ud.get("lead_id") == "l_1234567890", "l_1234567890", ud.get("lead_id"))
            s.ok("event carries ctwa_clid (Click-to-WhatsApp attribution)", ud.get("ctwa_clid") == "CTWA_ABC123", "CTWA_ABC123", ud.get("ctwa_clid"))
            s.ok("event carries email + phone match keys", ud.get("email") == "asha@example.com" and ud.get("phone") == pq, "both present", ud)
            s.ok("event scoped to this workspace", qcalls[0][3] == WS, WS, qcalls[0][3])

        # --- converted / won → Converted event ------------------------------ #
        pc = "919900000096"
        _seed(pc, status=cfg.STATUS_ASSIGNED)
        capi_leads.set_lead_identity(WS, pc, meta_lead_id="l_222", email="v@example.com")
        hooks.on_lead_status_change(WS, pc, cfg.STATUS_ASSIGNED, "won")
        conv = [c for c in calls if c[0] == "Converted" and c[1].get("phone") == pc]
        s.ok("Converted event fired on 'won'", len(conv) >= 1, ">=1 Converted", len(conv))

        # --- junk / not-interested → NO positive event ---------------------- #
        pj = "919900000097"
        _seed(pj, status=cfg.STATUS_NEW)
        before = len(calls)
        hooks.on_inbound_message(WS, pj, "not interested, please stop")
        after = [c for c in calls[before:] if c[1].get("phone") == pj]
        s.ok("closed-disinterest lead sends NO positive CAPI event", len(after) == 0, "0 events", after)
    finally:
        if original is not None:
            capi_service.send_capi_event = original
    return s


def scenario_agent_scheduled_followup(env):
    """NEW feature — an agent schedules a one-off template for a future date/time;
    the tick fires it once; a reply returns the lead to the SAME agent; cancel stops
    a pending one; and if the sticky agent is gone the reply falls back within the
    department."""
    from custom.jom import scheduled, config as cfg, departments as Dsvc
    from custom.jom.models import JomLifecycleState, JomScheduledMessage
    from models import db
    from datetime import timedelta
    D, agents = env["dept"], env["agents"]
    s = Step("Agent-scheduled follow-up — schedule → fire once → reply re-assigns → cancel → fallback")

    # --- schedule + fire ------------------------------------------------- #
    p = "919900000085"
    _seed(p, status=cfg.STATUS_ASSIGNED, agent_id=agents[0])
    row, err = scheduled.create(WS, p, cfg.TPL_OPENING, _now() + timedelta(days=40),
                                variables={"1": "Asha", "2": "November Bali"},
                                created_by_kind="agent", created_by_id=agents[0])
    s.ok("schedule created (pending, agent snapshot)",
         err is None and row and row["status"] == "pending" and row["assigned_agent_id"] == agents[0],
         "pending", (err, row))
    s.ok("past datetime is rejected",
         scheduled.create(WS, p, cfg.TPL_OPENING, _now() - timedelta(days=1))[1] is not None,
         "error", "")
    fired = []
    # not due yet
    r0 = scheduled.run_due(WS, now=_now(), send_fn=lambda ph, tpl, v: fired.append((ph, tpl)))
    s.ok("not fired before its time", r0["scheduled_sent"] == 0, "0", r0)
    # due now
    r1 = scheduled.run_due(WS, now=_now() + timedelta(days=41), send_fn=lambda ph, tpl, v: fired.append((ph, tpl)))
    s.ok("fires exactly once when due", r1["scheduled_sent"] == 1 and len(fired) == 1, "1 send", (r1, fired))
    again = scheduled.run_due(WS, now=_now() + timedelta(days=42), send_fn=lambda ph, tpl, v: fired.append((ph, tpl)))
    s.ok("never double-sends (claim-once)", again["scheduled_sent"] == 0, "0", again)

    # --- cancel a pending one -------------------------------------------- #
    row2, _ = scheduled.create(WS, p, cfg.TPL_OPENING, _now() + timedelta(days=50))
    _, cerr = scheduled.cancel(WS, row2["id"])
    s.ok("pending schedule can be cancelled", cerr is None, "cancelled", cerr)
    r2 = scheduled.run_due(WS, now=_now() + timedelta(days=51), send_fn=lambda ph, tpl, v: fired.append((ph, tpl)))
    s.ok("cancelled schedule never fires", r2["scheduled_sent"] == 0, "0", r2)

    # --- reply after send returns to the SAME agent ---------------------- #
    resolved = Dsvc.resolve_agent_with_fallback(WS, p)
    s.ok("reply returns to the SAME (valid) agent", resolved == agents[0], agents[0], resolved)

    # --- fallback: sticky agent INACTIVE → another active agent in the dept --- #
    from agent_auth.models import WorkspaceAgent
    pf = "919900000086"
    _seed(pf, status=cfg.STATUS_ASSIGNED, agent_id=agents[0])
    row3 = JomLifecycleState.query.filter_by(workspace_id=WS, customer_phone=pf).first()
    row3.department_id = D.id
    db.session.commit()
    gone = db.session.get(WorkspaceAgent, agents[0])
    gone.is_active = False            # simulate the sticky agent no longer available
    db.session.commit()
    resolved2 = Dsvc.resolve_agent_with_fallback(WS, pf)
    gone.is_active = True             # restore for other scenarios
    db.session.commit()
    s.ok("inactive sticky agent → falls back to another active dept agent",
         resolved2 == agents[1], agents[1], resolved2)
    return s


def scenario_ad_lead_seeding(env):
    """Click-to-WhatsApp ad lead has NO sheet columns → seed name (WA profile) +
    destination (from the AD text) even when the first message has no keyword."""
    from custom.jom import hooks, memory
    D = env["dept"]
    p = "919900000088"
    s = Step("CTA ad lead — seed name (WA profile) + destination (from the ad) into memory")
    _seed(p, status="new")
    # message itself has NO destination keyword; the ad text carries it
    hooks.on_inbound_message(WS, p, "hi", meta={
        "ctwa_clid": "CTWA_SEED1", "user_name": "Ravi Kumar",
        "ad_text": "simdest holiday packages — book now",
    })
    card = memory.get_card(WS, p)
    s.ok("name seeded from WhatsApp profile", card.get("customer_name") == "Ravi Kumar", "Ravi Kumar", card.get("customer_name"))
    s.ok("destination seeded from the AD (not the message)", card.get("destination") == D.name, D.name, card.get("destination"))
    # and it does NOT override a name we already know
    hooks.on_inbound_message(WS, p, "actually call me RK", meta={"user_name": "Someone Else"})
    s.ok("existing name is NOT overwritten by later inbound", memory.get_card(WS, p).get("customer_name") == "Ravi Kumar", "Ravi Kumar", memory.get_card(WS, p).get("customer_name"))
    return s


def scenario_reopen_fresh(env):
    from custom.jom import extras, qualification, memory
    from custom.jom.models import JomLifecycleState
    p = "919900000090"
    s = Step("Returning off-season — reopen as fresh, history kept, episode reset")
    _seed(p, status="closed")
    memory.update_card(WS, p, customer_name="Sara", destination="Bali")
    for _ in range(4):
        qualification.record_inbound(WS, p)
    r = extras.reopen_as_fresh(WS, p)
    s.ok("status reset to new", r.status == "new", "new", r.status)
    s.ok("reopen_count incremented", r.reopen_count >= 1, ">=1", r.reopen_count)
    s.ok("episode counter reset", qualification.episode_turns(WS, p) == 0, "0", qualification.episode_turns(WS, p))
    s.ok("memory history KEPT", memory.get_card(WS, p).get("customer_name") == "Sara", "Sara", "")
    return s


# --------------------------------------------------------------------------- #
# Harness
# --------------------------------------------------------------------------- #
ALL_PHONES = [f"9199000000{n:02d}" for n in range(1, 99)]


def _seed(phone, status="new", agent_id=None):
    from models import db
    from custom.jom.models import JomLifecycleState
    JomLifecycleState.query.filter_by(workspace_id=WS, customer_phone=phone).delete()
    row = JomLifecycleState(workspace_id=WS, customer_phone=phone, status=status, assigned_agent_id=agent_id)
    db.session.add(row)
    db.session.commit()
    return row


def run():
    from app import app
    with app.app_context():
        from models import db
        from werkzeug.security import generate_password_hash
        from agent_auth.models import WorkspaceAgent, AgentNumberAssignment
        from custom.jom import departments as Dsvc
        from custom.jom.models import JomLifecycleState, JomBehaviorScore, JomDepartment, JomDepartmentAgent

        from custom.jom.models import JomScheduledMessage

        def cleanup():
            for p in ALL_PHONES:
                JomLifecycleState.query.filter_by(workspace_id=WS, customer_phone=p).delete()
                JomBehaviorScore.query.filter_by(workspace_id=WS, customer_phone=p).delete()
                AgentNumberAssignment.query.filter_by(workspace_id=WS, customer_phone=p).delete()
                JomScheduledMessage.query.filter_by(workspace_id=WS, customer_phone=p).delete()
            for d in JomDepartment.query.filter_by(workspace_id=WS).filter(JomDepartment.name.like("SIM_%")).all():
                JomDepartmentAgent.query.filter_by(department_id=d.id).delete()
                db.session.delete(d)
            WorkspaceAgent.query.filter(WorkspaceAgent.username.like("jomsim_%")).delete()
            db.session.commit()

        cleanup()
        owner_id = db.session.execute(db.text("SELECT user_id FROM workspaces2 WHERE id=:w"), {"w": WS}).scalar()
        a1 = WorkspaceAgent(owner_user_id=owner_id, username="jomsim_a1", password_hash=generate_password_hash("x"), display_name="Sim A1", is_active=True)
        a2 = WorkspaceAgent(owner_user_id=owner_id, username="jomsim_a2", password_hash=generate_password_hash("x"), display_name="Sim A2", is_active=True)
        db.session.add_all([a1, a2]); db.session.commit()
        dep = Dsvc.create_department(WS, "SIM_Dest", keywords=["simdest"])
        Dsvc.add_agent(WS, dep.id, a1.id); Dsvc.add_agent(WS, dep.id, a2.id)
        env = {"dept": dep, "agents": [a1.id, a2.id]}

        scenarios = [
            scenario_qualified_and_routed, scenario_round_robin,
            scenario_ghosted_full_cycle, scenario_ghosted_cancelled_midway,
            scenario_dormant_full_cycle, scenario_dormant_rework_to_agent,
            scenario_reengage_after_close, scenario_destination_change,
            scenario_not_interested_immediately, scenario_qualify_soft_stall,
            scenario_window_continue, scenario_junk_filter, scenario_reopen_fresh,
            scenario_capi_feedback_loop, scenario_agent_scheduled_followup,
            scenario_ad_lead_seeding,
        ]

        print("\n" + "=" * 70)
        print("JOM END-TO-END SCENARIO SIMULATION")
        print("=" * 70)
        passed = 0
        failures = []
        for fn in scenarios:
            try:
                s = fn(env)
            except Exception as exc:
                import traceback
                print(f"\n● {fn.__name__}\n  ✗ CRASHED: {exc}")
                print("   " + traceback.format_exc().replace("\n", "\n   ")[:800])
                failures.append((fn.__name__, "CRASH", str(exc)))
                continue
            ok = s.failed is None
            print(f"\n● {s.name}")
            for line in s.log:
                print(line)
            if ok:
                passed += 1
            else:
                step, exp, act = s.failed
                print(f"  →→ BREAKS AT: {step}\n     expected: {exp}\n     actual:   {act}")
                failures.append((s.name, step, f"exp={exp} act={act}"))

        cleanup()
        print("\n" + "=" * 70)
        total = len(scenarios)
        pct = round(100 * passed / total)
        print(f"RESULT: {passed}/{total} scenarios passed  ({pct}%)")
        if failures:
            print("FAILURES:")
            for name, step, detail in failures:
                print(f"  ✗ [{name}] at '{step}' — {detail}")
        else:
            print("✅ ALL SCENARIOS PASS — flow logic is 100% (real Meta delivery / live AI still need the sandbox test).")
        print("=" * 70)
        return 0 if not failures else 1


if __name__ == "__main__":
    import sys
    sys.exit(run())
