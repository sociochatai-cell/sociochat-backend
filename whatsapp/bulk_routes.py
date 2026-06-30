
import logging
from datetime import datetime, timezone, timedelta
from flask import Blueprint, request, jsonify
from sqlalchemy import func, distinct, case

from shared_models import db, WhatsAppLinkTracking
from .models import WhatsAppAccount, WhatsAppMessage, WhatsAppConversation
from .capabilities import broadcast_capability_check
from .drip_models import WhatsAppDripCampaign, WhatsAppDripStep, WhatsAppDripEnrollment
from .flow_access import require_account_access
from .drip_engine import process_single_enrollment, trigger_campaign_now
from .utils import normalize_phone_robust
from .http_rate_limit import rate_limit
from notifications import notification_manager

logger = logging.getLogger(__name__)

bulk_bp = Blueprint("bulk", __name__, url_prefix="/api/whatsapp/bulk")


def _safe_dt_iso(value):
    if not value:
        return None
    try:
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat()
    except Exception:
        return None


def _as_aware_utc(value):
    if not value:
        return None
    try:
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    except Exception:
        return None


def _extract_name_from_variables(variables):
    if not isinstance(variables, dict):
        return None
    for key in ("name", "full_name", "customer_name", "first_name"):
        raw = variables.get(key)
        if raw:
            return str(raw).strip() or None
    return None


def _is_truthy_flag(value, default=True):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    return default


def _looks_like_url(value):
    text = str(value or "").strip().lower()
    return text.startswith("http://") or text.startswith("https://") or text.startswith("www.")


def _has_trackable_url_variable(variables):
    if not isinstance(variables, dict):
        return False
    ignored_keys = {
        "__enable_tracking_url",
        "__track_source",
        "__track_medium",
        "__track_campaign",
        "__track_content",
        "header_image_url",
        "header_video_url",
        "header_document_url",
    }
    for key, value in variables.items():
        if key in ignored_keys:
            continue
        if not _looks_like_url(value):
            continue
        key_text = str(key).strip().lower()
        if any(token in key_text for token in ("url", "link", "website", "track")):
            return True
    return False


def _reply_preview_from_content(content):
    """Best-effort short text preview of an inbound message's stored content."""
    if content is None:
        return None
    if isinstance(content, str):
        s = content
    elif isinstance(content, dict):
        s = (
            content.get("text")
            or content.get("body")
            or content.get("title")
            or content.get("caption")
            or ""
        )
        if not s:
            try:
                import json as _json
                s = _json.dumps(content, ensure_ascii=False)
            except Exception:
                s = str(content)
    else:
        s = str(content)
    s = (s or "").strip()
    return s[:280] if s else None


