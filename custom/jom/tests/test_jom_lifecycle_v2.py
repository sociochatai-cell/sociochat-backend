"""
JOM v2 test — the NEW behaviour added 2026-09-25:
  * client cadence (Ghosted 7/15, Dormant 7/14/24/31/45)
  * auto-enrolment into the right drip the moment a status is set
  * follow-up BUTTON taps -> lifecycle outcome (close / dormant / back-to-agent)
    and specifically that "already booked" CLOSES instead of re-activating
  * clear disinterest in free text -> closed immediately (drip does NOT continue)
  * episode turn counter + reset on re-engagement
  * memory card read/write + the prompt block the AI sees

Run inside the SocioChat container:
    docker exec -w /app <container> python -m custom.jom.tests.test_jom_lifecycle_v2
Self-cleaning: uses throwaway phones and removes its rows at the end.
"""
import os
os.environ.setdefault("JOM_WORKSPACE_IDS", "211")

WS = 211
P_BTN = "910000007001"   # button-tap lead
P_NEG = "910000007002"   # says "not interested"
P_MEM = "910000007003"   # memory card
P_EPI = "910000007004"   # episode counter

_fails = []
def check(name, cond):
    print(("  PASS " if cond else "  FAIL ") + name)
    if not cond:
        _fails.append(name)


def run():
    from app import app
    with app.app_context():
        from models import db
        from custom.jom import config as cfg, lifecycle, qualification, hooks, memory
        from custom.jom.models import JomLifecycleState, JomBehaviorScore

        phones = (P_BTN, P_NEG, P_MEM, P_EPI)

        def cleanup():
            for p in phones:
                JomLifecycleState.query.filter_by(workspace_id=WS, customer_phone=p).delete()
                JomBehaviorScore.query.filter_by(workspace_id=WS, customer_phone=p).delete()
            db.session.commit()

        def seed(phone, status="assigned"):
            row = JomLifecycleState(workspace_id=WS, customer_phone=phone, status=status)
            db.session.add(row)
            db.session.commit()
            return row

        cleanup()

        # ---------- cadence matches the client's ----------
        check("ghosted cadence = [7,15]", cfg.GHOSTED_DAYS == [7, 15])
        check("dormant cadence = [7,14,24,31,45]", cfg.DORMANT_DAYS == [7, 14, 24, 31, 45])
        check("message counts match cadences",
              len(cfg.GHOSTED_MESSAGES) == 2 and len(cfg.DORMANT_MESSAGES) == 5)

        # ---------- auto-enrolment: setting the status schedules the drip ----------
        seed(P_BTN)
        lifecycle.mark_ghosted(WS, P_BTN)
        row = JomLifecycleState.query.filter_by(workspace_id=WS, customer_phone=P_BTN).first()
        check("ghosted auto-enrolled (stage 0 + next follow-up scheduled)",
              row.status == cfg.STATUS_GHOSTED and row.drip_stage == "0" and row.next_followup_at is not None)

        # ---------- BUTTON: 'already booked' must CLOSE, not re-activate ----------
        out = hooks.on_inbound_message(WS, P_BTN, {"button_id": "jom_booked", "button_title": "Already booked"})
        row = JomLifecycleState.query.filter_by(workspace_id=WS, customer_phone=P_BTN).first()
        check("button 'booked' -> closed (NOT re-activated)",
              out.get("action") == "button_close" and row.status == cfg.STATUS_CLOSED)
        check("button 'booked' -> drip stopped", row.next_followup_at is None and row.drip_stage is None)
        check("button 'booked' -> reason recorded", row.close_reason == cfg.CLOSE_REASON_BOOKED_ELSEWHERE)

        # ---------- BUTTON: 'need more time' -> dormant (nurture continues) ----------
        cleanup(); seed(P_BTN)
        out = hooks.on_inbound_message(WS, P_BTN, {"button_id": "jom_more_time", "button_title": "Need more time"})
        row = JomLifecycleState.query.filter_by(workspace_id=WS, customer_phone=P_BTN).first()
        check("button 'need more time' -> dormant + drip scheduled",
              out.get("action") == "button_dormant" and row.status == cfg.STATUS_DORMANT
              and row.next_followup_at is not None)

        # ---------- BUTTON: 'rework' -> back to the (sticky) agent ----------
        cleanup(); r = seed(P_BTN, status=cfg.STATUS_GHOSTED); r.assigned_agent_id = None; db.session.commit()
        out = hooks.on_inbound_message(WS, P_BTN, {"button_id": "jom_rework", "button_title": "Rework my plan"})
        row = JomLifecycleState.query.filter_by(workspace_id=WS, customer_phone=P_BTN).first()
        check("button 'rework' -> re-activated for the agent",
              out.get("action") == "button_agent" and row.status == cfg.STATUS_ACTIVE)

        # ---------- FREE TEXT disinterest -> close immediately ----------
        cleanup(); seed(P_NEG, status=cfg.STATUS_DORMANT)
        lifecycle.mark_dormant(WS, P_NEG)
        out = hooks.on_inbound_message(WS, P_NEG, "sorry, not interested anymore")
        row = JomLifecycleState.query.filter_by(workspace_id=WS, customer_phone=P_NEG).first()
        check("'not interested' -> closed immediately",
              row.status == cfg.STATUS_CLOSED and row.close_reason == cfg.CLOSE_REASON_NOT_INTERESTED)
        check("'not interested' -> drip stopped (no more steps)", row.next_followup_at is None)

        # ---------- episode counter + reset on re-engagement ----------
        cleanup(); seed(P_EPI, status=cfg.STATUS_ACTIVE)
        for _ in range(3):
            qualification.record_inbound(WS, P_EPI)
        check("episode counter counts turns", qualification.episode_turns(WS, P_EPI) == 3)
        lifecycle.mark_dormant(WS, P_EPI)          # goes quiet
        lifecycle.on_reply(WS, P_EPI)              # comes back -> fresh episode
        check("episode counter resets on re-engagement", qualification.episode_turns(WS, P_EPI) == 0)

        # ---------- memory card ----------
        cleanup(); seed(P_MEM, status=cfg.STATUS_ACTIVE)
        memory.update_card(WS, P_MEM, customer_name="Rahul", destination="Kashmir",
                           budget="~1.2L", party_size="2")
        card = memory.get_card(WS, P_MEM)
        check("memory card stores what we know",
              card.get("customer_name") == "Rahul" and card.get("destination") == "Kashmir")
        check("known_fields lists them (AI must not re-ask)",
              set(["customer_name", "destination", "budget", "party_size"]).issubset(set(memory.known_fields(WS, P_MEM))))
        # destination change mid-conversation
        memory.set_destination(WS, P_MEM, "Bali")
        check("destination change is persisted (Kashmir -> Bali)",
              memory.get_card(WS, P_MEM).get("destination") == "Bali")
        memory.update_card(WS, P_MEM, last_summary="Was comparing Bali villas for a December honeymoon.")
        block = memory.prompt_block(WS, P_MEM)
        check("prompt block tells the AI what it knows",
              "never ask these again" in block.lower() and "Rahul" in block)
        check("prompt block carries 'where you left off'",
              "WHERE YOU LEFT OFF" in block and "December honeymoon" in block)
        check("prompt block points the AI at the CURRENT destination", "Bali" in block)

        cleanup()

    print("\nRESULT:", "ALL PASS" if not _fails else f"{len(_fails)} FAILED -> {_fails}")
    return 0 if not _fails else 1


if __name__ == "__main__":
    import sys
    sys.exit(run())
