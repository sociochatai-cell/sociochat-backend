"""
Knowledge / AI Training Actions — manage AI chatbot training sources.
"""

import logging
from typing import Dict, Any, List

from agent_backend.action_registry import BaseAction, action_registry

logger = logging.getLogger(__name__)


@action_registry.register(domain="knowledge", action="list_sources")
class ListKnowledgeSourcesAction(BaseAction):
    name = "list_knowledge_sources"
    description = "List AI training sources (websites, documents, FAQs)"
    required_params: List[str] = []

    def execute(self, params: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
        from whatsapp.faq_models import WhatsAppFAQ
        account_id = context.get("account_id")

        if not account_id:
            return {"status": "error", "message": "No WhatsApp account connected."}

        faq_count = WhatsAppFAQ.query.filter_by(account_id=account_id, is_active=True).count()

        try:
            from whatsapp.models import WhatsAppAccount
            acct = WhatsAppAccount.query.get(account_id)
        except Exception:
            acct = None

        prompt_override = getattr(acct, "prompt_override", None) or ""
        knowledge_base_id = getattr(acct, "knowledge_base_id", None) or ""

        lines = ["**AI Training Sources:**"]
        lines.append(f"• FAQs: **{faq_count}** active entries")
        if knowledge_base_id:
            lines.append(f"• Knowledge base: {knowledge_base_id}")
        if prompt_override:
            preview = prompt_override[:80] + ("..." if len(prompt_override) > 80 else "")
            lines.append(f"• Custom instructions: \"{preview}\"")
        if not knowledge_base_id and not prompt_override and faq_count == 0:
            lines.append("\n⚠️ No training data configured. Go to **Settings → AI** to add FAQs or custom instructions.")

        return {"status": "success", "message": "\n".join(lines)}


