"""
Bulk Messaging Actions — list, create, add recipients, send, schedule.
"""

import logging
from typing import Dict, Any, List

from agent_backend.action_registry import BaseAction, action_registry

logger = logging.getLogger(__name__)


@action_registry.register(domain="bulk", action="list")
class ListBulkCampaignsAction(BaseAction):
    name = "list_bulk_campaigns"
    description = "List bulk messaging campaigns"
    required_params: List[str] = []

    def execute(self, params: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
        try:
            from whatsapp.models import WhatsAppAccount
            from models import db
            workspace_id = context.get("workspace_id")
            account_id = context.get("account_id")

            if not account_id:
                return {"status": "error", "message": "No WhatsApp account connected."}

            # Use raw query since BulkCampaign model might vary
            from sqlalchemy import text
            result = db.session.execute(
                text("SELECT id, name, status, created_at FROM bulk_campaigns WHERE account_id = :aid ORDER BY created_at DESC LIMIT 20"),
                {"aid": account_id}
            ).fetchall()

            if not result:
                return {
                    "status": "success",
                    "message": "No bulk campaigns found. Would you like to create one?",
                    "data": [],
                }

            items = []
            lines = []
            for i, row in enumerate(result, 1):
                item = {"id": row[0], "name": row[1], "status": row[2]}
                items.append(item)
                lines.append(f"{i}. **{row[1]}** — {row[2]}")

            msg = f"**Bulk Campaigns ({len(items)}):**\n" + "\n".join(lines)
            return {"status": "success", "message": msg, "data": items, "ui_action": "show_list"}

        except Exception as exc:
            logger.warning("bulk/list query failed: %s", exc)
            return {
                "status": "error",
                "message": "⚠️ Could not load bulk campaigns. Go to the **Datasets** page to view them directly.",
                "navigate_to": "/dashboard/datasets",
            }


@action_registry.register(domain="bulk", action="create")
class CreateBulkCampaignAction(BaseAction):
    name = "create_bulk_campaign"
    description = "Create a new bulk messaging campaign"
    required_params = ["name"]
    optional_params = ["template_id"]

    def get_missing_params_prompt(self, missing: List[str]) -> str:
        prompts = {
            "name": "What should the bulk campaign be called?",
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
            workspace_id=str(workspace_id),
            name=params["name"],
            trigger_type="manual",
            status="draft",
        )
        db.session.add(campaign)
        db.session.commit()

        return {
            "status": "success",
            "message": f"✅ Bulk campaign **{params['name']}** created (ID: {campaign.id})! Go to **Datasets** to add recipients and a template, then launch.",
            "data": {"campaign_id": campaign.id, "name": campaign.name},
            "navigate_to": "/dashboard/datasets",
        }


@action_registry.register(domain="bulk", action="send")
class SendBulkCampaignAction(BaseAction):
    name = "send_bulk_campaign"
    description = "Send/start a bulk messaging campaign"
    required_params = ["campaign_id"]

    def get_missing_params_prompt(self, missing: List[str]) -> str:
        return "Which campaign should I send? Provide the campaign ID."

    def execute(self, params: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
        from whatsapp.drip_models import WhatsAppDripCampaign
        workspace_id = context.get("workspace_id")

        campaign = WhatsAppDripCampaign.query.filter_by(
            id=params["campaign_id"], workspace_id=str(workspace_id)
        ).first()
        if not campaign:
            return {"status": "error", "message": "Campaign not found."}

        if campaign.status == "active":
            return {"status": "error", "message": f"Campaign **{campaign.name}** is already active/sending."}

        return {
            "status": "success",
            "message": (
                f"📨 Campaign **{campaign.name}** is ready.\n"
                f"⚠️ Bulk sends require final confirmation on the **Datasets** page — "
                f"this ensures you've reviewed the audience and template before sending to real users."
            ),
            "navigate_to": "/dashboard/datasets",
        }


@action_registry.register(domain="bulk", action="schedule")
class ScheduleBulkCampaignAction(BaseAction):
    name = "schedule_bulk_campaign"
    description = "Schedule a bulk campaign for later sending"
    required_params = ["campaign_id", "scheduled_at"]

    def get_missing_params_prompt(self, missing: List[str]) -> str:
        prompts = {
            "campaign_id": "Which campaign would you like to schedule?",
            "scheduled_at": "When should it be sent? (e.g. 'tomorrow at 10am', '2024-03-01 14:00')",
        }
        first = missing[0]
        return prompts.get(first, f"Please provide **{first}**.")

    def execute(self, params: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
        from whatsapp.drip_models import WhatsAppDripCampaign
        workspace_id = context.get("workspace_id")

        campaign = WhatsAppDripCampaign.query.filter_by(
            id=params["campaign_id"], workspace_id=str(workspace_id)
        ).first()
        if not campaign:
            return {"status": "error", "message": "Campaign not found."}

        return {
            "status": "success",
            "message": (
                f"📅 Campaign **{campaign.name}** — scheduling requires the full wizard on the Datasets page "
                f"(audience review + time picker). Taking you there now."
            ),
            "navigate_to": "/dashboard/datasets",
        }
