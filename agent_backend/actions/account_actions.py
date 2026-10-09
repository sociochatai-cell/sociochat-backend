"""
Account Actions — query WhatsApp account info.
"""

import logging
from typing import Dict, Any, List

from agent_backend.action_registry import BaseAction, action_registry

logger = logging.getLogger(__name__)


@action_registry.register(domain="account", action="get_info")
class GetAccountInfoAction(BaseAction):
    name = "get_account_info"
    description = "Show account details (name, phone, WABA ID, quality, status)"
    required_params: List[str] = []
    optional_params = ["account_id"]

    def execute(self, params: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
        from whatsapp.models import WhatsAppAccount
        workspace_id = context.get("workspace_id")
        account_id = params.get("account_id") or context.get("account_id")

        if account_id:
            acct = WhatsAppAccount.query.filter_by(id=account_id, workspace_id=workspace_id).first()
        else:
            acct = WhatsAppAccount.query.filter_by(
                workspace_id=workspace_id, is_active=True
            ).first()

        if not acct:
            return {"status": "error", "message": "No WhatsApp account found for this workspace."}

        info = acct.to_dict()
        msg = (
            f"📱 **Account Info**\n"
            f"• **Name:** {info.get('verified_name', 'N/A')}\n"
            f"• **Phone:** {info.get('display_phone_number', 'N/A')}\n"
            f"• **WABA ID:** {info.get('waba_id', 'N/A')}\n"
            f"• **Quality:** {info.get('quality_score', 'N/A')}\n"
            f"• **Status:** {'Active' if info.get('is_active') else 'Inactive'}\n"
            f"• **Token Type:** {info.get('token_type', 'N/A')}"
        )
        return {"status": "success", "message": msg, "data": info, "ui_action": "show_card"}


@action_registry.register(domain="account", action="get_status")
class GetAccountStatusAction(BaseAction):
    name = "get_account_status"
    description = "Check connection status of WhatsApp account"
    required_params: List[str] = []
    optional_params = ["account_id"]

    def execute(self, params: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
        from whatsapp.models import WhatsAppAccount
        workspace_id = context.get("workspace_id")

        acct = WhatsAppAccount.query.filter_by(
            workspace_id=workspace_id, is_active=True
        ).first()

        if not acct:
            return {
                "status": "success",
                "message": "⚠️ No WhatsApp account connected to this workspace. Go to Settings to connect one.",
                "navigate_to": "/dashboard/settings",
            }

        status = "🟢 Connected & Active" if acct.is_active else "🔴 Disconnected"
        msg = (
            f"**Connection Status:** {status}\n"
            f"• **Phone:** {acct.display_phone_number or 'N/A'}\n"
            f"• **Quality:** {acct.quality_score or 'N/A'}"
        )
        return {"status": "success", "message": msg}


@action_registry.register(domain="account", action="list_accounts")
class ListAccountsAction(BaseAction):
    name = "list_accounts"
    description = "List all connected WhatsApp accounts"
    required_params: List[str] = []

    def execute(self, params: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
        from whatsapp.models import WhatsAppAccount
        workspace_id = context.get("workspace_id")

        accounts = WhatsAppAccount.query.filter_by(workspace_id=workspace_id).all()
        if not accounts:
            return {
                "status": "success",
                "message": "No WhatsApp accounts found. Connect one in Settings.",
                "navigate_to": "/dashboard/settings",
            }

        items = [a.to_dict() for a in accounts]
        lines = []
        for i, a in enumerate(items, 1):
            status = "🟢" if a.get("is_active") else "🔴"
            lines.append(f"{i}. {status} {a.get('verified_name', 'N/A')} — {a.get('display_phone_number', 'N/A')}")

        msg = f"**Connected Accounts ({len(items)}):**\n" + "\n".join(lines)
        return {"status": "success", "message": msg, "data": items, "ui_action": "show_list"}
