"""
Template Actions — list, create, send, sync, delete templates.
"""

import logging
from typing import Dict, Any, List

from agent_backend.action_registry import BaseAction, action_registry

logger = logging.getLogger(__name__)


@action_registry.register(domain="template", action="list")
class ListTemplatesAction(BaseAction):
    name = "list_templates"
    description = "List WhatsApp message templates (optionally filter by status/category)"
    required_params: List[str] = []
    optional_params = ["status", "category"]

    def execute(self, params: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
        from whatsapp.models import WhatsAppTemplate, WhatsAppAccount
        workspace_id = context.get("workspace_id")
        account_id = context.get("account_id")

        if not account_id:
            acct = WhatsAppAccount.query.filter_by(workspace_id=workspace_id, is_active=True).first()
            account_id = acct.id if acct else None

        if not account_id:
            return {"status": "error", "message": "No WhatsApp account connected."}

        query = WhatsAppTemplate.query.filter_by(account_id=account_id, is_archived=False)

        status_filter = params.get("status")
        if status_filter:
            query = query.filter(WhatsAppTemplate.status == status_filter.upper())

        category_filter = params.get("category")
        if category_filter:
            query = query.filter(WhatsAppTemplate.category == category_filter.upper())

        templates = query.order_by(WhatsAppTemplate.created_at.desc()).limit(20).all()

        if not templates:
            return {"status": "success", "message": "No templates found matching your criteria.", "data": []}

        items = []
        lines = []
        for i, t in enumerate(templates, 1):
            item = {
                "id": t.id,
                "name": t.name,
                "category": t.category,
                "status": t.status,
                "language": t.language,
            }
            items.append(item)
            emoji = {"APPROVED": "✅", "PENDING": "⏳", "REJECTED": "❌"}.get(t.status, "📝")
            lines.append(f"{i}. {emoji} **{t.name}** — {t.category} ({t.status})")

        msg = f"**Templates ({len(items)}):**\n" + "\n".join(lines)
        return {"status": "success", "message": msg, "data": items, "ui_action": "show_list"}


@action_registry.register(domain="template", action="create")
class CreateTemplateAction(BaseAction):
    name = "create_template"
    description = "Create a new WhatsApp message template"
    required_params = ["name", "category", "body"]
    optional_params = ["language", "header_text", "footer_text"]

    def get_missing_params_prompt(self, missing: List[str]) -> str:
        prompts = {
            "name": "What should the template be called? (Use lowercase with underscores, e.g. `order_confirmation`)",
            "category": "What category? Choose: **UTILITY**, **MARKETING**, or **AUTHENTICATION**",
            "body": "What should the template body text say? You can use {{1}}, {{2}} for variables.",
        }
        first = missing[0]
        return prompts.get(first, f"Please provide the **{first}**.")

    def execute(self, params: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
        from whatsapp.models import WhatsAppTemplate, WhatsAppAccount
        from models import db

        workspace_id = context.get("workspace_id")
        account_id = context.get("account_id")

        if not account_id:
            acct = WhatsAppAccount.query.filter_by(workspace_id=workspace_id, is_active=True).first()
            account_id = acct.id if acct else None

        if not account_id:
            return {"status": "error", "message": "No WhatsApp account connected."}

        name = params["name"].strip().lower().replace(" ", "_")
        category = params["category"].strip().upper()
        body = params["body"]
        language = params.get("language", "en_US")

        # Check if template name already exists. WhatsApp does NOT allow editing a
        # template's media/buttons/body once it's submitted — so "add an image /
        # CTA to this template" can't modify the existing one. Return an
        # actionable message (not a bare "already exists" that reads as a loop).
        existing = WhatsAppTemplate.query.filter_by(
            account_id=account_id, name=name, language=language
        ).first()
        if existing:
            _status = (existing.status or "").upper()
            return {
                "status": "error",
                "cannot_edit_existing": True,
                "message": (
                    f"A template named **{name}** already exists (status: {existing.status}). "
                    "WhatsApp doesn't allow changing a submitted template's image, buttons, or "
                    "text — you can't add them to this one. To get a version with an image and a "
                    "CTA button:\n"
                    f"• Create it under a **new name** (e.g. `{name}_v2`) with the image + button, or\n"
                    f"• Delete **{name}** first "
                    + ("(it's still pending), then recreate it with the image + button."
                       if _status in ("PENDING", "DRAFT", "REJECTED")
                       else "in the Templates page, then recreate it with the image + button.")
                ),
            }

        import re
        variable_count = len(set(re.findall(r'\{\{\d+\}\}', body)))

        template = WhatsAppTemplate(
            account_id=account_id,
            name=name,
            category=category,
            language=language,
            body_text=body,
            header_text=params.get("header_text"),
            footer_text=params.get("footer_text"),
            variable_count=variable_count,
            status="PENDING",
            local_status="DRAFT",
            components=[{"type": "BODY", "text": body}],
        )
        db.session.add(template)
        db.session.commit()

        msg = (
            f"✅ Template **{name}** created (ID: {template.id})!\n"
            f"• Category: {category}\n"
            f"• Language: {language}\n"
            f"• Variables: {variable_count}\n"
            f"• Status: DRAFT (needs submission to Meta for approval)"
        )
        return {
            "status": "success",
            "message": msg,
            "data": {"template_id": template.id, "name": name},
            "navigate_to": "/dashboard/templates",
        }


@action_registry.register(domain="template", action="send")
class SendTemplateAction(BaseAction):
    name = "send_template"
    description = "Send a template message to a phone number"
    required_params = ["to", "template_name"]
    optional_params = ["language", "params"]

    def get_missing_params_prompt(self, missing: List[str]) -> str:
        prompts = {
            "to": "Which phone number should I send it to? (Include country code, e.g. 919876543210)",
            "template_name": "Which template should I send? Type the template name.",
        }
        first = missing[0]
        return prompts.get(first, f"Please provide **{first}**.")

    def execute(self, params: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
        workspace_id = context.get("workspace_id")
        to = params["to"].strip()
        template_name = params["template_name"].strip()
        language = params.get("language", "en_US")

        # Confirmation required for sending messages
        session = context.get("session")
        if session and not getattr(session, '_confirmed', False):
            msg = f"📨 Ready to send template **{template_name}** to **{to}**. Proceed?"
            return {"status": "confirmation_required", "message": msg}

        try:
            from whatsapp.services import WhatsAppService
            account_id = context.get("account_id")
            svc = WhatsAppService(workspace_id=workspace_id, account_id=account_id)
            result = svc.send_template(
                to=to,
                template_name=template_name,
                language_code=language,
                components=params.get("params"),
            )
            if result.get("success") or result.get("messages"):
                return {
                    "status": "success",
                    "message": f"✅ Template **{template_name}** sent to **{to}** successfully!",
                    "data": result,
                }
            return {"status": "error", "message": f"Failed to send: {result.get('error', 'Unknown error')}"}
        except Exception as exc:
            return {"status": "error", "message": f"Send failed: {exc}"}


@action_registry.register(domain="template", action="sync")
class SyncTemplatesAction(BaseAction):
    name = "sync_templates"
    description = "Sync templates from Meta/WhatsApp"
    required_params: List[str] = []
    optional_params = ["account_id"]

    def execute(self, params: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
        workspace_id = context.get("workspace_id")
        account_id = context.get("account_id")

        if not account_id:
            return {"status": "error", "message": "No WhatsApp account connected."}

        try:
            from whatsapp.template_service import TemplateService
            svc = TemplateService(account_id=account_id)
            result = svc.sync_templates()
            synced = result.get("synced", 0) if isinstance(result, dict) else 0
            return {
                "status": "success",
                "message": f"🔄 Templates synced from Meta! {synced} templates updated.",
                "navigate_to": "/dashboard/templates",
            }
        except Exception as exc:
            logger.warning("Template sync failed: %s", exc)
            return {"status": "error", "message": f"Sync failed: {exc}"}


@action_registry.register(domain="template", action="delete")
class DeleteTemplateAction(BaseAction):
    name = "delete_template"
    description = "Delete a WhatsApp message template"
    required_params = ["template_id"]
    optional_params: List[str] = []

    def get_missing_params_prompt(self, missing: List[str]) -> str:
        return "Which template would you like to delete? Please provide the template ID or name."

    def execute(self, params: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
        from whatsapp.models import WhatsAppTemplate
        from models import db

        template_id = params["template_id"]
        try:
            template_id = int(template_id)
        except (ValueError, TypeError):
            # Try lookup by name
            workspace_id = context.get("workspace_id")
            account_id = context.get("account_id")
            t = WhatsAppTemplate.query.filter(
                WhatsAppTemplate.account_id == account_id,
                WhatsAppTemplate.name == str(template_id),
            ).first()
            if t:
                template_id = t.id
            else:
                return {"status": "error", "message": f"Template '{params['template_id']}' not found."}

        account_id = context.get("account_id")
        template = WhatsAppTemplate.query.filter_by(id=template_id, account_id=account_id).first()
        if not template:
            return {"status": "error", "message": "Template not found."}

        template.is_archived = True
        from datetime import datetime, timezone
        template.archived_at = datetime.now(timezone.utc)
        db.session.commit()

        return {
            "status": "success",
            "message": f"🗑️ Template **{template.name}** has been archived.",
        }
