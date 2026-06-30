"""Models for WhatsApp Forms (FlowOS) — bookings and scheduling."""

from datetime import datetime, timezone
from typing import Any, Dict

from models import db


class WhatsAppFormBusinessHours(db.Model):
    __tablename__ = "whatsapp_form_business_hours"
    __table_args__ = {"extend_existing": True}

    id = db.Column(db.Integer, primary_key=True)
    account_id = db.Column(db.Integer, db.ForeignKey("whatsapp_accounts.id"), nullable=False, index=True)
    day_of_week = db.Column(db.Integer, nullable=False)  # 0=Monday
    day_name = db.Column(db.String(16), nullable=False)
    open_time = db.Column(db.String(8), nullable=False, default="09:00")
    close_time = db.Column(db.String(8), nullable=False, default="17:00")
    slot_duration_minutes = db.Column(db.Integer, nullable=False, default=30)
    max_bookings_per_slot = db.Column(db.Integer, nullable=False, default=1)
    max_bookings_per_day = db.Column(db.Integer, nullable=True)
    # IANA timezone the open/close/slot times are expressed in. Used to fire
    # appointment reminders at the correct local wall-clock time.
    timezone = db.Column(db.String(64), nullable=False, default="Asia/Kolkata")
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = db.Column(
        db.DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "account_id": self.account_id,
            "day_of_week": self.day_of_week,
            "day_name": self.day_name,
            "open_time": self.open_time,
            "close_time": self.close_time,
            "slot_duration_minutes": self.slot_duration_minutes,
            "max_bookings_per_slot": self.max_bookings_per_slot,
            "max_bookings_per_day": self.max_bookings_per_day,
            "timezone": self.timezone,
            "is_active": self.is_active,
        }


class WhatsAppFormBlockedDate(db.Model):
    __tablename__ = "whatsapp_form_blocked_dates"
    __table_args__ = {"extend_existing": True}

    id = db.Column(db.Integer, primary_key=True)
    account_id = db.Column(db.Integer, db.ForeignKey("whatsapp_accounts.id"), nullable=False, index=True)
    blocked_date = db.Column(db.String(10), nullable=False)  # YYYY-MM-DD
    reason = db.Column(db.String(255), nullable=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "account_id": self.account_id,
            "blocked_date": self.blocked_date,
            "reason": self.reason,
        }


class WhatsAppFormBooking(db.Model):
    __tablename__ = "whatsapp_form_bookings"
    __table_args__ = {"extend_existing": True}

    id = db.Column(db.Integer, primary_key=True)
    account_id = db.Column(db.Integer, db.ForeignKey("whatsapp_accounts.id"), nullable=False, index=True)
    flow_id = db.Column(db.Integer, db.ForeignKey("whatsapp_flows.id"), nullable=True)
    conversation_id = db.Column(db.Integer, db.ForeignKey("whatsapp_conversations.id"), nullable=True)
    wa_id = db.Column(db.String(32), nullable=False, index=True)
    customer_name = db.Column(db.String(128), nullable=True)
    booking_date = db.Column(db.String(10), nullable=False)  # YYYY-MM-DD
    booking_time = db.Column(db.String(8), nullable=False)   # HH:MM
    service_type = db.Column(db.String(128), nullable=True)
    status = db.Column(db.String(16), nullable=False, default="confirmed")  # confirmed, cancelled, completed
    notes = db.Column(db.Text, nullable=True)
    # Appointment reminder: when (UTC) to fire, the APScheduler job id, and whether sent.
    remind_at = db.Column(db.DateTime, nullable=True)
    reminder_job_id = db.Column(db.String(64), nullable=True)
    reminded = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = db.Column(
        db.DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "account_id": self.account_id,
            "flow_id": self.flow_id,
            "conversation_id": self.conversation_id,
            "wa_id": self.wa_id,
            "customer_name": self.customer_name,
            "booking_date": self.booking_date,
            "booking_time": self.booking_time,
            "service_type": self.service_type,
            "status": self.status,
            "notes": self.notes,
            "remind_at": self.remind_at.isoformat() if self.remind_at else None,
            "reminded": self.reminded,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
