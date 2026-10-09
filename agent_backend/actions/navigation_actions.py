"""
Navigation Actions — map natural language to frontend routes.
"""

import logging
from typing import Dict, Any, List

from agent_backend.action_registry import BaseAction, action_registry

logger = logging.getLogger(__name__)

ROUTE_MAP = {
    "inbox": "/dashboard/inbox",
    "messages": "/dashboard/inbox",
    "chats": "/dashboard/inbox",
    "templates": "/dashboard/templates",
    "drip": "/dashboard/drip",
    "drip campaigns": "/dashboard/drip",
    "contacts": "/dashboard/contacts",
    "analytics": "/dashboard/tracking",
    "tracking": "/dashboard/tracking",
    "settings": "/dashboard/settings",
    "automation": "/dashboard/automation",
    "flows": "/dashboard/interactive-automation",
    "interactive flows": "/dashboard/interactive-automation",
    "whatsapp flows": "/dashboard/flows",
    "whatsapp forms": "/dashboard/flows",
    "forms": "/dashboard/flows",
    "bulk": "/dashboard/datasets",
    "datasets": "/dashboard/datasets",
    "dashboard": "/dashboard",
    "home": "/dashboard",
    "crm": "/dashboard/crm",
    "leads": "/dashboard/crm/leads",
    "deals": "/dashboard/crm/deals",
    "crm contacts": "/dashboard/crm/contacts",
    "catalog": "/dashboard/catalog",
    "orders": "/dashboard/orders",
    "status ads": "/ctwa/status/create",
    "ctwa": "/dashboard/campaign/create",
    "campaign": "/dashboard/campaign/create",
    "connect": "/dashboard/connect",
    "setup": "/dashboard/whatsapp/setup",
    "hub": "/dashboard/hub",
}


@action_registry.register(domain="navigation", action="go_to")
class NavigateAction(BaseAction):
    name = "navigate_to_page"
    description = "Navigate to a page in the dashboard"
    required_params = ["target"]
    optional_params: List[str] = []

    def get_missing_params_prompt(self, missing: List[str]) -> str:
        pages = ", ".join(f"**{k}**" for k in sorted(set(ROUTE_MAP.keys())))
        return f"Where would you like to go? Available pages: {pages}"

    def execute(self, params: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
        target = params["target"].strip().lower()

        route = ROUTE_MAP.get(target)
        if not route:
            # Fuzzy match
            for key, val in ROUTE_MAP.items():
                if target in key or key in target:
                    route = val
                    break

        if not route:
            pages = ", ".join(sorted(set(ROUTE_MAP.keys())))
            return {
                "status": "error",
                "message": f"I don't know where '{target}' is. Try: {pages}",
            }

        label = target.title()
        return {
            "status": "success",
            "message": f"🚀 Taking you to **{label}**...",
            "navigate_to": route,
            "ui_action": "navigate",
        }
