"""
Automation Actions — list, create welcome/away/keyword, toggle rules.
"""

import logging
from typing import Dict, Any, List

from agent_backend.action_registry import BaseAction, action_registry

logger = logging.getLogger(__name__)


@action_registry.register(domain="automation", action="list")
class ListAutomationRulesAction(BaseAction):
    name = "list_automation_rules"
    description = "List automation rules (welcome, away, keyword triggers)"
    required_params: List[str] = []
    optional_params = ["rule_type"]

    def execute(self, params: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
        from whatsapp.automation_models import WhatsAppAutomationRule
        from whatsapp.models import WhatsAppAccount

        workspace_id = context.get("workspace_id")
        account_id = context.get("account_id")

        if not account_id:
            return {"status": "error", "message": "No WhatsApp account connected."}

        query = WhatsAppAutomationRule.query.filter_by(account_id=account_id)
        rule_type = params.get("rule_type")
        if rule_type:
            query = query.filter_by(rule_type=rule_type)

        rules = query.order_by(WhatsAppAutomationRule.created_at.desc()).limit(20).all()

        if not rules:
            return {
                "status": "success",
                "message": "No automation rules found. Would you like to create one?",
                "data": [],
            }

        items = []
        lines = []
        for i, r in enumerate(rules, 1):
            item = {
                "id": r.id,
                "name": r.name,
                "rule_type": r.rule_type.value if hasattr(r.rule_type, "value") else str(r.rule_type),
                "is_active": r.is_active,
            }
            items.append(item)
            status = "🟢" if r.is_active else "🔴"
            lines.append(f"{i}. {status} **{r.name}** — {item['rule_type']}")

        msg = f"**Automation Rules ({len(items)}):**\n" + "\n".join(lines)
        return {"status": "success", "message": msg, "data": items, "ui_action": "show_list"}


@action_registry.register(domain="automation", action="create_welcome")
class CreateWelcomeRuleAction(BaseAction):
    name = "create_welcome_rule"
    description = "Create a welcome message automation"
    required_params = ["message"]
    optional_params: List[str] = []

    def get_missing_params_prompt(self, missing: List[str]) -> str:
        return "What should the welcome message say? (This is sent to new contacts automatically)"

    def execute(self, params: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
        from whatsapp.automation_models import WhatsAppAutomationRule
        from models import db

        account_id = context.get("account_id")
        if not account_id:
            return {"status": "error", "message": "No WhatsApp account connected."}

        workspace_id = context.get("workspace_id")

        rule = WhatsAppAutomationRule(
            account_id=account_id,
            workspace_id=workspace_id,
            name="Welcome Message",
            rule_type="welcome",
            response_type="text",
            response_text=params["message"],
            is_active=True,
        )
        db.session.add(rule)
        db.session.commit()

        return {
            "status": "success",
            "message": f"✅ Welcome message automation created!\nMessage: \"{params['message']}\"",
            "data": {"rule_id": rule.id},
        }


@action_registry.register(domain="automation", action="create_away")
class CreateAwayRuleAction(BaseAction):
    name = "create_away_rule"
    description = "Create an away message automation"
    required_params = ["message"]
    optional_params = ["schedule"]

    def get_missing_params_prompt(self, missing: List[str]) -> str:
        prompts = {
            "message": "What should the away message say?",
            "schedule": "What are your business hours? (e.g. 'Mon-Fri 9am-6pm')",
        }
        first = missing[0]
        return prompts.get(first, f"Please provide **{first}**.")

    def execute(self, params: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
        from whatsapp.automation_models import WhatsAppAutomationRule
        from models import db

        account_id = context.get("account_id")
        if not account_id:
            return {"status": "error", "message": "No WhatsApp account connected."}

        workspace_id = context.get("workspace_id")

        rule = WhatsAppAutomationRule(
            account_id=account_id,
            workspace_id=workspace_id,
            name="Away Message",
            rule_type="away",
            response_type="text",
            response_text=params["message"],
            is_active=True,
        )
        db.session.add(rule)
        db.session.commit()

        return {
            "status": "success",
            "message": f"✅ Away message automation created!\nMessage: \"{params['message']}\"",
            "data": {"rule_id": rule.id},
        }


@action_registry.register(domain="automation", action="create_keyword")
class CreateKeywordRuleAction(BaseAction):
    name = "create_keyword_rule"
    description = "Create a keyword trigger automation"
    required_params = ["keywords", "message"]
    optional_params: List[str] = []

    def get_missing_params_prompt(self, missing: List[str]) -> str:
        prompts = {
            "keywords": "What keywords should trigger this? (comma-separated, e.g. 'pricing, price, cost')",
            "message": "What should the auto-reply say when triggered?",
        }
        first = missing[0]
        return prompts.get(first, f"Please provide **{first}**.")

    def execute(self, params: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
        from whatsapp.automation_models import WhatsAppAutomationRule
        from models import db

        account_id = context.get("account_id")
        if not account_id:
            return {"status": "error", "message": "No WhatsApp account connected."}

        workspace_id = context.get("workspace_id")
        keywords = params["keywords"]
        if isinstance(keywords, str):
            keywords = [k.strip() for k in keywords.split(",")]

        rule = WhatsAppAutomationRule(
            account_id=account_id,
            workspace_id=workspace_id,
            name=f"Keyword: {', '.join(keywords[:3])}",
            rule_type="keyword",
            trigger_keywords=keywords,
            response_type="text",
            response_text=params["message"],
            is_active=True,
        )
        db.session.add(rule)
        db.session.commit()

        return {
            "status": "success",
            "message": (
                f"✅ Keyword trigger created!\n"
                f"• Keywords: {', '.join(keywords)}\n"
                f"• Reply: \"{params['message']}\""
            ),
            "data": {"rule_id": rule.id},
        }


@action_registry.register(domain="automation", action="toggle")
class ToggleAutomationAction(BaseAction):
    name = "toggle_automation"
    description = "Enable or disable an automation rule"
    required_params = ["rule_id"]
    optional_params = ["is_active"]

    def get_missing_params_prompt(self, missing: List[str]) -> str:
        return "Which automation rule? Provide the rule ID."

    def execute(self, params: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
        from whatsapp.automation_models import WhatsAppAutomationRule
        from models import db

        workspace_id = context.get("workspace_id")
        rule = WhatsAppAutomationRule.query.filter_by(
            id=params["rule_id"], workspace_id=workspace_id
        ).first()
        if not rule:
            return {"status": "error", "message": "Automation rule not found."}

        if "is_active" in params:
            rule.is_active = bool(params["is_active"])
        else:
            rule.is_active = not rule.is_active

        db.session.commit()
        status_text = "enabled ✅" if rule.is_active else "disabled 🔴"
        return {
            "status": "success",
            "message": f"Automation rule **{rule.name}** is now {status_text}.",
        }
