"""
Map users -> workspaces -> WhatsApp accounts -> CRM data, to explain why features
appear "scattered" across workspace IDs.

Usage (from backend/sociochat-backend):
    python inspect_user_workspaces.py                       # highlights saurabhbishnoi9@gmail.com
    python inspect_user_workspaces.py someone@gmail.com

Read-only. No DB writes.
"""

import sys
from app import app


def _safe(o, attr, default=None):
    try:
        return getattr(o, attr, default)
    except Exception:
        return default


def main():
    target_email = sys.argv[1] if len(sys.argv) > 1 else "saurabhbishnoi9@gmail.com"

    with app.app_context():
        from models import User, Workspace
        from whatsapp.models import WhatsAppAccount

        users = User.query.all()
        workspaces = Workspace.query.all()
        accounts = WhatsAppAccount.query.all()

        # email lookup by user id
        email_by_uid = {u.id: _safe(u, "email") for u in users}

        print("=" * 74)
        print("USERS")
        print("=" * 74)
        for u in users:
            print(f"  user_id={u.id}  email={_safe(u,'email')!r}  "
                  f"tenant_id={_safe(u,'tenant_id')}  name={_safe(u,'name')!r}  "
                  f"plan={_safe(u,'plan')}")

        print("")
        print("=" * 74)
        print("WORKSPACES (workspaces2)")
        print("=" * 74)
        owners_of = {}  # email -> [workspace ids]
        for w in workspaces:
            owner_email = email_by_uid.get(_safe(w, "user_id"))
            owners_of.setdefault(owner_email, []).append(w.id)
            print(f"  workspace_id={w.id}  owner_user_id={_safe(w,'user_id')}  "
                  f"owner_email={owner_email!r}  business_name={_safe(w,'business_name')!r}")

        print("")
        print("=" * 74)
        print("WHATSAPP ACCOUNTS  (which workspace each number is connected under)")
        print("=" * 74)
        ws_owner = {w.id: email_by_uid.get(_safe(w, "user_id")) for w in workspaces}
        for a in accounts:
            wid = _safe(a, "workspace_id")
            # workspace_id on accounts is a string; normalize for owner lookup
            try:
                wid_int = int(wid)
            except Exception:
                wid_int = wid
            print(f"  account_id={a.id}  workspace_id={wid!r}  "
                  f"owner_email={ws_owner.get(wid_int)!r}  "
                  f"phone={_safe(a,'display_phone_number')}  "
                  f"phone_number_id={_safe(a,'phone_number_id')}  active={_safe(a,'is_active')}")

        # CRM data per workspace
        print("")
        print("=" * 74)
        print("CRM DATA PER WORKSPACE")
        print("=" * 74)
        crm = getattr(app, "crm_models", {}) or {}
        Lead = crm.get("Lead")
        Contact = crm.get("Contact")
        for w in workspaces:
            wid = str(w.id)
            lead_n = contact_n = "?"
            try:
                if Lead is not None:
                    lead_n = Lead.query.filter(Lead.workspace_id == wid).count()
            except Exception as e:
                lead_n = f"err({e})"
            try:
                if Contact is not None:
                    contact_n = Contact.query.filter(Contact.workspace_id == wid).count()
            except Exception as e:
                contact_n = f"err({e})"
            wa_n = sum(1 for a in accounts if str(_safe(a, "workspace_id")) == wid)
            print(f"  workspace {w.id}: leads={lead_n}  contacts={contact_n}  "
                  f"whatsapp_accounts={wa_n}  owner={ws_owner.get(w.id)!r}")

        # Highlight the target user
        print("")
        print("=" * 74)
        print(f"TARGET: {target_email}")
        print("=" * 74)
        tu = User.query.filter(User.email == target_email).first()
        if not tu:
            print(f"  No user found with email {target_email!r}.")
            print(f"  Emails present: {[ _safe(u,'email') for u in users ]}")
            return
        print(f"  user_id={tu.id}  tenant_id={_safe(tu,'tenant_id')}")
        my_ws = [w.id for w in workspaces if _safe(w, "user_id") == tu.id]
        print(f"  workspaces OWNED by this user: {my_ws or 'NONE'}")
        my_wa = [(a.id, _safe(a, 'workspace_id'), _safe(a, 'display_phone_number'))
                 for a in accounts if str(_safe(a, 'workspace_id')) in {str(x) for x in my_ws}]
        print(f"  WhatsApp accounts under this user's workspace(s): {my_wa or 'NONE'}")
        other_wa = [(a.id, _safe(a, 'workspace_id'), _safe(a, 'display_phone_number'))
                    for a in accounts if str(_safe(a, 'workspace_id')) not in {str(x) for x in my_ws}]
        if other_wa:
            print(f"  WhatsApp accounts under OTHER workspaces (not this user): {other_wa}")
        print("")
        print("  => If this user owns 1 workspace but the WhatsApp numbers/automations")
        print("     live under a DIFFERENT workspace, that is exactly why features look")
        print("     'scattered': the login sees its own workspace, the chat data is elsewhere.")


if __name__ == "__main__":
    main()
