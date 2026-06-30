"""
One-off: create the CRM tables (leads/contacts/deals/tasks/activities/settings/...)
in the configured Postgres database, with clear per-table output.

Run from backend/sociochat-backend:
    python create_crm_tables.py

Safe to run repeatedly (checkfirst=True -> only creates what's missing).
"""

from app import app, db  # importing app builds the Flask app + registers CRM models


def main():
    with app.app_context():
        # Ensure CRM models are registered (no-op if already initialized on import).
        try:
            from SocioviaCrm.models import init_models
            init_models()
        except Exception as e:
            print(f"[warn] init_models: {e}")

        crm = getattr(app, "crm_models", {}) or {}
        if not crm:
            print("[error] no CRM models registered (app.crm_models is empty)")
            return

        print("CRM models:", ", ".join(crm.keys()))
        ok, err = 0, 0
        for name, model in crm.items():
            tbl = getattr(model, "__table__", None)
            if tbl is None:
                continue
            try:
                tbl.create(bind=db.engine, checkfirst=True)
                print(f"  OK   {name:12s} -> {tbl.name}")
                ok += 1
            except Exception as e:
                print(f"  ERR  {name:12s} -> {tbl.name}: {e}")
                err += 1

        print(f"\nDone. {ok} ensured, {err} failed.")
        # Quick verification that 'leads' is now queryable.
        try:
            n = db.session.execute(db.text("SELECT COUNT(*) FROM leads")).scalar()
            print(f"leads table is queryable. Current row count: {n}")
        except Exception as e:
            print(f"[error] leads still not queryable: {e}")


if __name__ == "__main__":
    main()
