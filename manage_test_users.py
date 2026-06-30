"""
Test-user management utility
============================

List, delete, or reset user accounts created during testing — so you can re-test
signup/payment with the same emails cleanly.

Usage (run from backend/sociochat-backend with the venv python):

  # See users (newest first). Optionally filter by an email substring.
  python manage_test_users.py list
  python manage_test_users.py list --like priyam

  # DRY RUN first (shows what WOULD be deleted, changes nothing):
  python manage_test_users.py delete --emails priyam11@gmail.com,test@x.com
  python manage_test_users.py delete --ids 12,13,14

  # Actually delete (add --yes):
  python manage_test_users.py delete --emails priyam11@gmail.com --yes

  # Non-destructive alternative: just reset a user back to free beta plan
  # (keeps the account, clears plan/pending so you can re-run the payment flow):
  python manage_test_users.py reset --ids 12 --yes

Notes
-----
* Deletion removes the user, their workspace(s), and all child rows (campaigns,
  contacts, subscription usage, payment transactions, etc.). It walks the real
  foreign-key graph and retries blocked deletes (children first), so it works on
  managed Postgres (Cloud SQL) without superuser privileges. Runs in one
  transaction — rolls back fully on any error.
* The internal tenant T0000 and its accounts are NOT special-cased — only delete
  ids/emails you actually created for testing.
"""

import os
import sys
import argparse

import psycopg2
from dotenv import load_dotenv

load_dotenv()

DB_URI = os.getenv("SQLALCHEMY_DATABASE_URI")
if not DB_URI:
    print("ERROR: SQLALCHEMY_DATABASE_URI not set in .env")
    sys.exit(1)


def _connect():
    return psycopg2.connect(DB_URI)


def _resolve_user_ids(cur, ids, emails):
    """Turn --ids / --emails into a concrete set of user ids that exist."""
    found = []
    if ids:
        cur.execute("SELECT id, email, tenant_id FROM users WHERE id = ANY(%s)", (ids,))
        found += cur.fetchall()
    if emails:
        cur.execute("SELECT id, email, tenant_id FROM users WHERE lower(email) = ANY(%s)",
                    ([e.lower() for e in emails],))
        found += cur.fetchall()
    # de-dup by id
    seen, uniq = set(), []
    for row in found:
        if row[0] not in seen:
            seen.add(row[0])
            uniq.append(row)
    return uniq


def _child_tables(cur, column):
    """All public tables that have the given column (e.g. 'user_id')."""
    cur.execute(
        "SELECT table_name FROM information_schema.columns "
        "WHERE table_schema='public' AND column_name=%s ORDER BY table_name",
        (column,),
    )
    return [r[0] for r in cur.fetchall()]


def _all_fks(cur):
    """Every FK as (child_table, child_col, parent_table, parent_col)."""
    cur.execute("""
        SELECT tc.table_name, kcu.column_name, ccu.table_name, ccu.column_name
        FROM information_schema.table_constraints tc
        JOIN information_schema.key_column_usage kcu
          ON tc.constraint_name = kcu.constraint_name AND tc.table_schema = kcu.table_schema
        JOIN information_schema.constraint_column_usage ccu
          ON ccu.constraint_name = tc.constraint_name AND ccu.table_schema = tc.table_schema
        WHERE tc.constraint_type = 'FOREIGN KEY' AND tc.table_schema = 'public'
    """)
    return cur.fetchall()


def _single_pks(cur):
    """{table: pk_col} for tables with a single-column primary key."""
    cur.execute("""
        SELECT tc.table_name, kcu.column_name
        FROM information_schema.table_constraints tc
        JOIN information_schema.key_column_usage kcu
          ON tc.constraint_name = kcu.constraint_name AND tc.table_schema = kcu.table_schema
        WHERE tc.constraint_type = 'PRIMARY KEY' AND tc.table_schema = 'public'
    """)
    by_table = {}
    for tbl, col in cur.fetchall():
        by_table.setdefault(tbl, []).append(col)
    return {t: cols[0] for t, cols in by_table.items() if len(cols) == 1}


