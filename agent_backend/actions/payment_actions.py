"""
Payment Actions — create payment links and send to contacts.
"""

import logging
from typing import Dict, Any, List

from agent_backend.action_registry import BaseAction, action_registry

logger = logging.getLogger(__name__)


@action_registry.register(domain="payment", action="send_link")
class SendPaymentLinkAction(BaseAction):
    name = "send_payment_link"
    description = "Create a payment link and send it to a contact via WhatsApp"
    required_params = ["phone", "amount", "product"]
    optional_params = ["customer_name", "customer_email"]

    def get_missing_params_prompt(self, missing: List[str]) -> str:
        prompts = {
            "phone": "Which phone number should I send the payment link to? (with country code, e.g. 919876543210)",
            "amount": "How much should the payment be? (in ₹)",
            "product": "What is the payment for? (product/service description)",
        }
        first = missing[0]
        return prompts.get(first, f"Please provide **{first}**.")

    def _clean_phone(self, raw: str) -> str:
        import re
        digits = re.sub(r'\D', '', str(raw))
        if len(digits) == 10:
            digits = "91" + digits
        return digits

    def _clean_amount(self, raw) -> float:
        import re
        digits = re.findall(r'[\d.]+', str(raw))
        if not digits:
            raise ValueError(f"Could not extract amount from: {raw}")
        return float(digits[0])

    def execute(self, params: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
        from models import db

        phone = self._clean_phone(params["phone"])
        try:
            amount = self._clean_amount(params["amount"])
        except ValueError as e:
            return {"status": "error", "message": str(e)}

        session = context.get("session")
        if not (session and getattr(session, "_confirmed", False)):
            return {
                "status": "confirmation_required",
                "message": (
                    f"💳 Ready to send payment link:\n"
                    f"• To: {phone}\n"
                    f"• Amount: ₹{amount:,.0f}\n"
                    f"• For: {params['product']}\n"
                    f"Proceed?"
                ),
            }

        workspace_id = context.get("workspace_id")
        account_id = context.get("account_id")

        if not account_id:
            return {"status": "error", "message": "No WhatsApp account connected."}

        try:
            from whatsapp.commerce_pay.models import WorkspacePaymentConfig
            config = WorkspacePaymentConfig.query.filter_by(
                workspace_id=int(workspace_id), is_active=True
            ).first()
            if not config or not config.is_connected:
                return {
                    "status": "error",
                    "message": "Payment not configured. Set up PayU credentials in **Settings → Payments** first.",
                    "navigate_to": "/dashboard/settings/payments",
                }
        except Exception:
            return {"status": "error", "message": "Payment system not available."}

        try:
            from whatsapp.commerce_pay.orders import create_order_and_send
            import os
            host_url = os.environ.get("HOST_URL", "https://sc.sociovia.com")

            order, link, sent = create_order_and_send(
                workspace_id=int(workspace_id),
                cfg_row=config,
                phone=phone,
                amount=amount,
                productinfo=params["product"][:160],
                customer_name=params.get("customer_name", ""),
                customer_email=params.get("customer_email", ""),
                origin="manual",
                host_url=host_url,
            )

            if sent:
                return {
                    "status": "success",
                    "message": (
                        f"✅ Payment link sent!\n"
                        f"• To: {params['phone']}\n"
                        f"• Amount: ₹{params['amount']}\n"
                        f"• Product: {params['product']}\n"
                        f"• Transaction ID: {order.txnid}\n"
                        f"• Status: Pending payment"
                    ),
                    "data": {"order_id": order.id, "txnid": order.txnid, "link": link},
                }
            else:
                return {
                    "status": "success",
                    "message": (
                        f"⚠️ Payment link created but message delivery failed.\n"
                        f"• Link: {link}\n"
                        f"• Transaction ID: {order.txnid}\n"
                        f"You can share the link manually."
                    ),
                    "data": {"order_id": order.id, "txnid": order.txnid, "link": link},
                }
        except Exception as exc:
            logger.warning("Payment link creation failed: %s", exc)
            return {"status": "error", "message": f"Could not create payment link: {exc}"}


@action_registry.register(domain="payment", action="status")
class PaymentStatusAction(BaseAction):
    name = "payment_status"
    description = "Check payment status by transaction ID or order ID"
    required_params = ["order_id"]

    def get_missing_params_prompt(self, missing: List[str]) -> str:
        return "Which order? Provide the order ID or transaction ID."

    def execute(self, params: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
        from models import db
        from sqlalchemy import text

        workspace_id = context.get("workspace_id")
        oid = str(params["order_id"])

        try:
            row = db.session.execute(text(
                "SELECT id, txnid, customer_name, customer_phone, amount, status, "
                "productinfo, created_at, paid_at "
                "FROM commerce_orders WHERE workspace_id = :wid AND (id::text = :oid OR txnid = :oid) "
                "LIMIT 1"
            ), {"wid": int(workspace_id), "oid": oid}).fetchone()
        except Exception as exc:
            return {"status": "error", "message": f"Could not check payment: {exc}"}

        if not row:
            return {"status": "error", "message": "Order not found."}

        emoji = {"paid": "✅", "pending": "⏳", "failed": "❌"}.get(row[5], "⚪")
        msg = (
            f"{emoji} **Payment #{row[0]}**\n"
            f"• Transaction: {row[1]}\n"
            f"• Customer: {row[2] or 'N/A'} ({row[3] or 'N/A'})\n"
            f"• Amount: ₹{float(row[4] or 0):,.0f}\n"
            f"• Status: **{row[5]}**\n"
            f"• Product: {row[6] or 'N/A'}\n"
            f"• Created: {row[7]}\n"
            f"• Paid: {row[8] or 'Not yet'}"
        )
        return {"status": "success", "message": msg}
