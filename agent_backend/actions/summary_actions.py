"""
Summary Actions — account overview and workspace stats.
"""

import logging
from typing import Dict, Any, List

from agent_backend.action_registry import BaseAction, action_registry

logger = logging.getLogger(__name__)


@action_registry.register(domain="account", action="summary")
class AccountSummaryAction(BaseAction):
    name = "account_summary"
    description = "Get a summary of your account — contacts, conversations, templates, campaigns, orders"
    required_params: List[str] = []

    def execute(self, params: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
        from models import db
        from sqlalchemy import text

        workspace_id = context.get("workspace_id")
        account_id = context.get("account_id")

        stats = {}

        try:
            r = db.session.execute(text("SELECT count(*) FROM contacts WHERE workspace_id = :wid"), {"wid": int(workspace_id)}).fetchone()
            stats["contacts"] = r[0] if r else 0
        except Exception:
            stats["contacts"] = "N/A"

        if account_id:
            try:
                r = db.session.execute(text(
                    "SELECT count(*) FROM whatsapp_conversations WHERE account_id = :aid"
                ), {"aid": account_id}).fetchone()
                stats["conversations"] = r[0] if r else 0
            except Exception:
                stats["conversations"] = "N/A"

            try:
                r = db.session.execute(text(
                    "SELECT count(*) FROM whatsapp_templates WHERE account_id = :aid AND is_archived = false"
                ), {"aid": account_id}).fetchone()
                stats["templates"] = r[0] if r else 0
            except Exception:
                stats["templates"] = "N/A"

            try:
                r = db.session.execute(text(
                    "SELECT count(*), "
                    "count(*) FILTER (WHERE status = 'active') "
                    "FROM whatsapp_drip_campaigns WHERE account_id = :aid"
                ), {"aid": account_id}).fetchone()
                stats["campaigns_total"] = r[0] if r else 0
                stats["campaigns_active"] = r[1] if r else 0
            except Exception:
                stats["campaigns_total"] = "N/A"
                stats["campaigns_active"] = "N/A"

            try:
                r = db.session.execute(text(
                    "SELECT count(*) FROM whatsapp_messages WHERE conversation_id IN "
                    "(SELECT id FROM whatsapp_conversations WHERE account_id = :aid) "
                    "AND created_at >= NOW() - INTERVAL '7 days'"
                ), {"aid": account_id}).fetchone()
                stats["messages_7d"] = r[0] if r else 0
            except Exception:
                stats["messages_7d"] = "N/A"
        else:
            stats["conversations"] = "No WhatsApp connected"
            stats["templates"] = "No WhatsApp connected"
            stats["campaigns_total"] = "N/A"
            stats["campaigns_active"] = "N/A"
            stats["messages_7d"] = "N/A"

        try:
            r = db.session.execute(text(
                "SELECT count(*), "
                "count(*) FILTER (WHERE status = 'paid'), "
                "COALESCE(SUM(amount) FILTER (WHERE status = 'paid'), 0) "
                "FROM commerce_orders WHERE workspace_id = :wid"
            ), {"wid": int(workspace_id)}).fetchone()
            stats["orders_total"] = r[0] if r else 0
            stats["orders_paid"] = r[1] if r else 0
            stats["revenue"] = float(r[2]) if r else 0
        except Exception:
            stats["orders_total"] = "N/A"
            stats["orders_paid"] = "N/A"
            stats["revenue"] = "N/A"

        try:
            r = db.session.execute(text(
                "SELECT count(*) FROM leads WHERE workspace_id = :wid"
            ), {"wid": int(workspace_id)}).fetchone()
            stats["leads"] = r[0] if r else 0
        except Exception:
            stats["leads"] = "N/A"

        try:
            r = db.session.execute(text(
                "SELECT count(*) FROM deals WHERE workspace_id = :wid"
            ), {"wid": int(workspace_id)}).fetchone()
            stats["deals"] = r[0] if r else 0
        except Exception:
            stats["deals"] = "N/A"

        revenue_str = f"₹{stats['revenue']:,.0f}" if isinstance(stats.get("revenue"), (int, float)) else stats.get("revenue", "N/A")

        msg = (
            f"📊 **Account Summary**\n\n"
            f"**Contacts & CRM:**\n"
            f"• Contacts: {stats['contacts']}\n"
            f"• Leads: {stats['leads']}\n"
            f"• Deals: {stats['deals']}\n\n"
            f"**WhatsApp:**\n"
            f"• Conversations: {stats['conversations']}\n"
            f"• Messages (last 7 days): {stats['messages_7d']}\n"
            f"• Templates: {stats['templates']}\n\n"
            f"**Campaigns:**\n"
            f"• Total: {stats['campaigns_total']}\n"
            f"• Active: {stats['campaigns_active']}\n\n"
            f"**Orders & Revenue:**\n"
            f"• Orders: {stats['orders_total']} ({stats['orders_paid']} paid)\n"
            f"• Revenue: {revenue_str}"
        )

        return {"status": "success", "message": msg, "data": stats}
