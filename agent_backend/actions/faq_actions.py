"""
FAQ Actions — list, create, update, delete FAQ entries for the AI chatbot.
"""

import logging
from typing import Dict, Any, List

from agent_backend.action_registry import BaseAction, action_registry

logger = logging.getLogger(__name__)


@action_registry.register(domain="faq", action="list")
class ListFAQsAction(BaseAction):
    name = "list_faqs"
    description = "List FAQ entries for the AI chatbot"
    required_params: List[str] = []
    optional_params = ["category"]

    def execute(self, params: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
        from whatsapp.faq_models import WhatsAppFAQ
        workspace_id = context.get("workspace_id")
        account_id = context.get("account_id")

        if not account_id:
            return {"status": "error", "message": "No WhatsApp account connected."}

        query = WhatsAppFAQ.query.filter_by(account_id=account_id)
        category = params.get("category")
        if category:
            query = query.filter(WhatsAppFAQ.category == category)

        faqs = query.order_by(WhatsAppFAQ.priority.asc()).limit(20).all()

        if not faqs:
            return {"status": "success", "message": "No FAQs found. Would you like to create one?", "data": []}

        items = []
        lines = []
        for i, f in enumerate(faqs, 1):
            item = {"id": f.id, "question": f.question, "category": f.category, "is_active": f.is_active, "match_count": f.match_count}
            items.append(item)
            status = "🟢" if f.is_active else "🔴"
            cat = f" [{f.category}]" if f.category else ""
            lines.append(f"{i}. {status} **{f.question[:60]}**{cat} ({f.match_count} matches)")

        msg = f"**FAQs ({len(items)}):**\n" + "\n".join(lines)
        return {"status": "success", "message": msg, "data": items, "ui_action": "show_list"}


@action_registry.register(domain="faq", action="create")
class CreateFAQAction(BaseAction):
    name = "create_faq"
    description = "Create a new FAQ entry for the AI chatbot"
    required_params = ["question", "answer"]
    optional_params = ["category", "keywords"]

    def get_missing_params_prompt(self, missing: List[str]) -> str:
        prompts = {
            "question": "What's the frequently asked question?",
            "answer": "What should the bot answer?",
        }
        first = missing[0]
        return prompts.get(first, f"Please provide **{first}**.")

    def execute(self, params: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
        from whatsapp.faq_models import WhatsAppFAQ
        from models import db

        workspace_id = context.get("workspace_id")
        account_id = context.get("account_id")

        if not account_id:
            return {"status": "error", "message": "No WhatsApp account connected."}

        keywords = params.get("keywords", [])
        if isinstance(keywords, str):
            keywords = [k.strip() for k in keywords.split(",")]

        faq = WhatsAppFAQ(
            workspace_id=str(workspace_id),
            account_id=account_id,
            question=params["question"],
            answer=params["answer"],
            category=params.get("category"),
            keywords=keywords,
            is_active=True,
        )
        db.session.add(faq)
        db.session.commit()

        return {
            "status": "success",
            "message": f"✅ FAQ created!\n• **Q:** {params['question']}\n• **A:** {params['answer'][:100]}{'...' if len(params['answer']) > 100 else ''}",
            "data": {"faq_id": faq.id},
        }


@action_registry.register(domain="faq", action="delete")
class DeleteFAQAction(BaseAction):
    name = "delete_faq"
    description = "Delete a FAQ entry"
    required_params = ["faq_id"]

    def get_missing_params_prompt(self, missing: List[str]) -> str:
        return "Which FAQ would you like to delete? Provide the FAQ ID."

    def execute(self, params: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
        from whatsapp.faq_models import WhatsAppFAQ
        from models import db

        account_id = context.get("account_id")
        faq = WhatsAppFAQ.query.filter_by(id=params["faq_id"], account_id=account_id).first()
        if not faq:
            return {"status": "error", "message": "FAQ not found."}

        session = context.get("session")
        if not (session and getattr(session, "_confirmed", False)):
            return {"status": "confirmation_required", "message": f"Delete FAQ: **{faq.question[:60]}**?"}

        db.session.delete(faq)
        db.session.commit()
        return {"status": "success", "message": "🗑️ FAQ deleted."}


@action_registry.register(domain="faq", action="toggle")
class ToggleFAQAction(BaseAction):
    name = "toggle_faq"
    description = "Enable or disable a FAQ entry"
    required_params = ["faq_id"]
    optional_params = ["is_active"]

    def get_missing_params_prompt(self, missing: List[str]) -> str:
        return "Which FAQ? Provide the FAQ ID."

    def execute(self, params: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
        from whatsapp.faq_models import WhatsAppFAQ
        from models import db

        account_id = context.get("account_id")
        faq = WhatsAppFAQ.query.filter_by(id=params["faq_id"], account_id=account_id).first()
        if not faq:
            return {"status": "error", "message": "FAQ not found."}

        if "is_active" in params:
            faq.is_active = bool(params["is_active"])
        else:
            faq.is_active = not faq.is_active
        db.session.commit()

        status_text = "enabled ✅" if faq.is_active else "disabled 🔴"
        return {"status": "success", "message": f"FAQ **{faq.question[:40]}** is now {status_text}."}
