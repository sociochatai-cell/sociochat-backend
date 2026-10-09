"""
Flow Actions — list and view WhatsApp Interactive Flows and Forms.
"""

import logging
from typing import Dict, Any, List

from agent_backend.action_registry import BaseAction, action_registry

logger = logging.getLogger(__name__)


@action_registry.register(domain="flow", action="list")
class ListFlowsAction(BaseAction):
    name = "list_flows"
    description = "List WhatsApp interactive flows"
    required_params: List[str] = []
    optional_params = ["status"]

    def execute(self, params: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
        from whatsapp.models import WhatsAppFlow
        account_id = context.get("account_id")

        if not account_id:
            return {"status": "error", "message": "No WhatsApp account connected."}

        query = WhatsAppFlow.query.filter_by(account_id=account_id)
        status_filter = params.get("status")
        if status_filter:
            query = query.filter(WhatsAppFlow.status == status_filter.upper())

        flows = query.order_by(WhatsAppFlow.created_at.desc()).limit(20).all()

        if not flows:
            return {"status": "success", "message": "No interactive flows found.", "data": []}

        items = []
        lines = []
        for i, f in enumerate(flows, 1):
            screen_count = len(f.get_screens()) if f.flow_json else 0
            item = {
                "id": f.id, "name": f.name, "category": f.category,
                "status": f.status, "version": f.flow_version,
                "screens": screen_count,
            }
            items.append(item)
            emoji = {"PUBLISHED": "🟢", "DRAFT": "📝", "DEPRECATED": "🔴"}.get(f.status, "⚪")
            lines.append(f"{i}. {emoji} **{f.name}** — {f.category} ({f.status}, v{f.flow_version}, {screen_count} screens)")

        msg = f"**Interactive Flows ({len(items)}):**\n" + "\n".join(lines)
        return {"status": "success", "message": msg, "data": items, "ui_action": "show_list"}


@action_registry.register(domain="flow", action="get_info")
class GetFlowInfoAction(BaseAction):
    name = "get_flow_info"
    description = "Get details of a specific interactive flow"
    required_params = ["flow_id"]

    def get_missing_params_prompt(self, missing: List[str]) -> str:
        return "Which flow? Provide the flow ID or name."

    def execute(self, params: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
        from whatsapp.models import WhatsAppFlow
        account_id = context.get("account_id")

        if not account_id:
            return {"status": "error", "message": "No WhatsApp account connected."}

        flow_id = params["flow_id"]
        try:
            fid = int(flow_id)
            flow = WhatsAppFlow.query.filter_by(id=fid, account_id=account_id).first()
        except (ValueError, TypeError):
            flow = WhatsAppFlow.query.filter_by(name=str(flow_id), account_id=account_id).first()

        if not flow:
            return {"status": "error", "message": "Flow not found."}

        screens = flow.get_screens()
        screen_names = [s.get("id", "unnamed") for s in screens]

        msg = (
            f"📋 **Flow: {flow.name}**\n"
            f"• Category: {flow.category}\n"
            f"• Status: {flow.status}\n"
            f"• Version: {flow.flow_version}\n"
            f"• Screens ({len(screens)}): {', '.join(screen_names)}\n"
            f"• Meta Flow ID: {flow.meta_flow_id or 'not published'}\n"
            f"• Created: {flow.created_at}"
        )
        return {"status": "success", "message": msg, "ui_action": "show_card"}


@action_registry.register(domain="form", action="list")
class ListFormsAction(BaseAction):
    name = "list_forms"
    description = "List WhatsApp forms (booking forms, lead gen forms)"
    required_params: List[str] = []

    def execute(self, params: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
        from whatsapp.models import WhatsAppFlow
        account_id = context.get("account_id")

        if not account_id:
            return {"status": "error", "message": "No WhatsApp account connected."}

        forms = WhatsAppFlow.query.filter(
            WhatsAppFlow.account_id == account_id,
            WhatsAppFlow.category.in_(["LEAD_GEN", "BOOKING", "SURVEY", "FEEDBACK"]),
        ).order_by(WhatsAppFlow.created_at.desc()).limit(20).all()

        if not forms:
            return {"status": "success", "message": "No forms found. Create one in **WhatsApp Forms**.", "data": [],
                    "navigate_to": "/dashboard/flows"}

        items = []
        lines = []
        for i, f in enumerate(forms, 1):
            item = {"id": f.id, "name": f.name, "category": f.category, "status": f.status}
            items.append(item)
            emoji = {"PUBLISHED": "🟢", "DRAFT": "📝"}.get(f.status, "⚪")
            lines.append(f"{i}. {emoji} **{f.name}** — {f.category} ({f.status})")

        msg = f"**Forms ({len(items)}):**\n" + "\n".join(lines)
        return {"status": "success", "message": msg, "data": items, "ui_action": "show_list"}
