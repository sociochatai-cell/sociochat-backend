"""
JOM v3 test — items (1) persona, (5) 24h window / Continue re-opener, (8) agent-inbox
bridge. Run inside the container:
    docker exec -w /app <container> python -m custom.jom.tests.test_jom_v3
Self-cleaning; uses throwaway phones.
"""
import os
os.environ.setdefault("JOM_WORKSPACE_IDS", "211")

from datetime import datetime, timezone, timedelta

WS = 211
OTHER_WS = 999999          # must NOT get the JOM persona
P_WIN = "910000008001"
P_MEM = "910000008002"
P_BRG = "910000008003"

_fails = []
def check(name, cond):
    print(("  PASS " if cond else "  FAIL ") + name)
    if not cond:
        _fails.append(name)


def run():
    from app import app
    with app.app_context():
        from models import db
        from custom.jom import config as cfg, window, persona, memory, departments as D
        from custom.jom.models import JomLifecycleState, JomBehaviorScore
        from agent_auth.models import WorkspaceAgent, AgentNumberAssignment
        from werkzeug.security import generate_password_hash

        phones = (P_WIN, P_MEM, P_BRG)

        def cleanup():
            for p in phones:
                JomLifecycleState.query.filter_by(workspace_id=WS, customer_phone=p).delete()
                JomBehaviorScore.query.filter_by(workspace_id=WS, customer_phone=p).delete()
                AgentNumberAssignment.query.filter_by(workspace_id=WS, customer_phone=p).delete()
            WorkspaceAgent.query.filter(WorkspaceAgent.username.like("jomv3_%")).delete()
            db.session.commit()

        def seed(phone, status=cfg.STATUS_ACTIVE, last_inbound=None):
            row = JomLifecycleState(workspace_id=WS, customer_phone=phone, status=status,
                                    last_inbound_at=last_inbound)
            db.session.add(row); db.session.commit()
            return row

        cleanup()
        now = datetime.now(timezone.utc)

        # ---------- (5) 24h window detection ----------
        r_open = seed(P_WIN, last_inbound=now - timedelta(hours=2))
        check("window OPEN when they messaged 2h ago", window.is_window_open(r_open, now) is True)
        r_open.last_inbound_at = now - timedelta(hours=30); db.session.commit()
        check("window CLOSED when they messaged 30h ago", window.is_window_open(r_open, now) is False)
        check("can_send_free_text agrees", window.can_send_free_text(WS, P_WIN, now) is False)

        # ---------- (5) Continue re-opener is due + dynamic ----------
        memory.update_card(WS, P_WIN, customer_name="Rahul", destination="Bali",
                           last_summary="We were comparing beach villas for your December honeymoon.")
        due = window.due_continue_nudges(WS, now)
        check("lapsed conversation is due a Continue nudge",
              any(d.customer_phone == P_WIN for d in due))
        line = window.context_line(WS, P_WIN)
        check("Continue line is DYNAMIC (uses the real conversation)",
              "December honeymoon" in line)

        captured = {}
        def fake_send(ws, phone, template_name, variables):
            captured["phone"] = phone; captured["tpl"] = template_name; captured["vars"] = variables

        res = window.run_continue_nudges(WS, now=now, send_fn=fake_send)
        check("Continue nudge sent", res.get("continue_sent", 0) >= 1)
        check("uses the Continue template", captured.get("tpl") == cfg.TPL_CONTINUE)
        check("passes name + dynamic context as variables",
              captured.get("vars", {}).get("1") == "Rahul"
              and "December honeymoon" in captured.get("vars", {}).get("2", ""))

        # not nudged twice for the SAME silence period
        res2 = window.run_continue_nudges(WS, now=now, send_fn=fake_send)
        check("does NOT nudge twice for the same silence", res2.get("continue_sent", 0) == 0)

        # a new inbound re-arms it
        row = JomLifecycleState.query.filter_by(workspace_id=WS, customer_phone=P_WIN).first()
        row.last_inbound_at = now; db.session.commit()
        later = now + timedelta(hours=30)
        due_again = window.due_continue_nudges(WS, later)
        check("re-arms after the customer speaks again",
              any(d.customer_phone == P_WIN for d in due_again))

        # ---------- (1) persona is JOM-only and carries the memory ----------
        seed(P_MEM)
        memory.update_card(WS, P_MEM, customer_name="Anita", destination="Kashmir", budget="1L")
        suffix = persona.system_suffix(WS, P_MEM)
        check("JOM persona applied for ws 211", "TRAVEL CONSULTANT FOR JOURNEY OF MILES" in suffix)
        check("persona enforces ONE question at a time", "ONE question per message" in suffix)
        check("persona carries the memory card", "Anita" in suffix and "Kashmir" in suffix)
        check("persona says never re-ask", "never ask these again" in suffix.lower())
        check("NO persona for a non-JOM workspace", persona.system_suffix(OTHER_WS, P_MEM) == "")

        # ---------- (8) agent-inbox bridge ----------
        owner_id = db.session.execute(db.text("SELECT user_id FROM workspaces2 WHERE id=:w"), {"w": WS}).scalar()
        ag = WorkspaceAgent(owner_user_id=owner_id, username="jomv3_a1",
                            password_hash=generate_password_hash("x"),
                            display_name="V3 Agent", is_active=True)
        db.session.add(ag); db.session.commit()
        seed(P_BRG)
        D.bridge_to_agent_inbox(WS, P_BRG, ag.id)
        asg = AgentNumberAssignment.query.filter_by(workspace_id=WS, customer_phone=P_BRG).first()
        check("routed lead now appears in the agent's inbox scope",
              asg is not None and asg.agent_id == ag.id)
        # re-pointing (re-engagement to the same/another agent) must not duplicate
        D.bridge_to_agent_inbox(WS, P_BRG, ag.id)
        n = AgentNumberAssignment.query.filter_by(workspace_id=WS, customer_phone=P_BRG).count()
        check("bridge is idempotent (no duplicate rows)", n == 1)

        cleanup()

    print("\nRESULT:", "ALL PASS" if not _fails else f"{len(_fails)} FAILED -> {_fails}")
    return 0 if not _fails else 1


if __name__ == "__main__":
    import sys
    sys.exit(run())
