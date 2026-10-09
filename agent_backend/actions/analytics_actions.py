"""
Analytics Actions — summary, trends, export.
"""

import logging
from typing import Dict, Any, List

from agent_backend.action_registry import BaseAction, action_registry

logger = logging.getLogger(__name__)


def _message_query_base(db, account_id, cutoff):
    """Build base query joining messages through conversations for account scoping."""
    from whatsapp.models import WhatsAppMessage, WhatsAppConversation
    return db.session.query(WhatsAppMessage).join(
        WhatsAppConversation,
        WhatsAppMessage.conversation_id == WhatsAppConversation.id,
    ).filter(
        WhatsAppConversation.account_id == account_id,
        WhatsAppMessage.created_at >= cutoff,
    )


@action_registry.register(domain="analytics", action="summary")
class AnalyticsSummaryAction(BaseAction):
    name = "analytics_summary"
    description = "Show analytics summary (messages sent, delivered, read, etc.)"
    required_params: List[str] = []
    optional_params = ["days", "account_id"]

    def execute(self, params: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
        from whatsapp.models import WhatsAppMessage, WhatsAppConversation
        from sqlalchemy import func
        from datetime import datetime, timezone, timedelta
        from models import db

        account_id = context.get("account_id")
        days = int(params.get("days", 7))

        if not account_id:
            return {"status": "error", "message": "No WhatsApp account connected."}

        cutoff = datetime.now(timezone.utc) - timedelta(days=days)

        try:
            base = _message_query_base(db, account_id, cutoff)
            total = base.count()
            sent = base.filter(WhatsAppMessage.direction == "outgoing").count()
            received = base.filter(WhatsAppMessage.direction == "incoming").count()

            data = {
                "period_days": days,
                "total_messages": total,
                "sent": sent,
                "received": received,
            }

            msg = (
                f"📊 **Analytics Summary (Last {days} days)**\n"
                f"• Total Messages: **{total}**\n"
                f"• Sent: **{sent}**\n"
                f"• Received: **{received}**"
            )
            return {"status": "success", "message": msg, "data": data, "ui_action": "show_card"}

        except Exception as exc:
            logger.warning("Analytics query failed: %s", exc)
            return {
                "status": "error",
                "message": "⚠️ Could not load analytics. View them on the **Tracking** page.",
                "navigate_to": "/dashboard/tracking",
            }


@action_registry.register(domain="analytics", action="trends")
class AnalyticsTrendsAction(BaseAction):
    name = "analytics_trends"
    description = "Show message trends over time"
    required_params: List[str] = []
    optional_params = ["days"]

    def execute(self, params: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
        from whatsapp.models import WhatsAppMessage, WhatsAppConversation
        from sqlalchemy import func, cast, Date
        from datetime import datetime, timezone, timedelta
        from models import db

        account_id = context.get("account_id")
        if not account_id:
            return {"status": "error", "message": "No WhatsApp account connected."}

        days = int(params.get("days", 7))
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)

        try:
            rows = db.session.query(
                cast(WhatsAppMessage.created_at, Date).label("day"),
                func.count().label("total"),
            ).join(
                WhatsAppConversation,
                WhatsAppMessage.conversation_id == WhatsAppConversation.id,
            ).filter(
                WhatsAppConversation.account_id == account_id,
                WhatsAppMessage.created_at >= cutoff,
            ).group_by("day").order_by("day").all()

            if not rows:
                return {"status": "success", "message": f"No messages in the last {days} days.", "data": []}

            lines = []
            data = []
            for day, total in rows:
                d = {"date": str(day), "count": total}
                data.append(d)
                bar = "█" * min(total, 30)
                lines.append(f"  {day}  {bar} {total}")

            msg = f"📈 **Message Trends (Last {days} days)**\n" + "\n".join(lines)
            return {"status": "success", "message": msg, "data": data}
        except Exception as exc:
            logger.warning("Trends query failed: %s", exc)
            return {"status": "error", "message": f"⚠️ Could not load trends: {exc}"}


@action_registry.register(domain="analytics", action="export")
class AnalyticsExportAction(BaseAction):
    name = "analytics_export"
    description = "Export analytics data"
    required_params: List[str] = []
    optional_params = ["format", "date_range"]

    def execute(self, params: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
        from whatsapp.models import WhatsAppMessage, WhatsAppConversation
        from sqlalchemy import func, cast, Date
        from datetime import datetime, timezone, timedelta
        from models import db

        account_id = context.get("account_id")
        if not account_id:
            return {"status": "error", "message": "No WhatsApp account connected."}

        days = 30
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)

        try:
            rows = db.session.query(
                cast(WhatsAppMessage.created_at, Date).label("day"),
                WhatsAppMessage.direction,
                func.count().label("cnt"),
            ).join(
                WhatsAppConversation,
                WhatsAppMessage.conversation_id == WhatsAppConversation.id,
            ).filter(
                WhatsAppConversation.account_id == account_id,
                WhatsAppMessage.created_at >= cutoff,
            ).group_by("day", WhatsAppMessage.direction).order_by("day").all()

            if not rows:
                return {"status": "success", "message": "No data to export in the last 30 days."}

            data = {}
            for day, direction, cnt in rows:
                d = str(day)
                if d not in data:
                    data[d] = {"date": d, "sent": 0, "received": 0}
                if direction == "outgoing":
                    data[d]["sent"] = cnt
                else:
                    data[d]["received"] = cnt

            export = list(data.values())
            total_sent = sum(r["sent"] for r in export)
            total_recv = sum(r["received"] for r in export)

            msg = (
                f"📥 **Analytics Export (Last 30 days)**\n"
                f"• Total Sent: **{total_sent}**\n"
                f"• Total Received: **{total_recv}**\n"
                f"• Days with data: **{len(export)}**\n\n"
                f"For a full CSV export, use the **Export** button on the Tracking page."
            )
            return {"status": "success", "message": msg, "data": export, "navigate_to": "/dashboard/tracking"}
        except Exception as exc:
            logger.warning("Analytics export failed: %s", exc)
            return {"status": "error", "message": f"⚠️ Could not export analytics: {exc}"}
