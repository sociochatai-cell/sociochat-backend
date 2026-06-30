"""Appointment reminders for WhatsApp Form bookings.

WhatsApp Flows have no time picker, so the booking time is collected via a
slot Dropdown and stored on the booking as `booking_time`. This module turns
that stored date+time into an exact-moment reminder using the existing
APScheduler (`whatsapp.scheduler`):

    booking_date + booking_time  (in the business timezone)
        -> minus a lead time (default 60 min)
        -> converted to UTC
        -> APScheduler 'date' job that calls send_booking_reminder()

The schedule is persisted in the `whatsapp_jobs` table, so reminders survive
a backend restart. Everything here is idempotent.
"""

import logging
import os
from datetime import datetime, timedelta, timezone

from models import db
from .flow_os_models import WhatsAppFormBooking, WhatsAppFormBusinessHours

logger = logging.getLogger(__name__)

# Timezone the booking_date/booking_time are expressed in, when the account's
# business hours don't specify one. Override per environment if needed.
DEFAULT_TZ = os.getenv("BOOKING_TIMEZONE", "Asia/Kolkata")
# How long before the appointment to send the reminder.
DEFAULT_LEAD_MINUTES = int(os.getenv("BOOKING_REMINDER_LEAD_MINUTES", "60"))


def _account_tz_name(account_id: int) -> str:
    """Timezone for an account, from its business hours, else the default."""
    row = WhatsAppFormBusinessHours.query.filter_by(account_id=account_id).first()
    return (getattr(row, "timezone", None) or DEFAULT_TZ) if row else DEFAULT_TZ


def compute_remind_at(booking, lead_minutes=None):
    """Return the UTC datetime to fire the reminder, or None if unschedulable.

    Combines booking_date (YYYY-MM-DD) + booking_time (HH:MM) in the account's
    timezone, then subtracts the lead time.
    """
    lead = DEFAULT_LEAD_MINUTES if lead_minutes is None else lead_minutes
    date_str = (booking.booking_date or "")[:10]
    time_str = (booking.booking_time or "")[:5]
    if not date_str or not time_str:
        return None
    try:
        naive = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
    except ValueError:
        logger.warning("Booking %s has unparseable date/time %r %r",
                       getattr(booking, "id", "?"), date_str, time_str)
        return None

    tz_name = _account_tz_name(booking.account_id)
    # pytz is a project dependency and bundles tz data (reliable on Windows).
    try:
        import pytz
        local_dt = pytz.timezone(tz_name).localize(naive)
    except Exception:
        logger.warning("Unknown timezone %r for booking %s; assuming UTC",
                       tz_name, getattr(booking, "id", "?"))
        local_dt = naive.replace(tzinfo=timezone.utc)

    appt_utc = local_dt.astimezone(timezone.utc)
    return appt_utc - timedelta(minutes=lead)


def schedule_booking_reminder(booking, lead_minutes=None):
    """Compute remind_at, stamp it on the booking, and register the APScheduler
    job. Does not commit — the caller owns the transaction. Idempotent: replaces
    any existing job for this booking. Returns the job id (or None)."""
    remind_at = compute_remind_at(booking, lead_minutes)
    booking.remind_at = remind_at
    booking.reminded = False
    booking.reminder_job_id = None

    if remind_at is None:
        return None

    now = datetime.now(timezone.utc)
    if remind_at <= now:
        # Appointment (minus lead) is already in the past — nothing to schedule.
        logger.info("Booking %s remind_at %s already passed; not scheduling",
                    booking.id, remind_at.isoformat())
        return None

    from .scheduler import add_booking_reminder_job
    job_id = add_booking_reminder_job(booking.id, remind_at)
    booking.reminder_job_id = job_id
    logger.info("Booking %s reminder scheduled for %s (job %s)",
                booking.id, remind_at.isoformat(), job_id)
    return job_id


def send_booking_reminder(booking_id: int):
    """Fired by APScheduler at remind_at. Sends the WhatsApp reminder. Idempotent."""
    booking = WhatsAppFormBooking.query.get(booking_id)
    if not booking:
        logger.warning("Booking %s not found; skipping reminder", booking_id)
        return
    if booking.reminded:
        logger.info("Booking %s already reminded; skipping", booking_id)
        return
    if booking.status == "cancelled":
        logger.info("Booking %s cancelled; skipping reminder", booking_id)
        return

    from .models import WhatsAppAccount
    from .services import WhatsAppService

    account = WhatsAppAccount.query.get(booking.account_id)
    if not account:
        logger.warning("Account %s for booking %s not found; skipping reminder",
                       booking.account_id, booking_id)
        return

    name = booking.customer_name or "there"
    what = booking.service_type or "your appointment"
    text = (
        f"Hi {name}! ⏰ Reminder: {what} on "
        f"{booking.booking_date} at {booking.booking_time}. See you soon!"
    )

    try:
        svc = WhatsAppService(
            phone_number_id=account.phone_number_id,
            workspace_id=account.workspace_id,
        )
        result = svc.send_text(to=booking.wa_id, text=text)
        if result.get("success"):
            booking.reminded = True
            db.session.commit()
            logger.info("Sent booking reminder for booking %s", booking_id)
        else:
            db.session.rollback()
            logger.warning("Booking reminder %s send failed: %s",
                           booking_id, result.get("error"))
    except Exception as e:
        db.session.rollback()
        logger.exception("Error sending booking reminder %s: %s", booking_id, e)
