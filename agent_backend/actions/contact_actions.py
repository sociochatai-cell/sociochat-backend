"""
Contact Actions — list and search contacts.
"""

import logging
from typing import Dict, Any, List

from agent_backend.action_registry import BaseAction, action_registry

logger = logging.getLogger(__name__)


@action_registry.register(domain="contact", action="list")
class ListContactsAction(BaseAction):
    name = "list_contacts"
    description = "List contacts"
    required_params: List[str] = []
    optional_params = ["status", "limit"]

    def execute(self, params: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
        from models import db
        from sqlalchemy import text

        workspace_id = context.get("workspace_id")

        limit = min(int(params.get("limit", 20)), 50)
        try:
            sql = ("SELECT id, phone, name, email, company, status, last_contacted "
                   "FROM contacts WHERE workspace_id = :wid "
                   "ORDER BY last_contacted DESC NULLS LAST LIMIT :lim")
            rows = db.session.execute(text(sql), {"wid": int(workspace_id), "lim": limit}).fetchall()
        except Exception as exc:
            logger.warning("Contact list failed: %s", exc)
            return {"status": "error", "message": f"Could not load contacts: {exc}"}

        if not rows:
            return {"status": "success", "message": "No contacts found.", "data": []}

        items = []
        lines = []
        for i, r in enumerate(rows, 1):
            item = {"id": r[0], "phone": r[1], "name": r[2], "email": r[3], "company": r[4], "status": r[5]}
            items.append(item)
            name = r[2] or r[1] or "Unknown"
            extra = f" · {r[4]}" if r[4] else ""
            lines.append(f"{i}. **{name}** — {r[1] or 'no phone'}{extra}")

        msg = f"**Contacts ({len(items)}):**\n" + "\n".join(lines)
        return {"status": "success", "message": msg, "data": items, "ui_action": "show_list"}


@action_registry.register(domain="contact", action="search")
class SearchContactsAction(BaseAction):
    name = "search_contacts"
    description = "Search contacts by name, phone, or email"
    required_params = ["query"]
    optional_params: List[str] = []

    def get_missing_params_prompt(self, missing: List[str]) -> str:
        return "What name or phone number should I search for?"

    def execute(self, params: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
        from models import db
        from sqlalchemy import text

        workspace_id = context.get("workspace_id")

        q = f"%{params['query'].strip()}%"
        try:
            sql = ("SELECT id, phone, name, email, company, status "
                   "FROM contacts WHERE workspace_id = :wid "
                   "AND (name ILIKE :q OR phone ILIKE :q OR email ILIKE :q) "
                   "ORDER BY last_contacted DESC NULLS LAST LIMIT 20")
            rows = db.session.execute(text(sql), {"wid": int(workspace_id), "q": q}).fetchall()
        except Exception as exc:
            logger.warning("Contact search failed: %s", exc)
            return {"status": "error", "message": f"Search failed: {exc}"}

        if not rows:
            return {"status": "success", "message": f"No contacts matching '{params['query']}' found.", "data": []}

        items = []
        lines = []
        for i, r in enumerate(rows, 1):
            item = {"id": r[0], "phone": r[1], "name": r[2], "email": r[3], "company": r[4], "status": r[5]}
            items.append(item)
            name = r[2] or r[1] or "Unknown"
            lines.append(f"{i}. **{name}** — {r[1] or 'no phone'}")

        msg = f"**Search Results ({len(items)}):**\n" + "\n".join(lines)
        return {"status": "success", "message": msg, "data": items, "ui_action": "show_list"}
