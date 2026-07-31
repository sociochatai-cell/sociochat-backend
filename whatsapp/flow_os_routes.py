"""WhatsApp Forms (FlowOS) API — submissions, analytics, sync, bookings."""

import json
import logging
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

import requests as http_requests
from flask import Blueprint, request, jsonify

from models import db
from .models import WhatsAppFlow, WhatsAppAccount, WhatsAppMessage, WhatsAppConversation
from .flow_os_models import WhatsAppFormBooking, WhatsAppFormBusinessHours, WhatsAppFormBlockedDate

logger = logging.getLogger(__name__)

flow_os_bp = Blueprint("flow_os", __name__, url_prefix="/api/whatsapp")
bookings_bp = Blueprint("bookings", __name__, url_prefix="/api/whatsapp/bookings")

DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def _parse_response_json(raw: Any) -> Dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {"value": parsed}
        except json.JSONDecodeError:
            return {"raw": raw}
    return {}


def _extract_submissions(account_id: int) -> List[Dict[str, Any]]:
    rows = (
        db.session.query(WhatsAppMessage, WhatsAppConversation)
        .join(WhatsAppConversation, WhatsAppMessage.conversation_id == WhatsAppConversation.id)
        .filter(
            WhatsAppConversation.account_id == account_id,
            WhatsAppMessage.type == "interactive",
        )
        .order_by(WhatsAppMessage.created_at.desc())
        .all()
    )

    submissions: List[Dict[str, Any]] = []
    for msg, conv in rows:
        content = msg.content or {}
        # content is a JSON column, so non-dict rows (e.g. a plain text body, or a
        # double-encoded JSON string) come back as a str — guard/parse before .get()
        # so the submissions list never 500s on such rows.
        if isinstance(content, str):
            try:
                content = json.loads(content)
            except (ValueError, TypeError):
                content = None
        if not isinstance(content, dict):
            continue
        if content.get("interactive_type") != "nfm_reply":
            continue
        response_json = _parse_response_json(content.get("response_json"))
        if not response_json:
            continue
        flow_id = content.get("flow_id")
        flow = None
        if flow_id:
            flow = WhatsAppFlow.query.filter_by(account_id=account_id, meta_flow_id=str(flow_id)).first()
            if not flow and str(flow_id).isdigit():
                flow = WhatsAppFlow.query.filter_by(account_id=account_id, id=int(flow_id)).first()

        # Derive a friendly form TYPE so the Submissions tab can group lead-capture /
        # feedback / booking / custom. Prefer the flow's Meta category; fall back to the
        # response shape (date+time → a booking).
        flow_category = (getattr(flow, "category", None) or "") if flow else ""
        _cat = flow_category.upper()
        if "BOOK" in _cat or "APPOINT" in _cat:
            form_type = "booking"
        elif "LEAD" in _cat:
            form_type = "lead"
        elif "SURVEY" in _cat or "FEEDBACK" in _cat:
            form_type = "feedback"
        elif any(k in response_json for k in ("booking_date", "preferred_date", "appointment_date")) \
                and any(k in response_json for k in ("preferred_time_slot", "booking_time", "time_slot", "appointment_time")):
            form_type = "booking"
        else:
            form_type = "custom"

        submissions.append({
            "id": msg.id,
            "flow_id": flow.id if flow else None,
            "wa_id": conv.user_phone,
            "conversation_id": conv.id,
            "response_json": response_json,
            "status": content.get("submission_status", "received"),
            "flow_category": flow_category or None,
            "form_type": form_type,
            # created_at is stored in UTC but the DateTime column is timezone-naive,
            # so stamp it as UTC here. Without the "+00:00" marker the browser reads
            # it as LOCAL time and shows every submission shifted by the UTC offset.
            "submitted_at": (
                (msg.created_at if msg.created_at.tzinfo else msg.created_at.replace(tzinfo=timezone.utc)).isoformat()
                if msg.created_at else None
            ),
            "flow_name": flow.name if flow else None,
        })
    return submissions


