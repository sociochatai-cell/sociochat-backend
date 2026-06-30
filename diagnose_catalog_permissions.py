"""
Diagnose Meta Catalog Permissions
==================================

Standalone, READ-ONLY diagnostic that explains WHY a Meta catalog API call
returns "(#100) Missing Permission".

It resolves the stored WhatsApp account(s) + access token the SAME way
``whatsapp/catalog_routes.py`` does, then asks Meta which permissions the token
actually has and re-runs the catalog calls with full HTTP status + JSON output.
Finally it prints a plain-English DIAGNOSIS that interprets the responses.

Usage
-----
    python diagnose_catalog_permissions.py            # scan ALL active accounts
    python diagnose_catalog_permissions.py 2          # only workspace 2 (falls back to all if none)

Safety
------
- Never prints the full access token (only first 12 + last 6 chars).
- Performs only GET requests against Meta (read-only).
- No database writes. Safe to run repeatedly.
"""

import sys
import json

import requests

# Mirror catalog_routes.py exactly.
GRAPH_API_BASE = "https://graph.facebook.com"

# HTTP timeout for Meta calls (seconds) — matches catalog_routes usage.
REQUEST_TIMEOUT = 15


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_api_version():
    """Mirror catalog_routes._get_api_version()."""
    import os
    return os.getenv("WHATSAPP_API_VERSION", "v22.0")


def _mask_token(token):
    """Return a masked token: first 12 + last 6 chars only. Never the full token."""
    if not token:
        return "(none)"
    if len(token) <= 18:
        return token[:4] + "..." + ("*" * max(0, len(token) - 4))
    return f"{token[:12]}...{token[-6:]} (len={len(token)})"


def _print_header(text):
    print("")
    print("=" * 78)
    print(text)
    print("=" * 78)


def _print_section(label):
    print("")
    print("-" * 78)
    print(label)
    print("-" * 78)


def _call_meta(label, url, token, params=None):
    """GET against Meta, print HTTP status + JSON body. Returns (status, json)."""
    _print_section(label)
    print(f"GET {url}")
    if params:
        print(f"    params: {params}")
    try:
        resp = requests.get(
            url,
            params=params or {},
            headers={"Authorization": f"Bearer {token}"},
            timeout=REQUEST_TIMEOUT,
        )
    except Exception as exc:  # noqa: BLE001 - diagnostic must keep going
        print(f"!! Request failed (could not reach Meta): {exc}")
        return None, None

    print(f"HTTP {resp.status_code}")
    body = None
    try:
        body = resp.json() if resp.content else {}
        print(json.dumps(body, indent=2, ensure_ascii=False))
    except ValueError:
        print("(non-JSON response body)")
        print(resp.text)
    return resp.status_code, body


# ── Diagnosis interpretation ───────────────────────────────────────────────────

def _extract_permissions(perm_body):
    perms = {}
    if not isinstance(perm_body, dict):
        return perms
    for row in (perm_body.get("data") or []):
        if isinstance(row, dict) and row.get("permission"):
            perms[row["permission"]] = row.get("status", "unknown")
    return perms


def _business_ids_from_list(businesses_body):
    ids = set()
    if not isinstance(businesses_body, dict):
        return ids
    for row in (businesses_body.get("data") or []):
        if isinstance(row, dict) and row.get("id"):
            ids.add(str(row["id"]))
    return ids


def _meta_error_code(body):
    if isinstance(body, dict):
        err = body.get("error") or {}
        if isinstance(err, dict):
            return err.get("code")
    return None


