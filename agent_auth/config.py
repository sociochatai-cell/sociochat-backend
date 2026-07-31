# agent_auth/config.py
"""
Agent (sub-login) permission configuration — SocioChat.
=======================================================
SINGLE SOURCE OF TRUTH for what an agent can be granted.

An "agent" is a restricted sub-login created by an account owner (a `users` row).
It is a DIFFERENT principal from the AI `agent_backend` (that is an NLU/action
system, unrelated to this auth module).

Two permission representations are kept in sync (mirrors the proven fix_bug design):
- allowed_pages : list of page keys  -> what the admin UI ticks (source of truth in Phase 1)
- allowed_paths : list of URL path patterns (wildcards) -> what the route guard matches

Phase 1 scope: FEATURE (page) permissions + WORKSPACE scoping.
Inbox/number scoping (Level 3) is layered on in later phases.
"""

# ==============================================================================
# Assignable feature pages (what an owner can grant an agent)
# ==============================================================================
# Format: {"key": str, "label": str, "category": str, "adminOnly": bool?}
APP_PAGES = [
    {"key": "dashboard", "label": "Dashboard", "category": "General"},

    # WhatsApp
    {"key": "whatsapp_inbox", "label": "WhatsApp Inbox", "category": "WhatsApp"},
    {"key": "whatsapp_templates", "label": "WhatsApp Templates", "category": "WhatsApp"},
    {"key": "whatsapp_contacts", "label": "WhatsApp Contacts", "category": "WhatsApp"},
    {"key": "whatsapp_datasets", "label": "WhatsApp Datasets", "category": "WhatsApp"},
    {"key": "whatsapp_analytics", "label": "WhatsApp Analytics", "category": "WhatsApp"},
    {"key": "whatsapp_bulk", "label": "Bulk Messaging", "category": "WhatsApp"},
    {"key": "whatsapp_automation", "label": "WhatsApp Automation", "category": "WhatsApp"},
    {"key": "whatsapp_interactive", "label": "Interactive Flows", "category": "WhatsApp"},
    {"key": "whatsapp_flows", "label": "WhatsApp Flows", "category": "WhatsApp"},
    {"key": "drip_campaigns", "label": "Drip Campaigns", "category": "WhatsApp"},

    # CRM
    {"key": "crm_dashboard", "label": "CRM Dashboard", "category": "CRM"},
    {"key": "crm_leads", "label": "CRM Leads", "category": "CRM"},
    {"key": "crm_contacts", "label": "CRM Contacts", "category": "CRM"},
    {"key": "crm_deals", "label": "CRM Deals", "category": "CRM"},

    # Marketing & analytics
    {"key": "analytics", "label": "Analytics", "category": "Marketing"},

    # Admin-only — NEVER assignable to an agent
    {"key": "settings", "label": "Settings", "category": "Admin", "adminOnly": True},
    {"key": "workspace_manage", "label": "Workspace Management", "category": "Admin", "adminOnly": True},
    {"key": "billing", "label": "Billing", "category": "Admin", "adminOnly": True},
    {"key": "subscription", "label": "Subscription", "category": "Admin", "adminOnly": True},
    {"key": "agents", "label": "Agents Management", "category": "Admin", "adminOnly": True},
]

# Map page key -> URL path pattern used by the frontend route guard.
PAGE_KEY_TO_PATH = {
    "dashboard": "/dashboard",
    "whatsapp_inbox": "/whatsapp/inbox/*",
    "whatsapp_templates": "/whatsapp/templates/*",
    "whatsapp_contacts": "/whatsapp/contacts/*",
    "whatsapp_datasets": "/whatsapp/datasets/*",
    "whatsapp_analytics": "/whatsapp/analytics/*",
    "whatsapp_bulk": "/whatsapp/bulk/*",
    "whatsapp_automation": "/whatsapp/automation/*",
    "whatsapp_interactive": "/whatsapp/interactive-automation/*",
    "whatsapp_flows": "/whatsapp/flows/*",
    "drip_campaigns": "/whatsapp/drip/*",
    "crm_dashboard": "/crm/dashboard/*",
    "crm_leads": "/crm/leads/*",
    "crm_contacts": "/crm/contacts/*",
    "crm_deals": "/crm/deals/*",
    "analytics": "/analytics/*",
}

# ==============================================================================
# Owner-only API families — an AGENT principal is NEVER allowed to call these,
# even though its token resolves to the owner user for data-ownership checks.
# This is the backend security boundary for Phase 1 (fail-closed on the crown
# jewels). Finer per-feature API gating is a Phase 1.5 hardening.
# ==============================================================================
AGENT_DENY_API_PREFIXES = (
    "/api/admin",
    "/api/superadmin",
    "/api/tenant",
    "/api/subscription",
    "/api/payments",
    "/api/billing",
    "/api/agent-admin",   # agents cannot manage agents
    "/api/agent/",        # the AI agent_backend surface is not for sub-logins
)

