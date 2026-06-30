"""
Backfill: create a CRM Contact for every existing CRM Lead that doesn't already
have one, so previously-captured WhatsApp leads also appear in the Contacts section.

Run from backend/sociochat-backend:
    python backfill_contacts_from_leads.py

Safe to run repeatedly (dedupes by external_id, then phone, per workspace).
Importing `app` also ensures the CRM tables exist first.
"""

from datetime import datetime
from app import app, db


def main():
    with app.app_context():
        crm = getattr(app, "crm_models", {}) or {}
        Lead = crm.get("Lead")
        Contact = crm.get("Contact")
        if Lead is None or Contact is None:
            print("[error] Lead/Contact model not configured")
            return

        ccols = {c.key for c in Contact.__table__.columns}
        leads = db.session.query(Lead).all()
        print(f"Found {len(leads)} lead(s)")
        created, skipped, failed = 0, 0, 0

        for lead in leads:
            ws = getattr(lead, "workspace_id", None)
            if ws is None:
                skipped += 1
                continue
            ws = str(ws)
            phone = getattr(lead, "phone", None)
            ext_id = getattr(lead, "external_id", None)

            existing = None
            try:
                if ext_id:
                    existing = db.session.query(Contact).filter(
                        Contact.workspace_id == ws,
                        Contact.external_source == "whatsapp",
                        Contact.external_id == ext_id,
                    ).first()
                if existing is None and phone:
                    existing = db.session.query(Contact).filter(
                        Contact.workspace_id == ws, Contact.phone == phone
                    ).first()
            except Exception as e:
                print("  dedupe error:", e)
                existing = None

            if existing is not None:
                skipped += 1
                continue

            values = {
                "workspace_id": ws,
                "name": getattr(lead, "name", None) or phone,
                "phone": phone,
                "email": getattr(lead, "email", None),
                "company": getattr(lead, "company", None),
                "status": "active",
                "external_source": getattr(lead, "external_source", None) or "whatsapp",
                "external_id": ext_id,
                "sync_status": "in_sync",
                "last_contacted": getattr(lead, "last_interaction_at", None),
                "created_at": getattr(lead, "created_at", None) or datetime.utcnow(),
                "updated_at": datetime.utcnow(),
            }
            kwargs = {k: v for k, v in values.items() if k in ccols}
            try:
                db.session.add(Contact(**kwargs))
                db.session.commit()
                created += 1
                print(f"  OK   contact for lead {getattr(lead, 'id', '?')} ({phone})")
            except Exception as e:
                db.session.rollback()
                failed += 1
                print(f"  ERR  lead {getattr(lead, 'id', '?')}: {e}")

        print(f"\nDone. {created} created, {skipped} skipped (already exist), {failed} failed.")
        try:
            n = db.session.execute(db.text("SELECT COUNT(*) FROM contacts")).scalar()
            print(f"contacts table total rows: {n}")
        except Exception as e:
            print(f"[error] contacts not queryable: {e}")


if __name__ == "__main__":
    main()