def cmd_list(args):
    conn = _connect()
    cur = conn.cursor()
    if args.like:
        cur.execute(
            "SELECT id, email, tenant_id, plan, status, created_at "
            "FROM users WHERE email ILIKE %s ORDER BY created_at DESC NULLS LAST, id DESC",
            (f"%{args.like}%",),
        )
    else:
        cur.execute(
            "SELECT id, email, tenant_id, plan, status, created_at "
            "FROM users ORDER BY created_at DESC NULLS LAST, id DESC LIMIT %s",
            (args.limit,),
        )
    rows = cur.fetchall()
    print(f"{'ID':>5}  {'EMAIL':<35} {'TENANT':>6}  {'PLAN':<12} {'STATUS':<22} CREATED")
    print("-" * 100)
    for uid, email, tid, plan, status, created in rows:
        print(f"{uid:>5}  {(email or ''):<35} {str(tid or ''):>6}  "
              f"{(plan or ''):<12} {(status or ''):<22} {created}")
    print(f"\n{len(rows)} user(s).")
    cur.close()
    conn.close()


def cmd_delete(args):
    ids = [int(x) for x in args.ids.split(",")] if args.ids else []
    emails = [x.strip() for x in args.emails.split(",")] if args.emails else []
    if not ids and not emails:
        print("Nothing to do: pass --ids and/or --emails.")
        return

    conn = _connect()
    cur = conn.cursor()
    targets = _resolve_user_ids(cur, ids, emails)
    if not targets:
        print("No matching users found.")
        cur.close(); conn.close()
        return

    uids = [t[0] for t in targets]
    print("Users to DELETE:")
    for uid, email, tid in targets:
        print(f"  id={uid}  email={email}  tenant_id={tid}")

    # Their workspaces.
    cur.execute("SELECT id FROM workspaces2 WHERE user_id = ANY(%s)", (uids,))
    ws_ids = [r[0] for r in cur.fetchall()]
    print(f"  -> {len(ws_ids)} workspace(s): {ws_ids}")

    if not args.yes:
        print("\nDRY RUN — nothing deleted. Re-run with --yes to actually delete.")
        cur.close(); conn.close()
        return

    fks = _all_fks(cur)
    pks = _single_pks(cur)

    # ---- 1) Reachability: walk the FK graph from the target users, collecting
    #         the in-scope primary-key id set of every dependent table. ----
    scope = {"users": set(uids)}
    changed = True
    while changed:
        changed = False
        for child, ccol, parent, _pcol in fks:
            pids = scope.get(parent)
            if not pids:
                continue
            pkcol = pks.get(child)
            if not pkcol:
                continue  # composite/no single PK — handled in the delete pass
            cur.execute(f"SELECT {pkcol} FROM {child} WHERE {ccol} = ANY(%s)", (list(pids),))
            newids = {r[0] for r in cur.fetchall()}
            if newids and not newids.issubset(scope.get(child, set())):
                scope.setdefault(child, set()).update(newids)
                changed = True

    # ---- 2) Build the delete statements ----
    # (a) every FK: delete child rows pointing at in-scope parent rows (covers
    #     composite-PK tables too). (b) soft user_id/workspace_id columns.
    stmts = []  # (label, sql, params)
    for child, ccol, parent, _pcol in fks:
        pids = scope.get(parent)
        if pids:
            stmts.append((child, f"DELETE FROM {child} WHERE {ccol} = ANY(%s)", (list(pids),)))
    # Soft columns can be INT or VARCHAR across tables — compare as text to be safe.
    ws_ids_txt = [str(x) for x in ws_ids]
    uids_txt = [str(x) for x in uids]
    for tbl in _child_tables(cur, "workspace_id"):
        if ws_ids:
            stmts.append((tbl, f"DELETE FROM {tbl} WHERE workspace_id::text = ANY(%s)", (ws_ids_txt,)))
    for tbl in _child_tables(cur, "user_id"):
        if tbl != "workspaces2":
            stmts.append((tbl, f"DELETE FROM {tbl} WHERE user_id::text = ANY(%s)", (uids_txt,)))
    stmts.append(("workspaces2", "DELETE FROM workspaces2 WHERE user_id = ANY(%s)", (uids,)))
    stmts.append(("users", "DELETE FROM users WHERE id = ANY(%s)", (uids,)))

    # ---- 3) Run with retry: a statement blocked by a child FK is retried on the
    #         next pass (after its children are gone). Loop until all succeed. ----
    deleted = {}
    pending = stmts
    last_err = None
    try:
        while pending:
            still, progressed = [], False
            for label, sql, params in pending:
                cur.execute("SAVEPOINT sp")
                try:
                    cur.execute(sql, params)
                    if cur.rowcount:
                        deleted[label] = deleted.get(label, 0) + cur.rowcount
                    cur.execute("RELEASE SAVEPOINT sp")
                    progressed = True
                except psycopg2.Error as e:
                    cur.execute("ROLLBACK TO SAVEPOINT sp")
                    last_err = e
                    still.append((label, sql, params))
            pending = still
            if not progressed:
                break

        if pending:
            raise RuntimeError(
                f"{len(pending)} delete(s) still blocked by foreign keys "
                f"(last error: {last_err})"
            )

        conn.commit()
        print("\nDeleted rows:")
        for tbl, n in sorted(deleted.items()):
            print(f"  {tbl:<32} {n}")
        print(f"\nRemoved {deleted.get('users', 0)} user(s). You can re-signup with those emails now.")
    except Exception as e:
        conn.rollback()
        print(f"\nDelete failed (rolled back, nothing changed): {e}")
    finally:
        cur.close()
        conn.close()