def _print_diagnosis(perms, businesses_body, meta_business_id, owned_status, owned_body):
    """Plain-English interpretation based on the ACTUAL JSON received."""
    _print_header("DIAGNOSIS (plain English)")
    findings = []

    # 1. Permission scopes
    if perms:
        for needed in ("business_management", "catalog_management"):
            status = perms.get(needed)
            if status is None:
                findings.append(
                    f"[X] '{needed}' is NOT present on this token -> the app was never "
                    f"granted it. Add '{needed}' to the login/embedded-signup scopes and "
                    "have the user reconnect WhatsApp."
                )
            elif status != "granted":
                findings.append(
                    f"[X] '{needed}' is present but status='{status}' (not granted) -> "
                    f"the user declined it. Reconnect and accept '{needed}'."
                )
            else:
                findings.append(f"[OK] '{needed}' is granted.")
    else:
        findings.append(
            "[?] Could not read /me/permissions. The token may be invalid/expired, or a "
            "System User token where /me/permissions is not meaningful."
        )

    # 2. Business Manager visibility
    if not meta_business_id:
        findings.append(
            "[!] account.meta_business_id is NOT set -> the failing "
            "owned_product_catalogs call cannot run. Set the Meta Business Manager ID "
            "(POST /api/whatsapp/catalogs/business-id)."
        )
    else:
        biz_ids = _business_ids_from_list(businesses_body)
        if businesses_body is None:
            findings.append(
                f"[?] Could not read /me/businesses; cannot confirm the token can see "
                f"business id={meta_business_id}."
            )
        elif str(meta_business_id) in biz_ids:
            findings.append(
                f"[OK] Business id={meta_business_id} IS in /me/businesses -> the user "
                "can see this Business Manager."
            )
        else:
            findings.append(
                f"[X] Business id={meta_business_id} is NOT in /me/businesses "
                f"(visible: {sorted(biz_ids) or 'none'}) -> the connected user is NOT an "
                "admin/member of that Business Manager, OR the token lacks "
                "business_management. Catalog calls on that business fail with (#100)."
            )

    # 3. The actual failing call
    if meta_business_id:
        if owned_status is None:
            findings.append("[?] owned_product_catalogs did not complete (network). Re-run.")
        elif owned_status == 200:
            count = len(owned_body.get("data") or []) if isinstance(owned_body, dict) else 0
            findings.append(
                f"[OK] owned_product_catalogs returned HTTP 200 with {count} catalog(s) -> "
                "permissions are sufficient for THIS call."
            )
        else:
            code = _meta_error_code(owned_body)
            base = f"[X] owned_product_catalogs returned HTTP {owned_status} (Meta code={code})."
            if code == 100:
                base += (" Code 100 here almost always means EITHER catalog_management is "
                         "not granted, OR the user is not an admin of business id="
                         f"{meta_business_id} (see visibility above).")
            elif code == 200:
                base += (" Code 200 -> token lacks the required catalog/business permission. "
                         "Reconnect with catalog_management + business_management.")
            elif code == 190:
                base += " Code 190 -> token invalid/expired. Reconnect the WhatsApp account."
            else:
                base += " See the JSON body above for Meta's exact message."
            findings.append(base)

    for line in findings:
        print(f"  {line}")


# ── Per-account diagnosis ─────────────────────────────────────────────────────

def diagnose_account(account, api_version):
    """Run the full read-only Meta permission check for ONE account."""
    token = account.get_access_token()

    _print_header(f"Account id={account.id}  (workspace_id={account.workspace_id!r})")
    print(f"waba_id              : {account.waba_id}")
    print(f"meta_business_id     : {account.meta_business_id}")
    print(f"display_phone_number : {account.display_phone_number}")
    print(f"phone_number_id      : {account.phone_number_id}")
    print(f"access_token (masked): {_mask_token(token)}")

    if not token:
        print("")
        print(">> No access token on this account (DB + env fallback empty). "
              "Skipping Meta checks for it.")
        return {"account_id": account.id, "workspace_id": account.workspace_id,
                "has_token": False, "meta_business_id": account.meta_business_id}

    meta_business_id = account.meta_business_id
    waba_id = account.waba_id

    # (a) permissions
    _ps, perm_body = _call_meta(
        "(a) GET /me/permissions  -> granted vs declined permissions",
        f"{GRAPH_API_BASE}/{api_version}/me/permissions", token,
    )
    perms = _extract_permissions(perm_body)
    if perms:
        print("")
        print("Permission summary (focus on catalog access):")
        for needed in ("business_management", "catalog_management"):
            print(f"    {needed:>22}: {perms.get(needed, 'NOT PRESENT')}")

    # (b) identity
    _call_meta(
        "(b) GET /me?fields=id,name  -> identity behind the token",
        f"{GRAPH_API_BASE}/{api_version}/me", token, params={"fields": "id,name"},
    )

    # (c) businesses
    _bs, businesses_body = _call_meta(
        "(c) GET /me/businesses?fields=id,name  -> accessible Business Managers",
        f"{GRAPH_API_BASE}/{api_version}/me/businesses", token, params={"fields": "id,name"},
    )

    owned_status, owned_body = None, None
    if meta_business_id:
        owned_status, owned_body = _call_meta(
            "(d) GET /{business}/owned_product_catalogs  -> THE FAILING CALL",
            f"{GRAPH_API_BASE}/{api_version}/{meta_business_id}/owned_product_catalogs",
            token, params={"fields": "id,name,product_count"},
        )
        _call_meta(
            "(e) GET /{business}?fields=id,name  -> can the token see this business?",
            f"{GRAPH_API_BASE}/{api_version}/{meta_business_id}", token,
            params={"fields": "id,name"},
        )
    else:
        _print_section("(d)/(e) owned_product_catalogs + business lookup -> SKIPPED")
        print("account.meta_business_id is not set; cannot run business-scoped calls.")

    if waba_id:
        _call_meta(
            "(f) GET /{waba_id}/product_catalogs  -> catalogs linked to the WABA",
            f"{GRAPH_API_BASE}/{api_version}/{waba_id}/product_catalogs", token,
            params={"fields": "id,name"},
        )

    _print_diagnosis(perms, businesses_body, meta_business_id, owned_status, owned_body)

    return {"account_id": account.id, "workspace_id": account.workspace_id,
            "has_token": True, "meta_business_id": meta_business_id,
            "perms": perms, "owned_status": owned_status}


