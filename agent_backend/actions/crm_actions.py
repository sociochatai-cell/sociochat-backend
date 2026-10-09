"""
CRM Actions — list, create, update leads and deals.
"""

import logging
from typing import Dict, Any, List

from agent_backend.action_registry import BaseAction, action_registry

logger = logging.getLogger(__name__)


@action_registry.register(domain="crm", action="list_leads")
class ListLeadsAction(BaseAction):
    name = "list_crm_leads"
    description = "List CRM leads (optionally filter by status)"
    required_params: List[str] = []
    optional_params = ["status"]

    def execute(self, params: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
        from whatsapp.crm_lead_models import CrmLead
        workspace_id = context.get("workspace_id")

        query = CrmLead.query.filter_by(workspace_id=int(workspace_id))
        status_filter = params.get("status")
        if status_filter:
            query = query.filter(CrmLead.status == status_filter.lower())

        leads = query.order_by(CrmLead.created_at.desc()).limit(20).all()

        if not leads:
            return {"status": "success", "message": "No leads found. Would you like to create one?", "data": []}

        items = []
        lines = []
        for i, l in enumerate(leads, 1):
            item = {"id": l.id, "name": l.name, "email": l.email, "phone": l.phone, "status": l.status}
            items.append(item)
            emoji = {"new": "🆕", "contacted": "📞", "qualified": "✅", "proposal": "📋", "closed": "🏁"}.get(l.status, "⚪")
            lines.append(f"{i}. {emoji} **{l.name}** — {l.status or 'new'} {('· ' + l.phone) if l.phone else ''}")

        msg = f"**CRM Leads ({len(items)}):**\n" + "\n".join(lines)
        return {"status": "success", "message": msg, "data": items, "ui_action": "show_list"}


@action_registry.register(domain="crm", action="create_lead")
class CreateLeadAction(BaseAction):
    name = "create_crm_lead"
    description = "Create a new CRM lead"
    required_params = ["name"]
    optional_params = ["email", "phone", "company", "status", "source"]

    def get_missing_params_prompt(self, missing: List[str]) -> str:
        return "What's the lead's name?"

    def execute(self, params: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
        from whatsapp.crm_lead_models import CrmLead
        from models import db

        workspace_id = context.get("workspace_id")

        lead = CrmLead(
            workspace_id=int(workspace_id),
            name=params["name"],
            email=params.get("email"),
            phone=params.get("phone"),
            company=params.get("company"),
            status=params.get("status", "new"),
            source=params.get("source", "agent"),
        )
        db.session.add(lead)
        db.session.commit()

        msg = f"✅ Lead **{params['name']}** created!"
        if params.get("phone"):
            msg += f"\n• Phone: {params['phone']}"
        if params.get("email"):
            msg += f"\n• Email: {params['email']}"

        return {
            "status": "success",
            "message": msg,
            "data": {"lead_id": lead.id, "name": lead.name},
            "navigate_to": "/dashboard/crm/leads",
        }


@action_registry.register(domain="crm", action="update_lead")
class UpdateLeadAction(BaseAction):
    name = "update_crm_lead"
    description = "Update a CRM lead's status or details"
    required_params = ["lead_id"]
    optional_params = ["name", "email", "phone", "status", "company"]

    def get_missing_params_prompt(self, missing: List[str]) -> str:
        return "Which lead would you like to update? Provide the lead ID."

    def execute(self, params: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
        from whatsapp.crm_lead_models import CrmLead
        from models import db

        workspace_id = context.get("workspace_id")
        lead = CrmLead.query.filter_by(id=params["lead_id"], workspace_id=int(workspace_id)).first()
        if not lead:
            return {"status": "error", "message": "Lead not found."}

        updated = []
        for field in ("name", "email", "phone", "status", "company"):
            if field in params and params[field] is not None:
                setattr(lead, field, params[field])
                updated.append(field)

        db.session.commit()
        return {"status": "success", "message": f"✅ Lead **{lead.name}** updated ({', '.join(updated)})."}


@action_registry.register(domain="crm", action="list_deals")
class ListDealsAction(BaseAction):
    name = "list_crm_deals"
    description = "List CRM deals (optionally filter by stage)"
    required_params: List[str] = []
    optional_params = ["stage"]

    def execute(self, params: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
        from models import db
        from sqlalchemy import text
        workspace_id = context.get("workspace_id")

        try:
            sql = "SELECT id, name, value, stage, status FROM deals WHERE workspace_id = :wid ORDER BY created_at DESC LIMIT 20"
            params_q = {"wid": int(workspace_id)}
            stage_filter = params.get("stage")
            if stage_filter:
                sql = "SELECT id, name, value, stage, status FROM deals WHERE workspace_id = :wid AND stage = :stage ORDER BY created_at DESC LIMIT 20"
                params_q["stage"] = stage_filter.lower()

            rows = db.session.execute(text(sql), params_q).fetchall()
        except Exception as exc:
            logger.warning("Deal list query failed: %s", exc)
            return {"status": "error", "message": f"Could not load deals: {exc}"}

        if not rows:
            return {"status": "success", "message": "No deals found. Would you like to create one?", "data": []}

        items = []
        lines = []
        for i, row in enumerate(rows, 1):
            item = {"id": row[0], "name": row[1], "value": float(row[2] or 0), "stage": row[3], "status": row[4]}
            items.append(item)
            emoji = {"won": "🏆", "lost": "❌", "open": "📊"}.get(item["status"], "⚪")
            lines.append(f"{i}. {emoji} **{item['name']}** — {item['stage']} (₹{item['value']:,.0f})")

        msg = f"**CRM Deals ({len(items)}):**\n" + "\n".join(lines)
        return {"status": "success", "message": msg, "data": items, "ui_action": "show_list"}


@action_registry.register(domain="crm", action="create_deal")
class CreateDealAction(BaseAction):
    name = "create_crm_deal"
    description = "Create a new CRM deal"
    required_params = ["name"]
    optional_params = ["value", "stage", "company", "contact_id"]

    def get_missing_params_prompt(self, missing: List[str]) -> str:
        return "What's the deal name?"

    def execute(self, params: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
        import uuid
        from models import db
        from sqlalchemy import text
        workspace_id = context.get("workspace_id")

        deal_id = str(uuid.uuid4())
        stage = params.get("stage", "prospect")
        value = params.get("value", 0)

        try:
            db.session.execute(text(
                "INSERT INTO deals (id, workspace_id, name, value, stage, status, company, created_at, updated_at) "
                "VALUES (:id, :wid, :name, :val, :stage, 'open', :company, NOW(), NOW())"
            ), {
                "id": deal_id,
                "wid": int(workspace_id),
                "name": params["name"],
                "val": value,
                "stage": stage,
                "company": params.get("company"),
            })
            db.session.commit()
        except Exception as exc:
            db.session.rollback()
            logger.warning("Deal create failed: %s", exc)
            return {"status": "error", "message": f"Could not create deal: {exc}"}

        return {
            "status": "success",
            "message": f"✅ Deal **{params['name']}** created (stage: {stage}).",
            "data": {"deal_id": deal_id, "name": params["name"]},
            "navigate_to": "/dashboard/crm/deals",
        }
