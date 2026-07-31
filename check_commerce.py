"""
Diagnostic: show stored PayU configs + the notification emails per workspace.

Run it with the SAME database your backend uses:

    # PowerShell — set the same DB URL your backend runs with, then:
    $env:SQLALCHEMY_DATABASE_URI = "<your postgres url>"
    myenv\\Scripts\\python.exe check_commerce.py

If you start the backend with run-local-api.ps1, that URL is
    postgresql://sociochat:sociochat@localhost:5432/sociochat
(and the Docker Postgres must be running: docker compose -f docker-compose.dev.yml up -d)
"""
import os
import sys

url = os.getenv("SQLALCHEMY_DATABASE_URI")
if not url:
    print("ERROR: set SQLALCHEMY_DATABASE_URI to your backend's Postgres URL first.")
    sys.exit(1)

import psycopg2

try:
    conn = psycopg2.connect(url)
except Exception as e:
    print(f"DB CONNECT FAILED: {e}")
    print("-> Your backend is NOT using this DB, or the DB isn't running.")
    sys.exit(1)

cur = conn.cursor()


def cols(table):
    cur.execute(
        "select column_name from information_schema.columns where table_name=%s", (table,)
    )
    return {r[0] for r in cur.fetchall()}


pc = cols("workspace_payment_configs")
if not pc:
    print("workspace_payment_configs table does NOT exist -> restart backend to create it.")
    sys.exit(0)

has_auto = "auto_request_payment" in pc
has_notify = "notify_emails" in pc
print(f"Schema: auto_request_payment col={has_auto}  notify_emails col={has_notify}")
if not (has_auto and has_notify):
    print("!! Missing new columns -> RESTART the backend so the schema self-heal adds them.\n")

sel = ["workspace_id", "mode", "is_active",
       "coalesce(merchant_key,'') as mkey",
       "(merchant_salt_encrypted is not null) as has_salt"]
if has_auto:
    sel.append("auto_request_payment")
if has_notify:
    sel.append("notify_emails")
cur.execute(f"select {', '.join(sel)} from workspace_payment_configs order by workspace_id")
rows = cur.fetchall()

print(f"\n=== PayU configs: {len(rows)} row(s) ===")
if not rows:
    print("(none stored — nothing saved, or saved against a different DB/workspace)")

for r in rows:
    d = dict(zip([s.split(" as ")[-1] if " as " in s else s for s in sel], r))
    ws = d["workspace_id"]
    mkey = d.get("mkey") or ""
    masked = ("•" * max(0, len(mkey) - 4) + mkey[-4:]) if mkey else "(none)"
    connected = bool(mkey and d.get("has_salt") and d.get("is_active"))
    print(f"\n-- workspace {ws} --")
    print(f"   connected : {connected}   mode={d.get('mode')}   key={masked}   salt_stored={d.get('has_salt')}")
    if has_auto:
        print(f"   auto_send : {d.get('auto_request_payment')}")
    if has_notify:
        print(f"   commerce notify_emails : {d.get('notify_emails') or '(none)'}")

    # Resolve the notification recipients (same order the backend uses).
    owner_email = acct_notif = None
    try:
        cur.execute("select user_id from workspaces2 where id=%s", (int(ws),))
        row = cur.fetchone()
        if row:
            cur.execute("select email from users where id=%s", (row[0],))
            u = cur.fetchone()
            owner_email = u[0] if u else None
    except Exception as e:
        conn.rollback(); owner_email = f"(lookup err: {e})"
    try:
        cur.execute(
            "select notification_email from whatsapp_accounts where workspace_id=%s and notification_email is not null",
            (str(ws),),
        )
        accs = [a[0] for a in cur.fetchall() if a[0]]
        acct_notif = ", ".join(accs) if accs else None
    except Exception as e:
        conn.rollback(); acct_notif = f"(lookup err: {e})"

    print("   -> notification emails go to:")
    print(f"        account notification_email : {acct_notif or '(not set)'}")
    print(f"        workspace owner email       : {owner_email or '(not found)'}")
    if has_notify:
        print(f"        + commerce extra recipients : {d.get('notify_emails') or '(none)'}")
    print("        (customers receive WhatsApp only — never email)")

# ---------------------------------------------------------------------------
# Recent orders — the evidence you need for a PayU refund/support ticket.
# ---------------------------------------------------------------------------
oc = cols("commerce_orders")
if oc:
    osel = ["txnid", "workspace_id", "customer_phone", "amount", "status", "created_at"]
    for opt in ("mode", "payu_mihpayid", "origin", "productinfo"):
        if opt in oc:
            osel.append(opt)
    cur.execute(
        f"select {', '.join(osel)} from commerce_orders order by created_at desc limit 25"
    )
    orows = cur.fetchall()
    print(f"\n=== Recent orders: {len(orows)} ===")
    print("   (mode='test' -> ran on test.payu.in sandbox; mode='live' -> REAL money)")
    for r in orows:
        d = dict(zip(osel, r))
        print(f"\n   txnid={d.get('txnid')}")
        print(f"     amount=INR {d.get('amount')}  status={d.get('status')}  MODE={d.get('mode','?')}")
        print(f"     payu_id={d.get('payu_mihpayid') or '(none)'}  when={d.get('created_at')}")
        print(f"     phone={d.get('customer_phone')}  item={d.get('productinfo','')}")
else:
    print("\n(no commerce_orders table yet)")

conn.close()
print("\nDone.")