def _generate_slots(account_id: int, date_str: str) -> List[Dict[str, Any]]:
    blocked = WhatsAppFormBlockedDate.query.filter_by(account_id=account_id, blocked_date=date_str).first()
    if blocked:
        return []

    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        day_of_week = dt.weekday()
    except ValueError:
        return []

    hours = WhatsAppFormBusinessHours.query.filter_by(account_id=account_id, day_of_week=day_of_week, is_active=True).first()
    if not hours:
        return []

    try:
        open_h, open_m = map(int, hours.open_time.split(":"))
        close_h, close_m = map(int, hours.close_time.split(":"))
    except (ValueError, AttributeError):
        return []

    start = open_h * 60 + open_m
    end = close_h * 60 + close_m
    duration = max(5, hours.slot_duration_minutes or 30)
    max_per_slot = max(1, hours.max_bookings_per_slot or 1)

    existing = WhatsAppFormBooking.query.filter_by(account_id=account_id, booking_date=date_str).all()
    slot_counts: Dict[str, int] = {}
    for b in existing:
        if b.status != "cancelled":
            slot_counts[b.booking_time] = slot_counts.get(b.booking_time, 0) + 1

    slots = []
    slot_id = 1
    for minute in range(start, end, duration):
        h, m = divmod(minute, 60)
        slot_time = f"{h:02d}:{m:02d}"
        current = slot_counts.get(slot_time, 0)
        slots.append({
            "id": slot_id,
            "slot_time": slot_time,
            "max_capacity": max_per_slot,
            "current_bookings": current,
            "is_blocked": False,
            "is_full": current >= max_per_slot,
        })
        slot_id += 1
    return slots


@flow_os_bp.route("/flows/submissions", methods=["GET"])
def list_flow_submissions():
    account_id = request.args.get("account_id", type=int)
    page = request.args.get("page", 1, type=int)
    per_page = min(request.args.get("per_page", 20, type=int), 100)

    if not account_id:
        return jsonify({"success": False, "error": "account_id is required"}), 400

    all_subs = _extract_submissions(account_id)
    total = len(all_subs)
    start = (page - 1) * per_page
    end = start + per_page

    return jsonify({
        "success": True,
        "submissions": all_subs[start:end],
        "total": total,
        "page": page,
        "per_page": per_page,
    })


@flow_os_bp.route("/flows/analytics/summary", methods=["GET"])
def flow_analytics_summary():
    account_id = request.args.get("account_id", type=int)
    if not account_id:
        return jsonify({"success": False, "error": "account_id is required"}), 400

    flows = WhatsAppFlow.query.filter_by(account_id=account_id).all()
    submissions = _extract_submissions(account_id)
    today = datetime.now(timezone.utc).date().isoformat()
    today_subs = [s for s in submissions if (s.get("submitted_at") or "")[:10] == today]

    categories: Dict[str, int] = {}
    flow_stats = []
    subs_by_flow: Dict[int, int] = {}
    for s in submissions:
        fid = s.get("flow_id")
        if fid:
            subs_by_flow[fid] = subs_by_flow.get(fid, 0) + 1

    for flow in flows:
        cat = flow.category or "CUSTOM"
        count = subs_by_flow.get(flow.id, 0)
        categories[cat] = categories.get(cat, 0) + count
        flow_stats.append({
            "flow_id": flow.id,
            "name": flow.name,
            "category": cat,
            "status": flow.status,
            "submissions": count,
        })

    flow_stats.sort(key=lambda x: x["submissions"], reverse=True)

    return jsonify({
        "success": True,
        "total_flows": len(flows),
        "total_submissions": len(submissions),
        "today_submissions": len(today_subs),
        "categories": categories,
        "flows": flow_stats,
    })