def _build_campaign_intelligence(campaign: WhatsAppDripCampaign):
    """Compute recipient-level status intelligence for a campaign."""
    enrollments = WhatsAppDripEnrollment.query.filter_by(campaign_id=campaign.id).all()

    recipients = {}
    enrollment_by_phone = {}
    campaign_tracking_enabled = True
    campaign_tracking_seen = False
    campaign_has_trackable_url = False

    for enrollment in enrollments:
        variables = enrollment.variables if isinstance(enrollment.variables, dict) else {}
        tracking_raw = variables.get("__enable_tracking_url")
        if tracking_raw is not None:
            campaign_tracking_seen = True
            campaign_tracking_enabled = campaign_tracking_enabled and _is_truthy_flag(tracking_raw, default=True)
        if _has_trackable_url_variable(variables):
            campaign_has_trackable_url = True

        normalized_phone = normalize_phone_robust(enrollment.phone_number) or (enrollment.phone_number or "")
        if not normalized_phone:
            continue

        recipient = recipients.setdefault(
            normalized_phone,
            {
                "phone_number": normalized_phone,
                "name": _extract_name_from_variables(variables),
                "enrollment_status": enrollment.status,
                "sent": bool(enrollment.sent),
                "delivered": bool(enrollment.delivered),
                "read": bool(enrollment.read),
                "failed": bool(enrollment.failed) or enrollment.status in ("failed", "blocked_missing_data"),
                "clicked": bool(enrollment.clicked),
                "replied": bool(enrollment.replied),
                "click_count": int(enrollment.click_count or 0),
                "last_status": "pending",
                "last_event_at": _as_aware_utc(
                    enrollment.read_at or enrollment.delivered_at or enrollment.sent_at or enrollment.clicked_at or enrollment.replied_at
                ),
                "error_message": enrollment.status_reason,
                "reply_preview": None,
                "reply_at": enrollment.replied_at,
                "message_id": enrollment.message_id,
                "tracking_id": enrollment.tracking_id,
            },
        )
        if recipient.get("name") is None:
            recipient["name"] = _extract_name_from_variables(variables)

        if recipient.get("read"):
            recipient["last_status"] = "read"
        elif recipient.get("delivered"):
            recipient["last_status"] = "delivered"
        elif recipient.get("sent"):
            recipient["last_status"] = "sent"
        elif recipient.get("failed"):
            recipient["last_status"] = "failed"

        enrollment_by_phone[normalized_phone] = enrollment

    status_rank = {
        "pending": 0,
        "sent": 1,
        "delivered": 2,
        "read": 3,
        "failed": 4,
    }

    msg_rows = (
        db.session.query(
            WhatsAppConversation.user_phone,
            WhatsAppMessage.status,
            WhatsAppMessage.created_at,
            WhatsAppMessage.sent_at,
            WhatsAppMessage.delivered_at,
            WhatsAppMessage.read_at,
            WhatsAppMessage.error_message,
        )
        .join(WhatsAppConversation, WhatsAppMessage.conversation_id == WhatsAppConversation.id)
        .filter(
            WhatsAppMessage.campaign_id == campaign.id,
            WhatsAppMessage.direction.in_(["outgoing", "echo"]),
        )
        .all()
    )

    for phone, msg_status, created_at, sent_at, delivered_at, read_at, error_message in msg_rows:
        normalized_phone = normalize_phone_robust(phone) or (phone or "")
        if not normalized_phone:
            continue

        recipient = recipients.setdefault(
            normalized_phone,
            {
                "phone_number": normalized_phone,
                "name": None,
                "enrollment_status": "unknown",
                "sent": False,
                "delivered": False,
                "read": False,
                "failed": False,
                "clicked": False,
                "replied": False,
                "click_count": 0,
                "last_status": "pending",
                "last_event_at": None,
                "error_message": None,
                "reply_preview": None,
                "reply_at": None,
            },
        )

        current_status = str(msg_status or "pending")
        recipient["sent"] = recipient["sent"] or current_status in {"sent", "delivered", "read", "failed"}
        recipient["delivered"] = recipient["delivered"] or current_status in {"delivered", "read"}
        recipient["read"] = recipient["read"] or current_status == "read"
        recipient["failed"] = recipient["failed"] or current_status == "failed"

        if current_status == "failed" and error_message:
            recipient["error_message"] = error_message

        previous_rank = status_rank.get(recipient.get("last_status") or "pending", 0)
        next_rank = status_rank.get(current_status, 0)
        if next_rank >= previous_rank:
            recipient["last_status"] = current_status

        event_at = _as_aware_utc(read_at or delivered_at or sent_at or created_at)
        last_event_at = _as_aware_utc(recipient.get("last_event_at"))
        if event_at and (last_event_at is None or event_at > last_event_at):
            recipient["last_event_at"] = event_at

    click_filters = [
        WhatsAppLinkTracking.workspace_id == str(campaign.workspace_id),
        WhatsAppLinkTracking.click_count > 0,
    ]
    if hasattr(WhatsAppLinkTracking, "campaign_name"):
        click_filters.append(WhatsAppLinkTracking.campaign_name == campaign.name)
    if hasattr(WhatsAppLinkTracking, "source"):
        click_filters.append(WhatsAppLinkTracking.source == "bulk")

    click_query = WhatsAppLinkTracking.query.filter(*click_filters)
    if campaign.account_id and hasattr(WhatsAppLinkTracking, "account_id"):
        click_query = click_query.filter(WhatsAppLinkTracking.account_id == campaign.account_id)

    try:
        clicks = click_query.all()
    except Exception as exc:
        # Legacy databases may miss newer whatsapp_link_tracking columns.
        # Keep intelligence endpoint available instead of failing hard.
        db.session.rollback()
        logger.warning("Skipping click-tracking enrichment for campaign %s: %s", campaign.id, exc)
        clicks = []

    for click in clicks:
        normalized_phone = normalize_phone_robust(click.phone_number) or (click.phone_number or "")
        if not normalized_phone:
            continue
        recipient = recipients.get(normalized_phone)
        if not recipient:
            continue
        recipient["clicked"] = bool(recipient.get("clicked") or int(click.click_count or 0) > 0)
        # Enrollment click_count is already updated by redirect tracking; avoid double counting by merging with max().
        recipient["click_count"] = max(int(recipient.get("click_count", 0) or 0), int(click.click_count or 0))
        click_last = _as_aware_utc(click.last_clicked_at)
        last_event_at = _as_aware_utc(recipient.get("last_event_at"))
        if click_last and (last_event_at is None or click_last > last_event_at):
            recipient["last_event_at"] = click_last

    # Inbound replies are now stamped with campaign_id on receipt (webhook.py); surface
    # the latest reply text per recipient so "replied"/reply_preview are authoritative
    # on reload instead of depending on the realtime patch. Ordered ascending so the
    # latest reply wins.
    try:
        reply_rows = (
            db.session.query(
                WhatsAppConversation.user_phone,
                WhatsAppMessage.content,
                WhatsAppMessage.created_at,
            )
            .join(WhatsAppConversation, WhatsAppMessage.conversation_id == WhatsAppConversation.id)
            .filter(
                WhatsAppMessage.campaign_id == campaign.id,
                WhatsAppMessage.direction == "incoming",
            )
            .order_by(WhatsAppMessage.created_at.asc())
            .all()
        )
    except Exception as exc:
        db.session.rollback()
        logger.warning("Skipping reply enrichment for campaign %s: %s", campaign.id, exc)
        reply_rows = []

    for phone, content, created_at in reply_rows:
        normalized_phone = normalize_phone_robust(phone) or (phone or "")
        if not normalized_phone:
            continue
        recipient = recipients.get(normalized_phone)
        if not recipient:
            continue
        preview = _reply_preview_from_content(content)
        if preview:
            recipient["reply_preview"] = preview
        recipient["replied"] = True
        event_at = _as_aware_utc(created_at)
        prev_reply_at = _as_aware_utc(recipient.get("reply_at"))
        if event_at and (prev_reply_at is None or event_at > prev_reply_at):
            recipient["reply_at"] = event_at

    summary = {
        "total_recipients": len(recipients),
        "pending": 0,
        "sent": 0,
        "delivered": 0,
        "read": 0,
        "failed": 0,
        "clicked": 0,
        "replied": 0,
    }

    for recipient in recipients.values():
        recipient["sent"] = bool(recipient["sent"] or recipient["delivered"] or recipient["read"] or recipient["failed"])
        recipient["delivered"] = bool(recipient["delivered"] or recipient["read"])
        if recipient["read"] or recipient["delivered"]:
            recipient["failed"] = False

        if recipient["failed"]:
            recipient["status_label"] = "failed"
        elif recipient["replied"]:
            recipient["status_label"] = "replied"
        elif recipient["clicked"]:
            recipient["status_label"] = "clicked"
        elif recipient["read"]:
            recipient["status_label"] = "read"
        elif recipient["delivered"]:
            recipient["status_label"] = "delivered"
        elif recipient["sent"]:
            recipient["status_label"] = "sent"
        else:
            recipient["status_label"] = "pending"

        summary["sent"] += int(recipient["sent"])
        summary["delivered"] += int(recipient["delivered"])
        summary["read"] += int(recipient["read"])
        summary["failed"] += int(recipient["failed"])
        summary["clicked"] += int(recipient["clicked"])
        summary["replied"] += int(recipient["replied"])
        summary["pending"] += int(not recipient["sent"] and not recipient["failed"])

    sent = summary["sent"]
    total = max(summary["total_recipients"], 1)
    summary["progress_percent"] = int((sent / total) * 100)
    summary["delivery_rate"] = int((summary["delivered"] / sent) * 100) if sent > 0 else 0
    summary["read_rate"] = int((summary["read"] / sent) * 100) if sent > 0 else 0
    summary["failure_rate"] = int((summary["failed"] / max(summary["total_recipients"], 1)) * 100)
    summary["click_rate"] = int((summary["clicked"] / sent) * 100) if sent > 0 else 0
    summary["reply_rate"] = int((summary["replied"] / sent) * 100) if sent > 0 else 0
    summary["tracking_enabled"] = campaign_tracking_enabled if campaign_tracking_seen else True
    summary["has_trackable_url"] = campaign_has_trackable_url

    serialized_recipients = []
    for rec in recipients.values():
        serialized_recipients.append(
            {
                **rec,
                "last_event_at": _safe_dt_iso(rec.get("last_event_at")),
                "reply_at": _safe_dt_iso(rec.get("reply_at")),
            }
        )

    serialized_recipients.sort(
        key=lambda r: (
            r.get("status_label") != "failed",
            r.get("status_label") != "pending",
            r.get("phone_number") or "",
        )
    )

    return summary, serialized_recipients, enrollment_by_phone


