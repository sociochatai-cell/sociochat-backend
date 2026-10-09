"""
Drip Campaign Actions — create, list, update, delete, enroll.
"""

import logging
from typing import Dict, Any, List

from agent_backend.action_registry import BaseAction, action_registry

logger = logging.getLogger(__name__)


@action_registry.register(domain="drip", action="list")
class ListDripCampaignsAction(BaseAction):
    name = "list_drip_campaigns"
    description = "List drip campaigns"
    required_params: List[str] = []

    def execute(self, params: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
        from whatsapp.drip_models import WhatsAppDripCampaign
        workspace_id = context.get("workspace_id")
        account_id = context.get("account_id")

        if not account_id:
            return {"status": "error", "message": "No WhatsApp account connected."}

        # Match the original drip_routes.list_campaigns() query:
        # 1) Filter by both account_id AND workspace_id
        # 2) Exclude bulk campaigns (trigger_type='manual')
        query = WhatsAppDripCampaign.query.filter_by(
            account_id=account_id,
        )
        if workspace_id:
            query = query.filter_by(workspace_id=workspace_id)

        query = query.filter(
            WhatsAppDripCampaign.trigger_type != "manual"
        )

        campaigns = query.order_by(
            WhatsAppDripCampaign.created_at.desc()
        ).limit(20).all()

        if not campaigns:
            return {"status": "success", "message": "No drip campaigns found. Would you like to create one?", "data": []}

        items = []
        lines = []
        for i, c in enumerate(campaigns, 1):
            item = {
                "id": c.id,
                "name": c.name,
                "status": c.status or "draft",
                "steps_count": len(c.steps),
                "enrolled_count": c.enrolled_count or 0,
            }
            items.append(item)
            status_emoji = {"active": "🟢", "paused": "⏸️", "draft": "📝"}.get(item["status"], "⚪")
            lines.append(f"{i}. {status_emoji} **{c.name}** — {item['status']} ({item['steps_count']} steps, {item['enrolled_count']} enrolled)")

        msg = f"**Drip Campaigns ({len(items)}):**\n" + "\n".join(lines)
        return {"status": "success", "message": msg, "data": items, "ui_action": "show_list"}


@action_registry.register(domain="drip", action="create")
class CreateDripCampaignAction(BaseAction):
    name = "create_drip_campaign"
    description = "Create a new drip campaign"
    required_params = ["name"]
    optional_params = ["description", "template_id"]

    def get_missing_params_prompt(self, missing: List[str]) -> str:
        prompts = {
            "name": "What should the drip campaign be called?",
        }
        first = missing[0]
        return prompts.get(first, f"Please provide **{first}**.")

    def execute(self, params: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
        from whatsapp.drip_models import WhatsAppDripCampaign
        from models import db

        workspace_id = context.get("workspace_id")
        account_id = context.get("account_id")

        if not account_id:
            return {"status": "error", "message": "No WhatsApp account connected."}

        campaign = WhatsAppDripCampaign(
            account_id=account_id,
            workspace_id=workspace_id,
            name=params["name"],
            description=params.get("description", ""),
            trigger_type="drip_manual",
            status="draft",
        )
        db.session.add(campaign)
        db.session.commit()

        return {
            "status": "success",
            "message": f"✅ Drip campaign **{params['name']}** created (ID: {campaign.id}). You can now add steps and enroll contacts.",
            "data": {"campaign_id": campaign.id, "name": campaign.name},
            "navigate_to": "/dashboard/drip",
        }


@action_registry.register(domain="drip", action="update")
class UpdateDripCampaignAction(BaseAction):
    name = "update_drip_campaign"
    description = "Update an existing drip campaign"
    required_params = ["campaign_id"]
    optional_params = ["name", "description", "status"]

    def get_missing_params_prompt(self, missing: List[str]) -> str:
        return "Which drip campaign would you like to update? Please provide the campaign ID."

    def execute(self, params: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
        from whatsapp.drip_models import WhatsAppDripCampaign
        from models import db

        workspace_id = context.get("workspace_id")
        campaign = WhatsAppDripCampaign.query.filter_by(
            id=params["campaign_id"], workspace_id=workspace_id
        ).first()
        if not campaign:
            return {"status": "error", "message": "Campaign not found."}

        if "name" in params:
            campaign.name = params["name"]
        if "description" in params:
            campaign.description = params["description"]

        db.session.commit()
        return {"status": "success", "message": f"✅ Campaign **{campaign.name}** updated."}


@action_registry.register(domain="drip", action="delete")
class DeleteDripCampaignAction(BaseAction):
    name = "delete_drip_campaign"
    description = "Delete a drip campaign"
    required_params = ["campaign_id"]

    def get_missing_params_prompt(self, missing: List[str]) -> str:
        return "Which campaign would you like to delete? Please provide the campaign ID."

    def execute(self, params: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
        from whatsapp.drip_models import WhatsAppDripCampaign
        from models import db

        session = context.get("session")
        if not (session and getattr(session, "_confirmed", False)):
            msg = f"Are you sure you want to delete campaign #{params['campaign_id']}? This cannot be undone."
            return {"status": "confirmation_required", "message": msg}

        workspace_id = context.get("workspace_id")
        campaign = WhatsAppDripCampaign.query.filter_by(
            id=params["campaign_id"], workspace_id=workspace_id
        ).first()
        if not campaign:
            return {"status": "error", "message": "Campaign not found."}

        name = campaign.name
        db.session.delete(campaign)
        db.session.commit()
        return {"status": "success", "message": f"🗑️ Campaign **{name}** has been deleted."}


@action_registry.register(domain="drip", action="add_step")
class AddDripStepAction(BaseAction):
    name = "add_drip_step"
    description = "Add a step to a drip campaign (template + delay)"
    required_params = ["campaign_id", "template_name"]
    optional_params = ["delay_minutes", "step_order", "language"]

    def get_missing_params_prompt(self, missing: List[str]) -> str:
        prompts = {
            "campaign_id": "Which drip campaign should I add a step to? (provide campaign ID)",
            "template_name": "Which template should this step send? (provide template name)",
        }
        first = missing[0]
        return prompts.get(first, f"Please provide **{first}**.")

    def execute(self, params: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
        from whatsapp.drip_models import WhatsAppDripCampaign, WhatsAppDripStep
        from models import db

        workspace_id = context.get("workspace_id")
        campaign = WhatsAppDripCampaign.query.filter_by(
            id=params["campaign_id"], workspace_id=workspace_id
        ).first()
        if not campaign:
            return {"status": "error", "message": "Campaign not found."}

        delay_minutes = int(params.get("delay_minutes", 0))
        step_order = int(params.get("step_order", len(campaign.steps) + 1))
        language = params.get("language", "en_US")

        step = WhatsAppDripStep(
            campaign_id=campaign.id,
            step_order=step_order,
            delay_seconds=delay_minutes * 60,
            template_name=params["template_name"],
            language=language,
        )
        db.session.add(step)
        db.session.commit()

        delay_label = f"{delay_minutes} min" if delay_minutes < 60 else f"{delay_minutes // 60}h {delay_minutes % 60}m"
        if delay_minutes == 0:
            delay_label = "immediately"

        return {
            "status": "success",
            "message": (
                f"✅ Step #{step_order} added to **{campaign.name}**!\n"
                f"• Template: {params['template_name']}\n"
                f"• Delay: {delay_label}\n"
                f"• Total steps: {len(campaign.steps)}"
            ),
            "data": {"step_id": step.id, "campaign_id": campaign.id},
        }


@action_registry.register(domain="drip", action="activate")
class ActivateDripAction(BaseAction):
    name = "activate_drip"
    description = "Activate a drip campaign (start sending)"
    required_params = ["campaign_id"]

    def get_missing_params_prompt(self, missing: List[str]) -> str:
        return "Which campaign should I activate? (provide campaign ID)"

    def execute(self, params: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
        from whatsapp.drip_models import WhatsAppDripCampaign
        from models import db

        session = context.get("session")
        if not (session and getattr(session, "_confirmed", False)):
            return {"status": "confirmation_required",
                    "message": f"Activate campaign #{params['campaign_id']}? It will start sending to enrolled contacts."}

        workspace_id = context.get("workspace_id")
        campaign = WhatsAppDripCampaign.query.filter_by(
            id=params["campaign_id"], workspace_id=workspace_id
        ).first()
        if not campaign:
            return {"status": "error", "message": "Campaign not found."}

        if not campaign.steps:
            return {"status": "error", "message": f"Campaign **{campaign.name}** has no steps. Add at least one step first."}

        campaign.status = "active"
        db.session.commit()

        return {
            "status": "success",
            "message": f"🟢 Campaign **{campaign.name}** is now **active** with {len(campaign.steps)} steps!",
        }


@action_registry.register(domain="drip", action="enroll")
class EnrollDripAction(BaseAction):
    name = "enroll_drip"
    description = "Enroll contacts into a drip campaign"
    required_params = ["campaign_id", "phone_numbers"]
    optional_params: List[str] = []

    def get_missing_params_prompt(self, missing: List[str]) -> str:
        prompts = {
            "campaign_id": "Which campaign should I enroll them in? (provide campaign ID)",
            "phone_numbers": "Which phone numbers? (comma-separated, with country codes)",
        }
        first = missing[0]
        return prompts.get(first, f"Please provide **{first}**.")

    def execute(self, params: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
        from whatsapp.drip_models import WhatsAppDripCampaign, WhatsAppDripEnrollment
        from whatsapp.models import WhatsAppAccount
        from models import db

        workspace_id = context.get("workspace_id")
        account_id = context.get("account_id")

        if not account_id:
            return {"status": "error", "message": "No WhatsApp account connected."}

        campaign = WhatsAppDripCampaign.query.filter_by(
            id=params["campaign_id"], workspace_id=workspace_id
        ).first()
        if not campaign:
            return {"status": "error", "message": "Campaign not found."}

        if campaign.status != "active":
            return {"status": "error", "message": f"Campaign **{campaign.name}** is {campaign.status} — activate it first."}

        phone_numbers = params["phone_numbers"]
        if isinstance(phone_numbers, str):
            phone_numbers = [p.strip() for p in phone_numbers.split(",") if p.strip()]

        enrolled = 0
        skipped = 0
        for phone in phone_numbers:
            existing = WhatsAppDripEnrollment.query.filter_by(
                campaign_id=campaign.id, phone_number=phone
            ).first()
            if existing:
                skipped += 1
                continue
            enrollment = WhatsAppDripEnrollment(
                campaign_id=campaign.id,
                account_id=account_id,
                phone_number=phone,
                status="active",
                current_step=0,
            )
            db.session.add(enrollment)
            enrolled += 1

        db.session.commit()
        msg = f"✅ Enrolled **{enrolled}** contacts into **{campaign.name}**."
        if skipped:
            msg += f" ({skipped} already enrolled, skipped)"
        return {"status": "success", "message": msg, "data": {"enrolled": enrolled, "skipped": skipped}}
