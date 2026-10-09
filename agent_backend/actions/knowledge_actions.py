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
        workspace_id = context.get("workspace_id")

        if not account_id:
            return {"status": "error", "message": "No WhatsApp account connected."}

        faq_count = WhatsAppFAQ.query.filter_by(account_id=account_id, is_active=True).count()

        # Check for AI config / training data
        try:
            from whatsapp.models import WhatsAppAccount
            acct = WhatsAppAccount.query.get(account_id)
            ai_config = (acct.ai_config or {}) if acct else {}
        except Exception:
            ai_config = {}

        website_url = ai_config.get("website_url", "")
        custom_instructions = ai_config.get("custom_instructions", "")

        lines = [f"**AI Training Sources:**"]
        lines.append(f"• FAQs: **{faq_count}** active entries")
        if website_url:
            lines.append(f"• Website: {website_url}")
        if custom_instructions:
            preview = custom_instructions[:80] + ("..." if len(custom_instructions) > 80 else "")
            lines.append(f"• Custom instructions: \"{preview}\"")
        if not website_url and not custom_instructions and faq_count == 0:
            lines.append("\n⚠️ No training data configured. Go to **Settings → AI** to add a website, FAQs, or custom instructions.")

        return {"status": "success", "message": "\n".join(lines)}


@action_registry.register(domain="knowledge", action="get_ai_config")
class GetAIConfigAction(BaseAction):
    name = "get_ai_config"
    description = "Show current AI chatbot configuration"
    required_params: List[str] = []

    def execute(self, params: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
        account_id = context.get("account_id")

        if not account_id:
            return {"status": "error", "message": "No WhatsApp account connected."}

        try:
            from whatsapp.models import WhatsAppAccount
            acct = WhatsAppAccount.query.get(account_id)
            if not acct:
                return {"status": "error", "message": "Account not found."}
            ai_config = acct.ai_config or {}
        except Exception as exc:
            return {"status": "error", "message": f"Could not load AI config: {exc}"}

        enabled = ai_config.get("enabled", False)
        model = ai_config.get("model", "not set")
        tone = ai_config.get("tone", "professional")
        website = ai_config.get("website_url", "none")
        instructions = ai_config.get("custom_instructions", "")

        msg = (
            f"🤖 **AI Chatbot Config**\n"
            f"• Status: {'🟢 Enabled' if enabled else '🔴 Disabled'}\n"
            f"• Model: {model}\n"
            f"• Tone: {tone}\n"
            f"• Website: {website}\n"
            f"• Custom instructions: {'set (' + str(len(instructions)) + ' chars)' if instructions else 'none'}"
        )
        return {"status": "success", "message": msg, "data": ai_config, "ui_action": "show_card"}