def _recipient_matches_segment(recipient, segment):
    if segment in (None, "", "all"):
        return True
    segment = str(segment).strip().lower()
    if segment == "pending":
        return bool(not recipient.get("sent") and not recipient.get("failed"))
    if segment == "sent":
        return bool(recipient.get("sent"))
    if segment == "delivered":
        return bool(recipient.get("delivered") and not recipient.get("failed"))
    if segment == "read":
        return bool(recipient.get("read") and not recipient.get("failed"))
    if segment == "failed":
        return bool(recipient.get("failed"))
    if segment == "clicked":
        return bool(recipient.get("clicked") and not recipient.get("failed"))
    if segment == "replied":
        return bool(recipient.get("replied") and not recipient.get("failed"))
    return True


def _recipient_matches_smart_filter(recipient, smart_filter):
    if not smart_filter:
        return True

    mode = str(smart_filter).strip().lower()
    if mode == "clicked_not_replied":
        return bool(recipient.get("clicked") and not recipient.get("replied"))
    if mode == "read_not_clicked":
        return bool(recipient.get("read") and not recipient.get("clicked"))
    if mode == "delivered_not_read":
        return bool(recipient.get("delivered") and not recipient.get("read"))
    return True

@bulk_bp.route("/campaigns", methods=["GET"])
def list_campaigns():
    """List bulk messaging campaigns (trigger_type='manual')."""
    workspace_id = request.args.get("workspace_id")
    status = request.args.get("status")
    
    if not workspace_id:
        return jsonify({"success": False, "error": "Workspace ID required"}), 400
        
    query = WhatsAppDripCampaign.query.filter_by(
        workspace_id=workspace_id,
        trigger_type="manual"  # Filter for bulk campaigns
    )
    
    if status and status != "all":
        query = query.filter_by(status=status)
        
    campaigns = query.order_by(WhatsAppDripCampaign.created_at.desc()).all()
    
    # Batch-fetch message status counts for all campaign IDs
    campaign_ids = [c.id for c in campaigns]
    msg_stats_query = db.session.query(
        WhatsAppMessage.campaign_id,
        WhatsAppMessage.status,
        func.count(distinct(WhatsAppMessage.conversation_id))
    ).filter(
        WhatsAppMessage.campaign_id.in_(campaign_ids)
    ).group_by(WhatsAppMessage.campaign_id, WhatsAppMessage.status).all()
    
    # Build {campaign_id: {status: count}} lookup
    campaign_msg_counts = {}
    for cid, status_val, cnt in msg_stats_query:
        campaign_msg_counts.setdefault(cid, {})[status_val] = cnt
    
    result = []
    for c in campaigns:
        # Calculate stats on the fly or use cached columns
        c_dict = c.to_dict()
        
        # Add extra stats needed for bulk view
        total = c.enrolled_count
        sent = c.completed_count # Approx for now
        
        # Fetch template info from first step if available
        first_step = c.steps[0] if c.steps else None
        if first_step:
            c_dict["template_name"] = first_step.template_name
            c_dict["language"] = first_step.language
        else:
            c_dict["template_name"] = None
            c_dict["language"] = None
            
        # Map trigger_value to scheduled_at for manual campaigns
        c_dict["scheduled_at"] = c.trigger_value if c.trigger_type == "manual" else None
        
        # Compute real delivery/read rates from message statuses
        counts = campaign_msg_counts.get(c.id, {})
        delivered = counts.get("delivered", 0) + counts.get("read", 0)
        read = counts.get("read", 0)
        failed = counts.get("failed", 0)
        
        c_dict.update({
             "total_recipients": total,
             "sent_count": sent,
             "pending_count": total - sent,
             "progress_percent": int((sent / total * 100)) if total > 0 else 0,
             "delivery_rate": int((delivered / sent * 100)) if sent > 0 else 0,
             "read_rate": int((read / sent * 100)) if sent > 0 else 0,
             "failed_count": failed,
        })
        result.append(c_dict)
        
    return jsonify({"success": True, "campaigns": result})