def cmd_reset(args):
    ids = [int(x) for x in args.ids.split(",")] if args.ids else []
    emails = [x.strip() for x in args.emails.split(",")] if args.emails else []
    conn = _connect()
    cur = conn.cursor()
    targets = _resolve_user_ids(cur, ids, emails)
    if not targets:
        print("No matching users found.")
        cur.close(); conn.close()
        return
    uids = [t[0] for t in targets]
    print("Users to RESET to free beta plan:")
    for uid, email, tid in targets:
        print(f"  id={uid}  email={email}  tenant_id={tid}")
    if not args.yes:
        print("\nDRY RUN — nothing changed. Re-run with --yes to apply.")
        cur.close(); conn.close()
        return
    cur.execute(
        "UPDATE users SET plan='beta', pending_plan=NULL, pending_billing=NULL, "
        "subscription_expires_at=NULL WHERE id = ANY(%s)",
        (uids,),
    )
    conn.commit()
    print(f"\nReset {cur.rowcount} user(s) to beta. Payment flow can be re-tested.")
    cur.close()
    conn.close()


def main():
    p = argparse.ArgumentParser(description="List / delete / reset test users.")
    sub = p.add_subparsers(dest="cmd", required=True)

    pl = sub.add_parser("list", help="List users")
    pl.add_argument("--like", help="Filter by email substring")
    pl.add_argument("--limit", type=int, default=100)
    pl.set_defaults(func=cmd_list)

    pd = sub.add_parser("delete", help="Delete users + all their data")
    pd.add_argument("--ids", help="Comma-separated user ids")
    pd.add_argument("--emails", help="Comma-separated emails")
    pd.add_argument("--yes", action="store_true", help="Actually delete (else dry run)")
    pd.set_defaults(func=cmd_delete)

    pr = sub.add_parser("reset", help="Reset users to free beta (non-destructive)")
    pr.add_argument("--ids", help="Comma-separated user ids")
    pr.add_argument("--emails", help="Comma-separated emails")
    pr.add_argument("--yes", action="store_true", help="Actually apply (else dry run)")
    pr.set_defaults(func=cmd_reset)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
