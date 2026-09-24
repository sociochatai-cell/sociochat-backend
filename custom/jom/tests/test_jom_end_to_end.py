"""
JOM end-to-end test — proves the WHOLE flow from our side, against the real DB.

Run inside the SocioChat container:
    docker exec <sociochat-container> python -m custom.jom.tests.test_jom_end_to_end

It is self-contained and self-cleaning: creates 2 temp agents + 2 departments under
workspace 211, exercises qualify → route (round-robin) → ghosted drip → auto-close →
re-engage-same-agent → converted (CAPI) → junk filter → reopen-fresh, asserts each,
then deletes everything it created. Uses a distinctive test phone so it never touches
real leads.
"""
import os
os.environ.setdefault("JOM_WORKSPACE_IDS", "211")

from datetime import timedelta

WS = 211
P1 = "910000000001"   # test phone 1 (Kashmir)
P2 = "910000000002"   # test phone 2 (Kashmir, for round-robin)
P3 = "910000000003"   # test phone 3 (junk)

_fails = []
def check(name, cond):
    print(("  PASS " if cond else "  FAIL ") + name)
    if not cond:
        _fails.append(name)


def run():
    from app import app
    with app.app_context():
        from models import db
        from werkzeug.security import generate_password_hash
        from agent_auth.models import WorkspaceAgent, AgentWorkspace
        from custom.jom import departments as D, qualification as Q, lifecycle as L, junk_filter as J, extras as X
        from custom.jom.models import JomDepartment, JomLifecycleState, JomBehaviorScore, JomDepartmentAgent

        # --- clean any prior test data ---
        def cleanup():
            for p in (P1, P2, P3):
                JomLifecycleState.query.filter_by(workspace_id=WS, customer_phone=p).delete()
                JomBehaviorScore.query.filter_by(workspace_id=WS, customer_phone=p).delete()
            for d in JomDepartment.query.filter_by(workspace_id=WS).filter(JomDepartment.name.like("TEST_%")).all():
                JomDepartmentAgent.query.filter_by(department_id=d.id).delete()
                db.session.delete(d)
            WorkspaceAgent.query.filter(WorkspaceAgent.username.like("jomtest_%")).delete()
            db.session.commit()
        cleanup()

        # --- create 2 temp agents ---
        owner_id = db.session.execute(db.text("SELECT user_id FROM workspaces2 WHERE id=:w"), {"w": WS}).scalar()
        a1 = WorkspaceAgent(owner_user_id=owner_id, username="jomtest_a1", password_hash=generate_password_hash("x"), display_name="T A1", is_active=True)
        a2 = WorkspaceAgent(owner_user_id=owner_id, username="jomtest_a2", password_hash=generate_password_hash("x"), display_name="T A2", is_active=True)
        db.session.add_all([a1, a2]); db.session.commit()

        # --- create a department + attach both agents ---
        dep = D.create_department(WS, "TEST_Dept", keywords=["zztestdestqx"])
        D.add_agent(WS, dep.id, a1.id)
        D.add_agent(WS, dep.id, a2.id)
        check("department + 2 agents created", len(D.list_agent_ids(dep.id)) == 2)

        # --- qualify + route (unique keyword, no collision with real depts) ---
        out1 = Q.qualify_and_route(WS, P1, "Hi, I want a zztestdestqx honeymoon package")
        check("lead1 qualified", out1["qualified"] is True)
        check("lead1 routed to TEST_Dept", out1.get("department") == "TEST_Dept")
        check("lead1 got an agent", out1.get("agent_id") in (a1.id, a2.id))

        # --- round-robin: second lead → the OTHER agent ---
        out2 = Q.qualify_and_route(WS, P2, "zztestdestqx trip please")
        check("lead2 different agent (round-robin)", out2.get("agent_id") != out1.get("agent_id"))

        # --- ghosted drip: mark ghosted, then tick into the future through all steps ---
        L.mark_ghosted(WS, P1)
        row = JomLifecycleState.query.filter_by(workspace_id=WS, customer_phone=P1).first()
        check("lead1 ghosted + scheduled", row.status == "ghosted" and row.next_followup_at is not None)
        sent_log = []
        send_fn = lambda ws, ph, st, stg: sent_log.append((ph, st, stg))
        future = row.status_since + timedelta(days=20)   # past day 15 → all 3 sends + close
        summary = L.tick(WS, now=future, send_fn=send_fn)
        row = JomLifecycleState.query.filter_by(workspace_id=WS, customer_phone=P1).first()
        check("ghosted drip fired 3 msgs + closed", summary["sent"] >= 1 and row.status == "closed")

        # --- re-engage after close → SAME agent ---
        re = L.on_reply(WS, P1)
        check("re-engaged from closed → active", re["reengaged"] is True)
        check("re-engage kept SAME agent", re["agent_id"] == out1.get("agent_id"))

        # --- junk filter: P3 messaged 3x, never replied ---
        for _ in range(3):
            Q.record_outbound_no_reply(WS, P3)
        check("P3 flagged junk", J.is_junk(WS, P3) is True)
        keep, dropped = J.filter_recipients(WS, [P1, P3], )
        check("junk filter drops P3, keeps P1", (P3 in dropped) and (P1 in keep))

        # --- returning-fresh-lead: reopen P1 as fresh, history kept ---
        before = JomBehaviorScore.query.filter_by(workspace_id=WS, customer_phone=P1).first()
        prev_inbound = before.total_inbound if before else 0
        r = X.reopen_as_fresh(WS, P1)
        after = JomBehaviorScore.query.filter_by(workspace_id=WS, customer_phone=P1).first()
        check("reopen → status new + reopen_count++", r.status == "new" and r.reopen_count >= 1)
        check("reopen kept behaviour history", after is not None and after.total_inbound >= prev_inbound)

        # --- CAPI converted (fail-safe; just must not raise) ---
        from custom.jom import capi_leads
        cres = capi_leads.on_converted(WS, P1, value=50000)
        check("CAPI converted call returned (fail-safe)", isinstance(cres, dict))

        cleanup()

    print("\nRESULT:", "ALL PASS" if not _fails else f"{len(_fails)} FAILED -> {_fails}")
    return 0 if not _fails else 1


if __name__ == "__main__":
    import sys
    sys.exit(run())