@bulk_bp.route("/campaigns", methods=["POST"])
@rate_limit("whatsapp.bulk.create")
@require_account_access
def create_campaign(account: WhatsAppAccount, workspace_id: str):
    """Create a new bulk campaign."""
    data = request.get_json() or {}
    account_id = account.id
    name = data.get("name")
    
    print(f"DEBUG: create_campaign payload: {data}")
    
    if not name:
        return jsonify({"success": False, "error": "Name is required"}), 400
        
    template_name = data.get("template_name")
    if not template_name:
        return jsonify({"success": False, "error": "Template is required for bulk campaigns"}), 400

    bc = broadcast_capability_check(account_id)
    if not bc.ok:
        return jsonify({"success": False, "error": bc.message, "code": "CAPABILITY_DENIED"}), 403
        
    from .safe_mode_engine import is_risk_allowed, RiskClass
    if not is_risk_allowed(account, RiskClass.HIGH):
        return jsonify({"success": False, "error": f"Bulk messaging is temporarily disabled by Safe Mode ({account.operational_mode}) to protect your account reputation.", "code": "SAFE_MODE_DENIED"}), 403
        
    # Create campaign
    campaign = WhatsAppDripCampaign(
        workspace_id=workspace_id,
        account_id=account_id,
        name=name,
        description=data.get("description", ""),
        trigger_type="manual",
        status="draft",
        created_at=datetime.now(timezone.utc)
    )
    
    db.session.add(campaign)
    db.session.flush() # Get ID
    
    # If template is selected, add as Step 1
    template_name = data.get("template_name")
    if template_name:
        step = WhatsAppDripStep(
            campaign_id=campaign.id,
            step_order=1,
            template_name=template_name,
            language=data.get("template_language", "en_US"),
            delay_seconds=0
        )
        db.session.add(step)
        
    db.session.commit()
    
    return jsonify({"success": True, "campaign": campaign.to_dict()})

@bulk_bp.route("/campaigns/<int:campaign_id>", methods=["GET"])
def get_campaign(campaign_id: int):
    """Get single bulk campaign details."""
    workspace_id = request.args.get("workspace_id")
    
    campaign = WhatsAppDripCampaign.query.get_or_404(campaign_id)
    
    if workspace_id and str(campaign.workspace_id) != str(workspace_id):
        return jsonify({"success": False, "error": "Access denied"}), 403
        
    return jsonify({"success": True, "campaign": campaign.to_dict()})

