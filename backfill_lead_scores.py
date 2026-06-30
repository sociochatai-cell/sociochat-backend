"""
Backfill: recompute the automatic lead score for every existing CRM Lead so
previously-captured leads pick up the scoring introduced by the scoring module.

Run from backend/sociochat-backend:
    python backfill_lead_scores.py

Safe to run repeatedly: recompute_and_save_lead_score is idempotent (it derives
the score from the lead's current state). Importing `app` also ensures the CRM
tables exist first.
"""

from app import app, db


def main():
    with app.app_context():
        crm = getattr(app, "crm_models", {}) or {}
        Lead = crm.get("Lead")
        if Lead is None:
            print("[error] Lead model not configured; nothing to backfill")
            return

        # Lazy import so a missing/half-built scoring module gives a clear message
        # instead of a confusing import error at module load time.
        try:
            from SocioviaCrm.lead_scoring import recompute_and_save_lead_score
        except Exception as e:
            print(f"[error] lead_scoring module not available: {e}")
            return

        try:
            leads = db.session.query(Lead).all()
        except Exception as e:
            print(f"[error] could not load leads: {e}")
            return

        print(f"Found {len(leads)} lead(s)")
        updated, unchanged, failed = 0, 0, 0

        for lead in leads:
            lead_id = getattr(lead, "id", "?")
            phone = getattr(lead, "phone", None)
            old_score = getattr(lead, "score", None)
            try:
                new_score = recompute_and_save_lead_score(lead)
            except Exception as e:
                # The scoring helper is contracted never to raise, but stay
                # defensive so one bad lead can't abort the whole backfill.
                failed += 1
                print(f"  ERR  lead {lead_id} ({phone}): {e}")
                continue

            if new_score != old_score:
                updated += 1
            else:
                unchanged += 1
            print(f"  OK   lead {lead_id} ({phone}): {old_score} -> {new_score}")

        print(
            f"\nDone. {updated} updated, {unchanged} unchanged, {failed} failed "
            f"(of {len(leads)} total)."
        )


if __name__ == "__main__":
    main()
