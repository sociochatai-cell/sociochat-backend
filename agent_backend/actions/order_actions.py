"""
Order Actions — list and view commerce orders.
"""

import logging
from typing import Dict, Any, List

from agent_backend.action_registry import BaseAction, action_registry

logger = logging.getLogger(__name__)


@action_registry.register(domain="order", action="list")
class ListOrdersAction(BaseAction):
    name = "list_orders"
    description = "List recent orders/payments"
    required_params: List[str] = []
    optional_params = ["status"]

    def execute(self, params: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
        from models import db
        from sqlalchemy import text

        workspace_id = context.get("workspace_id")

        try:
            sql = ("SELECT id, txnid, customer_name, customer_phone, amount, currency, "
                   "status, productinfo, created_at "
                   "FROM commerce_orders WHERE workspace_id = :wid ")
            bind = {"wid": int(workspace_id)}

            status_filter = params.get("status")
            if status_filter:
                sql += "AND status = :status "
                bind["status"] = status_filter.lower()

            sql += "ORDER BY created_at DESC LIMIT 20"
            rows = db.session.execute(text(sql), bind).fetchall()
        except Exception as exc:
            logger.warning("Order list failed: %s", exc)
            return {"status": "error", "message": f"Could not load orders: {exc}"}

        if not rows:
            return {"status": "success", "message": "No orders found.", "data": []}

        items = []
        lines = []
        for i, r in enumerate(rows, 1):
            item = {
                "id": r[0], "txnid": r[1], "customer": r[2] or r[3],
                "amount": float(r[4] or 0), "currency": r[5] or "INR",
                "status": r[6], "product": r[7],
            }
            items.append(item)
            emoji = {"success": "✅", "pending": "⏳", "failed": "❌"}.get(r[6], "⚪")
            name = r[2] or r[3] or "Unknown"
            lines.append(f"{i}. {emoji} **{name}** — ₹{item['amount']:,.0f} ({r[6]})")

        msg = f"**Orders ({len(items)}):**\n" + "\n".join(lines)
        return {"status": "success", "message": msg, "data": items, "ui_action": "show_list"}


@action_registry.register(domain="order", action="get_info")
class GetOrderInfoAction(BaseAction):
    name = "get_order_info"
    description = "Get details of a specific order"
    required_params = ["order_id"]

    def get_missing_params_prompt(self, missing: List[str]) -> str:
        return "Which order? Provide the order ID or transaction ID."

    def execute(self, params: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
        from models import db
        from sqlalchemy import text

        workspace_id = context.get("workspace_id")

        try:
            sql = ("SELECT id, txnid, customer_name, customer_phone, customer_email, "
                   "amount, currency, status, productinfo, mode, created_at, paid_at "
                   "FROM commerce_orders WHERE workspace_id = :wid AND (id::text = :oid OR txnid = :oid) "
                   "LIMIT 1")
            row = db.session.execute(text(sql), {"wid": int(workspace_id), "oid": str(params["order_id"])}).fetchone()
        except Exception as exc:
            logger.warning("Order lookup failed: %s", exc)
            return {"status": "error", "message": f"Could not load order: {exc}"}

        if not row:
            return {"status": "error", "message": "Order not found."}

        msg = (
            f"🧾 **Order #{row[0]}**\n"
            f"• Transaction: {row[1] or 'N/A'}\n"
            f"• Customer: {row[2] or 'N/A'} ({row[3] or 'no phone'})\n"
            f"• Email: {row[4] or 'N/A'}\n"
            f"• Amount: ₹{float(row[5] or 0):,.0f} {row[6] or 'INR'}\n"
            f"• Status: {row[7]}\n"
            f"• Product: {row[8] or 'N/A'}\n"
            f"• Payment mode: {row[9] or 'N/A'}\n"
            f"• Created: {row[10]}\n"
            f"• Paid: {row[11] or 'Not paid'}"
        )
        return {"status": "success", "message": msg, "ui_action": "show_card"}