@bulk_bp.route("/campaigns/<int:campaign_id>/stats", methods=["GET"])
def get_campaign_stats(campaign_id: int):
    """Get live stats for a campaign."""
    workspace_id = request.args.get("workspace_id")
    campaign = WhatsAppDripCampaign.query.get_or_404(campaign_id)

    if workspace_id and str(campaign.workspace_id) != str(workspace_id):
        return jsonify({"success": False, "error": "Access denied"}), 403

    try:
        # Enrollment-level stats
        total = WhatsAppDripEnrollment.query.filter_by(campaign_id=campaign_id).count()
        completed = WhatsAppDripEnrollment.query.filter_by(campaign_id=campaign_id, status="completed").count()
        enrollment_failed = WhatsAppDripEnrollment.query.filter_by(campaign_id=campaign_id, status="failed").count()
        active = WhatsAppDripEnrollment.query.filter_by(campaign_id=campaign_id, status="active").count()
        blocked = WhatsAppDripEnrollment.query.filter_by(campaign_id=campaign_id, status="blocked_missing_data").count()

        # Real stats from messages table - counts actual delivery outcomes from webhooks
        msg_stats = db.session.query(
            WhatsAppMessage.status,
            func.count(distinct(WhatsAppMessage.conversation_id))
        ).filter(
            WhatsAppMessage.campaign_id == campaign_id
        ).group_by(WhatsAppMessage.status).all()

        msg_counts = {s: c for s, c in msg_stats}

        # Message-level delivery failures (from Meta webhook, e.g. error 130472, 131049)
        msg_failed = msg_counts.get("failed", 0)
        # Delivered = delivered + read (read implies delivered)
        delivered = msg_counts.get("delivered", 0) + msg_counts.get("read", 0)
        read = msg_counts.get("read", 0)

        # Total failed = enrollment-level failures + message-level delivery failures
        total_failed = enrollment_failed + blocked + msg_failed
        # Sent = completed enrollments (API accepted the message)
        sent = completed

        summary, _, _ = _build_campaign_intelligence(campaign)

        stats = {
            "total_recipients": total,
            "sent": sent,
            "failed": total_failed,
            "pending": active,
            "queued": 0,
            "delivered": delivered,
            "read": read,
            "clicked": summary.get("clicked", 0),
            "replied": summary.get("replied", 0),
            "progress_percent": int((completed / total * 100)) if total > 0 else 0,
            "delivery_rate": int((delivered / sent * 100)) if sent > 0 else 0,
            "read_rate": int((read / sent * 100)) if sent > 0 else 0,
            "failure_rate": int((total_failed / total * 100)) if total > 0 else 0,
            "click_rate": summary.get("click_rate", 0),
            "reply_rate": summary.get("reply_rate", 0),
        }

        return jsonify({"success": True, "stats": stats})
    except Exception:
        logger.exception("Failed to compute campaign stats for campaign_id=%s", campaign_id)
        return jsonify({"success": True, "stats": {
            "total_recipients": 0,
            "sent": 0,
            "failed": 0,
            "pending": 0,
            "queued": 0,
            "delivered": 0,
            "read": 0,
            "clicked": 0,
            "replied": 0,
            "progress_percent": 0,
            "delivery_rate": 0,
            "read_rate": 0,
            "failure_rate": 0,
            "click_rate": 0,
            "reply_rate": 0,
            "degraded": True,
        }}), 200


@bulk_bp.route("/campaigns/<int:campaign_id>/summary", methods=["GET"])
def get_campaign_summary(campaign_id: int):
    """Return canonical recipient summary counts for campaign intelligence tabs."""
    workspace_id = request.args.get("workspace_id")

    campaign = WhatsAppDripCampaign.query.get_or_404(campaign_id)
    if workspace_id and str(campaign.workspace_id) != str(workspace_id):
        return jsonify({"success": False, "error": "Access denied"}), 403

    summary, _, _ = _build_campaign_intelligence(campaign)

    payload = {
        "all": int(summary.get("total_recipients", 0) or 0),
        "pending": int(summary.get("pending", 0) or 0),
        "sent": int(summary.get("sent", 0) or 0),
        "delivered": int(summary.get("delivered", 0) or 0),
        "read": int(summary.get("read", 0) or 0),
        "clicked": int(summary.get("clicked", 0) or 0),
        "replied": int(summary.get("replied", 0) or 0),
        "failed": int(summary.get("failed", 0) or 0),
        "as_of": datetime.now(timezone.utc).isoformat(),
    }

    return jsonify(
        {
            "success": True,
            "campaign_id": campaign.id,
            "summary": payload,
        }
    )


@bulk_bp.route("/campaigns/<int:campaign_id>/intelligence", methods=["GET"])
def get_campaign_intelligence(campaign_id: int):
    """Recipient-level intelligence for all campaign segments."""
    workspace_id = request.args.get("workspace_id")
    segment = request.args.get("segment", "all")
    smart_filter = request.args.get("smart_filter")
    search = (request.args.get("search") or "").strip().lower()
    limit = min(max(int(request.args.get("limit", 300)), 1), 1000)
    offset = max(int(request.args.get("offset", 0)), 0)

    campaign = WhatsAppDripCampaign.query.get_or_404(campaign_id)
    if workspace_id and str(campaign.workspace_id) != str(workspace_id):
        return jsonify({"success": False, "error": "Access denied"}), 403

    summary, recipients, _ = _build_campaign_intelligence(campaign)
    filtered = []
    for rec in recipients:
        if not _recipient_matches_segment(rec, segment):
            continue
        if not _recipient_matches_smart_filter(rec, smart_filter):
            continue
        if search:
            haystack = f"{rec.get('phone_number') or ''} {rec.get('name') or ''} {rec.get('reply_preview') or ''}".lower()
            if search not in haystack:
                continue
        filtered.append(rec)

    paged = filtered[offset:offset + limit]

    return jsonify(
        {
            "success": True,
            "campaign_id": campaign.id,
            "segment": segment,
            "summary": summary,
            "total_filtered": len(filtered),
            "recipients": paged,
        }
    )