# ── Main ────────────────────────────────────────────────────────────────────

def main():
    workspace_id = sys.argv[1] if len(sys.argv) > 1 else None

    from app import app, db  # noqa: F401 - imported to mirror app wiring

    api_version = _get_api_version()

    _print_header("Meta Catalog Permission Diagnostic")
    filter_desc = repr(workspace_id) if workspace_id else "ALL active accounts"
    print(f"workspace filter : {filter_desc}")
    print(f"api_version      : {api_version}")
    print(f"graph base       : {GRAPH_API_BASE}")

    with app.app_context():
        from whatsapp.models import WhatsAppAccount

        if workspace_id:
            accounts = (WhatsAppAccount.query
                        .filter_by(workspace_id=str(workspace_id), is_active=True).all())
            if not accounts:
                print("")
                print(f"No active WhatsApp account for workspace_id={workspace_id!r}. "
                      "Scanning ALL active accounts instead.")
                accounts = WhatsAppAccount.query.filter_by(is_active=True).all()
        else:
            accounts = WhatsAppAccount.query.filter_by(is_active=True).all()

        if not accounts:
            print("")
            print("No active WhatsApp accounts exist at all. Nothing to diagnose.")
            return

        print("")
        print(f"Will diagnose {len(accounts)} account(s):")
        for acc in accounts:
            print(f"    - id={acc.id} workspace_id={acc.workspace_id!r} "
                  f"waba_id={acc.waba_id} business_id={acc.meta_business_id}")

        results = [diagnose_account(acc, api_version) for acc in accounts]

        # Cross-account summary
        _print_header("OVERALL SUMMARY")
        with_token = [r for r in results if r.get("has_token")]
        with_biz = [r for r in results if r.get("meta_business_id")]
        print(f"  Accounts scanned          : {len(results)}")
        print(f"  Accounts WITH a token     : {len(with_token)} "
              f"-> {[r['account_id'] for r in with_token] or 'none'}")
        print(f"  Accounts WITH business_id : {len(with_biz)} "
              f"-> {[r['account_id'] for r in with_biz] or 'none'}")
        if not with_token:
            print("  -> NONE of the accounts have a usable access token. Stored tokens are "
                  "missing/undecryptable and no WHATSAPP_ACCESS_TOKEN env is set. Reconnect "
                  "a WhatsApp account so a token is saved, THEN re-run. Catalog calls cannot "
                  "work without a token.")
        elif not with_biz:
            print("  -> Account(s) have a token but NO meta_business_id. Set the Business "
                  "Manager ID for the account used for catalogs, then re-run.")
        else:
            print("  -> Read each account's DIAGNOSIS above; the one whose permissions/"
                  "business visibility fail is the cause of your catalog (#100).")

    print("")
    print("Done. (Read-only: no changes were made to the database or to Meta.)")


if __name__ == "__main__":
    main()
