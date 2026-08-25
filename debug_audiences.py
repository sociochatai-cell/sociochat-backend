"""
Run on the AWS server:  python debug_audiences.py
Prints exactly what Meta returns for your custom audiences.
"""
import requests

BASE = "https://graph.facebook.com/v21.0"

def main():
    import sys, os
    sys.path.insert(0, os.path.dirname(__file__))
    os.environ.setdefault("FLASK_ENV", "production")

    from dotenv import load_dotenv
    load_dotenv()
    from app import app

    with app.app_context():
        from models import SocialAccount
        from ctwa.models import CTWAWorkspaceSettings

        # Find the workspace settings
        ws_settings = CTWAWorkspaceSettings.query.all()
        print("=== Saved workspace ad settings ===")
        for s in ws_settings:
            print(f"  ws={s.workspace_id}  ad_account={s.ad_account_id}  page={s.page_id}")

        # Find all social accounts with Facebook tokens
        socials = SocialAccount.query.filter(
            SocialAccount.provider == 'facebook'
        ).all()

        print(f"\n=== Facebook SocialAccounts ({len(socials)}) ===")
        for sa in socials:
            token = sa.access_token
            print(f"\n--- SocialAccount id={sa.id}, ws={sa.workspace_id}, name={sa.account_name} ---")
            print(f"  token: ...{token[-20:]}")

            # 1. What accounts does this token see?
            r1 = requests.get(f"{BASE}/me/adaccounts",
                params={"access_token": token, "fields": "id,name", "limit": 50},
                timeout=15).json()
            accounts = r1.get("data") or []
            print(f"  /me/adaccounts: {len(accounts)} accounts")
            for a in accounts:
                print(f"    {a['id']} = {a.get('name')}")

            if r1.get("error"):
                print(f"  ERROR: {r1['error']}")
                continue

            # 2. For each workspace setting, check audiences
            for s in ws_settings:
                aid = s.ad_account_id
                if not aid:
                    continue
                if not aid.startswith("act_"):
                    aid = f"act_{aid}"

                visible = any(a["id"] == aid for a in accounts)
                print(f"\n  Checking audiences for {aid} (visible to token: {visible})")

                if not visible:
                    print("  SKIPPED - token cannot see this account")
                    continue

                # TOS
                r_tos = requests.get(f"{BASE}/{aid}",
                    params={"access_token": token, "fields": "tos_accepted,name"},
                    timeout=15).json()
                print(f"  Account name: {r_tos.get('name')}")
                print(f"  TOS: {r_tos.get('tos_accepted')}")

                # Audiences - no filtering
                r2 = requests.get(f"{BASE}/{aid}/customaudiences",
                    params={
                        "access_token": token,
                        "fields": "id,name,approximate_count,subtype,delivery_status,operation_status",
                        "limit": 100,
                    },
                    timeout=15)
                j2 = r2.json()
                print(f"  /customaudiences status={r2.status_code}")
                print(f"  data count: {len(j2.get('data') or [])}")
                if j2.get("error"):
                    print(f"  ERROR: {j2['error']}")
                for a in (j2.get("data") or []):
                    print(f"    - {a.get('name')} (id={a['id']}, subtype={a.get('subtype')}, "
                          f"approx={a.get('approximate_count')}, "
                          f"delivery={a.get('delivery_status')}, "
                          f"status={a.get('operation_status')})")

                # Also try /saved_audiences
                r3 = requests.get(f"{BASE}/{aid}/saved_audiences",
                    params={
                        "access_token": token,
                        "fields": "id,name,approximate_count",
                        "limit": 50,
                    },
                    timeout=15).json()
                saved = r3.get("data") or []
                print(f"  /saved_audiences: {len(saved)}")
                for a in saved:
                    print(f"    - {a.get('name')} (id={a['id']})")

    print("\nDone.")

if __name__ == "__main__":
    main()