@bulk_bp.route("/campaigns/<int:campaign_id>/retarget", methods=["POST"])
def retarget_campaign_recipients(campaign_id: int):
    """Create a new campaign from selected/segmented recipients and send immediately."""
    workspace_id = request.args.get("workspace_id") or (request.get_json() or {}).get("workspace_id")
    payload = request.get_json() or {}
    segment = str(payload.get("segment") or "all")
    smart_filter = payload.get("smart_filter")
    explicit_phones = payload.get("phone_numbers") or []
    has_explicit_selection = isinstance(explicit_phones, list) and len(explicit_phones) > 0
    send_now = bool(payload.get("send_now", True))

    campaign = WhatsAppDripCampaign.query.get_or_404(campaign_id)
    if workspace_id and str(campaign.workspace_id) != str(workspace_id):
        return jsonify({"success": False, "error": "Access denied"}), 403

    summary, recipients, enrollment_by_phone = _build_campaign_intelligence(campaign)

    selected_phones = set()
    if has_explicit_selection:
        for raw_phone in explicit_phones:
            normalized = normalize_phone_robust(raw_phone)
            if normalized:
                selected_phones.add(normalized)
    else:
        for rec in recipients:
            if _recipient_matches_segment(rec, segment):
                if _recipient_matches_smart_filter(rec, smart_filter):
                    selected_phones.add(rec["phone_number"])

    if not selected_phones:
        return jsonify({"success": False, "error": "No matching recipients to retarget"}), 400

    selection_label = "selected" if has_explicit_selection else segment

    new_campaign_name = (
        str(payload.get("name") or "").strip()
        or f"{campaign.name} - Retarget ({selection_label})"
    )

    new_campaign = WhatsAppDripCampaign(
        workspace_id=campaign.workspace_id,
        account_id=campaign.account_id,
        name=new_campaign_name,
        description=f"Retarget from campaign #{campaign.id} segment '{selection_label}'",
        trigger_type="manual",
        status="draft",
        created_at=datetime.now(timezone.utc),
    )
    db.session.add(new_campaign)
    db.session.flush()

    original_step = (
        WhatsAppDripStep.query.filter_by(campaign_id=campaign.id)
        .order_by(WhatsAppDripStep.step_order.asc())
        .first()
    )
    if not original_step:
        return jsonify({"success": False, "error": "Original campaign has no step to reuse"}), 400

    cloned_step = WhatsAppDripStep(
        campaign_id=new_campaign.id,
        step_order=1,
        template_name=original_step.template_name,
        language=original_step.language,
        delay_seconds=0,
    )
    db.session.add(cloned_step)

    now = datetime.now(timezone.utc)
    added = 0
    for phone in sorted(selected_phones):
        original_enrollment = enrollment_by_phone.get(phone)
        enrollment = WhatsAppDripEnrollment(
            campaign_id=new_campaign.id,
            phone_number=phone,
            current_step_order=0,
            status="active",
            variables=(original_enrollment.variables if original_enrollment else {}) or {},
            created_at=now,
            next_run_at=now,
        )
        db.session.add(enrollment)
        added += 1

    new_campaign.enrolled_count = added

    if send_now:
        new_campaign.status = "scheduled"
        new_campaign.trigger_value = datetime.now(timezone.utc).isoformat()

    db.session.commit()

    try:
        notification_manager.broadcast(
            "whatsapp_bulk_retarget_created",
            {
                "workspace_id": str(campaign.workspace_id),
                "campaign_id": new_campaign.id,
                "source_campaign_id": campaign.id,
                "segment": segment,
                "smart_filter": smart_filter,
                "recipient_count": added,
                "status": new_campaign.status,
            },
        )
    except Exception:
        logger.debug("Failed to broadcast retarget event", exc_info=True)

    return jsonify(
        {
            "success": True,
            "campaign_id": new_campaign.id,
            "campaign": new_campaign.to_dict(),
            "recipient_count": added,
            "segment": segment,
            "smart_filter": smart_filter,
            "source_campaign_id": campaign.id,
        }
    )


@bulk_bp.route("/campaigns/<int:campaign_id>/failed", methods=["GET"])
def get_campaign_failed_messages(campaign_id: int):
    """Get details of failed message deliveries for a campaign."""
    campaign = WhatsAppDripCampaign.query.get_or_404(campaign_id)
    
    # Get messages that failed delivery (webhook reported failure)
    failed_messages = db.session.query(
        WhatsAppMessage.id,
        WhatsAppMessage.error_code,
        WhatsAppMessage.error_message,
        WhatsAppMessage.created_at,
        WhatsAppConversation.user_phone
    ).join(
        WhatsAppConversation, WhatsAppMessage.conversation_id == WhatsAppConversation.id
    ).filter(
        WhatsAppMessage.campaign_id == campaign_id,
        WhatsAppMessage.status == "failed"
    ).all()
    
    # Get enrollments that failed at enrollment level
    failed_enrollments = WhatsAppDripEnrollment.query.filter(
        WhatsAppDripEnrollment.campaign_id == campaign_id,
        WhatsAppDripEnrollment.status.in_(["failed", "blocked_missing_data"])
    ).all()
    
    failures = []
    for msg in failed_messages:
        failures.append({
            "phone": msg.user_phone,
            "error_code": msg.error_code,
            "error_message": msg.error_message,
            "type": "delivery_failed",
            "timestamp": msg.created_at.isoformat() + "Z" if msg.created_at else None
        })
    for enr in failed_enrollments:
        failures.append({
            "phone": enr.phone_number,
            "error_code": None,
            "error_message": enr.status_reason or enr.status,
            "type": "enrollment_failed",
            "timestamp": None
        })
    
    return jsonify({"success": True, "failures": failures})

