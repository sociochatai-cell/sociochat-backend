"""
Inspect a visual automation's trigger config to debug why a keyword isn't matching.

Usage (from backend/sociochat-backend):
    python inspect_automation.py 5        # inspect automation id 5
    python inspect_automation.py          # defaults to id 5

Read-only. Prints trigger_type, trigger_config, and the trigger node from `nodes`.
"""

import sys
import json

from app import app


def _j(x):
    try:
        return json.dumps(x, indent=2, default=str, ensure_ascii=False)
    except Exception:
        return repr(x)


def main():
    aid = int(sys.argv[1]) if len(sys.argv) > 1 else 5

    with app.app_context():
        from whatsapp.visual_automation_models import WhatsAppVisualAutomation

        a = WhatsAppVisualAutomation.query.get(aid)
        if not a:
            print(f"No automation with id={aid}")
            # show what exists
            for x in WhatsAppVisualAutomation.query.all():
                print(f"  exists: id={x.id} name={x.name!r} account={x.account_id} ws={x.workspace_id!r}")
            return

        print("=" * 70)
        print(f"Automation id={a.id}  name={a.name!r}")
        print("=" * 70)
        print(f"account_id   : {a.account_id}")
        print(f"workspace_id : {a.workspace_id!r}")
        print(f"is_active    : {a.is_active}")
        print(f"status       : {a.status}")
        print(f"trigger_type : {a.trigger_type!r}   (matcher expects one of: any_reply, keyword, exact_match)")
        print("")
        print("trigger_config:")
        print(_j(a.trigger_config))
        print("")

        # What the matcher actually reads:
        cfg = a.trigger_config if isinstance(a.trigger_config, dict) else {}
        kw = cfg.get("keywords", "<<< KEY 'keywords' MISSING >>>")
        print(f"matcher reads trigger_config['keywords'] = {kw!r}")
        print("")

        # The trigger node (where the UI may actually store the keyword)
        nodes = a.nodes or []
        print(f"nodes: {len(nodes)} total")
        for n in nodes:
            if n.get("type") == "trigger":
                print("TRIGGER NODE:")
                print(_j(n))
        print("")
        print("DIAGNOSIS HINTS:")
        if a.trigger_type != "keyword":
            print(f"  - trigger_type is {a.trigger_type!r}, NOT 'keyword' -> the keyword branch never runs.")
        if not cfg.get("keywords"):
            print("  - trigger_config has no non-empty 'keywords' list -> matcher finds nothing to match.")
            print("    Check the TRIGGER NODE above: the keyword may be stored there (e.g. node['data']['keywords'])")
            print("    instead of in trigger_config, which is the save/match mismatch.")


if __name__ == "__main__":
    main()
