"""
Messaging Actions — send text or template to a phone number.
"""

import logging
from typing import Dict, Any, List

from agent_backend.action_registry import BaseAction, action_registry

logger = logging.getLogger(__name__)


@action_registry.register(domain="messaging", action="send_text")
class SendTextAction(BaseAction):
    name = "send_text_message"
    description = "Send a text message to a phone number"
    required_params = ["to", "text"]
    optional_params: List[str] = []

    def get_missing_params_prompt(self, missing: List[str]) -> str:
        prompts = {
            "to": "Which phone number? (Include country code, e.g. 919876543210)",
            "text": "What message should I send?",
        }
        first = missing[0]
        return prompts.get(first, f"Please provide **{first}**.")

    def execute(self, params: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
        workspace_id = context.get("workspace_id")
        to = params["to"].strip()
        text = params["text"]

        # Require confirmation
        session = context.get("session")
        if session and not getattr(session, "_confirmed", False):
            msg = f'📨 Ready to send message to **{to}**:\n"{text}"\n\nProceed?'
            return {"status": "confirmation_required", "message": msg}

        try:
            from whatsapp.services import WhatsAppService
            account_id = context.get("account_id")
            svc = WhatsAppService(workspace_id=workspace_id, account_id=account_id)
            result = svc.send_text(to, text)
            if result.get("success") or result.get("messages"):
                return {
                    "status": "success",
                    "message": f"✅ Message sent to **{to}**!",
                    "data": result,
                }
            return {"status": "error", "message": f"Failed to send: {result.get('error', 'Unknown error')}"}
        except Exception as exc:
            return {"status": "error", "message": f"Send failed: {exc}"}


# messaging/send_template removed — use template/send instead (identical functionality, avoids duplicate)