@bulk_bp.route("/campaigns/<int:campaign_id>/resubscribe-webhooks", methods=["POST"])
def resubscribe_campaign_webhooks(campaign_id: int):
    """Re-subscribe WABA webhooks for a campaign's account. 
    
    Use this to fix webhook delivery issues when status updates stop arriving.
    """
    campaign = WhatsAppDripCampaign.query.get_or_404(campaign_id)
    account = WhatsAppAccount.query.get(campaign.account_id)
    
    if not account:
        return jsonify({"success": False, "error": "Account not found"}), 404
    
    access_token = account.get_access_token()
    if not access_token:
        return jsonify({"success": False, "error": "No access token for account"}), 400
    
    from .health_check import subscribe_waba_to_webhooks
    success, message, details = subscribe_waba_to_webhooks(account.waba_id, access_token)
    
    return jsonify({
        "success": success,
        "message": message,
        "details": details,
        "waba_id": account.waba_id,
        "account_phone": account.display_phone_number,
    }), 200 if success else 400


@bulk_bp.route("/crm-audience", methods=["GET"])
def get_crm_audience():
    """Fetch CRM audience (Leads or Contacts) for bulk messaging."""
    workspace_id = request.args.get("workspace_id")
    source = request.args.get("source", "leads") # leads or contacts
    search = request.args.get("search")
    
    if not workspace_id:
        return jsonify({"success": False, "error": "Workspace ID required"}), 400
        
    # Access dynamic CRM models
    from flask import current_app
    crm_models = getattr(current_app, "crm_models", None)
    
    if not crm_models:
        return jsonify({"success": False, "error": "CRM models not initialized"}), 500
        
    model = crm_models["Lead"] if source == "leads" else crm_models["Contact"]
    
    query = model.query.filter_by(workspace_id=workspace_id)
    
    if search:
        search_term = f"%{search}%"
        query = query.filter(
            db.or_(
                model.name.ilike(search_term),
                model.phone.ilike(search_term),
                model.email.ilike(search_term)
            )
        )
        
    # Fetch ALL records (no limit as requested)
    records = query.order_by(model.created_at.desc()).all()
    
    audience = []
    
    # Summary stats
    total = len(records)
    with_phone = 0
    whatsapp_ready = 0
    
    for r in records:
        # Normalize phone using robust normalizer
        raw_phone = r.phone or ""
        norm_phone = normalize_phone_robust(raw_phone)
        
        # Valid if normalizer returned a result
        is_valid = norm_phone is not None
        
        if raw_phone:
            with_phone += 1
            if is_valid:
                whatsapp_ready += 1
        
        audience.append({
            "id": r.id,
            "name": r.name,
            "phone": raw_phone,
            "phone_normalized": norm_phone,
            "email": r.email,
            "company": r.company,
            "status": r.status,
            "source": getattr(r, "source", None) or getattr(r, "external_source", None),
            "whatsapp_ready": is_valid,
            "created_at": r.created_at.isoformat() if r.created_at else None
        })
        
    return jsonify({
        "success": True, 
        "audience": audience,
        "summary": {
            "total": total,
            "with_phone": with_phone,
            "whatsapp_ready": whatsapp_ready
        }
    })
@bulk_bp.route("/campaigns/<int:campaign_id>/recipients", methods=["POST"])
@rate_limit("whatsapp.bulk.recipients")
def add_recipients(campaign_id: int):
    """Add recipients to a bulk campaign."""
    workspace_id = request.args.get("workspace_id")
    data = request.get_json() or {}
    recipients = data.get("recipients", [])
    enable_tracking_url = bool(data.get("enable_tracking_url", True))
    
    if not recipients:
        return jsonify({"success": False, "error": "No recipients provided"}), 400
        
    campaign = WhatsAppDripCampaign.query.get_or_404(campaign_id)
    
    if workspace_id and str(campaign.workspace_id) != str(workspace_id):
        return jsonify({"success": False, "error": "Access denied"}), 403
        
    added_count = 0
    duplicates_count = 0
    invalid_count = 0
    
    for r in recipients:
        phone = r.get("phone_number")
        if not phone:
            invalid_count += 1
            continue

        # Robust normalization: handles multi-number, country codes, Mexico/Argentina, etc.
        # Use the row's country code (when the dataset provides one) so bare national numbers
        # are normalized for their actual country instead of defaulting to India (+91).
        row_cc = r.get("country_code") or r.get("countryCode")
        clean_phone = normalize_phone_robust(phone, country_code=row_cc)
        if not clean_phone:
            invalid_count += 1
            continue
            
        # Check for duplicate in this campaign
        existing = WhatsAppDripEnrollment.query.filter_by(
            campaign_id=campaign_id,
            phone_number=clean_phone
        ).first()
        
        if existing:
            # Update variables if needed
            merged_params = dict(r.get("params", {}) or {})
            merged_params["__enable_tracking_url"] = enable_tracking_url
            existing.variables = merged_params
            existing.name = r.get("name")
            duplicates_count += 1
        else:
            merged_params = dict(r.get("params", {}) or {})
            merged_params["__enable_tracking_url"] = enable_tracking_url
            enrollment = WhatsAppDripEnrollment(
                campaign_id=campaign_id,
                phone_number=clean_phone,
                current_step_order=0,
                status="active",
                variables=merged_params,
                created_at=datetime.now(timezone.utc),
                next_run_at=datetime.now(timezone.utc)
            )
            db.session.add(enrollment)
            added_count += 1
            
    db.session.commit()
    
    # Update stats
    campaign.enrolled_count = WhatsAppDripEnrollment.query.filter_by(campaign_id=campaign_id).count()
    db.session.commit()
    
    return jsonify({
        "success": True, 
        "added": added_count, 
        "duplicates": duplicates_count, 
        "invalid": invalid_count
    })

