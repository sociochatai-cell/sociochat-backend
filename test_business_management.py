"""
Quick test: does the stored WhatsApp token actually have a WORKING
`business_management` permission?

It loads the token for a workspace's active WhatsApp account and makes a few
`business_management`-gated Meta Graph API calls (read-only), printing the exact
HTTP status + body, then a clear PASS / FAIL verdict.

Usage (from backend/sociochat-backend):
    python test_business_management.py            # defaults to workspace 2 (the one with a token)
    python test_business_management.py 2

Safety: read-only (GET only), never prints the full token, no DB writes.
"""

import sys
import json

import requests

GRAPH = "https://graph.facebook.com"


def _ver():
    import os
    return os.getenv("WHATSAPP_API_VERSION", "v22.0")


def _mask(t):
    if not t:
        return "(none)"
    return f"{t[:12]}...{t[-6:]} (len={len(t)})" if len(t) > 18 else "(short token)"


def _call(label, url, token, params=None):
    print("")
    print("-" * 76)
    print(label)
    print(f"GET {url}")
    if params:
        print(f"    params: {params}")
    try:
        r = requests.get(
            url,
            params=params or {},
            headers={"Authorization": f"Bearer {token}"},
            timeout=15,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"!! request error: {exc}")
        return None, None
    print(f"HTTP {r.status_code}")
    body = {}
    try:
        body = r.json() if r.content else {}
        print(json.dumps(body, indent=2, ensure_ascii=False)[:1800])
    except ValueError:
        print(r.text[:1800])
    return r.status_code, body


def _err_code(body):
    if isinstance(body, dict):
        return (body.get("error") or {}).get("code")
    return None


def main():
    workspace_id = sys.argv[1] if len(sys.argv) > 1 else "2"

    from app import app  # noqa: F401 - importing builds the Flask app

    v = _ver()
    print("=" * 76)
    print("business_management permission TEST")
    print("=" * 76)
    print(f"workspace_id : {workspace_id}")
    print(f"api_version  : {v}")

    with app.app_context():
        from whatsapp.models import WhatsAppAccount

        account = (WhatsAppAccount.query
                   .filter_by(workspace_id=str(workspace_id), is_active=True).first())
        if not account:
            print(f"\nNo active WhatsApp account for workspace_id={workspace_id!r}. "
                  "Try another workspace, e.g. `python test_business_management.py 2`.")
            return

        token = account.get_access_token()
        print(f"\naccount id        : {account.id}")
        print(f"waba_id           : {account.waba_id}")
        print(f"meta_business_id  : {account.meta_business_id}")
        print(f"token (masked)    : {_mask(token)}")
        if not token:
            print("\nThis account has NO token stored. Cannot test. Reconnect it first.")
            return

        biz = account.meta_business_id

        # TEST 1 — /me/businesses: the cleanest business_management probe.
        s1, b1 = _call(
            "TEST 1: GET /me/businesses   (requires business_management)",
            f"{GRAPH}/{v}/me/businesses", token, {"fields": "id,name"},
        )

        # TEST 2 — read an edge on the business that needs business_management.
        s2, b2 = None, None
        if biz:
            s2, b2 = _call(
                "TEST 2: GET /{business}/owned_product_catalogs   (requires business_management)",
                f"{GRAPH}/{v}/{biz}/owned_product_catalogs", token, {"fields": "id,name"},
            )
        else:
            print("\n(Skipping TEST 2 — no meta_business_id set on this account.)")

        # VERDICT
        print("")
        print("=" * 76)
        print("VERDICT")
        print("=" * 76)
        passed = (s1 == 200) and (s2 in (None, 200))
        if passed:
            print("  [PASS] business_management is WORKING — the token can access "
                  "business-management edges (HTTP 200).")
            if s2 == 200 and isinstance(b2, dict):
                cats = b2.get("data") or []
                print(f"         owned_product_catalogs returned {len(cats)} catalog(s): "
                      f"{[c.get('id') for c in cats] or 'none'}")
                print("         -> Use one of these IDs to connect a catalog.")
        else:
            print("  [FAIL] business_management is NOT working.")
            for tag, sc, bd in (("/me/businesses", s1, b1),
                                ("owned_product_catalogs", s2, b2)):
                if sc is not None and sc != 200:
                    code = _err_code(bd)
                    print(f"         - {tag}: HTTP {sc} (Meta code={code})")
            print("")
            print("  Meaning: the token is missing the business_management permission "
                  "(or the user is not an admin of that business).")
            print("  Fix: regenerate the System User token with business_management + "
                  "catalog_management, assign the system user to the business/catalog, "
                  "then update the stored token (reconnect).")

    print("")
    print("Done. (Read-only: no changes were made.)")


if __name__ == "__main__":
    main()