@flow_os_bp.route("/flows/sync", methods=["POST"])
def sync_flows_from_meta():
    account_id = request.args.get("account_id", type=int)
    if not account_id:
        return jsonify({"success": False, "error": "account_id is required"}), 400

    account = WhatsAppAccount.query.get(account_id)
    if not account:
        return jsonify({"success": False, "error": "Account not found"}), 404

    access_token = account.get_access_token()
    if not access_token or not account.waba_id:
        return jsonify({"success": False, "error": "Account not connected to Meta"}), 400

    try:
        resp = http_requests.get(
            f"https://graph.facebook.com/v18.0/{account.waba_id}/flows",
            headers={"Authorization": f"Bearer {access_token}"},
            params={"fields": "id,name,status,categories"},
            timeout=30,
        )
        if resp.status_code != 200:
            err = resp.json().get("error", {}).get("message", "Meta API error")
            return jsonify({"success": False, "error": err}), 400

        meta_flows = resp.json().get("data", [])
        updated = 0
        for mf in meta_flows:
            meta_id = str(mf.get("id", ""))
            status = str(mf.get("status", "")).upper()
            local = WhatsAppFlow.query.filter_by(account_id=account_id, meta_flow_id=meta_id).first()
            if local and local.status != status:
                if status in ("DRAFT", "PUBLISHED", "DEPRECATED"):
                    local.status = status
                    updated += 1

        db.session.commit()
        return jsonify({"success": True, "updated": updated, "meta_count": len(meta_flows)})
    except Exception as exc:
        logger.exception("Flow sync failed")
        return jsonify({"success": False, "error": str(exc)}), 500


@bookings_bp.route("", methods=["GET"])
def list_bookings():
    account_id = request.args.get("account_id", type=int)
    date = request.args.get("date")
    if not account_id:
        return jsonify({"success": False, "error": "account_id is required"}), 400

    query = WhatsAppFormBooking.query.filter_by(account_id=account_id)
    if date:
        # Show the selected date's bookings PLUS any pending booking (regardless of
        # date) — pending ones need review/confirm and must not be hidden by the filter.
        from sqlalchemy import or_
        query = query.filter(or_(
            WhatsAppFormBooking.booking_date == date,
            WhatsAppFormBooking.status == "pending",
        ))
    bookings = query.order_by(
        WhatsAppFormBooking.booking_date.asc(), WhatsAppFormBooking.booking_time.asc()
    ).all()
    return jsonify({"success": True, "bookings": [b.to_dict() for b in bookings]})


@bookings_bp.route("/analytics", methods=["GET"])
def booking_analytics():
    account_id = request.args.get("account_id", type=int)
    days = request.args.get("days", 30, type=int)
    if not account_id:
        return jsonify({"success": False, "error": "account_id is required"}), 400

    since = datetime.now(timezone.utc) - timedelta(days=days)
    today = datetime.now(timezone.utc).date().isoformat()
    all_bookings = WhatsAppFormBooking.query.filter(
        WhatsAppFormBooking.account_id == account_id,
        WhatsAppFormBooking.created_at >= since,
    ).all()

    total = len(all_bookings)
    confirmed = sum(1 for b in all_bookings if b.status == "confirmed")
    pending = sum(1 for b in all_bookings if b.status == "pending")
    cancelled = sum(1 for b in all_bookings if b.status == "cancelled")
    today_count = sum(1 for b in all_bookings if b.booking_date == today and b.status != "cancelled")
    cancel_rate = round((cancelled / total * 100) if total else 0, 1)

    return jsonify({
        "success": True,
        "total_bookings": total,
        "confirmed": confirmed,
        "pending": pending,
        "cancelled": cancelled,
        "today_bookings": today_count,
        "cancellation_rate": cancel_rate,
    })


@bookings_bp.route("/slots/capacity", methods=["GET"])
def slot_capacity():
    account_id = request.args.get("account_id", type=int)
    date = request.args.get("date")
    if not account_id or not date:
        return jsonify({"success": False, "error": "account_id and date are required"}), 400
    return jsonify({"success": True, "slots": _generate_slots(account_id, date)})


@bookings_bp.route("/slot-times", methods=["GET"])
def slot_times():
    """Distinct time-slot LABELS from the configured active business hours — used to
    populate a WhatsApp form's time dropdown at build time (a published static Meta flow
    can't vary by weekday, so we take the union across all active days)."""
    account_id = request.args.get("account_id", type=int)
    if not account_id:
        return jsonify({"success": False, "error": "account_id is required"}), 400
    rows = WhatsAppFormBusinessHours.query.filter_by(account_id=account_id, is_active=True).all()
    mins_set = set()
    for h in rows:
        try:
            oh, om = map(int, (h.open_time or "09:00").split(":"))
            ch, cm = map(int, (h.close_time or "17:00").split(":"))
        except (ValueError, AttributeError):
            continue
        step = max(5, h.slot_duration_minutes or 30)
        m, end = oh * 60 + om, ch * 60 + cm
        while m < end:
            mins_set.add(m)
            m += step

    def _label(mins: int) -> str:
        h24, mm = mins // 60, mins % 60
        period = "AM" if h24 < 12 else "PM"
        h12 = h24 % 12 or 12
        return f"{h12}:{mm:02d} {period}"

    return jsonify({"success": True, "times": [_label(m) for m in sorted(mins_set)]})