@bulk_bp.route("/campaigns/<int:campaign_id>/schedule", methods=["POST"])
@rate_limit("whatsapp.bulk.schedule")
def schedule_campaign(campaign_id: int):
    """Schedule or send a campaign."""
    workspace_id = request.args.get("workspace_id") or (request.get_json() or {}).get("workspace_id")
    data = request.get_json() or {}
    
    campaign = WhatsAppDripCampaign.query.get_or_404(campaign_id)
    
    if workspace_id and str(campaign.workspace_id) != str(workspace_id):
        return jsonify({"success": False, "error": f"Access denied. Campaign WS: {campaign.workspace_id}, Req WS: {workspace_id}"}), 403
        
    scheduled_at_str = data.get("scheduled_at")
    
    if scheduled_at_str:
        try:
             # Parse ISO format
            scheduled_at = datetime.fromisoformat(scheduled_at_str.replace('Z', '+00:00'))
            # If naive, assume UTC (or handle timezone awareness properly based on app config)
            if scheduled_at.tzinfo is None:
                scheduled_at = scheduled_at.replace(tzinfo=timezone.utc)
                
            # If scheduling for future
            campaign.status = "scheduled"
            # Store scheduled_at in trigger_value (repurposing unused field for manual campaigns)
            campaign.trigger_value = scheduled_at.isoformat()
                
        except ValueError:
            return jsonify({"success": False, "error": "Invalid date format"}), 400
    else:
        # Send now -> Set status to 'scheduled' immediately so the next tick picks it up
        campaign.status = "scheduled"
        campaign.trigger_value = datetime.now(timezone.utc).isoformat()
        logger.info(f"Send Now: Campaign {campaign.id} set to SCHEDULED for immediate pickup")

        
    db.session.commit()
    
    # TODO: Trigger background job to start sending if 'running'
    
    return jsonify({"success": True, "status": campaign.status})

@bulk_bp.route("/campaigns/<int:campaign_id>/pause", methods=["POST"])
def pause_campaign(campaign_id: int):
    """Pause a running campaign."""
    workspace_id = request.args.get("workspace_id") or (request.get_json() or {}).get("workspace_id")
    
    campaign = WhatsAppDripCampaign.query.get_or_404(campaign_id)
    
    if workspace_id and str(campaign.workspace_id) != str(workspace_id):
        return jsonify({"success": False, "error": "Access denied"}), 403
        
    if campaign.status == "running":
        campaign.status = "paused"
        db.session.commit()
        
    return jsonify({"success": True, "status": campaign.status})

@bulk_bp.route("/campaigns/<int:campaign_id>/resume", methods=["POST"])
def resume_campaign(campaign_id: int):
    """Resume a paused campaign."""
    workspace_id = request.args.get("workspace_id") or (request.get_json() or {}).get("workspace_id")
    
    campaign = WhatsAppDripCampaign.query.get_or_404(campaign_id)
    
    if workspace_id and str(campaign.workspace_id) != str(workspace_id):
        return jsonify({"success": False, "error": "Access denied"}), 403
        
    if campaign.status == "paused":
        campaign.status = "running"
        db.session.commit()
        
    return jsonify({"success": True, "status": campaign.status})

@bulk_bp.route("/campaigns/<int:campaign_id>", methods=["DELETE"])
def delete_campaign(campaign_id: int):
    """Delete a campaign."""
    workspace_id = request.args.get("workspace_id") or (request.get_json() or {}).get("workspace_id")
    force = request.args.get("force") == "true"
    
    campaign = WhatsAppDripCampaign.query.get_or_404(campaign_id)
    
    if workspace_id and str(campaign.workspace_id) != str(workspace_id):
        return jsonify({"success": False, "error": "Access denied"}), 403
        
    if campaign.status == "running" and not force:
        return jsonify({"success": False, "error": "Cannot delete running campaign. Stop it first or use force=true"}), 400
        
    # Delete enrollments first
    WhatsAppDripEnrollment.query.filter_by(campaign_id=campaign_id).delete()
    
    # Delete steps
    WhatsAppDripStep.query.filter_by(campaign_id=campaign_id).delete()
    
    db.session.delete(campaign)
    db.session.commit()
    
    return jsonify({"success": True, "message": "Campaign deleted"})