# Owner-only workspace MUTATIONS (agents may read the workspace list, never mutate).
# (method, exact-path-or-prefix) — prefix match on the path.
AGENT_DENY_WORKSPACE_MUTATIONS = (
    ("POST", "/api/workspaces"),
    ("PUT", "/api/workspaces/"),
    ("PATCH", "/api/workspaces/"),
    ("DELETE", "/api/workspaces/"),
)

# ==============================================================================
# Level 1 — coarse per-MODULE feature enforcement (backend).
# An agent may only reach an API module if it has at least one granted page in
# that module. Coarse (module-level, not per-endpoint) so it never breaks the
# many endpoints one feature page legitimately calls, while still stopping an
# agent with only CRM pages from calling the WhatsApp API and vice-versa.
# Longest-prefix wins.
# ==============================================================================
AGENT_API_MODULE_PREFIXES = (
    ("/api/whatsapp", "whatsapp"),
    ("/api/leads", "crm"),
    ("/api/contacts", "crm"),
    ("/api/deals", "crm"),
    ("/api/tasks", "crm"),
    ("/api/campaigns", "crm"),
    ("/api/crm", "crm"),
    ("/api/analytics", "analytics"),
)

# Which granted page keys unlock each module.
AGENT_MODULE_PAGE_KEYS = {
    "whatsapp": {
        "whatsapp_inbox", "whatsapp_templates", "whatsapp_contacts", "whatsapp_datasets",
        "whatsapp_analytics", "whatsapp_bulk", "whatsapp_automation", "whatsapp_interactive",
        "whatsapp_flows", "drip_campaigns",
    },
    "crm": {"crm_dashboard", "crm_leads", "crm_contacts", "crm_deals"},
    "analytics": {"analytics", "whatsapp_analytics"},
}

# Families that are workspace-scoped DATA: an agent request to one of these MUST
# resolve to an assigned workspace, else it is rejected (fail closed). This
# closes the "omit workspace_id -> owner-wide data" and by-id leak paths.
AGENT_WORKSPACE_SCOPED_PREFIXES = (
    "/api/whatsapp",
    "/api/leads",
    "/api/contacts",
    "/api/deals",
    "/api/tasks",
    "/api/campaigns",
    "/api/crm",
    "/api/analytics",
)

# Agent requests that are allowed WITHOUT a workspace_id (not workspace-scoped
# data, or scoped by their own means). Checked as prefixes.
AGENT_NO_WORKSPACE_OK_PREFIXES = (
    "/api/agent-auth",
    "/api/workspaces",   # GET list is filtered to the agent's assigned set
)


def agent_module_for_path(path: str):
    """Return the coarse module ('whatsapp'|'crm'|'analytics') for an API path,
    or None if the path is not a module-gated data family."""
    p = (path or "").lower()
    best = None
    for prefix, module in AGENT_API_MODULE_PREFIXES:
        if p.startswith(prefix) and (best is None or len(prefix) > len(best[0])):
            best = (prefix, module)
    return best[1] if best else None


def agent_has_module(allowed_pages, module: str) -> bool:
    """True if any granted page unlocks the module."""
    keys = AGENT_MODULE_PAGE_KEYS.get(module, set())
    return bool(set(allowed_pages or []) & keys)


def is_agent_workspace_scoped(path: str) -> bool:
    """True if an agent request to this path MUST carry an assigned workspace."""
    p = (path or "").lower()
    if any(p.startswith(pre) for pre in AGENT_NO_WORKSPACE_OK_PREFIXES):
        return False
    return any(p.startswith(pre) for pre in AGENT_WORKSPACE_SCOPED_PREFIXES)


# ==============================================================================
# Helpers
# ==============================================================================
def get_all_page_keys() -> list:
    return [p["key"] for p in APP_PAGES]


def get_assignable_page_keys() -> list:
    """Page keys an owner may grant an agent (excludes adminOnly)."""
    return [p["key"] for p in APP_PAGES if not p.get("adminOnly", False)]


def get_admin_only_page_keys() -> list:
    return [p["key"] for p in APP_PAGES if p.get("adminOnly", False)]


def is_admin_only_page(key: str) -> bool:
    return key in get_admin_only_page_keys()


def convert_pages_to_paths(page_keys) -> list:
    """Convert legacy page keys -> URL path patterns for the route guard."""
    paths = []
    for key in page_keys or []:
        p = PAGE_KEY_TO_PATH.get(key)
        if p and p not in paths:
            paths.append(p)
    return paths


def validate_page_permissions(pages) -> tuple:
    """Validate a list of page keys.

    Returns (is_valid, errors, filtered_pages) — drops unknown/admin-only keys.
    """
    errors = []
    filtered = []
    assignable = set(get_assignable_page_keys())
    all_keys = set(get_all_page_keys())

    if not isinstance(pages, list):
        return (False, ["allowed_pages must be a list"], [])

    for page in pages:
        if not isinstance(page, str):
            errors.append(f"Invalid page key: {page!r}")
            continue
        if page not in all_keys:
            errors.append(f"Unknown page key: {page}")
            continue
        if page not in assignable:
            errors.append(f"Cannot assign admin-only page: {page}")
            continue
        if page not in filtered:
            filtered.append(page)

    return (len(errors) == 0, errors, filtered)