@bookings_bp.route("/<int:booking_id>/cancel", methods=["PUT"])
def cancel_booking(booking_id: int):
    booking = WhatsAppFormBooking.query.get(booking_id)
    if not booking:
        return jsonify({"success": False, "error": "Booking not found"}), 404
    data = request.get_json(silent=True) or {}
    booking.status = "cancelled"
    booking.notes = data.get("reason") or booking.notes
    db.session.commit()
    return jsonify({"success": True, "booking": booking.to_dict()})


@bookings_bp.route("/<int:booking_id>/confirm", methods=["PUT"])
def confirm_booking(booking_id: int):
    """Confirm a PENDING booking (optionally correcting date/time) and schedule its
    1-hour-before reminder. Body: { "booking_date": "YYYY-MM-DD", "booking_time": "HH:MM" }
    (both optional — omit to keep what the customer submitted)."""
    booking = WhatsAppFormBooking.query.get(booking_id)
    if not booking:
        return jsonify({"success": False, "error": "Booking not found"}), 404
    data = request.get_json(silent=True) or {}
    new_date = _normalize_booking_date(data.get("booking_date")) if data.get("booking_date") else None
    if new_date:
        booking.booking_date = str(new_date)[:10]
    if data.get("booking_time"):
        booking.booking_time = str(data["booking_time"])[:8]
    booking.status = "confirmed"
    # (Re)schedule the reminder now that the slot is final. Isolated so a scheduling
    # hiccup never blocks confirming the booking itself.
    try:
        db.session.flush()
        from .booking_reminders import schedule_booking_reminder
        schedule_booking_reminder(booking)
    except Exception as reminder_err:
        logger.warning("Could not schedule reminder for booking %s: %s", booking_id, reminder_err)
    db.session.commit()
    return jsonify({"success": True, "booking": booking.to_dict()})


@bookings_bp.route("/<int:booking_id>/complete", methods=["PUT"])
def complete_booking(booking_id: int):
    booking = WhatsAppFormBooking.query.get(booking_id)
    if not booking:
        return jsonify({"success": False, "error": "Booking not found"}), 404
    booking.status = "completed"
    db.session.commit()
    return jsonify({"success": True, "booking": booking.to_dict()})


@bookings_bp.route("/business-hours", methods=["GET"])
def get_business_hours():
    account_id = request.args.get("account_id", type=int)
    if not account_id:
        return jsonify({"success": False, "error": "account_id is required"}), 400
    hours = WhatsAppFormBusinessHours.query.filter_by(account_id=account_id).order_by(WhatsAppFormBusinessHours.day_of_week).all()
    return jsonify({"success": True, "hours": [h.to_dict() for h in hours]})


@bookings_bp.route("/business-hours", methods=["POST"])
def save_business_hours():
    data = request.get_json() or {}
    account_id = data.get("account_id")
    hours_data = data.get("hours", [])
    if not account_id:
        return jsonify({"success": False, "error": "account_id is required"}), 400

    WhatsAppFormBusinessHours.query.filter_by(account_id=account_id).delete()
    saved = []
    for h in hours_data:
        row = WhatsAppFormBusinessHours(
            account_id=account_id,
            day_of_week=h.get("day_of_week", 0),
            day_name=h.get("day_name", DAY_NAMES[h.get("day_of_week", 0) % 7]),
            open_time=h.get("open_time", "09:00"),
            close_time=h.get("close_time", "17:00"),
            slot_duration_minutes=h.get("slot_duration_minutes", 30),
            max_bookings_per_slot=h.get("max_bookings_per_slot", 1),
            max_bookings_per_day=h.get("max_bookings_per_day"),
            is_active=bool(h.get("is_active", True)),
        )
        db.session.add(row)
        saved.append(row)
    db.session.commit()
    return jsonify({"success": True, "hours": [r.to_dict() for r in saved]})


