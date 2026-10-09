"""
Booking Actions — list and manage WhatsApp form bookings.
"""

import logging
from typing import Dict, Any, List

from agent_backend.action_registry import BaseAction, action_registry

logger = logging.getLogger(__name__)


@action_registry.register(domain="booking", action="list")
class ListBookingsAction(BaseAction):
    name = "list_bookings"
    description = "List upcoming bookings/appointments"
    required_params: List[str] = []
    optional_params = ["status"]

    def execute(self, params: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
        from whatsapp.flow_os_models import WhatsAppFormBooking
        account_id = context.get("account_id")

        if not account_id:
            return {"status": "error", "message": "No WhatsApp account connected."}

        query = WhatsAppFormBooking.query.filter_by(account_id=account_id)
        status_filter = params.get("status")
        if status_filter:
            query = query.filter(WhatsAppFormBooking.status == status_filter.lower())

        bookings = query.order_by(WhatsAppFormBooking.booking_date.desc()).limit(20).all()

        if not bookings:
            return {"status": "success", "message": "No bookings found.", "data": []}

        items = []
        lines = []
        for i, b in enumerate(bookings, 1):
            item = b.to_dict()
            items.append(item)
            emoji = {"confirmed": "✅", "cancelled": "❌", "completed": "🏁"}.get(b.status, "📅")
            name = b.customer_name or b.wa_id
            lines.append(f"{i}. {emoji} **{name}** — {b.booking_date} at {b.booking_time} ({b.status})")

        msg = f"**Bookings ({len(items)}):**\n" + "\n".join(lines)
        return {"status": "success", "message": msg, "data": items, "ui_action": "show_list"}


@action_registry.register(domain="booking", action="cancel")
class CancelBookingAction(BaseAction):
    name = "cancel_booking"
    description = "Cancel a booking/appointment"
    required_params = ["booking_id"]

    def get_missing_params_prompt(self, missing: List[str]) -> str:
        return "Which booking would you like to cancel? Provide the booking ID."

    def execute(self, params: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
        from whatsapp.flow_os_models import WhatsAppFormBooking
        from models import db

        account_id = context.get("account_id")
        booking = WhatsAppFormBooking.query.filter_by(
            id=params["booking_id"], account_id=account_id
        ).first()
        if not booking:
            return {"status": "error", "message": "Booking not found."}

        session = context.get("session")
        if not (session and getattr(session, "_confirmed", False)):
            name = booking.customer_name or booking.wa_id
            return {
                "status": "confirmation_required",
                "message": f"Cancel booking for **{name}** on {booking.booking_date} at {booking.booking_time}?",
            }

        booking.status = "cancelled"
        db.session.commit()
        return {"status": "success", "message": f"❌ Booking #{booking.id} cancelled."}