@bookings_bp.route("/blocked-dates", methods=["GET"])
def get_blocked_dates():
    account_id = request.args.get("account_id", type=int)
    if not account_id:
        return jsonify({"success": False, "error": "account_id is required"}), 400
    dates = WhatsAppFormBlockedDate.query.filter_by(account_id=account_id).order_by(WhatsAppFormBlockedDate.blocked_date).all()
    return jsonify({"success": True, "dates": [d.to_dict() for d in dates]})


@bookings_bp.route("/blocked-dates", methods=["POST"])
def add_blocked_date():
    data = request.get_json() or {}
    account_id = data.get("account_id")
    date = data.get("date")
    if not account_id or not date:
        return jsonify({"success": False, "error": "account_id and date are required"}), 400
    existing = WhatsAppFormBlockedDate.query.filter_by(account_id=account_id, blocked_date=date).first()
    if existing:
        return jsonify({"success": True, "date": existing.to_dict()})
    row = WhatsAppFormBlockedDate(account_id=account_id, blocked_date=date, reason=data.get("reason"))
    db.session.add(row)
    db.session.commit()
    return jsonify({"success": True, "date": row.to_dict()})


@bookings_bp.route("/blocked-dates/<int:date_id>", methods=["DELETE"])
def remove_blocked_date(date_id: int):
    row = WhatsAppFormBlockedDate.query.get(date_id)
    if not row:
        return jsonify({"success": False, "error": "Not found"}), 404
    db.session.delete(row)
    db.session.commit()
    return jsonify({"success": True})


def _normalize_booking_date(val: Any) -> str:
    """Return a YYYY-MM-DD string.

    The WhatsApp Flows DatePicker submits the date as epoch milliseconds
    (e.g. "1751328000000"), not "YYYY-MM-DD". Convert that; otherwise pass
    through the first 10 chars of an already-formatted date string.
    """
    s = str(val).strip()
    if s.isdigit() and len(s) >= 12:  # epoch milliseconds
        try:
            return datetime.fromtimestamp(int(s) / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
        except (ValueError, OverflowError, OSError):
            return ""
    return s[:10]


def maybe_create_booking_from_submission(
    account_id: int,
    conversation_id: int,
    wa_id: str,
    response_json: Dict[str, Any],
    flow_id: Optional[str] = None,
) -> None:
    """Create a booking row when submission contains date/time fields."""
    date_val = (
        response_json.get("booking_date")
        or response_json.get("date")
        or response_json.get("appointment_date")
        or response_json.get("preferred_date")
        or response_json.get("selected_date")
        or response_json.get("appointment_day")
    )
    time_val = (
        response_json.get("booking_time")
        or response_json.get("time")
        or response_json.get("appointment_time")
        or response_json.get("slot")
        or response_json.get("preferred_time")
        or response_json.get("time_slot")
        or response_json.get("appointment_slot")
        or response_json.get("selected_time")
        or response_json.get("preferred_time_slot")
    )
    if not date_val or not time_val:
        return

    date_val = _normalize_booking_date(date_val)
    if not date_val:
        return

    name = (
        response_json.get("customer_name")
        or response_json.get("name")
        or response_json.get("full_name")
        or response_json.get("your_name")
    )
    service = response_json.get("service_type") or response_json.get("service")

    local_flow_id = None
    if flow_id:
        flow = WhatsAppFlow.query.filter_by(account_id=account_id, meta_flow_id=str(flow_id)).first()
        local_flow_id = flow.id if flow else None

    # New submissions land as PENDING — the operator reviews/corrects the slot in the
    # Bookings tab and confirms it. The reminder is scheduled only on confirm (see
    # confirm_booking), so an unconfirmed request never pings the customer.
    booking = WhatsAppFormBooking(
        account_id=account_id,
        flow_id=local_flow_id,
        conversation_id=conversation_id,
        wa_id=wa_id,
        customer_name=str(name) if name else None,
        booking_date=str(date_val)[:10],
        booking_time=str(time_val)[:8],
        service_type=str(service) if service else None,
        status="pending",
    )
    db.session.add(booking)
    # flush() assigns booking.id without committing; the caller commits the txn.
    try:
        db.session.flush()
    except Exception:
        pass
